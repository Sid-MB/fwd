"""One-time interactive offer to install fwd's Typer-generated shell completion.

Package installers intentionally do not mutate shell startup files. The first human invocation is the earliest point
where fwd knows both that a user is present and which shell is active, so this module offers Typer's supported
installer once and records the decision independently of ordinary configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from typer import completion

from fwd import ui

COMPLETION_PROMPT_PATH = Path.home() / ".fwd" / "completion-prompted"


def _shell_name() -> str | None:
    """Infer the active shell name from ``$SHELL`` without spawning a subprocess."""
    value = os.environ.get("SHELL", "").strip()
    if not value:
        return None
    name = Path(value).name.lower()
    return name if name in {"bash", "zsh", "fish", "powershell", "pwsh"} else None


def _completion_path(shell: str) -> Path | None:
    """Return Typer's standard completion-script location when it is predictable without installation."""
    paths = {
        "bash": Path.home() / ".bash_completions" / "fwd.sh",
        "zsh": Path.home() / ".zfunc" / "_fwd",
        "fish": Path.home() / ".config" / "fish" / "completions" / "fwd.fish",
    }
    return paths.get(shell)


def _record(outcome: str) -> None:
    """Persist the one-time decision best-effort; an unwritable home must never block the requested fwd command."""
    try:
        COMPLETION_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        COMPLETION_PROMPT_PATH.write_text(outcome + "\n", encoding="utf-8")
    except OSError as exc:
        ui.warn(f"could not remember the shell-completion choice ({exc})")


def offer_once() -> None:
    """Offer shell completion once, installing through Typer when accepted.

    Unsupported or undetectable shells are left unmarked so a later invocation from a supported shell can still ask.
    Installation failures are reported with the existing manual command and left unmarked so the user can retry.
    """
    if COMPLETION_PROMPT_PATH.exists():
        return
    shell = _shell_name()
    if shell is None:
        return
    installed_path = _completion_path(shell)
    if installed_path is not None and installed_path.is_file():
        _record(f"already-installed:{shell}")
        return
    if not ui.confirm(f"Install fwd shell completion for {shell}? This may update your shell startup file.", default=True):
        _record(f"declined:{shell}")
        return
    try:
        installed_shell, path = completion.install(shell=shell, prog_name="fwd", complete_var="_FWD_COMPLETE")
    except (OSError, RuntimeError, typer.Exit) as exc:
        ui.warn(f"could not install shell completion ({exc}); retry with 'fwd --install-completion'")
        return
    _record(f"installed:{installed_shell}")
    ui.ok(f"installed {installed_shell} completion at {path}; restart your shell to enable it")
