"""Remote coding-agent registry.

An agent is a magic one-word ``fwd up`` command with three pieces of behavior: the executable to bootstrap, the
command to start, and an optional settings synchronizer. Launch orchestration consumes this registry instead of
branching on agent names, so adding another agent is a small registration change rather than another launch mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from fwd.sshexec import SSHEndpoint
from fwd.tooling import ToolRequirement
from fwd.tooling.requirements import CLAUDE, CODEX


@dataclass(frozen=True)
class AgentSpec:
    """Describe one supported remote coding agent, including tools resolved by the shared remote installer."""

    name: str
    command: tuple[str, ...]
    tools: tuple[ToolRequirement, ...]
    sync_settings: Callable[[SSHEndpoint], None] | None = None
    send_command: Callable[[str, Mapping[str, object]], tuple[str, ...]] | None = None


def _sync_codex(endpoint: SSHEndpoint) -> None:
    """Import lazily so ordinary CLI startup does not load archive/SSH machinery."""
    from fwd import codex_state

    codex_state.upload_user_config(endpoint)


def _claude_send_command(message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
    """Build a streaming Claude Code turn that resumes the launched conversation when its id is known."""
    command = ["claude", "--print", "--verbose", "--output-format", "stream-json"]
    resume_id = flags.get("resume_id")
    if isinstance(resume_id, str) and resume_id:
        command.extend(("--resume", resume_id))
    else:
        command.append("--continue")
    command.append(message)
    return tuple(command)


def _codex_send_command(message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
    """Build a JSONL Codex turn that resumes the most recent conversation in the remote project."""
    del flags
    return ("codex", "exec", "--json", "resume", "--last", message)


AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(name="claude", command=("claude",), tools=(CLAUDE,), send_command=_claude_send_command),
    "codex": AgentSpec(name="codex", command=("codex",), tools=(CODEX,), sync_settings=_sync_codex, send_command=_codex_send_command),
}


def resolve(command: tuple[str, ...]) -> AgentSpec | None:
    """Return the registered agent for an exact magic command, otherwise ``None``."""
    if len(command) != 1:
        return None
    return AGENTS.get(command[0])
