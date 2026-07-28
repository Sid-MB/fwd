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
import secrets
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Sequence

from fwd import agents, backends, remote, sshexec, stop_after as stop_after_ops, sync, ui
from fwd.backends.base import Provisioner, TargetInfo, TargetStatus
from fwd.config import Config, ConfigError, TargetConfig, load_config
from fwd.state import SessionState, StateStore, endpoint_to_dict
from fwd.tooling import merge_requirements

# Length of the cwd digest appended to a derived session name. Six hex chars is ~16M values: plenty to separate the
# handful of checkouts one person has, short enough to stay readable in a tmux session name.
SESSION_HASH_LEN = 6


@dataclass(slots=True)
class _InterruptCleanup:
    """Track enough launch ownership to clean up safely when the user presses Ctrl-C."""

    store: StateStore
    session_name: str
    backend: Provisioner | None = None

    def cancel(self) -> None:
        """Remove an invocation-created resource, retain reused resources, and report remaining session count."""
        removed = False
        cleanup_error: Exception | None = None
        if self.backend is not None:
            try:
                removed = self.backend.cleanup_interrupted_provision(self.session_name)
            except Exception as exc:
                cleanup_error = exc
        if removed:
            self.store.remove(self.session_name)
        remaining = len(self.store.all())
        noun = "session" if remaining == 1 else "sessions"
        suffix = f"{remaining} {noun} still running"
        if cleanup_error is not None:
            disposition = "it remains tracked" if self.store.get(self.session_name) is not None else "it may still exist at the provider; check the provider console"
            ui.warn(f"startup canceled; could not remove the newly created session ({cleanup_error}); {disposition}; {suffix}")
        elif removed:
            ui.warn(f"startup canceled; removed newly created session {self.session_name!r}; {suffix}")
        else:
            ui.warn(f"startup canceled; no newly created resource was removed; {suffix}")

# Exact startup tokens with fwd-specific semantics. Keeping this as a lookup boundary makes future magic commands
# explicit instead of teaching the general arbitrary-command path to guess based on executable names.
MAGIC_CLAUDE_COMMAND: tuple[str, ...] = ("claude",)

# A commandless `fwd up` still creates a useful persistent tmux session, so a later `fwd attach` opens a normal shell.
REMOTE_SHELL_COMMAND = 'exec "${SHELL:-bash}" -l'

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


def derive_new_session_name(local_cwd: str | Path, reserved: set[str]) -> str:
    """Derive a fresh readable session name that cannot collide with locally tracked state.

    The stable directory-derived name remains the prefix so tables and provider consoles still identify the project.
    A random suffix is required rather than a numeric counter because local state may have been deleted while an old
    provider resource still exists; reusing that provider name would violate ``--new`` by silently adopting it.

    Args:
        local_cwd: Project directory used for the stable readable prefix.
        reserved: Session names already present in local state.
    """
    base = derive_session_name(local_cwd)
    while True:
        candidate = f"{base}-{secrets.token_hex(3)}"
        if candidate not in reserved:
            return candidate


def tmux_session_name(session_name: str) -> str:
    """Return the tmux session name for a fwd session, namespaced so fwd never touches a user's own sessions."""
    return f"fwd-{session_name}"


def _alias_matches(sessions: Sequence[SessionState], selector: str) -> list[SessionState]:
    """Return sessions matching a target label, falling back to a backend name only when no target label matches."""
    target_matches = [session for session in sessions if session.flags.get("target") == selector]
    return target_matches or [session for session in sessions if session.backend == selector]


def _selection_status(session: SessionState) -> TargetStatus:
    """Query status for alias disambiguation without emitting a misleading configuration error.

    Alias resolution only needs status when several saved sessions match. Reconstruct the backend from the config
    belonging to each session's project so cross-project aliases work, but return ``UNKNOWN`` when a target was
    renamed, deleted, or is otherwise ambiguous. An uncertain candidate must remain part of the ambiguity because it
    could still be running and billing.
    """
    try:
        cfg = load_config(Path(session.local_cwd))
        target_name = session.flags.get("target")
        target = cfg.targets.get(target_name) if isinstance(target_name, str) else None
        if target is None or target.backend != session.backend:
            backend_targets = [candidate for candidate in cfg.targets.values() if candidate.backend == session.backend]
            if len(backend_targets) != 1:
                return TargetStatus.UNKNOWN
            target = backend_targets[0]
        return status_of(backends.make_backend(target, cfg), session)
    except Exception:
        return TargetStatus.UNKNOWN


