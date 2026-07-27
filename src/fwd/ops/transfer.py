"""Manual file transfer for an existing session — ``fwd push`` / ``fwd pull``.

Design intent
-------------
Asymmetric on purpose. Push mirrors local over remote (honouring ``--delete``), matching launch semantics: local is
the source of truth for code. Pull is additive and path-scoped, because the remote is where *new* artifacts appear —
training outputs, generated files, edits made inside the remote Claude session — and a mirroring pull could delete
local work the user has not pushed yet. Losing an hour of remote GPU output is annoying; deleting uncommitted local
work is unforgivable.

Both commands deliberately reuse launch's filter configuration rather than defining their own, so a file excluded at
launch cannot silently appear on a later push.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import typer

from fwd import sync, ui
from fwd.config import load_config
from fwd.ops import launch as launch_ops
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState


def _endpoint_for(session: SessionState) -> SSHEndpoint:
    """Re-resolve a session's endpoint through its backend, falling back to the cached one.

    Transfers hit the same IP-churn problem as attach, so they must re-resolve too. The fallback keeps push/pull
    working while a backend's ``endpoint()`` is still unimplemented.
    """
    try:
        backend = launch_ops.backend_for(session)
        return backend.endpoint(session)
    except NotImplementedError:
        return session.ssh_endpoint()
    except typer.Exit:
        raise
    except Exception as exc:
        ui.warn(f"could not re-resolve the endpoint ({exc}); using the address from the last launch")
        return session.ssh_endpoint()


def push(name: str | None = None) -> None:
    """Re-sync the local working directory up to an existing session's remote directory.

    Args:
        name: Session name, target label, or backend name; ``None`` uses the session for the current directory.
    """
    session = launch_ops.resolve_session(name)
    local_cwd = Path(session.local_cwd).expanduser()
    if not local_cwd.is_dir():
        ui.die(f"the local directory for session {session.name!r} no longer exists: {local_cwd}")

    cfg = load_config(local_cwd)
    endpoint = _endpoint_for(session)
    with ui.step(f"Pushing {local_cwd.name} to {session.remote_dir}"):
        if endpoint.supports_rsync:
            sync.sync_up(endpoint, local_cwd, session.remote_dir, cfg.sync, delete=cfg.sync.delete)
        else:
            ui.warn("transport does not support rsync; using tar-over-ssh (whole-tree transfer)")
            sync.tar_up(endpoint, local_cwd, session.remote_dir, cfg.sync)
    ui.ok(f"pushed to {session.name!r}")


def pull(name: str | None = None, paths: Sequence[str] = ()) -> None:
    """Bring remote changes back down.

    Args:
        name: Session name, target label, or backend name; ``None`` uses the session for the current directory.
        paths: Specific remote-relative paths to fetch; empty pulls the whole tree minus excludes.
    """
    session = launch_ops.resolve_session(name)
    local_cwd = Path(session.local_cwd).expanduser()
    if not local_cwd.is_dir():
        ui.die(f"the local directory for session {session.name!r} no longer exists: {local_cwd}")

    cfg = load_config(local_cwd)
    endpoint = _endpoint_for(session)
    paths = tuple(paths)
    what = ", ".join(paths) if paths else "everything"
    with ui.step(f"Pulling {what} from {session.remote_dir}"):
        if endpoint.supports_rsync:
            sync.sync_down(endpoint, session.remote_dir, local_cwd, paths, cfg.sync)
        else:
            ui.warn("transport does not support rsync; using tar-over-ssh (whole-tree transfer)")
            sync.tar_down(endpoint, session.remote_dir, local_cwd, paths)
    ui.ok(f"pulled from {session.name!r} into {local_cwd}")
