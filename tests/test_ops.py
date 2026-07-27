"""Tests for the orchestration layer.

Design intent
-------------
These tests must keep passing while four other teammates rewrite the modules underneath them, so they never touch a
real implementation body. Everything mechanical (``sync``, ``remote``, ``claude_state``, ``sshexec``) is monkeypatched
on the *module object*, which works because the ops layer calls ``sync.sync_up(...)`` rather than importing the
function by name — attribute lookup happens at call time.

What is actually asserted is the part ops owns and nobody else can: the **order** of the stages, the **plumbing** of
flags into the right calls, session-name derivation, and the status-to-action matrix in attach. A recorder list
captures every stage as it runs, so an ordering regression (generating HANDOFF.md after the sync, say) fails loudly
instead of silently shipping an empty remote session.

No test performs real ssh, spawns a process, or writes outside tmp_path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from fwd import claude_state, codex_state, remote, sshexec, sync, ui
from fwd.backends.base import TargetInfo, TargetStatus
from fwd.config import Config, RunpodTargetConfig, SshTargetConfig, SyncConfig
from fwd.ops import attach as attach_ops
from fwd.ops import launch as launch_ops
from fwd.ops import lifecycle, transfer
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState, StateStore, endpoint_to_dict

ENDPOINT = SSHEndpoint(host="10.0.0.5", user="root", port=2222)


class FakeBackend:
    """A Provisioner-shaped double that records calls and returns canned results.

    Duck-typed rather than subclassing anything, so it stays valid however the real backends evolve.
    """

    name = "ssh"

    def __init__(self, calls: list[str], *, status: TargetStatus = TargetStatus.RUNNING, endpoint: SSHEndpoint = ENDPOINT, notes: list[str] | None = None, slurm_like: bool = False, job_id: str | None = "4242") -> None:
        self.calls = calls
        self._status = status
        self._endpoint = endpoint
        self._notes = notes or []
        self._job_id = job_id
        self.wrapper_args: tuple[Any, ...] | None = None
        self.provision_args: tuple[Any, ...] | None = None
        self.cleanup_created = False
        if slurm_like:
            # Only present when a test asks for it, mirroring how ops dispatches on hasattr for the Slurm-only hooks.
            self.claude_launch_wrapper = self._claude_launch_wrapper
            self.find_job_id = self._find_job_id

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        self.calls.append("provision")
        self.provision_args = (session_name, project_name, gpu)
        return TargetInfo(
            endpoint=self._endpoint,
            remote_dir="/workspace/proj",
            backend_ids={"pod_id": "abc123"},
            tool_prefix="/workspace/.fwd-tools",
            scratch="/workspace/.cache",
            notes=self._notes,
        )

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        self.calls.append("endpoint")
        return self._endpoint

    def status(self, session: SessionState) -> TargetStatus:
        self.calls.append("status")
        return self._status

    def stop(self, session: SessionState) -> None:
        self.calls.append("stop")

    def destroy(self, session: SessionState) -> None:
        self.calls.append("destroy")

    def doctor(self) -> list:
        return []

    def cleanup_interrupted_provision(self, session_name: str) -> bool:
        self.calls.append("cleanup_interrupted_provision")
        return self.cleanup_created

    def _claude_launch_wrapper(self, endpoint, session_name: str, remote_dir: str, claude_cmd: str, *, tool_prefix: str | None = None, gpu: str | None = None) -> str:
        self.calls.append("claude_launch_wrapper")
        self.wrapper_args = (endpoint, session_name, remote_dir, claude_cmd, tool_prefix, gpu)
        return f"bash {remote_dir}/.fwd/job.sh"

    def _find_job_id(self, endpoint, session_name: str) -> str | None:
        self.calls.append("find_job_id")
        return self._job_id


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory that is also the process cwd, so cwd-based resolution is exercised for real."""
    d = tmp_path / "myproject"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.fixture
def state_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateStore:
    """Redirect all ops state access to a tmp file via the single ``store()`` seam."""
    store = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(launch_ops, "store", lambda: store)
    return store


