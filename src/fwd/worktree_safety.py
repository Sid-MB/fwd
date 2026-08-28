"""Remote Git worktree safety checks for lifecycle actions that can discard VM-local files.

Stopping a disposable VM can be just as destructive as deleting it: container disks and home directories may be
recreated from an image on the next boot. Fwd therefore checks the project directory at the last responsible moment
and refuses lifecycle actions when Git reports tracked or untracked changes. The caller may bypass the guard only
with an explicit force flag.

The check intentionally treats an unreachable remote as unsafe. Continuing would turn "SSH is temporarily broken"
into silent data loss; users who have independently verified the machine can still make the explicit ``--force``
choice. Directories that are not Git worktrees pass because Git has no state for fwd to protect.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from fwd import ui
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState

DIRTY_EXIT = 42
CHECK_FAILED_EXIT = 43

#: Lockfiles that never count as uncommitted work. Each is fully regenerable from its manifest (``pyproject.toml``,
#: ``package.json``, ``Cargo.toml``, ...) by the tool that wrote it, so discarding a VM whose only change is a lockfile
#: loses no authored work -- while blocking on them would make the guard fire constantly, since routine installs and
#: syncs rewrite them.
IGNORED_LOCKFILES = (
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "flake.lock",
)


def ignored_lockfile_pattern() -> str:
    """Return the ERE matching porcelain-v1 lines whose path is only an ignored lockfile.

    Porcelain v1 lines are ``XY path``, so the filename is preceded either by the status separator space (top-level
    paths) or by a slash (paths in subdirectories); anchoring it at end-of-line therefore matches nested lockfiles
    while never matching a path that merely ends in a lockfile-like suffix (``my-uv.lock``). Shared by both shell
    fragments below so the interactive check and the server-side stop-after guard can never disagree.

    Known edge case: porcelain quotes paths containing special characters (``"weird path/uv.lock"``); the trailing
    quote defeats the ``$`` anchor, so such a lockfile still counts as dirty. That is the safe direction to fail.
    """
    names = "|".join(name.replace(".", r"\.") for name in IGNORED_LOCKFILES)
    return f"(^|[/ ])({names})$"


@dataclass(frozen=True, slots=True)
class WorktreeCheck:
    """Result of inspecting one remote project directory."""

    dirty: bool
    summary: str = ""


def _lockfile_filter_fragment() -> str:
    """Return the shell snippet that drops ignored-lockfile lines from ``$git_status``.

    Only run when ``$git_status`` is non-empty, because ``printf '%s\\n' ""`` would otherwise feed ``grep`` a blank
    line and make a clean worktree look dirty. ``grep -v`` exits 1 when it filters everything out, so ``|| true``
    keeps that expected case from reading as a command failure.
    """
    return (
        f"if [ -n \"$git_status\" ]; then git_status=$(printf '%s\\n' \"$git_status\" "
        f"| grep -vE '{ignored_lockfile_pattern()}' || true); fi"
    )


def _remote_check_command(remote_dir: str) -> str:
    """Return a POSIX-shell check whose special exit codes distinguish dirty state from inspection failure."""
    directory = shlex.quote(remote_dir)
    return (
        f"if ! command -v git >/dev/null 2>&1 || ! git -C {directory} rev-parse --is-inside-work-tree >/dev/null 2>&1; then exit 0; fi; "
        f"git_status=$(git -C {directory} status --porcelain=v1 --untracked-files=all 2>&1) || {{ printf '%s\\n' \"$git_status\"; exit {CHECK_FAILED_EXIT}; }}; "
        f"{_lockfile_filter_fragment()}; "
        f"if [ -n \"$git_status\" ]; then printf '%s\\n' \"$git_status\"; exit {DIRTY_EXIT}; fi"
    )


def inspect(endpoint: SSHEndpoint, remote_dir: str) -> WorktreeCheck:
    """Inspect a remote worktree, raising when the remote cannot prove that Git state is safe."""
    try:
        result = endpoint.run(_remote_check_command(remote_dir), check=False)
    except Exception as exc:
        raise RuntimeError(f"could not inspect the remote Git worktree: {exc}") from exc
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        return WorktreeCheck(dirty=False)
    if result.returncode == DIRTY_EXIT:
        lines = output.splitlines()
        visible = lines[:8]
        suffix = f"\n... and {len(lines) - len(visible)} more path(s)" if len(lines) > len(visible) else ""
        return WorktreeCheck(dirty=True, summary="\n".join(visible) + suffix)
    detail = output or f"remote check exited {result.returncode}"
    raise RuntimeError(f"could not inspect the remote Git worktree: {detail}")


def require_clean(endpoint: SSHEndpoint, session: SessionState, *, force: bool, action: str) -> None:
    """Refuse ``action`` unless the remote worktree is clean or the user explicitly forced it."""
    if force:
        return
    try:
        result = inspect(endpoint, session.remote_dir)
    except RuntimeError as exc:
        ui.die(f"{exc}. Refusing to {action} because VM-local work may be lost; retry when SSH works or pass --force.")
    if result.dirty:
        ui.die(
            f"refusing to {action}: remote Git worktree {session.remote_dir!r} has uncommitted changes:\n"
            f"{result.summary}\nCommit, stash, or pull the work back first; pass --force only to accept losing it."
        )


def shell_guard(remote_dir: str) -> str:
    """Return the Bash fragment used by server-owned stop-after immediately before shutdown."""
    directory = shlex.quote(remote_dir)
    return f"""worktree_label={directory}
if [ "$force_stop" -ne 1 ] && command -v git >/dev/null 2>&1 && git -C {directory} rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_status="$(git -C {directory} status --porcelain=v1 --untracked-files=all 2>&1)"
    git_rc=$?
    if [ "$git_rc" -ne 0 ]; then
        printf "stop-after blocked: could not inspect the Git worktree at %s; run stopafter --force only if losing VM-local work is acceptable\\n%s\\n" "$worktree_label" "$git_status" >&2
        exit {CHECK_FAILED_EXIT}
    fi
    {_lockfile_filter_fragment()}
    if [ -n "$git_status" ]; then
        printf "stop-after blocked: the Git worktree at %s has uncommitted changes\\n%s\\nCommit, stash, or pull the work back first; run stopafter --force only to accept losing it.\\n" "$worktree_label" "$git_status" >&2
        exit {DIRTY_EXIT}
    fi
fi"""
