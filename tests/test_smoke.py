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
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Every module in the package. Importing all of them catches circular imports and syntax errors in one shot.
MODULES = [
    "fwd",
    "fwd.cli",
    "fwd.cli_help",
    "fwd.cli_completion",
    "fwd.completion_setup",
    "fwd.skill_setup",
    "fwd.config",
    "fwd.state",
    "fwd.sshexec",
    "fwd.sync",
    "fwd.remote",
    "fwd.remote_tasks",
    "fwd.send_tasks",
    "fwd.task_stream",
    "fwd.output",
    "fwd.agents.claude_state",
    "fwd.agents.codex_state",
    "fwd.doctor",
    "fwd.wizard",
    "fwd.ui",
    "fwd.backends",
    "fwd.backends.base",
    "fwd.backends.ssh",
    "fwd.backends.runpod",
    "fwd.backends.slurm",
    "fwd.ops",
    "fwd.ops.launch",
    "fwd.ops.attach",
    "fwd.ops.lifecycle",
    "fwd.ops.diff",
    "fwd.ops.session_select",
    "fwd.ops.target_alias",
    "fwd.ops.transfer",
]

EXPECTED_COMMANDS = {"up", "launch", "attach", "ls", "push", "pull", "diff", "stop", "rm", "setup", "doctor", "version", "config", "default"}


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

    names = {cmd.name or cmd.callback.__name__ for cmd in app.registered_commands} | {group.name for group in app.registered_groups}
    assert EXPECTED_COMMANDS <= names, f"missing commands: {sorted(EXPECTED_COMMANDS - names)}"


def test_help_lists_commands() -> None:
    """`fwd --help` renders and exposes the primary command names."""
    from fwd.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("up", "attach", "ls", "push", "pull", "stop", "rm", "setup", "doctor"):
        assert name in result.output


def test_up_help_documents_fresh_session_flag() -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["up", "--help"])
    assert result.exit_code == 0
    assert "--new" in result.output


def test_rm_help_documents_bulk_removal() -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["rm", "--help"])
    assert result.exit_code == 0
    assert "--all" in result.output


def test_help_groups_short_aliases_with_canonical_commands() -> None:
    """Aliases remain callable through the same CLI surface."""
    from fwd.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert CliRunner().invoke(app, ["a", "--help"]).exit_code == 0
    assert CliRunner().invoke(app, ["s", "--help"]).exit_code == 0


def test_short_alias_dispatches_with_arguments_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd import cli
    from fwd.cli import app
    from fwd.config import Config
    from fwd.ops import attach, session_select
    from fwd.state import SessionState

    session = SessionState(name="demo", backend="ssh", local_cwd=str(Path.cwd()), remote_dir="/tmp/demo", tmux_session="fwd-demo", endpoint={})
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    selection = session_select.CurrentSelection(selector=session_select.SessionSelector(name="demo"), config=Config(), sessions=(session,), cwd=Path.cwd(), matches=(session,))
    monkeypatch.setattr(session_select, "select_current", lambda *args, **kwargs: selection)
    attached: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(attach, "attach", lambda *args, **kwargs: attached.append((args, kwargs)))
    result = CliRunner().invoke(app, ["a", "demo", "--restart"])

    assert result.exit_code == 0, result.output
    assert attached == [(("demo",), {"restart": True})]


def test_bare_command_dispatches_to_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd import cli
    from fwd.cli import app

    dispatched: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(cli, "_run_up", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0, result.output
    assert dispatched and dispatched[0][1]["reuse"] is True


def test_up_help_exposes_reuse_alias_and_omits_retired_connect_flag() -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["up", "--help"])
    assert result.exit_code == 0
    assert "--reuse" in result.output
    assert "-r" in result.output
    assert "--connect" not in result.output


def test_default_and_config_set_write_the_same_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The convenience command and general mutation surface must share semantics rather than merely similar help."""
    from fwd import config as config_mod
    from fwd.cli import app

    global_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    default_result = CliRunner().invoke(app, ["default", "codex"])
    assert default_result.exit_code == 0, default_result.output
    assert tomllib.loads(global_path.read_text(encoding="utf-8"))["default_command"] == ["codex"]

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    set_result = CliRunner().invoke(app, ["config", "set", "--project", "default_command", "python", "-m", "agent"])
    assert set_result.exit_code == 0, set_result.output
    assert tomllib.loads((project / ".fwd" / "config.toml").read_text(encoding="utf-8"))["default_command"] == ["python", "-m", "agent"]


def test_config_example_backend_compatibility_syntax_still_works() -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["config", "--example", "runpod"])
    assert result.exit_code == 0, result.output
    assert tomllib.loads(result.output)["targets"]["pod"]["backend"] == "runpod"


def test_config_rm_cli_reports_missing_and_removes_with_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd import config as config_mod
    from fwd.cli import app

    global_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    missing = CliRunner().invoke(app, ["config", "rm", "default_command"])
    assert missing.exit_code == 0

    global_path.write_text('default_command = ["codex"]\n', encoding="utf-8")
    removed = CliRunner().invoke(app, ["config", "rm", "default_command", "--force"])
    assert removed.exit_code == 0, removed.output
    assert "default_command" not in tomllib.loads(global_path.read_text(encoding="utf-8"))


def test_config_mutation_rejects_parent_rendering_flags() -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["config", "--schema", "rm", "default_command"])
    assert result.exit_code != 0


def test_version_command() -> None:
    from fwd import __version__
    from fwd.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_backend_registry_resolves_all_backends() -> None:
    """get_backend lazily imports concrete Backend subclasses with runtime and setup contracts."""
    from dataclasses import fields

    from fwd.backends import BACKENDS, Backend, backend_names, get_backend
    from fwd.config import TARGET_TYPES

    assert backend_names() == ["runpod", "slurm", "ssh"]
    for name in BACKENDS:
        cls = get_backend(name)
        assert issubclass(cls, Backend)
        assert cls.name == name
        parameters = cls.config_parameters()
        assert parameters
        assert len({parameter.name for parameter in parameters}) == len(parameters)
        assert all(parameter.flag.startswith("--") and parameter.help for parameter in parameters)
        config_fields = {field.name for field in fields(TARGET_TYPES[name])} - {"name", "backend"}
        assert {parameter.name for parameter in parameters} == config_fields
        for method in ("provision", "endpoint", "status", "stop", "destroy", "doctor"):
            assert callable(getattr(cls, method))


def test_every_backend_config_parameter_has_a_setup_flag() -> None:
    """Backend metadata and the agent-safe non-interactive CLI must never drift apart."""
    from fwd.backends import BACKENDS, get_backend
    from fwd.cli import app

    help_result = CliRunner().invoke(app, ["setup", "--help"])
    assert help_result.exit_code == 0
    for name in BACKENDS:
        for parameter in get_backend(name).config_parameters():
            assert parameter.flag in help_result.output


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


def test_legacy_state_uses_creation_time_as_running_time() -> None:
    """Sessions written before ``started_at`` existed retain a meaningful local duration after upgrade."""
    from fwd.state import SessionState

    session = SessionState.from_dict(
        {
            "name": "legacy",
            "backend": "ssh",
            "local_cwd": "/tmp/project",
            "remote_dir": "/tmp/remote",
            "tmux_session": "fwd-legacy",
            "endpoint": {},
            "created_at": "2026-01-02T03:04:05+00:00",
        }
    )
    assert session.started_at == session.created_at


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
