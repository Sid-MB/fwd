"""Slurm backend tests — entirely offline, driven by a duck-typed fake endpoint.

Design intent
-------------
No cluster is available (and none should be needed): every remote interaction in the backend goes through
``SSHEndpoint.run``, so a :class:`FakeEndpoint` that records command strings and replays canned stdout exercises the
real code paths. Tests assert on the *commands issued*, which is exactly the contract that breaks in production.

The quoting tests go one step further than string comparison: they execute the generated ``job.sh`` with a stub
``salloc`` on ``PATH`` that dumps its argv NUL-separated. That turns "does the payload survive four layers of shell
interpretation" from a guess into an executed fact, which is worth the two extra lines of fixture.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from fwd.backends import slurm_job
from fwd.backends.base import ProvisionError, TargetStatus
from fwd.backends.slurm import SlurmBackend, map_slurm_state
from fwd.config import Config, SlurmTargetConfig
from fwd.sshexec import SSHError
from fwd.state import SessionState

# --------------------------------------------------------------------------- fixtures


class FakeEndpoint:
    """Duck-typed stand-in for :class:`fwd.sshexec.SSHEndpoint` that records commands and replays canned output.

    ``rules`` maps a substring of the command to ``(returncode, stdout)``; the first match wins, and anything
    unmatched succeeds with empty output. ``check=True`` raises :class:`SSHError` exactly like the real endpoint, so
    error handling is tested rather than assumed. ``unreachable=True`` simulates a dead login node.
    """

    def __init__(self, host: str = "login1.hpc.example", *, rules: dict[str, tuple[int, str]] | None = None, unreachable: bool = False) -> None:
        self.host = host
        self.user = "sid"
        self.rules = rules or {}
        self.unreachable = unreachable
        self.calls: list[str] = []

    def run(self, cmd: str, *, check: bool = True, capture: bool = True, timeout: float | None = None, env: dict[str, str] | None = None) -> CompletedProcess[str]:
        self.calls.append(cmd)
        if self.unreachable:
            raise SSHError(f"ssh to {self.host} failed: Connection timed out")
        for needle, (code, out) in self.rules.items():
            if needle in cmd:
                if check and code != 0:
                    raise SSHError(f"remote command failed (exit {code}): {cmd}")
                return CompletedProcess(args=cmd, returncode=code, stdout=out, stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def saw(self, needle: str) -> bool:
        """Return whether any issued command contained ``needle``."""
        return any(needle in call for call in self.calls)


def make_target(**overrides: Any) -> SlurmTargetConfig:
    """Build a representative cluster target; overrides keep individual tests to one meaningful line."""
    base: dict[str, Any] = {
        "name": "cluster",
        "login_host": "login.hpc.example",
        "user": "sid",
        "remote_base": "/scratch/sid/fwd",
        "alloc": "--time=04:00:00 --cpus-per-task=4",
        "env_setup": [],
    }
    base.update(overrides)
    return SlurmTargetConfig(**base)


def make_backend(**overrides: Any) -> SlurmBackend:
    return SlurmBackend(make_target(**overrides), Config())


def make_session(**overrides: Any) -> SessionState:
    base: dict[str, Any] = {
        "name": "demo",
        "backend": "slurm",
        "local_cwd": "/home/sid/demo",
        "remote_dir": "/scratch/sid/fwd/demo",
        "tmux_session": "fwd-demo",
        "endpoint": {"host": "login.hpc.example", "user": "sid"},
        "backend_ids": {"login_host": "login1.hpc.example"},
    }
    base.update(overrides)
    return SessionState(**base)


def pin(backend: SlurmBackend, endpoints: dict[str, FakeEndpoint]) -> None:
    """Route the backend's endpoint construction to fakes keyed by hostname."""
    backend._endpoint_for = lambda host: endpoints[host]  # type: ignore[method-assign]


