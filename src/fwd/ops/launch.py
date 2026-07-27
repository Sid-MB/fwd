"""Launch orchestration — the canonical order of operations for standing up a remote session.

Design intent
-------------
This module owns the *sequence*, and nothing else. Every individual action (transfer files, install tooling, move
Claude state, create tmux) lives in a mechanical module; ``launch`` decides what happens in what order and what to do
when a stage fails. Keeping the ordering in one readable function is the payoff of the ``Provisioner`` boundary: the
same seven steps work for a static SSH box, a RunPod pod and a Slurm allocation.

Two properties are load-bearing and every change here must preserve them:

**Idempotency.** ``fwd up`` is also the repair command. A launch that died during bootstrap must be fixable by running
the exact same command again, so every stage is either naturally idempotent (rsync, bootstrap's marker file, dep
installs from lockfiles) or explicitly guarded (tmux is only created when ``tmux_exists`` says it is missing). Nothing
here refuses to run because a previous attempt got partway.

**Ordering constraints that are not obvious.** Three of them:

- The ControlMaster opens immediately after ``wait_for_ssh`` because stages 3-7 are six-plus separate ssh
  invocations; without multiplexing each one re-authenticates.
- ``HANDOFF.md`` is generated *before* the sync, not alongside the other Claude-state steps. It is a file in the
  project tree, so if it were written after the mirror it would simply never reach the remote.
- The transcript bundle is exported locally before the sync but *imported* after bootstrap, because the import needs
  a remote home directory that bootstrap may have just created.

This module also hosts the shared session-resolution helpers (:func:`store`, :func:`derive_session_name`,
:func:`resolve_session`, :func:`backend_for`, :func:`exec_attach`). ``attach``/``lifecycle``/``transfer`` import them
from here rather than from a fifth module, which keeps the ops import graph a star with no cycles.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NoReturn

from fwd import agents, backends, claude_state, remote, sshexec, sync, ui
from fwd.backends.base import Provisioner, TargetInfo, TargetStatus
from fwd.config import Config, ConfigError, TargetConfig, load_config
from fwd.state import SessionState, StateStore, endpoint_to_dict

# Length of the cwd digest appended to a derived session name. Six hex chars is ~16M values: plenty to separate the
# handful of checkouts one person has, short enough to stay readable in a tmux session name.
SESSION_HASH_LEN = 6

# The prompt handed to claude when context arrives as a document rather than a transcript.
HANDOFF_PROMPT = "Read HANDOFF.md, then continue the work it describes"

# Exact startup tokens with fwd-specific semantics. Keeping this as a lookup boundary makes future magic commands
# explicit instead of teaching the general arbitrary-command path to guess based on executable names.
MAGIC_CLAUDE_COMMAND: tuple[str, ...] = ("claude",)
MAGIC_CODEX_COMMAND: tuple[str, ...] = ("codex",)

# A commandless `fwd up` still creates a useful persistent tmux session, so a later `fwd attach` opens a normal shell.
REMOTE_SHELL_COMMAND = 'exec "${SHELL:-bash}" -l'

# How long a generated HANDOFF.md stays reusable. Regenerating costs a full ``claude -p`` round trip (~65 s measured
# in the live e2e), which a repair rerun of ``fwd up`` should not have to pay again.
HANDOFF_MAX_AGE_SECONDS = 15 * 60


def store() -> StateStore:
    """Return the session store.

    A function rather than a module-level singleton so tests can redirect state to a tmp path with one monkeypatch,
    and so no import-time filesystem access happens just because ``fwd --help`` was typed.
    """
    return StateStore()


def derive_session_name(local_cwd: str | Path) -> str:
    """Derive a stable default session name from a working directory.

    Format is ``<slug>-<6 hex>``: the directory basename plus a digest of the *absolute* path. The digest is what
    makes this correct — two checkouts of the same repo (``~/work/api`` and ``~/scratch/api``) must not collide onto
    one remote session, while the same directory must always resolve to the same name across invocations so reattach
    and reuse work without the user ever passing ``--name``.

    The slug is sanitized to ``[a-z0-9_-]`` because the result becomes both a tmux session name and a provider
    resource name, and both dislike dots, spaces and colons.
    """
    path = Path(local_cwd).expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", path.name).strip("-").lower() or "project"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:SESSION_HASH_LEN]
    return f"{slug}-{digest}"


def tmux_session_name(session_name: str) -> str:
    """Return the tmux session name for a fwd session, namespaced so fwd never touches a user's own sessions."""
    return f"fwd-{session_name}"


