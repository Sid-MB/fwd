"""Remove fwd-owned local data while leaving final package-manager removal to the still-running CLI process.

The executable cannot reliably delete its own environment on every platform while it is running. This module removes
the data around that executable—state/config, installed coding-agent skill, completions, and temporary artifacts—then
prints package-manager-specific commands that the user can run after this process exits.

Remote resources are deliberately outside the uninstall boundary. Losing ``state.json`` while a pod or allocation is
still tracked would make billing resources harder to find, so uninstall refuses by default and points to ``fwd rm
--all``. ``--force`` is an explicit acknowledgement that only local bookkeeping will be removed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fwd import ui
from fwd.skill_setup import skills_environment
from fwd.state import STATE_PATH, StateStore

PACKAGE_NAME = "fwd"
REPOSITORY_URL = "https://github.com/Sid-MB/fwd"
PACKAGE_SPEC = f"git+{REPOSITORY_URL}"
ISSUES_URL = f"{REPOSITORY_URL}/issues"
TEMP_PREFIXES = ("fwd-skill-update-", "fwd-session-", "fwd-codex-", "fwd-cm-")
SKILLS_REMOVE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class InstallationAdvice:
    """Commands that finish CLI removal and make a later reinstall or one-off run straightforward."""

    manager: str
    uninstall: str
    reinstall: str
    temporary: str | None


def _command(*arguments: str) -> str:
    """Build a shell-safe example command for the current platform."""
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def installation_advice() -> InstallationAdvice:
    """Infer the active environment manager from ``sys.prefix`` and return safe follow-up commands."""
    prefix = Path(sys.prefix)
    uv = shutil.which("uv")
    pipx = shutil.which("pipx")
    if prefix.name == PACKAGE_NAME and prefix.parent.name == "tools":
        return InstallationAdvice(
            manager="uv",
            uninstall=_command("uv", "tool", "uninstall", PACKAGE_NAME),
            reinstall=_command("uv", "tool", "install", PACKAGE_SPEC),
            temporary=_command("uvx", "--from", PACKAGE_SPEC, PACKAGE_NAME),
        )
    if "pipx" in {part.lower() for part in prefix.parts}:
        return InstallationAdvice(
            manager="pipx",
            uninstall=_command("pipx", "uninstall", PACKAGE_NAME),
            reinstall=_command("pipx", "install", PACKAGE_SPEC),
            temporary=_command("pipx", "run", "--spec", PACKAGE_SPEC, PACKAGE_NAME),
        )
    python = sys.executable
    temporary = _command(shutil.which("uvx") or "uvx", "--from", PACKAGE_SPEC, PACKAGE_NAME) if uv else (_command(pipx, "run", "--spec", PACKAGE_SPEC, PACKAGE_NAME) if pipx else None)
    return InstallationAdvice(
        manager="pip",
        uninstall=_command(python, "-m", "pip", "uninstall", PACKAGE_NAME),
        reinstall=_command(python, "-m", "pip", "install", PACKAGE_SPEC),
        temporary=temporary,
    )


def _tracked_session_count() -> int:
    """Read existing state without creating ``~/.fwd`` merely to discover that it is absent."""
    if not STATE_PATH.exists():
        return 0
    try:
        return len(StateStore(STATE_PATH).all())
    except OSError as exc:
        ui.warn(f"could not inspect tracked sessions before uninstall ({exc})")
        return 0


def _completion_paths(home: Path) -> tuple[Path, ...]:
    """Return every standalone completion file Typer may have installed for fwd."""
    return (
        home / ".bash_completions" / f"{ui.COMMAND_NAME}.sh",
        home / ".zfunc" / f"_{ui.COMMAND_NAME}",
        home / ".config" / "fish" / "completions" / f"{ui.COMMAND_NAME}.fish",
    )


def _skill_paths(home: Path) -> tuple[Path, ...]:
    """Return canonical and compatibility skill locations used by Codex and Claude."""
    return (
        home / ".claude" / "skills" / ui.COMMAND_NAME,
        home / ".codex" / "skills" / ui.COMMAND_NAME,
        home / ".agents" / "skills" / ui.COMMAND_NAME,
    )


def _remove_skill_with_npx() -> bool:
    """Ask the skills CLI to remove fwd from every global agent, returning whether it completed successfully.

    Direct path cleanup still follows because older skills CLI releases can retain their shared canonical directory
    when agent-specific links exist. Running the owner CLI first lets it clean any metadata and additional agent links
    that fwd itself does not know about.
    """
    npx = shutil.which("npx")
    if not npx:
        return False
    command = [npx, "--yes", "skills", "remove", "--global", PACKAGE_NAME, "--agent", "*", "--yes"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=SKILLS_REMOVE_TIMEOUT_SECONDS, env=skills_environment(noninteractive=True))
    except (OSError, subprocess.TimeoutExpired) as exc:
        ui.warn(f"could not remove the coding-agent skill using npx skills ({exc}); cleaning its known paths directly")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        ui.warn(f"npx skills could not remove the coding-agent skill{suffix}; cleaning its known paths directly")
        return False
    ui.ok("removed the installed fwd coding-agent skill using npx skills")
    return True


def _remove_skill_lock_entry(home: Path) -> bool:
    """Remove only fwd's optional skills-CLI lock entry while preserving all other skills and preferences."""
    path = home / ".agents" / ".skill-lock.json"
    if not path.is_file():
        return False
    document = json.loads(path.read_text(encoding="utf-8"))
    skills = document.get("skills")
    if not isinstance(skills, dict) or skills.pop(ui.COMMAND_NAME, None) is None:
        return False
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def _temporary_paths() -> tuple[Path, ...]:
    """Discover only fwd-prefixed temporary artifacts, never arbitrary files in the shared temp directory."""
    root = Path(tempfile.gettempdir())
    return tuple(sorted({path for prefix in TEMP_PREFIXES for path in root.glob(f"{prefix}*")}))


