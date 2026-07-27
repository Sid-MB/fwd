"""Interactive viewer for durable remote task logs.

The viewer owns terminal mode only while it is active. Ctrl-C cancels the remote tmux task through
:mod:`fwd.remote_tasks`; Ctrl-B terminates only the SSH follower. Agent JSONL stays untouched for pipes and agent
callers, while interactive humans see concise prose and tool events.
"""

from __future__ import annotations

import json
import os
import selectors
import sys
import termios
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from fwd import remote_tasks, ui
from fwd.output import is_machine_environment
from fwd.send_tasks import SendTask
from fwd.sshexec import SSHEndpoint

CONTROL_HINT_DELAY = 2.0


@dataclass(frozen=True, slots=True)
class StreamResult:
    """How a task-viewing invocation ended."""

    disposition: str
    exit_code: int


class AgentOutput:
    """Convert agent JSONL into concise terminal output without weakening machine-readable streams."""

    def __init__(self, task: SendTask) -> None:
        self.task = task
        self.buffer = b""
        self.human = task.kind == "agent" and sys.stdout.isatty() and not is_machine_environment()
        self.printed_claude_text = False

    def feed(self, data: bytes, destination: object) -> None:
        """Write complete lines, retaining an incomplete JSONL tail for the next read."""
        if not self.human:
            destination.write(data)
            destination.flush()
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._write_rendered(line.decode("utf-8", errors="replace"), destination)

    def finish(self, destination: object) -> None:
        """Flush a final unterminated line."""
        if self.buffer:
            self._write_rendered(self.buffer.decode("utf-8", errors="replace"), destination)
            self.buffer = b""

    def _write_rendered(self, line: str, destination: object) -> None:
        rendered = self._render(line)
        if rendered:
            destination.write((rendered + "\n").encode())
            destination.flush()

    def _render(self, line: str) -> str:
        if not line.strip():
            return ""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        return self._codex(event) if self.task.agent == "codex" else self._claude(event)

    @staticmethod
    def _codex(event: dict[str, object]) -> str:
        event_type = str(event.get("type", ""))
        item = event.get("item")
        item = item if isinstance(item, dict) else {}
        item_type = str(item.get("type", ""))
        if event_type == "turn.started":
            return "Working…"
        if event_type == "item.started":
            if item_type == "command_execution":
                return f"→ {item.get('command', 'command')}"
            if item_type in {"mcp_tool_call", "web_search", "file_change"}:
                return f"→ {item_type.replace('_', ' ')}"
        if event_type == "item.completed" and item_type == "agent_message":
            return str(item.get("text", ""))
        if event_type in {"turn.failed", "error"}:
            return f"error: {event.get('message') or event.get('error') or 'agent turn failed'}"
        return ""

    def _claude(self, event: dict[str, object]) -> str:
        event_type = str(event.get("type", ""))
        if event_type == "result":
            return "" if self.printed_claude_text else str(event.get("result", ""))
        if event_type != "assistant":
            return ""
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        rendered: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text", ""))
                if text:
                    self.printed_claude_text = True
                    rendered.append(text)
            elif block.get("type") == "tool_use":
                rendered.append(f"→ {block.get('name', 'tool')}")
        return "\n".join(rendered)


@contextmanager
def control_keys() -> Iterator[int | None]:
    """Put interactive stdin in a mode where fwd can distinguish Ctrl-C from Ctrl-B."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        yield None
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    changed = termios.tcgetattr(fd)
    changed[3] &= ~termios.ISIG
    termios.tcsetattr(fd, termios.TCSADRAIN, changed)
    try:
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def terminate_viewer(process: object) -> None:
    """Stop only the local SSH follower and reap it."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except Exception:
        process.kill()
        process.wait()


def stream(task: SendTask, endpoint: SSHEndpoint, *, timeout: float | None = None) -> StreamResult:
    """Stream a task until completion, cancellation, backgrounding, or timeout."""
    process = remote_tasks.follow_process(endpoint, task)
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
    if process.stderr is not None:
        selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
    started = time.monotonic()
    agent_output = AgentOutput(task)
    hint_printed = False
    disposition = "completed"
    try:
        with control_keys() as input_fd:
            if input_fd is not None:
                selector.register(input_fd, selectors.EVENT_READ, None)
            while True:
                elapsed = time.monotonic() - started
                if input_fd is not None and not hint_printed and elapsed >= CONTROL_HINT_DELAY and process.poll() is None:
                    ui.err_console.print("[dim](Press Ctrl-C to cancel, Ctrl-B to background)[/]")
                    hint_printed = True
                if timeout is not None and elapsed >= timeout and process.poll() is None:
                    remote_tasks.stop(endpoint, task)
                    disposition = "canceled"
                    ui.warn(f"task {task.id} exceeded {timeout:g}s and was canceled")
                    terminate_viewer(process)
                    break
                for key, _ in selector.select(timeout=0.1):
                    if key.data is None:
                        data = os.read(input_fd, 64)
                        if b"\x03" in data:
                            remote_tasks.stop(endpoint, task)
                            disposition = "canceled"
                            terminate_viewer(process)
                            break
                        if b"\x02" in data:
                            disposition = "backgrounded"
                            terminate_viewer(process)
                            break
                    else:
                        data = os.read(key.fileobj.fileno(), 65536)
                        if data:
                            if key.data is sys.stdout.buffer:
                                agent_output.feed(data, key.data)
                            else:
                                key.data.write(data)
                                key.data.flush()
                        else:
                            selector.unregister(key.fileobj)
                if disposition != "completed":
                    break
                if process.poll() is not None and (not selector.get_map() or all(key.data is None for key in selector.get_map().values())):
                    break
    except KeyboardInterrupt:
        remote_tasks.stop(endpoint, task)
        disposition = "canceled"
        terminate_viewer(process)
    finally:
        if process.stdout is not None:
            agent_output.finish(sys.stdout.buffer)
        selector.close()
    if disposition == "backgrounded":
        return StreamResult(disposition, 0)
    if disposition == "canceled":
        return StreamResult(disposition, 130)
    return StreamResult(disposition, process.wait())
