"""Read-only local/remote content comparison with standard ``diff`` exit semantics.

Both sides are materialized into temporary snapshots using the same filters as ``fwd push``. Comparing snapshots
rather than the live local tree prevents generated, intentionally unsynced paths such as ``.venv`` and
``node_modules`` from making a synchronized project appear dirty. The remote snapshot is never merged into the local
checkout.

Exit codes intentionally match POSIX diff: 0 means identical, 1 means content differs, and 2 means the comparison or
transfer failed. Unified diff text goes to stdout; progress and diagnostics remain on stderr.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from fwd import selection, sync, ui
from fwd.config import Config, load_config
from fwd.ops import launch as launch_ops
from fwd.ops.transfer import _endpoint_for
from fwd.state import SessionState


def resolve_session(selector: str | None) -> SessionState:
    """Resolve through the shared exact-session, target-label, backend, or current-directory rules."""
    return launch_ops.resolve_session(selector)


def _relative_path(value: str | None) -> Path | None:
    """Validate a remote-project-relative path without consulting either filesystem."""
    if value in (None, "", "."):
        return None
    posix = PurePosixPath(value)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        ui.die(f"diff path must stay inside the project: {value!r}", code=2)
    cleaned = Path(*[part for part in posix.parts if part not in ("", ".")])
    return cleaned if cleaned.parts else None


def _snapshot_local(source: Path, destination: Path, config: Config) -> None:
    """Copy the local sync domain into ``destination`` using Git's authoritative selection when available."""
    destination.mkdir(parents=True, exist_ok=True)
    with selection.upload_manifest(source, config.sync) as manifest:
        filters = selection.rsync_manifest_args(manifest) if manifest is not None else sync.rsync_filters(config.sync, source)
        argv = [
            *sync.RSYNC_BASE,
            *filters,
            f"{str(source).rstrip('/')}/",
            f"{destination}/",
        ]
        process = subprocess.run(argv, capture_output=True, text=True, check=False)
    if process.returncode not in ({0} | set(sync.RSYNC_PARTIAL_EXITS)):
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"local snapshot failed (rsync exit {process.returncode}): {detail}")
    if process.returncode in sync.RSYNC_PARTIAL_EXITS:
        ui.warn(f"local snapshot completed with warnings (rsync exit {process.returncode})")


def _placeholder(path: Path, other: Path) -> None:
    """Create an empty counterpart so ``diff -N`` can represent one-sided files and directories."""
    if path.exists() or path.is_symlink():
        return
    if other.is_dir():
        path.mkdir(parents=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def _compare(local_root: Path, remote_root: Path, relative: Path | None, *, quiet: bool = False) -> int:
    """Emit a unified recursive diff for the requested subtree and return 0, 1, or 2."""
    local = local_root / relative if relative is not None else local_root
    remote = remote_root / relative if relative is not None else remote_root
    if not local.exists() and not local.is_symlink() and not remote.exists() and not remote.is_symlink():
        return 0
    _placeholder(local, remote)
    _placeholder(remote, local)
    process = subprocess.run(["diff", "-ruN", str(local), str(remote)], capture_output=True, text=True, check=False)
    output = process.stdout.replace(str(local_root), "local").replace(str(remote_root), "remote")
    if output and not quiet:
        ui.raw(output)
    if process.stderr:
        ui.error(process.stderr.strip())
    return process.returncode if process.returncode in (0, 1) else max(2, process.returncode)


def diff(target: str | None = None, path: str | None = None, *, quiet: bool = False) -> int:
    """Compare local and remote sync-domain content without changing either side.

    Args:
        target: Exact session name, configured target label, or backend name; ``None`` uses this directory's session.
        path: Optional project-relative file or directory; ``None`` compares the entire project.
        quiet: Suppress unified diff text while preserving the exit status.
    """
    session = resolve_session(target)
    local_cwd = Path(session.local_cwd).expanduser()
    if not local_cwd.is_dir():
        ui.die(f"the local directory for session {session.name!r} no longer exists: {local_cwd}", code=2)
    relative = _relative_path(path)
    config = load_config(local_cwd)
    endpoint = _endpoint_for(session)
    with tempfile.TemporaryDirectory(prefix="fwd-diff-") as temporary:
        root = Path(temporary)
        local_snapshot = root / "local"
        remote_snapshot = root / "remote"
        with ui.step(f"Reading local and remote content for {session.name!r}"):
            _snapshot_local(local_cwd, local_snapshot, config)
            if endpoint.supports_rsync:
                sync.sync_down(endpoint, session.remote_dir, remote_snapshot, (), config.sync, filter_dir=local_cwd)
            else:
                ui.warn("transport does not support rsync; using tar-over-ssh for the remote snapshot")
                sync.tar_down(endpoint, session.remote_dir, remote_snapshot, sync_cfg=config.sync)
        return _compare(local_snapshot, remote_snapshot, relative, quiet=quiet)
