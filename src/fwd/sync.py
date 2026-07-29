"""File transfer — rsync, with a tar-over-ssh fallback.

Design intent (owned by the core/sync teammate)
-----------------------------------------------
One-shot transfers only; no continuous watching in the MVP. Push is a mirror (``--delete``) so the remote tree is a
faithful copy of local, while pull is additive and path-scoped so a careless pull cannot delete local work.

Git working trees use Git's own tracked/untracked enumeration as the ignore authority, then layer an optional
``.fwdignore`` and ``SyncConfig.exclude`` over that manifest. Non-Git directories retain rsync/tar filter fallbacks.
Push includes ``.git`` because the remote session needs history to diff, blame and commit; pull never imports remote
repository metadata into the local checkout. Platform metadata is excluded permanently in both directions.

``tar_up``/``tar_down`` exist because RunPod's proxy transport cannot run a remote rsync binary. Uploads use a
byte-bounded stream into a remote stage before changing the live project; callers warn when the no-delta tar fallback
is engaged.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from fwd import config as config_mod
from fwd import selection
from fwd import ui
from fwd.config import ALWAYS_PULL_EXCLUDES, ALWAYS_SYNC_EXCLUDES, SyncConfig
from fwd.rsync_transport import PROGRESS_PREFIX
from fwd.sshexec import SSHEndpoint, SSHError

# Public compatibility alias; selection owns the filename because it now builds the authoritative upload manifest.
FWDIGNORE_NAME = selection.FWDIGNORE_NAME

# -a preserves modes/symlinks/times, -z compresses on the wire. No -v: progress is fwd's ui.step, not rsync spam.
#
# --no-owner/--no-group cancel the -o/-g that -a implies, and must come *after* -a because rsync applies options left
# to right. Owner preservation is meaningless for us — the remote is a single-user container — and it is actively
# fatal on RunPod, whose /workspace is a MooseFS network volume that refuses chown to a foreign uid. Local files are
# uid 501, so without these flags every single file logs "chown failed: Operation not permitted" and rsync exits 23.
RSYNC_BASE: tuple[str, ...] = ("rsync", "-az", "--no-owner", "--no-group")

# rsync exit codes that mean "the transfer happened, but some files were skipped": 23 = partial transfer due to error
# (a permission fixup, an unreadable file), 24 = source files vanished mid-run (normal when a build is running).
# Treating these as fatal aborts a launch after the pod has already been provisioned, for files that did transfer.
RSYNC_PARTIAL_EXITS: frozenset[int] = frozenset({23, 24})

# macOS bsdtar stores extended attributes as AppleDouble "._name" sidecar files, which arrive as visible junk in every
# directory of a Linux remote. COPYFILE_DISABLE suppresses them; GNU tar ignores the variable.
TAR_ENV: dict[str, str] = {"COPYFILE_DISABLE": "1"}
BYTES_PER_GB = 1_000_000_000
_STREAM_CHUNK_SIZE = 1024 * 1024
_LARGE_UPLOAD_ENTRY_BYTES = 200_000_000
_MAX_LARGE_UPLOAD_ENTRIES = 10
_RSYNC_ENTRY_PREFIX = "__FWD_UPLOAD_ENTRY__"
TransferProgress = Callable[[int], None]
TransferPath = Callable[[str], None]


class _UploadLimitExceeded(RuntimeError):
    """Internal signal raised only after an upload stream crosses its configured byte budget."""

    def __init__(self, observed_bytes: int) -> None:
        super().__init__(observed_bytes)
        self.observed_bytes = observed_bytes


@dataclass(frozen=True, slots=True)
class _LargeUploadEntry:
    """One filtered file or aggregate folder large enough to explain an upload-limit rejection."""

    path: str
    size_bytes: int
    kind: Literal["file", "folder"]


def _configured_filters(sync_cfg: SyncConfig, local_dir: str | Path, *, include_fwdignore: bool = True) -> list[str]:
    """Return project-configured excludes and the optional ``.fwdignore`` argument."""
    root = Path(local_dir).expanduser()
    args = [f"--exclude={pattern}" for pattern in sync_cfg.exclude]
    fwdignore = root / FWDIGNORE_NAME
    if include_fwdignore and fwdignore.is_file():
        args.append(f"--exclude-from={fwdignore}")
    return args


def _portable_filters(sync_cfg: SyncConfig, local_dir: str | Path, *, include_fwdignore: bool = True) -> list[str]:
    """Return exclusions shared by rsync and tar fallback; tar cannot reproduce per-directory ``.gitignore`` rules."""
    return [*[f"--exclude={pattern}" for pattern in ALWAYS_SYNC_EXCLUDES], *_configured_filters(sync_cfg, local_dir, include_fwdignore=include_fwdignore)]


def rsync_filters(sync_cfg: SyncConfig, local_dir: str | Path, *, include_fwdignore: bool = True) -> list[str]:
    """Build the rsync filter/exclude argv fragment for a project directory.

    Permanent platform exclusions come first so neither a negated Git rule nor ``.fwdignore`` can re-include them.
    Repository rules then precede configurable exclusions, preserving the established project-filter behavior.

    ``SyncConfig.exclude`` is used verbatim rather than unioned with ``DEFAULT_EXCLUDES``: ``config.load_config``
    already seeds it with the defaults, and unioning at use time would make it impossible for a project to *shrink*
    the configurable list. ``ALWAYS_SYNC_EXCLUDES`` is a separate invariant and therefore cannot be shrunk.

    Args:
        sync_cfg: Exclude patterns and whether to honour ``.gitignore``.
        local_dir: Project root, inspected for ``.gitignore``/``.fwdignore``.

    Returns:
        rsync arguments, e.g. ``["--exclude=.DS_Store", "--filter=:- .gitignore", "--exclude=.venv", ...]``.
    """
    root = Path(local_dir).expanduser()
    args = [f"--exclude={pattern}" for pattern in ALWAYS_SYNC_EXCLUDES]
    if sync_cfg.use_gitignore:
        # ':-' is a per-directory merge: every .gitignore in the tree applies to its own subtree, matching git.
        args.append("--filter=:- .gitignore")
    args += _configured_filters(sync_cfg, root, include_fwdignore=include_fwdignore)
    return args


def _display_size(size_bytes: int) -> str:
    """Format a byte count compactly for the upload-limit error."""
    if size_bytes >= BYTES_PER_GB:
        return f"{size_bytes / BYTES_PER_GB:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.1f} KB"
    return f"{size_bytes} bytes"


def _large_upload_entries(local_dir: str | Path, sync_cfg: SyncConfig, *, portable: bool = False) -> list[_LargeUploadEntry]:
    """Find the largest filtered files and aggregate folders after an upload crosses its streaming limit.

    This diagnostic intentionally runs only on failure, preserving the one-pass successful upload path. A local rsync
    dry run remains the selection engine so `.gitignore`, `.fwdignore`, and configured exclusions match the failed
    transport. Tar fallback passes ``portable=True`` because it cannot implement per-directory `.gitignore` rules.
    """
    source = Path(local_dir).expanduser().resolve()
    folder_sizes: dict[str, int] = {}
    large_files: list[_LargeUploadEntry] = []
    with selection.upload_manifest(source, sync_cfg, extra_excludes=ALWAYS_SYNC_EXCLUDES) as manifest, tempfile.TemporaryDirectory(prefix="fwd-upload-diagnostic-") as destination:
        filters = selection.rsync_manifest_args(manifest) if manifest is not None else (_portable_filters(sync_cfg, source) if portable else rsync_filters(sync_cfg, source))
        argv = [
            *RSYNC_BASE,
            "--dry-run",
            f"--out-format={_RSYNC_ENTRY_PREFIX}%l\t%n",
            *filters,
            f"{source}/",
            f"{destination}/",
        ]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "LC_ALL": "C"})
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.startswith(_RSYNC_ENTRY_PREFIX):
                continue
            payload = line.removeprefix(_RSYNC_ENTRY_PREFIX).rstrip("\n")
            size_text, separator, relative_text = payload.partition("\t")
            if not separator or relative_text.endswith("/"):
                continue
            try:
                size_bytes = int(size_text)
            except ValueError:
                continue
            relative = PurePosixPath(relative_text)
            if size_bytes > _LARGE_UPLOAD_ENTRY_BYTES:
                large_files.append(_LargeUploadEntry(relative.as_posix(), size_bytes, "file"))
            for parent in relative.parents:
                parent_text = parent.as_posix()
                if parent_text == ".":
                    break
                folder_sizes[parent_text] = folder_sizes.get(parent_text, 0) + size_bytes
        returncode = proc.wait()
    if returncode != 0:
        raise SSHError(f"could not inspect large upload entries (rsync exit {returncode})")
    large_folders = [
        _LargeUploadEntry(path, size_bytes, "folder")
        for path, size_bytes in folder_sizes.items()
        if size_bytes > _LARGE_UPLOAD_ENTRY_BYTES
    ]
    return sorted([*large_files, *large_folders], key=lambda entry: (-entry.size_bytes, entry.kind, entry.path))[:_MAX_LARGE_UPLOAD_ENTRIES]


def _upload_limit_error(
    local_dir: str | Path,
    sync_cfg: SyncConfig,
    observed_bytes: int,
    *,
    cleanup_complete: bool = True,
    portable: bool = False,
) -> None:
    """Explain a transfer-time limit rejection and identify its largest included files and folders."""
    source = Path(local_dir).expanduser().resolve()
    suggested_gb = max(1, int(sync_cfg.max_size_gb) + 1)
    project_path = source / config_mod.PROJECT_CONFIG_RELPATH
    fwdignore_path = source / FWDIGNORE_NAME
    cleanup = (
        "removed the incomplete remote staging copy"
        if cleanup_complete
        else "could not confirm removal of the incomplete remote staging copy; it was never applied to the live project"
    )
    ui.info("upload limit reached; finding included files and folders larger than 200 MB")
    try:
        large_entries = _large_upload_entries(source, sync_cfg, portable=portable)
    except (OSError, SSHError) as exc:
        ui.warn(f"could not inspect the largest upload entries: {exc}")
        large_entries = []
    large_detail = ""
    if large_entries:
        lines = [f"  {_display_size(entry.size_bytes):>9}  {entry.kind:<6}  {entry.path}" for entry in large_entries]
        large_detail = "\nLargest included files/folders over 200 MB:\n" + "\n".join(lines)
    ui.die(
        f"upload from {source} crossed sync.max_size_gb={sync_cfg.max_size_gb:g} GB while streaming "
        f"(stopped at approximately {_display_size(observed_bytes)} and {cleanup}). "
        f"To omit unintended files or folders, add their project-relative paths to {fwdignore_path} "
        f"(create the file if needed), then retry. "
        f"Raise this project only with '{ui.command(f'config set --project sync.max_size_gb {suggested_gb}')}', or set max_size_gb = {suggested_gb} "
        f"under [sync] in {project_path}. To change the user default, run '{ui.command(f'config set sync.max_size_gb {suggested_gb}')}' "
        f"or edit {config_mod.GLOBAL_CONFIG_PATH}.{large_detail}"
    )


def _stop_process(proc: subprocess.Popen[object]) -> None:
    """Stop a transfer subprocess and its ssh/tar descendants, escalating only when graceful termination stalls."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _stream_transfer_paths(stream, on_path: TransferPath | None, tar_style: bool = False) -> None:
    """Drain a subprocess line stream and report normalized non-empty project-relative paths.

    BSD tar prefixes verbose archive/extract members with ``a `` or ``x `` while GNU tar prints the member directly.
    SSH and tar diagnostics share stderr on fallback pulls, so recognizable diagnostics remain warnings rather than
    being mislabeled as project files.
    """
    for raw_line in stream:
        line = raw_line.decode(errors="replace") if isinstance(raw_line, bytes) else raw_line
        path = line.rstrip("\r\n")
        if not path or path in (".", "./"):
            continue
        if tar_style and path.startswith(("Warning:", "ssh:", "tar:")):
            ui.warn(path)
            continue
        if tar_style and path.startswith(("a ", "x ")):
            path = path[2:]
        if path.startswith("./"):
            path = path[2:]
        if on_path is not None:
            on_path(path)


