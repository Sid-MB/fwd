"""Authoritative local upload manifests for Git working trees.

Rsync's per-directory merge support is not consistent across implementations: macOS openrsync can miss deeply nested
``.gitignore`` files when the source root is several directories above them. Git itself is the only trustworthy
interpreter of Git ignore semantics, so uploads ask ``git ls-files --cached --others --exclude-standard`` for the
working tree and apply the repository rules once more with ``--no-index`` so even tracked paths matching an ignore
rule stay outside the sync domain. Fwd's configured exclusions and ``.fwdignore`` are then applied through an isolated
temporary Git repository. The real ``.git/`` directory is appended explicitly because remote coding sessions need
repository history for diff, blame, and commits.
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


def custom_ignore_patterns(
    source: Path,
    sync_cfg: SyncConfig,
    extra_excludes: tuple[str, ...] = (),
    *,
    include_fwdignore: bool = True,
) -> list[str]:
    """Combine project rules with configured and caller-owned exclusions in increasing precedence order.

    Returned as a list rather than a blob so continuous sync can hand the same layered patterns to Mutagen, which
    takes one ``--ignore`` argument per rule. One-shot transfers keep rendering it back into gitignore-file text.
    """
    patterns: list[str] = []
    fwdignore = source / FWDIGNORE_NAME
    if include_fwdignore and fwdignore.is_file():
        patterns.extend(fwdignore.read_text(encoding="utf-8").splitlines())
    # Git ignore rules use last-match-wins, whereas rsync's filter list used first-match-wins. Putting configured
    # exclusions last preserves their prior precedence over any negated pattern in .fwdignore.
    patterns.extend(sync_cfg.exclude)
    patterns.extend(extra_excludes)
    return [pattern for pattern in patterns if pattern]


def _custom_ignore_text(
    source: Path,
    sync_cfg: SyncConfig,
    extra_excludes: tuple[str, ...] = (),
    *,
    include_fwdignore: bool = True,
) -> str:
    """Render :func:`custom_ignore_patterns` as the gitignore-format file body Git's matcher consumes."""
    return "".join(f"{pattern}\n" for pattern in custom_ignore_patterns(source, sync_cfg, extra_excludes, include_fwdignore=include_fwdignore))


def _git_candidates(source: Path, *, include_ignored_rule_files: bool = True) -> list[bytes]:
    """Return tracked plus untracked/non-ignored paths and every nested ``.gitignore`` as NUL-safe bytes.

    A ``.gitignore`` is allowed to ignore itself (the Convex local-state layout does exactly this). Including those
    files explicitly lets the remote GNU rsync commit preserve ignored remote-only state on later mirrored pushes.
    Read-only comparisons disable this upload-specific exception so a self-ignored rule file remains ignored.
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
    return list(dict.fromkeys([*candidates, *(ignored_rule_files(source) if include_ignored_rule_files else [])]))


def ignored_rule_files(source: Path) -> list[bytes]:
    """Return the ``.gitignore`` files a repository's own rules hide from ``--exclude-standard``.

    Factored out of :func:`_git_candidates` because continuous sync needs exactly the same set: Mutagen's ignore list
    is built from the rule files fwd can enumerate, so a self-ignored nested ``.gitignore`` that the upload path
    honours must not be invisible to the continuous path.
    """
    return [path for path in _git_ignored_paths(source) if path == b".gitignore" or path.endswith(b"/.gitignore")]


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


def _git_ignored_candidates(source: Path, candidates: list[bytes]) -> set[bytes]:
    """Return candidate paths matching repository ignore rules even when Git already tracks those paths.

    ``git ls-files --cached`` normally retains tracked paths regardless of ignore rules. Fwd deliberately applies
    ``--no-index`` so an accidentally tracked credential or generated artifact still remains outside synchronization.
    """
    proc = subprocess.run(
        ["git", "-C", str(source), "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"\0".join(candidates) + (b"\0" if candidates else b""),
        check=False,
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        detail = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git could not apply repository ignore rules to the comparison selection (exit {proc.returncode})" + (f": {detail}" if detail else ""))
    return {path for path in proc.stdout.split(b"\0") if path}


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


def filtered_candidates(
    source: str | Path,
    candidates: list[bytes],
    sync_cfg: SyncConfig,
    *,
    apply_gitignore: bool,
    include_fwdignore: bool = True,
    extra_excludes: tuple[str, ...] = (),
) -> list[bytes]:
    """Filter arbitrary local or remote candidate names through the local project's comparison policy.

    Remote-only names are intentionally evaluated against local rules: the local checkout is the source of truth for
    what the next push would synchronize, and a stale or missing remote `.gitignore` must not disclose ignored files.
    """
    root = Path(source).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="fwd-candidate-selection-") as temporary:
        temp_root = Path(temporary)
        ignore_file = temp_root / "fwdignore"
        ignore_file.write_text(_custom_ignore_text(root, sync_cfg, extra_excludes, include_fwdignore=include_fwdignore), encoding="utf-8")
        ignored = _custom_ignored_paths(candidates, ignore_file, temp_root / "filter-repo")
        if apply_gitignore:
            ignored.update(_git_ignored_candidates(root, candidates))
    return [path for path in candidates if path not in ignored]


@contextmanager
def upload_manifest(
    source: str | Path,
    sync_cfg: SyncConfig,
    *,
    include_git_metadata: bool = True,
    include_ignored_rule_files: bool = True,
    exclude_ignored_tracked: bool = True,
    extra_excludes: tuple[str, ...] = (),
) -> Iterator[Path | None]:
    """Yield a NUL-delimited upload manifest, or ``None`` when Git-based selection is unavailable or disabled.

    The temporary manifest remains alive for the caller's transfer. Non-Git directories retain the existing rsync/tar
    filter fallback, while standalone Git roots get exact nested-ignore behavior on every local rsync implementation.

    Args:
        include_git_metadata: Append ``.git/`` for upload callers; comparisons disable it because repository databases
            are runtime state rather than project content.
        include_ignored_rule_files: Re-add self-ignored nested ``.gitignore`` files for uploads; comparisons disable
            the exception so their visible domain matches ordinary Git ignore semantics.
        exclude_ignored_tracked: Apply repository ignore rules to tracked candidates too. Enabled by default so upload
            and comparison domains agree and an accidentally tracked ignored credential is not synchronized.
        extra_excludes: Caller-owned patterns applied after `.fwdignore` and configured exclusions.
    """
    root = Path(source).expanduser().resolve()
    if not sync_cfg.use_gitignore or not _git_worktree_root(root):
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="fwd-upload-selection-") as temporary:
        temp_root = Path(temporary)
        candidates = _git_candidates(root, include_ignored_rule_files=include_ignored_rule_files)
        selected = filtered_candidates(
            root,
            candidates,
            sync_cfg,
            apply_gitignore=False,
            extra_excludes=extra_excludes,
        )
        if exclude_ignored_tracked:
            repository_ignored = _git_ignored_candidates(root, selected)
            selected = [
                path
                for path in selected
                if path not in repository_ignored
                or (include_ignored_rule_files and (path == b".gitignore" or path.endswith(b"/.gitignore")))
            ]
        manifest = temp_root / "manifest"
        with manifest.open("wb") as stream:
            for path in selected:
                stream.write(path + b"\0")
            if include_git_metadata:
                stream.write(b".git/\0")
        yield manifest


def rsync_manifest_args(manifest: Path) -> list[str]:
    """Return rsync arguments that consume a NUL-delimited manifest and recurse explicit directory entries."""
    return ["--recursive", "--from0", f"--files-from={manifest}"]
