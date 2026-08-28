"""Remote worktree guard tests: the generated shell fragment is executed for real against temporary Git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fwd import worktree_safety


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Return a committed one-file Git repo so later writes are the only dirtiness under test."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _check(repo: Path) -> subprocess.CompletedProcess[str]:
    command = worktree_safety._remote_check_command(str(repo))
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True)


def test_clean_worktree_passes(repo: Path) -> None:
    assert _check(repo).returncode == 0


def test_lockfile_only_change_is_clean(repo: Path) -> None:
    """A regenerable lockfile is not authored work, so it must not block lifecycle actions."""
    (repo / "uv.lock").write_text("version = 2\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "package-lock.json").write_text("{}\n")
    result = _check(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_change_alongside_lockfile_reports_only_the_real_file(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 2\n")
    (repo / "app.py").write_text("print('bye')\n")
    result = _check(repo)
    assert result.returncode == worktree_safety.DIRTY_EXIT
    assert "app.py" in result.stdout
    assert "uv.lock" not in result.stdout


def test_untracked_file_is_dirty(repo: Path) -> None:
    (repo / "notes.txt").write_text("scratch\n")
    assert _check(repo).returncode == worktree_safety.DIRTY_EXIT


def _guard(repo: Path) -> subprocess.CompletedProcess[str]:
    script = "force_stop=0\n" + worktree_safety.shell_guard(str(repo)) + "\nexit 0"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_shell_guard_matches_the_check_command_on_lockfiles(repo: Path) -> None:
    """Server-side stop-after must ignore the same lockfiles the interactive guard does."""
    (repo / "uv.lock").write_text("version = 2\n")
    assert _guard(repo).returncode == 0
    (repo / "app.py").write_text("print('bye')\n")
    blocked = _guard(repo)
    assert blocked.returncode == worktree_safety.DIRTY_EXIT
    assert "app.py" in blocked.stderr
    assert "uv.lock" not in blocked.stderr


def test_non_git_directory_passes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _check(plain).returncode == 0
