"""Terminal output helpers — the only module allowed to print.

Design intent
-------------
Two consoles, deliberately. ``console`` writes stdout and is reserved for *data* the user might pipe or capture
(``fwd ls`` tables, resolved paths). ``err_console`` writes stderr and carries all progress chrome: spinners, step
marks, warnings, errors. That split means ``fwd ls | grep foo`` keeps working while spinners still animate.

:func:`step` is the workhorse. A launch is a sequence of slow, failure-prone stages (provision, wait for ssh, sync,
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
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape
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


def table(title: str, columns: Sequence[str], rows: Iterable[Sequence[str]], *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
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


def code_examples(examples: Sequence[tuple[str, str]], *, heading: str = "Useful commands:") -> str:
    """Format labeled command examples for either direct printing or embedding in a post-process shell message."""
    lines = [heading]
    lines.extend(f"  {label}: {code(command_text)}" for label, command_text in examples)
    return "\n".join(lines)


def show_code_examples(examples: Sequence[tuple[str, str]], *, heading: str = "Useful commands:") -> None:
    """Print a compact command-reference block to stderr without contaminating structured stdout.

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
