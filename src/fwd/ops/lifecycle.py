"""Session lifecycle — list, stop, remove.

Design intent
-------------
``ls`` queries each backend's ``status()`` live rather than trusting stored state, because the worst failure mode for
a tool that spends money is showing "running" for a pod that is gone — or, far more expensively, "stopped" for one
that is still billing.

That live query is also the fragile part: it shells out to ``runpodctl`` or ssh-es into a login node, once per
session. A single unreachable cluster must not take the whole table down, so every per-session lookup is individually
wrapped and degrades to a ``?`` cell. The rule here is that ``fwd ls`` always renders something, whatever else is
broken — it is the command users reach for precisely when things are broken.

``stop`` keeps the state entry and persistent storage, but RunPod CPU pods have no persistent volume and lose their
container disk on stop. ``remove`` is not reversible, so it confirms and names exactly what will be destroyed.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime

import typer

from fwd import remote, remote_tasks, ui
from fwd.backends.base import TargetStatus
from fwd.ops import launch as launch_ops
from fwd.output import OutputFormat
from fwd.send_tasks import SendTaskStore
from fwd.state import SessionState

# Rendered when a backend cannot be reached or has not implemented status yet. Distinct from every real status so the
# table never implies knowledge fwd does not have.
UNKNOWN_STATUS = "?"


def task_store() -> SendTaskStore:
    """Return the durable send-task store through a replaceable test boundary."""
    return SendTaskStore()


def _parse_time(value: str) -> datetime | None:
    """Parse a persisted ISO timestamp as UTC, tolerating malformed and older timezone-naive state."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _compact_duration(timestamp: str, now: datetime) -> str:
    """Render elapsed local time as compact days, hours, and minutes, retaining seconds only below one minute."""
    parsed = _parse_time(timestamp)
    if parsed is None:
        return "?"
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    days, remaining_minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return "".join(parts)


