"""Durable remote task orchestration for ``fwd send``.

``send`` is a task controller rather than an SSH subprocess wrapper. Commands and non-interactive agent turns run in
remote tmux windows, write persistent logs, and receive stable IDs. Any later invocation can follow or cancel them,
and closing the original terminal cannot accidentally kill remote work.

The viewer reserves two local keys only while it is streaming a task: Ctrl-C cancels the remote window, while Ctrl-B
terminates only the SSH log follower and leaves the task in the background. A two-second delay keeps the hint out of
the way for quick commands.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path

import typer

from fwd import agents, github_auth, remote, remote_tasks, stop_after as stop_after_ops, task_stream, ui
from fwd.backends.base import TargetStatus
from fwd.config import ConfigError, load_config
from fwd.ops import launch as launch_ops
from fwd.output import OutputFormat
from fwd.send_tasks import SendTask, SendTaskStore, new_task_id, now
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.state import SessionState


def store() -> SendTaskStore:
    """Return the task store through a replaceable test boundary."""
    return SendTaskStore()


def _running_endpoint(session_name: str | None) -> tuple[SessionState, SSHEndpoint]:
    """Resolve a live session and endpoint without starting or repairing compute."""
    session = launch_ops.resolve_session(session_name)
    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)
    if status is not TargetStatus.RUNNING:
        remedies = {
            TargetStatus.STOPPED: f"restart it explicitly with {ui.command('attach --restart')!r}",
            TargetStatus.PENDING: "wait for it to become running",
            TargetStatus.GONE: "the remote resource no longer exists",
            TargetStatus.JOB_ENDED: f"start a new allocation with {ui.command('attach')!r}",
            TargetStatus.UNKNOWN: f"run {ui.command('doctor')!r} and retry when status is available",
        }
        ui.die(f"cannot send to session {session.name!r}: target status is {status}; {remedies.get(status, 'the target must be running')}")
    return session, backend.endpoint(session)


def _refresh(task: SendTask, endpoint: SSHEndpoint | None = None) -> SendTask:
    """Refresh one active task from its remote marker and persist terminal states."""
    if not task.active:
        return task
    try:
        if endpoint is None:
            _, endpoint = _running_endpoint(task.session)
        status, code = remote_tasks.status(endpoint, task)
    except (Exception, typer.Exit):
        return task
    if status == "unknown":
        return task
    task.status = "canceled" if code == 130 else status
    task.exit_code = code
    if not task.active and task.finished_at is None:
        task.finished_at = now()
    store().upsert(task)
    return task


def _age(timestamp: str) -> str:
    """Render a compact elapsed duration for the task table."""
    try:
        created = datetime.fromisoformat(timestamp)
        seconds = max(0, int((datetime.now(UTC) - created).total_seconds()))
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def list_tasks(*, output_format: OutputFormat = OutputFormat.auto, include_all: bool = False, session_name: str | None = None) -> None:
    """Render durable send tasks, refreshing active rows when their sessions are reachable."""
    tasks = store().all()
    if session_name:
        tasks = [task for task in tasks if task.session == session_name]
    endpoints: dict[str, SSHEndpoint | None] = {}
    for task in tasks:
        if not task.active:
            continue
        if task.session not in endpoints:
            try:
                _, endpoints[task.session] = _running_endpoint(task.session)
            except (Exception, typer.Exit):
                endpoints[task.session] = None
        if endpoints[task.session] is not None:
            _refresh(task, endpoints[task.session])
        elif task.kind == "stopafter":
            session = launch_ops.store().get(task.session)
            if session is not None:
                try:
                    status = launch_ops.status_of(launch_ops.backend_for(session), session)
                except (Exception, typer.Exit):
                    status = TargetStatus.UNKNOWN
                if status in (TargetStatus.STOPPED, TargetStatus.JOB_ENDED, TargetStatus.GONE):
                    finished = now()
                    store().update(task.id, status="completed", exit_code=0, finished_at=finished)
                    task.status = "completed"
                    task.exit_code = 0
                    task.finished_at = finished
    shown = tasks if include_all else [task for task in tasks if task.active]
    active_count = sum(task.active for task in tasks)
    ui.table(
        f"{ui.command('send')} tasks ({active_count} active)",
        ("id", "kind", "status", "session", "after", "running", "command / message"),
        (
            (
                task.id,
                task.agent or task.kind,
                task.status,
                task.session,
                ", ".join(task.dependency_ids) or "-",
                _age(task.created_at),
                task.label,
            )
            for task in shown
        ),
        output_format=output_format,
    )
    if shown:
        ui.info(
            f"attach: {ui.command('send <id>')}    cancel: {ui.command('send cancel <id>')}    "
            f"stop after current work: {ui.command('send stopafter')}    disarm: {ui.command('send cancel stopafter')}"
        )


def follow(task: SendTask, endpoint: SSHEndpoint, *, timeout: float | None = None) -> int:
    """Stream a task and persist the viewer's terminal disposition."""
    result = task_stream.stream(task, endpoint, timeout=timeout)
    if result.disposition == "backgrounded":
        ui.ok(f"Backgrounded task {task.id}; attach with {ui.command(f'send {task.id}')!r}, cancel with {ui.command(f'send {task.id} --stop')!r}")
        return 0
    if result.disposition == "canceled":
        store().update(task.id, status="canceled", exit_code=130, finished_at=now())
        ui.ok(f"Canceled task {task.id}; the {ui.command()} session is still running")
        return 130
    code = result.exit_code
    status = "completed" if code == 0 else ("canceled" if code == 130 else "failed")
    store().update(task.id, status=status, exit_code=code, finished_at=now())
    return code