def salloc_argv_from_script(script: str, tmp_path: Path) -> list[str]:
    """Execute a rendered ``job.sh`` with a stub ``salloc`` and return the argv it really received.

    This is the ground truth for the quoting tests: whatever bash hands the stub is what a real ``salloc`` would get.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "salloc"
    stub.write_text('#!/bin/bash\nfor a in "$@"; do printf "%s\\0" "$a"; done\n', encoding="utf-8")
    stub.chmod(0o755)
    path = tmp_path / "job.sh"
    path.write_text(script, encoding="utf-8")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", str(path)], capture_output=True, text=True, env=env, check=True)
    return proc.stdout.split("\0")[:-1]


# --------------------------------------------------------------------------- job script rendering


def test_job_script_shape() -> None:
    """The script sets strict mode, sources fwd-env.sh from the tool prefix, and execs salloc."""
    script = slurm_job.render_job_script(make_target(), "demo", "/scratch/sid/fwd/demo", "/scratch/sid/fwd/.fwd-tools", "claude")
    assert script.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in script
    assert "/scratch/sid/fwd/.fwd-tools/fwd-env.sh" in script
    assert "\nexec salloc " in script
    assert script.endswith("\n")


def test_job_script_argv_basic(tmp_path: Path) -> None:
    """Executed for real: salloc receives alloc flags, the job name, and one payload argument."""
    script = slurm_job.render_job_script(make_target(), "demo", "/scratch/sid/fwd/demo", "/tools", "claude")
    argv = salloc_argv_from_script(script, tmp_path)
    assert argv[:5] == ["--time=04:00:00", "--cpus-per-task=4", "-J", "fwd-demo", "srun"]
    assert argv[5:8] == ["--pty", "bash", "-lc"]
    assert len(argv) == 9
    assert argv[8].endswith("; cd /scratch/sid/fwd/demo; claude")


def test_job_script_payload_survives_quotes_and_spaces(tmp_path: Path) -> None:
    """env_setup lines with spaces/quotes and a claude_cmd with double quotes reach salloc byte-for-byte."""
    target = make_target(env_setup=["module load python/3.12", "export FOO='a b c'", 'export BAR="x y"'])
    claude_cmd = 'claude "Read HANDOFF.md and continue; don\'t ask"'
    script = slurm_job.render_job_script(target, "de mo", "/scratch/sid/my project", "/tools", claude_cmd)
    payload = salloc_argv_from_script(script, tmp_path)[-1]
    assert "module load python/3.12 && export FOO='a b c' && export BAR=\"x y\"" in payload
    assert "cd '/scratch/sid/my project'" in payload
    assert payload.endswith(claude_cmd)
    # The payload is one argument, so the compute-node shell sees the command exactly as written.
    assert shlex.split(f"bash -lc {shlex.quote(payload)}")[-1] == payload


def test_job_script_payload_executes_as_written(tmp_path: Path) -> None:
    """End-to-end: bash -lc on the rendered payload runs the claude command with its quoting intact."""
    target = make_target(env_setup=["export FWD_TEST='a b'"])
    payload = slurm_job.render_payload(target, str(tmp_path), "/tools", 'printf "%s|%s" "$FWD_TEST" "it\'s fine"')
    proc = subprocess.run(["bash", "-lc", payload], capture_output=True, text=True, check=True)
    assert proc.stdout.endswith("a b|it's fine")


def test_job_name_and_partition_account_injection(tmp_path: Path) -> None:
    """partition/account are emitted only when configured, before the srun tail."""
    plain = salloc_argv_from_script(slurm_job.render_job_script(make_target(), "demo", "/d", "/t", "claude"), tmp_path)
    assert not [a for a in plain if a.startswith("--partition") or a.startswith("--account")]

    target = make_target(partition="gpu-short", account="proj 42")
    argv = salloc_argv_from_script(slurm_job.render_job_script(target, "demo", "/d", "/t", "claude"), tmp_path)
    assert argv.index("--partition=gpu-short") < argv.index("srun")
    assert "--account=proj 42" in argv


def test_alloc_template_with_gres_is_preserved(tmp_path: Path) -> None:
    """A template that already requests GPUs is passed through untouched, even with --gpu set."""
    target = make_target(alloc="--time=08:00:00 --gres=gpu:a100:2 --mem=64G")
    argv = salloc_argv_from_script(slurm_job.render_job_script(target, "demo", "/d", "/t", "claude", gpu="4"), tmp_path)
    assert argv[:4] == ["--time=08:00:00", "--gres=gpu:a100:2", "--mem=64G", "-J"]
    assert len([a for a in argv if a.startswith("--gres")]) == 1


@pytest.mark.parametrize(
    ("gpu", "expected"),
    [(None, []), ("2", ["--gres=gpu:2"]), ("a100", ["--gres=gpu:a100:1"])],
)
def test_gpu_override_appends_gres(gpu: str | None, expected: list[str]) -> None:
    argv = slurm_job.effective_alloc("--time=01:00:00", gpu)
    assert argv == ["--time=01:00:00", *expected]


def test_tmux_command_points_at_job_script() -> None:
    assert slurm_job.render_tmux_command("/scratch/sid/fwd/demo") == "bash /scratch/sid/fwd/demo/.fwd/job.sh"
    assert slurm_job.render_tmux_command("/scratch/my dir/") == "bash '/scratch/my dir/.fwd/job.sh'"


# --------------------------------------------------------------------------- provision / pinning


def test_provision_pins_concrete_login_host() -> None:
    """First connect resolves the round-robin alias to a concrete node and builds the endpoint against it."""
    backend = make_backend()
    alias = FakeEndpoint("login.hpc.example", rules={"hostname": (0, "login3.hpc.example\n")})
    pinned = FakeEndpoint("login3.hpc.example")
    pin(backend, {"login.hpc.example": alias, "login3.hpc.example": pinned})

    info = backend.provision("demo", "myproj")

    assert info.endpoint.host == "login3.hpc.example"
    assert info.backend_ids == {"login_host": "login3.hpc.example", "login_alias": "login.hpc.example", "pin": "hostname"}
    assert info.status is TargetStatus.RUNNING
    assert info.remote_dir == "/scratch/sid/fwd/myproj"
    assert info.tool_prefix == "/scratch/sid/fwd/.fwd-tools"
    assert info.scratch == "/scratch/sid/fwd/.fwd-cache"
    assert info.notes == []
    assert pinned.saw("mkdir -p /scratch/sid/fwd/myproj /scratch/sid/fwd/myproj/.fwd /scratch/sid/fwd/.fwd-tools /scratch/sid/fwd/.fwd-cache")


def test_provision_falls_back_to_alias_when_pinned_host_unreachable() -> None:
    """Clusters without per-node DNS: keep the alias, record the downgrade, and warn."""
    backend = make_backend()
    alias = FakeEndpoint("login.hpc.example", rules={"hostname": (0, "login3.internal\n")})
    pinned = FakeEndpoint("login3.internal", unreachable=True)
    pin(backend, {"login.hpc.example": alias, "login3.internal": pinned})

    info = backend.provision("demo", "myproj")

    assert info.endpoint.host == "login.hpc.example"
    assert info.backend_ids["login_host"] == "login.hpc.example"
    assert info.backend_ids["pin"] == "alias"
    assert info.notes and "not directly reachable" in info.notes[0]


def test_provision_keeps_proxy_jump_and_auth_fields() -> None:
    """Pinning changes which node we land on, never how we reach the cluster."""
    backend = make_backend(proxy_jump="bastion.example", key_path="~/.ssh/hpc", port=2222)
    backend._gpu = None
    info_ep = backend._endpoint_for("login7.hpc.example")
    assert (info_ep.proxy_jump, info_ep.key_path, info_ep.port, info_ep.user) == ("bastion.example", "~/.ssh/hpc", 2222, "sid")


def test_provision_requires_remote_base_and_login_host() -> None:
    with pytest.raises(ProvisionError, match="remote_base"):
        make_backend(remote_base="").provision("demo", "myproj")
    with pytest.raises(ProvisionError, match="login_host"):
        make_backend(login_host="").provision("demo", "myproj")


def test_provision_unreachable_login_raises_provision_error() -> None:
    backend = make_backend()
    pin(backend, {"login.hpc.example": FakeEndpoint("login.hpc.example", unreachable=True)})
    with pytest.raises(ProvisionError, match="cannot reach login host"):
        backend.provision("demo", "myproj")


def test_endpoint_uses_pinned_host_not_alias() -> None:
    backend = make_backend()
    assert backend.endpoint(make_session()).host == "login1.hpc.example"
    assert backend.endpoint(make_session(backend_ids={})).host == "login.hpc.example"


# --------------------------------------------------------------------------- job.sh planting


def test_write_job_script_uses_quoted_heredoc() -> None:
    """The remote write must not expand anything: quoted heredoc, mkdir, chmod, exact path."""
    backend = make_backend()
    ep = FakeEndpoint()
    path = backend.write_job_script(ep, "demo", "/scratch/sid/fwd/demo", 'claude "$HOME is not expanded"')

    assert path == "/scratch/sid/fwd/demo/.fwd/job.sh"
    cmd = ep.calls[-1]
    assert "mkdir -p /scratch/sid/fwd/demo/.fwd" in cmd
    assert "<<'FWD_JOB_SCRIPT_EOF'" in cmd
    assert "chmod +x /scratch/sid/fwd/demo/.fwd/job.sh" in cmd
    body = cmd.split("<<'FWD_JOB_SCRIPT_EOF'\n", 1)[1].split("\nFWD_JOB_SCRIPT_EOF\n", 1)[0]
    assert body + "\n" == backend.job_script("demo", "/scratch/sid/fwd/demo", 'claude "$HOME is not expanded"')


def test_claude_launch_wrapper_writes_and_returns_tmux_command() -> None:
    backend = make_backend()
    ep = FakeEndpoint()
    cmd = backend.claude_launch_wrapper(ep, "demo", "/scratch/sid/fwd/demo", "claude --resume abc123")

    assert cmd == "bash /scratch/sid/fwd/demo/.fwd/job.sh"
    assert ep.saw("claude --resume abc123")


def test_job_script_uses_gpu_remembered_from_provision() -> None:
    backend = make_backend()
    alias = FakeEndpoint("login.hpc.example", rules={"hostname": (0, "login.hpc.example\n")})
    pin(backend, {"login.hpc.example": alias})
    backend.provision("demo", "myproj", gpu="h100")
    assert "--gres=gpu:h100:1" in backend.job_script("demo", "/scratch/sid/fwd/demo", "claude")


def test_write_job_script_failure_becomes_provision_error() -> None:
    backend = make_backend()
    ep = FakeEndpoint(rules={"cat >": (1, "")})
    with pytest.raises(ProvisionError, match="failed to write"):
        backend.write_job_script(ep, "demo", "/scratch/sid/fwd/demo", "claude")


# --------------------------------------------------------------------------- job tracking


def test_find_job_id_matches_exact_job_name_and_takes_newest() -> None:
    ep = FakeEndpoint(
        rules={
            "squeue": (
                0,
                "1001 fwd-demo\n"
                "1002 fwd-demo-other\n"
                "1010 other-job\n"
                "1009 fwd-demo\n"
                "1003 fwd-demoX\n",
            )
        }
    )
    assert make_backend().find_job_id(ep, "demo") == "1009"
    assert 'squeue -u "$USER" -h -o "%i %j"' in ep.calls[0]


def test_find_job_id_handles_array_ids_and_no_match() -> None:
    backend = make_backend()
    assert backend.find_job_id(FakeEndpoint(rules={"squeue": (0, "77_3 fwd-demo\n77_11 fwd-demo\n")}), "demo") == "77_11"
    assert backend.find_job_id(FakeEndpoint(rules={"squeue": (0, "\n")}), "demo") is None
    assert backend.find_job_id(FakeEndpoint(rules={"squeue": (1, "")}), "demo") is None
    assert backend.find_job_id(FakeEndpoint(unreachable=True), "demo") is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("RUNNING", TargetStatus.RUNNING),
        ("COMPLETING", TargetStatus.RUNNING),
        ("CG", TargetStatus.RUNNING),
        ("R", TargetStatus.RUNNING),
        ("PENDING", TargetStatus.PENDING),
        ("PD", TargetStatus.PENDING),
        ("CONFIGURING", TargetStatus.PENDING),
        ("SUSPENDED", TargetStatus.PENDING),
        ("COMPLETED", TargetStatus.JOB_ENDED),
        ("CANCELLED by 40123", TargetStatus.JOB_ENDED),
        ("TIMEOUT", TargetStatus.JOB_ENDED),
        ("OUT_OF_MEMORY", TargetStatus.JOB_ENDED),
        ("NODE_FAIL", TargetStatus.JOB_ENDED),
        ("", TargetStatus.JOB_ENDED),
        ("SOMETHING_NEW", TargetStatus.RUNNING),
    ],
)
def test_state_mapping_table(state: str, expected: TargetStatus) -> None:
    assert map_slurm_state(state) is expected


@pytest.mark.parametrize(
    ("squeue", "expected"),
    [((0, "RUNNING\n"), TargetStatus.RUNNING), ((0, "PENDING\n"), TargetStatus.PENDING), ((0, ""), TargetStatus.JOB_ENDED), ((1, ""), TargetStatus.JOB_ENDED)],
)
def test_status_from_recorded_job_id(squeue: tuple[int, str], expected: TargetStatus) -> None:
    backend = make_backend()
    ep = FakeEndpoint("login1.hpc.example", rules={"squeue -j": squeue})
    pin(backend, {"login1.hpc.example": ep})
    assert backend.status(make_session(backend_ids={"login_host": "login1.hpc.example", "job_id": "1234"})) is expected
    assert ep.saw("squeue -j 1234 -h -o %T")


def test_status_unreachable_login_is_gone() -> None:
    backend = make_backend()
    pin(backend, {"login1.hpc.example": FakeEndpoint("login1.hpc.example", unreachable=True)})
    assert backend.status(make_session()) is TargetStatus.GONE


def test_status_without_job_id_rescans_then_reports_running() -> None:
    """No id recorded yet: rescan by name, and if still nothing the login node is up so the session is usable."""
    backend = make_backend()
    found = FakeEndpoint("login1.hpc.example", rules={'squeue -u "$USER"': (0, "555 fwd-demo\n"), "squeue -j": (0, "PENDING\n")})
    pin(backend, {"login1.hpc.example": found})
    assert backend.status(make_session()) is TargetStatus.PENDING

    backend2 = make_backend()
    empty = FakeEndpoint("login1.hpc.example", rules={"squeue": (0, "")})
    pin(backend2, {"login1.hpc.example": empty})
    assert backend2.status(make_session()) is TargetStatus.RUNNING


# --------------------------------------------------------------------------- stop / destroy


def test_stop_cancels_by_id_and_kills_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend()
    ep = FakeEndpoint("login1.hpc.example")
    pin(backend, {"login1.hpc.example": ep})
    killed: list[str] = []
    monkeypatch.setattr("fwd.remote.tmux_kill", lambda endpoint, session: killed.append(session))

    backend.stop(make_session(backend_ids={"login_host": "login1.hpc.example", "job_id": "4321"}))

    assert ep.saw("scancel 4321")
    assert killed == ["fwd-demo"]


def test_stop_without_job_id_cancels_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend()
    ep = FakeEndpoint("login1.hpc.example")
    pin(backend, {"login1.hpc.example": ep})
    monkeypatch.setattr("fwd.remote.tmux_kill", lambda endpoint, session: None)

    backend.stop(make_session())
    assert ep.saw('scancel -u "$USER" -n fwd-demo')


def test_stop_is_safe_when_login_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend()
    pin(backend, {"login1.hpc.example": FakeEndpoint("login1.hpc.example", unreachable=True)})
    monkeypatch.setattr("fwd.remote.tmux_kill", lambda endpoint, session: (_ for _ in ()).throw(SSHError("down")))
    backend.stop(make_session())  # must not raise


def test_destroy_removes_only_paths_under_remote_base(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend()
    ep = FakeEndpoint("login1.hpc.example")
    pin(backend, {"login1.hpc.example": ep})
    monkeypatch.setattr("fwd.remote.tmux_kill", lambda endpoint, session: None)

    backend.destroy(make_session())
    assert ep.saw("rm -rf -- /scratch/sid/fwd/demo")


@pytest.mark.parametrize(
    "remote_dir",
    ["/", "", "/scratch/sid/fwd", "/home/sid/demo", "relative/path", "/scratch/sid/fwd/../../etc"],
)
def test_destroy_refuses_foreign_paths(remote_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand-edited state or a repointed config must never turn `fwd rm` into rm -rf on someone else's tree."""
    backend = make_backend()
    ep = FakeEndpoint("login1.hpc.example")
    pin(backend, {"login1.hpc.example": ep})
    monkeypatch.setattr("fwd.remote.tmux_kill", lambda endpoint, session: None)

    with pytest.raises(ProvisionError, match="refusing to delete"):
        backend.destroy(make_session(remote_dir=remote_dir))
    assert not ep.saw("rm -rf")


