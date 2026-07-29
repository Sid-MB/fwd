"""Terminal output helpers — the only module allowed to print.

Design intent
-------------
Two consoles, deliberately. ``console`` writes stdout and is reserved for *data* the user might pipe or capture
(``fwd ls`` tables, resolved paths). ``err_console`` writes stderr and carries all progress chrome: spinners, step
marks, warnings, errors. That split means ``fwd ls | grep foo`` keeps working while spinners still animate.

:func:`step` is the general workhorse, while :func:`transfer_step` adds byte and throughput reporting for sync. A
launch is a sequence of slow, failure-prone stages (provision, wait for ssh, sync,
bootstrap, deps, Claude state, tmux) and users need to know both which stage is running and which one broke. The
context manager shows a live spinner, then replaces it with a persistent one-line result including elapsed time, so a
completed launch reads as a checklist. On exception it marks the step failed and re-raises — it never swallows errors.

Spinners are suppressed when stderr is not a tty so CI logs and piped output stay clean.

Every message is passed through :func:`rich.markup.escape` before interpolation. Messages are *data* — they routinely
carry config snippets like ``[targets.pod]``, rsync filters like ``--filter=':- .gitignore'`` and Slurm specs — and Rich
would silently parse a bracketed literal as a style tag and delete it. That is not hypothetical: ``fwd`` with no config
used to advise "run 'fwd setup' or add  to ~/.fwd/config.toml", having swallowed the one thing the user needed to see.
The decorating markup here is a fixed prefix written by this module, so escaping the caller's string costs nothing and
no call site needs to remember to do it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import NoReturn

import typer
from rich.console import Console
from rich.filesize import decimal
from rich.markup import escape
from rich.progress import BarColumn, Progress, ProgressColumn, Task, TextColumn, TransferSpeedColumn
from rich.text import Text

from fwd.output import OutputFormat, RecordElement, TableElement, is_machine_environment, render

console = Console()
err_console = Console(stderr=True)

# The executable's displayed name belongs here rather than in individual prompts, errors, and help examples. Alternate
# builds can configure it before process startup without touching internal compatibility paths such as ``~/.fwd``.
COMMAND_NAME = os.environ.get("FWD_COMMAND_NAME", "fwd")


def _tty() -> bool:
    """Return whether stderr is an interactive terminal (gates spinner animation)."""
    return err_console.is_terminal


def interactive_terminal() -> bool:
    """Return whether a human terminal may receive prompts, attachment, and optional discovery hints."""
    return sys.stdin.isatty() and console.is_terminal and not is_machine_environment()


@contextmanager
def step(message: str, *, quiet: bool = False) -> Iterator[None]:
    """Run a block as a labelled progress step.

    Args:
        message: Present-tense label, e.g. ``"Syncing 1.2 MB to remote"``.
        quiet: Skip all output; used when a caller renders its own progress.

    Yields:
        ``None``. Exceptions propagate after the step is marked failed.
    """
    if quiet:
        yield
        return

    started = time.monotonic()
    safe = escape(message)
    if _tty():
        with err_console.status(f"[bold cyan]{safe}[/]", spinner="dots"):
            try:
                yield
            except BaseException:
                err_console.print(f"[bold red]x[/] {safe} [dim]failed after {time.monotonic() - started:.1f}s[/]")
                raise
    else:
        try:
            yield
        except BaseException:
            err_console.print(f"error: {safe} failed after {time.monotonic() - started:.1f}s")
            raise
    elapsed = time.monotonic() - started
    if _tty():
        err_console.print(f"[bold green]✓[/] {safe} [dim]{elapsed:.1f}s[/]")
    else:
        err_console.print(f"ok: {safe} ({elapsed:.1f}s)")


class _TransferredColumn(ProgressColumn):
    """Render cumulative streamed bytes without implying that fwd knows the upload's final size."""

    def render(self, task: Task) -> Text:
        """Format the task's cumulative byte count using Rich's decimal MB/GB units."""
        return Text(decimal(int(task.completed)), style="cyan")


class _TransferProgress(Progress):
    """Place recent transfer paths on dedicated left-aligned lines below the normal progress table."""

    def get_renderables(self) -> Iterable[object]:
        """Yield the progress table followed by markup-free path lines from the task's immutable snapshot."""
        tasks = self.tasks
        yield self.make_tasks_table(tasks)
        if not tasks:
            return
        paths = tasks[0].fields.get("paths", ())
        if not paths:
            return
        rendered = Text()
        for index, path in enumerate(paths):
            if index:
                rendered.append("\n")
            rendered.append("  ↳ ", style="dim")
            rendered.append(str(path), style="dim")
        yield rendered


