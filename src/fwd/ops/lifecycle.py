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

Before either operation changes remote state, the shared worktree guard refuses tracked or untracked Git changes
unless the caller explicitly forces possible data loss. ``stop`` keeps the state entry and configured persistent
storage; ``remove`` is not reversible, so it confirms and names exactly what will be destroyed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer

from fwd import command_docs, port_forwarding, remote, remote_tasks, stop_after as stop_after_ops, ui, worktree_safety
from fwd.backends.base import Backend, TargetStatus
from fwd.ops import launch as launch_ops
from fwd.output import OutputFormat, OutputValue
from fwd.send_tasks import SendTaskStore
from fwd.session_columns import LS_COLUMNS
from fwd.state import SessionState

# Rendered when a backend cannot be reached or has not implemented status yet. Distinct from every real status so the
# table never implies knowledge fwd does not have.
UNKNOWN_STATUS = command_docs.UNKNOWN_STATUS
LIST_MAX_WORKERS = 8


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


def _ports_output(session: SessionState) -> OutputValue:
    """Return concise forwarding text for humans and typed mapping records for JSON consumers."""
    mappings = port_forwarding.mappings_from_state(session.ports)
    master_active = bool(mappings) and port_forwarding.active(session.ports_ssh_endpoint(), session.name)
    payload = [{"local": mapping.local, "remote": mapping.remote, "active": master_active} for mapping in mappings]
    return OutputValue(port_forwarding.summary(session.ports_ssh_endpoint(), session.name, mappings, master_active=master_active), payload)


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
        probe = getattr(backend, "list_status", backend.status)
        return probe(session)
    except NotImplementedError:
        return UNKNOWN_STATUS
    except Exception:
        return UNKNOWN_STATUS


def _stop_after_summary(session: SessionState, status: TargetStatus | str, tasks: list) -> str:
    """Describe queued remote shutdown and reconcile it once the backend confirms that compute stopped."""
    scheduled = [task for task in tasks if task.session == session.name and task.kind == "stopafter" and task.active]
    if status in (TargetStatus.STOPPED, TargetStatus.JOB_ENDED, TargetStatus.GONE):
        for task in scheduled:
            try:
                task_store().update(task.id, status="completed", exit_code=0, finished_at=datetime.now(UTC).isoformat())
            except Exception:
                pass
        return "-"
    if scheduled:
        return ", ".join(f"{task.id} ({task.status})" for task in scheduled)
    if status != TargetStatus.RUNNING or not session.flags.get("stop_after_script"):
        return "-"
    try:
        endpoint = session.ssh_endpoint()
        try:
            marker = stop_after_ops.status(endpoint, session, timeout=1.0)
        except TypeError:
            # Retain compatibility with third-party/test replacements that implement the original two-argument hook.
            marker = stop_after_ops.status(endpoint, session)
    except Exception:
        return "-"
    return f"agent ({marker})" if marker in {"scheduled", "stopping", "blocked", "failed"} else "-"


def _shown_columns(columns: tuple[str, ...] | None) -> tuple[str, ...]:
    """Return canonical columns, retaining session names when focused output selects another field."""
    if not columns:
        return LS_COLUMNS
    unknown = set(columns) - set(LS_COLUMNS)
    if unknown:
        raise ValueError(f"unknown session column(s): {', '.join(sorted(unknown))}")
    requested = set(columns)
    return tuple(column for column in LS_COLUMNS if column == "name" or column in requested)


def _session_row(
    session: SessionState,
    *,
    shown_columns: tuple[str, ...],
    needs_status: bool,
    tasks: list,
    now: datetime,
) -> tuple[list[object], TargetStatus | str]:
    """Build one independent table row so slow provider and SSH probes can run concurrently."""
    status = _live_status(session) if needs_status else UNKNOWN_STATUS
    values = {
        "name": session.name,
        "backend": session.backend,
        "status": status,
        "stop after": _stop_after_summary(session, status, tasks) if "stop after" in shown_columns else "-",
        "running": _compact_duration(session.started_at, now) if status == TargetStatus.RUNNING else "-",
        "tmux": session.tmux_session,
        "local dir": session.local_cwd,
        "last attached": _short_time(session.last_attached, now),
        "ids": _ids_summary(session),
        "ports": _ports_output(session) if "ports" in shown_columns else "-",
    }
    return [values[column] for column in shown_columns], status