def resolve_session(name: str | None, *, required: bool = True) -> SessionState | None:
    """Look up a session by explicit name, else by the current working directory.

    The shared entry point for every command that operates on an existing session, so ``attach``, ``push``, ``stop``
    and ``rm`` all agree on what "this directory's session" means.

    Args:
        name: Explicit session name, or ``None`` to look up by cwd.
        required: Abort with an actionable message when nothing is found.
    """
    st = store()
    session = st.get(name) if name else st.get_for_cwd(Path.cwd())
    if session is None and required:
        if name:
            known = ", ".join(s.name for s in st.all()) or "none"
            ui.die(f"no session named {name!r} (known sessions: {known})")
        ui.die("no fwd session for this directory; run 'fwd up' to create one")
    return session


def backend_for(session: SessionState, *, config: Config | None = None) -> Provisioner:
    """Reconstruct the backend that owns an existing session.

    The target name is recorded in ``flags["target"]`` at launch. When that target has since been renamed or deleted
    we fall back to the first configured target with the same backend type, because a session's *backend* is a fact
    about the remote resource while its target config is only how we reached it. Failing outright would leave the
    user unable to even ``stop`` a running pod after tidying their config.
    """
    cfg = config if config is not None else load_config(Path.cwd())
    target_name = session.flags.get("target")
    target: TargetConfig | None
    try:
        target = cfg.target(target_name)
    except ConfigError:
        target = None
    if target is None or target.backend != session.backend:
        matches = [t for t in cfg.targets.values() if t.backend == session.backend]
        if not matches:
            ui.die(
                f"session {session.name!r} uses the {session.backend!r} backend but no such target is configured; "
                f"re-add it to your config to manage this session"
            )
        target = matches[0]
    return backends.make_backend(target, cfg)


def exec_attach(endpoint: sshexec.SSHEndpoint, tmux_session: str, session_name: str | None = None) -> NoReturn:
    """Replace this process with an interactive attach to a remote tmux session.

    Prefers :func:`fwd.remote.tmux_attach_argv` so remote-command construction stays in one place, falling back to
    :meth:`~fwd.sshexec.SSHEndpoint.exec_interactive` when that helper is unavailable. Either way the Python process
    is *replaced*: attach I/O is never proxied through Python, which is what keeps resize, mouse and ctrl-C native.

    Refuses up front without a tty. ``tmux attach`` cannot work on a pipe, so this is guaranteed to fail either way —
    but exec'ing into ssh to find out leaks two confusing lines from other tools (``Pseudo-terminal will not be
    allocated...`` / ``open terminal failed: not a terminal``) that say nothing about what the user should do
    instead. See docs/live-e2e-report.md, R2-2.
    """
    if not sys.stdin.isatty():
        ui.die("attach needs an interactive terminal; in scripts use 'fwd up' without --attach")
    try:
        argv = remote.tmux_attach_argv(endpoint, tmux_session, session_name)
    except NotImplementedError:
        argv = None
    if argv:
        os.execvp(argv[0], argv)
    endpoint.exec_interactive(remote.tmux_attach_command(tmux_session, session_name))


def build_claude_command(*, resume_id: str | None, use_handoff: bool) -> str:
    """Build the ``claude`` invocation tmux will run.

    Three escalating levels of carried context, matching the plan's flags: resume a real transcript, start fresh but
    point claude at a handoff document, or start clean.
    """
    if resume_id:
        return f"claude --resume {shlex.quote(resume_id)}"
    if use_handoff:
        return f"claude {shlex.quote(HANDOFF_PROMPT)}"
    return "claude"


