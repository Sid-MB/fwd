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

import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def _tty() -> bool:
    """Return whether stderr is an interactive terminal (gates spinner animation)."""
    return err_console.is_terminal


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
            err_console.print(f"x {safe} failed after {time.monotonic() - started:.1f}s")
            raise
    err_console.print(f"[bold green]✓[/] {safe} [dim]{time.monotonic() - started:.1f}s[/]")


def info(message: str) -> None:
    """Print a neutral status line to stderr."""
    err_console.print(f"[dim]·[/] {escape(message)}")


def ok(message: str) -> None:
    """Print a success line to stderr."""
    err_console.print(f"[bold green]✓[/] {escape(message)}")


def warn(message: str) -> None:
    """Print a warning to stderr. Used for degraded-but-working situations (rsync fallback, ignored config keys)."""
    err_console.print(f"[bold yellow]![/] {escape(message)}")


def error(message: str) -> None:
    """Print an error to stderr without exiting."""
    err_console.print(f"[bold red]x[/] {escape(message)}")


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


def table(title: str, columns: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    """Render a table to stdout (it is data, not chrome).

    Args:
        title: Table caption.
        columns: Header labels.
        rows: Row values; each row is rendered with ``str()`` applied per cell.
    """
    tbl = Table(title=title, title_justify="left", header_style="bold")
    for column in columns:
        tbl.add_column(column)
    for row in rows:
        tbl.add_row(*[str(cell) for cell in row])
    console.print(tbl)


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Ask for yes/no confirmation.

    Returns ``default`` immediately when stdin is not interactive, so scripted use of destructive commands (``fwd rm``)
    does not hang waiting on input that will never arrive.
    """
    if not console.is_terminal:
        return default
    return typer.confirm(prompt, default=default)
