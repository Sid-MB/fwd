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

from fwd import github_auth, sync, ui
from fwd.config import load_config
from fwd.ops import launch as launch_ops
from fwd.sshexec import SSHEndpoint, SSHError
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
    with ui.transfer_step(f"Pushing {local_cwd.name} to {session.remote_dir}") as transfer:
        if endpoint.supports_rsync:
            sync.sync_up(
                endpoint,
                local_cwd,
                session.remote_dir,
                cfg.sync,
                delete=cfg.sync.delete,
                on_progress=transfer,
                on_path=transfer.path,
            )
        else:
            ui.warn("transport does not support rsync; using tar-over-ssh (whole-tree transfer)")
            sync.tar_up(
                endpoint,
                local_cwd,
                session.remote_dir,
                cfg.sync,
                delete=cfg.sync.delete,
                on_progress=transfer,
                on_path=transfer.path,
            )
    session.flags["github_auth_ready"] = False
    tool_prefix = session.flags.get("tool_prefix")
    if cfg.github.auth and isinstance(tool_prefix, str) and tool_prefix:
        try:
            if github_auth.ensure_remote(endpoint, local_cwd, session.remote_dir, tool_prefix):
                session.flags["github_auth_ready"] = True
        except (github_auth.GitHubAuthError, SSHError) as exc:
            ui.warn(f"project push succeeded, but GitHub authentication could not be restored ({exc})")
    launch_ops.store().update(session.name, flags=session.flags)
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
    with ui.transfer_step(f"Pulling {what} from {session.remote_dir}", show_bytes=False) as transfer:
        if endpoint.supports_rsync:
            sync.sync_down(endpoint, session.remote_dir, local_cwd, paths, cfg.sync, on_path=transfer.path)
        else:
            ui.warn("transport does not support rsync; using tar-over-ssh (whole-tree transfer)")
            sync.tar_down(endpoint, session.remote_dir, local_cwd, paths, cfg.sync, on_path=transfer.path)
    ui.ok(f"pulled from {session.name!r} into {local_cwd}")
    _suggest_continuous(session, cfg, endpoint)


def _suggest_continuous(session: SessionState, cfg, endpoint: SSHEndpoint) -> None:
    """Mention continuous sync once a day after a manual pull, when it would actually work here.

    A manual pull is the moment the feature is most obviously relevant — the user just did by hand what continuous
    mode would have done for them. Every condition below exists to make sure the hint is never noise: it is silent
    when continuous sync is already on, when the transport cannot support it, and, through :mod:`fwd.tips`, when it was
    shown recently. It is deliberately not silent merely because Mutagen is missing: not having installed it yet is the
    normal state for someone who has never heard of the feature, and :func:`fwd.mutagen_sync.ensure_installed` offers
    to install it at the point they opt in.
    """
    from fwd import mutagen_sync, tips

    target = session.flags.get("target")
    if cfg.continuous_sync_for(target if isinstance(target, str) else None):
        return
    if not mutagen_sync.supports_continuous(endpoint) or not tips.should_show(tips.CONTINUOUS_SYNC):
        return
    ui.info_with_code("tip: keep this project continuously in sync with ", ui.command("sync on"), f" (uses Mutagen; see {ui.command('sync --help')})")
    tips.mark_shown(tips.CONTINUOUS_SYNC)