def _prepare_stop_after(session: SessionState, endpoint: SSHEndpoint) -> str:
    """Install the remotely owned action before work starts, so arming failure cannot strand an unprotected task."""
    backend = launch_ops.backend_for(session)
    return stop_after_ops.prepare(endpoint, backend, session)


def _active_tasks(session: SessionState, endpoint: SSHEndpoint, *, include_stop_after: bool = True) -> list[SendTask]:
    """Refresh and return active tasks for one session, optionally excluding lifecycle actions."""
    tasks = [task for task in store().all() if task.session == session.name]
    refreshed = [_refresh(task, endpoint) for task in tasks]
    for task in refreshed:
        if task.kind != "stopafter" or task.status != "unknown":
            continue
        try:
            marker = stop_after_ops.status(endpoint, session)
        except Exception:
            continue
        if marker in {"idle", "stopped", "canceled"}:
            final_status = "canceled" if marker == "canceled" else "completed"
            final_code = 130 if marker == "canceled" else 0
            finished = now()
            store().update(task.id, status=final_status, exit_code=final_code, finished_at=finished)
            task.status = final_status
            task.exit_code = final_code
            task.finished_at = finished
    active = [task for task in refreshed if task.active]
    return active if include_stop_after else [task for task in active if task.kind != "stopafter"]


def _schedule_stop_after(session: SessionState, endpoint: SSHEndpoint, dependencies: tuple[str, ...], *, prepared_action: str | None = None, force: bool = False) -> SendTask:
    """Queue one stop action after every dependency, rejecting ambiguous duplicate shutdown schedules."""
    existing = [task for task in _active_tasks(session, endpoint) if task.kind == "stopafter"]
    if existing:
        ids = ", ".join(task.id for task in existing)
        ui.die(f"stop-after is already queued for session {session.name!r} ({ids}); cancel it with {ui.command('send cancel stopafter')!r}")
    action = prepared_action or _prepare_stop_after(session, endpoint)
    task = SendTask(
        id=new_task_id("stopafter"),
        session=session.name,
        kind="stopafter",
        command=[action, "--foreground", *(["--force"] if force else [])],
        label=f"stop session {session.name}",
        status="queued" if dependencies else "running",
        dependencies=list(dependencies),
    )
    store().upsert(task)
    try:
        remote_tasks.start(endpoint, session.name, session.remote_dir, task)
    except Exception:
        store().update(task.id, status="failed", exit_code=2, finished_at=now())
        raise
    dependency_text = f" after {', '.join(dependencies)}" if dependencies else ""
    ui.ok(f"Queued stop-after {task.id} for session {session.name!r}{dependency_text}")
    return task