def _run_bounded_rsync(
    argv: Sequence[str],
    sentinel: Path,
    on_progress: TransferProgress | None = None,
    on_path: TransferPath | None = None,
) -> None:
    """Run rsync, concurrently forwarding paths and relay byte updates, then translate its limit sentinel."""
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    diagnostics: list[str] = []
    assert proc.stdout is not None
    assert proc.stderr is not None
    path_thread = threading.Thread(target=_stream_transfer_paths, args=(proc.stdout, on_path), daemon=True)
    path_thread.start()
    for line in proc.stderr:
        if line.startswith(PROGRESS_PREFIX):
            try:
                transferred_bytes = int(line.removeprefix(PROGRESS_PREFIX).strip())
            except ValueError:
                continue
            if on_progress is not None:
                on_progress(transferred_bytes)
        else:
            diagnostics.append(line.rstrip())
    returncode = proc.wait()
    path_thread.join()
    if sentinel.is_file():
        try:
            observed_bytes = int(sentinel.read_text(encoding="utf-8"))
        except ValueError:
            observed_bytes = 0
        raise _UploadLimitExceeded(observed_bytes)
    if returncode in RSYNC_PARTIAL_EXITS:
        ui.warn(f"rsync push completed with warnings (rsync exit {returncode}: some files were skipped)")
        return
    if returncode != 0:
        detail = "\n".join(line for line in diagnostics if line)
        raise SSHError(f"rsync push failed (exit {returncode})" + (f": {detail}" if detail else ""))


