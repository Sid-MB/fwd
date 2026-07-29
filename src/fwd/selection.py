"""Authoritative local upload manifests for Git working trees.

Rsync's per-directory merge support is not consistent across implementations: macOS openrsync can miss deeply nested
``.gitignore`` files when the source root is several directories above them. Git itself is the only trustworthy
interpreter of Git ignore semantics, so uploads ask ``git ls-files --cached --others --exclude-standard`` for the
tracked and non-ignored working tree. Fwd's configured exclusions and ``.fwdignore`` are then applied through an
isolated temporary Git repository, ensuring they also remove tracked paths. The real ``.git/`` directory is appended
explicitly because remote coding sessions need repository history for diff, blame, and commits.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fwd.config import SyncConfig

FWDIGNORE_NAME = ".fwdignore"


def _git_worktree_root(source: Path) -> bool:
    """Return whether ``source`` is a standalone Git worktree root whose metadata directory can be synchronized."""
    if not (source / ".git").is_dir():
        return False
    proc = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == source.resolve()
    except OSError:
        return False


def _custom_ignore_text(source: Path, sync_cfg: SyncConfig) -> str:
    """Combine project rules with configured excludes ordered so the latter cannot be negated by ``.fwdignore``."""
    patterns: list[str] = []
    fwdignore = source / FWDIGNORE_NAME
    if fwdignore.is_file():
        patterns.extend(fwdignore.read_text(encoding="utf-8").splitlines())
    # Git ignore rules use last-match-wins, whereas rsync's filter list used first-match-wins. Putting configured
    # exclusions last preserves their prior precedence over any negated pattern in .fwdignore.
    patterns.extend(sync_cfg.exclude)
    return "".join(f"{pattern}\n" for pattern in patterns if pattern)


def _git_candidates(source: Path) -> list[bytes]:
    """Return tracked plus untracked/non-ignored paths and every nested ``.gitignore`` as NUL-safe bytes.

    A ``.gitignore`` is allowed to ignore itself (the Convex local-state layout does exactly this). Including those
    files explicitly lets the remote GNU rsync commit preserve ignored remote-only state on later mirrored pushes.
    """
    proc = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git could not enumerate the upload selection (exit {proc.returncode})" + (f": {detail}" if detail else ""))
    candidates = [path for path in proc.stdout.split(b"\0") if path]
    nested_ignore_files = [
        path
        for path in _git_ignored_paths(source)
        if path == b".gitignore" or path.endswith(b"/.gitignore")
    ]
    return list(dict.fromkeys([*candidates, *nested_ignore_files]))


def _git_ignored_paths(source: Path) -> list[bytes]:
    """Return Git-ignored paths, allowing directory entries to collapse large ignored subtrees."""
    ignored_proc = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
        check=False,
        capture_output=True,
    )
    if ignored_proc.returncode != 0:
        detail = ignored_proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git could not enumerate ignored paths (exit {ignored_proc.returncode})" + (f": {detail}" if detail else ""))
    return [path for path in ignored_proc.stdout.split(b"\0") if path]


def git_ignored_patterns(source: str | Path, sync_cfg: SyncConfig) -> str:
    """Return Git-ignored paths as tar exclusion patterns, or an empty string outside a supported Git root."""
    root = Path(source).expanduser().resolve()
    if not sync_cfg.use_gitignore or not _git_worktree_root(root):
        return ""
    return "".join(f"{os.fsdecode(path)}\n" for path in _git_ignored_paths(root))


def _custom_ignored_paths(candidates: list[bytes], ignore_file: Path, filter_repo: Path) -> set[bytes]:
    """Use Git's wildmatch implementation to apply fwd exclusions to tracked and untracked candidates alike."""
    filter_repo.mkdir()
    subprocess.run(["git", "-C", str(filter_repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(filter_repo), "config", "core.excludesFile", str(ignore_file)], check=True)
    proc = subprocess.run(
        ["git", "-C", str(filter_repo), "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"\0".join(candidates) + (b"\0" if candidates else b""),
        check=False,
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        detail = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git could not apply fwd upload exclusions (exit {proc.returncode})" + (f": {detail}" if detail else ""))
    return {path for path in proc.stdout.split(b"\0") if path}


@contextmanager
def upload_manifest(source: str | Path, sync_cfg: SyncConfig) -> Iterator[Path | None]:
    """Yield a NUL-delimited upload manifest, or ``None`` when Git-based selection is unavailable or disabled.

    The temporary manifest remains alive for the caller's transfer. Non-Git directories retain the existing rsync/tar
    filter fallback, while standalone Git roots get exact nested-ignore behavior on every local rsync implementation.
    """
    root = Path(source).expanduser().resolve()
    if not sync_cfg.use_gitignore or not _git_worktree_root(root):
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="fwd-upload-selection-") as temporary:
        temp_root = Path(temporary)
        ignore_file = temp_root / "fwdignore"
        ignore_file.write_text(_custom_ignore_text(root, sync_cfg), encoding="utf-8")
        candidates = _git_candidates(root)
        ignored = _custom_ignored_paths(candidates, ignore_file, temp_root / "filter-repo")
        manifest = temp_root / "manifest"
        with manifest.open("wb") as stream:
            for path in candidates:
                if path not in ignored:
                    stream.write(path + b"\0")
            stream.write(b".git/\0")
        yield manifest


def rsync_manifest_args(manifest: Path) -> list[str]:
    """Return rsync arguments that consume a NUL-delimited manifest and recurse explicit directory entries."""
    return ["--recursive", "--from0", f"--files-from={manifest}"]