# --------------------------------------------------------------------------- doctor


def test_doctor_reports_all_checks_on_a_healthy_cluster() -> None:
    backend = make_backend()
    ep = FakeEndpoint(
        "login.hpc.example",
        rules={
            "hostname": (0, "login2.hpc.example\n"),
            "squeue --version": (0, "slurm 23.02.7\n"),
            "tmux -V": (0, "tmux 3.3a\n"),
            "test -w": (0, "writable\n"),
            "df -P": (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100 40 60 40% /scratch\n"),
        },
    )
    pin(backend, {"login.hpc.example": ep})

    results = {c.name: c for c in backend.doctor()}
    assert set(results) == {"ssh", "config", "login-host", "squeue", "tmux", "remote_base", "scratch-space"}
    assert all(c.ok for c in results.values())
    assert "login2.hpc.example" in results["login-host"].detail


def test_doctor_warns_on_full_scratch_and_missing_slurm() -> None:
    backend = make_backend()
    ep = FakeEndpoint(
        "login.hpc.example",
        rules={
            "hostname": (0, "login2\n"),
            "squeue --version": (127, "squeue: command not found"),
            "df -P": (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100 97 3 97% /scratch\n"),
        },
    )
    pin(backend, {"login.hpc.example": ep})

    results = {c.name: c for c in backend.doctor()}
    assert results["squeue"].ok is False and results["squeue"].hint
    assert results["scratch-space"].ok is False and "97%" in results["scratch-space"].detail


def test_doctor_short_circuits_on_unconfigured_and_unreachable_target() -> None:
    unconfigured = {c.name: c for c in make_backend(remote_base="").doctor()}
    assert unconfigured["config"].ok is False and "login-host" not in unconfigured

    backend = make_backend()
    pin(backend, {"login.hpc.example": FakeEndpoint("login.hpc.example", unreachable=True)})
    results = {c.name: c for c in backend.doctor()}
    assert results["login-host"].ok is False and "squeue" not in results