def ls(
    *,
    output_format: OutputFormat | str = OutputFormat.auto,
    all_projects: bool = False,
    columns: tuple[str, ...] | None = None,
    session_names: tuple[str, ...] | None = None,
) -> None:
    """List sessions with optional project, session, and column filtering."""
    tracked_sessions = launch_ops.store().all()
    current_project = Path.cwd().resolve()
    current_sessions = [session for session in tracked_sessions if Path(session.local_cwd).expanduser().resolve() == current_project]
    other_sessions = [session for session in tracked_sessions if Path(session.local_cwd).expanduser().resolve() != current_project]
    if session_names is not None:
        selected_names = set(session_names)
        sessions = [session for session in tracked_sessions if session.name in selected_names]
    else:
        sessions = tracked_sessions if all_projects else current_sessions
    shown_columns = _shown_columns(columns)
    needs_status = any(column in shown_columns for column in ("status", "stop after", "running"))
    now = datetime.now(UTC)
    tasks = []
    if "stop after" in shown_columns:
        try:
            tasks = task_store().all()
        except Exception:
            # Session listing is the recovery UI and must remain usable even when optional task metadata is unreadable.
            tasks = []
    rows: list[list[object]] = []
    session_statuses: list[tuple[SessionState, TargetStatus | str]] = []
    def build_row(session: SessionState) -> tuple[list[object], TargetStatus | str]:
        return _session_row(session, shown_columns=shown_columns, needs_status=needs_status, tasks=tasks, now=now)

    if len(sessions) > 1 and needs_status:
        with ThreadPoolExecutor(max_workers=min(LIST_MAX_WORKERS, len(sessions)), thread_name_prefix="fwd-ls") as executor:
            results = list(executor.map(build_row, sessions))
    else:
        results = [build_row(session) for session in sessions]
    for session, (row, status) in zip(sessions, results, strict=True):
        if needs_status:
            session_statuses.append((session, status))
        rows.append(row)
    ui.table(
        f"{ui.command()} sessions ({len(rows)} active)",
        shown_columns,
        rows,
        output_format=output_format,
    )
    if columns is None:
        examples = command_docs.manage_session_examples(session_statuses) if session_statuses else command_docs.start_session_examples()
        heading = command_docs.MANAGE_HEADING if session_statuses else command_docs.START_HEADING
        ui.show_code_examples(examples, heading=heading)
    if not all_projects and other_sessions and ui.interactive_terminal():
        session_count = len(other_sessions)
        if session_count == 1:
            prefix = "There is 1 session open in another project. Run "
        else:
            project_count = len({Path(session.local_cwd).expanduser().resolve() for session in other_sessions})
            project_noun = "project" if project_count == 1 else "projects"
            prefix = f"There are {session_count} sessions open in {project_count} other {project_noun}. Run "
        ui.info_with_code(prefix, ui.command("ls --all-projects"), " to show.")


def _stop_continuous_sync(session: SessionState) -> None:
    """Tear down a session's continuous sync before its compute goes away, never blocking teardown.

    A Mutagen session outliving its target would keep retrying a connection to a machine that no longer exists and,
    worse, keep a stale two-way session around that could propagate against a *recreated* target later. Stopping it is
    still strictly best-effort: releasing billable compute is the operation that must always complete.
    """
    try:
        from fwd.ops import synccmd

        if synccmd.stop_session(session):
            ui.info(f"stopped continuous sync for {session.name!r}")
    except Exception as exc:
        ui.warn(f"could not stop continuous sync ({exc}); continuing")


def stop(name: str | None = None, *, force: bool = False) -> None:
    """Stop a session: kill the remote tmux session, then suspend the target.

    tmux is killed first and best-effort. If the target is already unreachable the kill is pointless but harmless,
    and letting its failure abort the run would leave a billing pod alive — the expensive half must always execute.

    Args:
        name: Session name, target label, or backend name; ``None`` uses the session for the current directory.
        force: Skip the remote Git worktree safety check.
    """
    session = launch_ops.resolve_session(name)
    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)
    interrupted = False
    try:
        endpoint = backend.endpoint(session)
    except Exception as exc:
        if status not in {TargetStatus.GONE, TargetStatus.STOPPED} and not force:
            ui.die(f"could not reach session {session.name!r} to check for uncommitted Git changes ({exc}); refusing to stop it. Retry when SSH works or pass --force.")
        endpoint = session.ssh_endpoint()
    if status not in {TargetStatus.GONE, TargetStatus.STOPPED}:
        worktree_safety.require_clean(endpoint, session, force=force, action=f"stop session {session.name!r}")
    _stop_continuous_sync(session)
    try:
        from fwd.ops import ports as ports_ops

        ports_ops.close_session_ports(session)
    except Exception as exc:
        ui.warn(f"could not close local port forwarding ({exc}); continuing to stop the target")
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
    if session.backend == "runpod" and getattr(target, "compute_type", None) == "cpu" and not session.backend_ids.get("network_volume_id"):
        ui.ok(f"stopped {session.name!r}; RunPod wiped its CPU container disk, recreate and re-sync with {ui.command(f'attach {session.name}')!r}, delete forever with {ui.command(f'rm {session.name}')!r}")
    else:
        ui.ok(f"stopped {session.name!r}; persistent data is preserved, restart with {ui.command(f'attach {session.name}')!r}")


