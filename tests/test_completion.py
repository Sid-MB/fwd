"""Tests for state-aware session completion and CLI wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer import _click
from typer.main import get_command

from fwd import cli_completion
from fwd.cli import app
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