def _start_task(session: SessionState, endpoint: SSHEndpoint, task: SendTask, *, detach: bool, timeout: float | None, stop_after: bool = False, force_stop_after: bool = False) -> int:
    """Persist and start a task, atomically arm an optional remote stop, then either return or follow it."""
    prepared_action = _prepare_stop_after(session, endpoint) if stop_after else None
    stop_task: SendTask | None = None
    store().upsert(task)
    try:
        if stop_after:
            # Arm the waiter first. It blocks on this task's future exit marker, so a local disconnect immediately
            # after the command starts cannot strand compute without its promised remote shutdown.
            stop_task = _schedule_stop_after(session, endpoint, (task.id,), prepared_action=prepared_action, force=force_stop_after)
        remote_tasks.start(endpoint, session.name, session.remote_dir, task)
    except Exception:
        if stop_task is not None:
            remote_tasks.stop(endpoint, stop_task)
            store().update(stop_task.id, status="canceled", exit_code=130, finished_at=now())
        remote_tasks.stop(endpoint, task)
        store().update(task.id, status="failed", exit_code=2, finished_at=now())
        raise
    if task.kind == "agent":
        ui.ok(f"Sent to {task.agent.title()} in session {session.name!r} (task {task.id})")
    else:
        ui.ok(f"Started task {task.id} in session {session.name!r}: {task.label}")
    if detach:
        ui.ok(f"Backgrounded task {task.id}; attach with {ui.command(f'send {task.id}')!r}, cancel with {ui.command(f'send {task.id} --stop')!r}")
        return 0
    return follow(task, endpoint, timeout=timeout)


def _matching_agent_tasks(session_name: str, agent_name: str, endpoint: SSHEndpoint) -> list[SendTask]:
    """Return active tasks for one agent, newest first, after refreshing their remote status."""
    matches = [task for task in store().all() if task.session == session_name and task.kind == "agent" and task.agent == agent_name]
    return [task for task in (_refresh(task, endpoint) for task in matches) if task.active]


def _session_agent(session: SessionState, selector: str) -> agents.Agent:
    """Resolve ``agent`` or an explicit agent name against the session's launched command."""
    launched = agents.resolve(launch_ops.initial_command_for(session))
    if launched is None:
        ui.die(f"session {session.name!r} is not running a registered coding agent; launch one with {ui.command('up codex')!r} or {ui.command('up claude')!r}")
    if selector != "agent" and selector != launched.name:
        ui.die(f"session {session.name!r} is running {launched.name}, not {selector}")
    return launched


def _stop_task(task: SendTask, endpoint: SSHEndpoint) -> None:
    """Cancel one exact task and update local metadata."""
    task = _refresh(task, endpoint)
    if not task.active:
        ui.info(f"task {task.id} is already {task.status}")
        return
    remote_tasks.stop(endpoint, task)
    store().update(task.id, status="canceled", exit_code=130, finished_at=now())
    ui.ok(f"Canceled task {task.id}; the {ui.command()} session is still running")


def _cancel_tasks(session: SessionState, endpoint: SSHEndpoint, selectors: tuple[str, ...]) -> int:
    """Cancel queued work by task id, every queued task, all active tasks, or the stop-after lifecycle action."""
    active = _active_tasks(session, endpoint)
    cancel_remote_stop = selectors == ("stopafter",)
    if not selectors:
        selected = [task for task in active if task.status == "queued"]
        description = "queued tasks"
    elif selectors == ("all",):
        selected = active
        cancel_remote_stop = True
        description = "active tasks"
    elif cancel_remote_stop:
        selected = [task for task in active if task.kind == "stopafter"]
        description = "stop-after"
    else:
        selected = []
        for task_id in selectors:
            task = store().get(task_id)
            if task is None or task.session != session.name:
                ui.die(f"no task {task_id!r} belongs to session {session.name!r}; inspect tasks with {ui.command('send --ls')!r}")
            if task.active:
                selected.append(task)
        description = "selected tasks"
        cancel_remote_stop = any(task.kind == "stopafter" for task in selected)
    cancel_remote_stop = cancel_remote_stop or any(task.kind == "stopafter" for task in selected)
    if cancel_remote_stop and not stop_after_ops.cancel(endpoint, session):
        ui.die(f"stop-after has already begun for session {session.name!r} and can no longer be canceled")
    # Stop lifecycle waiters before their dependencies. Canceling a dependency writes its exit marker, which would
    # otherwise release a still-live stop-after task into its shutdown countdown.
    selected.sort(key=lambda task: task.kind != "stopafter")
    for task in selected:
        remote_tasks.stop(endpoint, task)
        store().update(task.id, status="canceled", exit_code=130, finished_at=now())
    if cancel_remote_stop and not selected:
        ui.ok(f"Canceled stop-after for session {session.name!r}; the session is still running")
    elif not selected:
        ui.info(f"no {description} to cancel in session {session.name!r}")
    else:
        noun = "task" if len(selected) == 1 else "tasks"
        ui.ok(f"Canceled {len(selected)} {noun} in session {session.name!r}; the session is still running")
    return 0