def choose_session(candidates: Sequence[SessionState], selector: str) -> SessionState:
    """Resolve an already-matched alias safely, preferring the sole active session instead of arbitrary recency.

    A single saved match is unambiguous even when stopped, which keeps ``attach --restart`` and ``rm`` useful. When
    several saved sessions match, one running or pending target may disambiguate them only if every other status is
    known not to be active. Otherwise require an exact session name; destructive and billing-sensitive commands must
    never guess which resource the user meant.
    """
    if not candidates:
        raise ValueError("choose_session requires at least one candidate")
    if len(candidates) == 1:
        return candidates[0]
    statuses = [(session, _selection_status(session)) for session in candidates]
    active = [session for session, status in statuses if status in (TargetStatus.RUNNING, TargetStatus.PENDING)]
    uncertain = any(status == TargetStatus.UNKNOWN for _, status in statuses)
    if len(active) == 1 and not uncertain:
        return active[0]
    details = ", ".join(f"{session.name} ({status.value})" for session, status in statuses)
    ui.die(
        f"{selector!r} matches multiple sessions: {details}. "
        f"Pass an exact session name; inspect all sessions with {ui.command('ls --all-projects')!r}."
    )


def resolve_session(name: str | None, *, required: bool = True) -> SessionState | None:
    """Look up an exact session, target/backend alias, or the current directory's session.

    The shared entry point for every command that operates on an existing session, so ``attach``, ``push``, ``stop``
    and ``rm`` all agree on both aliases and what "this directory's session" means.

    Args:
        name: Session name, target label, or backend name; ``None`` looks up by cwd.
        required: Abort with an actionable message when nothing is found.
    """
    st = store()
    session = st.get(name) if name else st.get_for_cwd(Path.cwd())
    if session is None and name:
        matches = _alias_matches(st.all(), name)
        if matches:
            session = choose_session(matches, name)
    if session is None and required:
        if name:
            known = ", ".join(s.name for s in st.all()) or "none"
            ui.die(f"no session, target, or backend matches {name!r} (known sessions: {known})")
        ui.die(f"no {ui.command()} sessions for this directory; run {ui.command('up')!r} to create one")
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
    :meth:`~fwd.sshexec.SSHEndpoint.exec_interactive` when that helper is unavailable. The Python process is replaced
    by either SSH directly or a tiny local shell that waits for SSH and prints follow-up commands after its closing
    line; attach I/O is never proxied through Python, so resize, mouse, and Ctrl-C remain native.

    Refuses up front without a tty. ``tmux attach`` cannot work on a pipe, so this is guaranteed to fail either way —
    but exec'ing into ssh to find out leaks two confusing lines from other tools (``Pseudo-terminal will not be
    allocated...`` / ``open terminal failed: not a terminal``) that say nothing about what the user should do
    instead. See docs/live-e2e-report.md, R2-2.
    """
    if not sys.stdin.isatty():
        ui.die(f"attach needs an interactive terminal; in scripts use {ui.command('up')!r} without --attach")
    try:
        argv = remote.tmux_attach_argv(endpoint, tmux_session, session_name)
    except NotImplementedError:
        argv = None
    if argv:
        os.execvp(argv[0], argv)
    endpoint.exec_interactive(remote.tmux_attach_command(tmux_session))


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
        ui.warn(f"could not determine the job id ({exc}); {ui.command('ls')!r} will rescan by name")
        return {}
    if not job_id:
        ui.info("job is queued; its id will be picked up on the next status check")
        return {}
    ui.info(f"slurm job {job_id}")
    return {"job_id": job_id}


def initial_command_for(session: SessionState) -> tuple[str, ...]:
    """Return the startup argv recorded for a session, treating older state files as Claude sessions."""
    recorded = session.flags.get("initial_command")
    if recorded is None:
        return MAGIC_CLAUDE_COMMAND
    return tuple(str(part) for part in recorded)


def build_standard_startup_command(initial_command: tuple[str, ...]) -> str:
    """Build the persistent tmux command for a shell, registered agent, or arbitrary argv.

    Registered agents already own a long-lived interactive process and must remain the pane's direct command. A
    successful finite arbitrary command instead falls through to a login shell so commands such as ``fwd up echo hi``
    do not make tmux disappear before launch verification; its output remains in the pane scrollback and the session
    stays useful. A non-zero command exits without the fallback shell so the liveness check still reports genuine
    startup failures rather than disguising them as ready sessions.
    """
    if not initial_command:
        return REMOTE_SHELL_COMMAND
    agent = agents.resolve(initial_command)
    if agent is not None:
        return shlex.join(agent.command)
    command = shlex.join(initial_command)
    persistent = f"({command}); status=$?; if [ \"$status\" -eq 0 ]; then exec \"${{SHELL:-bash}}\" -l; fi; exit \"$status\""
    return shlex.join(["bash", "-lc", persistent])


def startup_command_for(session: SessionState) -> str:
    """Rebuild the persistent tmux command for restart and allocation-recovery paths."""
    if session.flags.get("command_via_send"):
        return REMOTE_SHELL_COMMAND
    initial = initial_command_for(session)
    agent = agents.resolve(initial)
    if agent is not None:
        return agent.restart_command(session.flags)
    return build_standard_startup_command(initial)


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


def _sync_project(endpoint: sshexec.SSHEndpoint, local_cwd: Path, remote_dir: str, cfg: Config) -> None:
    """Mirror the project up, choosing rsync or the tar fallback based on what the transport supports."""
    if endpoint.supports_rsync:
        sync.sync_up(endpoint, local_cwd, remote_dir, cfg.sync, delete=cfg.sync.delete)
        return
    # RunPod's proxy transport cannot run a remote rsync binary, so delta transfer is lost entirely. Loud, because
    # the user is about to wonder why every push is slow.
    ui.warn("transport does not support rsync; falling back to tar-over-ssh (no delta transfer, slower pushes)")
    sync.tar_up(endpoint, local_cwd, remote_dir, cfg.sync)


def launch(
    target: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    *,
    new: bool = False,
    initial_command: tuple[str, ...] | None = MAGIC_CLAUDE_COMMAND,
    session: bool = False,
    handoff: bool = False,
    user_config: bool = False,
    creds: bool = False,
    attach: bool = False,
    push_only: bool = False,
    run_command_as_task: bool = False,
) -> SessionState:
    """Run the launch pipeline and safely clean up invocation-owned resources on Ctrl-C."""
    cleanup = _InterruptCleanup(store=store(), session_name=name or derive_session_name(Path.cwd()))
    try:
        return _launch(
            target=target,
            gpu=gpu,
            name=name,
            new=new,
            initial_command=initial_command,
            session=session,
            handoff=handoff,
            user_config=user_config,
            creds=creds,
            attach=attach,
            push_only=push_only,
            run_command_as_task=run_command_as_task,
            interrupt_cleanup=cleanup,
        )
    except KeyboardInterrupt:
        cleanup.cancel()
        raise


def _launch(
    target: str | None = None,
    gpu: str | None = None,
    name: str | None = None,
    *,
    new: bool = False,
    initial_command: tuple[str, ...] | None = MAGIC_CLAUDE_COMMAND,
    session: bool = False,
    handoff: bool = False,
    user_config: bool = False,
    creds: bool = False,
    attach: bool = False,
    push_only: bool = False,
    run_command_as_task: bool = False,
    interrupt_cleanup: _InterruptCleanup,
) -> SessionState:
    """Provision, sync and bootstrap a target, then start a persistent shell, command, or Claude session.

    Args:
        target: Target name from config; ``None`` resolves via the existing session, then ``default_target``.
        gpu: GPU override passed to the backend.
        name: Session name; defaults to :func:`derive_session_name` of the cwd.
        new: Create a fresh randomly suffixed session instead of reusing the current directory's session.
        initial_command: Remote argv to start inside tmux. ``None`` resolves the configured default for the selected
            target, empty starts a login shell, and exactly ``("claude",)`` enables fwd's transcript-aware Claude
            workflow. The public ``fwd up`` command passes ``None`` when no command or agent is given.
        session: Transfer the live transcript for ``claude --resume`` (best-effort).
        handoff: Generate and use ``HANDOFF.md`` instead of a transcript.
        user_config: Upload the user's Claude config bundle.
        creds: Lift local Claude credentials to the remote machine (warns).
        attach: Exec into the remote tmux session when everything is ready.
        push_only: Stop after syncing files, before bootstrap.
        run_command_as_task: Start a shell as the primary pane so the caller can run and stream ``initial_command``
            through the durable task manager after launch while retaining the original argv in session metadata.

    Returns:
        The persisted :class:`~fwd.state.SessionState`. Does not return when ``attach`` is ``True``, since the
        process is replaced by ssh.
    """
    local_cwd = Path.cwd().resolve()
    try:
        cfg = load_config(local_cwd)
    except ConfigError as exc:
        ui.die(str(exc))

    st = interrupt_cleanup.store
    if new and name is not None:
        ui.die(f"{ui.command('up')} accepts either --new or --name, not both")
    directory_session = st.get_for_cwd(local_cwd)
    if new:
        session_name = derive_new_session_name(local_cwd, {session.name for session in st.all()})
        existing = None
        target_hint = directory_session
    else:
        session_name = name or derive_session_name(local_cwd)
        # An explicit --name looks up exactly that name; otherwise a session already registered for this directory wins,
        # even if it was created under an older naming scheme.
        existing = st.get(session_name) if name else (directory_session or st.get(session_name))
        if existing is not None:
            session_name = existing.name
        target_hint = existing
    interrupt_cleanup.session_name = session_name

    target_cfg = _resolve_target_or_setup(cfg, target, target_hint, local_cwd)
    backend = backends.make_backend(target_cfg, cfg)
    interrupt_cleanup.backend = backend
    if initial_command is None:
        initial_command = cfg.command_for(target_cfg.name)
    agent = agents.resolve(initial_command)
    agent_options = agents.AgentLaunchOptions(session=session, handoff=handoff, user_config=user_config, creds=creds)
    flags: dict[str, Any] = {
        "session": False,
        "handoff": False,
        "user_config": False,
        "creds": False,
    }
    if agent is not None:
        try:
            flags.update(agent.launch_flags(cfg, agent_options))
        except ValueError as exc:
            ui.die(str(exc))
    elif agent_options.any():
        ui.die(f"--session, --handoff, --user-config, and --creds require a compatible coding agent such as {ui.command('up claude')!r}")
    flags["initial_command"] = list(initial_command)
    flags["command_via_send"] = run_command_as_task

    if new:
        replaced = f" instead of reusing {directory_session.name!r}" if directory_session is not None else ""
        ui.info(f"creating new session {session_name!r}{replaced}")
    elif existing is not None:
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
    # Persist as soon as a provider resource exists. Every remaining stage can fail independently (SSH readiness,
    # sync, bootstrap, dependency install, agent startup); delaying state until tmux succeeds would orphan a billable
    # pod that neither `fwd ls` nor `fwd stop` can see. The final persist below refreshes flags and late backend ids.
    flags["gpu"] = gpu
    _persist(st, session_name, target_cfg, local_cwd, remote_dir, endpoint, info, flags, preserve_started_at=True)
    ui.info(f"tracking provisioned instance as session {session_name!r}; stop it with {ui.command(f'stop {session_name}')!r} even if launch setup fails")

    # 2. Wait for sshd, then multiplex every later stage over a single connection.
    with ui.step(f"Waiting for SSH on {endpoint.host}:{endpoint.port}"):
        if not sshexec.wait_for_ssh(endpoint):
            ui.die(
                f"{endpoint.ssh_target()} did not become reachable; check the target is running and that your key is "
                f"authorized, then run {ui.command('doctor')!r}"
            )
    try:
        endpoint.open_control_master()
    except sshexec.SSHError as exc:
        # Multiplexing is an optimization; a launch without it is merely slower.
        ui.warn(f"ssh multiplexing unavailable, continuing without it ({exc})")

    # 3. Agent-local prep happens before sync because implementations may create project files such as HANDOFF.md.
    # The result is deliberately opaque and comes back to the same agent after bootstrap for any remote import.
    agent_local_state = agent.prepare_local(local_cwd, flags) if agent is not None else None

    # 4. Files up.
    with ui.step(f"Syncing {local_cwd.name} to {remote_dir}"):
        _sync_project(endpoint, local_cwd, remote_dir, cfg)

    if push_only:
        return _persist(st, session_name, target_cfg, local_cwd, remote_dir, endpoint, info, flags, preserve_started_at=True)

    # 5. Core environment plus only the project/agent tools this launch actually needs.
    tool_prefix = info.tool_prefix or f"{remote_dir.rstrip('/')}/.fwd-tools"
    with ui.step("Bootstrapping remote tooling"):
        remote.run_bootstrap(endpoint, tool_prefix=tool_prefix, remote_dir=remote_dir, scratch=info.scratch)

    project_plan = remote.detect_toolchain_plan(local_cwd)
    requirements = merge_requirements(project_plan.requirements, agent.tools if agent is not None else ())
    if requirements:
        with ui.step(f"Preparing remote tools ({len(requirements)} requirement(s))"):
            remote.ensure_tools(endpoint, requirements)

    # 6. Project dependencies, inferred by class-based toolchains; the project escape hatch remains last.
    dep_commands = project_plan.commands
    if dep_commands:
        with ui.step(f"Installing project dependencies ({len(dep_commands)} step(s))"):
            remote.run_dep_install(endpoint, remote_dir, dep_commands)
    else:
        ui.info("no supported project manifests detected; skipping dependency install")

    # 7. Let the selected agent install settings/state, then ask it for the persistent command. The orchestration has
    # no name-specific branches: new agents implement the same hooks and register themselves in fwd.agents.
    if agent is not None:
        flags.update(agent.prepare_remote(endpoint, remote_dir, flags, agent_local_state))

    flags["tool_prefix"] = tool_prefix
    if run_command_as_task:
        startup_cmd = REMOTE_SHELL_COMMAND
    elif agent is not None:
        startup_cmd = agent.startup_command(flags)
    else:
        startup_cmd = build_standard_startup_command(initial_command)
    if agent is not None:
        runtime_session = SessionState(
            name=session_name,
            backend=target_cfg.backend,
            local_cwd=str(local_cwd),
            remote_dir=remote_dir,
            tmux_session=tmux_session_name(session_name),
            endpoint=endpoint_to_dict(endpoint),
            backend_ids=dict(info.backend_ids),
            flags={**flags, "target": target_cfg.name},
        )
        try:
            action = stop_after_ops.prepare(endpoint, backend, runtime_session, agent_guidance=True)
            startup_cmd = stop_after_ops.with_agent_environment(startup_cmd, action)
            flags["stop_after_script"] = action
        except stop_after_ops.StopAfterUnsupported as exc:
            ui.warn(str(exc))
        except Exception as exc:
            ui.warn(f"could not install the remote stopafter helper for {agent.name}: {exc}")
    flags["gpu"] = gpu
    tmux_name = tmux_session_name(session_name)
    tmux_was_running = remote.tmux_exists(endpoint, tmux_name)
    if tmux_was_running:
        ui.info(f"remote tmux session {tmux_name!r} is already running; leaving it as is")
    else:
        tmux_cmd = build_tmux_command(backend, endpoint, session_name, remote_dir, tool_prefix, startup_cmd, gpu=gpu)
        with ui.step(f"Starting remote session {tmux_name!r}"):
            remote.tmux_new(endpoint, tmux_name, remote_dir, tmux_cmd)

    backend_ids = dict(info.backend_ids)
    backend_ids.update(track_job_id(backend, endpoint, session_name))
    info.backend_ids = backend_ids

    state = _persist(st, session_name, target_cfg, local_cwd, remote_dir, endpoint, info, flags, preserve_started_at=tmux_was_running)
    if not attach:
        ui.ok(f"session {session_name!r} ready; attach with {ui.command(f'attach {session_name}')!r}")
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
    *,
    preserve_started_at: bool,
) -> SessionState:
    """Write (or refresh) the session's state entry and return it.

    ``created_at`` and ``last_attached`` are preserved across reruns so a repaired session keeps its history.
    ``started_at`` is preserved only if the same tmux session was already alive; creating a new tmux session begins a
    new locally tracked run. The endpoint and backend ids are always overwritten because those churn on pod restarts.
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
        if preserve_started_at:
            state.started_at = previous.started_at
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
