"""Codex agent integration."""

from __future__ import annotations

from typing import Mapping

from fwd import ui
from fwd.agents import codex_state
from fwd.agents.base import Agent
from fwd.sshexec import SSHEndpoint
from fwd.tooling.requirements import CODEX


class CodexAgent(Agent):
    """Install Codex, synchronize portable settings, and resume its latest remote conversation."""

    name = "codex"
    command = ("codex",)
    tools = (CODEX,)

    def prepare_remote(self, endpoint: SSHEndpoint, remote_dir: str, flags: dict[str, object], local_state: object | None) -> dict[str, object]:
        """Upload allowlisted Codex settings and skills without copying authentication."""
        del remote_dir, flags, local_state
        with ui.step("Uploading Codex settings and skills"):
            codex_state.upload_user_config(endpoint)
        return {}

    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Start a new interactive Codex conversation."""
        del flags
        return "codex"

    def send_command(self, message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
        """Resume the most recent Codex conversation and emit JSONL suitable for streaming."""
        del flags
        return ("codex", "exec", "--json", "resume", "--last", message)
