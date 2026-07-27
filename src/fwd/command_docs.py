"""Canonical session-command documentation and context-aware examples.

Command hints used to be assembled independently in lifecycle tables, post-attach output, and Typer help. That made
small wording or syntax improvements easy to apply in one place while leaving another stale. This module keeps each
command's public name, hint label, summary, and example construction together; operations decide *when* a hint is
applicable, while this module decides *how* that command is documented and rendered.

The functions return plain ``(label, command)`` records understood by :func:`fwd.ui.show_code_examples`. They never
print, inspect configuration, or contact a backend, so callers can reuse them in interactive, Markdown, JSON-adjacent,
and post-SSH contexts without side effects.
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
    """One command's canonical CLI documentation shared by hints and ``--help``."""

    name: str
    hint_label: str
    summary: str


UP = CommandDoc("up", "Default target and command", "Provision or reuse a target, synchronize the project, bootstrap its tools, and start a persistent session.")
ATTACH = CommandDoc("attach", "Reattach", "Attach to the unambiguous existing session matching the supplied selectors.")
SEND = CommandDoc("send", "Send command", "Start, follow, background, list, or cancel durable remote tasks.")
LIST = CommandDoc("ls", "See all sessions", "List managed sessions with live backend status.")
STOP = CommandDoc("stop", "Stop", "Kill remote tmux and ask the backend to suspend billable compute; storage preservation depends on the target.")
REMOVE = CommandDoc("rm", "Remove", "Destroy one session target, or every tracked target with --all, and forget their state.")


def _session_example(command: CommandDoc, session_name: str) -> tuple[str, str]:
    """Build a positional session-command example using shell-safe quoting."""
    return command.hint_label, shlex.join([ui.COMMAND_NAME, command.name, session_name])


def send_example(session_name: str) -> tuple[str, str]:
    """Build a directly runnable durable-command example for one known session."""
    return SEND.hint_label, shlex.join([ui.COMMAND_NAME, SEND.name, "--name", session_name, "--", "echo", "hello"])


def start_session_examples() -> tuple[tuple[str, str], ...]:
    """Return launch guidance for an empty session list without implying that a session can already be managed."""
    return (
        (UP.hint_label, ui.command(UP.name)),
        ("Choose a target and agent", ui.command(f"{UP.name} runpod codex")),
    )


def manage_session_examples(session_statuses: Sequence[tuple[SessionState, TargetStatus | str]]) -> tuple[tuple[str, str], ...]:
    """Return only management commands applicable to at least one displayed session.

    The caller supplies statuses it already queried for the table, preventing hint rendering from repeating provider
    calls. Unknown targets remain stoppable because stopping may be the safest billing action, while send requires a
    confirmed running or pending target and removal remains available for every tracked entry.
    """
    if not session_statuses:
        return ()
    examples: list[tuple[str, str]] = []
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


def post_attach_examples(session_name: str) -> tuple[tuple[str, str], ...]:
    """Return commands useful immediately after detaching or exiting an attached session."""
    return (
        _session_example(ATTACH, session_name),
        _session_example(STOP, session_name),
        (LIST.hint_label, ui.command(LIST.name)),
    )
