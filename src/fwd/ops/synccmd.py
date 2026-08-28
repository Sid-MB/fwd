"""``fwd sync`` — turn continuous synchronization on or off, and inspect it.

Design intent
-------------
Continuous sync has two halves that must not be confused: a **persisted preference** (should this target sync
continuously?) and a **running Mutagen session** (is it doing so right now?). ``fwd sync on`` and ``off`` write the
preference *and* reconcile the live session in the same breath, because a toggle that only took effect at the next
``fwd up`` would not be a toggle. That is the whole requirement: it can be flipped while a session is running and the
change is immediate.

The preference is written per target, into ``[targets.NAME] continuous_sync``, through the same
:mod:`fwd.ops.configcmd` machinery every other config mutation uses — into *the file that declares that target*, which
:func:`_declaring_scope` resolves from raw provenance rather than from the merged config. A target that is not declared in config —
``runpod``, a ``user@host`` spelling, an ``~/.ssh/config`` alias — has no table to extend, and materializing one would
turn an inferred name into a half-written backend declaration. Those fall back to the project-scoped ``sync.continuous``
default, which is the closest honest equivalent, and the command says exactly what it wrote either way.

Reading and reconciling live state is shared with launch/attach/stop through :mod:`fwd.mutagen_sync`; this module owns
only the command-shaped behaviour: which session, what to write, and what to print.
"""

from __future__ import annotations

from pathlib import Path

import typer

from fwd import mutagen_sync, ui
from fwd.config import ConfigError, load_config
from fwd.ops import launch as launch_ops
from fwd.output import OutputFormat
from fwd.state import SessionState

FLAG_KEY = "mutagen_session"


def _resolve(name: str | None) -> SessionState | None:
    """Resolve the session a ``fwd sync`` invocation acts on, tolerating there being none.

    Uses the same resolution as ``push``/``pull`` so ``fwd sync on`` inside a project means the same thing as
    ``fwd pull`` does. ``required=False`` because enabling continuous sync before ever launching is legitimate: the
    preference is written and the next ``fwd up`` honours it.
    """
    return launch_ops.resolve_session(name, required=False)


def _project_dir(session: SessionState | None) -> Path:
    """Return the project directory whose configuration a toggle should read and write."""
    if session is None:
        return Path.cwd().resolve()
    local_cwd = Path(session.local_cwd).expanduser()
    return local_cwd.resolve() if local_cwd.is_dir() else Path.cwd().resolve()


def _target_name(session: SessionState | None, project_dir: Path) -> str | None:
    """Return the target the toggle applies to: the session's own, else the project's configured default."""
    if session is not None:
        recorded = session.flags.get("target")
        if isinstance(recorded, str) and recorded:
            return recorded
    try:
        return load_config(project_dir).target().name
    except ConfigError:
        return None


def _declaring_scope(target: str, project_dir: Path) -> str | None:
    """Return which config file actually declares ``[targets.NAME]``: ``"project"``, ``"global"``, or ``None``.

    The merged config cannot answer this, and guessing wrong is destructive rather than merely untidy. Writing a
    ``targets.NAME.continuous_sync`` override into the global file for a target declared only in a project's
    ``.fwd/config.toml`` materializes a ``[targets.NAME]`` table there with no ``backend`` key — which
    :func:`fwd.config.parse_target` rejects, so ``load_config`` then raises in *every other project* and every fwd
    command stops working globally. Provenance is read from the raw files through the machinery ``fwd config`` already
    uses for exactly this question. A target declared in both files resolves to the project one, because that is the
    file whose value wins the merge and therefore the only place an override reliably takes effect.
    """
    from fwd.ops import configcmd

    origins = {label for path, label in configcmd.provenance(project_dir)[0].items() if path[:2] == ("targets", target)}
    if configcmd.SRC_PROJECT in origins:
        return configcmd.SRC_PROJECT
    return configcmd.SRC_GLOBAL if configcmd.SRC_GLOBAL in origins else None