class TransferDisplay:
    """Thread-safe transfer callbacks for cumulative bytes and a rolling path window."""

    def __init__(self, update_bytes: Callable[[int], None], update_path: Callable[[str], None]) -> None:
        self._update_bytes = update_bytes
        self._update_path = update_path

    def __call__(self, transferred_bytes: int) -> None:
        """Update cumulative compressed wire bytes."""
        self._update_bytes(transferred_bytes)

    def path(self, path: str) -> None:
        """Update the recent-path display or emit a durable non-interactive log entry."""
        self._update_path(path)


@contextmanager
def transfer_step(message: str, *, show_bytes: bool = True) -> Iterator[TransferDisplay]:
    """Render transfer progress plus five transient recent paths, then persist one compact result line.

    Fwd deliberately enforces its upload limit while streaming instead of scanning the tree first, so no trustworthy
    total exists while the transfer is active. The bar therefore pulses while its adjacent columns report cumulative
    wire bytes and current throughput. Path callbacks may arrive from a drain thread, so a lock protects the rolling
    window. Redirected output retains every path as a normal stderr line because transient terminal rendering is
    unavailable and complete automation logs are more useful than an arbitrary sample.
    """
    started = time.monotonic()
    safe = escape(message)
    completed = 0

    if _tty():
        columns: list[ProgressColumn | str] = [
            TextColumn("[bold cyan]{task.description}[/]"),
            BarColumn(bar_width=None, pulse_style="cyan"),
        ]
        if show_bytes:
            columns.extend((_TransferredColumn(), TransferSpeedColumn()))
        progress = _TransferProgress(
            *columns,
            console=err_console,
            transient=True,
        )
        recent_paths: deque[str] = deque(maxlen=5)
        path_lock = threading.Lock()
        with progress:
            task_id = progress.add_task(safe, total=None, paths=())

            def update_bytes(transferred_bytes: int) -> None:
                nonlocal completed
                completed = max(completed, transferred_bytes)
                progress.update(task_id, completed=completed)

            def update_path(path: str) -> None:
                with path_lock:
                    recent_paths.append(path)
                    snapshot = tuple(recent_paths)
                progress.update(task_id, paths=snapshot)

            try:
                yield TransferDisplay(update_bytes, update_path)
            except BaseException:
                elapsed = time.monotonic() - started
                detail = f" · {decimal(completed)}" if show_bytes else ""
                err_console.print(f"[bold red]x[/] {safe} [dim]failed after {elapsed:.1f}s{detail}[/]")
                raise
    else:

        def update_bytes(transferred_bytes: int) -> None:
            nonlocal completed
            completed = max(completed, transferred_bytes)

        try:
            yield TransferDisplay(update_bytes, transfer_path)
        except BaseException:
            elapsed = time.monotonic() - started
            detail = f" ({decimal(completed)} transferred)" if show_bytes else ""
            err_console.print(f"error: {safe} failed after {elapsed:.1f}s{detail}")
            raise

    elapsed = time.monotonic() - started
    average_speed = completed / elapsed if elapsed > 0 else 0
    if _tty() and show_bytes:
        err_console.print(f"[bold green]✓[/] {safe} [dim]{decimal(completed)} · {decimal(int(average_speed))}/s · {elapsed:.1f}s[/]")
    elif _tty():
        err_console.print(f"[bold green]✓[/] {safe} [dim]{elapsed:.1f}s[/]")
    elif show_bytes:
        err_console.print(f"ok: {safe} ({decimal(completed)}, {decimal(int(average_speed))}/s, {elapsed:.1f}s)")
    else:
        err_console.print(f"ok: {safe} ({elapsed:.1f}s)")


def transfer_path(path: str) -> None:
    """Print one project-relative path reported by an active push or pull without contaminating data stdout."""
    safe = escape(path)
    err_console.print(f"[dim]  ↳[/] {safe}" if _tty() else f"sync: {safe}")


def info(message: str) -> None:
    """Print a neutral status line to stderr."""
    safe = escape(message)
    err_console.print(f"[dim]·[/] {safe}" if _tty() else f"info: {safe}")


def info_with_code(before: str, command_text: str, after: str = "") -> None:
    """Print an informational sentence containing one safely styled command without treating user text as Rich markup."""
    if not _tty():
        info(f"{before}{code(command_text)}{after}")
        return
    line = Text()
    line.append("· ", style="dim")
    line.append(before)
    line.append(command_text, style="bold cyan")
    line.append(after)
    err_console.print(line)


def ok(message: str) -> None:
    """Print a success line to stderr."""
    safe = escape(message)
    err_console.print(f"[bold green]✓[/] {safe}" if _tty() else f"ok: {safe}")


def warn(message: str) -> None:
    """Print a warning to stderr. Used for degraded-but-working situations (rsync fallback, ignored config keys)."""
    safe = escape(message)
    err_console.print(f"[bold yellow]![/] {safe}" if _tty() else f"warning: {safe}")


