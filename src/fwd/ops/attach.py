"""Attach and the bare-``fwd`` smart default.

Design intent
-------------
Attach is the reconciliation point of the whole tool. State is only a cache of what fwd believed last time, and
reality drifts underneath it constantly: pods get stopped from the RunPod console, Slurm allocations hit their time
limit, someone kills a tmux session by hand. So attach never trusts stored state — it re-resolves the endpoint through
the backend (RunPod hands out a new IP on every restart) and branches on live ``status()``:

- ``RUNNING``   → attach, stamping ``last_attached``. If launch preparation failed before tmux was created, ``--raw`` starts a plain recovery shell in the synced project instead of rerunning preparation.
- ``PENDING``   → attach anyway. On a busy cluster a queued allocation is normal and can last hours, and the tmux pane
  shows ``salloc``'s queue position, so attaching is genuinely useful rather than an error.
- ``STOPPED``   → offer a full ``launch`` relaunch. A restarted pod has a wiped container disk, so only the complete
  pipeline (bootstrap, deps, Claude state) repairs it.
- ``JOB_ENDED`` → offer an *in-place* allocation restart: kill the stale tmux pane, ask the backend for a fresh job
  wrapper, start tmux again. Deliberately not a full relaunch — the login node and the shared filesystem are
  untouched, so re-syncing and re-bootstrapping would waste minutes to achieve nothing.
- ``GONE``      → the resource is gone upstream; offer to prune the stale state entry.

Every branch that would start billable compute goes through :func:`_confirm_restart`, which refuses to act on a
default when there is no tty. See its docstring for why that is not paranoia.

The final act is always ``exec``: the Python process is replaced by ssh, so the terminal genuinely belongs to the
remote tmux rather than being a pipe fwd copies bytes through.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import NoReturn

import typer

from fwd import agents, github_auth, remote, stop_after as stop_after_ops, ui
from fwd.backends.base import TargetStatus
from fwd.config import ConfigError
from fwd.ops import launch as launch_ops
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.state import SessionState, endpoint_to_dict


def _relaunch(
    session: SessionState,
    *,
    forward_ports: tuple[str, ...] | None = None,
    setup_github: bool | None = None,
    control_mode: bool = False,
) -> NoReturn:
    """Re-run the full launch pipeline for an existing session, reusing its recorded launch flags.

    Never returns: ``launch`` execs into attach on success.
    """
    flags = session.flags
    launch_ops.launch(
        target=flags.get("target"),
        machine=flags.get("machine"),
        gpu=flags.get("gpu"),
        name=session.name,
        initial_command=launch_ops.initial_command_for(session),
        session=bool(flags.get("session")),
        handoff=bool(flags.get("handoff")),
        user_config=bool(flags.get("user_config")),
        creds=bool(flags.get("creds")),
        setup_github=setup_github,
        attach=True,
        control_mode=control_mode,
        forward_ports=forward_ports,
    )
    raise typer.Exit(0)


def _restart_allocation(backend, endpoint: SSHEndpoint, session: SessionState) -> None:
    """Request a fresh compute allocation for a session whose job ended, without redoing the launch.

    The stale tmux session must be killed first: its pane holds a finished ``salloc``, and reusing it would attach
    the user to a dead shell. Sync and bootstrap are skipped on purpose — the cluster filesystem is shared and still
    holds everything from the original launch.
    """
    tool_prefix = session.flags.get("tool_prefix")
    startup_cmd = launch_ops.startup_command_for(session)
    agent = agents.resolve(launch_ops.initial_command_for(session))
    if agent is not None:
        try:
            action = stop_after_ops.prepare(endpoint, backend, session, agent_guidance=True)
            startup_cmd = stop_after_ops.with_agent_environment(startup_cmd, action)
            session.flags["stop_after_script"] = action
        except stop_after_ops.StopAfterUnsupported as exc:
            ui.warn(str(exc))
        except Exception as exc:
            ui.warn(f"could not install the remote stopafter helper for {agent.name}: {exc}")
    with ui.step("Killing the stale tmux session"):
        remote.tmux_kill(endpoint, session.tmux_session)
    with ui.step("Requesting a new allocation"):
        tmux_cmd = launch_ops.build_tmux_command(
            backend,
            endpoint,
            session.name,
            session.remote_dir,
            tool_prefix,
            startup_cmd,
            gpu=session.flags.get("gpu"),
        )
        remote.tmux_new(endpoint, session.tmux_session, session.remote_dir, tmux_cmd)

    job_ids = launch_ops.track_job_id(backend, endpoint, session.name)
    if job_ids:
        session.backend_ids = {**session.backend_ids, **job_ids}
    launch_ops.store().update(session.name, backend_ids=session.backend_ids, flags=session.flags)


def _tmux_alive(endpoint: SSHEndpoint, tmux_session: str) -> bool:
    """Return whether the remote tmux session exists, treating an unavailable check as "assume alive".

    Optimistic on purpose: when we cannot tell, attaching and letting tmux report the truth beats wrongly offering to
    rebuild a perfectly healthy session.
    """
    try:
        return remote.tmux_exists(endpoint, tmux_session)
    except NotImplementedError:
        return True
    except Exception:
        return True


def _start_raw_shell(endpoint: SSHEndpoint, session: SessionState) -> None:
    """Create the missing primary tmux session as a plain login shell without rerunning launch preparation.

    This recovery path intentionally skips sync, tool resolution, dependency installation, project setup, and agent startup. It exists so a user can enter an already-running target after one of those launch stages failed, repair the remote environment manually, exit the recovery shell, and rerun the normal launch.
    """
    with ui.step("Starting raw recovery shell"):
        remote.tmux_new(endpoint, session.tmux_session, session.remote_dir, launch_ops.REMOTE_SHELL_COMMAND)


def _confirm_restart(prompt: str, *, restart: bool, action: str) -> None:
    """Gate any action that starts billable compute, or abort with an actionable message.

    The live e2e run (dev-docs/live-e2e-report.md) caught the hazard this exists to close: :func:`fwd.ui.confirm` returns
    its *default* when there is no tty, and these prompts default to yes, so a scripted ``fwd attach`` against a
    stopped pod silently reprovisioned hardware at $0.25/hr with nobody watching. Spending money is the one decision that
    must never be made by a default.

    So the rule is: an explicit ``--restart`` always authorizes, an interactive user is asked, and a non-interactive
    run without the flag exits rather than guessing. Note the tty test is on **stdin** — that is what determines
    whether a human can actually answer, whereas ``ui.confirm`` inspects stdout, which stays a tty even when input is
    redirected from ``/dev/null``.

    Args:
        prompt: Question shown to an interactive user.
        restart: Value of the caller's ``--restart`` flag; ``True`` skips the prompt entirely.
        action: Imperative describing what will be started, used in the non-interactive error.
    """
    if restart:
        return
    if not sys.stdin.isatty():
        ui.die(
            f"refusing to {action} without confirmation because this is not an interactive terminal; "
            f"re-run with --restart if that is what you want"
        )
    if not ui.confirm(prompt, default=True):
        raise typer.Exit(1)


def attach(
    name: str | None = None,
    *,
    restart: bool = False,
    raw: bool = False,
    forward_ports: tuple[str, ...] | None = None,
    setup_github: bool | None = None,
    control_mode: bool = False,
) -> NoReturn:
    """Attach to an existing session's remote tmux, reconciling live status first.

    Args:
        name: Session name, target label, or backend name; ``None`` resolves the session registered for the current directory.
        restart: Authorize restarting stopped compute without prompting. Required for any restart in a
            non-interactive run, since the alternative is silently spending money.
        raw: When the target is running but its primary tmux session is missing, create a plain recovery shell without rerunning sync, tool installation, dependency installation, project setup, or agent startup.
        forward_ports: Optional mappings requested by the ``fwd up --reuse`` compatibility path; direct attach leaves forwarding unchanged.
        setup_github: Per-attach GitHub setup override; ``None`` uses the current project configuration.
        control_mode: Pass tmux ``-CC`` for native iTerm2 control-mode integration.

    Never returns: either execs into ssh, relaunches, or exits with a message.
    """
    session = launch_ops.resolve_session(name)
    if forward_ports is not None:
        from fwd.ops import ports as ports_ops

        ports_ops.preflight_launch_ports(session, forward_ports)
    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)

    if status is TargetStatus.UNKNOWN:
        # Deliberately before the GONE branch and deliberately non-destructive: an inconclusive answer must never
        # reach the offer-to-prune path below (dev-docs/live-e2e-report.md, R2-1).
        ui.die(
            f"could not determine the status of session {session.name!r} — the {session.backend} provider did not "
            f"answer. This is usually transient; try again in a moment. Run {ui.command('ls')!r} to see what {ui.command()} knows."
        )

    if status is TargetStatus.GONE:
        ui.warn(f"the {session.backend} target behind session {session.name!r} no longer exists")
        if ui.confirm(f"remove the stale session entry for {session.name!r}?", default=False):
            launch_ops.store().remove(session.name)
            ui.ok(f"removed session {session.name!r}")
        raise typer.Exit(1)

    if status is TargetStatus.STOPPED:
        # A stopped pod has a wiped container disk, so only the full launch pipeline can repair it.
        ui.warn(f"session {session.name!r}: target is stopped")
        _confirm_restart(f"restart session {session.name!r}?", restart=restart, action="restart billable compute")
        _relaunch(session, forward_ports=forward_ports, setup_github=setup_github, control_mode=control_mode)

    # Re-resolve rather than trusting the cached address: RunPod reassigns IP and port on every restart.
    try:
        endpoint = backend.endpoint(session)
    except NotImplementedError:
        endpoint = session.ssh_endpoint()
    except Exception as exc:
        ui.die(f"could not resolve a connection to session {session.name!r}: {exc}")

    if status is TargetStatus.JOB_ENDED:
        # The login node and the shared filesystem are both fine; only the allocation is gone. Re-requesting one in
        # place is far cheaper than a full relaunch, and per the Slurm contract no re-sync is needed.
        ui.warn(f"the compute allocation for {session.name!r} has ended")
        _confirm_restart("request a new allocation?", restart=restart, action="request a new allocation")
        _restart_allocation(backend, endpoint, session)
    elif status is TargetStatus.PENDING:
        # Normal on a busy cluster and can last hours. The tmux pane shows salloc's queue position, so attaching is
        # genuinely useful rather than an error.
        ui.info(f"session {session.name!r} is queued; attaching to the waiting allocation")

    fresh = endpoint_to_dict(endpoint)
    if fresh != session.endpoint:
        # The address moved; persist it so a later push/pull does not have to re-resolve.
        session.endpoint = fresh
        ui.info(f"endpoint moved to {endpoint.ssh_target()}:{endpoint.port}")

    if not _tmux_alive(endpoint, session.tmux_session):
        ui.warn(f"remote tmux session {session.tmux_session!r} is not running")
        if raw:
            _start_raw_shell(endpoint, session)
        else:
            # Cheaper than the other two paths (the target is already up), but it still reruns the whole launch pipeline, so it goes through the same gate rather than inventing a second policy.
            _confirm_restart("restart the remote session on this target?", restart=restart, action="rerun the launch")
            _relaunch(session, forward_ports=forward_ports, setup_github=setup_github, control_mode=control_mode)

    local_cwd = Path(session.local_cwd).expanduser().resolve()
    try:
        cfg = launch_ops.load_config(local_cwd)
    except ConfigError as exc:
        ui.die(str(exc))
    setup_github_effective = cfg.github.auth if setup_github is None else setup_github
    if setup_github_effective:
        tool_prefix = session.flags.get("tool_prefix")
        if not isinstance(tool_prefix, str) or not tool_prefix:
            if setup_github is True:
                ui.die(f"session {session.name!r} predates remote tool metadata; rerun {ui.command('up')!r} to repair it")
            ui.warn("GitHub setup skipped because this session predates its recorded remote tool path")
        else:
            try:
                ready = github_auth.ensure_remote(
                    endpoint,
                    local_cwd,
                    session.remote_dir,
                    tool_prefix,
                    required=setup_github is True,
                )
                if ready:
                    session.flags["github_auth_ready"] = True
            except (github_auth.GitHubAuthError, SSHError) as exc:
                ui.die(str(exc))

    # Continuous sync may have been enabled since launch, or its daemon may have been restarted, so attach reconciles
    # it rather than assuming launch already did.
    from fwd.ops import synccmd

    synccmd.ensure_for_session(session, cfg, endpoint=endpoint)

    if forward_ports is not None:
        ports_ops.ensure_session_ports(session, forward_ports, endpoint=endpoint)

    session.touch_attached()
    launch_ops.store().upsert(session)
    launch_ops.exec_attach(endpoint, session.tmux_session, session.name, control_mode=control_mode)


def smart_default(*, restart: bool = False) -> NoReturn:
    """Implement bare ``fwd``: attach, launch a saved default, or enter first-time setup.

    Setup remains the safe boundary for choosing between an existing SSH machine and billable compute. Its own mode
    detection prompts humans while giving agents exact required flags rather than attempting an interactive exchange.
    """
    session = launch_ops.resolve_session(None, required=False)
    if session is not None:
        # Returned rather than called bare: attach never returns in production, but if it ever did, falling through
        # would launch a second machine for a directory that already has one.
        return attach(session.name, restart=restart)
    ui.info(f"no {ui.command()} sessions for {Path.cwd().name}; looking for a saved target")
    launch_ops.launch(initial_command=None, attach=True)
    raise typer.Exit(0)


def smart_default_command(*, restart: bool = False) -> str:
    """Describe the canonical command that bare ``fwd`` will execute without changing state or prompting.

    Existing sessions expand to an explicit attach. With no session, a resolvable saved target expands to an explicit
    target/default-command launch; a genuinely empty configuration expands to setup. Invalid or ambiguous config uses
    the generic launch spelling because the launch path itself owns the actionable error.
    """
    session = launch_ops.store().get_for_cwd(Path.cwd())
    if session is None:
        session = launch_ops.store().get(launch_ops.derive_session_name(Path.cwd()))
    if session is not None:
        arguments = [ui.COMMAND_NAME, "attach", session.name]
        if restart:
            arguments.append("--restart")
        return shlex.join(arguments)
    try:
        cfg = launch_ops.load_config(Path.cwd())
    except Exception:
        return ui.command("up --attach")
    if not cfg.targets and not cfg.default_target:
        return ui.command("setup")
    try:
        target = cfg.target(None)
        initial_command = cfg.command_for(target.name)
    except Exception:
        return ui.command("up --attach")
    return shlex.join([ui.COMMAND_NAME, "up", "--target", target.name, "--attach", "--", *initial_command])
