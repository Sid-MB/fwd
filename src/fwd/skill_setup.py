"""One-time interactive offer to install fwd's bundled coding-agent skill.

The skill is distributed with the repository and installed through the open ``skills`` CLI, rather than by the Python package installer. Prompting from the first human invocation keeps package installation side-effect free while making the agent-facing documentation discoverable alongside shell-completion onboarding.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fwd import ui

SKILL_SOURCE = "Sid-MB/fwd"
SKILL_COMMAND = ("skills", "add", SKILL_SOURCE)
SKILL_PROMPT_PATH = Path.home() / ".fwd" / "skill-prompted"


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
    command_text = f"npx {' '.join(SKILL_COMMAND)}"
    if not ui.confirm(f"Install the fwd skill for your coding agents with '{command_text}'?", default=True):
        _record("declined")
        return
    npx = shutil.which("npx")
    if npx is None:
        ui.warn(f"could not install the fwd skill because npx is not on PATH; retry later with '{command_text}'")
        return
    try:
        result = subprocess.run([npx, *SKILL_COMMAND], check=False)
    except OSError as exc:
        ui.warn(f"could not install the fwd skill ({exc}); retry with '{command_text}'")
        return
    if result.returncode != 0:
        ui.warn(f"could not install the fwd skill (npx exited with status {result.returncode}); retry with '{command_text}'")
        return
    _record("installed")
    ui.ok(f"installed the fwd coding-agent skill from {SKILL_SOURCE}")
