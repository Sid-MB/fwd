"""Tests for state-aware session completion and CLI wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer import _click
from typer.main import get_command

from fwd import cli_completion
from fwd.cli import app
from fwd.config import Config, RunpodTargetConfig, SshTargetConfig
from fwd.state import SessionState, StateStore


def _session(name: str, directory: Path, *, backend: str = "ssh", target: str = "work", last_attached: str | None = None) -> SessionState:
    return SessionState(
        name=name,
        backend=backend,
        local_cwd=str(directory),
        remote_dir="/remote/project",
        tmux_session=f"fwd-{name}",
        endpoint={"host": "example", "user": "dev", "port": 22},
        flags={"target": target},
        last_attached=last_attached,
    )


def test_complete_session_filters_names_and_provides_help_tooltips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.json")
    store.upsert(_session("alpha", tmp_path / "api", backend="runpod", target="cpu", last_attached="2026-07-27T12:34:00"))
    store.upsert(_session("beta", tmp_path / "web"))
    monkeypatch.setattr(cli_completion, "_session_store", lambda: store)
    items = cli_completion.complete_session(None, [], "a")  # type: ignore[arg-type] - callback ignores context

    assert [item[0] for item in items] == ["alpha"]
    assert items[0][1] == "runpod · target=cpu · dir=api · last=2026-07-27 12:34"


def test_complete_session_never_breaks_shell_on_state_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenStore:
        def all(self):
            raise OSError("locked")

    monkeypatch.setattr(cli_completion, "_session_store", BrokenStore)
    assert cli_completion.complete_session(None, [], "") == []  # type: ignore[arg-type] - callback ignores context


def test_complete_target_combines_configured_builtin_and_ssh_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(targets={"lab": SshTargetConfig(name="lab", host="lab.example", user="sid"), "gpu-box": RunpodTargetConfig(name="gpu-box", compute_type="gpu", gpu="NVIDIA A40")})
    monkeypatch.setattr(cli_completion, "load_config", lambda: config)
    monkeypatch.setattr(cli_completion, "ssh_config_host_aliases", lambda: {"cluster", "lab"})

    items = cli_completion.complete_target(None, [], "")  # type: ignore[arg-type] - callback ignores context

    assert [value for value, _ in items] == ["cluster", "gpu-box", "lab", "runpod"]
    assert dict(items)["cluster"] == "OpenSSH Host alias · zero-config SSH target"
    assert dict(items)["gpu-box"] == "runpod · NVIDIA A40 · secure"
    assert dict(items)["lab"] == "ssh · sid@lab.example"
    assert dict(items)["runpod"] == "built-in RunPod target · CPU by default"


def test_complete_target_keeps_zero_config_default_when_config_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_completion, "load_config", lambda: (_ for _ in ()).throw(OSError("broken")))
    monkeypatch.setattr(cli_completion, "ssh_config_host_aliases", lambda: set())
    assert cli_completion.complete_target(None, [], "run") == [("runpod", "built-in RunPod target · CPU by default")]  # type: ignore[arg-type] - callback ignores context


def test_static_rich_completions_filter_values_and_include_help() -> None:
    assert cli_completion.complete_agent(None, [], "co") == [("codex", "Codex · sync settings, config, and skills; auto-attach in a terminal")]  # type: ignore[arg-type] - callback ignores context
    assert cli_completion.complete_compute_type(None, [], "") == [("cpu", "CPU-only compute · default"), ("gpu", "GPU compute")]  # type: ignore[arg-type] - callback ignores context
    assert cli_completion.complete_output_format(None, [], "j") == [("json", "structured JSON")]  # type: ignore[arg-type] - callback ignores context
    assert cli_completion.complete_config_key(None, [], "default_") == [("default_command", "argv launched by bare fwd"), ("default_target", "target used when --target is omitted")]  # type: ignore[arg-type] - callback ignores context


@pytest.mark.parametrize(
    ("command_name", "parameter_name"),
    (
        ("up", "name"),
        ("attach", "name"),
        ("a", "name"),
        ("send", "name"),
        ("s", "name"),
        ("push", "name"),
        ("pull", "name"),
        ("stop", "name"),
        ("rm", "name"),
    ),
)
def test_every_session_selecting_command_uses_completion(command_name: str, parameter_name: str) -> None:
    root = get_command(app)
    root_context = _click.Context(root)
    command = root.get_command(root_context, command_name)
    assert command is not None
    parameter = next(parameter for parameter in command.params if parameter.name == parameter_name)
    assert parameter._custom_shell_complete is not None


@pytest.mark.parametrize(
    ("command_name", "parameter_name"),
    (
        ("up", "selectors"),
        ("up", "target"),
        ("up", "gpu"),
        ("launch", "selectors"),
        ("launch", "target"),
        ("launch", "gpu"),
        ("default", "command"),
        ("default", "target"),
        ("ls", "output_format"),
        ("setup", "backend"),
        ("setup", "target_name"),
        ("setup", "host"),
        ("setup", "login_host"),
        ("setup", "proxy_jump"),
        ("setup", "compute_type"),
        ("setup", "cloud_type"),
        ("setup", "gpu"),
        ("setup", "image"),
        ("doctor", "target"),
        ("doctor", "output_format"),
        ("info", "output_format"),
    ),
)
def test_every_discoverable_cli_value_uses_rich_completion(command_name: str, parameter_name: str) -> None:
    root = get_command(app)
    root_context = _click.Context(root)
    command = root.get_command(root_context, command_name)
    assert command is not None
    parameter = next(parameter for parameter in command.params if parameter.name == parameter_name)
    assert parameter._custom_shell_complete is not None


def test_config_set_key_and_target_use_rich_completion() -> None:
    root = get_command(app)
    root_context = _click.Context(root)
    config_command = root.get_command(root_context, "config")
    assert config_command is not None
    config_context = _click.Context(config_command, parent=root_context)
    set_command = config_command.get_command(config_context, "set")
    assert set_command is not None
    for parameter_name in ("key", "value", "target"):
        parameter = next(parameter for parameter in set_command.params if parameter.name == parameter_name)
        assert parameter._custom_shell_complete is not None
    rm_command = config_command.get_command(config_context, "rm")
    assert rm_command is not None
    for parameter_name in ("key", "target"):
        parameter = next(parameter for parameter in rm_command.params if parameter.name == parameter_name)
        assert parameter._custom_shell_complete is not None
