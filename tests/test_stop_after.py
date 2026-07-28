"""Remote stop-after helper rendering and backend contract tests."""

from __future__ import annotations

import subprocess

import pytest

from fwd import stop_after
from fwd.backends.runpod import RunpodBackend
from fwd.config import Config, RunpodTargetConfig
from fwd.state import SessionState


class FakeEndpoint:
    """Record remote writes without requiring an SSH server."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs):
        del kwargs
        self.commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")


class SelfStoppingBackend:
    """Minimal backend double exposing only the new remote lifecycle hook."""

    def remote_stop_command(self, session: SessionState) -> str:
        return f"provider-stop {session.name}"


class UnsupportedBackend:
    """Third-party-style backend that explicitly lacks a remote stop action."""

    def remote_stop_command(self, session: SessionState) -> None:
        del session
        return None


def session() -> SessionState:
    """Build one representative persisted session."""
    return SessionState(
        name="demo",
        backend="ssh",
        local_cwd="/local/project",
        remote_dir="/remote/project",
        tmux_session="fwd-demo",
        endpoint={},
        flags={"tool_prefix": "/remote/.fwd-tools"},
    )


def test_prepare_installs_remote_action_agent_helper_mapping_and_guidance() -> None:
    endpoint = FakeEndpoint()

    action = stop_after.prepare(endpoint, SelfStoppingBackend(), session(), agent_guidance=True)

    assert action == "/remote/.fwd-tools/stop-after/demo/action"
    rendered = "\n".join(endpoint.commands)
    assert "provider-stop demo" in rendered
    assert "fwd-tasks-demo" in rendered
    assert "FWD_STOP_AFTER_SCRIPT" in rendered
    assert "/stop-after/by-tmux/fwd-demo" in rendered
    assert "$HOME/.codex/AGENTS.md" in rendered
    assert "$HOME/.claude/CLAUDE.md" in rendered


def test_prepare_rejects_backend_without_remote_stop_contract() -> None:
    with pytest.raises(stop_after.StopAfterUnsupported):
        stop_after.prepare(FakeEndpoint(), UnsupportedBackend(), session())


def test_runpod_remote_stop_uses_pod_scoped_environment_with_recorded_fallback() -> None:
    target = RunpodTargetConfig(name="pod")
    backend = RunpodBackend(target, Config(targets={"pod": target}))
    value = session()
    value.backend = "runpod"
    value.backend_ids["pod_id"] = "pod-123"

    command = backend.remote_stop_command(value)

    assert "RUNPOD_POD_ID" in command
    assert "pod-123" in command
    assert 'runpodctl pod stop "$pod_id"' in command


def test_agent_environment_exports_exact_session_action() -> None:
    wrapped = stop_after.with_agent_environment("codex", "/tools/stop-after/demo/action")

    assert "FWD_STOP_AFTER_SCRIPT=/tools/stop-after/demo/action" in wrapped
    assert "exec codex" in wrapped


def test_rendered_remote_action_is_valid_bash() -> None:
    rendered = stop_after._render_action(session(), "provider-stop demo")

    result = subprocess.run(["bash", "-n"], input=rendered, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr


def test_cancel_reports_when_remote_shutdown_already_began() -> None:
    endpoint = FakeEndpoint()

    def too_late(command: str, **kwargs):
        del kwargs
        endpoint.commands.append(command)
        return subprocess.CompletedProcess(["ssh"], 3, stdout="", stderr="already stopping")

    endpoint.run = too_late  # type: ignore[method-assign]

    assert stop_after.cancel(endpoint, session()) is False
