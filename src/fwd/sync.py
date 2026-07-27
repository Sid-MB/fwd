"""File transfer — rsync, with a tar-over-ssh fallback.

Design intent (owned by the core/sync teammate)
-----------------------------------------------
One-shot transfers only; no continuous watching in the MVP. Push is a mirror (``--delete``) so the remote tree is a
faithful copy of local, while pull is additive and path-scoped so a careless pull cannot delete local work.

Filters combine three sources, in order: the repo's own ``.gitignore`` (via rsync's ``:- .gitignore`` per-directory
filter), an optional ``.fwdignore`` for remote-specific exclusions, and ``SyncConfig.exclude``. ``.git`` is never
excluded — the remote session needs history to diff, blame and commit.

``tar_up``/``tar_down`` exist because RunPod's proxy transport cannot run a remote rsync binary. They stream a tar
through ssh, giving correctness at the cost of no delta transfer, and callers must warn the user when they engage.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from fwd import ui
from fwd.config import SyncConfig
from fwd.sshexec import SSHEndpoint, SSHError

# Remote-specific ignore file, sitting alongside .gitignore. Exists so a project can keep something out of the remote
# machine (large fixtures, local-only secrets) without polluting its git ignore rules.
FWDIGNORE_NAME = ".fwdignore"

# -a preserves modes/symlinks/times, -z compresses on the wire. No -v: progress is fwd's ui.step, not rsync spam.
#
# --no-owner/--no-group cancel the -o/-g that -a implies, and must come *after* -a because rsync applies options left
# to right. Owner preservation is meaningless for us — the remote is a single-user container — and it is actively
# fatal on RunPod, whose /workspace is a MooseFS network volume that refuses chown to a foreign uid. Local files are
# uid 501, so without these flags every single file logs "chown failed: Operation not permitted" and rsync exits 23.
RSYNC_BASE: tuple[str, ...] = ("rsync", "-az", "--no-owner", "--no-group")

# rsync exit codes that mean "the transfer happened, but some files were skipped": 23 = partial transfer due to error
# (a permission fixup, an unreadable file), 24 = source files vanished mid-run (normal when a build is running).
# Treating these as fatal aborts a launch after the pod has already been rented, for files that did transfer.
RSYNC_PARTIAL_EXITS: frozenset[int] = frozenset({23, 24})

# macOS bsdtar stores extended attributes as AppleDouble "._name" sidecar files, which arrive as visible junk in every
# directory of a Linux remote. COPYFILE_DISABLE suppresses them; GNU tar ignores the variable.
TAR_ENV: dict[str, str] = {"COPYFILE_DISABLE": "1"}


def rsync_filters(sync_cfg: SyncConfig, local_dir: str | Path) -> list[str]:
    """Build the rsync filter/exclude argv fragment for a project directory.

    The gitignore merge rule comes first so it reads like the repo's own semantics, then the configured excludes, then
    ``.fwdignore``. Ordering is cosmetic here because every rule is an exclusion and rsync only cares about precedence
    between conflicting include/exclude pairs, of which we emit none.

    ``SyncConfig.exclude`` is used verbatim rather than unioned with ``DEFAULT_EXCLUDES``: ``config.load_config``
    already seeds it with the defaults, and unioning at use time would make it impossible for a project to *shrink*
    the list (the documented behaviour in ``config.py``).

    Args:
        sync_cfg: Exclude patterns and whether to honour ``.gitignore``.
        local_dir: Project root, inspected for ``.gitignore``/``.fwdignore``.

    Returns:
        rsync arguments, e.g. ``["--filter=:- .gitignore", "--exclude=.venv", ...]``.
    """
    root = Path(local_dir).expanduser()
    args: list[str] = []
    if sync_cfg.use_gitignore:
        # ':-' is a per-directory merge: every .gitignore in the tree applies to its own subtree, matching git.
        args.append("--filter=:- .gitignore")
    args += [f"--exclude={pattern}" for pattern in sync_cfg.exclude]
    fwdignore = root / FWDIGNORE_NAME
    if fwdignore.is_file():
        args.append(f"--exclude-from={fwdignore}")
    return args


def _run(argv: Sequence[str], *, what: str) -> None:
    """Run a transfer subprocess with output streamed, raising :class:`SSHError` on a genuine failure.

    Partial-transfer exits are downgraded to a warning: they mean the bytes arrived but some per-file operation was
    refused, and aborting there would kill a launch (and waste an already-rented pod) over something cosmetic.
    """
    proc = subprocess.run(list(argv), check=False)
    if proc.returncode in RSYNC_PARTIAL_EXITS:
        ui.warn(f"{what} completed with warnings (rsync exit {proc.returncode}: some files were skipped)")
        return
    if proc.returncode != 0:
        raise SSHError(f"{what} failed (exit {proc.returncode})")


def _ensure_remote_dir(endpoint: SSHEndpoint, remote_dir: str) -> None:
    """Create the remote destination. rsync only creates the final path component, not intermediate parents."""
    endpoint.run(f"mkdir -p {shlex.quote(remote_dir)}")


def sync_up(
    endpoint: SSHEndpoint,
    local_dir: str | Path,
    remote_dir: str,
    sync_cfg: SyncConfig,
    *,
    delete: bool = True,
) -> None:
    """Mirror the local project directory to the remote machine.

    Dispatches to :func:`tar_up` when ``endpoint.supports_rsync`` is ``False``.

    Args:
        endpoint: Target machine.
        local_dir: Local project root.
        remote_dir: Absolute remote destination.
        sync_cfg: Filter configuration.
        delete: Remove remote files absent locally, making the copy an exact mirror.
    """
    if not endpoint.supports_rsync:
        tar_up(endpoint, local_dir, remote_dir, sync_cfg)
        return

    source = f"{str(Path(local_dir).expanduser()).rstrip('/')}/"
    _ensure_remote_dir(endpoint, remote_dir)
    argv = [
        *RSYNC_BASE,
        "-e",
        endpoint.rsync_shell(),
        *rsync_filters(sync_cfg, local_dir),
    ]
    if delete and sync_cfg.delete:
        argv.append("--delete")
    argv += [source, f"{endpoint.ssh_target()}:{remote_dir}/"]
    _run(argv, what="rsync push")


def sync_down(
    endpoint: SSHEndpoint,
    remote_dir: str,
    local_dir: str | Path,
    paths: Sequence[str] = (),
    sync_cfg: SyncConfig | None = None,
) -> None:
    """Pull work back from the remote machine.

    Never deletes local files — a pull is additive by design.

    Args:
        paths: Specific paths relative to ``remote_dir``; empty means the whole tree (minus excludes).
    """
    if not endpoint.supports_rsync:
        tar_down(endpoint, remote_dir, local_dir, paths)
        return

    destination = Path(local_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    base = remote_dir.rstrip("/")
    argv = [*RSYNC_BASE, "-e", endpoint.rsync_shell()]
    if sync_cfg is not None and not paths:
        # Path-scoped pulls are an explicit user request ("give me exactly this file"), so filters only apply to a
        # whole-tree pull where the excludes are what keep .venv/node_modules from coming back down.
        argv += rsync_filters(sync_cfg, local_dir)
    if paths:
        # rsync accepts several sources against one host by using the :"a" :"b" form; simplest correct spelling is one
        # source per relative path, all sharing the same remote root.
        argv += [f"{endpoint.ssh_target()}:{base}/{p.lstrip('/')}" for p in paths]
        argv.append(f"{destination}/")
    else:
        argv += [f"{endpoint.ssh_target()}:{base}/", f"{destination}/"]
    _run(argv, what="rsync pull")


def _tar_excludes(sync_cfg: SyncConfig) -> list[str]:
    """Translate the configured excludes into ``tar --exclude`` flags.

    The tar fallback cannot honour ``.gitignore`` (tar has no per-directory merge concept), so callers already warn
    that this transport is degraded.
    """
    return [f"--exclude={pattern}" for pattern in sync_cfg.exclude]


def tar_up(endpoint: SSHEndpoint, local_dir: str | Path, remote_dir: str, sync_cfg: SyncConfig) -> None:
    """Upload by streaming a local tar into a remote ``tar -x``, for transports without rsync.

    Filtering happens locally when building the archive, since the remote side only extracts.
    """
    source = Path(local_dir).expanduser()
    _ensure_remote_dir(endpoint, remote_dir)
    tar_argv = ["tar", "czf", "-", *_tar_excludes(sync_cfg), "-C", str(source), "."]
    ssh_argv = [*endpoint.ssh_argv(), f"tar xzf - -C {shlex.quote(remote_dir)}"]
    _pipe(tar_argv, ssh_argv, what="tar push")


def tar_down(
    endpoint: SSHEndpoint,
    remote_dir: str,
    local_dir: str | Path,
    paths: Sequence[str] = (),
) -> None:
    """Download by streaming a remote tar into a local extract, for transports without rsync."""
    destination = Path(local_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    members = [shlex.quote(p.lstrip("/")) for p in paths] or ["."]
    remote = f"tar czf - -C {shlex.quote(remote_dir.rstrip('/'))} {' '.join(members)}"
    _pipe([*endpoint.ssh_argv(), remote], ["tar", "xzf", "-", "-C", str(destination)], what="tar pull")


def _pipe(producer: Sequence[str], consumer: Sequence[str], *, what: str) -> None:
    """Stream ``producer`` stdout into ``consumer`` stdin and raise if either side fails.

    Both exit codes are checked because a remote extract can fail (disk full, missing tar) long after the local tar
    finished happily, and silently losing that would corrupt the remote tree.
    """
    env = {**os.environ, **TAR_ENV}
    with subprocess.Popen(list(producer), stdout=subprocess.PIPE, env=env) as left:
        assert left.stdout is not None
        right = subprocess.Popen(list(consumer), stdin=left.stdout, env=env)
        # Closing our copy lets the producer see EPIPE if the consumer dies, instead of blocking forever.
        left.stdout.close()
        right_rc = right.wait()
    if left.returncode != 0 or right_rc != 0:
        raise SSHError(f"{what} failed (local exit {left.returncode}, remote exit {right_rc})")