def _run_command_task(
    session: SessionState,
    endpoint: SSHEndpoint,
    arguments: tuple[str, ...],
    *,
    detach: bool,
    timeout: float | None,
    stop_after: bool = False,
    force_stop_after: bool = False,
    setup_github: bool | None = None,
) -> int:
    """Create and run one literal command task against an already-resolved live session."""
    if arguments and Path(arguments[0]).name == "git" and "push" in arguments[1:]:
        _prepare_github_auth(session, endpoint, setup_github=setup_github)
    task = SendTask(
        id=new_task_id("command"),
        session=session.name,
        kind="command",
        command=list(arguments),
        label=shlex.join(arguments),
    )
    try:
        return _start_task(session, endpoint, task, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after)
    except SSHError as exc:
        ui.die(str(exc))
    except Exception as exc:
        ui.die(f"could not start task on session {session.name!r}: {exc}")


def _prepare_github_auth(session: SessionState, endpoint: SSHEndpoint, *, setup_github: bool | None = None) -> None:
    """Apply configured GitHub authentication to an existing session without synchronizing repository content.

    Both direct pushes and coding-agent turns use this path. Preparing agent turns matters because Codex or Claude may
    decide to run ``git push`` internally, after fwd has already handed control to the live conversation.
    """
    if setup_github is None and session.flags.get("github_auth_ready") is True:
        return
    local_cwd = Path(session.local_cwd).expanduser().resolve()
    try:
        cfg = load_config(local_cwd)
    except ConfigError as exc:
        ui.die(str(exc))
    setup_github_effective = cfg.github.auth if setup_github is None else setup_github
    if not setup_github_effective:
        return
    tool_prefix = session.flags.get("tool_prefix")
    if not isinstance(tool_prefix, str) or not tool_prefix:
        ui.die(f"session {session.name!r} predates remote tool metadata; retrieve any remote-only work before repairing it with {ui.command('up')!r}")
    try:
        ready = github_auth.ensure_remote(
            endpoint,
            local_cwd,
            session.remote_dir,
            tool_prefix,
            required=setup_github is True,
        )
        if ready:
            session.flags["github_auth_ready"] = True
            launch_ops.store().update(session.name, flags=session.flags)
    except (github_auth.GitHubAuthError, SSHError) as exc:
        ui.die(str(exc))


def run_command(
    arguments: tuple[str, ...],
    *,
    name: str | None = None,
    detach: bool = False,
    timeout: float | None = None,
    stop_after: bool = False,
    force_stop_after: bool = False,
    setup_github: bool | None = None,
) -> int:
    """Run literal argv through the durable task streamer without reinterpreting its first token as a task ID."""
    if not arguments:
        ui.die(f"no remote command specified; use {ui.command('send -- COMMAND [ARG ...]')!r}")
    session, endpoint = _running_endpoint(name)
    return _run_command_task(session, endpoint, arguments, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after, setup_github=setup_github)


