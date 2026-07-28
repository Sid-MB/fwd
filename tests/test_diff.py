"""Content and CLI contracts for ``fwd diff``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fwd.backends.base import TargetStatus
from fwd.ops import diff as diff_ops
from fwd.state import SessionState, StateStore


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _session(name: str, target: str, backend: str, created: str, *, attached: str | None = None) -> SessionState:
    return SessionState(
        name=name,
        backend=backend,
        local_cwd="/tmp/project",
        remote_dir="/remote/project",
        tmux_session=f"fwd-{name}",
        endpoint={"host": "example", "user": ""},
        created_at=created,
        last_attached=attached,
        flags={"target": target},
    )


def test_compare_returns_zero_for_identical_snapshots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local, remote = tmp_path / "local", tmp_path / "remote"
    _write(local, "src/app.py", "print('same')\n")
    _write(remote, "src/app.py", "print('same')\n")

    assert diff_ops._compare(local, remote, None) == 0
    assert capsys.readouterr().out == ""


def test_compare_returns_one_and_emits_stable_unified_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local, remote = tmp_path / "local", tmp_path / "remote"
    _write(local, "src/app.py", "local\n")
    _write(remote, "src/app.py", "remote\n")

    assert diff_ops._compare(local, remote, None) == 1
    output = capsys.readouterr().out
    assert "local/src/app.py" in output
    assert "remote/src/app.py" in output
    assert "-local" in output and "+remote" in output


def test_compare_path_scopes_out_unrelated_differences_and_quiet_suppresses_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local, remote = tmp_path / "local", tmp_path / "remote"
    _write(local, "same.txt", "same\n")
    _write(remote, "same.txt", "same\n")
    _write(local, "other.txt", "local\n")
    _write(remote, "other.txt", "remote\n")

    assert diff_ops._compare(local, remote, Path("same.txt")) == 0
    assert diff_ops._compare(local, remote, Path("other.txt"), quiet=True) == 1
    assert capsys.readouterr().out == ""


def test_compare_reports_one_sided_file_as_a_difference(tmp_path: Path) -> None:
    local, remote = tmp_path / "local", tmp_path / "remote"
    local.mkdir()
    _write(remote, "new.txt", "remote only\n")
    assert diff_ops._compare(local, remote, Path("new.txt"), quiet=True) == 1


@pytest.mark.parametrize("value", ("/etc/passwd", "../secret", "src/../../secret"))
def test_diff_path_rejects_project_escape(value: str) -> None:
    with pytest.raises(Exception):
        diff_ops._relative_path(value)


def test_diff_selector_resolves_target_then_backend_to_sole_active_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.json")
    store.upsert(_session("old", "work", "ssh", "2026-01-01T00:00:00+00:00"))
    store.upsert(_session("new", "work", "ssh", "2026-01-02T00:00:00+00:00", attached="2026-01-03T00:00:00+00:00"))
    monkeypatch.setattr(diff_ops.launch_ops, "store", lambda: store)
    monkeypatch.setattr(
        diff_ops.launch_ops,
        "_selection_status",
        lambda session: TargetStatus.RUNNING if session.name == "new" else TargetStatus.STOPPED,
    )

    assert diff_ops.resolve_session("old").name == "old"
    assert diff_ops.resolve_session("work").name == "new"
    assert diff_ops.resolve_session("ssh").name == "new"


@pytest.mark.parametrize("operation_code", (0, 1))
def test_diff_cli_preserves_standard_exit_codes(operation_code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd.cli import app

    monkeypatch.setattr(diff_ops, "diff", lambda *args, **kwargs: operation_code)
    result = CliRunner().invoke(app, ["diff"])
    assert result.exit_code == operation_code


def test_diff_cli_maps_operational_failures_above_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwd.cli import app

    monkeypatch.setattr(diff_ops, "diff", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network failed")))
    result = CliRunner().invoke(app, ["diff"])
    assert result.exit_code == 2
