"""Scaffold smoke tests — the contract guard for parallel development.

These deliberately test *shape*, not behaviour. Four teammates are filling in different modules against the shared
contracts at the same time, so the fastest possible feedback that someone broke an import, renamed a dataclass field or
dropped a CLI command is worth more here than any single functional assertion. Behavioural tests belong beside the
implementations that arrive later.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Every module in the package. Importing all of them catches circular imports and syntax errors in one shot.
MODULES = [
    "fwd",
    "fwd.cli",
    "fwd.config",
    "fwd.state",
    "fwd.sshexec",
    "fwd.sync",
    "fwd.remote",
    "fwd.claude_state",
    "fwd.doctor",
    "fwd.wizard",
    "fwd.ui",
    "fwd.backends",
    "fwd.backends.base",
    "fwd.backends.ssh_host",
    "fwd.backends.runpod",
    "fwd.backends.slurm",
    "fwd.ops",
    "fwd.ops.launch",
    "fwd.ops.attach",
    "fwd.ops.lifecycle",
    "fwd.ops.transfer",
]

EXPECTED_COMMANDS = {"up", "launch", "attach", "ls", "push", "pull", "stop", "rm", "setup", "doctor", "version"}


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    """Every fwd module imports cleanly."""
    assert importlib.import_module(module) is not None


def test_version_present() -> None:
    import fwd

    assert fwd.__version__


def test_typer_app_registers_all_commands() -> None:
    """The Typer app instantiates and exposes the full command surface from the plan."""
    from fwd.cli import app

    names = {cmd.name or cmd.callback.__name__ for cmd in app.registered_commands}
    assert EXPECTED_COMMANDS <= names, f"missing commands: {sorted(EXPECTED_COMMANDS - names)}"


def test_help_lists_commands() -> None:
    """`fwd --help` renders and mentions the primary commands."""
    from fwd.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("up", "attach", "ls", "push", "pull", "stop", "rm", "setup", "doctor"):
        assert name in result.output


def test_version_command() -> None:
    from fwd import __version__
    from fwd.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_backend_registry_resolves_all_backends() -> None:
    """get_backend lazily imports each registered backend and the classes satisfy the Provisioner protocol shape."""
    from fwd.backends import BACKENDS, backend_names, get_backend

    assert backend_names() == ["runpod", "slurm", "ssh"]
    for name in BACKENDS:
        cls = get_backend(name)
        assert cls.name == name
        for method in ("provision", "endpoint", "status", "stop", "destroy", "doctor"):
            assert callable(getattr(cls, method))


def test_unknown_backend_raises_provision_error() -> None:
    from fwd.backends import ProvisionError, get_backend

    with pytest.raises(ProvisionError):
        get_backend("nope")


def test_state_roundtrip(tmp_path: Path) -> None:
    """StateStore is fully implemented, so verify the core upsert/lookup/remove cycle."""
    from fwd.state import SessionState, StateStore, endpoint_to_dict
    from fwd.sshexec import SSHEndpoint

    store = StateStore(tmp_path / "state.json")
    endpoint = SSHEndpoint(host="1.2.3.4", user="root", port=2222, supports_rsync=False)
    session = SessionState(
        name="demo",
        backend="ssh",
        local_cwd=str(tmp_path),
        remote_dir="/workspace/demo",
        tmux_session="fwd-demo",
        endpoint=endpoint_to_dict(endpoint),
    )
    store.upsert(session)

    assert [s.name for s in store.all()] == ["demo"]
    assert store.get("demo") is not None
    assert store.get_for_cwd(tmp_path).name == "demo"
    assert store.get("demo").ssh_endpoint() == endpoint
    assert store.update("demo", remote_dir="/other").remote_dir == "/other"
    assert store.get("demo").remote_dir == "/other"
    assert store.remove("demo") is True
    assert store.remove("demo") is False


def test_corrupt_state_degrades_to_empty(tmp_path: Path) -> None:
    """A truncated or garbage state file must not break the CLI."""
    from fwd.state import StateStore

    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert StateStore(path).all() == []


def test_config_deep_merge_and_target_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project config must be able to override one field of a globally-declared target."""
    from fwd import config as config_mod

    global_path = tmp_path / "global.toml"
    global_path.write_text(
        'default_target = "cluster"\n'
        "[targets.cluster]\n"
        'backend = "slurm"\n'
        'login_host = "login.example"\n'
        'user = "sid"\n'
        'remote_base = "/scratch/sid"\n'
        'alloc = "--time=04:00:00"\n',
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    (project / ".fwd").mkdir(parents=True)
    (project / ".fwd" / "config.toml").write_text('[targets.cluster]\nalloc = "--time=08:00:00 --gres=gpu:1"\n', encoding="utf-8")

    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    cfg = config_mod.load_config(project)

    target = cfg.target()
    assert target.backend == "slurm"
    assert target.alloc == "--time=08:00:00 --gres=gpu:1"
    # Inherited from the global file rather than reset to the dataclass default.
    assert target.login_host == "login.example"
    assert target.remote_base == "/scratch/sid"
    assert cfg.sources == [global_path, project / ".fwd" / "config.toml"]


def test_config_no_targets_raises() -> None:
    from fwd.config import Config, ConfigError

    with pytest.raises(ConfigError):
        Config().target()


def test_bootstrap_script_ships_and_is_valid_bash() -> None:
    """bootstrap.sh must exist as package data and parse, since it is piped to a remote shell verbatim."""
    from fwd.remote import BOOTSTRAP_PATH

    assert BOOTSTRAP_PATH.is_file()
    text = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for var in ("FWD_TOOL_PREFIX", "FWD_REMOTE_DIR", "FWD_SCRATCH"):
        assert var in text
    assert subprocess.run(["bash", "-n", str(BOOTSTRAP_PATH)], capture_output=True).returncode == 0


def test_cli_entrypoint_help_runs_as_subprocess() -> None:
    """The installed console script path works end to end, not just the in-process app."""
    result = subprocess.run([sys.executable, "-m", "fwd.cli", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "up" in result.stdout
