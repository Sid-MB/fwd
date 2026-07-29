"""Open, list, and close local port forwarding for existing fwd sessions."""

from __future__ import annotations

from pathlib import Path

import typer

from fwd import port_forwarding, ui
from fwd.backends.base import TargetStatus
from fwd.config import load_config
from fwd.ops import launch as launch_ops
from fwd.ops import session_select
from fwd.state import SessionState, endpoint_to_dict


def _session_and_specs(arguments: tuple[str, ...], name: str | None) -> tuple[SessionState, tuple[str, ...]]:
    """Resolve the optional single selector followed by a contiguous mapping suffix."""
    if arguments and not port_forwarding.mapping_argument(arguments[0]):
        selectors = arguments[:1]
        specs = arguments[1:]
    else:
        selectors = ()
        specs = arguments
    selection = session_select.select_current(selectors, name=name, state=launch_ops.store())
    if not selection.matches:
        ui.die(f"no session matches {selection.selector.describe()}; inspect available sessions with {ui.command('ls --all-projects')!r}")
    return launch_ops.choose_session(selection.matches, selection.selector.describe()), specs


def _running_endpoint(session: SessionState):
    """Resolve the current endpoint without provisioning or restarting stopped compute."""
    config = load_config(Path(session.local_cwd))
    backend = launch_ops.backend_for(session, config=config)
    status = launch_ops.status_of(backend, session)
    if status is not TargetStatus.RUNNING:
        remedies = {
            TargetStatus.STOPPED: f"restart it explicitly with {ui.command(f'attach {session.name} --restart')!r}",
            TargetStatus.PENDING: "wait for it to become running",
            TargetStatus.GONE: "the remote resource no longer exists",
            TargetStatus.JOB_ENDED: f"start a new allocation with {ui.command(f'attach {session.name}')!r}",
            TargetStatus.UNKNOWN: f"run {ui.command('doctor')!r} and retry when status is available",
        }
        ui.die(f"cannot expose ports for session {session.name!r}: target status is {status}; {remedies.get(status, 'the target must be running')}")
    endpoint = backend.endpoint(session)
    launch_ops.store().update(session.name, endpoint=endpoint_to_dict(endpoint))
    return endpoint


def _open_session_mappings(
    session: SessionState,
    requested: tuple[port_forwarding.PortMapping, ...],
    endpoint,
    *,
    accept_existing: bool,
) -> tuple[port_forwarding.PortMapping, ...]:
    """Open or reconcile mappings for a resolved running session and return mappings activated by this call."""
    existing = port_forwarding.mappings_from_state(session.ports)
    existing_by_local = {mapping.local: mapping for mapping in existing}
    additions: list[port_forwarding.PortMapping] = []
    conflicts: list[str] = []
    repeated: list[int] = []
    for mapping in requested:
        current = existing_by_local.get(mapping.local)
        if current is None:
            additions.append(mapping)
        elif accept_existing and current == mapping:
            continue
        elif current.remote != mapping.remote:
            conflicts.append(f"{mapping.local} already maps to remote {current.remote}, not {mapping.remote}")
        else:
            repeated.append(mapping.local)
    if conflicts:
        ui.die(f"session {session.name!r} has conflicting forwarding: {'; '.join(conflicts)}; close the local port before changing its remote destination")
    if repeated:
        ui.die(f"session {session.name!r} already tracks local port(s): {', '.join(map(str, sorted(repeated)))}; close them before reopening")
    master_endpoint = session.ports_ssh_endpoint()
    master_active = port_forwarding.active(master_endpoint, session.name)
    if master_active and (not session.ports_endpoint or endpoint != master_endpoint):
        try:
            port_forwarding.close(master_endpoint, session.name)
        except port_forwarding.PortForwardError as exc:
            ui.die(f"the forwarding master still targets the previous endpoint and could not be closed: {exc}")
        master_active = False
    combined = (*existing, *additions)
    if master_active and not additions:
        return ()
    if not combined:
        return ()
    if not master_active:
        unavailable = port_forwarding.unavailable_local_ports(combined)
    else:
        unavailable = port_forwarding.unavailable_local_ports(tuple(additions))
    if unavailable:
        noun = "port is" if len(unavailable) == 1 else "ports are"
        ui.die(f"local {noun} already in use: {', '.join(map(str, unavailable))}; no port forwarding was opened")
    try:
        port_forwarding.open_forwards(endpoint, session.name, tuple(additions) if master_active else combined, master_active=master_active)
    except port_forwarding.PortForwardError as exc:
        if master_active:
            port_forwarding.cancel_forwards(endpoint, session.name, tuple(additions), check=False)
        else:
            port_forwarding.close(endpoint, session.name)
        ui.die(str(exc))
    try:
        updated = launch_ops.store().update(session.name, ports=[mapping.to_dict() for mapping in combined], ports_endpoint=endpoint_to_dict(endpoint))
        if updated is None:
            raise RuntimeError(f"session {session.name!r} disappeared from local state")
    except Exception:
        if master_active:
            port_forwarding.cancel_forwards(endpoint, session.name, tuple(additions), check=False)
        else:
            port_forwarding.close(endpoint, session.name)
        raise
    return tuple(additions) if master_active else combined