def _run(argv: Sequence[str], *, what: str, on_path: TransferPath | None = None) -> None:
    """Run a transfer subprocess with output streamed, raising :class:`SSHError` on a genuine failure.

    Partial-transfer exits are downgraded to a warning: they mean the bytes arrived but some per-file operation was
    refused, and aborting there would kill a launch (and waste an already-provisioned pod) over something cosmetic.
    """
    if on_path is None:
        returncode = subprocess.run(list(argv), check=False).returncode
    else:
        proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, text=True, bufsize=1)
        assert proc.stdout is not None
        _stream_transfer_paths(proc.stdout, on_path)
        returncode = proc.wait()
    if returncode in RSYNC_PARTIAL_EXITS:
        ui.warn(f"{what} completed with warnings (rsync exit {returncode}: some files were skipped)")
        return
    if returncode != 0:
        raise SSHError(f"{what} failed (exit {returncode})")


def _create_remote_stage(endpoint: SSHEndpoint, remote_dir: str) -> str:
    """Create and return a sibling staging directory so an interrupted upload cannot mutate the live project."""
    remote = shlex.quote(remote_dir.rstrip("/"))
    proc = endpoint.run(f"set -eu; remote={remote}; parent=$(dirname \"$remote\"); mkdir -p \"$parent\"; mktemp -d \"$parent/.fwd-upload.XXXXXX\"")
    stage = (proc.stdout or "").strip()
    if not stage or not Path(stage).name.startswith(".fwd-upload."):
        raise SSHError(f"remote host returned an invalid upload staging path: {stage!r}")
    return stage


