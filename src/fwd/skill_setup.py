"""One-time interactive offer to install fwd's bundled coding-agent skill.

The skill is distributed with the repository and installed through the open ``skills`` CLI, rather than by the Python package installer. Prompting from the first human invocation keeps package installation side-effect free while making the agent-facing documentation discoverable alongside shell-completion onboarding.
"""

from __future__ import annotations

import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

from fwd import ui

SKILL_SOURCE = "Sid-MB/fwd"
SKILL_NAME = "fwd"
SKILL_ADD_COMMAND = ("skills", "add", SKILL_SOURCE)
SKILL_UPDATE_COMMAND = ("--yes", "skills", "update", SKILL_NAME, "-y")
SKILL_PROMPT_PATH = Path.home() / ".fwd" / "skill-prompted"


def _current_revision() -> str:
    """Fingerprint the installed CLI and bundled skill so Git installs are detected even before a package version bump."""
    package_dir = Path(__file__).resolve().parent
    digest = sha256()
    for path in sorted(package_dir.rglob("*.py")):
        digest.update(path.relative_to(package_dir).as_posix().encode())
        digest.update(path.read_bytes())
    payload_root = package_dir if (package_dir / "SKILL.md").is_file() else package_dir.parents[1]
    payload_paths = [payload_root / "SKILL.md", *(payload_root / "agents").rglob("*"), *(payload_root / "references").rglob("*"), *(payload_root / "skills").rglob("*"), payload_root / ".codex-plugin" / "plugin.json"]
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


def offer_once() -> None:
    """Offer to run ``npx skills add Sid-MB/fwd`` once, retaining inherited stdio for the installer's own prompts."""
    if SKILL_PROMPT_PATH.exists():
        return
    command_text = f"npx {' '.join(SKILL_ADD_COMMAND)}"
    if not ui.confirm(f"Install the fwd skill for your coding agents with '{command_text}'?", default=True):
        _record("declined")
        return
    npx = shutil.which("npx")
    if npx is None:
        ui.warn(f"could not install the fwd skill because npx is not on PATH; retry later with '{command_text}'")
        return
    try:
        result = subprocess.run([npx, *SKILL_ADD_COMMAND], check=False)
    except OSError as exc:
        ui.warn(f"could not install the fwd skill ({exc}); retry with '{command_text}'")
        return
    if result.returncode != 0:
        ui.warn(f"could not install the fwd skill (npx exited with status {result.returncode}); retry with '{command_text}'")
        return
    _record(f"installed:{_current_revision()}")
    ui.ok(f"installed the fwd coding-agent skill from {SKILL_SOURCE}")


def update_if_needed() -> None:
    """Update an accepted skill once per installed CLI revision without introducing another confirmation prompt."""
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
        ui.warn("could not update the installed fwd skill because npx is not on PATH; it will retry next time")
        return
    command_text = f"npx {' '.join(SKILL_UPDATE_COMMAND)}"
    try:
        result = subprocess.run([npx, *SKILL_UPDATE_COMMAND], check=False)
    except OSError as exc:
        ui.warn(f"could not update the installed fwd skill ({exc}); it will retry with '{command_text}'")
        return
    if result.returncode != 0:
        ui.warn(f"could not update the installed fwd skill (npx exited with status {result.returncode}); it will retry with '{command_text}'")
        return
    _record(f"installed:{revision}")
    ui.ok("updated the installed fwd coding-agent skill")