def _short_time(value: str | None, now: datetime) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD HH:MM (age)``, or ``-`` when the session was never attached."""
    if not value:
        return "-"
    return f"{value.replace('T', ' ')[:16]} ({_compact_duration(value, now)})"


def _ids_summary(session: SessionState) -> str:
    """Render ``backend_ids`` as compact ``key=value`` pairs, since each backend stores different handles."""
    if not session.backend_ids:
        return "-"
    return " ".join(f"{k}={v}" for k, v in sorted(session.backend_ids.items()))


def _example_command(action: str, session_name: str | None) -> str:
    """Build a copy-pasteable lifecycle example, retaining an unfenced ``<name>`` placeholder for an empty table."""
    return shlex.join([ui.COMMAND_NAME, action, session_name]) if session_name else ui.command(f"{action} <name>")


def _live_status(session: SessionState) -> TargetStatus | str:
    """Return a session's live status, isolating every failure mode to this one cell.

    Constructing the backend can fail on its own (target deleted from config, backend module unimportable), which is
    why the construction sits inside the same guard as the status call.
    """
    try:
        backend = launch_ops.backend_for(session)
    except typer.Exit:
        # backend_for calls ui.die when the target is unconfigurable; in a table that is one bad row, not fatal.
        return UNKNOWN_STATUS
    except Exception:
        return UNKNOWN_STATUS
    try:
        return backend.status(session)
    except NotImplementedError:
        return UNKNOWN_STATUS
    except Exception:
        return UNKNOWN_STATUS


def ls(*, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """List all sessions with live, backend-reconciled status."""
    sessions = launch_ops.store().all()
    now = datetime.now(UTC)
    rows = []
    for session in sessions:
        status = _live_status(session)
        rows.append(
            [
                session.name,
                session.backend,
                status,
                _compact_duration(session.started_at, now) if status == TargetStatus.RUNNING else "-",
                session.tmux_session,
                session.local_cwd,
                _short_time(session.last_attached, now),
                _ids_summary(session),
            ]
        )
    ui.table(
        f"{ui.command()} sessions ({len(rows)} active)",
        ["name", "backend", "status", "running", "tmux", "local dir", "last attached", "ids"],
        rows,
        output_format=output_format,
    )
    example_name = sessions[0].name if sessions else None
    ui.show_code_examples(
        (
            ("Reattach", _example_command("attach", example_name)),
            ("Stop", _example_command("stop", example_name)),
            ("Remove", _example_command("rm", example_name)),
        ),
        heading="Manage a session:",
    )


def stop(name: str | None = None) -> None:
    """Stop a session: kill the remote tmux session, then suspend the target.

    tmux is killed first and best-effort. If the target is already unreachable the kill is pointless but harmless,
    and letting its failure abort the run would leave a billing pod alive — the expensive half must always execute.

    Args:
        name: Session name; ``None`` uses the session for the current directory.
    """
    session = launch_ops.resolve_session(name)
    backend = launch_ops.backend_for(session)
    interrupted = False

    try:
        endpoint = backend.endpoint(session)
    except Exception:
        endpoint = session.ssh_endpoint()
    try:
        with ui.step(f"Stopping remote session {session.tmux_session!r}"):
            try:
                remote.tmux_kill(endpoint, session.tmux_session)
            finally:
                remote_tasks.kill_manager(endpoint, session.name)
                task_store().cancel_session(session.name)
    except KeyboardInterrupt:
        # The provider stop is the billing-critical half. Ctrl-C may cancel a slow SSH/tmux call, but it must not
        # strand running compute; finish the provider action before honoring the interrupt.
        interrupted = True
        ui.warn("stop interrupted while closing tmux; continuing with the provider stop")
    except Exception as exc:
        ui.warn(f"could not kill the remote tmux session ({exc}); continuing to stop the target")

    try:
        with ui.step(f"Stopping {session.backend} target for {session.name!r}"):
            backend.stop(session)
    except KeyboardInterrupt:
        interrupted = True
        ui.warn("provider stop was interrupted; retrying once so compute is not left billing")
        with ui.step(f"Retrying stop for {session.backend} target {session.name!r}"):
            backend.stop(session)
    if interrupted:
        remaining = max(0, len(launch_ops.store().all()) - 1)
        noun = "session" if remaining == 1 else "sessions"
        ui.warn(f"stop canceled by user after the provider was stopped; {remaining} {noun} still running")
        raise KeyboardInterrupt
    target = getattr(backend, "target", None)
    if session.backend == "runpod" and getattr(target, "compute_type", None) == "cpu":
        ui.ok(f"stopped {session.name!r}; RunPod wiped its CPU container disk, recreate and re-sync with {ui.command(f'attach {session.name}')!r}")
    else:
        ui.ok(f"stopped {session.name!r}; persistent data is preserved, restart with {ui.command(f'attach {session.name}')!r}")


def remove(name: str | None = None, *, force: bool = False) -> None:
    """Destroy a session's target and delete its state entry.

    Irreversible — RunPod volumes and Slurm scratch directories go with it — so the prompt names the backend and
    target explicitly rather than asking a generic "are you sure?".

    Args:
        name: Session name; ``None`` uses the session for the current directory.
        force: Skip the confirmation prompt.
    """
    session = launch_ops.resolve_session(name)
    if not force and not ui.confirm(
        f"destroy the {session.backend} target for session {session.name!r} and delete its data?", default=False
    ):
        ui.info("aborted")
        return

    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)
    try:
        endpoint = backend.endpoint(session)
    except Exception:
        endpoint = session.ssh_endpoint()
    try:
        with ui.step(f"Closing remote sessions for {session.name!r}"):
            remote.tmux_kill(endpoint, session.tmux_session)
            remote_tasks.kill_manager(endpoint, session.name)
    except Exception as exc:
        ui.warn(f"could not close every remote tmux session ({exc}); continuing with target destruction")
    task_store().cancel_session(session.name)
    if status is TargetStatus.GONE:
        ui.info("target is already gone upstream; removing the local state entry only")
    else:
        try:
            with ui.step(f"Destroying {session.backend} target for {session.name!r}"):
                backend.destroy(session)
        except Exception as exc:
            # The state entry is still removed below: keeping a session pointing at a target we failed to destroy
            # would strand the user, and 'fwd ls' would keep showing a row they cannot act on.
            ui.warn(f"could not destroy the target ({exc}); check your provider console for leftover resources")

    launch_ops.store().remove(session.name)
    ui.ok(f"removed session {session.name!r}")