def dispatch(
    arguments: tuple[str, ...],
    *,
    name: str | None = None,
    timeout: float | None = None,
    detach: bool = False,
    stop: bool = False,
    immediate: bool = False,
    stop_after: bool = False,
    force_stop_after: bool = False,
    list_only: bool = False,
    include_all: bool = False,
    literal_command: bool = False,
    output_format: OutputFormat = OutputFormat.auto,
) -> int:
    """Interpret the unified send grammar and return the desired CLI exit status."""
    if stop_after and (stop or immediate):
        ui.die("--stop-after cannot be combined with --stop or --immediate")
    if force_stop_after and not stop_after and arguments[:1] != ("stopafter",):
        ui.die("--force is only valid with --stop-after or the 'stopafter' action")
    if list_only:
        if arguments or stop or immediate or detach or stop_after:
            ui.die("--ls cannot be combined with a task, command, --stop, --stop-after, --immediate, or --detach")
        if name is not None:
            name = launch_ops.resolve_session(name).name
        list_tasks(output_format=output_format, include_all=include_all, session_name=name)
        return 0

    if literal_command:
        if stop or immediate:
            ui.die("a literal command after '--' cannot be combined with --stop or --immediate")
        if not arguments:
            ui.die(f"no remote command specified after '--'; use {ui.command('send -- COMMAND [ARG ...]')!r}")
        session, endpoint = _running_endpoint(name)
        return _run_command_task(session, endpoint, arguments, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after)

    task_store = store()
    subject = arguments[0] if arguments else None
    exact = task_store.get(subject) if subject else None
    if exact is not None and name is None:
        session, endpoint = _running_endpoint(exact.session)
    else:
        session, endpoint = _running_endpoint(name)

    if subject == "cancel":
        if stop or immediate or stop_after or detach:
            ui.die(f"{ui.command('send cancel')!r} cannot be combined with --stop, --immediate, --stop-after, or --detach")
        return _cancel_tasks(session, endpoint, arguments[1:])

    if subject == "stopafter":
        if len(arguments) != 1 or stop or immediate or stop_after:
            ui.die(f"use {ui.command('send stopafter')!r} by itself to queue shutdown after all active work")
        dependencies = tuple(task.id for task in _active_tasks(session, endpoint, include_stop_after=False))
        _schedule_stop_after(session, endpoint, dependencies, force=force_stop_after)
        return 0

    if exact is not None:
        remainder = arguments[1:]
        if exact.session != session.name:
            ui.die(f"task {exact.id} belongs to session {exact.session!r}, not {session.name!r}")
        if immediate:
            if not remainder:
                ui.die("--immediate requires a replacement agent message")
            stop = True
        if stop:
            _stop_task(exact, endpoint)
            if not remainder:
                return 0
            if exact.kind != "agent" or exact.agent is None:
                ui.die(f"a command task cannot be replaced with a message; start another command with {ui.command('send -- COMMAND')!r}")
            agent = agents.AGENTS[exact.agent]
            message = " ".join(remainder)
            _prepare_github_auth(session, endpoint)
            agent.prepare_send(endpoint, session.flags)
            replacement = SendTask(
                id=new_task_id("agent"),
                session=session.name,
                kind="agent",
                agent=agent.name,
                command=list(agent.send_command(message, session.flags, tmux_session=session.tmux_session, remote_dir=session.remote_dir)),
                label=message,
            )
            return _start_task(session, endpoint, replacement, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after)
        if remainder:
            ui.die(f"task {exact.id} is an existing task; omit extra arguments to attach")
        if stop_after:
            dependencies = (exact.id,) if exact.active else ()
            _schedule_stop_after(session, endpoint, dependencies, force=force_stop_after)
        return follow(_refresh(exact, endpoint), endpoint, timeout=timeout)

    if subject in {"agent", *agents.AGENTS.keys()}:
        agent = _session_agent(session, subject)
        message = " ".join(arguments[1:]).strip()
        if immediate and not message:
            ui.die("--immediate requires a replacement agent message")
        active = _matching_agent_tasks(session.name, agent.name, endpoint)
        if immediate:
            stop = True
        if stop and active:
            if len(active) > 1:
                ids = ", ".join(task.id for task in active)
                ui.die(f"multiple {agent.name} tasks are active ({ids}); cancel one with {ui.command('send <id> --stop')!r}")
            _stop_task(active[0], endpoint)
            active = []
        elif stop:
            remote.tmux_interrupt(endpoint, session.tmux_session)
            ui.ok(f"Interrupted {agent.name.title()} in session {session.name!r}; the agent session is still running")
        if not message:
            if stop:
                return 0
            if len(active) != 1:
                detail = "none are active" if not active else f"choose one: {', '.join(task.id for task in active)}"
                ui.die(f"cannot attach by {agent.name} selector: {detail}")
            if stop_after:
                _schedule_stop_after(session, endpoint, (active[0].id,), force=force_stop_after)
            return follow(active[0], endpoint, timeout=timeout)
        try:
            _prepare_github_auth(session, endpoint)
            agent.prepare_send(endpoint, session.flags)
            command = agent.send_command(message, session.flags, tmux_session=session.tmux_session, remote_dir=session.remote_dir)
        except SSHError as exc:
            ui.die(str(exc))
        dependency = active[0].id if active else None
        task = SendTask(
            id=new_task_id("agent"),
            session=session.name,
            kind="agent",
            agent=agent.name,
            command=list(command),
            label=message,
            status="queued" if dependency else "running",
            depends_on=dependency,
        )
        return _start_task(session, endpoint, task, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after)

    if stop or immediate:
        if subject is None:
            active = [task for task in task_store.all() if task.session == session.name and _refresh(task, endpoint).active]
            if len(active) != 1:
                ui.die(f"--stop without a task id requires exactly one active task; found {len(active)} (run {ui.command('send --ls')!r})")
            _stop_task(active[0], endpoint)
            return 0
        ui.die(f"no send task named {subject!r}; run {ui.command('send --ls')!r} to see active task ids")
    if not arguments:
        ui.die(f"no remote command specified; use {ui.command('send -- COMMAND [ARG ...]')!r}, {ui.command('send agent MESSAGE')!r}, or {ui.command('send --ls')!r}")
    return _run_command_task(session, endpoint, arguments, detach=detach, timeout=timeout, stop_after=stop_after, force_stop_after=force_stop_after)
