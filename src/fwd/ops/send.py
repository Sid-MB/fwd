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

import typer

from fwd import agents, remote, remote_tasks, task_stream, ui
from fwd.backends.base import TargetStatus
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
    shown = tasks if include_all else [task for task in tasks if task.active]
    active_count = sum(task.active for task in tasks)
    ui.table(
        f"{ui.command('send')} tasks ({active_count} active)",
        ("id", "kind", "status", "session", "running", "command / message"),
        (
            (
                task.id,
                task.agent or task.kind,
                task.status,
                task.session,
                _age(task.created_at),
                task.label,
            )
            for task in shown
        ),
        output_format=output_format,
    )
    if shown:
        ui.info(f"attach: {ui.command('send <id>')}    cancel: {ui.command('send <id> --stop')}")


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


def _start_task(session: SessionState, endpoint: SSHEndpoint, task: SendTask, *, detach: bool, timeout: float | None) -> int:
    """Persist and start a task, then either return or follow it."""
    store().upsert(task)
    try:
        remote_tasks.start(endpoint, session.name, session.remote_dir, task)
    except Exception:
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


def _session_agent(session: SessionState, selector: str) -> agents.AgentSpec:
    """Resolve ``agent`` or an explicit agent name against the session's launched command."""
    launched = agents.resolve(launch_ops.initial_command_for(session))
    if launched is None:
        ui.die(f"session {session.name!r} is not running a registered coding agent; launch one with {ui.command('up codex')!r} or {ui.command('up claude')!r}")
    if selector != "agent" and selector != launched.name:
        ui.die(f"session {session.name!r} is running {launched.name}, not {selector}")
    if launched.send_command is None:
        ui.die(f"{launched.name} does not support messages through {ui.command('send')!r}")
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


def dispatch(
    arguments: tuple[str, ...],
    *,
    name: str | None = None,
    timeout: float | None = None,
    detach: bool = False,
    stop: bool = False,
    immediate: bool = False,
    list_only: bool = False,
    include_all: bool = False,
    output_format: OutputFormat = OutputFormat.auto,
) -> int:
    """Interpret the unified send grammar and return the desired CLI exit status."""
    if list_only:
        if arguments or stop or immediate or detach:
            ui.die("--ls cannot be combined with a task, command, --stop, --immediate, or --detach")
        list_tasks(output_format=output_format, include_all=include_all, session_name=name)
        return 0

    task_store = store()
    subject = arguments[0] if arguments else None
    exact = task_store.get(subject) if subject else None
    if exact is not None and name is None:
        session, endpoint = _running_endpoint(exact.session)
    else:
        session, endpoint = _running_endpoint(name)

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
            replacement = SendTask(
                id=new_task_id("agent"),
                session=session.name,
                kind="agent",
                agent=agent.name,
                command=list(agent.send_command(message, session.flags)),
                label=message,
            )
            return _start_task(session, endpoint, replacement, detach=detach, timeout=timeout)
        if remainder:
            ui.die(f"task {exact.id} is an existing task; omit extra arguments to attach")
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
            return follow(active[0], endpoint, timeout=timeout)
        command = agent.send_command(message, session.flags)
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
        return _start_task(session, endpoint, task, detach=detach, timeout=timeout)

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
    task = SendTask(
        id=new_task_id("command"),
        session=session.name,
        kind="command",
        command=list(arguments),
        label=shlex.join(arguments),
    )
    try:
        return _start_task(session, endpoint, task, detach=detach, timeout=timeout)
    except SSHError as exc:
        ui.die(str(exc))
    except Exception as exc:
        ui.die(f"could not start task on session {session.name!r}: {exc}")