def build_tmux_command(
    backend: Provisioner,
    endpoint: sshexec.SSHEndpoint,
    session_name: str,
    remote_dir: str,
    tool_prefix: str | None,
    claude_cmd: str,
    *,
    gpu: str | None = None,
) -> str:
    """Build the shell command the remote tmux session will run.

    Backends may override this through an optional ``claude_launch_wrapper(endpoint, session_name, remote_dir,
    claude_cmd, *, tool_prefix=None, gpu=None) -> str`` hook, dispatched by ``hasattr``. Slurm is the only backend
    that needs it: there tmux must not run ``claude`` directly but ``bash <remote_dir>/.fwd/job.sh``, a generated
    script that performs env setup and wraps the command in ``salloc ... srun --pty``. The hook has the side effect
    of writing that script remotely, which is why it takes an endpoint and is called here rather than being a pure
    string builder.

    The default (ssh, runpod) sources bootstrap's generated ``fwd-env.sh`` — putting the toolchain installed under
    ``tool_prefix`` on PATH and pointing caches at scratch — changes into the project directory, then ``exec``s
    claude so no useless parent shell lingers.
    """
    wrapper = getattr(backend, "claude_launch_wrapper", None)
    if callable(wrapper):
        return wrapper(endpoint, session_name, remote_dir, claude_cmd, tool_prefix=tool_prefix, gpu=gpu)

    parts: list[str] = []
    if tool_prefix:
        env_file = f"{tool_prefix.rstrip('/')}/fwd-env.sh"
        # Best-effort: a target bootstrapped by an older fwd may not have the file yet.
        parts.append(f". {shlex.quote(env_file)} 2>/dev/null || true")
    parts.append(f"cd {shlex.quote(remote_dir)} || exit 1")
    parts.append(f"exec {claude_cmd}")
    return f"bash -lc {shlex.quote('; '.join(parts))}"


def track_job_id(backend: Provisioner, endpoint: sshexec.SSHEndpoint, session_name: str) -> dict[str, str]:
    """Return backend ids discovered after the tmux session starts, currently just Slurm's job id.

    Dispatched by ``hasattr`` like the launch wrapper, because only Slurm has a queued resource whose identity is not
    known until *after* the session is running: the job id appears in ``squeue`` once ``salloc`` inside tmux has been
    accepted. ``None`` is the normal answer on a busy cluster where the job is still queueing, and it must never fail
    a launch — the backend rescans by name later.
    """
    finder = getattr(backend, "find_job_id", None)
    if not callable(finder):
        return {}
    try:
        job_id = finder(endpoint, session_name)
    except Exception as exc:
        ui.warn(f"could not determine the job id ({exc}); 'fwd ls' will rescan by name")
        return {}
    if not job_id:
        ui.info("job is queued; its id will be picked up on the next status check")
        return {}
    ui.info(f"slurm job {job_id}")
    return {"job_id": job_id}


def claude_command_for(session: SessionState) -> str:
    """Rebuild the ``claude`` invocation for an existing session from its recorded launch flags.

    Used by relaunch paths that must not redo the Claude state transfer: the transcript is already installed remotely
    and ``HANDOFF.md`` is already in the synced tree, so a restart only needs to point claude at them again.
    """
    return build_claude_command(
        resume_id=session.flags.get("resume_id"),
        use_handoff=bool(session.flags.get("handoff")) and not session.flags.get("resume_id"),
    )


def initial_command_for(session: SessionState) -> tuple[str, ...]:
    """Return the startup argv recorded for a session, treating older state files as Claude sessions."""
    recorded = session.flags.get("initial_command")
    if recorded is None:
        return MAGIC_CLAUDE_COMMAND
    return tuple(str(part) for part in recorded)


def startup_command_for(session: SessionState) -> str:
    """Rebuild the persistent tmux command for restart and allocation-recovery paths."""
    initial = initial_command_for(session)
    if initial == MAGIC_CLAUDE_COMMAND:
        return claude_command_for(session)
    if not initial:
        return REMOTE_SHELL_COMMAND
    return shlex.join(initial)