def _persist(target: str | None, enabled: bool, project_dir: Path) -> None:
    """Write the continuous-sync preference at the most specific scope the target supports."""
    from fwd.ops import configcmd

    value = ("true" if enabled else "false",)
    try:
        # Parsed only to fail early: writing an override into a file that already does not load would bury the real
        # error under a second one, and the value we write is decided from raw provenance rather than from the merge.
        load_config(project_dir)
    except ConfigError as exc:
        ui.die(str(exc))
    scope = _declaring_scope(target, project_dir) if target and configcmd.KEY_SEGMENT.fullmatch(target) else None
    if scope is not None:
        in_project = scope == configcmd.SRC_PROJECT
        configcmd.set_value(f"targets.{target}.continuous_sync", value, user=not in_project, project=in_project, project_dir=project_dir)
        return
    if target:
        ui.info(f"target {target!r} is not declared in [targets], so there is no per-target setting to write; setting the project default instead")
    configcmd.set_value("sync.continuous", value, project=True, project_dir=project_dir)


def _mutagen_name(session: SessionState) -> str:
    """Return the Mutagen session name for an fwd session, preferring the one recorded at creation time."""
    recorded = session.flags.get(FLAG_KEY)
    return recorded if isinstance(recorded, str) and recorded else mutagen_sync.session_name(session.name)


def start(session: SessionState, *, endpoint=None) -> mutagen_sync.SyncSessionStatus | None:
    """Ensure the Mutagen session for one fwd session exists and is running, recording its name in session flags.

    Shared by ``fwd sync on``, launch, and attach so all three establish continuous sync identically — including the
    interactive Mutagen install prompt, which belongs to the first command that actually needs the binary rather than
    to fwd's installation.

    Args:
        endpoint: Freshly resolved endpoint; ``None`` re-resolves through the session's backend, since a restarted
            target may have moved and a Mutagen session pointed at the previous address would never connect.
    """
    from fwd.ops.transfer import _endpoint_for

    resolved = endpoint if endpoint is not None else _endpoint_for(session)
    if not mutagen_sync.supports_continuous(resolved):
        ui.warn(f"continuous sync is unavailable on this transport ({session.backend} without scp support); use {ui.command('push')!r} and {ui.command('pull')!r} instead")
        return None
    mutagen_sync.ensure_installed()
    local_cwd = Path(session.local_cwd).expanduser()
    if not local_cwd.is_dir():
        ui.die(f"the local directory for session {session.name!r} no longer exists: {local_cwd}")
    cfg = load_config(local_cwd)
    name = _mutagen_name(session)
    with ui.step(f"Starting continuous sync for {session.name!r}"):
        state = mutagen_sync.ensure_session(resolved, local_cwd, session.remote_dir, name, cfg.sync)
    session.flags[FLAG_KEY] = name
    launch_ops.store().update(session.name, flags=session.flags)
    return state


def stop_session(session: SessionState, *, force: bool = False) -> bool:
    """Terminate one fwd session's Mutagen session, returning whether one was running.

    Best-effort by contract: this runs inside ``fwd stop`` and ``fwd rm``, where the expensive half of the operation is
    releasing compute, so a Mutagen failure is reported and stepped over rather than propagated.

    Teardown returns immediately unless :data:`FLAG_KEY` records that fwd actually created a Mutagen session for this
    fwd session. Without that gate, every ``fwd stop`` and ``fwd rm`` on a machine that merely *has* Mutagen installed
    would talk to the daemon — and talking to it starts a persistent fwd-owned daemon for a user who never enabled
    continuous sync.

    Args:
        force: Skip the flag gate and try to terminate by derived name anyway. Used by ``fwd sync off``, which is an
            explicit request to stop syncing and should still clean up a session whose flag was lost.
    """
    if not force and not isinstance(session.flags.get(FLAG_KEY), str):
        return False
    try:
        return mutagen_sync.terminate(_mutagen_name(session))
    except mutagen_sync.MutagenError as exc:
        ui.warn(f"could not stop continuous sync for {session.name!r} ({exc}); check it with {ui.command('sync status')!r}")
        return False


def ensure_for_session(session: SessionState, cfg, *, endpoint=None) -> None:
    """Reconcile one session's live Mutagen state with its effective configuration, never failing the caller.

    Called from launch and attach, where continuous sync is a convenience layered on top of the operation the user
    asked for. A target that cannot support it, a missing binary, or a Mutagen error must not take down a launch, so
    every failure degrades to a warning naming the command that retries it.
    """
    target = session.flags.get("target")
    if not cfg.continuous_sync_for(target if isinstance(target, str) else None):
        return
    if endpoint is not None and not mutagen_sync.supports_continuous(endpoint):
        ui.warn(f"continuous sync is enabled but unavailable on this transport; {ui.command('push')!r} and {ui.command('pull')!r} remain the way to move files")
        return
    try:
        start(session, endpoint=endpoint)
    except typer.Exit:
        ui.warn(f"continuous sync is enabled but could not be started; run {ui.command('sync on')!r} to retry")
    except Exception as exc:
        ui.warn(f"could not start continuous sync ({exc}); run {ui.command('sync on')!r} to retry")


