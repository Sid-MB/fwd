"""One-shot remote command execution for ``fwd send`` / ``fwd s``.

Design intent
-------------
Sending a command is deliberately narrower than attaching: it resolves the same live endpoint but never allocates,
starts, repairs, or attaches to compute. This makes it safe for agents and scripts, which can read ordinary stdout,
stderr, and exit status without risking an interactive terminal takeover or an implicit billing transition.

Commands execute through the endpoint's non-interactive SSH login shell from the session's remote project directory.
Each argv element is shell-quoted locally before it crosses SSH, preserving argument boundaries and preventing a value
such as a filename containing spaces from becoming shell syntax. Callers that intentionally need pipes, redirects, or
expansion can request a shell explicitly, for example ``fwd send -- bash -lc 'cat *.json | jq .'``.
"""

from __future__ import annotations

import shlex

import typer

from fwd import ui
from fwd.backends.base import TargetStatus
from fwd.ops import launch as launch_ops
from fwd.sshexec import SSHError


def send(command: tuple[str, ...], *, name: str | None = None, timeout: float | None = None) -> None:
    """Run one command in a running session's remote project directory.

    Args:
        command: Remote argv following the CLI's ``--`` separator.
        name: Session name; ``None`` resolves the session associated with the current local directory.
        timeout: Optional local timeout in seconds for the complete SSH command.

    The remote process inherits fwd's stdout and stderr, so terminal users see output live and agents can capture the
    two streams normally. Its exit status becomes fwd's exit status. A non-running or unknowable target is an error:
    this operation never prompts for or performs a restart.
    """
    if not command:
        ui.die("no remote command specified; use 'fwd send -- COMMAND [ARG ...]'")

    session = launch_ops.resolve_session(name)
    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)
    if status is not TargetStatus.RUNNING:
        remedies = {
            TargetStatus.STOPPED: "restart it explicitly with 'fwd attach --restart'",
            TargetStatus.PENDING: "wait for it to become running",
            TargetStatus.GONE: "the remote resource no longer exists",
            TargetStatus.JOB_ENDED: "start a new allocation with 'fwd attach'",
            TargetStatus.UNKNOWN: "run 'fwd doctor' and retry when status is available",
        }
        ui.die(f"cannot send a command to session {session.name!r}: target status is {status}; {remedies.get(status, 'the target must be running')}")

    try:
        endpoint = backend.endpoint(session)
        remote_command = f"cd {shlex.quote(session.remote_dir)} && {shlex.join(command)}"
        completed = endpoint.run(remote_command, check=False, capture=False, timeout=timeout)
    except SSHError as exc:
        ui.die(str(exc))
    except Exception as exc:
        ui.die(f"could not execute command on session {session.name!r}: {exc}")
    raise typer.Exit(completed.returncode)

