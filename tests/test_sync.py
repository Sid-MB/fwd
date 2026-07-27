"""Unit tests for the transfer layer and the ssh argv builder it depends on.

Everything here is pure argv/filter construction — no sockets, no subprocesses. That is deliberate: the parts of
``sync``/``sshexec`` that can silently do the wrong thing are the flag combinations (a missing ``--delete``, a lost
``-p``, filters in the wrong shape), and those are exactly what unit tests can pin down. Actual transfers are covered
by the docker-sshd harness in ``tests/harness/docker-sshd``.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from fwd.config import DEFAULT_EXCLUDES, SyncConfig
from fwd.sshexec import SSHEndpoint
from fwd.sync import RSYNC_BASE, rsync_filters

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def _endpoint(**kwargs) -> SSHEndpoint:
    defaults = {"host": "example.com", "user": "dev"}
    return SSHEndpoint(**{**defaults, **kwargs})


# --------------------------------------------------------------------------------------------------------------
# ssh_argv construction
# --------------------------------------------------------------------------------------------------------------


def test_ssh_argv_defaults_are_batch_and_accept_new() -> None:
    argv = _endpoint().ssh_argv(control=False)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[-1] == "dev@example.com"
    # Port 22 is the default; emitting -p 22 would be noise.
    assert "-p" not in argv
    assert "-t" not in argv


def test_ssh_argv_control_options_only_when_requested() -> None:
    endpoint = _endpoint()
    with_control = endpoint.ssh_argv(control=True)
    without = endpoint.ssh_argv(control=False)

    assert "ControlMaster=auto" in with_control
    assert f"ControlPath={endpoint.control_path()}" in with_control
    assert any(opt.startswith("ControlPersist=") for opt in with_control)
    assert not any("Control" in opt for opt in without)


def test_ssh_argv_includes_key_port_proxy_and_extra_opts() -> None:
    endpoint = _endpoint(port=2299, key_path="/tmp/id_test", proxy_jump="external.example", extra_opts=["-o", "ServerAliveInterval=30"])
    argv = endpoint.ssh_argv(tty=True, control=False)

    assert argv[argv.index("-p") + 1] == "2299"
    assert argv[argv.index("-i") + 1] == "/tmp/id_test"
    assert argv[argv.index("-J") + 1] == "external.example"
    assert "-t" in argv
    # extra_opts land immediately before the target so user options are the last thing ssh sees.
    assert argv[-3:] == ["-o", "ServerAliveInterval=30", "dev@example.com"]


def test_ssh_argv_expands_tilde_in_key_path() -> None:
    argv = _endpoint(key_path="~/.ssh/id_ed25519").ssh_argv(control=False)
    key = argv[argv.index("-i") + 1]
    assert "~" not in key
    assert key.endswith("/.ssh/id_ed25519")


def test_ssh_target_omits_user_when_unset() -> None:
    assert SSHEndpoint(host="example.com", user="").ssh_target() == "example.com"


def test_control_path_is_unique_per_user_host_port() -> None:
    a = _endpoint(port=22).control_path()
    b = _endpoint(port=2222).control_path()
    c = _endpoint(host="other.com").control_path()
    assert len({a, b, c}) == 3


def test_control_path_stays_within_the_unix_socket_limit(monkeypatch) -> None:
    """A long $HOME or hostname must not produce ssh's "too long for Unix domain socket" on every command."""
    from fwd import sshexec

    monkeypatch.setattr(sshexec, "CONTROL_DIR", Path("/var/folders/sn/80trdzt973qbn3mx7l67b4d40000gn/T/tmp.DsrpXJTUZ2/home/.fwd/cm"))
    endpoint = _endpoint(host="a-very-long-runpod-pod-hostname.proxy.runpod.net", port=41337)
    path = endpoint.control_path()

    assert len(str(path)) + sshexec.SOCKET_SUFFIX_BUDGET <= sshexec.SOCKET_PATH_LIMIT, path
    # Same endpoint, later process: attach must resolve to the socket that up created.
    assert path == endpoint.control_path()


