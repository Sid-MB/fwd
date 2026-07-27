"""Shared selector parsing and session matching for ``up``, ``attach``, and bare ``fwd``.

The CLI accepts the same concepts through several spellings: a session name, a configured target or backend, a
registered coding agent, or an arbitrary startup command. Keeping their precedence here prevents subtle drift where
``fwd up codex`` launches one thing but ``fwd attach codex`` searches for another.

Resolution is deliberately local and side-effect free. Exact session names win first. A target/backend consumes the
next positional token before an agent does, which makes a configured target named ``codex`` deterministic; the warning
explains ``--agent codex`` and how to rename the target. Remaining words are either one registered agent or an exact
arbitrary command. Session matching applies every supplied selector and, unless an exact name was given, stays within
the current project directory.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import typer

from fwd import agents, ui
from fwd.config import Config, ConfigError, GLOBAL_CONFIG_PATH, TARGET_TYPES, implicit_target
from fwd.state import SessionState, StateStore


@dataclass(frozen=True, slots=True)
class TargetSelector:
    """A user-facing target token plus its launch and matching interpretation."""

    raw: str
    launch_name: str
    exact_name: str | None = None
    backend: str | None = None

    def matches(self, session: SessionState) -> bool:
        """Return whether ``session`` satisfies this configured-target or backend selector."""
        if self.exact_name is not None:
            return session.flags.get("target") == self.exact_name
        return self.backend is not None and session.backend == self.backend


@dataclass(frozen=True, slots=True)
class SessionSelector:
    """Normalized selectors shared by launch and attachment entry points."""

    name: str | None = None
    target: TargetSelector | None = None
    agent: str | None = None
    command: tuple[str, ...] | None = None
    gpu: str | None = None

    @property
    def initial_command(self) -> tuple[str, ...] | None:
        """Return explicit startup argv, or ``None`` when target/project configuration should choose the default."""
        if self.agent is not None:
            return agents.AGENTS[self.agent].command
        return self.command

    @property
    def constrained(self) -> bool:
        """Return whether the user supplied anything beyond the implicit current-project selector."""
        return any((self.name, self.target, self.agent, self.command is not None, self.gpu))

    def describe(self) -> str:
        """Render a concise description for errors without exposing provider credentials."""
        parts = []
        if self.name:
            parts.append(f"name={self.name!r}")
        if self.target:
            parts.append(f"target={self.target.raw!r}")
        if self.agent:
            parts.append(f"agent={self.agent!r}")
        if self.command is not None:
            parts.append(f"command={shlex.join(self.command)!r}")
        if self.gpu:
            parts.append(f"gpu={self.gpu!r}")
        return ", ".join(parts) or "the current project"


@dataclass(frozen=True, slots=True)
class CurrentSelection:
    """One immutable local config/state snapshot plus the sessions matching its normalized selector."""

    selector: SessionSelector
    config: Config
    sessions: tuple[SessionState, ...]
    cwd: Path
    matches: tuple[SessionState, ...]


def _recency(session: SessionState) -> str:
    """Prefer actual use time over creation time when choosing among several matching sessions."""
    return session.last_attached or session.created_at


def _backend_target(token: str, config: Config, sessions: Sequence[SessionState]) -> TargetSelector:
    """Resolve a backend token to its most recently used configured target while retaining backend-wide matching."""
    configured = {name for name, target in config.targets.items() if target.backend == token}
    matching_history = [session for session in sessions if session.backend == token and session.flags.get("target") in configured]
    if matching_history:
        launch_name = max(matching_history, key=_recency).flags.get("target")
        if isinstance(launch_name, str) and launch_name:
            return TargetSelector(token, launch_name, backend=token)
    if len(configured) == 1:
        return TargetSelector(token, next(iter(configured)), backend=token)
    if len(configured) > 1:
        names = ", ".join(sorted(configured))
        ui.die(f"backend {token!r} has multiple configured targets and no usage history selects one: {names}. Pass --target NAME.")
    # RunPod is a complete built-in zero-config target. Other backend names continue into launch so its existing
    # backend-specific configuration error remains authoritative.
    return TargetSelector(token, token, backend=token)


def target_selector(token: str, config: Config, sessions: Sequence[SessionState]) -> TargetSelector | None:
    """Interpret ``token`` as an exact target, backend, or zero-config target; return ``None`` for ordinary commands."""
    if token in config.targets:
        target = config.targets[token]
        return TargetSelector(token, token, exact_name=token, backend=target.backend)
    if token in TARGET_TYPES:
        return _backend_target(token, config, sessions)
    inferred = implicit_target(token)
    if inferred is not None:
        target, _ = inferred
        return TargetSelector(token, target.name, exact_name=target.name, backend=target.backend)
    return None


def _warn_target_agent_collision(token: str) -> None:
    """Explain deterministic target precedence and both immediate and permanent ways to remove the ambiguity."""
    ui.warn(
        f"selector {token!r} names both a target and coding agent; treating it as the target. "
        f"Select the agent explicitly with {ui.command(f'up --agent {token}')!r}. To remove the conflict, rename "
        f"[targets.{token}] in the config file that defines it (often {GLOBAL_CONFIG_PATH}); run {ui.command('config')!r} "
        "to see loaded config files."
    )


def parse(
    positional: Sequence[str],
    *,
    config: Config,
    sessions: Sequence[SessionState],
    target: str | None = None,
    agent: str | None = None,
    name: str | None = None,
    gpu: str | None = None,
) -> SessionSelector:
    """Parse flags and positional selectors using the shared session-target-agent-command precedence."""
    remaining = list(positional)
    known_sessions = {session.name for session in sessions}
    selected_name = name
    selected_target = target_selector(target, config, sessions) if target else None
    selected_agent = agent

    if selected_agent is not None and selected_agent not in agents.AGENTS:
        ui.die(f"unknown coding agent {selected_agent!r}; choose one of: {', '.join(sorted(agents.AGENTS))}")

    if remaining and remaining[0] in known_sessions:
        positional_name = remaining.pop(0)
        if selected_name is not None and selected_name != positional_name:
            ui.die(f"session specified twice: {selected_name!r} and {positional_name!r}")
        selected_name = positional_name

    if remaining and selected_target is None:
        candidate = target_selector(remaining[0], config, sessions)
        if candidate is not None:
            token = remaining.pop(0)
            if token in agents.AGENTS:
                _warn_target_agent_collision(token)
            selected_target = candidate

    command: tuple[str, ...] | None = None
    if remaining:
        if selected_agent is not None:
            ui.die(f"cannot combine --agent {selected_agent} with an arbitrary command: {shlex.join(remaining)}")
        if len(remaining) == 1 and remaining[0] in agents.AGENTS:
            selected_agent = remaining[0]
        else:
            command = tuple(remaining)

    return SessionSelector(name=selected_name, target=selected_target, agent=selected_agent, command=command, gpu=gpu)


def matches(session: SessionState, selector: SessionSelector, *, cwd: Path) -> bool:
    """Return whether one stored session satisfies every selector and the implicit project scope."""
    if selector.name is not None:
        if session.name != selector.name:
            return False
    elif Path(session.local_cwd).expanduser().resolve() != cwd.expanduser().resolve():
        return False
    if selector.target is not None and not selector.target.matches(session):
        return False
    initial_command = tuple(str(part) for part in session.flags.get("initial_command", ("claude",)))
    if selector.agent is not None and initial_command != agents.AGENTS[selector.agent].command:
        return False
    if selector.command is not None and initial_command != selector.command:
        return False
    if selector.gpu is not None and session.flags.get("gpu") != selector.gpu:
        return False
    return True


def matching_sessions(sessions: Iterable[SessionState], selector: SessionSelector, *, cwd: Path) -> list[SessionState]:
    """Return matching sessions newest-use first."""
    return sorted((session for session in sessions if matches(session, selector, cwd=cwd)), key=_recency, reverse=True)


def parse_current(
    positional: Sequence[str],
    *,
    target: str | None = None,
    agent: str | None = None,
    name: str | None = None,
    gpu: str | None = None,
    project_dir: Path | None = None,
    state: StateStore | None = None,
) -> tuple[SessionSelector, Config, list[SessionState], Path]:
    """Load local config/state once and parse selectors against the same snapshot used for matching."""
    from fwd.config import load_config

    cwd = (project_dir or Path.cwd()).expanduser().resolve()
    store = state or StateStore()
    sessions = store.all()
    try:
        config = load_config(cwd)
    except (ConfigError, OSError) as exc:
        ui.die(str(exc))
    return parse(positional, config=config, sessions=sessions, target=target, agent=agent, name=name, gpu=gpu), config, sessions, cwd


def select_current(
    positional: Sequence[str],
    *,
    target: str | None = None,
    agent: str | None = None,
    name: str | None = None,
    gpu: str | None = None,
    project_dir: Path | None = None,
    state: StateStore | None = None,
) -> CurrentSelection:
    """Parse and match against one state snapshot, rejecting an exact name combined with conflicting selectors."""
    selector, config, sessions, cwd = parse_current(positional, target=target, agent=agent, name=name, gpu=gpu, project_dir=project_dir, state=state)
    matched = matching_sessions(sessions, selector, cwd=cwd)
    exact = next((candidate for candidate in sessions if selector.name and candidate.name == selector.name), None)
    if exact is not None and not matched:
        ui.die(f"session {exact.name!r} exists but does not match every supplied selector ({selector.describe()}); use a different --name or remove the conflicting selector")
    return CurrentSelection(selector=selector, config=config, sessions=tuple(sessions), cwd=cwd, matches=tuple(matched))


def recognized_root_selector(token: str) -> bool:
    """Return whether an unknown root token may be rewritten to ``up --reuse`` instead of rejected by Click."""
    if token in agents.AGENTS or token in TARGET_TYPES:
        return True
    try:
        state = StateStore()
        sessions = state.all()
        if token in {session.name for session in sessions}:
            return True
        from fwd.config import load_config

        return target_selector(token, load_config(), sessions) is not None
    except Exception:
        return False