def _resolve_claude_flags(
    cfg: Config,
    *,
    session: bool,
    handoff: bool,
    user_config: bool,
    creds: bool,
) -> dict[str, bool]:
    """Merge command-line Claude flags with their config defaults.

    ``session`` and ``handoff`` are alternatives rather than additives: transferring the real transcript already
    carries the context a handoff document would only summarize. Since the S1 spike proved transcript relocation
    works, ``session`` is the default and ``--handoff`` is the explicit opt-out — passing it forces handoff mode and
    suppresses the transfer entirely, which is what a user wants when the conversation is long and they only need the
    conclusions. ``--session`` re-enables the transfer when config has turned it off.

    The runtime fallback chain (session → handoff → plain claude) is applied later in :func:`launch`, because it can
    only be resolved once the export and import have actually been attempted.
    """
    if handoff:
        want_session, want_handoff = False, True
    elif session:
        want_session, want_handoff = True, cfg.claude.handoff
    else:
        want_session, want_handoff = cfg.claude.session, cfg.claude.handoff
    return {
        "session": want_session,
        "handoff": want_handoff,
        "user_config": user_config or cfg.claude.user_config,
        "creds": creds or cfg.claude.creds,
    }


def _resolve_target(cfg: Config, requested: str | None, existing: SessionState | None) -> TargetConfig:
    """Pick the target config: explicit flag, else the existing session's recorded target, else the config default.

    Honouring the existing session's target matters for reuse — a second ``fwd up`` in a directory must land on the
    same machine even if the user has since changed ``default_target``.
    """
    if requested:
        return cfg.target(requested)
    if existing is not None:
        recorded = existing.flags.get("target")
        if recorded:
            try:
                return cfg.target(recorded)
            except ConfigError:
                # Renamed or removed since launch; fall through to normal default resolution.
                ui.warn(f"target {recorded!r} from the existing session is no longer configured; using the default")
    return cfg.target(None)


def _resolve_target_or_setup(cfg: Config, requested: str | None, existing: SessionState | None, local_cwd: Path) -> TargetConfig:
    """Resolve a target, running first-time setup when bare ``fwd`` has no configured choice.

    Setup is attempted only when no target was requested, no targets exist, and ordinary resolution failed. Explicit
    ``fwd up --target ...`` calls continue to use in-memory inference without writing config, while typos and ambiguous
    configured targets keep their specific errors. The wizard chooses its own interaction mode: terminals prompt,
    whereas agents and redirected callers receive actionable missing-flag errors instead of blocking.
    """
    try:
        return _resolve_target(cfg, requested, existing)
    except ConfigError as exc:
        if requested or cfg.targets:
            ui.die(str(exc))
        from fwd import wizard

        ui.info("no saved target found; starting first-time setup")
        wizard.run_wizard()
        try:
            return _resolve_target(load_config(local_cwd), requested, existing)
        except ConfigError as retry_exc:
            ui.die(str(retry_exc))


def _fresh_handoff(local_cwd: Path) -> Path | None:
    """Return an existing ``HANDOFF.md`` if it is recent enough to reuse, else ``None``.

    Generating a handoff shells out to ``claude -p``, which the live e2e measured at **64 seconds** — by far the most
    expensive stage of a launch. Since ``fwd up`` doubles as the repair command, a user fixing a failed launch would
    otherwise pay that minute again on every retry, to regenerate a summary of a conversation that has not changed.

    Fifteen minutes is chosen to cover the realistic repair loop (retry, tweak config, retry again) while still
    regenerating for a genuinely new session later in the day. Deleting the file forces regeneration, which is the
    obvious escape hatch and is mentioned in the message the caller prints.
    """
    handoff = local_cwd / "HANDOFF.md"
    if not handoff.is_file():
        return None
    try:
        age = time.time() - handoff.stat().st_mtime
    except OSError:
        return None
    return handoff if 0 <= age < HANDOFF_MAX_AGE_SECONDS else None


