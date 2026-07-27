"""Shared selector grammar and reuse-policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from fwd import cli
from fwd.config import Config, RunpodTargetConfig, SshTargetConfig
from fwd.ops import session_select
from fwd.state import SessionState


def _session(
    name: str,
    cwd: Path,
    *,
    target: str = "work",
    backend: str = "ssh",
    command: tuple[str, ...] = ("claude",),
    created: str = "2026-01-01T00:00:00+00:00",
) -> SessionState:
    return SessionState(
        name=name,
        backend=backend,
        local_cwd=str(cwd),
        remote_dir="/remote/project",
        tmux_session=f"fwd-{name}",
        endpoint={},
        created_at=created,
        flags={"target": target, "initial_command": list(command)},
    )


def test_parser_prioritizes_target_over_same_named_agent_and_explains_disambiguation(tmp_path: Path, capsys) -> None:
    config = Config(targets={"codex": SshTargetConfig(name="codex", host="example")})
    selector = session_select.parse(("codex",), config=config, sessions=[])

    assert selector.target is not None
    assert selector.target.exact_name == "codex"
    assert selector.agent is None
    output = capsys.readouterr().err
    assert "names both a target and coding agent" in output
    assert "fwd up --agent codex" in output
    assert "[targets.codex]" in output
    assert "fwd config" in output


def test_explicit_target_leaves_positional_agent_unambiguous() -> None:
    config = Config(targets={"pod": RunpodTargetConfig(name="pod")})
    selector = session_select.parse(("codex",), config=config, sessions=[], target="pod")

    assert selector.target is not None
    assert selector.target.exact_name == "pod"
    assert selector.agent == "codex"
    assert selector.initial_command == ("codex",)


def test_exact_session_name_wins_and_other_selectors_are_conjunctive(tmp_path: Path) -> None:
    current = _session("demo", tmp_path, target="pod", backend="runpod", command=("codex",))
    config = Config(targets={"pod": RunpodTargetConfig(name="pod")})
    selector = session_select.parse(("demo",), config=config, sessions=[current], target="pod", agent="codex")

    assert selector.name == "demo"
    assert session_select.matching_sessions([current], selector, cwd=tmp_path) == [current]
    mismatch = session_select.parse(("demo",), config=config, sessions=[current], agent="claude")
    assert session_select.matching_sessions([current], mismatch, cwd=tmp_path) == []


def test_unnamed_matching_stays_in_current_project_and_prefers_recent_use(tmp_path: Path) -> None:
    old = _session("old", tmp_path, command=("codex",), created="2026-01-01T00:00:00+00:00")
    new = _session("new", tmp_path, command=("codex",), created="2026-01-02T00:00:00+00:00")
    other = _session("other-project", tmp_path / "other", command=("codex",), created="2026-01-03T00:00:00+00:00")
    selector = session_select.SessionSelector(agent="codex")

    assert session_select.matching_sessions([old, other, new], selector, cwd=tmp_path) == [new, old]


def test_gpu_is_a_conjunctive_launch_selector(tmp_path: Path) -> None:
    cpu = _session("cpu", tmp_path, target="pod", backend="runpod", command=("codex",))
    gpu = _session("gpu", tmp_path, target="pod", backend="runpod", command=("codex",))
    gpu.flags["gpu"] = "NVIDIA A100"
    selector = session_select.parse(("codex",), config=Config(), sessions=[cpu, gpu], agent=None, gpu="NVIDIA A100")

    assert session_select.matching_sessions([cpu, gpu], selector, cwd=tmp_path) == [gpu]


def test_up_reuse_attaches_matching_session_in_interactive_terminal(tmp_path: Path, monkeypatch) -> None:
    matched = _session("demo", tmp_path, command=("codex",))
    selector = session_select.SessionSelector(agent="codex")
    attached = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    selection = session_select.CurrentSelection(selector=selector, config=Config(), sessions=(matched,), cwd=tmp_path, matches=(matched,))
    monkeypatch.setattr(session_select, "select_current", lambda *args, **kwargs: selection)
    from fwd.ops import attach as attach_ops

    monkeypatch.setattr(attach_ops, "attach", lambda name, **kwargs: attached.append((name, kwargs)))

    cli._run_up(("codex",), reuse=True)

    assert attached == [("demo", {"restart": False})]


def test_up_reuse_noninteractive_no_match_prints_exact_creation_command(tmp_path: Path, monkeypatch, capsys) -> None:
    selector = session_select.SessionSelector(target=session_select.TargetSelector("runpod", "runpod", backend="runpod"), agent="codex")
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    selection = session_select.CurrentSelection(selector=selector, config=Config(), sessions=(), cwd=tmp_path, matches=())
    monkeypatch.setattr(session_select, "select_current", lambda *args, **kwargs: selection)

    with pytest.raises(typer.Exit):
        cli._run_up(("runpod", "codex"), reuse=True, create_argv=("fwd", "up", "runpod", "codex"))

    output = capsys.readouterr().err
    assert "non-interactive mode" in output
    assert "fwd up runpod codex" in output
    assert output.rstrip().endswith("`fwd up runpod codex`")


@pytest.mark.parametrize("reuse_flag", ["--reuse", "-r"])
def test_up_cli_passes_positional_and_flag_selectors_to_shared_dispatch(monkeypatch, reuse_flag: str) -> None:
    dispatched = []
    monkeypatch.setattr(cli, "_run_up", lambda positional, **kwargs: dispatched.append((positional, kwargs)))

    result = CliRunner().invoke(cli.app, ["up", reuse_flag, "--target", "pod", "--agent", "codex", "--name", "demo"])

    assert result.exit_code == 0, result.output
    positional, options = dispatched[0]
    assert positional == ()
    assert options["target"] == "pod"
    assert options["agent"] == "codex"
    assert options["name"] == "demo"
    assert options["reuse"] is True
    assert options["create_argv"] == ("fwd", "up", "--target", "pod", "--agent", "codex", "--name", "demo")


def test_up_cli_returns_streamed_command_exit_status(monkeypatch) -> None:
    """A remote command failure must remain scriptable instead of being hidden behind successful provisioning."""
    monkeypatch.setattr(cli, "_run_up", lambda positional, **kwargs: 7)

    result = CliRunner().invoke(cli.app, ["up", "echo", "hi"])

    assert result.exit_code == 7


@pytest.mark.parametrize(("retired", "replacement"), [("--connect", "--reuse"), ("-c", "-r")])
def test_retired_connect_option_fails_instead_of_becoming_a_remote_command(monkeypatch, retired: str, replacement: str) -> None:
    dispatched = []
    monkeypatch.setattr(cli, "_run_up", lambda positional, **kwargs: dispatched.append((positional, kwargs)))

    result = CliRunner().invoke(cli.app, ["up", retired])

    assert result.exit_code != 0
    assert f"{retired} was renamed to {replacement}" in result.output
    assert dispatched == []


def test_reuse_creation_command_preserves_remote_short_flags_after_separator(monkeypatch) -> None:
    dispatched = []
    monkeypatch.setattr(cli, "_run_up", lambda positional, **kwargs: dispatched.append((positional, kwargs)))

    result = CliRunner().invoke(cli.app, ["up", "--reuse", "--", "bash", "-c", "echo hello"])

    assert result.exit_code == 0, result.output
    assert dispatched[0][0] == ("bash", "-c", "echo hello")
    assert dispatched[0][1]["create_argv"] == ("fwd", "up", "--", "bash", "-c", "echo hello")


def test_registered_agent_launch_auto_attaches_only_through_shared_policy(tmp_path: Path, monkeypatch) -> None:
    selector = session_select.SessionSelector(agent="codex")
    selection = session_select.CurrentSelection(selector=selector, config=Config(), sessions=(), cwd=tmp_path, matches=())
    launched = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(session_select, "select_current", lambda *args, **kwargs: selection)
    from fwd.ops import launch as launch_ops

    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: launched.append(kwargs))

    cli._run_up(("codex",))

    assert launched[0]["initial_command"] == ("codex",)
    assert launched[0]["attach"] is True


@pytest.mark.parametrize(("attach", "expected_stream"), [(False, True), (True, False)])
def test_explicit_up_command_streams_by_default_or_attaches_directly(tmp_path: Path, monkeypatch, attach: bool, expected_stream: bool) -> None:
    """Explicit command argv must use the durable task streamer unless the caller requests direct tmux attachment."""
    selector = session_select.SessionSelector(command=("echo", "hi"))
    selection = session_select.CurrentSelection(selector=selector, config=Config(), sessions=(), cwd=tmp_path, matches=())
    launched: list[dict[str, object]] = []
    streamed: list[tuple[tuple[str, ...], str | None]] = []
    state = _session("demo", tmp_path, command=("echo", "hi"))
    monkeypatch.setattr(session_select, "select_current", lambda *args, **kwargs: selection)
    from fwd.ops import launch as launch_ops
    from fwd.ops import send as send_ops

    monkeypatch.setattr(launch_ops, "launch", lambda **kwargs: launched.append(kwargs) or state)
    monkeypatch.setattr(send_ops, "run_command", lambda arguments, *, name=None: streamed.append((arguments, name)) or 7)

    result = cli._run_up(("echo", "hi"), attach=attach)

    assert launched[0]["initial_command"] == ("echo", "hi")
    assert launched[0]["run_command_as_task"] is expected_stream
    assert launched[0]["attach"] is attach
    assert streamed == ([(("echo", "hi"), "demo")] if expected_stream else [])
    assert result == (7 if expected_stream else None)


def test_bare_selector_flags_forward_to_reuse_dispatch(monkeypatch) -> None:
    dispatched = []
    monkeypatch.setattr(cli, "_run_up", lambda positional, **kwargs: dispatched.append((positional, kwargs)))

    result = CliRunner().invoke(cli.app, ["--target", "pod", "--agent", "codex", "--name", "demo"])

    assert result.exit_code == 0, result.output
    assert dispatched[0][0] == ()
    assert dispatched[0][1]["target"] == "pod"
    assert dispatched[0][1]["agent"] == "codex"
    assert dispatched[0][1]["name"] == "demo"
    assert dispatched[0][1]["reuse"] is True