def error(message: str) -> None:
    """Print an error to stderr without exiting."""
    safe = escape(message)
    err_console.print(f"[bold red]x[/] {safe}" if _tty() else f"error: {safe}")


def die(message: str, *, code: int = 1) -> NoReturn:
    """Print an error and abort the CLI.

    Raises :class:`typer.Exit` rather than calling ``sys.exit`` so Typer unwinds cleanly and tests can assert on the
    exit code via ``CliRunner``.
    """
    error(message)
    raise typer.Exit(code)


def raw(text: str) -> None:
    """Write text to stdout verbatim: no wrapping, no markup, no highlighting.

    Rich's console hard-wraps to the terminal width (80 when stdout is a pipe), which silently corrupts any output that
    is *machine* input rather than prose — ``fwd config --example > ~/.fwd/config.toml`` produced broken TOML because
    long trailing comments were folded onto continuation lines that were no longer comments. Anything whose line
    structure is load-bearing must go through here instead of ``console.print``.
    """
    sys.stdout.write(text)


def table(title: str, columns: Sequence[str], rows: Iterable[Sequence[object]], *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """Render a table to stdout (it is data, not chrome).

    Args:
        title: Table caption.
        columns: Header labels.
        rows: Row values; each row is rendered with ``str()`` applied per cell.
    """
    element = TableElement(title, tuple(columns), tuple(tuple(row) for row in rows))
    render(element, output_format=output_format, console=console)
    # Some commands follow structured stdout with usage guidance on stderr. Flush the data first so a merged terminal
    # or subprocess log preserves the intended table-then-guidance order even when stdout is block-buffered.
    console.file.flush()


def record(title: str, fields: Sequence[tuple[str, object]], *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """Render a structured key/value record through the same renderer selection as tables."""
    render(RecordElement(title, tuple(fields)), output_format=output_format, console=console)


def command(arguments: str = "") -> str:
    """Return the configured executable name, optionally followed by a display-ready argument string.

    This helper deliberately does not shell-quote ``arguments`` because it is also used with documentation
    placeholders such as ``<name>``. Callers interpolating real untrusted argv should continue to use
    :func:`shlex.join` with :data:`COMMAND_NAME` as its first element.
    """
    return f"{COMMAND_NAME} {arguments}".rstrip()


def accent(text: str) -> str:
    """Return a bold cyan terminal label for short prompt anchors.

    Typer's prompts are implemented by Click rather than Rich, so Rich markup such as ``[bold cyan]`` would be shown
    literally. Typer's ANSI styling is understood by Click and is automatically stripped when color is unavailable.
    """
    return typer.style(text, fg=typer.colors.CYAN, bold=True)


def command_accent() -> str:
    """Return the configured command name as the purple brand anchor used in interactive prompts."""
    return typer.style(command(), fg=typer.colors.MAGENTA, bold=True)


def announce_alias(actual: str, *, invoked: str | None = None) -> None:
    """Print an alias expansion before the canonical command begins producing its own progress output."""
    info(f"{invoked or command()} → {actual}")


def code(text: str) -> str:
    """Render an inline code fragment for the active output mode.

    Interactive terminals get bold cyan text so commands remain visually distinct without punctuation noise.
    Redirected, agent, and CI output gets Markdown-compatible backtick fencing. The fence is always longer than any
    backtick run in the fragment, so shell snippets containing command substitution remain valid Markdown.
    """
    if _tty():
        return typer.style(text, fg=typer.colors.CYAN, bold=True)
    longest_run = current_run = 0
    for character in text:
        current_run = current_run + 1 if character == "`" else 0
        longest_run = max(longest_run, current_run)
    fence = "`" * (longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def code_examples(examples: Sequence[str], *, heading: str = "Useful commands:") -> str:
    """Format command hints as one compact line for direct printing or embedding in a post-process shell message."""
    return f"{heading} {' | '.join(code(command_text) for command_text in examples)}"


def show_code_examples(examples: Sequence[str], *, heading: str = "Useful commands:") -> None:
    """Print a compact single-line command reference to stderr without contaminating structured stdout.

    ``code_examples`` deliberately returns a plain string because the attach path embeds it in a local shell wrapper
    that runs only after SSH exits. In a terminal that string contains ANSI styling produced by :func:`code`, so it
    must be decoded into a Rich ``Text`` object instead of passed through the regular message escaper; treating an ANSI
    control sequence as Rich markup corrupts it. Non-interactive output disables markup and retains literal backticks.
    """
    rendered = code_examples(examples, heading=heading)
    err_console.print(Text.from_ansi(rendered) if _tty() else rendered, markup=False)


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Ask for yes/no confirmation.

    Returns ``default`` immediately when stdin is not interactive, so scripted use of destructive commands (``fwd rm``)
    does not hang waiting on input that will never arrive.
    """
    if not console.is_terminal:
        return default
    return typer.confirm(prompt, default=default)
