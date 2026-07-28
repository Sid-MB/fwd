"""Codex agent integration."""

from __future__ import annotations

from typing import Mapping

from fwd import ui
from fwd.agents import codex_state
from fwd.agents.base import Agent
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.tooling.requirements import CODEX


def _remote_control_status(endpoint: SSHEndpoint) -> int:
    """Return 0 when Remote Control and ChatGPT auth are ready, 2 for missing auth, or 1 when unsupported."""
    probe = endpoint.run(
        "codex remote-control start --help >/dev/null 2>&1 || exit 1; "
        "codex login status 2>/dev/null | grep -q 'Logged in using ChatGPT' || exit 2; "
        "printf fwd-codex-remote-control-ready",
        check=False,
    )
    if probe.returncode == 2:
        return 2
    return 0 if probe.returncode == 0 and probe.stdout.strip() == "fwd-codex-remote-control-ready" else 1


def _start_remote_control(endpoint: SSHEndpoint) -> bool:
    """Start the managed app-server daemon without making an optional capability block the primary TUI."""
    try:
        result = endpoint.run("codex remote-control start --json >/dev/null", check=False, timeout=120)
    except SSHError as exc:
        ui.warn(f"could not start Codex Remote Control; continuing with the terminal session ({exc})")
        return False
    if result.returncode != 0:
        ui.warn("could not start Codex Remote Control; continuing with the terminal session")
        return False
    ui.info("Codex Remote Control daemon is running; supported signed-in clients can discover this machine")
    return True


class CodexAgent(Agent):
    """Install Codex, synchronize portable settings, and resume its latest remote conversation."""

    name = "codex"
    command = ("codex",)
    tools = (CODEX,)

    def prepare_remote(self, endpoint: SSHEndpoint, remote_dir: str, flags: dict[str, object], local_state: object | None) -> dict[str, object]:
        """Upload portable state and start the independent remote-control daemon when supported."""
        del remote_dir, flags, local_state
        with ui.step("Uploading Codex settings and skills"):
            codex_state.upload_user_config(endpoint)
        remote_control_status = _remote_control_status(endpoint)
        if remote_control_status == 0:
            with ui.step("Starting Codex Remote Control"):
                return {"remote_control": _start_remote_control(endpoint)}
        if remote_control_status == 2:
            ui.info("Codex Remote Control is installed but requires a ChatGPT login on the remote")
        return {"remote_control": False}

    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Start a new interactive Codex conversation."""
        del flags
        return "codex"

    def send_command(self, message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
        """Resume the most recent Codex conversation and emit JSONL suitable for streaming."""
        del flags
        return ("codex", "exec", "--json", "resume", "--last", message)