def _cleanup_remote_stage(endpoint: SSHEndpoint, stage: str) -> bool:
    """Best-effort removal for a validated directory created by :func:`_create_remote_stage`."""
    if not Path(stage).name.startswith(".fwd-upload."):
        raise SSHError(f"refusing to remove invalid upload staging path: {stage!r}")
    try:
        proc = endpoint.run(f"rm -rf -- {shlex.quote(stage)}", check=False)
    except SSHError as exc:
        ui.warn(f"could not remove remote upload staging directory {stage!r}: {exc}")
        return False
    if proc.returncode != 0:
        ui.warn(f"could not remove remote upload staging directory {stage!r} (remote exit {proc.returncode})")
        return False
    return True


def _rsync_commit_command(stage: str, remote_dir: str, sync_cfg: SyncConfig, source: Path, *, delete: bool) -> str:
    """Build the remote stage-to-project rsync that runs only after the bounded network transfer succeeds."""
    stage_arg = shlex.quote(f"{stage.rstrip('/')}/")
    remote_arg = shlex.quote(f"{remote_dir.rstrip('/')}/")
    excludes = shlex.quote(_combined_excludes(sync_cfg, source))
    permanent_args = "".join(f" {shlex.quote(f'--exclude={pattern}')}" for pattern in ALWAYS_SYNC_EXCLUDES)
    filter_arg = f" {shlex.quote('--filter=:- .gitignore')}" if sync_cfg.use_gitignore else ""
    delete_arg = " --delete" if delete and sync_cfg.delete else ""
    return (
        f"set -eu; stage={shlex.quote(stage)}; remote={shlex.quote(remote_dir.rstrip('/'))}; "
        "parent=$(dirname \"$remote\"); excludes=$(mktemp \"$parent/.fwd-excludes.XXXXXX\"); "
        "cleanup() { rm -rf -- \"$stage\"; rm -f -- \"$excludes\"; }; trap cleanup EXIT HUP INT TERM; "
        f"printf %s {excludes} > \"$excludes\"; mkdir -p \"$remote\"; "
        f"rsync -a --no-owner --no-group{permanent_args}{filter_arg} --exclude-from=\"$excludes\"{delete_arg} {stage_arg} {remote_arg}"
    )