# ------------------------------------------------------------------------------------------------------- commands


def on(name: str | None = None) -> None:
    """Enable continuous sync for a session's target and start it immediately when that session is live."""
    session = _resolve(name)
    project_dir = _project_dir(session)
    _persist(_target_name(session, project_dir), True, project_dir)
    if session is None:
        ui.info(f"no session is running here yet; the next {ui.command('up')!r} will start continuous sync")
        return
    try:
        state = start(session)
    except mutagen_sync.MutagenError as exc:
        # Mutagen's own failures (an unreachable host, a refused agent install) are user-facing conditions, not bugs;
        # ``fwd sync on`` is the one command whose whole job is starting the session, so it reports them as errors.
        ui.die(f"could not start continuous sync: {exc}")
    if state is None:
        return
    ui.ok(f"continuous sync is running for {session.name!r} ({state.status})")
    ui.info(f".git is never continuously synced; keep moving repository state with {ui.command('push')!r} and {ui.command('pull')!r}")


def off(name: str | None = None) -> None:
    """Disable continuous sync for a session's target and terminate any Mutagen session already running for it."""
    session = _resolve(name)
    project_dir = _project_dir(session)
    _persist(_target_name(session, project_dir), False, project_dir)
    if session is None:
        return
    if stop_session(session, force=True):
        ui.ok(f"stopped continuous sync for {session.name!r}")
    else:
        ui.info(f"no continuous sync was running for {session.name!r}")


def status(name: str | None = None, *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """Report the configured intent and the live Mutagen state for one session, including unresolved conflicts."""
    session = _resolve(name)
    project_dir = _project_dir(session)
    target = _target_name(session, project_dir)
    try:
        cfg = load_config(project_dir)
    except ConfigError as exc:
        ui.die(str(exc))
    enabled = cfg.continuous_sync_for(target)

    if mutagen_sync.binary_path() is None:
        ui.record(
            ui.command("sync status"),
            (("configured", "on" if enabled else "off"), ("target", target or "-"), ("mutagen", "not installed")),
            output_format=output_format,
        )
        if enabled:
            ui.info(f"continuous sync is enabled but Mutagen is missing; {mutagen_sync.install_instructions()}")
        return

    live = mutagen_sync.status(_mutagen_name(session)) if session is not None else None
    rows = [
        ("configured", "on" if enabled else "off"),
        ("target", target or "-"),
        ("session", session.name if session is not None else "-"),
        ("state", live.status if live is not None else "not running"),
        ("local", live.alpha if live is not None else str(project_dir)),
        ("remote", live.beta if live is not None else (session.remote_dir if session is not None else "-")),
        ("conflicts", live.conflicts if live is not None else 0),
    ]
    ui.record(ui.command("sync status"), tuple(rows), output_format=output_format)

    if live is None:
        if enabled and session is not None:
            ui.info(f"continuous sync is enabled but not running; start it with {ui.command('sync on')!r}")
        return
    for problem in live.problems:
        ui.warn(problem)
    if live.conflicts:
        # Verified live: re-editing the losing side does not settle a conflict — it is one more competing change.
        # Mutagen clears it once only one endpoint still has content, and then propagates the survivor.
        ui.warn(f"{live.conflicts} unresolved conflict(s): two-way-safe never picks a winner. Delete the copy you do not want on one side; the surviving version then propagates.")
        ui.info_with_code("see which paths conflict with ", mutagen_sync.describe_command(live.name))
    elif live.healthy:
        ui.ok("continuous sync is healthy")
    # Mutagen reads its ignore list once, at session creation, so later .gitignore edits are not picked up in place.
    ui.info(f"ignore rules were captured when this sync started; refresh them after editing .gitignore with {ui.command('sync off')!r} then {ui.command('sync on')!r}")
