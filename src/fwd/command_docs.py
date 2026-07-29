"""Canonical session-command documentation and context-aware examples.

Command hints used to be assembled independently in lifecycle tables, post-attach output, and Typer help. That made
small wording or syntax improvements easy to apply in one place while leaving another stale. This module keeps each
command's public name, summary, and example construction together; operations decide *when* a hint is applicable,
while this module decides which commands are suggested.

The functions return command strings understood by :func:`fwd.ui.show_code_examples`. They never print, inspect
configuration, or contact a backend, so callers can reuse them in interactive, Markdown, JSON-adjacent, and post-SSH
contexts without side effects.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Sequence

from fwd import ui
from fwd.backends.base import TargetStatus
from fwd.state import SessionState

UNKNOWN_STATUS = "?"
START_HEADING = "Start a session:"
MANAGE_HEADING = "Manage a session:"
NEXT_STEPS_HEADING = "Next steps:"


@dataclass(frozen=True, slots=True)
class CommandDoc:
    """One command's canonical name and summary shared by hints and ``--help``."""

    name: str
    summary: str


UP = CommandDoc("up", "Provision or reuse a target, synchronize the project, bootstrap its tools, and start a persistent session.")
ATTACH = CommandDoc("attach", "Attach to the unambiguous existing session matching the supplied selectors.")
SEND = CommandDoc("send", "Start, follow, background, list, or cancel durable remote tasks.")
LIST = CommandDoc("ls", "List managed sessions with live backend status.")
PORTS = CommandDoc("ports", "Open, list, or close loopback-only local ports forwarded to a running session.")
STOP = CommandDoc("stop", "Check the remote Git worktree, kill tmux, and suspend billable compute while retaining configured persistent storage.")
REMOVE = CommandDoc("rm", "Destroy one or more session targets, or every tracked target with --all, and forget their state; confirmation identifies running work and remote data at risk.")


def _session_example(command: CommandDoc, session_name: str) -> str:
    """Build a positional session-command example using shell-safe quoting."""
    return shlex.join([ui.COMMAND_NAME, command.name, session_name])


def send_example(session_name: str) -> str:
    """Build a directly runnable durable-command example for one known session."""
    return shlex.join([ui.COMMAND_NAME, SEND.name, "--name", session_name, "--", "echo", "hello"])


def start_session_examples() -> tuple[str, ...]:
    """Return launch guidance for an empty session list without implying that a session can already be managed."""
    return (ui.command(UP.name), ui.command(f"{UP.name} runpod codex"))


def manage_session_examples(session_statuses: Sequence[tuple[SessionState, TargetStatus | str]]) -> tuple[str, ...]:
    """Return only management commands applicable to at least one displayed session.

    The caller supplies statuses it already queried for the table, preventing hint rendering from repeating provider
    calls. Unknown targets remain stoppable because stopping may be the safest billing action, while send requires a
    confirmed running or pending target and removal remains available for every tracked entry.
    """
    if not session_statuses:
        return ()
    examples: list[str] = []
    attachable = next((session for session, status in session_statuses if status == TargetStatus.RUNNING), None)
    attachable = attachable or next((session for session, status in session_statuses if status == TargetStatus.PENDING), None)
    attachable = attachable or next((session for session, status in session_statuses if status != TargetStatus.GONE), None)
    sendable = next((session for session, status in session_statuses if status in (TargetStatus.RUNNING, TargetStatus.PENDING)), None)
    stoppable = next((session for session, status in session_statuses if status in (TargetStatus.RUNNING, TargetStatus.PENDING, TargetStatus.UNKNOWN, UNKNOWN_STATUS)), None)
    if attachable is not None:
        examples.append(_session_example(ATTACH, attachable.name))
    if sendable is not None:
        examples.append(send_example(sendable.name))
    if stoppable is not None:
        examples.append(_session_example(STOP, stoppable.name))
    examples.append(_session_example(REMOVE, session_statuses[0][0].name))
    return tuple(examples)


def post_attach_examples(session_name: str) -> tuple[str, ...]:
    """Return commands useful immediately after detaching or exiting an attached session."""
    return (
        _session_example(ATTACH, session_name),
        _session_example(STOP, session_name),
        ui.command(LIST.name),
    )