def _resolve_unique_sessions(names: tuple[str, ...]) -> list[SessionState]:
    """Resolve a batch of selectors before changing remote state, preserving order and collapsing aliases that identify the same session."""
    sessions: list[SessionState] = []
    seen: set[str] = set()
    for name in names:
        session = launch_ops.resolve_session(name)
        if session.name not in seen:
            sessions.append(session)
            seen.add(session.name)
    return sessions


def stop_many(names: tuple[str, ...], *, force: bool = False) -> None:
    """Stop every selected session independently and return a nonzero exit after attempting the full batch if any operation fails."""
    sessions = _resolve_unique_sessions(names)
    failures = 0
    try:
        for session in sessions:
            try:
                stop(session.name, force=force)
            except typer.Exit:
                failures += 1
            except Exception as exc:
                failures += 1
                ui.error(f"could not stop session {session.name!r}: {exc}")
    except KeyboardInterrupt:
        ui.warn(f"batch stop canceled; {len(sessions) - failures} or fewer requested sessions may have been processed")
        raise
    if failures:
        ui.error(f"stopped {len(sessions) - failures} of {len(sessions)} requested sessions; {failures} failed")
        raise typer.Exit(1)


@dataclass(frozen=True, slots=True)
class _RemovalPlan:
    """Snapshot the backend and authoritative state used to explain and execute one removal consistently."""

    session: SessionState
    backend: Backend
    status: TargetStatus


def _prepare_removal(session: SessionState) -> _RemovalPlan:
    """Resolve one backend and its authoritative status before asking the user to authorize any consequences."""
    backend = launch_ops.backend_for(session)
    return _RemovalPlan(session=session, backend=backend, status=launch_ops.status_of(backend, session))


def _removal_data(plan: _RemovalPlan) -> str:
    """Describe the provider-owned data that destroy removes in terms meaningful at the confirmation prompt."""
    session = plan.session
    remote_dir = session.remote_dir or "<unknown remote path>"
    network_volume_id = session.backend_ids.get("network_volume_id")
    if network_volume_id:
        return f"persistent RunPod network volume {network_volume_id!r} and project data at {remote_dir!r}"
    filesystem_id = session.backend_ids.get("filesystem_id")
    if session.backend == "lambda" and filesystem_id:
        return f"persistent Lambda filesystem {filesystem_id!r} and project data at {remote_dir!r}"
    if session.backend == "lambda":
        return f"disposable Lambda instance storage and project data at {remote_dir!r}"
    if session.backend == "runpod":
        return f"RunPod pod storage and project data at {remote_dir!r}"
    if session.backend == "slurm":
        return f"Slurm scratch project directory {remote_dir!r}"
    if session.backend == "ssh":
        return f"remote project directory {remote_dir!r}"
    return f"{session.backend} remote project data at {remote_dir!r}"


def _removal_consequence(plan: _RemovalPlan) -> str | None:
    """Return the destructive consequence to authorize, or ``None`` when only stale local state remains."""
    session = plan.session
    status = plan.status
    if status is TargetStatus.GONE:
        return None
    data = _removal_data(plan)
    runtime = "SSH session" if session.backend == "ssh" else f"{session.backend} target"
    if status is TargetStatus.RUNNING:
        return f"stop the running {runtime} and permanently delete the {data}"
    if status is TargetStatus.PENDING:
        return f"cancel the pending {runtime} and permanently delete the {data}"
    if status is TargetStatus.UNKNOWN:
        return f"the {runtime} state could not be verified; it may still be running, and the {data} may be permanently deleted"
    return f"permanently delete the {data}"


def _confirm_removals(plans: list[_RemovalPlan]) -> bool:
    """Confirm only removals that can stop work or discard data, with one consequence line per affected session."""
    consequences = [(plan.session.name, consequence) for plan in plans if (consequence := _removal_consequence(plan)) is not None]
    if not consequences:
        return True
    noun = "session" if len(plans) == 1 else f"{len(plans)} sessions"
    lines = [f"remove {noun}? This will:"]
    lines.extend(f"  - {name}: {consequence}" for name, consequence in consequences)
    stale_count = len(plans) - len(consequences)
    if stale_count:
        stale_noun = "entry" if stale_count == 1 else "entries"
        lines.append(f"  - clear {stale_count} already-gone local state {stale_noun}; no remote resources will be touched for these")
    return ui.confirm("\n".join(lines), default=True)


