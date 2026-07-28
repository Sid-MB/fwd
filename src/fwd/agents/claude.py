"""Claude Code agent integration.

Claude is the most stateful built-in agent: local preparation may export a transcript or generate a handoff document,
while remote preparation can upload config, credentials, and the relocated transcript. Those details live here so
the general launch pipeline sees the same hooks it sees for Codex or any future agent.
"""

from __future__ import annotations

import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from fwd import ui
from fwd.agents.base import Agent, AgentLaunchOptions
from fwd.agents import claude_state
from fwd.config import Config
from fwd.sshexec import SSHEndpoint
from fwd.tooling.requirements import CLAUDE

HANDOFF_PROMPT = "Read HANDOFF.md, then continue the work it describes"
HANDOFF_MAX_AGE_SECONDS = 15 * 60


def build_command(*, resume_id: str | None, use_handoff: bool) -> str:
    """Build a Claude startup command for transcript resume, handoff context, or a clean conversation."""
    if resume_id:
        return f"claude --resume {shlex.quote(resume_id)}"
    if use_handoff:
        return f"claude {shlex.quote(HANDOFF_PROMPT)}"
    return "claude"


def fresh_handoff(local_cwd: Path) -> Path | None:
    """Return a recent HANDOFF.md so repair launches avoid another expensive local agent run."""
    handoff = local_cwd / "HANDOFF.md"
    if not handoff.is_file():
        return None
    try:
        age = time.time() - handoff.stat().st_mtime
    except OSError:
        return None
    return handoff if 0 <= age < HANDOFF_MAX_AGE_SECONDS else None


class ClaudeAgent(Agent):
    """Transfer optional Claude state and construct commands that resume the same conversation."""

    name = "claude"
    command = ("claude",)
    tools = (CLAUDE,)

    def launch_flags(self, config: Config, options: AgentLaunchOptions) -> dict[str, Any]:
        """Merge explicit transfer switches with Claude config, preserving handoff/session precedence."""
        if options.handoff:
            want_session, want_handoff = False, True
        elif options.session:
            want_session, want_handoff = True, config.claude.handoff
        else:
            want_session, want_handoff = config.claude.session, config.claude.handoff
        return {
            "session": want_session,
            "handoff": want_handoff,
            "user_config": options.user_config or config.claude.user_config,
            "creds": options.creds or config.claude.creds,
        }

    def prepare_local(self, local_cwd: Path, flags: dict[str, Any]) -> object | None:
        """Export a resumable transcript and ensure any requested handoff is present before project sync."""
        bundle: Path | None = None
        if flags["session"]:
            with ui.step("Exporting Claude session transcript"):
                bundle = claude_state.export_session_bundle(local_cwd, Path(tempfile.mkdtemp(prefix="fwd-session-")))
            if bundle is None:
                flags["session"] = False
                if flags["handoff"]:
                    ui.info("falling back to a handoff document")
        if flags["handoff"]:
            existing = fresh_handoff(local_cwd)
            if existing is not None:
                age = (time.time() - existing.stat().st_mtime) / 60
                ui.info(f"reusing HANDOFF.md from {age:.0f} min ago (delete it to force regeneration)")
            else:
                with ui.step("Generating HANDOFF.md"):
                    claude_state.make_handoff(local_cwd)
        return bundle

    def prepare_remote(self, endpoint: SSHEndpoint, remote_dir: str, flags: dict[str, Any], local_state: object | None) -> dict[str, Any]:
        """Install requested user state and import the transcript, degrading cleanly when optional transfer fails."""
        if flags["user_config"]:
            with ui.step("Uploading Claude user config"):
                claude_state.upload_user_config(endpoint)
        if flags["creds"]:
            creds_json: str | None = None
            with ui.step("Copying Claude credentials"):
                creds_json = claude_state.read_keychain_creds()
                if creds_json:
                    claude_state.upload_creds(endpoint, creds_json)
            if creds_json:
                ui.warn("a live Claude token now exists on the remote machine at ~/.claude/.credentials.json (mode 600)")
            else:
                ui.warn("no local Claude credentials found; you will need to log in inside the remote session")

        resume_id: str | None = None
        if isinstance(local_state, Path):
            with ui.step("Importing Claude session transcript"):
                remote_home = endpoint.run('printf %s "$HOME"').stdout.strip() or f"/home/{endpoint.user}"
                resume_id = claude_state.import_session_bundle(endpoint, local_state, remote_dir, remote_home)
        if flags["session"] and not resume_id:
            if flags["handoff"]:
                ui.warn("could not install the transcript remotely; the session will start from HANDOFF.md instead")
            else:
                ui.warn(f"could not install the transcript remotely; starting a fresh session (try {ui.command('up --handoff')!r})")
        return {"resume_id": resume_id}

    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Start Claude with the context chosen during this launch."""
        resume_id = flags.get("resume_id")
        return build_command(
            resume_id=resume_id if isinstance(resume_id, str) else None,
            use_handoff=bool(flags.get("handoff")) and not resume_id,
        )

    def send_command(self, message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
        """Send a streaming Claude turn, resuming the exact transferred conversation when known."""
        command = ["claude", "--print", "--verbose", "--output-format", "stream-json"]
        resume_id = flags.get("resume_id")
        if isinstance(resume_id, str) and resume_id:
            command.extend(("--resume", resume_id))
        else:
            command.append("--continue")
        command.append(message)
        return tuple(command)
