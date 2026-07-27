"""Remote environment setup and tmux session management.

Design intent (owned by the core/remote teammate)
-------------------------------------------------
Two responsibilities that always run back-to-back during a launch:

1. **Bootstrap + dependencies.** ``bootstrap.sh`` is piped to a remote ``bash -s`` (never copied first, so it cannot
   drift from the installed package) and installs uv/bun/node/claude/tmux under ``FWD_TOOL_PREFIX``. Project
   dependencies are then inferred from lockfiles rather than configured, because the lockfile *is* the declaration:
   ``uv.lock`` → ``uv sync``, ``bun.lock*`` → ``bun install``, ``package-lock.json`` → ``npm ci``, ``pnpm-lock.yaml`` →
   ``pnpm install --frozen-lockfile``, ``requirements.txt`` → ``uv pip install -r``. A project's own
   ``.fwd/setup.sh`` runs last so it can build on whatever the detected manager installed.

2. **tmux.** tmux is what makes the session persistent: the ``claude`` process is owned by a detached tmux session, so
   a dropped laptop connection is irrelevant. Note ``tmux_attach_argv`` returns *argv* rather than attaching itself —
   the caller feeds it to :meth:`fwd.sshexec.SSHEndpoint.exec_interactive` so Python is replaced by ssh and terminal
   behaviour stays native.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from fwd.sshexec import SSHEndpoint, SSHError

# Package-relative so it resolves identically from a wheel install and an editable checkout. Switch to
# importlib.resources.as_file if fwd is ever shipped as a zipapp, where __file__ is not a real filesystem path.
BOOTSTRAP_PATH: Path = Path(__file__).parent / "scripts" / "bootstrap.sh"

# Written by bootstrap.sh; sourcing it is what puts uv/bun/node/claude on PATH and redirects caches to scratch.
FWD_ENV_RELPATH = "fwd-env.sh"

# Fixed-location pointer in $HOME that sources the above. Exists because the tool prefix varies per backend
# (/workspace on RunPod, scratch on Slurm, ~/.fwd-tools on plain ssh) and a non-login remote shell knows none of them.
HOME_ENV_RELPATH = ".fwd-env.sh"

# How long to let a freshly created tmux session settle before deciding it is alive. Long enough that a command which
# dies on a missing binary has exited, short enough to be invisible in a launch that already takes tens of seconds.
TMUX_SETTLE_SECONDS = 2

# Project escape hatch, run after the detected package manager so it can build on those installs.
PROJECT_SETUP_RELPATH = ".fwd/setup.sh"

# Python lockfile -> install command. Independent of the JS ecosystem: a repo can legitimately need both.
PYTHON_DEP_RULES: tuple[tuple[str, str], ...] = (("uv.lock", "uv sync"),)

# JS lockfile -> install command, in priority order. Only the FIRST match runs: two JS package managers installing into
# the same node_modules fight each other, and a leftover package-lock.json in a repo that migrated to bun is common.
JS_DEP_RULES: tuple[tuple[str, str], ...] = (
    ("bun.lockb", "bun install"),
    ("bun.lock", "bun install"),
    ("package-lock.json", "npm ci"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn --frozen-lockfile"),
)

# Flattened view, kept for callers that only want the lockfile->command mapping.
DEP_RULES: tuple[tuple[str, str], ...] = PYTHON_DEP_RULES + JS_DEP_RULES


def _source_env() -> str:
    """Return the shell prefix that loads ``fwd-env.sh`` if present.

    Why the pointer file: ``ssh host 'cmd'`` runs a *non-interactive, non-login* bash, which sources neither
    ``~/.bashrc`` (Ubuntu's even early-returns for non-interactive shells) nor ``~/.profile``. So nothing has exported
    ``FWD_TOOL_PREFIX`` by the time a dependency install runs, and expanding it here would silently produce
    ``/fwd-env.sh``. ``run_dep_install``'s signature has no tool_prefix to pass either. bootstrap therefore writes
    ``~/.fwd-env.sh`` with the resolved prefix baked in, giving every later command one fixed, prefix-independent
    entry point. The ``FWD_TOOL_PREFIX`` branch is the fallback for callers that do export it.

    Guarded with ``-f`` rather than assumed, so a dependency install still runs (using system tools) on a machine where
    bootstrap was skipped or partially failed.
    """
    return (
        f'if [ -f "$HOME/{HOME_ENV_RELPATH}" ]; then . "$HOME/{HOME_ENV_RELPATH}"; '
        f'elif [ -n "${{FWD_TOOL_PREFIX:-}}" ] && [ -f "$FWD_TOOL_PREFIX/{FWD_ENV_RELPATH}" ]; then . "$FWD_TOOL_PREFIX/{FWD_ENV_RELPATH}"; fi; '
    )


def run_bootstrap(
    endpoint: SSHEndpoint,
    *,
    tool_prefix: str,
    remote_dir: str,
    scratch: str | None = None,
) -> None:
    """Pipe ``bootstrap.sh`` to the remote host and run it.

    Args:
        endpoint: Target machine.
        tool_prefix: Exported as ``FWD_TOOL_PREFIX``; must be on persistent storage for the install to survive a stop.
        remote_dir: Exported as ``FWD_REMOTE_DIR``.
        scratch: Exported as ``FWD_SCRATCH`` for caches; defaults to a path under ``tool_prefix`` when ``None``.

    Raises:
        fwd.sshexec.SSHError: If the script exits nonzero.
    """
    env = {
        "FWD_TOOL_PREFIX": tool_prefix,
        "FWD_REMOTE_DIR": remote_dir,
        "FWD_SCRATCH": scratch or f"{tool_prefix.rstrip('/')}/scratch",
    }
    # stream=True: bootstrap can take minutes on a cold machine and silence reads as a hang.
    endpoint.run_script(BOOTSTRAP_PATH, env=env, check=True, stream=True)


def detect_dep_commands(local_dir: str | Path) -> list[str]:
    """Infer dependency-install commands from lockfiles present in the project.

    Detection reads the *local* tree, not the remote one: local is the source of truth and this runs before (or
    independently of) the sync, so it must not require a connection.

    Returns:
        Shell commands in execution order, with ``.fwd/setup.sh`` last if it exists. Empty when nothing is detected.
    """
    root = Path(local_dir).expanduser()
    commands: list[str] = []
    for filename, command in PYTHON_DEP_RULES:
        if (root / filename).is_file() and command not in commands:
            commands.append(command)
    for filename, command in JS_DEP_RULES:
        if (root / filename).is_file():
            commands.append(command)
            break  # One JS manager only; see JS_DEP_RULES.

    if not commands:
        # No lockfile: fall back to the declaration files. pyproject implies uv can resolve it; a bare requirements.txt
        # has no project to sync, so we make an explicit venv first.
        if (root / "pyproject.toml").is_file():
            commands.append("uv sync")
        elif (root / "requirements.txt").is_file():
            commands.append("uv venv && uv pip install -r requirements.txt")

    if (root / PROJECT_SETUP_RELPATH).is_file():
        # Always last: it exists to build on whatever the package manager just installed.
        commands.append(f"bash {PROJECT_SETUP_RELPATH}")
    return commands


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
    tmux_cmd = shlex.join(["tmux", "new-session", "-d", "-s", session, "-c", cwd, "bash", "-lc", inner])
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
    probe = f"{_source_env()}sleep {TMUX_SETTLE_SECONDS}; tmux has-session -t {shlex.quote('=' + session)} 2>/dev/null"
    if endpoint.run(probe, check=False).returncode == 0:
        return
    binary = shlex.split(command)[0] if command.strip() else command
    found = endpoint.run(f"{_source_env()}command -v {shlex.quote(binary)}", check=False)
    where = found.stdout.strip() if found.returncode == 0 else "not found on PATH"
    raise SSHError(
        f"remote tmux session {session!r} exited immediately after starting {command!r} "
        f"({binary}: {where}). The remote tooling is probably missing or broken — re-run with a fresh bootstrap."
    )


def tmux_attach_argv(endpoint: SSHEndpoint, session: str) -> list[str]:
    """Return the full local argv that attaches to a remote tmux session.

    Returned rather than executed so the caller can ``exec`` it and hand the tty straight to ssh.
    """
    # "=name" is tmux's exact-match target syntax; a bare name would fnmatch and could hit "fwd-ab" when asked for "fwd-a".
    remote = f"{_source_env()}tmux attach -t {shlex.quote('=' + session)}"
    return [*endpoint.ssh_argv(tty=True), remote]


def tmux_kill(endpoint: SSHEndpoint, session: str) -> None:
    """Kill a remote tmux session; no-op if it does not exist."""
    remote = f"{_source_env()}tmux kill-session -t {shlex.quote('=' + session)} 2>/dev/null || true"
    endpoint.run(remote, check=False)


def tmux_exists(endpoint: SSHEndpoint, session: str) -> bool:
    """Return whether a remote tmux session is alive (``tmux has-session``)."""
    remote = f"{_source_env()}tmux has-session -t {shlex.quote('=' + session)} 2>/dev/null"
    return endpoint.run(remote, check=False).returncode == 0