@pytest.fixture
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the rich console so table assertions see full cell values instead of ellipsis truncation."""
    monkeypatch.setattr(ui.console, "width", 300)


@pytest.fixture
def calls() -> list[str]:
    """Ordered record of every stage the launch pipeline performs."""
    return []


@pytest.fixture
def fake_backend(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """Install a FakeBackend as the result of backend construction everywhere ops looks it up."""
    backend = FakeBackend(calls)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    return backend


@pytest.fixture
def stub_world(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every mechanical module function with a recorder.

    Returns a dict of captured arguments so tests can assert on plumbing (which paths, which flags) as well as order.
    """
    captured: dict[str, Any] = {}

    def record(label: str, result: Any = None, capture: str | None = None):
        def fn(*args, **kwargs):
            calls.append(label)
            if capture:
                captured[capture] = (args, kwargs)
            return result

        return fn

    monkeypatch.setattr(sshexec, "wait_for_ssh", record("wait_for_ssh", True))
    monkeypatch.setattr(SSHEndpoint, "open_control_master", record("control_master"))
    # The transcript import asks the remote for $HOME; nothing else in ops runs a bare remote command.
    monkeypatch.setattr(SSHEndpoint, "run", lambda self, cmd, **kw: SimpleNamespace(stdout="/home/root", returncode=0))
    monkeypatch.setattr(sync, "sync_up", record("sync_up", capture="sync_up"))
    monkeypatch.setattr(sync, "tar_up", record("tar_up", capture="tar_up"))
    monkeypatch.setattr(sync, "sync_down", record("sync_down", capture="sync_down"))
    monkeypatch.setattr(sync, "tar_down", record("tar_down", capture="tar_down"))
    monkeypatch.setattr(remote, "run_bootstrap", record("run_bootstrap", capture="run_bootstrap"))
    monkeypatch.setattr(remote, "detect_dep_commands", record("detect_deps", ["uv sync"]))
    monkeypatch.setattr(remote, "run_dep_install", record("run_dep_install", capture="run_dep_install"))
    monkeypatch.setattr(remote, "tmux_exists", record("tmux_exists", False))
    monkeypatch.setattr(remote, "tmux_new", record("tmux_new", capture="tmux_new"))
    monkeypatch.setattr(remote, "tmux_kill", record("tmux_kill"))
    monkeypatch.setattr(lifecycle.remote_tasks, "kill_manager", record("task_manager_kill"))
    monkeypatch.setattr(lifecycle, "task_store", lambda: SimpleNamespace(cancel_session=record("tasks_cancel")))
    monkeypatch.setattr(claude_state, "upload_user_config", record("upload_user_config"))
    monkeypatch.setattr(claude_state, "read_keychain_creds", record("read_keychain_creds", "{}"))
    monkeypatch.setattr(claude_state, "upload_creds", record("upload_creds"))
    monkeypatch.setattr(claude_state, "make_handoff", record("make_handoff", Path("HANDOFF.md")))
    monkeypatch.setattr(claude_state, "export_session_bundle", record("export_bundle", Path("/tmp/bundle")))
    monkeypatch.setattr(claude_state, "import_session_bundle", record("import_bundle", "sess-123"))
    # exec_attach would replace the process; record it and stop the pipeline the way the real exec would.
    monkeypatch.setattr(launch_ops, "exec_attach", record("exec_attach", None))
    return captured


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """A minimal single-ssh-target config, bypassing TOML entirely."""
    cfg = Config(
        default_target="dev",
        targets={"dev": SshTargetConfig(name="dev", host="10.0.0.5", user="root")},
        sync=SyncConfig(),
    )
    monkeypatch.setattr(launch_ops, "load_config", lambda project_dir=None: cfg)
    return cfg


# --- session name derivation -------------------------------------------------------------------------------------


def test_derive_session_name_is_slug_plus_digest(tmp_path: Path) -> None:
    d = tmp_path / "My Project.v2"
    d.mkdir()
    name = launch_ops.derive_session_name(d)
    slug, _, digest = name.rpartition("-")
    assert slug == "my-project-v2"
    assert len(digest) == launch_ops.SESSION_HASH_LEN
    assert digest.isalnum()


def test_derive_session_name_is_stable_and_path_unique(tmp_path: Path) -> None:
    """Same directory always yields the same name; same basename in different places must not collide."""
    a = tmp_path / "one" / "api"
    b = tmp_path / "two" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert launch_ops.derive_session_name(a) == launch_ops.derive_session_name(a)
    assert launch_ops.derive_session_name(a) != launch_ops.derive_session_name(b)


def test_tmux_session_name_is_namespaced() -> None:
    assert launch_ops.tmux_session_name("api-abc123") == "fwd-api-abc123"


# --- claude command construction ---------------------------------------------------------------------------------


def test_build_claude_command_variants() -> None:
    assert launch_ops.build_claude_command(resume_id="s-1", use_handoff=False) == "claude --resume s-1"
    assert launch_ops.build_claude_command(resume_id=None, use_handoff=False) == "claude"
    handoff = launch_ops.build_claude_command(resume_id=None, use_handoff=True)
    assert handoff.startswith("claude ") and launch_ops.HANDOFF_PROMPT in handoff
    # A resume id always wins: a transcript already carries the context a handoff would summarize.
    assert launch_ops.build_claude_command(resume_id="s-1", use_handoff=True) == "claude --resume s-1"