def _remove_path(path: Path) -> bool:
    """Remove one exact file, symlink, or directory without following symlinks; return whether anything existed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _remove_bash_hook(home: Path) -> bool:
    """Remove Typer's exact fwd completion source line while preserving every unrelated Bash setting."""
    path = home / ".bashrc"
    if not path.is_file():
        return False
    completion_path = home / ".bash_completions" / f"{ui.COMMAND_NAME}.sh"
    source_line = f"source '{completion_path}'"
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    retained = [line for line in lines if line.strip() != source_line]
    if retained == lines:
        return False
    path.write_text("\n".join(retained).rstrip() + "\n", encoding="utf-8")
    return True


def _powershell_profiles(home: Path) -> tuple[Path, ...]:
    """Return standard per-user profile files used by Windows PowerShell and PowerShell Core."""
    return (
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
    )


def _remove_powershell_hooks(home: Path) -> int:
    """Remove only the exact Typer-generated fwd completion script from standard PowerShell profiles."""
    try:
        from typer._completion_shared import get_completion_script

        script = get_completion_script(prog_name=ui.COMMAND_NAME, complete_var=f"_{ui.COMMAND_NAME.upper()}_COMPLETE", shell="powershell")
    except Exception:
        return 0
    changed = 0
    for path in _powershell_profiles(home):
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = original.replace(script + "\n", "").replace(script, "")
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def uninstall(*, force: bool = False) -> int:
    """Remove local fwd artifacts and print the command that finishes uninstalling the active Python package.

    Args:
        force: Skip confirmation and allow deletion of local state even when remote sessions remain tracked.

    Returns:
        ``0`` after complete cleanup, or ``1`` when one or more exact paths could not be removed.
    """
    session_count = _tracked_session_count()
    if session_count and not force:
        noun = "session" if session_count == 1 else "sessions"
        verb = "is" if session_count == 1 else "are"
        ui.die(
            f"{session_count} remote {noun} {verb} still tracked. Uninstall will not stop or destroy remote resources. "
            f"Run {ui.command('rm --all')!r} first, or pass --force to remove local state and accept that those resources may remain running."
        )
    if session_count:
        ui.warn(f"removing local state for {session_count} tracked remote session(s); remote resources will not be stopped or destroyed")
    if not force:
        if not ui.interactive_terminal():
            ui.die("uninstall is destructive and cannot confirm in non-interactive mode; pass --force to remove local files")
        if not ui.confirm("Remove fwd config, state, coding-agent skill, completions, and temporary files?", default=False):
            ui.info("aborted")
            return 0

    home = Path.home()
    _remove_skill_with_npx()
    paths = (*_skill_paths(home), *_completion_paths(home), *_temporary_paths(), home / ".fwd")
    removed = 0
    failures: list[tuple[Path, str]] = []
    for path in paths:
        try:
            removed += int(_remove_path(path))
        except OSError as exc:
            failures.append((path, str(exc)))
    try:
        removed += int(_remove_bash_hook(home))
    except OSError as exc:
        failures.append((home / ".bashrc", str(exc)))
    try:
        removed += _remove_powershell_hooks(home)
    except OSError as exc:
        failures.append((home / "Documents", str(exc)))
    try:
        removed += int(_remove_skill_lock_entry(home))
    except (OSError, ValueError, TypeError) as exc:
        failures.append((home / ".agents" / ".skill-lock.json", str(exc)))

    if failures:
        for path, error in failures:
            ui.warn(f"could not remove {path}: {error}")
        ui.warn(f"removed {removed} local artifact(s), but {len(failures)} cleanup action(s) failed")
    else:
        ui.ok(f"removed {removed} local fwd artifact(s), including the coding-agent skill and temporary logs")

    advice = installation_advice()
    examples = [advice.uninstall, advice.reinstall]
    if advice.temporary:
        examples.append(advice.temporary)
    ui.show_code_examples(tuple(examples), heading="After this command exits:")
    ui.info(f"If you try fwd again, feedback and bug reports are welcome at {ISSUES_URL}")
    return 1 if failures else 0
