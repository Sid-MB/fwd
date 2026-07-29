"""Byte-bounded duplex transport used as rsync's local remote-shell command.

Rsync owns a bidirectional protocol over SSH, so its ordinary stdout is status text rather than uploaded bytes. This
relay sits between rsync and SSH: a companion thread forwards and counts rsync-to-SSH bytes while the main thread
forwards SSH-to-rsync responses. Crossing the configured budget writes a local sentinel and terminates SSH, allowing
the parent fwd process to distinguish an intentional size cutoff from an ordinary rsync or connection failure.
"""

from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

CHUNK_SIZE = 1024 * 1024
LIMIT_EXIT_CODE = 99
PROGRESS_PREFIX = "__FWD_UPLOAD_PROGRESS__"


def _write_all(fd: int, content: bytes) -> None:
    """Write a complete protocol chunk without relying on buffered Python streams during thread shutdown."""
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _report_progress(sent_bytes: int) -> None:
    """Emit a machine-readable cumulative count for the parent fwd process; rsync does not expose wire progress."""
    print(f"{PROGRESS_PREFIX}{sent_bytes}", file=sys.stderr, flush=True)


def _copy_local_input(proc: subprocess.Popen[bytes], limit_bytes: int, sentinel: Path, exceeded: threading.Event, sent_bytes: list[int]) -> None:
    """Forward and count local rsync bytes until EOF, remote closure, or the circuit breaker trips."""
    assert proc.stdin is not None
    try:
        while True:
            try:
                chunk = os.read(sys.stdin.fileno(), CHUNK_SIZE)
            except BlockingIOError:
                if proc.poll() is not None:
                    return
                select.select([sys.stdin.fileno()], [], [], 0.1)
                continue
            if not chunk:
                return
            remaining = limit_bytes - sent_bytes[0]
            if len(chunk) > remaining:
                if remaining > 0:
                    _write_all(proc.stdin.fileno(), chunk[:remaining])
                    sent_bytes[0] += remaining
                    _report_progress(sent_bytes[0])
                sentinel.write_text(str(sent_bytes[0] + len(chunk) - max(remaining, 0)), encoding="utf-8")
                exceeded.set()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return
            _write_all(proc.stdin.fileno(), chunk)
            sent_bytes[0] += len(chunk)
            _report_progress(sent_bytes[0])
    except BrokenPipeError:
        return
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass


def relay(limit_bytes: int, sentinel: Path, ssh_argv: Sequence[str]) -> int:
    """Run ``ssh_argv`` and relay rsync's protocol, returning 99 after the outbound budget is crossed."""
    proc = subprocess.Popen(list(ssh_argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None
    assert proc.stdout is not None
    exceeded = threading.Event()
    sent_bytes = [0]
    input_thread = threading.Thread(target=_copy_local_input, args=(proc, limit_bytes, sentinel, exceeded, sent_bytes), daemon=True)
    input_thread.start()
    try:
        while chunk := proc.stdout.read1(CHUNK_SIZE):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        returncode = proc.wait()
    input_thread.join(timeout=0.1)
    return LIMIT_EXIT_CODE if exceeded.is_set() else returncode


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the fixed relay settings while preserving every following SSH argument verbatim."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--sentinel", type=Path, required=True)
    parser.add_argument("ssh_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    ssh_argv = args.ssh_argv[1:] if args.ssh_argv[:1] == ["--"] else args.ssh_argv
    if args.limit <= 0 or not ssh_argv:
        return 2
    return relay(args.limit, args.sentinel, ssh_argv)


if __name__ == "__main__":
    raise SystemExit(main())
