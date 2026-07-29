"""Common contract for remotely hosted coding agents.

The launch pipeline has three agent extension points: prepare local state before project synchronization, prepare
remote state after tools are installed, and build the long-lived command placed in tmux. Send support belongs here
too, so the CLI never needs to branch on an agent name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from typing import Any, Mapping

from fwd.config import Config
from fwd.sshexec import SSHEndpoint
from fwd.tooling import ToolRequirement

ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AgentLaunchOptions:
    """Agent-related CLI switches passed to every implementation.

    The current switches describe Claude's optional transfer modes. Other agents reject unsupported non-default
    options through :meth:`Agent.launch_flags`; a future cross-agent option can be added here without changing launch
    orchestration.
    """

    session: bool = False
    handoff: bool = False
    user_config: bool = False
    creds: bool = False

    def any(self) -> bool:
        """Return whether the caller explicitly requested any agent-specific behavior."""
        return any((self.session, self.handoff, self.user_config, self.creds))


class Agent(ABC):
    """Base class every built-in coding agent implements.

    Implementations own their executable requirements, state/config transfer, startup and resume commands, and send
    protocol. General launch orchestration calls these hooks at fixed lifecycle points and treats the returned local
    state as opaque, so adding an agent does not require editing the launch pipeline.
    """

    name: str
    command: tuple[str, ...]
    tools: tuple[ToolRequirement, ...]

    def launch_flags(self, config: Config, options: AgentLaunchOptions) -> dict[str, Any]:
        """Resolve CLI/config options into serializable session flags.

        Agents with no special launch flags accept only the default option set. This catches accidental use of
        Claude-only switches without teaching the orchestrator which implementation owns them.
        """
        if options.any():
            raise ValueError(f"agent {self.name!r} does not support --session, --handoff, --user-config, or --creds")
        runtime = config.agent(self.name)
        invalid_names = sorted(name for name in runtime.environment if not ENVIRONMENT_NAME.fullmatch(name))
        if invalid_names:
            raise ValueError(f"agent {self.name!r} has invalid environment variable name(s): {', '.join(invalid_names)}")
        return {
            "agent_full_access": runtime.full_access,
            "agent_args": list(runtime.args),
            "agent_environment": dict(runtime.environment),
        }

    def runtime_args(self, flags: Mapping[str, object]) -> list[str]:
        """Return the configured argv extension recorded with the session."""
        value = flags.get("agent_args")
        return [str(part) for part in value] if isinstance(value, list) else []

    def with_environment_defaults(self, command: str, flags: Mapping[str, object]) -> str:
        """Export configured variables only when the remote shell has not already defined them."""
        value = flags.get("agent_environment")
        if not isinstance(value, dict) or not value:
            return command
        exports = []
        for name, default in value.items():
            if not isinstance(name, str) or not ENVIRONMENT_NAME.fullmatch(name):
                continue
            exports.append(f'[ "${{{name}+x}}" = x ] || export {name}={shlex.quote(str(default))}')
        return f"{'; '.join(exports)}; exec {command}" if exports else command

    def environment_command(self, command: list[str], flags: Mapping[str, object]) -> tuple[str, ...]:
        """Return argv for a non-interactive agent command with the same environment defaults as the TUI."""
        plain = shlex.join(command)
        wrapped = self.with_environment_defaults(plain, flags)
        return tuple(command) if wrapped == plain else ("bash", "-lc", wrapped)

    def prepare_local(self, local_cwd: Path, flags: dict[str, Any]) -> object | None:
        """Prepare state that must exist before project synchronization and return opaque transfer state."""
        del local_cwd, flags
        return None

    def prepare_remote(self, endpoint: SSHEndpoint, remote_dir: str, flags: dict[str, Any], local_state: object | None) -> dict[str, Any]:
        """Install settings/state after bootstrap and return additional serializable session flags."""
        del endpoint, remote_dir, flags, local_state
        return {}

    def restart_command(self, flags: Mapping[str, object]) -> str:
        """Build the command used when restarting an existing session without retransferring state."""
        return self.startup_command(flags)

    @abstractmethod
    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Build the long-lived command placed in the session's primary tmux pane."""

    @abstractmethod
    def send_command(self, message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
        """Build a non-interactive command that sends one message into the agent's current conversation."""
