"""Remote environment setup and tmux session management.

Design intent (owned by the core/remote teammate)
-------------------------------------------------
Two responsibilities that always run back-to-back during a launch:

1. **Bootstrap + dependencies.** ``bootstrap.sh`` is piped to a remote ``bash -s`` (never copied first, so it cannot
   drift from the installed package) and establishes persistent paths plus tmux. Class-based project toolchains and
   coding-agent specs then feed shared tool requirements to one resolver, which reuses working remote executables
   before trying persistent user-space installers. A project's own ``.fwd/setup.sh`` runs last.

2. **tmux.** tmux is what makes the session persistent: the ``claude`` process is owned by a detached tmux session, so
   a dropped laptop connection is irrelevant. Note ``tmux_attach_argv`` returns *argv* rather than attaching itself —
   the caller feeds it to :meth:`fwd.sshexec.SSHEndpoint.exec_interactive` so Python is replaced by ssh and terminal
   behaviour stays native.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from fwd import command_docs, ui
from fwd.remote_env import FWD_ENV_RELPATH, HOME_ENV_RELPATH, source_env as _source_env
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.tmux_config import REMOTE_TMUX_CONFIG_RELPATH, install as _install_tmux_config
from fwd.toolchains import PROJECT_SETUP_RELPATH, plan as toolchain_plan
from fwd.tooling import ToolchainPlan, ensure_tools

# Package-relative so it resolves identically from a wheel install and an editable checkout. Switch to
# importlib.resources.as_file if fwd is ever shipped as a zipapp, where __file__ is not a real filesystem path.
BOOTSTRAP_PATH: Path = Path(__file__).parent / "scripts" / "bootstrap.sh"

# How long to let a freshly created tmux session settle before deciding it is alive. Long enough that a command which
# dies on a missing binary has exited, short enough to be invisible in a launch that already takes tens of seconds.
TMUX_SETTLE_SECONDS = 2

def _tmux_exact_target(session: str) -> str:
    """Return tmux's exact-match target with shell quoting forced.

    tmux uses a leading ``=`` to disable prefix/glob target matching. Python's :func:`shlex.quote` considers ``=`` a
    safe character and leaves ``=fwd-name`` bare, but zsh interprets a leading equals sign as executable-path
    expansion before tmux sees it. Explicit single-quote construction is therefore required even for sanitized fwd
    session names; the replacement keeps this helper correct for arbitrary future names.
    """
    value = f"={session}"
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run_bootstrap(
    endpoint: SSHEndpoint,
    *,
    tool_prefix: str,
    remote_dir: str,
    scratch: str | None = None,
) -> None:
    """Pipe the provider-independent environment/tmux bootstrap to the remote host and run it.

    Args:
        endpoint: Target machine.
        tool_prefix: Exported as ``FWD_TOOL_PREFIX``; must be on persistent storage for the install to survive a stop.
        remote_dir: Exported as ``FWD_REMOTE_DIR``.
        scratch: Exported as ``FWD_SCRATCH`` for caches; defaults to a path under ``tool_prefix`` when ``None``.

    Raises:
        fwd.sshexec.SSHError: If the script exits nonzero.
    """
    env = {
        "FWD_COMMAND_NAME": ui.COMMAND_NAME,
        "FWD_TOOL_PREFIX": tool_prefix,
        "FWD_REMOTE_DIR": remote_dir,
        "FWD_SCRATCH": scratch or f"{tool_prefix.rstrip('/')}/scratch",
    }
    # stream=True: bootstrap can take minutes on a cold machine and silence reads as a hang.
    endpoint.run_script(BOOTSTRAP_PATH, env=env, check=True, stream=True)


def install_tmux_config(endpoint: SSHEndpoint) -> str:
    """Install the preferred local tmux configuration, or fwd's portable fallback when none exists."""
    return _install_tmux_config(endpoint)


def detect_toolchain_plan(local_dir: str | Path) -> ToolchainPlan:
    """Detect project ecosystems locally and return their complete shared-tool setup plan."""
    return toolchain_plan(local_dir)


def detect_dep_commands(local_dir: str | Path) -> list[str]:
    """Return dependency commands from every detected class-based toolchain.

    Kept as a compatibility helper for callers that only need commands; launch consumes the full plan so its agent and
    project requirements can share one deduplicated resolver pass.
    """
    return list(detect_toolchain_plan(local_dir).commands)


def run_dep_install(endpoint: SSHEndpoint, remote_dir: str, commands: Sequence[str]) -> None:
    """Run dependency-install commands remotely in ``remote_dir``, streaming output.

    Each command gets its own ssh round trip so a failure names the exact command, and each re-sources ``fwd-env.sh``
    because commands do not share a shell.
    """
    for command in commands:
        remote = f"{_source_env()}cd {shlex.quote(remote_dir)} && {command}"
        endpoint.run(remote, check=True, capture=False)


