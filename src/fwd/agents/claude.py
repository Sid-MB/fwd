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


def build_command(*, resume_id: str | None, use_handoff: bool, remote_control_name: str | None = None, runtime_args: tuple[str, ...] = ()) -> str:
    """Build a Claude startup command with the selected context and optional cross-device control."""
    command = ["claude", *runtime_args]
    if resume_id:
        command.extend(("--resume", resume_id))
    elif use_handoff:
        command.append(HANDOFF_PROMPT)
    plain_command = shlex.join(command)
    if not remote_control_name:
        return plain_command
    remote_command = shlex.join(["claude", "--remote-control", remote_control_name, *command[1:]])
    fallback = f"{remote_command} || {{ printf '%s\\n' 'Claude Remote Control unavailable; starting a normal terminal session.' >&2; exec {plain_command}; }}"
    return f"bash -lc {shlex.quote(fallback)}"


def _remote_control_status(endpoint: SSHEndpoint) -> int:
    """Return 0 when Remote Control can start, 2 when supported but not account-authenticated, or 1 when absent."""
    probe = endpoint.run(
        "claude --help 2>&1 | grep -q -- '--remote-control' || exit 1; "
        "status=$(claude auth status --json 2>/dev/null) || exit 2; "
        "printf %s \"$status\" | grep -Eq '\"authMethod\"[[:space:]]*:[[:space:]]*\"claude\\.ai\"' || exit 2; "
        "printf %s \"$status\" | grep -Eq '\"subscriptionType\"[[:space:]]*:[[:space:]]*\"(pro|max|team|enterprise)\"' || exit 2; "
        "printf fwd-claude-remote-control-ready",
        check=False,
    )
    if probe.returncode == 2:
        return 2
    return 0 if probe.returncode == 0 and probe.stdout.strip() == "fwd-claude-remote-control-ready" else 1


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
    remote_home_entry = ".claude"

    def launch_flags(self, config: Config, options: AgentLaunchOptions) -> dict[str, Any]:
        """Merge explicit transfer switches with Claude config, preserving handoff/session precedence."""
        runtime_flags = super().launch_flags(config, AgentLaunchOptions())
        if options.handoff:
            want_session, want_handoff = False, True
        elif options.session:
            want_session, want_handoff = True, config.claude.handoff
        else:
            want_session, want_handoff = config.claude.session, config.claude.handoff
        return {
            **runtime_flags,
            "session": want_session,
            "handoff": want_handoff,
            "user_config": options.user_config or config.claude.user_config,
            "creds": options.creds or config.claude.creds,
        }

    def _runtime_args(self, flags: Mapping[str, object]) -> list[str]:
        """Apply bypassPermissions by default while respecting an explicitly configured permission-mode argument."""
        configured = self.runtime_args(flags)
        permission_flags = {"--permission-mode", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
        has_permission_mode = any(part in permission_flags or part.startswith("--permission-mode=") for part in configured)
        access = ["--permission-mode", "bypassPermissions"] if bool(flags.get("agent_full_access", True)) and not has_permission_mode else []
        return [*access, *configured]

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
        """Install requested state, import the transcript, and enable supported cross-device control."""
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
        remote_control_name: str | None = None
        remote_control_status = _remote_control_status(endpoint)
        if remote_control_status == 0:
            remote_control_name = f"fwd: {Path(remote_dir).name}"
            ui.info(f"Claude Remote Control enabled as {remote_control_name!r}")
        elif remote_control_status == 2:
            ui.info("Claude Remote Control is installed but requires a claude.ai Pro, Max, Team, or Enterprise login on the remote")
        return {"resume_id": resume_id, "remote_control_name": remote_control_name}

    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Start Claude with the context chosen during this launch."""
        resume_id = flags.get("resume_id")
        command = build_command(
            resume_id=resume_id if isinstance(resume_id, str) else None,
            use_handoff=bool(flags.get("handoff")) and not resume_id,
            remote_control_name=flags.get("remote_control_name") if isinstance(flags.get("remote_control_name"), str) else None,
            runtime_args=tuple(self._runtime_args(flags)),
        )
        return self.with_environment_defaults(command, flags)

    def send_command(self, message: str, flags: Mapping[str, object], *, tmux_session: str = "", remote_dir: str = "") -> tuple[str, ...]:
        """Send a streaming Claude turn, resuming the exact transferred conversation when known."""
        del tmux_session, remote_dir
        command = ["claude", *self._runtime_args(flags), "--print", "--verbose", "--output-format", "stream-json"]
        resume_id = flags.get("resume_id")
        if isinstance(resume_id, str) and resume_id:
            command.extend(("--resume", resume_id))
        else:
            command.append("--continue")
        command.append(message)
        return self.environment_command(command, flags)