def test_control_path_keeps_the_readable_form_when_it_fits(monkeypatch) -> None:
    from fwd import sshexec

    monkeypatch.setattr(sshexec, "CONTROL_DIR", Path("/tmp/fwd/cm"))
    assert sshexec.control_dir() == Path("/tmp/fwd/cm")
    assert _endpoint(port=2299).control_path().name == "dev@example.com:2299"


def test_control_dir_falls_back_when_home_is_too_deep(monkeypatch) -> None:
    from fwd import sshexec

    monkeypatch.setattr(sshexec, "CONTROL_DIR", Path("/" + "d" * 90 + "/.fwd/cm"))
    fallback = sshexec.control_dir()
    assert fallback != sshexec.CONTROL_DIR
    assert len(str(fallback)) + 17 + sshexec.SOCKET_SUFFIX_BUDGET <= sshexec.SOCKET_PATH_LIMIT


def test_control_flags_are_spliced_before_the_target() -> None:
    """ssh stops parsing options at the hostname, so -O check appended after user@host would become a remote command."""
    endpoint = _endpoint()
    argv = endpoint._control_argv("-O", "check")
    assert argv[-3:] == ["-O", "check", "dev@example.com"]


def test_probe_argv_sets_a_short_connect_timeout_and_skips_control(monkeypatch) -> None:
    """wait_for_ssh must fail fast and must not ride a possibly-stale master socket."""
    import subprocess as sp

    from fwd import sshexec

    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001 - test double for subprocess.run
        seen.append(list(argv))
        return sp.CompletedProcess(argv, 0)

    monkeypatch.setattr(sshexec.subprocess, "run", fake_run)
    assert sshexec.wait_for_ssh(_endpoint(), timeout=1.0, interval=0.01) is True

    argv = seen[0]
    assert argv[1:3] == ["-o", f"ConnectTimeout={sshexec.PROBE_CONNECT_TIMEOUT}"]
    assert not any("Control" in part for part in argv)
    assert argv[-2:] == ["dev@example.com", "true"]


def test_rsync_shell_mirrors_ssh_options_without_the_target() -> None:
    endpoint = _endpoint(port=2299, key_path="/tmp/id_test")
    parts = shlex.split(endpoint.rsync_shell())
    assert parts[0] == "ssh"
    assert parts == endpoint.ssh_argv()[:-1]
    assert endpoint.ssh_target() not in parts


# --------------------------------------------------------------------------------------------------------------
# rsync filters
# --------------------------------------------------------------------------------------------------------------


def test_rsync_filters_default_shape(tmp_path: Path) -> None:
    args = rsync_filters(SyncConfig(), tmp_path)
    assert args[0] == "--filter=:- .gitignore"
    for pattern in DEFAULT_EXCLUDES:
        assert f"--exclude={pattern}" in args
    # .git must reach the remote so the session can diff, blame and commit.
    assert not any(arg == "--exclude=.git" for arg in args)


def test_rsync_filters_omits_gitignore_when_disabled(tmp_path: Path) -> None:
    args = rsync_filters(SyncConfig(use_gitignore=False), tmp_path)
    assert not any("gitignore" in arg for arg in args)


def test_rsync_filters_uses_config_excludes_verbatim(tmp_path: Path) -> None:
    """A project must be able to *shrink* the exclude list, so config wins outright over DEFAULT_EXCLUDES."""
    args = rsync_filters(SyncConfig(exclude=["only-this"]), tmp_path)
    assert [a for a in args if a.startswith("--exclude=")] == ["--exclude=only-this"]


def test_rsync_filters_picks_up_fwdignore(tmp_path: Path) -> None:
    (tmp_path / ".fwdignore").write_text("big-fixtures/\n", encoding="utf-8")
    args = rsync_filters(SyncConfig(), tmp_path)
    assert f"--exclude-from={tmp_path / '.fwdignore'}" in args


def test_rsync_filters_skips_absent_fwdignore(tmp_path: Path) -> None:
    assert not any(arg.startswith("--exclude-from=") for arg in rsync_filters(SyncConfig(), tmp_path))


