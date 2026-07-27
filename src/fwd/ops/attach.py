"""Attach and the bare-``fwd`` smart default.

Design intent
-------------
Attach is the reconciliation point of the whole tool. State is only a cache of what fwd believed last time, and
reality drifts underneath it constantly: pods get stopped from the RunPod console, Slurm allocations hit their time
limit, someone kills a tmux session by hand. So attach never trusts stored state — it re-resolves the endpoint through
the backend (RunPod hands out a new IP on every restart) and branches on live ``status()``:

- ``RUNNING``   → attach, stamping ``last_attached``.
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

import sys
from pathlib import Path
from typing import NoReturn

import typer

from fwd import remote, ui
from fwd.backends.base import TargetStatus
from fwd.ops import launch as launch_ops
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState, endpoint_to_dict


def _relaunch(session: SessionState) -> NoReturn:
    """Re-run the full launch pipeline for an existing session, reusing its recorded launch flags.

    Never returns: ``launch`` execs into attach on success.
    """
    flags = session.flags
    launch_ops.launch(
        target=flags.get("target"),
        name=session.name,
        session=bool(flags.get("session")),
        handoff=bool(flags.get("handoff")),
        user_config=bool(flags.get("user_config")),
        creds=bool(flags.get("creds")),
        attach=True,
    )
    raise typer.Exit(0)


def _restart_allocation(backend, endpoint: SSHEndpoint, session: SessionState) -> None:
    """Request a fresh compute allocation for a session whose job ended, without redoing the launch.

    The stale tmux session must be killed first: its pane holds a finished ``salloc``, and reusing it would attach
    the user to a dead shell. Sync and bootstrap are skipped on purpose — the cluster filesystem is shared and still
    holds everything from the original launch.
    """
    tool_prefix = session.flags.get("tool_prefix")
    claude_cmd = launch_ops.claude_command_for(session)
    with ui.step("Killing the stale tmux session"):
        remote.tmux_kill(endpoint, session.tmux_session)
    with ui.step("Requesting a new allocation"):
        tmux_cmd = launch_ops.build_tmux_command(
            backend,
            endpoint,
            session.name,
            session.remote_dir,
            tool_prefix,
            claude_cmd,
            gpu=session.flags.get("gpu"),
        )
        remote.tmux_new(endpoint, session.tmux_session, session.remote_dir, tmux_cmd)

    job_ids = launch_ops.track_job_id(backend, endpoint, session.name)
    if job_ids:
        session.backend_ids = {**session.backend_ids, **job_ids}
        launch_ops.store().update(session.name, backend_ids=session.backend_ids)


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


def _confirm_restart(prompt: str, *, restart: bool, action: str) -> None:
    """Gate any action that starts billable compute, or abort with an actionable message.

    The live e2e run (docs/live-e2e-report.md) caught the hazard this exists to close: :func:`fwd.ui.confirm` returns
    its *default* when there is no tty, and these prompts default to yes, so a scripted ``fwd attach`` against a
    stopped pod silently re-rented hardware at $0.25/hr with nobody watching. Spending money is the one decision that
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


def attach(name: str | None = None, *, restart: bool = False) -> NoReturn:
    """Attach to an existing session's remote tmux, reconciling live status first.

    Args:
        name: Session name; ``None`` resolves the session registered for the current directory.
        restart: Authorize restarting stopped compute without prompting. Required for any restart in a
            non-interactive run, since the alternative is silently spending money.

    Never returns: either execs into ssh, relaunches, or exits with a message.
    """
    session = launch_ops.resolve_session(name)
    backend = launch_ops.backend_for(session)
    status = launch_ops.status_of(backend, session)

    if status is TargetStatus.UNKNOWN:
        # Deliberately before the GONE branch and deliberately non-destructive: an inconclusive answer must never
        # reach the offer-to-prune path below (docs/live-e2e-report.md, R2-1).
        ui.die(
            f"could not determine the status of session {session.name!r} — the {session.backend} provider did not "
            f"answer. This is usually transient; try again in a moment. Run 'fwd ls' to see what fwd knows."
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
        _relaunch(session)

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
        # Cheaper than the other two paths (the target is already up), but it still reruns the whole launch pipeline,
        # so it goes through the same gate rather than inventing a second policy.
        _confirm_restart("restart the Claude session on this target?", restart=restart, action="rerun the launch")
        _relaunch(session)

    session.touch_attached()
    launch_ops.store().upsert(session)
    launch_ops.exec_attach(endpoint, session.tmux_session)


def smart_default(*, restart: bool = False) -> NoReturn:
    """Implement bare ``fwd``: attach to this directory's session, else launch a deliberately saved default.

    First-use provider selection belongs to ``fwd up --target ...`` because a bare command cannot safely choose between
    an existing SSH machine and billable compute. Once a session or default exists, one word still gets back to work.
    """
    session = launch_ops.resolve_session(None, required=False)
    if session is not None:
        # Returned rather than called bare: attach never returns in production, but if it ever did, falling through
        # would launch a second machine for a directory that already has one.
        return attach(session.name, restart=restart)
    ui.info(f"no fwd session for {Path.cwd().name}; looking for a saved default target")
    launch_ops.launch()
    raise typer.Exit(0)
