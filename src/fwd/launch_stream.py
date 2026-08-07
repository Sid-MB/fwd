"""Foreground viewer and Ctrl-B detachment for the local ``fwd up`` launch pipeline.

Remote command tasks can detach cheaply because tmux already owns their work. Provisioning is local orchestration over provider APIs, SSH, and file transfer, so returning the terminal must leave a local worker alive. This module forks that worker before launch begins, captures its complete terminal output in a private log, and lets the parent act only as a viewer. Ctrl-B exits the viewer without signaling the worker; Ctrl-C signals the worker process group so :mod:`fwd.ops.launch` retains ownership of its existing interrupt cleanup and newly created provider resources are still canceled safely.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from fwd import task_stream, ui
from fwd.output import is_machine_environment

if TYPE_CHECKING:
    from fwd.state import SessionState

LAUNCH_HINT_DELAY = 2.0
LOG_DIRECTORY = Path.home() / ".fwd" / "logs"


@dataclass(frozen=True, slots=True)
class LaunchStreamResult:
    """Outcome returned to the foreground CLI after monitoring a local launch worker."""

    disposition: str
    exit_code: int
    session_name: str | None
    log_path: Path


def available() -> bool:
    """Return whether this process can offer Unix fork-based interactive launch detachment."""
    return hasattr(os, "fork") and sys.stdin.isatty() and sys.stderr.isatty() and not is_machine_environment()


def _write_status(path: Path, **fields: object) -> None:
    """Atomically persist minimal worker status beside its log for post-detachment diagnosis."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(fields, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _child(worker: Callable[[], SessionState], log_path: Path, status_path: Path, terminal_width: int) -> None:
    """Detach from the invoking terminal, run launch with terminal-quality log rendering, and exit without parent cleanup handlers."""
    os.setsid()
    input_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND)
    os.dup2(input_fd, sys.stdin.fileno())
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(input_fd)
    os.close(log_fd)
    ui.console = Console(file=sys.stdout, force_terminal=True, width=terminal_width)
    ui.err_console = Console(file=sys.stderr, force_terminal=True, width=terminal_width)
    _write_status(status_path, status="running", pid=os.getpid(), log=str(log_path))
    code = 1
    try:
        state = worker()
        _write_status(status_path, status="ready", pid=os.getpid(), session=state.name, exit_code=0, log=str(log_path))
        code = 0
    except KeyboardInterrupt:
        ui.err_console.print("Aborted.")
        _write_status(status_path, status="canceled", pid=os.getpid(), exit_code=130, log=str(log_path))
        code = 130
    except typer.Abort:
        ui.err_console.print("Aborted.")
        _write_status(status_path, status="canceled", pid=os.getpid(), exit_code=1, log=str(log_path))
        code = 1
    except typer.Exit as exc:
        code = int(exc.exit_code or 0)
        _write_status(status_path, status="failed" if code else "complete", pid=os.getpid(), exit_code=code, log=str(log_path))
    except BaseException:
        traceback.print_exc()
        _write_status(status_path, status="failed", pid=os.getpid(), exit_code=1, log=str(log_path))
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)


def _drain(log_path: Path, position: int) -> int:
    """Replay newly appended launch-log bytes to the foreground terminal and return the new offset."""
    with log_path.open("rb", buffering=0) as log:
        log.seek(position)
        data = log.read()
    if data:
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    return position + len(data)


def _status_session(status_path: Path) -> str | None:
    """Read the completed session name without making a malformed diagnostic file fatal."""
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session = payload.get("session") if isinstance(payload, dict) else None
    return str(session) if session else None


def run(worker: Callable[[], SessionState]) -> LaunchStreamResult:
    """Run one launch in a detached worker while streaming it until completion, Ctrl-C, or Ctrl-B."""
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(LOG_DIRECTORY, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIRECTORY / f"launch-{stamp}-{os.getpid()}.log"
    status_path = LOG_DIRECTORY / f"launch-{stamp}-{os.getpid()}.json"
    log_path.touch(mode=0o600)
    os.chmod(log_path, 0o600)
    terminal_width = shutil.get_terminal_size((100, 24)).columns
    pid = os.fork()
    if pid == 0:
        _child(worker, log_path, status_path, terminal_width)
    position = 0
    started = time.monotonic()
    hint_printed = False
    disposition = "completed"
    wait_status: int | None = None
    try:
        with task_stream.control_keys() as input_fd:
            while True:
                position = _drain(log_path, position)
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    wait_status = status
                    break
                elapsed = time.monotonic() - started
                if input_fd is not None and not hint_printed and elapsed >= LAUNCH_HINT_DELAY:
                    ui.err_console.print("[dim](Press Ctrl-C to cancel, Ctrl-B to background)[/]")
                    hint_printed = True
                if input_fd is not None:
                    readable, _, _ = select.select([input_fd], [], [], 0.1)
                    if readable:
                        data = os.read(input_fd, 64)
                        if b"\x03" in data:
                            disposition = "canceled"
                            try:
                                os.killpg(pid, signal.SIGINT)
                            except ProcessLookupError:
                                try:
                                    os.kill(pid, signal.SIGINT)
                                except ProcessLookupError:
                                    pass
                            _, wait_status = os.waitpid(pid, 0)
                            break
                        if b"\x02" in data:
                            disposition = "backgrounded"
                            break
                else:
                    time.sleep(0.1)
    finally:
        position = _drain(log_path, position)
    if disposition == "backgrounded":
        return LaunchStreamResult(disposition, 0, None, log_path)
    assert wait_status is not None
    exit_code = os.waitstatus_to_exitcode(wait_status)
    return LaunchStreamResult(disposition, exit_code, _status_session(status_path), log_path)
