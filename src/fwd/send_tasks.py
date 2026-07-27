"""Persistent local metadata for durable ``fwd send`` tasks.

Every send operation has two halves: a remote tmux window owns the actual process, while this small local registry
remembers how later CLI invocations can find it. Keeping task state separate from session state lets the session
schema remain focused on provisioned resources and lets old completed tasks be pruned without touching lifecycle
bookkeeping.

The registry is intentionally descriptive rather than authoritative. A task marked ``running`` may have completed
while fwd was not running; callers refresh it from the remote exit marker before rendering or acting on it.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

TASKS_PATH = Path.home() / ".fwd" / "tasks.json"
TASKS_VERSION = 1


def now() -> str:
    """Return the canonical UTC timestamp used by task records."""
    return datetime.now(UTC).isoformat()


def new_task_id(kind: str) -> str:
    """Return a readable, collision-resistant task identifier."""
    prefix = "agt" if kind == "agent" else "cmd"
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class SendTask:
    """One command or agent turn running on an existing fwd session."""

    id: str
    session: str
    kind: str
    command: list[str]
    label: str
    status: str = "running"
    agent: str | None = None
    created_at: str = ""
    finished_at: str | None = None
    exit_code: int | None = None
    depends_on: str | None = None

    def __post_init__(self) -> None:
        """Fill timestamps for newly constructed tasks while preserving deserialized values."""
        if not self.created_at:
            self.created_at = now()

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record to JSON primitives."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SendTask:
        """Read a record written by this or a newer fwd, ignoring unknown fields."""
        known = {field.name for field in fields(cls)}
        kwargs = {key: item for key, item in value.items() if key in known}
        for required, default in (("id", ""), ("session", ""), ("kind", "command"), ("command", []), ("label", "")):
            kwargs.setdefault(required, default)
        return cls(**kwargs)

    @property
    def active(self) -> bool:
        """Return whether the remote task may still be doing work."""
        return self.status in {"queued", "running", "unknown"}


class SendTaskStore:
    """Atomic, process-safe accessor for ``~/.fwd/tasks.json``."""

    def __init__(self, path: Path = TASKS_PATH) -> None:
        self.path = Path(path)

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _decode(self, raw: str) -> dict[str, dict[str, Any]]:
        if not raw.strip():
            return {}
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        tasks = document.get("tasks", {}) if isinstance(document, dict) else {}
        return tasks if isinstance(tasks, dict) else {}

    def _write(self, tasks: dict[str, dict[str, Any]]) -> None:
        self._ensure_parent()
        fd, temporary = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tasks-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": TASKS_VERSION, "tasks": tasks}, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[dict[str, dict[str, Any]]]:
        self._ensure_parent()
        handle: BinaryIO
        with open(self.path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                handle.seek(0)
                yield self._decode(handle.read().decode("utf-8", errors="replace"))
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def all(self) -> list[SendTask]:
        """Return every task, newest first."""
        with self._locked(exclusive=False) as tasks:
            records = [SendTask.from_dict(value) for value in tasks.values()]
        return sorted(records, key=lambda task: task.created_at, reverse=True)

    def get(self, task_id: str) -> SendTask | None:
        """Return one exact task."""
        with self._locked(exclusive=False) as tasks:
            value = tasks.get(task_id)
        return SendTask.from_dict(value) if value else None

    def upsert(self, task: SendTask) -> None:
        """Insert or replace a task."""
        with self._locked(exclusive=True) as tasks:
            tasks[task.id] = task.to_dict()
            self._write(tasks)

    def update(self, task_id: str, **updates: Any) -> SendTask | None:
        """Patch a task without losing concurrent updates to other records."""
        with self._locked(exclusive=True) as tasks:
            value = tasks.get(task_id)
            if value is None:
                return None
            task = SendTask.from_dict(value)
            for key, item in updates.items():
                if hasattr(task, key):
                    setattr(task, key, item)
            tasks[task_id] = task.to_dict()
            self._write(tasks)
        return task

    def cancel_session(self, session_name: str) -> int:
        """Mark every active task for a stopped fwd session as canceled."""
        changed = 0
        with self._locked(exclusive=True) as tasks:
            for task_id, value in list(tasks.items()):
                task = SendTask.from_dict(value)
                if task.session != session_name or not task.active:
                    continue
                task.status = "canceled"
                task.exit_code = 130
                task.finished_at = now()
                tasks[task_id] = task.to_dict()
                changed += 1
            if changed:
                self._write(tasks)
        return changed