def test_build_tmux_command_default_sources_env_and_cds(calls: list[str]) -> None:
    cmd = launch_ops.build_tmux_command(FakeBackend(calls), ENDPOINT, "api-1", "/workspace/proj", "/workspace/.fwd-tools", "claude")
    assert "fwd-env.sh" in cmd
    assert "cd /workspace/proj" in cmd
    assert "exec claude" in cmd
    assert cmd.startswith("bash -lc ")


def test_build_tmux_command_uses_backend_hook_when_present(calls: list[str]) -> None:
    """A backend exposing claude_launch_wrapper fully replaces the default command construction."""
    backend = FakeBackend(calls, slurm_like=True)
    cmd = launch_ops.build_tmux_command(backend, ENDPOINT, "api-1", "/scratch/proj", "/scratch/tools", "claude --resume x", gpu="A100")
    assert cmd == "bash /scratch/proj/.fwd/job.sh"
    assert "claude_launch_wrapper" in calls
    assert "fwd-env.sh" not in cmd
    # The wrapper writes job.sh remotely, so it must receive the endpoint plus the tool prefix and gpu.
    assert backend.wrapper_args == (ENDPOINT, "api-1", "/scratch/proj", "claude --resume x", "/scratch/tools", "A100")


# --- launch flow -------------------------------------------------------------------------------------------------


