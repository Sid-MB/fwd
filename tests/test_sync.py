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
import sys
from pathlib import Path

import pytest
import typer

from fwd import config as config_mod
from fwd import selection
from fwd import ui
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


def test_rsync_transport_stops_outbound_wire_bytes_at_the_limit(tmp_path: Path) -> None:
    """The duplex relay carries a real rsync protocol and records overflow before forwarding a huge stream."""
    from fwd import rsync_transport, sync as sync_mod

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _write(source, "hello.txt", "hello")
    fake_ssh = tmp_path / "fake_ssh.py"
    fake_ssh.write_text(
        "import os, sys\ncommand = sys.argv[2:]\n"
        "os.execl('/bin/sh', 'sh', '-c', command[0]) if len(command) == 1 else os.execvp(command[0], command)\n",
        encoding="utf-8",
    )
    successful_sentinel = tmp_path / "not-exceeded"
    bounded_shell = shlex.join(
        [
            sys.executable,
            str(Path(rsync_transport.__file__)),
            "--limit",
            "1000000",
            "--sentinel",
            str(successful_sentinel),
            "--",
            sys.executable,
            str(fake_ssh),
        ]
    )
    progress: list[int] = []
    sync_mod._run_bounded_rsync([*RSYNC_BASE, "-e", bounded_shell, f"{source}/", f"fake:{destination}/"], successful_sentinel, progress.append)
    assert (destination / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert not successful_sentinel.exists()
    assert progress == sorted(progress)
    assert progress[-1] > 0

    sentinel = tmp_path / "exceeded"
    consumer = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
    proc = subprocess.run(
        [sys.executable, str(Path(rsync_transport.__file__)), "--limit", "3", "--sentinel", str(sentinel), "--", *consumer],
        input=b"abcdef",
        capture_output=True,
    )

    assert proc.returncode == rsync_transport.LIMIT_EXIT_CODE
    assert int(sentinel.read_text(encoding="utf-8")) >= 6


def test_rsync_overflow_cleans_remote_stage_before_reporting_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-limit rsync must discard its partial stage and never run the live-project commit."""
    from fwd import sync as sync_mod

    stage = "/workspace/.fwd-upload.test"
    cleaned: list[str] = []
    monkeypatch.setattr(sync_mod, "_create_remote_stage", lambda endpoint, remote_dir: stage)
    monkeypatch.setattr(sync_mod, "_run_bounded_rsync", lambda argv, sentinel, on_progress=None: (_ for _ in ()).throw(sync_mod._UploadLimitExceeded(1_000_000_001)))
    monkeypatch.setattr(sync_mod, "_cleanup_remote_stage", lambda endpoint, path: cleaned.append(path) or True)
    monkeypatch.setattr(sync_mod, "_upload_limit_error", lambda source, cfg, observed, **kwargs: (_ for _ in ()).throw(typer.Exit(1)))

    with pytest.raises(typer.Exit):
        sync_mod.sync_up(_endpoint(), tmp_path, "/workspace/project", SyncConfig(max_size_gb=1))

    assert cleaned == [stage]


def test_tar_upload_always_stages_and_traps_cleanup() -> None:
    """A non-mirroring tar push stages safely and reports cumulative compressed bytes from its streaming relay."""
    from fwd import sync as sync_mod

    command = sync_mod._tar_mirror_command("/workspace/project", ".next\nnode_modules\n", delete=False)
    assert 'stage=$(mktemp -d "$parent/.fwd-upload.XXXXXX")' in command
    assert "trap cleanup EXIT HUP INT TERM" in command
    assert "tar xzf - -v -C \"$stage\"" in command
    assert "comm -23" not in command
    assert command.index("tar xzf") < command.index("cp -a")

    progress: list[int] = []
    sync_mod._pipe(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcdef')"],
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        what="test tar push",
        max_bytes=100,
        on_progress=progress.append,
    )
    assert progress == [6]


def test_upload_limit_error_offers_project_and_user_config_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A stopped stream explains cleanup and both ways to make a deliberate larger-project exception."""
    from fwd import sync as sync_mod

    global_path = tmp_path / "home" / "config.toml"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setattr(
        sync_mod,
        "_large_upload_entries",
        lambda source, cfg, **kwargs: [sync_mod._LargeUploadEntry("datasets/checkpoints", 850_000_000, "folder")],
    )
    monkeypatch.setattr(ui.err_console, "width", 500)

    with pytest.raises(typer.Exit) as exc_info:
        sync_mod._upload_limit_error(tmp_path, SyncConfig(max_size_gb=1), 1_000_000_001)

    assert exc_info.value.exit_code == 1
    error = " ".join(capsys.readouterr().err.split())
    assert "sync.max_size_gb=1" in error
    assert "removed the incomplete remote staging copy" in error
    assert "config set --project sync.max_size_gb 2" in error
    assert str(tmp_path / ".fwd" / "config.toml") in error
    assert str(tmp_path / ".fwdignore") in error
    assert "add their project-relative paths" in error
    assert str(global_path) in error
    assert "850.0 MB folder datasets/checkpoints" in error


@needs_rsync
def test_large_upload_entries_aggregate_only_filtered_paths_over_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure diagnostics reuse upload filters and aggregate qualifying files into every non-root parent folder."""
    from fwd import sync as sync_mod

    monkeypatch.setattr(sync_mod, "_LARGE_UPLOAD_ENTRY_BYTES", 10)
    _write(tmp_path, "assets/model.bin", "m" * 12)
    _write(tmp_path, "assets/shards/one.bin", "1" * 7)
    _write(tmp_path, "assets/shards/two.bin", "2" * 7)
    _write(tmp_path, ".next/cache/huge.bin", "x" * 100)
    _write(tmp_path, "small.txt", "tiny")

    entries = {(entry.kind, entry.path): entry.size_bytes for entry in sync_mod._large_upload_entries(tmp_path, SyncConfig())}

    assert entries[("file", "assets/model.bin")] == 12
    assert entries[("folder", "assets/shards")] == 14
    assert entries[("folder", "assets")] == 26
    assert not any(".next" in path for _, path in entries)


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
    with selection.upload_manifest(source, sync_cfg) as manifest:
        filters = selection.rsync_manifest_args(manifest) if manifest is not None else rsync_filters(sync_cfg, source)
        argv = [*RSYNC_BASE, *filters, "--delete", f"{source}/", f"{destination}/"]
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
def test_git_manifest_honours_deep_nested_ignores_and_fwdignore_for_tracked_files(tmp_path: Path) -> None:
    """Git is the ignore authority at every depth while tracked code and untracked WIP remain in the upload domain."""
    source = tmp_path / "source"
    _write(source, "src/main.py", "print('tracked')\n")
    _write(source, "src/wip.py", "print('untracked')\n")
    _write(source, "packages/backend/.convex/.gitignore", "/*\n")
    _write(source, "packages/backend/.convex/local/cache.blob", "ignored local state\n")
    _write(source, "generated/tracked.txt", "excluded by fwdignore\n")
    _write(source, ".fwdignore", "generated/\n")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "add",
            "src/main.py",
            "generated/tracked.txt",
        ],
        check=True,
    )

    shipped = _shipped(source, tmp_path / "destination", SyncConfig())

    assert {"src/main.py", "src/wip.py", "packages/backend/.convex/.gitignore", ".fwdignore", ".git/HEAD"} <= shipped
    assert "packages/backend/.convex/local/cache.blob" not in shipped
    assert "generated/tracked.txt" not in shipped

    with selection.upload_manifest(source, SyncConfig()) as manifest:
        assert manifest is not None
        archive = tmp_path / "selection.tar.gz"
        subprocess.run(["tar", "czf", str(archive), "-C", str(source), "--null", f"--files-from={manifest}"], check=True)
        archived = set(subprocess.run(["tar", "tzf", str(archive)], check=True, capture_output=True, text=True).stdout.splitlines())
    assert "packages/backend/.convex/.gitignore" in archived
    assert "packages/backend/.convex/local/cache.blob" not in archived
    assert "packages/backend/.convex/" in selection.git_ignored_patterns(source, SyncConfig())


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
    _write(source, ".next/cache/webpack.bin", "compiled\n")
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