def _sync_project(endpoint: sshexec.SSHEndpoint, local_cwd: Path, remote_dir: str, cfg: Config) -> None:
    """Mirror the project up, choosing rsync or the tar fallback based on what the transport supports."""
    if endpoint.supports_rsync:
        sync.sync_up(endpoint, local_cwd, remote_dir, cfg.sync, delete=cfg.sync.delete)
        return
    # RunPod's proxy transport cannot run a remote rsync binary, so delta transfer is lost entirely. Loud, because
    # the user is about to wonder why every push is slow.
    ui.warn("transport does not support rsync; falling back to tar-over-ssh (no delta transfer, slower pushes)")
    sync.tar_up(endpoint, local_cwd, remote_dir, cfg.sync)


def _transfer_claude_state(
    endpoint: sshexec.SSHEndpoint,
    remote_dir: str,
    flags: dict[str, bool],
    bundle: Path | None,
) -> str | None:
    """Run the opt-in Claude state steps and return a session id to resume, if any.

    Each step is independently failure-tolerant: a session that starts without your skills directory is inconvenient,
    but a launch that aborts three minutes in over one optional upload is worse. The transcript import is the step
    that most often fails (foreign-session validation tightened in claude >= 2.1.9), and the caller downgrades that
    to a warning and falls back to a handoff or a clean session.
    """
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

    if bundle is None:
        return None
    with ui.step("Importing Claude session transcript"):
        remote_home = endpoint.run('printf %s "$HOME"').stdout.strip() or f"/home/{endpoint.user}"
        return claude_state.import_session_bundle(endpoint, bundle, remote_dir, remote_home)