def test_launch_runs_stages_in_order(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """The canonical ordering: provision, wait, local prep, sync, bootstrap, deps, claude state, tmux, attach."""
    launch_ops.launch(attach=True)

    ordered = [c for c in calls if c not in {"detect_deps", "tmux_exists", "status", "endpoint"}]
    assert ordered == [
        "provision",
        "wait_for_ssh",
        "control_master",
        "export_bundle",
        "sync_up",
        "run_bootstrap",
        "run_dep_install",
        "import_bundle",
        "tmux_new",
        "exec_attach",
    ]


def test_launch_resolves_none_initial_command_from_target_default(project, state_store, config, fake_backend, stub_world, calls) -> None:
    config.default_command = ["claude"]
    config.target_default_commands["dev"] = ["codex"]
    state = launch_ops.launch(initial_command=None, attach=False)
    assert state.flags["initial_command"] == ["codex"]
    tmux_command = stub_world["tmux_new"][0][3]
    assert "exec codex" in tmux_command


def test_launch_exports_transcript_before_sync_imports_after_bootstrap(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """Export is local and cheap; the import needs a remote home that bootstrap may have just created."""
    launch_ops.launch(attach=False)
    assert calls.index("export_bundle") < calls.index("sync_up")
    assert calls.index("run_bootstrap") < calls.index("import_bundle")


def test_launch_generates_handoff_before_sync(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """HANDOFF.md must exist before the mirror runs or it never reaches the remote machine."""
    launch_ops.launch(handoff=True)
    assert calls.index("make_handoff") < calls.index("sync_up")


def test_launch_persists_state(project, state_store, config, fake_backend, stub_world) -> None:
    launch_ops.launch(attach=False)
    saved = state_store.get_for_cwd(project)
    assert saved is not None
    assert saved.backend == "ssh"
    assert saved.remote_dir == "/workspace/proj"
    assert saved.tmux_session == f"fwd-{saved.name}"
    assert saved.backend_ids == {"pod_id": "abc123"}
    assert saved.endpoint == endpoint_to_dict(ENDPOINT)
    # The target name is recorded so reuse and lifecycle commands can rebuild the same backend.
    assert saved.flags["target"] == "dev"


def test_launch_prints_exact_resolved_instance(project, state_store, config, fake_backend, stub_world, capsys: pytest.CaptureFixture[str]) -> None:
    """Users selecting a generic backend alias must still see the concrete target, endpoint, port, and provider id."""
    launch_ops.launch(attach=False)
    output = " ".join(capsys.readouterr().err.split())
    assert "resolved target 'dev' to ssh instance root@10.0.0.5:2222" in output
    assert "pod_id=abc123" in output


def test_launch_plumbs_gpu_and_name(project, state_store, config, fake_backend, stub_world) -> None:
    launch_ops.launch(gpu="A100", name="custom", attach=False)
    assert fake_backend.provision_args == ("custom", "myproject", "A100")
    assert state_store.get("custom") is not None


def test_launch_no_attach_skips_exec(project, state_store, config, fake_backend, stub_world, calls) -> None:
    launch_ops.launch(attach=False)
    assert "exec_attach" not in calls


def test_launch_failure_after_provision_remains_listable_and_stoppable(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """A billable provider resource must enter state before any remote setup stage can orphan it."""
    monkeypatch.setattr(remote, "run_bootstrap", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bootstrap failed")))

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        launch_ops.launch(initial_command=("codex",), attach=False)

    saved = state_store.get_for_cwd(project)
    assert saved is not None
    assert saved.backend_ids == {"pod_id": "abc123"}
    assert saved.flags["target"] == "dev"
    lifecycle.stop(saved.name)
    assert "stop" in calls


def test_ctrl_c_during_new_provision_removes_owned_resource_and_reports_zero_sessions(project, state_store, config, fake_backend, capsys, monkeypatch, wide_console) -> None:
    """Interruption cleanup is armed before provision returns, covering Ctrl-C during a provider readiness wait."""
    fake_backend.cleanup_created = True
    monkeypatch.setattr(fake_backend, "provision", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        launch_ops.launch(initial_command=("codex",), attach=False)

    assert "cleanup_interrupted_provision" in fake_backend.calls
    assert state_store.all() == []
    output = " ".join(capsys.readouterr().err.split())
    assert "startup canceled; removed newly created session" in output
    assert "0 sessions still running" in output


def test_ctrl_c_during_reused_launch_keeps_resource_and_uses_singular_count(project, state_store, config, fake_backend, stub_world, capsys, monkeypatch, wide_console) -> None:
    """A reused resource is not owned by this invocation and must survive its interruption."""
    existing = _seed(state_store, project)
    monkeypatch.setattr(remote, "run_bootstrap", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        launch_ops.launch(initial_command=("codex",), attach=False)

    assert state_store.get(existing.name) is not None
    output = " ".join(capsys.readouterr().err.split())
    assert "no newly created resource was removed" in output
    assert "1 session still running" in output


def test_launch_transfers_transcript_by_default(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """The S1 spike proved transcript relocation works, so moving the real conversation is the default."""
    launch_ops.launch(attach=False)
    assert "export_bundle" in calls
    assert "import_bundle" in calls
    # A successful transcript transfer means no handoff document is generated.
    assert "make_handoff" not in calls
    assert "--resume sess-123" in stub_world["tmux_new"][0][3]


def test_launch_handoff_flag_forces_handoff_over_session(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """--handoff is the explicit opt-out: it suppresses the transcript transfer entirely."""
    launch_ops.launch(handoff=True, attach=False)
    assert "export_bundle" not in calls
    assert "import_bundle" not in calls
    assert launch_ops.HANDOFF_PROMPT in stub_world["tmux_new"][0][3]


def test_launch_reuses_a_fresh_handoff(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """Regenerating costs a ~65s claude -p round trip, which a repair rerun must not pay again."""
    (project / "HANDOFF.md").write_text("previous handoff", encoding="utf-8")
    launch_ops.launch(handoff=True, attach=False)
    assert "make_handoff" not in calls
    # The session still points at the document; only the regeneration was skipped.
    assert launch_ops.HANDOFF_PROMPT in stub_world["tmux_new"][0][3]


def test_launch_regenerates_a_stale_handoff(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """Past the freshness window the conversation has probably moved on, so the summary is rebuilt."""
    handoff = project / "HANDOFF.md"
    handoff.write_text("old handoff", encoding="utf-8")
    stale = time.time() - launch_ops.HANDOFF_MAX_AGE_SECONDS - 60
    os.utime(handoff, (stale, stale))
    launch_ops.launch(handoff=True, attach=False)
    assert "make_handoff" in calls


def test_fresh_handoff_helper_boundaries(tmp_path: Path) -> None:
    """Directly pin the freshness predicate, since the launch tests can only observe it indirectly."""
    assert launch_ops._fresh_handoff(tmp_path) is None
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text("x", encoding="utf-8")
    assert launch_ops._fresh_handoff(tmp_path) == handoff
    stale = time.time() - launch_ops.HANDOFF_MAX_AGE_SECONDS - 1
    os.utime(handoff, (stale, stale))
    assert launch_ops._fresh_handoff(tmp_path) is None


def test_launch_falls_back_to_handoff_when_export_returns_none(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """Rung two of the chain: no local transcript, but handoff is enabled, so a document is generated instead."""
    monkeypatch.setattr(claude_state, "export_session_bundle", lambda *a, **k: None)
    cfg = launch_ops.load_config()
    cfg.claude.handoff = True
    launch_ops.launch(attach=False)
    assert "make_handoff" in calls
    assert "import_bundle" not in calls
    assert launch_ops.HANDOFF_PROMPT in stub_world["tmux_new"][0][3]


def test_launch_falls_back_to_plain_claude_when_export_returns_none(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """Rung three: nothing to transfer and handoff disabled, so the session starts clean rather than failing."""
    monkeypatch.setattr(claude_state, "export_session_bundle", lambda *a, **k: None)
    launch_ops.launch(attach=False)
    assert "make_handoff" not in calls
    assert stub_world["tmux_new"][0][3].endswith("exec claude'")


def test_launch_falls_back_to_plain_claude_when_import_returns_none(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """A remote import that cannot be validated must degrade, never abort: claude_state never raises."""
    monkeypatch.setattr(claude_state, "import_session_bundle", lambda *a, **k: None)
    launch_ops.launch(attach=False)
    assert "--resume" not in stub_world["tmux_new"][0][3]


def test_launch_user_config_and_creds_flags(project, state_store, config, fake_backend, stub_world, calls) -> None:
    launch_ops.launch(user_config=True, creds=True, attach=False)
    assert "upload_user_config" in calls
    assert "read_keychain_creds" in calls
    assert "upload_creds" in calls


def test_launch_skips_secret_bearing_steps_by_default(project, state_store, config, fake_backend, stub_world, calls) -> None:
    """Anything touching dotfiles or credentials stays opt-in, however convenient it would be."""
    launch_ops.launch(attach=False)
    assert "upload_user_config" not in calls
    assert "read_keychain_creds" not in calls
    assert "upload_creds" not in calls


def test_codex_launch_syncs_settings_selects_codex_bootstrap_and_starts_codex(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """The registry must drive all three integration points without activating Claude transcript or credential work."""
    monkeypatch.setattr(codex_state, "upload_user_config", lambda endpoint: calls.append("upload_codex_config"))

    state = launch_ops.launch(initial_command=("codex",), attach=False)

    assert "upload_codex_config" in calls
    assert "export_bundle" not in calls
    assert "import_bundle" not in calls
    assert "read_keychain_creds" not in calls
    assert stub_world["run_bootstrap"][1]["agent"] == "codex"
    assert stub_world["tmux_new"][0][3].endswith("exec codex'")
    assert state.flags["initial_command"] == ["codex"]


def test_launch_uses_tar_when_rsync_unsupported(project, state_store, config, calls, stub_world, monkeypatch) -> None:
    """A proxy transport must transparently fall back to tar-over-ssh."""
    backend = FakeBackend(calls, endpoint=SSHEndpoint(host="proxy", user="root", supports_rsync=False))
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    launch_ops.launch(attach=False)
    assert "tar_up" in calls
    assert "sync_up" not in calls


def test_launch_skips_tmux_creation_when_session_alive(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """Re-running fwd up must repair a half-launched session without duplicating a live tmux session."""
    monkeypatch.setattr(remote, "tmux_exists", lambda *a, **k: True)
    launch_ops.launch(attach=False)
    assert "tmux_new" not in calls
    assert "run_bootstrap" in calls


def test_launch_reuses_existing_session_for_cwd(project, state_store, config, fake_backend, stub_world) -> None:
    """A second launch in the same directory keeps the original name and created_at."""
    first = launch_ops.launch(attach=False)
    second = launch_ops.launch(attach=False)
    assert second.name == first.name
    assert second.created_at == first.created_at
    assert len(state_store.all()) == 1


def test_launch_dies_when_ssh_never_comes_up(project, state_store, config, fake_backend, stub_world, monkeypatch) -> None:
    monkeypatch.setattr(sshexec, "wait_for_ssh", lambda *a, **k: False)
    with pytest.raises(typer.Exit):
        launch_ops.launch(attach=False)


def test_launch_push_only_stops_after_sync(project, state_store, config, fake_backend, stub_world, calls) -> None:
    launch_ops.launch(push_only=True, attach=False)
    assert "sync_up" in calls
    assert "run_bootstrap" not in calls
    assert "tmux_new" not in calls


def test_launch_continues_when_control_master_fails(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """Multiplexing is an optimization; losing it must not fail the launch."""

    def boom(self, **kwargs):
        raise sshexec.SSHError("no master")

    monkeypatch.setattr(SSHEndpoint, "open_control_master", boom)
    launch_ops.launch(attach=False)
    assert "tmux_new" in calls


# --- attach reconcile matrix -------------------------------------------------------------------------------------


def _seed(store: StateStore, project: Path, **overrides: Any) -> SessionState:
    """Insert a session for the project directory."""
    session = SessionState(
        name="myproject-abc123",
        backend="ssh",
        local_cwd=str(project),
        remote_dir="/workspace/proj",
        tmux_session="fwd-myproject-abc123",
        endpoint=endpoint_to_dict(ENDPOINT),
        flags={"target": "dev"},
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    store.upsert(session)
    return session


@pytest.fixture
def attach_world(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Neutralize the two process-replacing/interactive calls attach makes."""
    monkeypatch.setattr(launch_ops, "exec_attach", lambda endpoint, tmux, session_name=None: calls.append("exec_attach"))
    monkeypatch.setattr(remote, "tmux_exists", lambda *a, **k: True)


def test_attach_running_execs_and_stamps_last_attached(project, state_store, config, fake_backend, attach_world, calls) -> None:
    _seed(state_store, project)
    attach_ops.attach()
    assert "exec_attach" in calls
    assert state_store.get("myproject-abc123").last_attached is not None


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdin is an interactive terminal, so restart prompts are reachable."""
    monkeypatch.setattr(attach_ops.sys.stdin, "isatty", lambda: True)


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdin is not a terminal, as in CI or a piped script."""
    monkeypatch.setattr(attach_ops.sys.stdin, "isatty", lambda: False)


@pytest.mark.parametrize("status", [TargetStatus.STOPPED, TargetStatus.JOB_ENDED])
def test_attach_never_auto_restarts_without_a_tty(project, state_store, config, attach_world, calls, monkeypatch, no_tty, status) -> None:
    """The live-e2e hazard: a scripted attach must never silently reprovision billable hardware.

    ui.confirm returns its *default* (yes) when there is no tty, so without this guard a cron job attaching to a
    stopped pod would start charging money with nobody watching.
    """
    backend = FakeBackend(calls, status=status, slurm_like=(status is TargetStatus.JOB_ENDED))
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("must not prompt without a tty"))
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: pytest.fail("must not restart without a tty"))
    _seed(state_store, project)

    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert "exec_attach" not in calls
    assert state_store.get("myproject-abc123") is not None


def test_attach_restart_flag_authorizes_non_interactive_restart(project, state_store, config, attach_world, calls, monkeypatch, no_tty) -> None:
    """--restart is the explicit authorization that makes a scripted restart legitimate."""
    backend = FakeBackend(calls, status=TargetStatus.STOPPED)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("--restart must not prompt"))
    relaunched: list[dict] = []
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: relaunched.append(kwargs))
    _seed(state_store, project)

    with pytest.raises(typer.Exit):
        attach_ops.attach(restart=True)
    assert relaunched and relaunched[0]["name"] == "myproject-abc123"


def test_attach_dead_tmux_also_gated_without_tty(project, state_store, config, attach_world, calls, monkeypatch, no_tty) -> None:
    """Rerunning the launch pipeline is cheaper than provisioning hardware, but still goes through the same gate."""
    monkeypatch.setattr(remote, "tmux_exists", lambda *a, **k: False)
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: pytest.fail("must not relaunch without a tty"))
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.attach()


def test_attach_gone_does_not_prune_state_without_tty(project, state_store, config, attach_world, calls, monkeypatch, no_tty) -> None:
    """The GONE prompt defaults to no, so a non-interactive run leaves the entry for a human to look at."""
    backend = FakeBackend(calls, status=TargetStatus.GONE)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert state_store.get("myproject-abc123") is not None


def test_smart_default_forwards_restart_flag(project, state_store, config, attach_world, calls, monkeypatch, no_tty) -> None:
    backend = FakeBackend(calls, status=TargetStatus.STOPPED)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    relaunched: list[dict] = []
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: relaunched.append(kwargs))
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.smart_default(restart=True)
    assert relaunched


def test_attach_stopped_offers_full_relaunch(project, state_store, config, attach_world, calls, monkeypatch, tty) -> None:
    """A stopped pod has a wiped container disk, so only the complete pipeline can repair it."""
    backend = FakeBackend(calls, status=TargetStatus.STOPPED)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    relaunched: list[dict] = []
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: relaunched.append(kwargs))
    _seed(state_store, project)

    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert relaunched and relaunched[0]["name"] == "myproject-abc123"
    # The original launch flags are replayed rather than reset to defaults.
    assert relaunched[0]["target"] == "dev"


def test_attach_pending_attaches_to_queued_allocation(project, state_store, config, attach_world, calls, monkeypatch) -> None:
    """A queued Slurm job is normal and its pane shows the queue position, so attaching beats erroring."""
    backend = FakeBackend(calls, status=TargetStatus.PENDING, slurm_like=True)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("PENDING must not prompt"))
    _seed(state_store, project)
    attach_ops.attach()
    assert "exec_attach" in calls
    assert "claude_launch_wrapper" not in calls


def test_attach_job_ended_restarts_allocation_in_place(project, state_store, config, attach_world, calls, monkeypatch, tty) -> None:
    """JOB_ENDED must kill the stale pane and re-wrap, but never re-sync: the shared filesystem is intact."""
    backend = FakeBackend(calls, status=TargetStatus.JOB_ENDED, slurm_like=True, job_id="9001")
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(remote, "tmux_kill", lambda *a, **k: calls.append("tmux_kill"))
    monkeypatch.setattr(remote, "tmux_new", lambda *a, **k: calls.append("tmux_new"))
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: pytest.fail("JOB_ENDED must not run a full relaunch"))
    _seed(state_store, project, flags={"target": "dev", "handoff": True, "tool_prefix": "/scratch/tools"})

    attach_ops.attach()
    assert calls.index("tmux_kill") < calls.index("claude_launch_wrapper") < calls.index("tmux_new")
    assert "sync_up" not in calls and "run_bootstrap" not in calls
    assert "exec_attach" in calls
    # The refreshed job id must be persisted so the next status check targets the new allocation.
    assert state_store.get("myproject-abc123").backend_ids["job_id"] == "9001"


def test_attach_job_ended_declined_exits(project, state_store, config, attach_world, calls, monkeypatch, tty) -> None:
    backend = FakeBackend(calls, status=TargetStatus.JOB_ENDED, slurm_like=True)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert "exec_attach" not in calls


def test_attach_declining_relaunch_exits(project, state_store, config, attach_world, calls, monkeypatch, tty) -> None:
    backend = FakeBackend(calls, status=TargetStatus.STOPPED)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert "exec_attach" not in calls


def test_launch_records_slurm_job_id(project, state_store, config, stub_world, calls, monkeypatch) -> None:
    """The job id only exists after tmux starts salloc, so it is collected post-tmux and merged into backend_ids."""
    backend = FakeBackend(calls, slurm_like=True, job_id="777")
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    state = launch_ops.launch(attach=False)
    assert calls.index("tmux_new") < calls.index("find_job_id")
    assert state.backend_ids["job_id"] == "777"


def test_launch_tolerates_queued_job_without_id(project, state_store, config, stub_world, calls, monkeypatch) -> None:
    """None is the normal answer on a busy cluster and must never fail the launch."""
    backend = FakeBackend(calls, slurm_like=True, job_id=None)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    state = launch_ops.launch(attach=False)
    assert "job_id" not in state.backend_ids


def test_attach_gone_offers_state_removal(project, state_store, config, attach_world, calls, monkeypatch) -> None:
    backend = FakeBackend(calls, status=TargetStatus.GONE)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    _seed(state_store, project)
    with pytest.raises(typer.Exit):
        attach_ops.attach()
    assert state_store.get("myproject-abc123") is None


def test_attach_refreshes_moved_endpoint(project, state_store, config, attach_world, calls, monkeypatch) -> None:
    """RunPod reassigns IP and port on restart, so attach must persist the re-resolved address."""
    moved = SSHEndpoint(host="10.9.9.9", user="root", port=40000)
    backend = FakeBackend(calls, endpoint=moved)
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    _seed(state_store, project)
    attach_ops.attach()
    assert state_store.get("myproject-abc123").endpoint == endpoint_to_dict(moved)


def test_attach_unknown_name_dies(project, state_store, config, fake_backend, attach_world) -> None:
    with pytest.raises(typer.Exit):
        attach_ops.attach("does-not-exist")


def test_smart_default_attaches_when_session_exists(project, state_store, config, fake_backend, attach_world, calls) -> None:
    _seed(state_store, project)
    attach_ops.smart_default()
    assert "exec_attach" in calls


def test_smart_default_launches_when_no_session(project, state_store, config, fake_backend, monkeypatch, calls) -> None:
    launched: list[dict] = []
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: launched.append(kwargs))
    with pytest.raises(typer.Exit):
        attach_ops.smart_default()
    assert launched == [{"initial_command": None, "attach": True}]


# --- lifecycle ---------------------------------------------------------------------------------------------------


def test_ls_renders_and_survives_backend_failure(project, state_store, config, monkeypatch, capsys, wide_console) -> None:
    """One dead backend must degrade to a single unknown cell, not break the whole table."""

    class Exploding(FakeBackend):
        def status(self, session):
            raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: Exploding([]))
    _seed(state_store, project)
    lifecycle.ls()
    captured = capsys.readouterr()
    assert "fwd sessions (1 active)" in captured.out
    assert "myproject-abc123" in captured.out
    assert lifecycle.UNKNOWN_STATUS in captured.out
    assert "`fwd attach myproject-abc123`" in captured.err
    assert "`fwd stop myproject-abc123`" in captured.err
    assert "`fwd rm myproject-abc123`" in captured.err


def test_ls_empty_is_not_an_error(project, state_store, config, capsys, wide_console) -> None:
    lifecycle.ls()
    captured = capsys.readouterr()
    assert "fwd sessions (0 active)" in captured.out
    assert "`fwd attach <name>`" in captured.err


def test_ls_shows_live_status(project, state_store, config, calls, monkeypatch, capsys, wide_console) -> None:
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: FakeBackend(calls, status=TargetStatus.STOPPED))
    _seed(state_store, project)
    lifecycle.ls()
    assert "stopped" in capsys.readouterr().out


def test_ls_json_exposes_named_rows(project, state_store, config, calls, monkeypatch, capsys) -> None:
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: FakeBackend(calls, status=TargetStatus.RUNNING))
    _seed(state_store, project)
    lifecycle.ls(output_format="json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "table"
    assert payload["title"] == "fwd sessions (1 active)"
    assert payload["rows"][0]["name"] == "myproject-abc123"
    assert payload["rows"][0]["status"] == "running"


def test_stop_kills_tmux_then_backend(project, state_store, config, fake_backend, stub_world, calls) -> None:
    _seed(state_store, project)
    lifecycle.stop()
    assert calls.index("tmux_kill") < calls.index("stop")
    assert calls.index("task_manager_kill") < calls.index("stop")
    assert calls.index("tasks_cancel") < calls.index("stop")
    # Stopping preserves the session so it can be restarted later.
    assert state_store.get("myproject-abc123") is not None


def test_stop_still_stops_target_when_tmux_kill_fails(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    """The expensive half must always run: a pod left billing is the failure that actually costs money."""
    monkeypatch.setattr(remote, "tmux_kill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreachable")))
    _seed(state_store, project)
    lifecycle.stop()
    assert "stop" in calls


def test_ctrl_c_during_stop_still_stops_provider_and_reports_remaining_sessions(project, state_store, config, fake_backend, stub_world, calls, monkeypatch, capsys) -> None:
    _seed(state_store, project)
    _seed(state_store, project, name="other-session", local_cwd="/tmp/other")
    monkeypatch.setattr(remote, "tmux_kill", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        lifecycle.stop("myproject-abc123")

    assert "stop" in calls
    output = " ".join(capsys.readouterr().err.split())
    assert "provider was stopped" in output
    assert "1 session still running" in output


def test_stop_warns_that_cpu_runpod_data_was_wiped(project, state_store, config, fake_backend, stub_world, capsys) -> None:
    config.targets["dev"] = RunpodTargetConfig(name="dev", compute_type="cpu")
    fake_backend.target = config.targets["dev"]
    _seed(state_store, project, backend="runpod")

    lifecycle.stop()

    output = capsys.readouterr().err
    assert "RunPod wiped its CPU container disk" in output
    assert "fwd attach myproject-abc123" in output


def test_remove_confirms_then_destroys(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    _seed(state_store, project)
    lifecycle.remove()
    assert "destroy" in calls
    assert state_store.get("myproject-abc123") is None


def test_remove_aborts_without_confirmation(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    _seed(state_store, project)
    lifecycle.remove()
    assert "destroy" not in calls
    assert state_store.get("myproject-abc123") is not None


def test_remove_force_skips_confirmation(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("should not prompt with --force"))
    _seed(state_store, project)
    lifecycle.remove(force=True)
    assert "destroy" in calls


def test_remove_prunes_state_even_when_destroy_fails(project, state_store, config, calls, stub_world, monkeypatch) -> None:
    """A target we cannot destroy must not leave an unusable row in ls forever."""

    class BadDestroy(FakeBackend):
        def destroy(self, session):
            raise RuntimeError("provider error")

    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: BadDestroy(calls))
    _seed(state_store, project)
    lifecycle.remove(force=True)
    assert state_store.get("myproject-abc123") is None


# --- transfer ----------------------------------------------------------------------------------------------------


def test_push_uses_sync_up(project, state_store, config, fake_backend, stub_world, calls, monkeypatch) -> None:
    monkeypatch.setattr(transfer, "load_config", lambda project_dir=None: config)
    _seed(state_store, project)
    transfer.push()
    args, kwargs = stub_world["sync_up"]
    assert args[2] == "/workspace/proj"
    assert kwargs["delete"] is True


def test_pull_passes_paths_through(project, state_store, config, fake_backend, stub_world, monkeypatch) -> None:
    monkeypatch.setattr(transfer, "load_config", lambda project_dir=None: config)
    _seed(state_store, project)
    transfer.pull(paths=("outputs/", "run.log"))
    args, _ = stub_world["sync_down"]
    assert args[1] == "/workspace/proj"
    assert args[3] == ("outputs/", "run.log")


def test_pull_defaults_to_whole_tree(project, state_store, config, fake_backend, stub_world, monkeypatch) -> None:
    monkeypatch.setattr(transfer, "load_config", lambda project_dir=None: config)
    _seed(state_store, project)
    transfer.pull()
    args, _ = stub_world["sync_down"]
    assert args[3] == ()


def test_transfer_uses_tar_fallback(project, state_store, config, calls, stub_world, monkeypatch) -> None:
    backend = FakeBackend(calls, endpoint=SSHEndpoint(host="proxy", user="root", supports_rsync=False))
    monkeypatch.setattr(launch_ops.backends, "make_backend", lambda target, config: backend)
    monkeypatch.setattr(transfer, "load_config", lambda project_dir=None: config)
    _seed(state_store, project)
    transfer.push()
    assert "tar_up" in calls


def test_push_dies_when_local_dir_is_gone(project, state_store, config, fake_backend, stub_world, tmp_path) -> None:
    _seed(state_store, project, local_cwd=str(tmp_path / "deleted"))
    with pytest.raises(typer.Exit):
        transfer.push()
