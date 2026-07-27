"""Unit tests for dependency detection, tmux argv construction and the bootstrap script's shape.

Dependency detection is the highest-value thing to test here: it silently decides what runs on the remote machine, and
a wrong answer surfaces minutes later as an opaque install failure. The tmux tests pin the argv/remote-command strings
because a subtly wrong quoting there breaks attach in ways that only reproduce on a real tty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fwd.remote import (
    BOOTSTRAP_PATH,
    detect_dep_commands,
    run_bootstrap,
    tmux_attach_argv,
)
from fwd.sshexec import SSHEndpoint


def _endpoint(**kwargs) -> SSHEndpoint:
    defaults = {"host": "example.com", "user": "dev"}
    return SSHEndpoint(**{**defaults, **kwargs})


def _touch(root: Path, *relpaths: str) -> Path:
    for relpath in relpaths:
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


# --------------------------------------------------------------------------------------------------------------
# Dependency detection
# --------------------------------------------------------------------------------------------------------------


def test_detect_nothing_in_empty_dir(tmp_path: Path) -> None:
    assert detect_dep_commands(tmp_path) == []


def test_detect_uv_lock(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, "uv.lock", "pyproject.toml")) == ["uv sync"]


def test_detect_bun_lockb_and_bun_lock_collapse_to_one_command(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, "bun.lockb")) == ["bun install"]
    assert detect_dep_commands(_touch(tmp_path, "bun.lock")) == ["bun install"]


def test_detect_npm_pnpm_yarn(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path / "a", "package-lock.json")) == ["npm ci"]
    assert detect_dep_commands(_touch(tmp_path / "b", "pnpm-lock.yaml")) == ["pnpm install --frozen-lockfile"]
    assert detect_dep_commands(_touch(tmp_path / "c", "yarn.lock")) == ["yarn --frozen-lockfile"]


def test_detect_polyglot_project_runs_both_managers(tmp_path: Path) -> None:
    """A Python project with a JS sidecar needs both installs, python first."""
    assert detect_dep_commands(_touch(tmp_path, "uv.lock", "package-lock.json")) == ["uv sync", "npm ci"]


def test_pyproject_without_lockfile_falls_back_to_uv_sync(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, "pyproject.toml")) == ["uv sync"]


def test_requirements_txt_creates_a_venv_first(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, "requirements.txt")) == ["uv venv && uv pip install -r requirements.txt"]


def test_pyproject_wins_over_requirements_txt(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, "pyproject.toml", "requirements.txt")) == ["uv sync"]


def test_lockfile_suppresses_the_declaration_fallback(tmp_path: Path) -> None:
    """uv.lock already implies pyproject.toml; the fallback must not duplicate the command."""
    assert detect_dep_commands(_touch(tmp_path, "uv.lock", "pyproject.toml", "requirements.txt")) == ["uv sync"]


def test_project_setup_script_runs_last(tmp_path: Path) -> None:
    commands = detect_dep_commands(_touch(tmp_path, "uv.lock", ".fwd/setup.sh"))
    assert commands == ["uv sync", "bash .fwd/setup.sh"]


def test_project_setup_script_alone(tmp_path: Path) -> None:
    assert detect_dep_commands(_touch(tmp_path, ".fwd/setup.sh")) == ["bash .fwd/setup.sh"]


# --------------------------------------------------------------------------------------------------------------
# In-progress projects
#
# The realistic input is not a pristine checkout: a user forwards a directory they have been working in for hours, so
# it already contains a built .venv/node_modules, byte caches, and uncommitted edits. Detection must key off the
# lockfile (the declaration) and ignore the presence of an already-installed tree, because that tree is
# platform-specific and never travels — the remote rebuilds it from the same lockfile.
# --------------------------------------------------------------------------------------------------------------


def _uv_project(root: Path) -> Path:
    """Mid-development Python project: locked, already installed locally, with an uncommitted edit in flight."""
    _touch(root, "uv.lock", "pyproject.toml", "src/app.py", "src/wip_feature.py")
    _touch(root, ".venv/pyvenv.cfg", ".venv/lib/python3.12/site-packages/six.py", "src/__pycache__/app.cpython-312.pyc")
    return root


def _bun_project(root: Path) -> Path:
    """Mid-development bun project: bun.lock plus an already-populated node_modules."""
    _touch(root, "package.json", "bun.lock", "index.ts", "wip.ts")
    _touch(root, "node_modules/left-pad/index.js", "node_modules/.bin/left-pad")
    return root


def _pnpm_project(root: Path) -> Path:
    """Mid-development pnpm project: pnpm-lock.yaml plus node_modules and a project-local content-addressable store."""
    _touch(root, "package.json", "pnpm-lock.yaml", "index.js", "wip.js")
    _touch(root, "node_modules/.pnpm/left-pad@1.3.0/node_modules/left-pad/index.js", ".pnpm-store/v3/files/00/abc")
    return root


def test_in_progress_uv_project_detects_uv_sync(tmp_path: Path) -> None:
    assert detect_dep_commands(_uv_project(tmp_path)) == ["uv sync"]


def test_in_progress_bun_project_detects_bun_install(tmp_path: Path) -> None:
    assert detect_dep_commands(_bun_project(tmp_path)) == ["bun install"]


def test_in_progress_pnpm_project_detects_pnpm_install(tmp_path: Path) -> None:
    assert detect_dep_commands(_pnpm_project(tmp_path)) == ["pnpm install --frozen-lockfile"]


def test_bun_lock_wins_over_package_lock_json(tmp_path: Path) -> None:
    """bun.lock is checked before package-lock.json, so a repo migrating to bun does not run a stale npm ci first."""
    _touch(_bun_project(tmp_path), "package-lock.json")
    assert detect_dep_commands(tmp_path)[0] == "bun install"


def test_only_one_js_manager_runs_when_lockfiles_coexist(tmp_path: Path) -> None:
    """Two JS managers installing into the same node_modules fight; the highest-priority lockfile wins outright."""
    _touch(_pnpm_project(tmp_path), "package-lock.json")
    assert detect_dep_commands(tmp_path) == ["npm ci"]


def test_python_and_js_managers_both_run_for_a_polyglot_project(tmp_path: Path) -> None:
    """Unlike two JS managers, uv and bun own disjoint trees, so a polyglot repo needs both."""
    _bun_project(tmp_path)
    _touch(tmp_path, "uv.lock", "pyproject.toml")
    assert detect_dep_commands(tmp_path) == ["uv sync", "bun install"]


def test_installed_tree_alone_is_not_a_signal(tmp_path: Path) -> None:
    """A node_modules/.venv without any lockfile must not trigger an install command."""
    _touch(tmp_path, "node_modules/left-pad/index.js", ".venv/pyvenv.cfg")
    assert detect_dep_commands(tmp_path) == []


# --------------------------------------------------------------------------------------------------------------
# tmux argv
# --------------------------------------------------------------------------------------------------------------


def test_tmux_attach_argv_uses_tty_and_exact_target() -> None:
    argv = tmux_attach_argv(_endpoint(port=2299), "fwd-demo")
    assert argv[0] == "ssh"
    assert "-t" in argv
    assert argv[argv.index("-p") + 1] == "2299"
    # "=fwd-demo" is tmux's exact-match syntax; a bare name would fnmatch onto "fwd-demo2".
    assert argv[-1].endswith("tmux attach -t '=fwd-demo'")
    assert "fwd-env.sh" in argv[-1]


def test_tmux_attach_argv_prints_discoverable_next_steps_after_detach() -> None:
    argv = tmux_attach_argv(_endpoint(), "fwd-demo", "demo-project")
    remote_command = argv[-1]
    assert "To attach, use `fwd attach demo-project`" in remote_command
    assert "to stop, run `fwd stop demo-project`." in remote_command
    assert "status=$?" in remote_command
    assert 'exit \"$status\"' in remote_command


def test_tmux_attach_remote_command_is_a_single_argv_element() -> None:
    """ssh takes the remote command as one string; splitting it would break on any quoted argument."""
    argv = tmux_attach_argv(_endpoint(), "fwd-demo")
    assert sum(1 for part in argv if "tmux" in part) == 1


def test_tmux_helpers_issue_the_expected_remote_commands(monkeypatch) -> None:
    """tmux_new/kill/exists are thin wrappers; assert on the remote command string they build."""
    from fwd import remote as remote_mod

    issued: list[tuple[str, dict]] = []

    class FakeCompleted:
        returncode = 0

    def fake_run(self, cmd, **kwargs):  # noqa: ANN001 - test double matching SSHEndpoint.run
        issued.append((cmd, kwargs))
        return FakeCompleted()

    monkeypatch.setattr(SSHEndpoint, "run", fake_run)
    endpoint = _endpoint()

    remote_mod.tmux_new(endpoint, "fwd-demo", "/home/dev/proj", "claude", env={"FOO": "bar baz"})
    remote_mod.tmux_kill(endpoint, "fwd-demo")
    assert remote_mod.tmux_exists(endpoint, "fwd-demo") is True

    # tmux_new issues two commands: the create, then the liveness re-check.
    new_cmd, verify_cmd, kill_cmd, exists_cmd = (cmd for cmd, _ in issued)
    assert "tmux new-session -d -s fwd-demo -c /home/dev/proj bash -lc" in new_cmd
    assert f"sleep {remote_mod.TMUX_SETTLE_SECONDS}" in verify_cmd
    assert "has-session -t '=fwd-demo'" in verify_cmd
    # The inner shell snippet is shlex-quoted into the tmux argv, so the export survives one level of nesting.
    assert "bar baz" in new_cmd
    assert "kill-session -t '=fwd-demo'" in kill_cmd
    assert "has-session -t '=fwd-demo'" in exists_cmd
    # Killing something that is already gone must not raise.
    assert issued[2][1]["check"] is False


def test_tmux_exact_target_survives_zsh_equals_expansion() -> None:
    """A bare `=name` is special syntax in zsh; every remote tmux target must keep literal shell quotes."""
    from fwd.remote import _tmux_exact_target

    assert _tmux_exact_target("fwd-demo") == "'=fwd-demo'"
    result = subprocess.run(["zsh", "-c", f"printf %s {_tmux_exact_target('fwd-demo')}"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == "=fwd-demo"


def test_tmux_new_raises_when_the_session_dies_immediately(monkeypatch) -> None:
    """`tmux new-session -d` returns 0 once the session exists, not once the command is running.

    A wiped claude binary makes the pane exit within milliseconds, and fwd used to report a ready session over a dead
    tmux server. The post-create liveness re-check is what turns that silent success into a real error.
    """
    import pytest

    from fwd import remote as remote_mod
    from fwd.sshexec import SSHError

    class FakeCompleted:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(self, cmd, **kwargs):  # noqa: ANN001 - test double matching SSHEndpoint.run
        if "new-session" in cmd:
            return FakeCompleted(0)
        if "has-session" in cmd:
            return FakeCompleted(1)  # the pane already exited
        return FakeCompleted(1)  # command -v claude: not found

    monkeypatch.setattr(SSHEndpoint, "run", fake_run)
    with pytest.raises(SSHError) as excinfo:
        remote_mod.tmux_new(_endpoint(), "fwd-demo", "/home/dev/proj", "claude --resume abc")

    message = str(excinfo.value)
    assert "exited immediately" in message
    assert "claude" in message
    assert "not found on PATH" in message


def test_tmux_new_succeeds_when_the_session_stays_alive(monkeypatch) -> None:
    from fwd import remote as remote_mod

    class FakeCompleted:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(SSHEndpoint, "run", lambda self, cmd, **kw: FakeCompleted())
    remote_mod.tmux_new(_endpoint(), "fwd-demo", "/home/dev/proj", "claude")  # must not raise


# --------------------------------------------------------------------------------------------------------------
# bootstrap.sh
# --------------------------------------------------------------------------------------------------------------


def test_run_bootstrap_passes_requested_agent_to_script(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run_script(self, script, **kwargs):  # noqa: ANN001 - endpoint-shaped test double
        captured["script"] = script
        captured.update(kwargs)

    monkeypatch.setattr(SSHEndpoint, "run_script", fake_run_script)
    run_bootstrap(_endpoint(), tool_prefix="/tools", remote_dir="/project", scratch="/cache", agent="codex")

    assert captured["script"] == BOOTSTRAP_PATH
    assert captured["env"] == {
        "FWD_TOOL_PREFIX": "/tools",
        "FWD_REMOTE_DIR": "/project",
        "FWD_SCRATCH": "/cache",
        "FWD_AGENT": "codex",
    }
    assert captured["check"] is True
    assert captured["stream"] is True


def test_bootstrap_minimal_mode_writes_env_file_and_marker(tmp_path: Path) -> None:
    """FWD_BOOTSTRAP_MINIMAL=1 must produce a complete environment with zero network access."""
    prefix = tmp_path / "tools"
    remote_dir = tmp_path / "proj"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FWD_TOOL_PREFIX": str(prefix),
        "FWD_REMOTE_DIR": str(remote_dir),
        "FWD_SCRATCH": str(tmp_path / "scratch"),
        "FWD_BOOTSTRAP_MINIMAL": "1",
    }
    (tmp_path / "home").mkdir()

    first = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr

    env_file = prefix / "fwd-env.sh"
    assert env_file.is_file()
    text = env_file.read_text(encoding="utf-8")
    assert f'export UV_CACHE_DIR="{tmp_path / "scratch"}/uv-cache"' in text
    assert str(prefix / "bin") in text
    assert remote_dir.is_dir()
    assert list(prefix.glob(".fwd-bootstrap-*"))

    # Shell hook is written once and only once, no matter how many bootstraps run.
    rc = tmp_path / "home" / ".bashrc"
    second = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert "skipping" in second.stdout
    assert rc.read_text(encoding="utf-8").count("# fwd environment") == 1
    assert (tmp_path / "home" / ".profile").read_text(encoding="utf-8").count("# fwd environment") == 1


def test_remote_commands_do_not_depend_on_an_exported_tool_prefix() -> None:
    """`ssh host 'cmd'` is a non-interactive, non-login shell: no rc file has run, so FWD_TOOL_PREFIX is unset.

    Regression test for a bootstrap that installed uv/bun correctly while every dependency install still died with
    "uv: command not found", because the env-file path expanded to "/fwd-env.sh".
    """
    from fwd.remote import _source_env

    prefix = _source_env()
    assert '"$HOME/.fwd-env.sh"' in prefix
    # The FWD_TOOL_PREFIX branch must be guarded, never expanded blind.
    assert '"/fwd-env.sh"' not in prefix
    assert '[ -n "${FWD_TOOL_PREFIX:-}" ]' in prefix


def test_bootstrap_writes_a_home_pointer_and_rewrites_it_when_missing(tmp_path: Path) -> None:
    """The $HOME pointer is what makes non-login shells work, so a marker without a pointer must force a re-run.

    That combination is real on RunPod: the marker sits on the persistent volume while $HOME is container disk that
    the platform wipes when the pod stops.
    """
    prefix = tmp_path / "tools"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "FWD_TOOL_PREFIX": str(prefix),
        "FWD_REMOTE_DIR": str(tmp_path / "proj"),
        "FWD_BOOTSTRAP_MINIMAL": "1",
    }
    assert subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True).returncode == 0

    pointer = home / ".fwd-env.sh"
    assert pointer.is_file()
    assert str(prefix / "fwd-env.sh") in pointer.read_text(encoding="utf-8")

    # Wipe only the pointer; the marker survives. The next bootstrap must not fast-exit.
    pointer.unlink()
    second = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    # "already applied" is the marker fast-exit; MINIMAL mode's "skipping tool installs" is a different message.
    assert "already applied" not in second.stdout
    assert pointer.is_file()


def _fake_tool(bindir: Path, name: str) -> Path:
    """Create a stub executable that answers --version, so bootstrap's `have`/version guards are satisfied offline."""
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text(f'#!/bin/sh\necho "{name} 1.0.0"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def test_bootstrap_marker_is_not_trusted_when_a_tool_is_missing(tmp_path: Path) -> None:
    """A version marker on persistent storage can outlive binaries on ephemeral disk — RunPod wipes $HOME on stop.

    Regression test for a restarted pod being permanently broken: the marker short-circuited bootstrap, so the wiped
    claude binary was never reinstalled and every later `fwd up` reported success over a tmux session that died.
    Runs with FWD_BOOTSTRAP_MINIMAL unset (the path that does the real installs) but with stubs on PATH and no curl,
    so nothing touches the network.
    """
    prefix = tmp_path / "tools"
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fakebin"
    for tool in ("uv", "claude", "tmux"):
        _fake_tool(fake_bin, tool)
    # Stub the network installers to fail instantly. fake_bin shadows /usr/bin, so the real curl/npm are unreachable
    # and this test can never touch the network no matter which install branch bootstrap takes.
    for blocked in ("curl", "npm"):
        path = fake_bin / blocked
        path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        path.chmod(0o755)

    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "FWD_TOOL_PREFIX": str(prefix),
        "FWD_REMOTE_DIR": str(tmp_path / "proj"),
    }
    first = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert list(prefix.glob(".fwd-bootstrap-*"))

    # Marker and pointer both intact and the tools run: this one must fast-exit.
    second = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert "already applied" in second.stdout

    # Now simulate the pod restart: the payload is gone, the marker on the volume is not.
    (fake_bin / "claude").unlink()
    third = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    assert third.returncode == 0, third.stderr
    assert "already applied" not in third.stdout
    assert "claude" in third.stderr


