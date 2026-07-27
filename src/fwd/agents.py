"""Remote coding-agent registry.

An agent is a magic one-word ``fwd up`` command with three pieces of behavior: the executable to bootstrap, the
command to start, and an optional settings synchronizer. Launch orchestration consumes this registry instead of
branching on agent names, so adding another agent is a small registration change rather than another launch mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fwd.sshexec import SSHEndpoint


@dataclass(frozen=True)
class AgentSpec:
    """Describe one supported remote coding agent."""

    name: str
    command: tuple[str, ...]
    sync_settings: Callable[[SSHEndpoint], None] | None = None


def _sync_codex(endpoint: SSHEndpoint) -> None:
    """Import lazily so ordinary CLI startup does not load archive/SSH machinery."""
    from fwd import codex_state

    codex_state.upload_user_config(endpoint)


AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(name="claude", command=("claude",)),
    "codex": AgentSpec(name="codex", command=("codex",), sync_settings=_sync_codex),
}


def resolve(command: tuple[str, ...]) -> AgentSpec | None:
    """Return the registered agent for an exact magic command, otherwise ``None``."""
    if len(command) != 1:
        return None
    return AGENTS.get(command[0])

