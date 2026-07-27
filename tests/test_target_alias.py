"""Behavioural contract for root-level target and backend shorthand commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fwd.config import Config, RunpodTargetConfig, SshTargetConfig
from fwd.state import SessionState, StateStore


def _session(name: str, target: str, created: str, *, attached: str | None = None) -> SessionState:
    """Build the smallest state entry needed to establish target-use recency."""
    return SessionState(
        name=name,
        backend="ssh",
        local_cwd="/tmp/project",
        remote_dir="/tmp/project",
        tmux_session=f"fwd-{name}",
        endpoint={"host": "example", "user": ""},
        created_at=created,
        last_attached=attached,
        flags={"target": target},
    )


def test_exact_target_name_wins_over_backend_shorthand(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd.ops import target_alias

    config = Config(
        targets={
            "ssh": RunpodTargetConfig(name="ssh"),
            "machine": SshTargetConfig(name="machine", host="machine.example"),
        }
    )
    selection = target_alias.resolve("ssh", config)
    assert selection is not None
    assert selection.target.name == "ssh"
    assert selection.reason == "configured target"


def test_backend_selects_most_recently_used_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd.ops import target_alias

    state = StateStore(tmp_path / "state.json")
    state.upsert(_session("older-launch", "alpha", "2026-01-03T00:00:00+00:00"))
    state.upsert(_session("recently-attached", "beta", "2026-01-01T00:00:00+00:00", attached="2026-01-04T00:00:00+00:00"))
    monkeypatch.setattr(target_alias, "store", lambda: state)
    config = Config(targets={"alpha": SshTargetConfig(name="alpha", host="a"), "beta": SshTargetConfig(name="beta", host="b")})

    selection = target_alias.resolve("ssh", config)
    assert selection is not None
    assert selection.target.name == "beta"
    assert selection.reason == "most recently used target for backend"


def test_backend_with_multiple_unused_targets_is_explicitly_ambiguous(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from fwd.ops import target_alias

    class EmptyStore:
        def all(self) -> list[SessionState]:
            return []

    monkeypatch.setattr(target_alias, "store", EmptyStore)
    config = Config(targets={"alpha": SshTargetConfig(name="alpha", host="a"), "beta": SshTargetConfig(name="beta", host="b")})
    with pytest.raises(Exception):
        target_alias.resolve("ssh", config)
    assert "multiple configured targets but no usage history" in capsys.readouterr().err


def test_noninteractive_target_alias_fails_before_launch_and_names_explicit_form(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from fwd.ops import launch as launch_ops
    from fwd.ops import target_alias

    config = Config(targets={"work": SshTargetConfig(name="work", host="work.example")})
    monkeypatch.setattr(target_alias, "load_config", lambda: config)
    monkeypatch.setattr(target_alias, "interactive_terminal", lambda: False)
    called = False

    def fake_launch(**kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(launch_ops, "launch", fake_launch)
    with pytest.raises(Exception):
        target_alias.forward("work")
    assert not called
    output = capsys.readouterr().err
    assert "selector 'work' resolved to target 'work'" in output
    assert "fwd up --target work" in output


def test_interactive_target_alias_launches_default_and_attaches(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from fwd.ops import launch as launch_ops
    from fwd.ops import target_alias

    config = Config(targets={"work": SshTargetConfig(name="work", host="work.example", user="sid", port=2200)})
    monkeypatch.setattr(target_alias, "load_config", lambda: config)
    monkeypatch.setattr(target_alias, "interactive_terminal", lambda: True)
    received: dict[str, object] = {}
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: received.update(kwargs))

    target_alias.forward("work")

    assert received == {"target": "work", "initial_command": None, "attach": True}
    output = capsys.readouterr().err
    assert output.splitlines()[0] == "info: fwd work → fwd up --target work --attach -- claude"
    assert "sid@work.example:2200" in output


def test_missing_backend_never_runs_setup_noninteractively(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from fwd import wizard
    from fwd.ops import target_alias

    monkeypatch.setattr(target_alias, "load_config", lambda: Config())
    monkeypatch.setattr(target_alias, "interactive_terminal", lambda: False)
    monkeypatch.setattr(wizard, "run_wizard", lambda **kwargs: pytest.fail("wizard must not run"))
    with pytest.raises(Exception):
        target_alias.forward("ssh")
    output = capsys.readouterr().err
    assert "non-interactive mode" in output
    assert "fwd setup --backend ssh --help" in output


def test_missing_backend_offers_scoped_setup_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd import wizard
    from fwd.ops import launch as launch_ops
    from fwd.ops import target_alias

    empty = Config()
    configured = Config(targets={"cluster": SshTargetConfig(name="cluster", host="cluster.example")})
    configs = iter((empty, empty, configured))
    monkeypatch.setattr(target_alias, "load_config", lambda: next(configs))
    monkeypatch.setattr(target_alias, "interactive_terminal", lambda: True)
    monkeypatch.setattr(target_alias.ui, "confirm", lambda prompt, default: True)
    wizard_args: dict[str, object] = {}
    launch_args: dict[str, object] = {}
    monkeypatch.setattr(wizard, "run_wizard", lambda **kwargs: wizard_args.update(kwargs))
    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: launch_args.update(kwargs))

    target_alias.forward("ssh")

    assert wizard_args == {"force_interactive": True, "backend": "ssh"}
    assert launch_args == {"target": "cluster", "initial_command": None, "attach": True}


def test_static_command_has_priority_over_same_named_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer._click import Context
    from typer.main import get_command

    from fwd.cli import app
    from fwd.ops import target_alias

    monkeypatch.setattr(target_alias, "load_config", lambda: Config(targets={"stop": SshTargetConfig(name="stop", host="example")}))
    root = get_command(app)
    resolved = root.get_command(Context(root), "stop")
    assert resolved is not None
    assert "suspend billable compute" in (resolved.help or "")


def test_root_completion_includes_targets_and_backend_shorthands(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer._click import Context
    from typer.main import get_command

    from fwd.cli import app
    from fwd.ops import target_alias

    monkeypatch.setattr(target_alias, "load_config", lambda: Config(targets={"work": SshTargetConfig(name="work", host="example")}))
    root = get_command(app)
    values = {item.value for item in root.shell_complete(Context(root), "")}
    assert {"ssh", "runpod", "slurm", "work"} <= values


def test_recognized_dynamic_alias_is_invocable_through_root_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise command construction itself so Typer/Click abstract-class incompatibilities cannot hide behind unit seams."""
    from fwd.cli import app
    from fwd.ops import target_alias

    forwarded: list[str] = []
    monkeypatch.setattr(target_alias, "recognized", lambda selector: selector == "work")
    monkeypatch.setattr(target_alias, "forward", forwarded.append)

    result = CliRunner().invoke(app, ["work"])

    assert result.exit_code == 0, result.output
    assert forwarded == ["work"]


def test_unknown_name_remains_a_normal_click_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd.cli import app
    from fwd.ops import target_alias

    monkeypatch.setattr(target_alias, "load_config", lambda: Config())
    result = CliRunner().invoke(app, ["does-not-exist"])
    assert result.exit_code != 0
    assert "No such command 'does-not-exist'" in result.output