def sync_up(
    endpoint: SSHEndpoint,
    local_dir: str | Path,
    remote_dir: str,
    sync_cfg: SyncConfig,
    *,
    delete: bool = True,
    on_progress: TransferProgress | None = None,
    on_path: TransferPath | None = None,
) -> None:
    """Mirror the local project directory to the remote machine.

    Dispatches to :func:`tar_up` when ``endpoint.supports_rsync`` is ``False``.

    Args:
        endpoint: Target machine.
        local_dir: Local project root.
        remote_dir: Absolute remote destination.
        sync_cfg: Filter configuration.
        delete: Remove remote files absent locally, making the copy an exact mirror.
        on_progress: Optional callback receiving cumulative compressed outbound bytes.
        on_path: Optional callback receiving each project-relative path as rsync selects it for transfer.
    """
    if not endpoint.supports_rsync:
        tar_up(endpoint, local_dir, remote_dir, sync_cfg, delete=delete, on_progress=on_progress, on_path=on_path)
        return

    source_path = Path(local_dir).expanduser()
    source = f"{str(source_path).rstrip('/')}/"
    with selection.upload_manifest(source_path, sync_cfg, extra_excludes=ALWAYS_SYNC_EXCLUDES) as manifest:
        stage = _create_remote_stage(endpoint, remote_dir)
        limit_bytes = int(sync_cfg.max_size_gb * BYTES_PER_GB)
        try:
            with tempfile.TemporaryDirectory(prefix="fwd-rsync-limit-") as limit_dir:
                sentinel = Path(limit_dir) / "exceeded"
                relay = Path(__file__).with_name("rsync_transport.py")
                bounded_shell = shlex.join(
                    [
                        sys.executable,
                        str(relay),
                        "--limit",
                        str(limit_bytes),
                        "--sentinel",
                        str(sentinel),
                        "--",
                        *shlex.split(endpoint.rsync_shell()),
                    ]
                )
                upload_filters = selection.rsync_manifest_args(manifest) if manifest is not None else rsync_filters(sync_cfg, source_path)
                argv = [
                    *RSYNC_BASE,
                    *(["--out-format=%n%L"] if on_path is not None else []),
                    "-e",
                    bounded_shell,
                    *upload_filters,
                    source,
                    f"{endpoint.ssh_target()}:{stage}/",
                ]
                if on_path is None:
                    _run_bounded_rsync(argv, sentinel, on_progress)
                else:
                    _run_bounded_rsync(argv, sentinel, on_progress, on_path)
            endpoint.run(_rsync_commit_command(stage, remote_dir, sync_cfg, source_path, delete=delete))
        except _UploadLimitExceeded as exc:
            cleanup_complete = _cleanup_remote_stage(endpoint, stage)
            stage = ""
            _upload_limit_error(source_path, sync_cfg, exc.observed_bytes, cleanup_complete=cleanup_complete)
        finally:
            if stage:
                _cleanup_remote_stage(endpoint, stage)


