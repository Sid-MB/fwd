"""Session state persistence — ``~/.fwd/state.json``.

Design intent
-------------
fwd is a short-lived CLI: each invocation is a fresh process that must rediscover what the previous one created.
State is therefore the memory of the tool, and it must satisfy four constraints:

1. **Lookup by cwd.** The bare ``fwd`` command means "attach to the session for this directory", so we index sessions
   by their resolved local cwd as well as by name.
2. **Crash safety.** A launch that dies midway must never leave an unparseable state file, since that would brick
   every later invocation. All writes go to a temp file in the same directory and land via atomic ``os.replace``.
3. **Concurrency.** Two terminals can run ``fwd`` at once. Every mutation is a read-modify-write under an exclusive
   ``fcntl.flock`` so one process cannot clobber the other's session entry.
4. **Forgiveness.** A missing or corrupt file degrades to empty state rather than raising: losing session bookkeeping
   is annoying, but a hard crash on every command is worse. Backends can always re-resolve live resources.

The stored endpoint is a plain dict rather than an :class:`~fwd.sshexec.SSHEndpoint` so the file stays valid JSON and
tolerates added fields; :func:`endpoint_from_dict` filters unknown keys on the way back in.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from fwd.sshexec import SSHEndpoint

STATE_PATH = Path.home() / ".fwd" / "state.json"
STATE_VERSION = 1


def _now() -> str:
    """Return a UTC ISO-8601 timestamp; all state timestamps use this single format."""
    return datetime.now(UTC).isoformat()


def endpoint_to_dict(ep: SSHEndpoint) -> dict[str, Any]:
    """Serialize an :class:`~fwd.sshexec.SSHEndpoint` to JSON primitives for storage."""
    return {
        "host": ep.host,
        "user": ep.user,
        "port": ep.port,
        "key_path": ep.key_path,
        "proxy_jump": ep.proxy_jump,
        "supports_rsync": ep.supports_rsync,
        "extra_opts": list(ep.extra_opts),
    }


def endpoint_from_dict(d: dict[str, Any]) -> SSHEndpoint:
    """Rebuild an :class:`~fwd.sshexec.SSHEndpoint`, ignoring keys written by a newer fwd version."""
    known = {f.name for f in fields(SSHEndpoint)}
    kwargs = {k: v for k, v in d.items() if k in known}
    kwargs.setdefault("host", "")
    kwargs.setdefault("user", "")
    return SSHEndpoint(**kwargs)


@dataclass(slots=True)
class SessionState:
    """One live (or believed-live) remote session.

    ``backend_ids`` is intentionally an open dict rather than typed fields: RunPod needs ``pod_id``, Slurm needs
    ``job_id`` plus the pinned ``login_host``, and the ssh backend needs nothing. Keeping it loose means adding a
    backend never requires a state-schema migration.

    ``flags`` records the launch-time choices (``session``/``handoff``/``user_config``/``creds``) so a later
    ``fwd attach`` after a pod restart can reproduce the same environment without the user retyping flags.
    """

    name: str
    backend: str
    local_cwd: str
    remote_dir: str
    tmux_session: str
    endpoint: dict[str, Any]
    backend_ids: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    last_attached: str | None = None
    flags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of this session."""
        return {
            "name": self.name,
            "backend": self.backend,
            "local_cwd": self.local_cwd,
            "remote_dir": self.remote_dir,
            "tmux_session": self.tmux_session,
            "endpoint": dict(self.endpoint),
            "backend_ids": dict(self.backend_ids),
            "created_at": self.created_at,
            "last_attached": self.last_attached,
            "flags": dict(self.flags),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionState:
        """Rebuild a session from stored JSON, ignoring unknown keys and defaulting missing ones."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        for required in ("name", "backend", "local_cwd", "remote_dir", "tmux_session"):
            kwargs.setdefault(required, "")
        kwargs.setdefault("endpoint", {})
        return cls(**kwargs)

    def ssh_endpoint(self) -> SSHEndpoint:
        """Return the stored endpoint as an :class:`~fwd.sshexec.SSHEndpoint`.

        Callers that may face a re-provisioned target (RunPod IP churn) should prefer the backend's
        ``endpoint(session)`` re-resolution instead of trusting this cached value.
        """
        return endpoint_from_dict(self.endpoint)

    def touch_attached(self) -> None:
        """Stamp ``last_attached`` to now; caller is responsible for persisting via :meth:`StateStore.upsert`."""
        self.last_attached = _now()


class StateStore:
    """Locked, atomic accessor for the sessions file.

    Every public method opens the file, locks it, and closes it. There is no long-lived handle and no in-memory
    cache: a CLI process is short enough that re-reading a few KB is free, and a cache would only create staleness
    bugs when two terminals operate on the same session.
    """

    def __init__(self, path: Path = STATE_PATH) -> None:
        """Args:
        path: Location of the state file. Overridden in tests to keep them off the real ``~/.fwd``.
        """
        self.path = Path(path)

    def _ensure_parent(self) -> None:
        """Create ``~/.fwd`` if needed; 0700 because the directory also holds ControlMaster sockets."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _decode(self, raw: str) -> dict[str, dict[str, Any]]:
        """Parse the document and return the sessions mapping, tolerating corruption and legacy shapes."""
        if not raw.strip():
            return {}
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Corrupt state must not brick the CLI; treat as empty and let backends re-resolve live resources.
            return {}
        if not isinstance(doc, dict):
            return {}
        sessions = doc.get("sessions", {})
        return sessions if isinstance(sessions, dict) else {}

    def _write(self, sessions: dict[str, dict[str, Any]]) -> None:
        """Atomically replace the state file with the given sessions mapping."""
        doc = {"version": STATE_VERSION, "sessions": sessions}
        self._ensure_parent()
        # Temp file must share the destination directory so os.replace stays within one filesystem and is atomic.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[dict[str, dict[str, Any]]]:
        """Yield the sessions mapping while holding an flock on the state file.

        The lock is taken on the state file itself (opened ``a+b`` so it is created if absent) and released when the
        handle closes. Mutating callers use ``exclusive=True`` and must call :meth:`_write` before the block exits,
        which is safe because the write's ``os.replace`` happens while we still hold the lock on the old inode.
        """
        self._ensure_parent()
        handle: BinaryIO
        with open(self.path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                handle.seek(0)
                raw = handle.read().decode("utf-8", errors="replace")
                yield self._decode(raw)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def all(self) -> list[SessionState]:
        """Return every known session, newest ``created_at`` first."""
        with self._locked(exclusive=False) as sessions:
            items = [SessionState.from_dict(v) for v in sessions.values()]
        return sorted(items, key=lambda s: s.created_at, reverse=True)

    def get(self, name: str) -> SessionState | None:
        """Return the session with this name, or ``None``."""
        with self._locked(exclusive=False) as sessions:
            raw = sessions.get(name)
        return SessionState.from_dict(raw) if raw else None

    def get_for_cwd(self, cwd: str | Path) -> SessionState | None:
        """Return the most recently created session launched from this directory, or ``None``.

        Paths are resolved on both sides so symlinked or relative invocations still match. This powers the bare
        ``fwd`` smart default.
        """
        target = str(Path(cwd).expanduser().resolve())
        matches = [s for s in self.all() if str(Path(s.local_cwd).expanduser().resolve()) == target]
        return matches[0] if matches else None

    def upsert(self, session: SessionState) -> None:
        """Insert or replace a session by name."""
        with self._locked(exclusive=True) as sessions:
            sessions[session.name] = session.to_dict()
            self._write(sessions)

    def remove(self, name: str) -> bool:
        """Delete a session by name.

        Returns:
            ``True`` if a session was removed, ``False`` if the name was unknown.
        """
        with self._locked(exclusive=True) as sessions:
            if name not in sessions:
                return False
            del sessions[name]
            self._write(sessions)
        return True

    def update(self, name: str, **updates: Any) -> SessionState | None:
        """Patch individual fields of a stored session under one lock.

        Preferred over read-then-:meth:`upsert` for small edits (e.g. a new endpoint after a pod restart) because it
        cannot lose a concurrent writer's changes to other sessions.

        Returns:
            The updated session, or ``None`` if the name was unknown.
        """
        with self._locked(exclusive=True) as sessions:
            raw = sessions.get(name)
            if raw is None:
                return None
            session = SessionState.from_dict(raw)
            for key, value in updates.items():
                if hasattr(session, key):
                    setattr(session, key, value)
            sessions[name] = session.to_dict()
            self._write(sessions)
        return session