def test_bootstrap_codex_mode_validates_codex_without_requiring_claude(tmp_path: Path) -> None:
    """A Codex launch must not install or validate Claude, which may legitimately be absent on a fresh target."""
    prefix = tmp_path / "tools"
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fakebin"
    for tool in ("uv", "codex", "tmux"):
        _fake_tool(fake_bin, tool)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "FWD_TOOL_PREFIX": str(prefix),
        "FWD_REMOTE_DIR": str(tmp_path / "proj"),
        "FWD_AGENT": "codex",
    }

    first = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)
    second = subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True)

    assert first.returncode == 0, first.stderr
    assert "codex present" in first.stdout
    assert "claude" not in first.stdout
    assert "already applied" in second.stdout


def test_bootstrap_requires_its_contract_vars(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(BOOTSTRAP_PATH)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "FWD_TOOL_PREFIX" in result.stderr


def test_bootstrap_marker_is_version_stamped_and_stale_ones_are_removed(tmp_path: Path) -> None:
    prefix = tmp_path / "tools"
    prefix.mkdir()
    stale = prefix / ".fwd-bootstrap-0"
    stale.write_text("old", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FWD_TOOL_PREFIX": str(prefix),
        "FWD_REMOTE_DIR": str(tmp_path / "proj"),
        "FWD_BOOTSTRAP_MINIMAL": "1",
    }
    assert subprocess.run(["bash", str(BOOTSTRAP_PATH)], env=env, capture_output=True, text=True).returncode == 0
    assert not stale.exists()
    assert len(list(prefix.glob(".fwd-bootstrap-*"))) == 1