def launch(
    target: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    *,
    initial_command: tuple[str, ...] | None = MAGIC_CLAUDE_COMMAND,
    session: bool = False,
    handoff: bool = False,
    user_config: bool = False,
    creds: bool = False,
    attach: bool = False,
    push_only: bool = False,
) -> SessionState:
    """Provision, sync and bootstrap a target, then start a persistent shell, command, or Claude session.

    Args:
        target: Target name from config; ``None`` resolves via the existing session, then ``default_target``.
        gpu: GPU override passed to the backend.
        name: Session name; defaults to :func:`derive_session_name` of the cwd.
        initial_command: Remote argv to start inside tmux. ``None`` resolves the configured default for the selected
            target, empty starts a login shell, and exactly ``("claude",)`` enables fwd's transcript-aware Claude
            workflow. The public ``fwd up`` command explicitly passes an empty tuple when no command is given.
        session: Transfer the live transcript for ``claude --resume`` (best-effort).
        handoff: Generate and use ``HANDOFF.md`` instead of a transcript.
        user_config: Upload the user's Claude config bundle.
        creds: Lift local Claude credentials to the remote machine (warns).
        attach: Exec into the remote tmux session when everything is ready.
        push_only: Stop after syncing files, before bootstrap.

    Returns:
        The persisted :class:`~fwd.state.SessionState`. Does not return when ``attach`` is ``True``, since the
        process is replaced by ssh.
    """
    local_cwd = Path.cwd().resolve()
    try:
        cfg = load_config(local_cwd)
    except ConfigError as exc:
        ui.die(str(exc))

    st = store()
    session_name = name or derive_session_name(local_cwd)
    # An explicit --name looks up exactly that name; otherwise a session already registered for this directory wins,
    # even if it was created under an older naming scheme.
    existing = st.get(session_name) if name else (st.get_for_cwd(local_cwd) or st.get(session_name))
    if existing is not None:
        session_name = existing.name

    target_cfg = _resolve_target_or_setup(cfg, target, existing, local_cwd)
    backend = backends.make_backend(target_cfg, cfg)
    if initial_command is None:
        initial_command = cfg.command_for(target_cfg.name)
    agent = agents.resolve(initial_command)
    is_claude = agent is not None and agent.name == "claude"
    if not is_claude and any((session, handoff, user_config, creds)):
        ui.die("--session, --handoff, --user-config, and --creds are only valid with 'fwd up claude'")
    flags = _resolve_claude_flags(cfg, session=session, handoff=handoff, user_config=user_config, creds=creds) if is_claude else {
        "session": False,
        "handoff": False,
        "user_config": False,
        "creds": False,
    }
    flags["initial_command"] = list(initial_command)

    if existing is not None:
        ui.info(f"reusing session {session_name!r} on target {target_cfg.name!r}")

    # 1. Provision. Contractually reuse-or-create per session name, including starting a stopped target, so this is
    # also the restart path for a session whose pod was stopped or whose allocation ended.
    with ui.step(f"Provisioning {target_cfg.backend} target {target_cfg.name!r}"):
        info: TargetInfo = backend.provision(session_name, local_cwd.name, gpu=gpu)
    for note in info.notes:
        ui.warn(note)
    endpoint = info.endpoint
    remote_dir = info.remote_dir
    provider_identity = " ".join(f"{key}={value}" for key, value in sorted(info.backend_ids.items()))
    ui.info(f"resolved target {target_cfg.name!r} to {target_cfg.backend} instance {endpoint.ssh_target()}:{endpoint.port}" + (f" ({provider_identity})" if provider_identity else ""))

    # 2. Wait for sshd, then multiplex every later stage over a single connection.
    with ui.step(f"Waiting for SSH on {endpoint.host}:{endpoint.port}"):
        if not sshexec.wait_for_ssh(endpoint):
            ui.die(
                f"{endpoint.ssh_target()} did not become reachable; check the target is running and that your key is "
                f"authorized, then run 'fwd doctor'"
            )
    try:
        endpoint.open_control_master()
    except sshexec.SSHError as exc:
        # Multiplexing is an optimization; a launch without it is merely slower.
        ui.warn(f"ssh multiplexing unavailable, continuing without it ({exc})")

    # 3. Magic-Claude local prep, before the sync. General commands skip this entire concern. HANDOFF.md must be inside
    # the tree that gets mirrored, and the transcript bundle is cheapest to export while touching local disk.
    bundle: Path | None = None
    if flags["session"]:
        with ui.step("Exporting Claude session transcript"):
            bundle = claude_state.export_session_bundle(local_cwd, Path(tempfile.mkdtemp(prefix="fwd-session-")))
        if bundle is None:
            # First rung of the fallback chain. The export already warned about the specific reason (no transcript
            # for this directory, usually), so only the consequence is reported here.
            flags["session"] = False
            if flags["handoff"]:
                ui.info("falling back to a handoff document")
    if flags["handoff"]:
        existing_handoff = _fresh_handoff(local_cwd)
        if existing_handoff is not None:
            age = (time.time() - existing_handoff.stat().st_mtime) / 60
            ui.info(f"reusing HANDOFF.md from {age:.0f} min ago (delete it to force regeneration)")
        else:
            with ui.step("Generating HANDOFF.md"):
                # Never returns None and never raises: a CLI failure yields a TODO-marked template instead.
                claude_state.make_handoff(local_cwd)

    # 4. Files up.
    with ui.step(f"Syncing {local_cwd.name} to {remote_dir}"):
        _sync_project(endpoint, local_cwd, remote_dir, cfg)

    if push_only:
        return _persist(st, session_name, target_cfg, local_cwd, remote_dir, endpoint, info, flags)

    # 5. Tooling. Idempotent remotely via bootstrap's version-stamped marker, so reruns are nearly free.
    tool_prefix = info.tool_prefix or f"{remote_dir.rstrip('/')}/.fwd-tools"
    with ui.step("Bootstrapping remote tooling"):
        remote.run_bootstrap(endpoint, tool_prefix=tool_prefix, remote_dir=remote_dir, scratch=info.scratch, agent=agent.name if agent else None)

    # 6. Project dependencies, inferred from lockfiles rather than configured.
    dep_commands = remote.detect_dep_commands(local_cwd)
    if dep_commands:
        with ui.step(f"Installing project dependencies ({len(dep_commands)} step(s))"):
            remote.run_dep_install(endpoint, remote_dir, dep_commands)
    else:
        ui.info("no lockfiles detected; skipping dependency install")

    # 7. Optional Claude state, then the persistent shell/command itself.
    resume_id = _transfer_claude_state(endpoint, remote_dir, flags, bundle) if is_claude else None
    if agent is not None and agent.sync_settings is not None:
        with ui.step(f"Uploading {agent.name.title()} settings and skills"):
            agent.sync_settings(endpoint)
    if is_claude and flags["session"] and not resume_id:
        # Second rung: the import failed remotely and already warned why. HANDOFF.md is only available if it was
        # generated pre-sync, so if handoff was off this degrades to a clean session rather than silently pretending.
        if flags["handoff"]:
            ui.warn("could not install the transcript remotely; the session will start from HANDOFF.md instead")
        else:
            ui.warn("could not install the transcript remotely; starting a fresh session (try 'fwd up --handoff')")

    startup_cmd = (
        build_claude_command(resume_id=resume_id, use_handoff=flags["handoff"] and not resume_id)
        if is_claude
        else (REMOTE_SHELL_COMMAND if not initial_command else shlex.join(agent.command if agent is not None else initial_command))
    )
    # Recorded so a later relaunch in a fresh process can rebuild the same command without redoing the transfer.
    flags["resume_id"] = resume_id
    flags["gpu"] = gpu
    flags["tool_prefix"] = tool_prefix
    tmux_name = tmux_session_name(session_name)
    if remote.tmux_exists(endpoint, tmux_name):
        ui.info(f"remote tmux session {tmux_name!r} is already running; leaving it as is")
    else:
        tmux_cmd = build_tmux_command(backend, endpoint, session_name, remote_dir, tool_prefix, startup_cmd, gpu=gpu)
        with ui.step(f"Starting remote session {tmux_name!r}"):
            remote.tmux_new(endpoint, tmux_name, remote_dir, tmux_cmd)

    backend_ids = dict(info.backend_ids)
    backend_ids.update(track_job_id(backend, endpoint, session_name))
    info.backend_ids = backend_ids

    state = _persist(st, session_name, target_cfg, local_cwd, remote_dir, endpoint, info, flags)
    if not attach:
        ui.ok(f"session {session_name!r} ready; attach with 'fwd attach {session_name}'")
        return state

    state.touch_attached()
    st.upsert(state)
    exec_attach(endpoint, tmux_name, session_name)