def tmux_new(
    endpoint: SSHEndpoint,
    session: str,
    cwd: str,
    command: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Create a detached tmux session running ``command``.

    Detached-then-attach (rather than launching attached) means the session exists and is recorded in state even if the
    user's terminal dies during the handoff.

    Args:
        session: tmux session name, conventionally ``fwd-<name>``.
        cwd: Working directory for the session (``-c``).
        command: Command tmux runs, e.g. ``claude --resume <id>``.
        env: Variables exported before launching, for tool paths and caches.
    """
    exports = "".join(f"export {key}={shlex.quote(str(value))}; " for key, value in (env or {}).items())
    # The inner command is wrapped in `bash -lc` so fwd-env.sh, the user's profile and shell functions are all live
    # inside the session — a raw tmux command would run with tmux's own minimal environment.
    inner = f"{_source_env()}{exports}{command}"
    tmux_args = shlex.join(["new-session", "-d", "-s", session, "-c", cwd, "bash", "-lc", inner])
    config_path = f"$HOME/{REMOTE_TMUX_CONFIG_RELPATH}"
    tmux_cmd = f'if [ -f "{config_path}" ]; then tmux -f "{config_path}" {tmux_args}; else tmux {tmux_args}; fi'
    endpoint.run(f"{_source_env()}{tmux_cmd}", check=True)
    _verify_tmux_alive(endpoint, session, command)


def _verify_tmux_alive(endpoint: SSHEndpoint, session: str, command: str) -> None:
    """Fail loudly if the session died immediately after creation.

    ``tmux new-session -d`` returns 0 as soon as the session is *created*, not as soon as the command is *running*. If
    the command is missing (a wiped ``claude`` binary is the real-world case) the pane exits within milliseconds and
    fwd would otherwise report a ready session over a tmux server that is already gone. The settle delay is what makes
    this meaningful: checking instantly would race the dying pane and always pass.

    Raises:
        SSHError: If the session is gone, with the failing command and a probe of whether its binary exists.
    """
    probe = f"{_source_env()}sleep {TMUX_SETTLE_SECONDS}; tmux has-session -t {_tmux_exact_target(session)} 2>/dev/null"
    if endpoint.run(probe, check=False).returncode == 0:
        return
    binary = shlex.split(command)[0] if command.strip() else command
    found = endpoint.run(f"{_source_env()}command -v {shlex.quote(binary)}", check=False)
    where = found.stdout.strip() if found.returncode == 0 else "not found on PATH"
    raise SSHError(
        f"remote tmux session {session!r} exited immediately after starting {command!r} "
        f"({binary}: {where}). The startup command failed or exited unexpectedly — check its arguments and remote "
        "dependencies, then re-run the launch."
    )


def tmux_attach_command(session: str, *, control_mode: bool = False) -> str:
    """Build the remote tmux attach command without post-session output.

    Follow-up guidance belongs to the local wrapper in :func:`tmux_attach_argv`: OpenSSH prints its
    ``Shared connection ... closed`` message locally after this remote command exits, so a remote-side reminder would
    necessarily appear above that line rather than below it.
    """
    mode = " -CC" if control_mode else ""
    return f"{_source_env()}tmux{mode} attach -t {_tmux_exact_target(session)}"


def tmux_attach_argv(
    endpoint: SSHEndpoint,
    session: str,
    fwd_session: str | None = None,
    *,
    control_mode: bool = False,
) -> list[str]:
    """Return the local argv that attaches and, when possible, prints useful commands after SSH exits.

    Without an fwd session name this is the direct ``ssh -t`` argv used by low-level callers. Production attaches
    include the name, so a small local ``sh`` wrapper waits for SSH to print its closing message, prints the fenced
    reattach/stop/list examples beneath it, and then preserves SSH's exit status. The shell and SSH share the terminal
    process group; terminal resize, mouse reporting, and Ctrl-C continue to go directly to the foreground SSH client.
    """
    ssh_argv = [*endpoint.ssh_argv(tty=True), tmux_attach_command(session, control_mode=control_mode)]
    if not fwd_session:
        return ssh_argv
    examples = ui.code_examples(command_docs.post_attach_examples(fwd_session), heading=command_docs.NEXT_STEPS_HEADING)
    ssh_command = shlex.join(ssh_argv)
    wrapper = f"{ssh_command}; status=$?; printf '\\n%s\\n' {shlex.quote(examples)} >&2; exit \"$status\""
    return ["sh", "-c", wrapper]


def tmux_kill(endpoint: SSHEndpoint, session: str) -> None:
    """Kill a remote tmux session; no-op if it does not exist."""
    remote = f"{_source_env()}tmux kill-session -t {_tmux_exact_target(session)} 2>/dev/null || true"
    endpoint.run(remote, check=False)


def tmux_interrupt(endpoint: SSHEndpoint, session: str) -> None:
    """Send Ctrl-C to a session's active pane without killing its agent or conversation.

    This handles ``fwd send agent --stop`` before any managed send task exists: the original agent launched by
    ``fwd up`` owns the main pane, so interrupting that pane cancels its current turn while keeping it available.
    """
    command = f"{_source_env()}tmux send-keys -t {_tmux_exact_target(session)} C-c"
    endpoint.run(command, check=False)


def tmux_exists(endpoint: SSHEndpoint, session: str) -> bool:
    """Return whether a remote tmux session is alive (``tmux has-session``)."""
    remote = f"{_source_env()}tmux has-session -t {_tmux_exact_target(session)} 2>/dev/null"
    return endpoint.run(remote, check=False).returncode == 0
