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


def _short_time(value: str | None) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD HH:MM`` for table display, or ``-`` when never set."""
    if not value:
        return "-"
    return value.replace("T", " ")[:16]


def _ids_summary(session: SessionState) -> str:
    """Render ``backend_ids`` as compact ``key=value`` pairs, since each backend stores different handles."""
    if not session.backend_ids:
        return "-"
    return " ".join(f"{k}={v}" for k, v in sorted(session.backend_ids.items()))


def _live_status(session: SessionState) -> str:
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
        return str(backend.status(session))
    except NotImplementedError:
        return UNKNOWN_STATUS
    except Exception:
        return UNKNOWN_STATUS


def ls(*, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """List all sessions with live, backend-reconciled status."""
    sessions = launch_ops.store().all()
    rows = []
    for session in sessions:
        rows.append(
            [
                session.name,
                session.backend,
                _live_status(session),
                session.tmux_session,
                session.local_cwd,
                _short_time(session.last_attached),
                _ids_summary(session),
            ]
        )
    ui.table(
        f"fwd sessions ({len(rows)} active)",
        ["name", "backend", "status", "tmux", "local dir", "last attached", "ids"],
        rows,
        output_format=output_format,
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
        ui.ok(f"stopped {session.name!r}; RunPod wiped its CPU container disk, recreate and re-sync with 'fwd attach {session.name}'")
    else:
        ui.ok(f"stopped {session.name!r}; persistent data is preserved, restart with 'fwd attach {session.name}'")


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
