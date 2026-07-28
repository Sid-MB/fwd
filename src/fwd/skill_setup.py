"""Install the coding-agent skill bundled inside the local fwd Python package.

Python package installers should not mutate agent configuration, so the first human ``fwd`` invocation still asks
before running the open ``skills`` CLI. The source is never GitHub: fwd materializes the wheel/editable install's
``SKILL.md``, ``references/``, and ``agents/`` into ``~/.fwd/skill-source/fwd`` and gives that filesystem path to
``skills add``. The narrow staged tree matters because a directory containing a root ``SKILL.md`` is copied as one
skill; pointing at the Python package or repository root would accidentally install source code, tests, and caches.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

from fwd import ui

SKILL_NAME = ui.COMMAND_NAME
SKILL_PROMPT_PATH = Path.home() / ".fwd" / "skill-prompted"
LOCAL_SKILL_SOURCE = Path.home() / ".fwd" / "skill-source"
TARGET_AGENTS = ("codex", "claude-code")


def _payload_root() -> Path:
    """Return the directory containing fwd's canonical skill payload in wheels and editable checkouts."""
    package_dir = Path(__file__).resolve().parent
    return package_dir if (package_dir / "SKILL.md").is_file() else package_dir.parents[1]


def _payload_directory(payload_root: Path, name: str) -> Path:
    """Resolve one canonical skill directory in a repository checkout or its collision-free wheel location."""
    if name == "agents" and (payload_root / "skill_agents").is_dir():
        return payload_root / "skill_agents"
    return payload_root / name


def _materialize_skill_source() -> Path:
    """Copy only the bundled skill payload into a stable local source directory.

    Returns:
        The parent directory accepted by ``skills add``; it contains exactly one ``fwd/SKILL.md`` skill.
    """
    payload_root = _payload_root()
    target = LOCAL_SKILL_SOURCE / SKILL_NAME
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(payload_root / "SKILL.md", target / "SKILL.md")
    for directory in ("references", "agents"):
        source = _payload_directory(payload_root, directory)
        if source.is_dir():
            shutil.copytree(source, target / directory)
    return LOCAL_SKILL_SOURCE


def _add_command(npx: str, source: Path, *, noninteractive: bool) -> list[str]:
    """Build one local-source install/refresh command for the two agents fwd supports."""
    command = [npx]
    if noninteractive:
        command.append("--yes")
    command.extend(("skills", "add", str(source), "--global", "--agent", *TARGET_AGENTS, "--skill", SKILL_NAME))
    if noninteractive:
        command.append("-y")
    return command


def skills_environment(*, noninteractive: bool) -> dict[str, str]:
    """Return an inherited environment with telemetry disabled always and agent detection for noninteractive calls."""
    environment = {**os.environ, "DISABLE_TELEMETRY": "1"}
    if noninteractive:
        # AI_AGENT makes `skills` run noninteractively through https://www.npmjs.com/package/@vercel/detect-agent.
        environment["AI_AGENT"] = SKILL_NAME
    return environment


def _current_revision() -> str:
    """Fingerprint the installed CLI and skill payload so editable installs refresh before a version bump."""
    package_dir = Path(__file__).resolve().parent
    digest = sha256()
    for path in sorted(package_dir.rglob("*.py")):
        digest.update(path.relative_to(package_dir).as_posix().encode())
        digest.update(path.read_bytes())
    payload_root = _payload_root()
    payload_paths = [payload_root / "SKILL.md", *_payload_directory(payload_root, "agents").rglob("*"), *(payload_root / "references").rglob("*")]
    for path in sorted((path for path in payload_paths if path.is_file()), key=lambda item: item.relative_to(payload_root).as_posix()):
        digest.update(path.relative_to(payload_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _record(outcome: str) -> None:
    """Persist the one-time decision best-effort without blocking fwd when the home directory is unwritable."""
    try:
        SKILL_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SKILL_PROMPT_PATH.write_text(outcome + "\n", encoding="utf-8")
    except OSError as exc:
        ui.warn(f"could not remember the coding-agent skill choice ({exc})")


def _new_update_log_path() -> Path:
    """Create a persistent temporary log path for one quiet automatic skill refresh.

    Automatic updates run during an otherwise ordinary CLI invocation, so forwarding the interactive ``skills`` UI
    obscures the command the user actually requested. The temporary directory intentionally outlives this process:
    the one-line result can point curious users or bug reports at the complete installer transcript.
    """
    directory = Path(tempfile.mkdtemp(prefix=f"{ui.COMMAND_NAME}-skill-update-"))
    return directory / "npx-skills.log"


def offer_once() -> None:
    """Offer one local, global Codex/Claude skill install while retaining the installer's terminal."""
    if SKILL_PROMPT_PATH.exists():
        return
    if not ui.confirm(f"Install the {ui.command_accent()} skill for Codex and Claude using {ui.accent('npx skills')}?", default=True):
        _record("declined")
        return
    npx = shutil.which("npx")
    if npx is None:
        ui.warn(f"could not install the bundled {ui.command()} skill because npx is not on PATH; it will offer again next time")
        return
    try:
        source = _materialize_skill_source()
        result = subprocess.run(_add_command(npx, source, noninteractive=False), check=False, env=skills_environment(noninteractive=False))
    except OSError as exc:
        ui.warn(f"could not install the bundled {ui.command()} skill ({exc}); it will offer again next time")
        return
    if result.returncode != 0:
        ui.warn(f"could not install the bundled {ui.command()} skill (npx exited with status {result.returncode}); it will offer again next time")
        return
    _record(f"installed:{_current_revision()}")
    ui.ok(f"installed the bundled {ui.command()} skill for Codex and Claude from {source}")


def update_if_needed() -> None:
    """Reinstall the bundled local skill once per fwd revision without fetching fwd from GitHub or prompting."""
    try:
        state = SKILL_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not state.startswith("installed"):
        return
    revision = _current_revision()
    if state == f"installed:{revision}":
        return
    npx = shutil.which("npx")
    if npx is None:
        ui.warn(f"could not update the installed {ui.command()} skill because npx is not on PATH; it will retry next time")
        return
    try:
        log_path = _new_update_log_path()
    except OSError as exc:
        ui.warn(f"could not create a log for the installed {ui.command()} skill update ({exc}); it will retry next time")
        return
    try:
        source = _materialize_skill_source()
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(_add_command(npx, source, noninteractive=True), check=False, stdout=log, stderr=subprocess.STDOUT, env=skills_environment(noninteractive=True))
    except OSError as exc:
        ui.warn(f"could not update the installed {ui.command()} skill from the local package ({exc}); logs at {log_path}; it will retry next time")
        return
    if result.returncode != 0:
        ui.warn(f"could not update the installed {ui.command()} skill from the local package (npx exited with status {result.returncode}); logs at {log_path}; it will retry next time")
        return
    _record(f"installed:{revision}")
    ui.ok(f"updated the installed {ui.command()} coding-agent skill. logs at {log_path}")
