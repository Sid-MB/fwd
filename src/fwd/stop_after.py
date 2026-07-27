"""Remote-owned stop-after scheduling shared by launch, send, lifecycle, and coding agents.

A local ``finally: fwd stop`` is not sufficient: laptops sleep, terminals disappear, and network connections fail. Stop-after therefore installs a tiny per-session action on the remote host. Managed send tasks run that action in their own tmux window after all dependencies finish, while an interactive coding agent can invoke the generic ``stopafter`` helper inherited through ``FWD_STOP_AFTER_SCRIPT``.

The backend supplies only its provider-specific remote stop command. This module owns the common ordering: record the schedule, wait briefly so task logs and exit markers settle, close the primary tmux session, ask the provider to suspend compute, and finally close the hidden task manager. RunPod's pod-scoped credentials, Slurm's login-node ``scancel``, and SSH's tmux-only stop semantics all remain inside their backend boundary.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from fwd.backends.base import Backend
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState

STOP_DELAY_SECONDS = 3
GUIDANCE_BEGIN = "<!-- fwd stopafter guidance begin -->"
GUIDANCE_END = "<!-- fwd stopafter guidance end -->"
GUIDANCE = f"""{GUIDANCE_BEGIN}
When working inside an fwd-managed remote session, the `stopafter` command is available. Run `stopafter` only as your final action after requested work and durable output are complete; it schedules this fwd session and its remote compute to stop without depending on the user's local computer. Run `stopafter --cancel` before shutdown begins to cancel it.
{GUIDANCE_END}
"""


class StopAfterUnsupported(RuntimeError):
    """Raised when a backend cannot safely reproduce ``fwd stop`` from its remote endpoint."""


def _safe_session_name(name: str) -> str:
    """Return the same conservative identifier alphabet used by remote tmux task names."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "session"


def _tool_prefix(session: SessionState) -> str:
    """Resolve the bootstrapped tool root recorded at launch, with a legacy-state fallback."""
    recorded = session.flags.get("tool_prefix")
    if isinstance(recorded, str) and recorded:
        return recorded.rstrip("/")
    return f"{session.remote_dir.rstrip('/')}/.fwd-tools"


def action_path(session: SessionState) -> str:
    """Return the per-session remote action path used by managed tasks and the agent helper."""
    return f"{_tool_prefix(session)}/stop-after/{_safe_session_name(session.name)}/action"


def _state_dir(session: SessionState) -> str:
    """Return the remote marker directory used for status and cancellation."""
    return f"$HOME/.fwd/stop-after/{_safe_session_name(session.name)}"


def _write_executable(endpoint: SSHEndpoint, path: str, content: str) -> None:
    """Atomically replace one small remote helper without requiring Python or an upload transport."""
    parent = str(PurePosixPath(path).parent)
    temporary = f"{path}.tmp"
    command = f"umask 077; mkdir -p {shlex.quote(parent)}; printf %s {shlex.quote(content)} > {shlex.quote(temporary)}; chmod 700 {shlex.quote(temporary)}; mv -f {shlex.quote(temporary)} {shlex.quote(path)}"
    endpoint.run(command, check=True)


def _render_action(session: SessionState, provider_stop: str) -> str:
    """Render the concrete delayed stop action for one session and backend."""
    primary = shlex.quote(session.tmux_session)
    manager = shlex.quote(f"fwd-tasks-{_safe_session_name(session.name)}")
    state_dir = _state_dir(session)
    return f"""#!/usr/bin/env bash
set -u
state_dir="{state_dir}"
state_file="$state_dir/state"
pid_file="$state_dir/pid"
mkdir -p "$state_dir"

cancel() {{
    current="$(cat "$state_file" 2>/dev/null || printf "idle")"
    if [ "$current" = "stopping" ] || [ "$current" = "stopped" ]; then
        printf "stop-after already began for {session.name}; it can no longer be canceled\\n" >&2
        return 3
    fi
    if [ -f "$pid_file" ]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    fi
    printf "canceled\\n" > "$state_file"
    rm -f "$pid_file"
    printf "stop-after canceled for {session.name}\\n"
}}

case "${{1:-}}" in
    --cancel|cancel)
        cancel
        exit $?
        ;;
    --status|status)
        cat "$state_file" 2>/dev/null || printf "idle\\n"
        exit 0
        ;;
    --foreground)
        printf "%s\\n" "$$" > "$pid_file"
        trap 'printf "canceled\\n" > "$state_file"; rm -f "$pid_file"; exit 130' INT TERM HUP
        printf "scheduled\\n" > "$state_file"
        sleep {STOP_DELAY_SECONDS}
        printf "stopping\\n" > "$state_file"
        if [ -n "${{FWD_TASK_DIR:-}}" ]; then
            printf "0\\n" > "$FWD_TASK_DIR/exit"
            printf "done\\n" > "$FWD_TASK_DIR/state"
        fi
        tmux kill-session -t {primary} 2>/dev/null || true
        {provider_stop}
        printf "stopped\\n" > "$state_file"
        rm -f "$pid_file"
        tmux kill-session -t {manager} 2>/dev/null || true
        exit 0
        ;;
    *)
        if [ -f "$pid_file" ]; then
            pid="$(cat "$pid_file" 2>/dev/null || true)"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                printf "stop-after is already scheduled for {session.name}\\n"
                exit 0
            fi
        fi
        nohup "$0" --foreground >> "$state_dir/stop-after.log" 2>&1 < /dev/null &
        printf "%s\\n" "$!" > "$pid_file"
        printf "stop-after scheduled for {session.name}\\n"
        ;;
esac
"""