def test_rsync_base_is_archive_compressed_and_skips_ownership() -> None:
    """--no-owner/--no-group must follow -a: rsync applies options left to right, and -a implies -o -g.

    Without them a push onto a RunPod MooseFS volume logs a chown failure per file and exits 23.
    """
    assert RSYNC_BASE[:2] == ("rsync", "-az")
    assert "--no-owner" in RSYNC_BASE
    assert "--no-group" in RSYNC_BASE
    assert RSYNC_BASE.index("-az") < RSYNC_BASE.index("--no-owner")


def test_partial_transfer_exits_are_warnings_not_failures(monkeypatch, capsys) -> None:
    """rsync 23/24 mean the bytes arrived but some per-file operation was refused; aborting there wastes a provisioned pod."""
    import subprocess as sp

    from fwd import sync as sync_mod

    for code in sorted(sync_mod.RSYNC_PARTIAL_EXITS):
        monkeypatch.setattr(sync_mod.subprocess, "run", lambda argv, **kw: sp.CompletedProcess(argv, code))
        sync_mod._run(["rsync"], what="rsync push")  # must not raise


def test_genuine_rsync_failures_still_raise(monkeypatch) -> None:
    import subprocess as sp

    from fwd import sync as sync_mod
    from fwd.sshexec import SSHError

    monkeypatch.setattr(sync_mod.subprocess, "run", lambda argv, **kw: sp.CompletedProcess(argv, 12))
    with pytest.raises(SSHError):
        sync_mod._run(["rsync"], what="rsync push")


# --------------------------------------------------------------------------------------------------------------
# What actually ships, for an in-progress project
#
# These run the real rsync binary over a local source/destination pair. Asserting on the argv only proves we *asked*
# for the right filters; running rsync proves it interprets them the way we think, which is the part that has
# historically gone wrong (per-directory ':- .gitignore' merges especially). No ssh involved.
# --------------------------------------------------------------------------------------------------------------


def _write(root: Path, relpath: str, content: str = "x\n") -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _shipped(source: Path, destination: Path, sync_cfg: SyncConfig) -> set[str]:
    """Mirror ``source`` into ``destination`` with fwd's filters and return the relative paths that arrived."""
    destination.mkdir(parents=True, exist_ok=True)
    argv = [*RSYNC_BASE, *rsync_filters(sync_cfg, source), "--delete", f"{source}/", f"{destination}/"]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()}


def _in_progress_uv_project(root: Path) -> Path:
    """Python project mid-session: locked, venv already built, byte caches, and an unsaved-to-git feature file."""
    _write(root, "pyproject.toml", '[project]\nname = "demo"\ndependencies = ["six"]\n')
    _write(root, "uv.lock", "version = 1\n")
    _write(root, "src/app.py", "import six\n")
    _write(root, "src/wip_feature.py", "# uncommitted work in progress\n")
    _write(root, ".gitignore", ".venv/\n__pycache__/\n*.log\n")
    _write(root, ".venv/pyvenv.cfg", "home = /usr\n")
    _write(root, ".venv/lib/python3.12/site-packages/six.py", "# vendored\n")
    _write(root, "src/__pycache__/app.cpython-312.pyc", "bytecode\n")
    _write(root, "debug.log", "noise\n")
    _write(root, ".git/HEAD", "ref: refs/heads/main\n")
    return root


def _in_progress_bun_project(root: Path) -> Path:
    """bun project mid-session: bun.lock plus a populated node_modules."""
    _write(root, "package.json", '{"name":"demo","dependencies":{"left-pad":"1.3.0"}}\n')
    _write(root, "bun.lock", "{}\n")
    _write(root, "index.ts", "import leftPad from 'left-pad'\n")
    _write(root, "wip.ts", "// uncommitted work in progress\n")
    _write(root, ".gitignore", "node_modules/\n")
    _write(root, "node_modules/left-pad/index.js", "module.exports = 1\n")
    _write(root, "node_modules/.bin/left-pad", "#!/bin/sh\n")
    _write(root, ".git/HEAD", "ref: refs/heads/main\n")
    return root