def ensure_session_ports(session: SessionState, specs: tuple[str, ...], *, endpoint=None) -> None:
    """Ensure launch-requested or configured mappings exist, preserving unrelated forwards and accepting exact matches."""
    if not specs:
        return
    try:
        requested = port_forwarding.parse_mappings(specs)
    except port_forwarding.PortForwardError as exc:
        ui.die(str(exc))
    resolved_endpoint = endpoint or _running_endpoint(session)
    activated = _open_session_mappings(session, requested, resolved_endpoint, accept_existing=True)
    for mapping in activated:
        ui.ok(f"forwarding 127.0.0.1:{mapping.local} to {session.name}:127.0.0.1:{mapping.remote}")


def preflight_launch_ports(session: SessionState | None, specs: tuple[str, ...]) -> None:
    """Validate launch mappings and reject free-port conflicts before provisioning or restarting remote compute."""
    if not specs:
        return
    try:
        requested = port_forwarding.parse_mappings(specs)
    except port_forwarding.PortForwardError as exc:
        ui.die(str(exc))
    existing_by_local = {mapping.local: mapping for mapping in port_forwarding.mappings_from_state(session.ports)} if session is not None else {}
    existing_master_active = session is not None and port_forwarding.active(session.ports_ssh_endpoint(), session.name)
    additions: list[port_forwarding.PortMapping] = []
    conflicts: list[str] = []
    for mapping in requested:
        current = existing_by_local.get(mapping.local)
        if current is None:
            additions.append(mapping)
        elif current == mapping and not existing_master_active:
            additions.append(mapping)
        elif current.remote != mapping.remote:
            conflicts.append(f"{mapping.local} already maps to remote {current.remote}, not {mapping.remote}")
    if conflicts:
        ui.die(f"configured forwarding conflicts with session {session.name!r}: {'; '.join(conflicts)}")
    unavailable = port_forwarding.unavailable_local_ports(tuple(additions))
    if unavailable:
        noun = "port is" if len(unavailable) == 1 else "ports are"
        ui.die(f"local {noun} already in use: {', '.join(map(str, unavailable))}; launch was canceled before provisioning")


def open_ports(arguments: tuple[str, ...], *, name: str | None = None) -> None:
    """Open loopback-only background forwards after an all-or-nothing local bind preflight."""
    session, specs = _session_and_specs(arguments, name)
    if not specs:
        ui.die(f"no ports specified; use {ui.command('ports PORT [PORT ...]')!r}")
    try:
        requested = port_forwarding.parse_mappings(specs)
    except port_forwarding.PortForwardError as exc:
        ui.die(str(exc))
    existing_locals = {mapping.local for mapping in port_forwarding.mappings_from_state(session.ports)}
    repeated = sorted(mapping.local for mapping in requested if mapping.local in existing_locals)
    if repeated:
        ui.die(f"session {session.name!r} already tracks local port(s): {', '.join(map(str, repeated))}; close them before reopening")
    unavailable = port_forwarding.unavailable_local_ports(requested)
    if unavailable:
        noun = "port is" if len(unavailable) == 1 else "ports are"
        ui.die(f"local {noun} already in use: {', '.join(map(str, unavailable))}; no port forwarding was opened")
    endpoint = _running_endpoint(session)
    _open_session_mappings(session, requested, endpoint, accept_existing=False)

    for mapping in requested:
        ui.ok(f"forwarding 127.0.0.1:{mapping.local} to {session.name}:127.0.0.1:{mapping.remote}")