def _render_helper() -> str:
    """Render the PATH-level agent command, dispatching through the current session's environment."""
    return """#!/usr/bin/env bash
set -u
if [ -z "${FWD_STOP_AFTER_SCRIPT:-}" ] && [ -n "${FWD_TOOL_PREFIX:-}" ] && command -v tmux >/dev/null 2>&1; then
    tmux_name="$(tmux display-message -p '#S' 2>/dev/null || true)"
    mapping="$FWD_TOOL_PREFIX/stop-after/by-tmux/$tmux_name"
    if [ -n "$tmux_name" ] && [ -f "$mapping" ]; then
        FWD_STOP_AFTER_SCRIPT="$(cat "$mapping")"
    fi
fi
if [ -z "${FWD_STOP_AFTER_SCRIPT:-}" ]; then
    printf "stopafter: this shell is not inside a stop-after-enabled fwd session\\n" >&2
    exit 2
fi
exec "$FWD_STOP_AFTER_SCRIPT" "$@"
"""


def _install_agent_guidance(endpoint: SSHEndpoint) -> None:
    """Append one idempotent managed block to the documented user-level instruction files for Codex and Claude."""
    for path in ("$HOME/.codex/AGENTS.md", "$HOME/.claude/CLAUDE.md"):
        command = f'path="{path}"; mkdir -p "$(dirname "$path")"; touch "$path"; grep -Fq {shlex.quote(GUIDANCE_BEGIN)} "$path" || printf "\\n%s\\n" {shlex.quote(GUIDANCE.rstrip())} >> "$path"'
        endpoint.run(command, check=False)


def prepare(endpoint: SSHEndpoint, backend: Backend, session: SessionState, *, agent_guidance: bool = False) -> str:
    """Install or refresh the session action and generic helper, returning the action path.

    ``prepare`` is idempotent and intentionally runs again before each scheduled stop. Backend identifiers and tmux names may have changed after a provider restart, so stale remote scripts are less safe than a cheap rewrite.
    """
    provider_stop = backend.remote_stop_command(session)
    if provider_stop is None:
        raise StopAfterUnsupported(f"backend {session.backend!r} cannot stop itself remotely; --stop-after is not supported for this target")
    action = action_path(session)
    helper = f"{_tool_prefix(session)}/bin/stopafter"
    mapping = f"{_tool_prefix(session)}/stop-after/by-tmux/{session.tmux_session}"
    _write_executable(endpoint, action, _render_action(session, provider_stop))
    _write_executable(endpoint, helper, _render_helper())
    _write_executable(endpoint, mapping, f"{action}\n")
    if agent_guidance:
        _install_agent_guidance(endpoint)
    return action


def cancel(endpoint: SSHEndpoint, session: SessionState) -> bool:
    """Cancel a delayed action, returning ``False`` once provider shutdown has already begun."""
    result = endpoint.run(f"{shlex.quote(action_path(session))} --cancel", check=False)
    return result.returncode == 0


def status(endpoint: SSHEndpoint, session: SessionState) -> str:
    """Return the remote action marker without raising when an older session has no helper."""
    result = endpoint.run(f"{shlex.quote(action_path(session))} --status", check=False)
    value = (result.stdout or "").strip()
    return value if value in {"idle", "scheduled", "stopping", "stopped", "canceled"} else "idle"


def with_agent_environment(command: str, action: str) -> str:
    """Wrap an agent command so tool subprocesses inherit the session-specific helper path."""
    inner = f"export FWD_STOP_AFTER_SCRIPT={shlex.quote(action)}; exec {command}"
    return shlex.join(["bash", "-lc", inner])