def sync_down(
    endpoint: SSHEndpoint,
    remote_dir: str,
    local_dir: str | Path,
    paths: Sequence[str] = (),
    sync_cfg: SyncConfig | None = None,
    *,
    filter_dir: str | Path | None = None,
    manifest: Path | None = None,
    extra_excludes: Sequence[str] = (),
    include_fwdignore: bool = True,
    on_path: TransferPath | None = None,
) -> None:
    """Pull work back from the remote machine.

    Never deletes local files — a pull is additive by design.

    Args:
        paths: Specific paths relative to ``remote_dir``; empty means the whole tree (minus excludes).
        filter_dir: Project directory whose ``.fwdignore`` supplies filters when ``local_dir`` is a temporary snapshot.
        manifest: Optional local NUL-delimited selection applied to the remote source for an exact read-only snapshot.
        extra_excludes: Patterns that remain excluded even when the caller broadens ordinary sync filters.
        include_fwdignore: Whether the project `.fwdignore` participates; broad diagnostic comparisons disable it.
        on_path: Optional callback receiving each project-relative path as it is downloaded.
    """
    if not endpoint.supports_rsync:
        tar_down(
            endpoint,
            remote_dir,
            local_dir,
            paths,
            sync_cfg,
            filter_dir=filter_dir,
            manifest=manifest,
            extra_excludes=extra_excludes,
            include_fwdignore=include_fwdignore,
            on_path=on_path,
        )
        return

    destination = Path(local_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    base = remote_dir.rstrip("/")
    argv = [*RSYNC_BASE, "-e", endpoint.rsync_shell()]
    if on_path is not None:
        argv.append("--out-format=%n%L")
    pull_excludes = tuple(dict.fromkeys((*ALWAYS_PULL_EXCLUDES, *extra_excludes)))
    argv += [f"--exclude={pattern}" for pattern in pull_excludes]
    if manifest is not None and not paths:
        argv += selection.rsync_manifest_args(manifest)
    elif sync_cfg is not None and not paths:
        # Configurable project filters apply only to whole-tree pulls. Permanent platform and repository-metadata
        # exclusions above also protect explicit path pulls, because those files are never a valid sync result.
        argv += rsync_filters(sync_cfg, filter_dir or local_dir, include_fwdignore=include_fwdignore)
    if paths:
        # rsync accepts several sources against one host by using the :"a" :"b" form; simplest correct spelling is one
        # source per relative path, all sharing the same remote root.
        argv += [f"{endpoint.ssh_target()}:{base}/{p.lstrip('/')}" for p in paths]
        argv.append(f"{destination}/")
    else:
        argv += [f"{endpoint.ssh_target()}:{base}/", f"{destination}/"]
    _run(argv, what="rsync pull", on_path=on_path)


def _tar_excludes(sync_cfg: SyncConfig) -> list[str]:
    """Translate the configured excludes into ``tar --exclude`` flags.

    The tar fallback cannot honour ``.gitignore`` (tar has no per-directory merge concept), so callers already warn
    that this transport is degraded.
    """
    return [f"--exclude={pattern}" for pattern in (*ALWAYS_SYNC_EXCLUDES, *sync_cfg.exclude)]


def _combined_excludes(sync_cfg: SyncConfig, source: Path) -> str:
    """Return the exclusion file used to identify remote paths owned by a staged synchronization.

    Applying the upload transport's configured and ``.fwdignore`` patterns during the remote stage commit keeps
    deletion from removing excluded environments and caches that intentionally survive outside the sync domain.
    """
    patterns = [*ALWAYS_SYNC_EXCLUDES, *sync_cfg.exclude]
    fwdignore = source / FWDIGNORE_NAME
    if fwdignore.is_file():
        patterns.extend(fwdignore.read_text(encoding="utf-8").splitlines())
    return "".join(f"{pattern}\n" for pattern in patterns if pattern)


def _tar_mirror_command(remote_dir: str, excludes: str, *, delete: bool = True) -> str:
    """Build a staged remote tar extraction command with optional rsync-like stale-file deletion.

    The upload first lands in a sibling staging directory. Sorted tar manifests identify old, non-excluded paths
    absent from the incoming tree; files are removed and directories are removed only when empty, so an excluded
    descendant prevents its parent from being deleted. The stage is also used without deletion so an interrupted or
    over-limit stream never partially overwrites the live project. Type changes are resolved before the overlay.
    """
    remote = shlex.quote(remote_dir.rstrip("/"))
    exclude_text = shlex.quote(excludes)
    delete_command = (
        "LC_ALL=C comm -23 \"$old\" \"$new\" | LC_ALL=C sort -r | while IFS= read -r entry; do "
        "[ \"$entry\" = \"./\" ] && continue; relative=${entry#./}; "
        "existing=\"$remote/$relative\"; if [ -d \"$existing\" ] && [ ! -L \"$existing\" ]; then rmdir -- \"$existing\" 2>/dev/null || true; else rm -f -- \"$existing\"; fi; "
        "done; "
        if delete
        else ""
    )
    return (
        f"set -eu; remote={remote}; parent=$(dirname \"$remote\"); mkdir -p \"$remote\" \"$parent\"; "
        f"stage=$(mktemp -d \"$parent/.fwd-upload.XXXXXX\"); old=$(mktemp \"$parent/.fwd-old.XXXXXX\"); "
        f"new=$(mktemp \"$parent/.fwd-new.XXXXXX\"); old_raw=$(mktemp \"$parent/.fwd-old-raw.XXXXXX\"); "
        f"new_raw=$(mktemp \"$parent/.fwd-new-raw.XXXXXX\"); excludes=$(mktemp \"$parent/.fwd-excludes.XXXXXX\"); "
        "cleanup() { rm -rf -- \"$stage\"; rm -f -- \"$old\" \"$new\" \"$old_raw\" \"$new_raw\" \"$excludes\"; }; trap cleanup EXIT HUP INT TERM; "
        f"printf %s {exclude_text} > \"$excludes\"; tar xzf - -v -C \"$stage\" > \"$new_raw\" 2>&1; "
        "tar cf /dev/null -v --exclude-from=\"$excludes\" -C \"$remote\" . > \"$old_raw\" 2>&1; "
        "sed 's/^[ax] //' \"$old_raw\" | LC_ALL=C sort > \"$old\"; sed 's/^[ax] //' \"$new_raw\" | LC_ALL=C sort > \"$new\"; "
        f"{delete_command}"
        "while IFS= read -r entry; do [ \"$entry\" = \"./\" ] && continue; relative=${entry#./}; "
        "incoming=\"$stage/$relative\"; existing=\"$remote/$relative\"; "
        "if [ -d \"$incoming\" ] && [ ! -L \"$incoming\" ]; then "
        "if { [ -e \"$existing\" ] || [ -L \"$existing\" ]; } && { [ ! -d \"$existing\" ] || [ -L \"$existing\" ]; }; then rm -f -- \"$existing\"; fi; "
        "elif [ -d \"$existing\" ] && [ ! -L \"$existing\" ]; then rm -rf -- \"$existing\"; fi; "
        "done < \"$new\"; cp -a \"$stage\"/. \"$remote\"/"
    )


def tar_up(
    endpoint: SSHEndpoint,
    local_dir: str | Path,
    remote_dir: str,
    sync_cfg: SyncConfig,
    *,
    delete: bool = True,
    on_progress: TransferProgress | None = None,
    on_path: TransferPath | None = None,
) -> None:
    """Upload by streaming a local tar into a remote project, for transports without rsync.

    Filtering happens locally when building the archive. When deletion is enabled, remote manifests remove stale
    synchronized paths while preserving content selected by ``sync.exclude`` and ``.fwdignore``. ``on_progress``
    receives cumulative compressed bytes after each chunk reaches the SSH process; ``on_path`` receives tar's
    project-relative member names while the archive is produced.
    """
    source = Path(local_dir).expanduser()
    with selection.upload_manifest(source, sync_cfg, extra_excludes=ALWAYS_SYNC_EXCLUDES) as manifest:
        if manifest is not None:
            tar_argv = ["tar", "czvf" if on_path is not None else "czf", "-", "-C", str(source), "--null", f"--files-from={manifest}"]
        else:
            tar_excludes = _tar_excludes(sync_cfg)
            fwdignore = source / FWDIGNORE_NAME
            if fwdignore.is_file():
                tar_excludes.append(f"--exclude-from={fwdignore}")
            tar_argv = ["tar", "czvf" if on_path is not None else "czf", "-", *tar_excludes, "-C", str(source), "."]
        remote_excludes = _combined_excludes(sync_cfg, source)
        if manifest is not None:
            remote_excludes += selection.git_ignored_patterns(source, sync_cfg)
        remote_command = _tar_mirror_command(remote_dir, remote_excludes, delete=delete and sync_cfg.delete)
        ssh_argv = [*endpoint.ssh_argv(), remote_command]
        try:
            _pipe(
                tar_argv,
                ssh_argv,
                what="tar push",
                max_bytes=int(sync_cfg.max_size_gb * BYTES_PER_GB),
                on_progress=on_progress,
                on_path=on_path,
                producer_paths_from_stderr=on_path is not None,
            )
        except _UploadLimitExceeded as exc:
            _upload_limit_error(source, sync_cfg, exc.observed_bytes, portable=True)


def _tar_down_manifest(endpoint: SSHEndpoint, remote_dir: str, destination: Path, manifest: Path) -> None:
    """Download exactly a local NUL manifest over SSH, tolerating absent remote members as comparison differences.

    The extractor drains SSH stdout while the manifest is written to SSH stdin, preserving the bidirectional protocol
    without buffering the archive. GNU and BSD tar both return 1 when a listed source member is missing; that is an
    expected one-sided diff, so exit 1 is accepted only when local extraction completed successfully.
    """
    remote = f"tar czf - -C {shlex.quote(remote_dir.rstrip('/'))} --null --files-from=-"
    env = {**os.environ, **TAR_ENV}
    ssh = subprocess.Popen([*endpoint.ssh_argv(), remote], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, start_new_session=True)
    assert ssh.stdin is not None
    assert ssh.stdout is not None
    extractor = subprocess.Popen(["tar", "xzf", "-", "-C", str(destination)], stdin=ssh.stdout, env=env, start_new_session=True)
    ssh.stdout.close()
    try:
        ssh.stdin.write(manifest.read_bytes())
        ssh.stdin.close()
        extractor_rc = extractor.wait()
        ssh_rc = ssh.wait()
    except BrokenPipeError:
        _stop_process(ssh)
        _stop_process(extractor)
        raise SSHError("tar pull failed because the remote side closed the manifest stream")
    if extractor_rc != 0 or ssh_rc not in (0, 1):
        raise SSHError(f"tar pull failed (remote exit {ssh_rc}, local exit {extractor_rc})")
    if ssh_rc == 1:
        ui.warn("remote snapshot omitted one or more selected paths that are absent or unreadable; they will appear as differences")


def tar_down(
    endpoint: SSHEndpoint,
    remote_dir: str,
    local_dir: str | Path,
    paths: Sequence[str] = (),
    sync_cfg: SyncConfig | None = None,
    *,
    filter_dir: str | Path | None = None,
    manifest: Path | None = None,
    extra_excludes: Sequence[str] = (),
    include_fwdignore: bool = True,
    on_path: TransferPath | None = None,
) -> None:
    """Download by streaming a remote tar into a local extract, for transports without rsync."""
    destination = Path(local_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    if manifest is not None and not paths:
        _tar_down_manifest(endpoint, remote_dir, destination, manifest)
        return
    members = [shlex.quote(p.lstrip("/")) for p in paths] or ["."]
    excludes = [f"--exclude={pattern}" for pattern in ALWAYS_PULL_EXCLUDES]
    if sync_cfg is not None and not paths:
        excludes.extend(f"--exclude={pattern}" for pattern in sync_cfg.exclude)
    if sync_cfg is not None and not paths and include_fwdignore:
        fwdignore = Path(filter_dir or local_dir).expanduser() / FWDIGNORE_NAME
        if fwdignore.is_file():
            excludes.extend(f"--exclude={pattern}" for pattern in fwdignore.read_text(encoding="utf-8").splitlines() if pattern)
    excludes.extend(f"--exclude={pattern}" for pattern in extra_excludes)
    excludes = list(dict.fromkeys(excludes))
    tar_mode = "czvf" if on_path is not None else "czf"
    remote = f"tar {tar_mode} - {' '.join(shlex.quote(flag) for flag in excludes)} -C {shlex.quote(remote_dir.rstrip('/'))} {' '.join(members)}"
    _pipe(
        [*endpoint.ssh_argv(), remote],
        ["tar", "xzf", "-", "-C", str(destination)],
        what="tar pull",
        on_path=on_path,
        producer_paths_from_stderr=on_path is not None,
    )


def _pipe(
    producer: Sequence[str],
    consumer: Sequence[str],
    *,
    what: str,
    max_bytes: int | None = None,
    on_progress: TransferProgress | None = None,
    on_path: TransferPath | None = None,
    producer_paths_from_stderr: bool = False,
) -> None:
    """Stream ``producer`` stdout into ``consumer`` stdin and raise if either side fails.

    Both exit codes are checked because a remote extract can fail (disk full, missing tar) long after the local tar
    finished happily, and silently losing that would corrupt the remote tree. A bounded upload is relayed through this
    process so compressed wire bytes can stop the transfer without a separate filesystem scan; ``on_progress`` receives
    the cumulative count after each forwarded chunk. When tar writes verbose member names to producer stderr, a
    dedicated thread drains them into ``on_path`` so a large listing cannot deadlock the archive stream.
    """
    env = {**os.environ, **TAR_ENV}
    if max_bytes is not None:
        left = subprocess.Popen(
            list(producer),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if producer_paths_from_stderr else None,
            env=env,
            start_new_session=True,
        )
        right = subprocess.Popen(list(consumer), stdin=subprocess.PIPE, env=env, start_new_session=True)
        path_thread = None
        if producer_paths_from_stderr:
            assert left.stderr is not None
            path_thread = threading.Thread(target=_stream_transfer_paths, args=(left.stderr, on_path, True), daemon=True)
            path_thread.start()
        observed_bytes = 0
        assert left.stdout is not None
        assert right.stdin is not None
        try:
            while chunk := left.stdout.read1(_STREAM_CHUNK_SIZE):
                observed_bytes += len(chunk)
                if observed_bytes > max_bytes:
                    raise _UploadLimitExceeded(observed_bytes)
                right.stdin.write(chunk)
                if on_progress is not None:
                    on_progress(observed_bytes)
            right.stdin.close()
            left_rc = left.wait()
            right_rc = right.wait()
            if path_thread is not None:
                path_thread.join()
        except _UploadLimitExceeded:
            _stop_process(left)
            right.stdin.close()
            try:
                right.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process(right)
            if path_thread is not None:
                path_thread.join()
            raise
        except BrokenPipeError:
            _stop_process(left)
            _stop_process(right)
            if path_thread is not None:
                path_thread.join()
            raise SSHError(f"{what} failed because the remote side closed the stream")
        if left_rc != 0 or right_rc != 0:
            raise SSHError(f"{what} failed (local exit {left_rc}, remote exit {right_rc})")
        return
    with subprocess.Popen(
        list(producer),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if producer_paths_from_stderr else None,
        env=env,
    ) as left:
        assert left.stdout is not None
        path_thread = None
        if producer_paths_from_stderr:
            assert left.stderr is not None
            path_thread = threading.Thread(target=_stream_transfer_paths, args=(left.stderr, on_path, True), daemon=True)
            path_thread.start()
        right = subprocess.Popen(list(consumer), stdin=left.stdout, env=env)
        # Closing our copy lets the producer see EPIPE if the consumer dies, instead of blocking forever.
        left.stdout.close()
        right_rc = right.wait()
        if path_thread is not None:
            path_thread.join()
    if left.returncode != 0 or right_rc != 0:
        raise SSHError(f"{what} failed (local exit {left.returncode}, remote exit {right_rc})")
