"""Read-only local/remote content comparison with Git-style output and standard diff exit semantics.

Both sides are materialized into temporary snapshots. The default domain is the content synchronized by ``fwd push``
except repository metadata: `.git`, OS metadata, configured exclusions, `.fwdignore`, and Git-ignored files do not
make a synchronized project appear dirty. ``--include-gitignored`` restores only Git-ignored content, while
``--include-unsynced`` restores every ordinary exclusion. A small permanent exclusion set remains in both modes so
repository databases and platform junk can never dominate or leak through a diagnostic comparison.

Exit codes intentionally match POSIX diff: 0 means identical, 1 means content differs, and 2 means the comparison or
transfer failed. Git supplies familiar terminal colors and unified formatting interactively; redirected output is
plain text. Progress and diagnostics remain on stderr.
"""

from __future__ import annotations

from dataclasses import replace
import os
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from fwd import selection, sync, ui
from fwd.config import ALWAYS_PULL_EXCLUDES, Config, SyncConfig, load_config
from fwd.ops import launch as launch_ops
from fwd.ops.transfer import _endpoint_for
from fwd.output import is_machine_environment
from fwd.state import SessionState

# Diff and pull share the permanent project-content boundary. Push differs only by including `.git/` for remote agents.
ALWAYS_DIFF_EXCLUDES: tuple[str, ...] = ALWAYS_PULL_EXCLUDES


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


def _comparison_config(config: Config, *, include_gitignored: bool, include_unsynced: bool) -> SyncConfig:
    """Derive the ordinary comparison filters without mutating the project's upload configuration."""
    if include_unsynced:
        return replace(config.sync, exclude=[], use_gitignore=False)
    if include_gitignored:
        return replace(config.sync, use_gitignore=False)
    # Diff is a diagnostic surface that may print file contents, so its safe default honours Git ignore rules even
    # when a project deliberately disabled them for upload. The explicit include flag is the disclosure boundary.
    return replace(config.sync, use_gitignore=True)


def _snapshot_local(
    source: Path,
    destination: Path,
    sync_config: SyncConfig,
    *,
    manifest: Path | None,
    include_fwdignore: bool,
) -> None:
    """Copy the selected local comparison domain while enforcing permanent exclusions in every mode."""
    destination.mkdir(parents=True, exist_ok=True)
    filters = [f"--exclude={pattern}" for pattern in ALWAYS_DIFF_EXCLUDES]
    filters.extend(selection.rsync_manifest_args(manifest) if manifest is not None else sync.rsync_filters(sync_config, source, include_fwdignore=include_fwdignore))
    filters = list(dict.fromkeys(filters))
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


def _remote_git_candidates(endpoint, remote_dir: str) -> list[bytes] | None:
    """Return remote tracked and nonignored file names without reading their contents or repository metadata."""
    command = f"git -C {shlex.quote(remote_dir.rstrip('/'))} ls-files --cached --others --exclude-standard -z"
    process = endpoint.run(command, check=False)
    if process.returncode != 0:
        ui.warn("could not enumerate the remote Git selection; remote-only paths may be omitted from this comparison")
        return None
    candidates: list[bytes] = []
    for value in (process.stdout or "").split("\0"):
        posix = PurePosixPath(value)
        if not value or posix.is_absolute() or any(part == ".." for part in posix.parts):
            continue
        candidates.append(os.fsencode(str(posix)))
    return candidates


def _remote_manifest(remote_candidates: list[bytes], destination: Path, local_cwd: Path, sync_config: SyncConfig) -> Path:
    """Write a remote selection filtered by local rules so one-sided files remain visible without secret disclosure."""
    selected_remote = selection.filtered_candidates(
        local_cwd,
        remote_candidates,
        sync_config,
        apply_gitignore=True,
        extra_excludes=ALWAYS_DIFF_EXCLUDES,
    )
    with destination.open("wb") as stream:
        for path in selected_remote:
            stream.write(path + b"\0")
    return destination


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
    """Emit Git's unified no-index diff for the requested subtree and return 0, 1, or 2."""
    local = local_root / relative if relative is not None else local_root
    remote = remote_root / relative if relative is not None else remote_root
    if not local.exists() and not local.is_symlink() and not remote.exists() and not remote.is_symlink():
        return 0
    _placeholder(local, remote)
    _placeholder(remote, local)
    color = "always" if ui.console.is_terminal and ui.console.color_system is not None and not ui.console.no_color and not is_machine_environment() else "never"
    process = subprocess.run(
        ["git", "--no-pager", "diff", "--no-index", "--no-ext-diff", f"--color={color}", "--", str(local), str(remote)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = process.stdout.replace(str(local_root), "local").replace(str(remote_root), "remote")
    if output and not quiet:
        ui.raw(output)
    if process.stderr:
        ui.error(process.stderr.strip())
    return process.returncode if process.returncode in (0, 1) else max(2, process.returncode)


def diff(
    target: str | None = None,
    path: str | None = None,
    *,
    quiet: bool = False,
    include_gitignored: bool = False,
    include_unsynced: bool = False,
) -> int:
    """Compare local and remote project content without changing either side.

    Args:
        target: Exact session name, configured target label, or backend name; ``None`` uses this directory's session.
        path: Optional project-relative file or directory; ``None`` compares the entire project.
        quiet: Suppress unified diff text while preserving the exit status.
        include_gitignored: Compare Git-ignored content while retaining configured and `.fwdignore` exclusions.
        include_unsynced: Compare every ordinary unsynced path; permanent metadata exclusions still apply.
    """
    session = resolve_session(target)
    local_cwd = Path(session.local_cwd).expanduser()
    if not local_cwd.is_dir():
        ui.die(f"the local directory for session {session.name!r} no longer exists: {local_cwd}", code=2)
    relative = _relative_path(path)
    config = load_config(local_cwd)
    comparison_config = _comparison_config(config, include_gitignored=include_gitignored, include_unsynced=include_unsynced)
    include_fwdignore = not include_unsynced
    endpoint = _endpoint_for(session)
    with tempfile.TemporaryDirectory(prefix="fwd-diff-") as temporary:
        root = Path(temporary)
        local_snapshot = root / "local"
        remote_snapshot = root / "remote"
        with selection.upload_manifest(
            local_cwd,
            comparison_config,
            include_git_metadata=False,
            include_ignored_rule_files=False,
            exclude_ignored_tracked=True,
            extra_excludes=ALWAYS_DIFF_EXCLUDES,
        ) as manifest:
            with ui.step(f"Reading local and remote content for {session.name!r}"):
                comparison_manifest = manifest
                if manifest is not None:
                    remote_candidates = _remote_git_candidates(endpoint, session.remote_dir)
                    if remote_candidates is not None:
                        comparison_manifest = _remote_manifest(remote_candidates, root / "remote-manifest", local_cwd, comparison_config)
                _snapshot_local(
                    local_cwd,
                    local_snapshot,
                    comparison_config,
                    manifest=manifest,
                    include_fwdignore=include_fwdignore,
                )
                if not endpoint.supports_rsync:
                    ui.warn("transport does not support rsync; using tar-over-ssh for the remote snapshot")
                sync.sync_down(
                    endpoint,
                    session.remote_dir,
                    remote_snapshot,
                    (),
                    comparison_config,
                    filter_dir=local_cwd,
                    manifest=comparison_manifest,
                    extra_excludes=ALWAYS_DIFF_EXCLUDES,
                    include_fwdignore=include_fwdignore,
                )
        return _compare(local_snapshot, remote_snapshot, relative, quiet=quiet)