def _persist(
    st: StateStore,
    session_name: str,
    target_cfg: TargetConfig,
    local_cwd: Path,
    remote_dir: str,
    endpoint: sshexec.SSHEndpoint,
    info: TargetInfo,
    flags: dict[str, Any],
) -> SessionState:
    """Write (or refresh) the session's state entry and return it.

    ``created_at`` and ``last_attached`` are preserved across reruns so a repaired session keeps its history, while
    the endpoint and backend ids are always overwritten — those are exactly the fields that churn on a pod restart.
    """
    previous = st.get(session_name)
    state = SessionState(
        name=session_name,
        backend=target_cfg.backend,
        local_cwd=str(local_cwd),
        remote_dir=remote_dir,
        tmux_session=tmux_session_name(session_name),
        endpoint=endpoint_to_dict(endpoint),
        backend_ids=dict(info.backend_ids),
        flags={**flags, "target": target_cfg.name},
    )
    if previous is not None:
        state.created_at = previous.created_at
        state.last_attached = previous.last_attached
    st.upsert(state)
    return state


def status_of(backend: Provisioner, session: SessionState) -> TargetStatus:
    """Query a backend's status, mapping a failing backend onto ``UNKNOWN`` and an unimplemented one onto ``RUNNING``.

    Shared by ``attach`` and ``ls`` so both reconcile identically. A backend that raises is treated as "cannot
    determine" rather than propagating, because status is advisory — the user still needs the command they typed to do
    something sensible. ``NotImplementedError`` is distinguished on purpose: during development that means "this
    backend cannot answer yet", and assuming anything worse would make fwd nag about healthy sessions.

    An unexpected exception maps to ``UNKNOWN``, never ``GONE`` (docs/live-e2e-report.md, R2-1). Only a backend that
    *affirmatively* confirms the resource is missing may return ``GONE``, because that is the value which unlocks the
    offer to delete the user's session entry.
    """
    try:
        return backend.status(session)
    except NotImplementedError:
        return TargetStatus.RUNNING
    except Exception:
        return TargetStatus.UNKNOWN
