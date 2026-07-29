"""Codex agent integration."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import Mapping

from fwd import ui
from fwd.agents import codex_state
from fwd.agents.base import Agent
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.tooling.requirements import CODEX

MANAGED_CODEX = '"$HOME/.codex/packages/standalone/current/codex"'
CODEX_TUI_SEND_PATH = Path(__file__).parent.parent / "scripts" / "codex_tui_send.py"


def _remote_control_status(endpoint: SSHEndpoint) -> int:
    """Return 0 when managed Remote Control and ChatGPT auth are ready, 2 for missing auth, or 1 when unsupported."""
    probe = endpoint.run(
        f"test -x {MANAGED_CODEX} || exit 1; "
        f"{MANAGED_CODEX} remote-control start --help >/dev/null 2>&1 || exit 1; "
        f"{MANAGED_CODEX} login status 2>/dev/null | grep -q 'Logged in using ChatGPT' || exit 2; "
        "printf fwd-codex-remote-control-ready",
        check=False,
    )
    if probe.returncode == 2:
        return 2
    return 0 if probe.returncode == 0 and probe.stdout.strip() == "fwd-codex-remote-control-ready" else 1


def _start_remote_control(endpoint: SSHEndpoint) -> bool:
    """Start the managed app-server daemon without making an optional capability block the primary TUI."""
    try:
        result = endpoint.run(f"{MANAGED_CODEX} remote-control start --json >/dev/null", check=False, timeout=120)
    except SSHError as exc:
        ui.warn(f"could not start Codex Remote Control; continuing with the terminal session ({exc})")
        return False
    if result.returncode != 0:
        ui.warn("could not start Codex Remote Control; continuing with the terminal session")
        return False
    ui.info("Codex Remote Control daemon is running; supported signed-in clients can discover this machine")
    return True


class CodexAgent(Agent):
    """Install Codex, synchronize portable settings, and resume its latest remote conversation."""

    name = "codex"
    command = ("codex",)
    tools = (CODEX,)
    remote_home_entry = ".codex"

    def _runtime_args(self, flags: Mapping[str, object]) -> list[str]:
        """Apply VM-safe full access unless configured argv already selects an approval or sandbox policy."""
        configured = self.runtime_args(flags)
        policy_flags = {"--yolo", "--dangerously-bypass-approvals-and-sandbox", "--sandbox", "-s", "--ask-for-approval", "-a"}
        has_policy = any(part in policy_flags or any(part.startswith(f"{flag}=") for flag in policy_flags if flag.startswith("--")) for part in configured)
        access = ["--dangerously-bypass-approvals-and-sandbox"] if bool(flags.get("agent_full_access", True)) and not has_policy else []
        return [*access, *configured]

    def prepare_remote(self, endpoint: SSHEndpoint, remote_dir: str, flags: dict[str, object], local_state: object | None) -> dict[str, object]:
        """Upload portable state and start the independent remote-control daemon when supported."""
        del remote_dir, flags, local_state
        with ui.step("Uploading Codex settings and skills"):
            codex_state.upload_user_config(endpoint)
        remote_control_status = _remote_control_status(endpoint)
        if remote_control_status == 0:
            with ui.step("Starting Codex Remote Control"):
                return {"remote_control": _start_remote_control(endpoint)}
        if remote_control_status == 2:
            ui.info("Codex Remote Control is installed but requires a ChatGPT login on the remote")
        return {"remote_control": False}

    def startup_command(self, flags: Mapping[str, object]) -> str:
        """Start a new interactive Codex conversation."""
        command = shlex.join(["codex", *self._runtime_args(flags)])
        return self.with_environment_defaults(command, flags)

    def prepare_send(self, endpoint: SSHEndpoint, flags: Mapping[str, object]) -> None:
        """Install the small pane-to-rollout bridge used to address the exact live Codex TUI."""
        tool_prefix = flags.get("tool_prefix")
        if not isinstance(tool_prefix, str) or not tool_prefix:
            raise SSHError("this Codex session predates its recorded tool prefix; rerun `fwd up codex` once to repair it")
        remote_path = f"{tool_prefix.rstrip('/')}/bin/fwd-codex-tui-send"
        digest = hashlib.sha256(CODEX_TUI_SEND_PATH.read_bytes()).hexdigest()
        present = endpoint.run(
            f"test -x {shlex.quote(remote_path)} && test \"$(sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1)\" = {shlex.quote(digest)}",
            check=False,
        )
        if present.returncode != 0:
            endpoint.upload_file(CODEX_TUI_SEND_PATH, remote_path)

    def send_command(self, message: str, flags: Mapping[str, object], *, tmux_session: str = "", remote_dir: str = "") -> tuple[str, ...]:
        """Drive the existing Codex TUI and stream its exact persisted response instead of spawning a second Codex."""
        tool_prefix = flags.get("tool_prefix")
        if not isinstance(tool_prefix, str) or not tool_prefix or not tmux_session or not remote_dir:
            raise SSHError("the live Codex send bridge is missing session metadata; rerun `fwd up codex` once to repair it")
        helper = f"{tool_prefix.rstrip('/')}/bin/fwd-codex-tui-send"
        return (helper, "--tmux-session", tmux_session, "--cwd", remote_dir, message)
