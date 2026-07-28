"""Class-based coding-agent registry.

Each supported coding agent lives in one module and implements :class:`Agent`. The launch and send layers consume
only that contract, which keeps agent-specific state transfer and command construction out of general orchestration.
"""

from __future__ import annotations

from fwd.agents.base import Agent, AgentLaunchOptions
from fwd.agents.claude import ClaudeAgent
from fwd.agents.codex import CodexAgent

AGENTS: dict[str, Agent] = {agent.name: agent for agent in (ClaudeAgent(), CodexAgent())}

# Compatibility name for integrations written against the original data-only registry. It deliberately aliases the
# richer class contract rather than preserving a second abstraction.
AgentSpec = Agent


def resolve(command: tuple[str, ...]) -> Agent | None:
    """Return the registered agent for an exact magic command, otherwise ``None``."""
    if len(command) != 1:
        return None
    return AGENTS.get(command[0])


__all__ = ["AGENTS", "Agent", "AgentLaunchOptions", "AgentSpec", "resolve"]
