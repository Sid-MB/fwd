#!/usr/bin/env python3
"""Send one message to an existing Codex TUI pane and stream its persisted turn events.

The Codex TUI owns the live thread. Starting ``codex exec resume --last`` beside it creates a second client that can
wait on the active rollout lock, select an unrelated recent thread, or exit without output when the TUI has not yet
persisted a turn. This helper instead submits input through the exact tmux pane and follows that TUI's rollout file.
Its stdout is Codex-exec-compatible JSONL so fwd's existing human and machine stream renderers remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DISCOVERY_TIMEOUT_SECONDS = 30
READY_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.05
INPUT_SETTLE_SECONDS = 0.15
ACTIVE_TARGET: str | None = None


def _parser() -> argparse.ArgumentParser:
    """Build the documented command-line interface used by fwd's remote task manager."""
    parser = argparse.ArgumentParser(description="Send a message to a live Codex tmux pane and stream the response as JSONL.")
    parser.add_argument("--tmux-session", required=True, help="Exact fwd tmux session name whose first pane runs the Codex TUI.")
    parser.add_argument("--cwd", required=True, help="Remote project directory used to identify the TUI's persisted Codex rollout.")
    parser.add_argument("message", help="Complete user message to submit to the existing Codex conversation.")
    return parser


def _emit(value: dict[str, Any]) -> None:
    """Write one immediately visible Codex-compatible JSONL event."""
    print(json.dumps(value, separators=(",", ":")), flush=True)


def _session_timestamp(meta: dict[str, Any]) -> float:
    """Return a sortable session creation timestamp, treating malformed metadata as oldest."""
    value = meta.get("timestamp")
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _process_started(pane_pid: int) -> float:
    """Return the Linux process start epoch used to reject rollouts from an earlier TUI instance."""
    try:
        fields = Path(f"/proc/{pane_pid}/stat").read_text(encoding="utf-8").split()
        started_ticks = int(fields[21])
        boot_line = next(line for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines() if line.startswith("btime "))
        boot_epoch = int(boot_line.split()[1])
        return boot_epoch + started_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return 0