def _in_progress_pnpm_project(root: Path) -> Path:
    """pnpm project mid-session: node_modules plus a project-local content-addressable store."""
    _write(root, "package.json", '{"name":"demo","dependencies":{"left-pad":"1.3.0"}}\n')
    _write(root, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(root, "index.js", "require('left-pad')\n")
    _write(root, "wip.js", "// uncommitted work in progress\n")
    # .pnpm-store is only project-local when the user sets store-dir, and repos that do always gitignore it.
    _write(root, ".gitignore", "node_modules/\n.pnpm-store/\n")
    _write(root, "node_modules/.pnpm/left-pad@1.3.0/node_modules/left-pad/index.js", "module.exports = 1\n")
    _write(root, ".pnpm-store/v3/files/00/abcdef", "blob\n")
    _write(root, ".git/HEAD", "ref: refs/heads/main\n")
    return root


@needs_rsync
def test_in_progress_uv_project_ships_sources_not_the_venv(tmp_path: Path) -> None:
    shipped = _shipped(_in_progress_uv_project(tmp_path / "src_"), tmp_path / "dst", SyncConfig())

    assert {"pyproject.toml", "uv.lock", "src/app.py", "src/wip_feature.py", ".gitignore", ".git/HEAD"} <= shipped
    assert not any(path.startswith(".venv/") for path in shipped)
    assert not any("__pycache__" in path for path in shipped)
    assert "debug.log" not in shipped


@needs_rsync
def test_in_progress_bun_project_ships_lockfile_not_node_modules(tmp_path: Path) -> None:
    shipped = _shipped(_in_progress_bun_project(tmp_path / "src_"), tmp_path / "dst", SyncConfig())

    assert {"package.json", "bun.lock", "index.ts", "wip.ts", ".git/HEAD"} <= shipped
    assert not any(path.startswith("node_modules/") for path in shipped)


@needs_rsync
def test_in_progress_pnpm_project_ships_lockfile_not_store_or_modules(tmp_path: Path) -> None:
    shipped = _shipped(_in_progress_pnpm_project(tmp_path / "src_"), tmp_path / "dst", SyncConfig())

    assert {"package.json", "pnpm-lock.yaml", "index.js", "wip.js", ".git/HEAD"} <= shipped
    assert not any(path.startswith("node_modules/") for path in shipped)
    assert not any(path.startswith(".pnpm-store/") for path in shipped)


@needs_rsync
def test_installed_trees_are_excluded_even_without_a_gitignore(tmp_path: Path) -> None:
    """The default exclude list must stand on its own: not every project gitignores its build output."""
    source = tmp_path / "src_"
    _write(source, "main.py", "print(1)\n")
    _write(source, ".venv/pyvenv.cfg", "home = /usr\n")
    _write(source, "node_modules/left-pad/index.js", "module.exports = 1\n")
    _write(source, "__pycache__/main.cpython-312.pyc", "bytecode\n")

    shipped = _shipped(source, tmp_path / "dst", SyncConfig(use_gitignore=False))
    assert shipped == {"main.py"}


@needs_rsync
def test_pnpm_store_can_be_excluded_by_config_without_a_gitignore(tmp_path: Path) -> None:
    """.pnpm-store is not in DEFAULT_EXCLUDES, so verify the config path covers a repo that does not gitignore it."""
    source = tmp_path / "src_"
    _write(source, "index.js", "require('left-pad')\n")
    _write(source, ".pnpm-store/v3/files/00/abcdef", "blob\n")

    cfg = SyncConfig(exclude=[*DEFAULT_EXCLUDES, ".pnpm-store"], use_gitignore=False)
    assert _shipped(source, tmp_path / "dst", cfg) == {"index.js"}


@needs_rsync
def test_second_push_mirrors_deletions(tmp_path: Path) -> None:
    """--delete is what makes push a mirror; a file removed locally must disappear remotely."""
    source = _in_progress_uv_project(tmp_path / "src_")
    destination = tmp_path / "dst"
    _shipped(source, destination, SyncConfig())
    (source / "src/wip_feature.py").unlink()

    assert "src/wip_feature.py" not in _shipped(source, destination, SyncConfig())