def close_ports(arguments: tuple[str, ...], *, name: str | None = None) -> None:
    """Close selected local ports, or every mapping when no ports are specified."""
    session, specs = _session_and_specs(arguments, name)
    try:
        requested = port_forwarding.parse_mappings(specs)
    except port_forwarding.PortForwardError as exc:
        ui.die(str(exc))
    endpoint = session.ports_ssh_endpoint()
    existing = port_forwarding.mappings_from_state(session.ports)
    if not requested:
        try:
            port_forwarding.close(endpoint, session.name)
        except port_forwarding.PortForwardError as exc:
            ui.die(str(exc))
        remaining: tuple[port_forwarding.PortMapping, ...] = ()
        closed = existing
    else:
        requested_locals = {mapping.local for mapping in requested}
        closed = tuple(mapping for mapping in existing if mapping.local in requested_locals)
        missing = sorted(requested_locals - {mapping.local for mapping in closed})
        if missing:
            ui.die(f"session {session.name!r} has no forwarded local port(s): {', '.join(map(str, missing))}")
        remaining = tuple(mapping for mapping in existing if mapping.local not in requested_locals)
        master_active = port_forwarding.active(endpoint, session.name)
        try:
            if master_active and remaining:
                port_forwarding.cancel_forwards(endpoint, session.name, closed)
            elif master_active:
                port_forwarding.close(endpoint, session.name)
        except port_forwarding.PortForwardError as exc:
            ui.die(str(exc))
    launch_ops.store().update(session.name, ports=[mapping.to_dict() for mapping in remaining], ports_endpoint=dict(session.ports_endpoint) if remaining else {})
    rendered = ", ".join(str(mapping.local) for mapping in closed) or "all ports"
    ui.ok(f"closed local port forwarding for session {session.name!r}: {rendered}")


def close_all_projects() -> None:
    """Close every tracked session's local forwarding without contacting or stopping remote compute."""
    sessions = launch_ops.store().all()
    forwarded = [session for session in sessions if session.ports]
    failures: list[str] = []
    for session in sessions:
        try:
            close_session_ports(session)
        except port_forwarding.PortForwardError as exc:
            failures.append(f"{session.name}: {exc}")
    if failures:
        ui.die(f"could not close forwarding for {len(failures)} session(s); their mappings remain tracked: {'; '.join(failures)}")
    ui.ok(f"closed local port forwarding across all projects ({len(forwarded)} session(s))")


def list_ports(
    arguments: tuple[str, ...],
    *,
    name: str | None = None,
    all_projects: bool = False,
    output_format="auto",
) -> None:
    """Render the ports-focused session table globally, for one selector, or for the current project."""
    from fwd.ops import lifecycle

    if all_projects:
        if arguments or name is not None:
            ui.die("--all-projects cannot be combined with a session selector")
        lifecycle.ls(all_projects=True, columns=("ports",), output_format=output_format)
        return
    if arguments or name is not None:
        session, specs = _session_and_specs(arguments, name)
        if specs:
            ui.die("port mappings cannot be combined with --ls")
        lifecycle.ls(all_projects=True, columns=("ports",), session_names=(session.name,), output_format=output_format)
        return
    lifecycle.ls(columns=("ports",), output_format=output_format)


def close_session_ports(session: SessionState) -> None:
    """Best-effort lifecycle cleanup used by stop and remove."""
    port_forwarding.close(session.ports_ssh_endpoint(), session.name)
    launch_ops.store().update(session.name, ports=[], ports_endpoint={})