def _rollout_for(codex_home: Path, cwd: str, *, not_before: float) -> Path | None:
    """Find the newest non-exec Codex session created by the current TUI process."""
    matches: list[tuple[float, Path]] = []
    for path in codex_home.glob("sessions/*/*/*/rollout-*.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline())
        except (OSError, json.JSONDecodeError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(payload, dict) or payload.get("cwd") != cwd:
            continue
        # A former fwd send may have created a newer codex_exec rollout beside the actual TUI. Those automation
        # sessions are never the pane we just addressed, so exclude them instead of letting timestamp order steal
        # the live response stream.
        if payload.get("source") == "exec" or payload.get("originator") == "codex_exec":
            continue
        timestamp = _session_timestamp(payload)
        if timestamp + 2 < not_before:
            continue
        matches.append((timestamp, path))
    return max(matches, default=(0, None), key=lambda item: item[0])[1]


def _pane_pid(target: str) -> int:
    """Return the live TUI pane's process id or fail with an actionable error."""
    result = subprocess.run(["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"], text=True, capture_output=True)
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def _codex_home(pane_pid: int) -> Path:
    """Read CODEX_HOME from the TUI process, falling back to this task's equivalent environment."""
    try:
        entries = Path(f"/proc/{pane_pid}/environ").read_bytes().split(b"\0")
    except OSError:
        entries = []
    for entry in entries:
        if entry.startswith(b"CODEX_HOME="):
            return Path(entry.split(b"=", 1)[1].decode(errors="surrogateescape"))
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _submit(target: str, message: str) -> None:
    """Type literal input and Codex's carriage-return submit key into the existing pane."""
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", message], check=True)
    # Ratatui can receive the literal text and submit key in one terminal read. A short boundary lets it commit the
    # paste into the composer before C-m, avoiding a dropped submit that leaves the message visibly unsubmitted.
    time.sleep(INPUT_SETTLE_SECONDS)
    subprocess.run(["tmux", "send-keys", "-t", target, "C-m"], check=True)


def _wait_until_ready(target: str) -> bool:
    """Wait for Codex's composer to render so startup keystrokes cannot be lost or interpreted by an earlier screen."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(["tmux", "capture-pane", "-p", "-t", target], text=True, capture_output=True)
        if result.returncode != 0:
            return False
        if "Context " in result.stdout and "›" in result.stdout:
            return True
        time.sleep(POLL_SECONDS)
    return False


def _cancel_active_turn(signum: int, frame: object) -> None:
    """Interrupt the bridged Codex turn when the durable helper window is canceled."""
    del signum, frame
    if ACTIVE_TARGET is not None and _pane_pid(ACTIVE_TARGET):
        subprocess.run(["tmux", "send-keys", "-t", ACTIVE_TARGET, "C-c"], check=False)
    raise SystemExit(130)


def _event_message(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the event kind and payload from one rollout record."""
    if record.get("type") != "event_msg":
        return "", {}
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return "", {}
    return str(payload.get("type", "")), payload


def _follow(path: Path, offset: int, message: str, target: str) -> int:
    """Follow appended rollout records through the matching user event and task completion."""
    saw_user = False
    last_agent_message = ""
    with path.open(encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                if _pane_pid(target) == 0:
                    _emit({"type": "error", "message": "the Codex TUI pane exited before the turn completed"})
                    return 1
                time.sleep(POLL_SECONDS)
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type, payload = _event_message(record)
            if not saw_user:
                if event_type == "user_message" and payload.get("message") == message:
                    saw_user = True
                    _emit({"type": "turn.started"})
                continue
            if event_type == "agent_message":
                text = payload.get("message")
                if isinstance(text, str) and text and text != last_agent_message:
                    last_agent_message = text
                    _emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
            elif event_type == "task_complete":
                final = payload.get("last_agent_message")
                if isinstance(final, str) and final and final != last_agent_message:
                    _emit({"type": "item.completed", "item": {"type": "agent_message", "text": final}})
                _emit({"type": "turn.completed"})
                return 0
            elif event_type in {"turn_aborted", "stream_error", "error"}:
                _emit({"type": "turn.failed", "message": str(payload.get("message") or payload.get("error") or event_type)})
                return 1


def main() -> int:
    """Submit the requested message and stream the exact live TUI turn."""
    global ACTIVE_TARGET
    args = _parser().parse_args()
    target = f"={args.tmux_session}:0.0"
    pane_pid = _pane_pid(target)
    if pane_pid == 0:
        _emit({"type": "error", "message": f"Codex tmux pane {args.tmux_session!r} is not running"})
        return 1
    if not _wait_until_ready(target):
        _emit({"type": "error", "message": f"Codex tmux pane {args.tmux_session!r} did not become ready within {READY_TIMEOUT_SECONDS}s"})
        return 1
    codex_home = _codex_home(pane_pid)
    pane_started = _process_started(pane_pid)
    rollout = _rollout_for(codex_home, args.cwd, not_before=pane_started)
    offset = rollout.stat().st_size if rollout is not None else 0
    for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, _cancel_active_turn)
    try:
        _submit(target, args.message)
        ACTIVE_TARGET = target
    except subprocess.CalledProcessError as exc:
        _emit({"type": "error", "message": f"could not submit input to the Codex TUI: {exc}"})
        return 1
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    while rollout is None and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        rollout = _rollout_for(codex_home, args.cwd, not_before=pane_started)
    if rollout is None:
        _emit({"type": "error", "message": f"Codex did not create a rollout for {args.cwd!r} within {DISCOVERY_TIMEOUT_SECONDS}s"})
        return 1
    result = _follow(rollout, offset, args.message, target)
    ACTIVE_TARGET = None
    return result


if __name__ == "__main__":
    raise SystemExit(main())