def remove(name: str | None = None, *, force: bool = False, _confirmed: bool = False, _plan: _RemovalPlan | None = None) -> None:
    """Destroy a session's target and delete its state entry.

    Confirmed-gone targets have no remote consequence, so their stale local entries are cleared without ceremony.
    Every other state prompts with the running work and provider-owned data that removal can destroy.

    Args:
        name: Session name, target label, or backend name; ``None`` uses the session for the current directory.
        force: Skip the confirmation prompt.
        _confirmed: Internal batch-removal flag indicating that the shared consequence prompt already succeeded.
        _plan: Internal batch-removal snapshot that prevents a second provider query after confirmation.
    """
    plan = _plan or _prepare_removal(launch_ops.resolve_session(name))
    session = plan.session
    if not force and not _confirmed and not _confirm_removals([plan]):
        ui.info("aborted")
        return

    backend = plan.backend
    status = plan.status
    try:
        endpoint = backend.endpoint(session)
    except Exception as exc:
        if status not in {TargetStatus.GONE, TargetStatus.STOPPED} and not force:
            ui.die(f"could not reach session {session.name!r} to check for uncommitted Git changes ({exc}); refusing to remove it. Retry when SSH works or pass --force.")
        endpoint = session.ssh_endpoint()
    if status not in {TargetStatus.GONE, TargetStatus.STOPPED}:
        worktree_safety.require_clean(endpoint, session, force=force, action=f"remove session {session.name!r}")
    _stop_continuous_sync(session)
    try:
        from fwd.ops import ports as ports_ops

        ports_ops.close_session_ports(session)
    except Exception as exc:
        ui.die(f"could not close local port forwarding ({exc}); target destruction was canceled so the tunnel remains tracked")
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


def remove_many(names: tuple[str, ...], *, force: bool = False) -> None:
    """Destroy selected sessions after one consequence-aware confirmation, isolating per-session failures."""
    sessions = _resolve_unique_sessions(names)
    plans = [_prepare_removal(session) for session in sessions]
    count = len(sessions)
    if not force and not _confirm_removals(plans):
        ui.info("aborted")
        return

    failures = 0
    completed = 0
    try:
        for plan in plans:
            session = plan.session
            try:
                remove(session.name, force=force, _confirmed=True, _plan=plan)
                completed += 1
            except typer.Exit:
                failures += 1
            except Exception as exc:
                failures += 1
                ui.error(f"could not remove session {session.name!r}: {exc}")
    except KeyboardInterrupt:
        remaining = count - completed
        remaining_noun = "session" if remaining == 1 else "sessions"
        ui.warn(f"batch removal canceled; {remaining} selected {remaining_noun} were not completed")
        raise
    if failures:
        ui.error(f"removed {completed} of {count} selected sessions; {failures} failed")
        raise typer.Exit(1)


def remove_all(*, force: bool = False) -> None:
    """Destroy every tracked target and delete every session state entry.

    The session and provider-state snapshots are taken before confirmation so the prompt names the exact consequences
    the user authorizes. If every target is already gone, no prompt is needed because only harmless local tracking
    remains. Failures are isolated so one broken target does not prevent later targets from being destroyed.

    Args:
        force: Skip the single bulk confirmation prompt.
    """
    store = launch_ops.store()
    sessions = store.all()
    if not sessions:
        ui.info("no sessions to remove")
        return

    plans = [_prepare_removal(session) for session in sessions]
    count = len(plans)
    noun = "session" if count == 1 else "sessions"
    if not force and not _confirm_removals(plans):
        ui.info("aborted")
        return

    failures = 0
    try:
        for plan in plans:
            session = plan.session
            try:
                remove(session.name, force=force, _confirmed=True, _plan=plan)
            except typer.Exit:
                failures += 1
            except Exception as exc:
                failures += 1
                ui.error(f"could not remove session {session.name!r}: {exc}")
    except KeyboardInterrupt:
        remaining = len(store.all())
        remaining_noun = "session" if remaining == 1 else "sessions"
        ui.warn(f"bulk removal canceled; {remaining} {remaining_noun} remain")
        raise

    remaining = len(store.all())
    removed = count - remaining
    if failures or remaining:
        remaining_noun = "session" if remaining == 1 else "sessions"
        ui.error(f"removed {removed} of {count} sessions; {remaining} {remaining_noun} remain")
        raise typer.Exit(1)
    ui.ok(f"removed all {count} {noun}")
