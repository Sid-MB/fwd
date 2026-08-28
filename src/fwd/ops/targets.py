"""Configured-target management — ``fwd targets``.

Design intent
-------------
``fwd ls`` and ``fwd rm`` manage *sessions*: live, billable remote compute tracked in ``~/.fwd/state.json``. This module
manages the other half — the ``[targets.NAME]`` declarations in fwd's layered config, which describe *how to reach*
compute and outlive any individual session. Keeping the two in separate command groups is deliberate: removing a target
must never be mistaken for destroying a machine, so every removal here is a pure config edit and its confirmation points
at ``fwd rm`` for the billable half.

Every operation is local and offline. Listing, inspection, and removal read the merged config plus local session state
and never contact a provider, so the group keeps working while an account or cluster is unreachable. Editing reuses
:func:`fwd.wizard.run_wizard` rather than reimplementing prompts, seeding a target's current values as prompt defaults
so ``update`` is an edit in place instead of a re-entry.

Removal writes through :mod:`fwd.ops.configcmd` (the machinery behind ``fwd config rm targets.NAME``) so scope
resolution, comment-preserving TOML round-tripping, and empty-table pruning have exactly one implementation. The one
piece of policy this module adds is repairing ``default_target`` afterwards: a dangling default would make every later
target-less command fail with a confusing error about a target the user just deleted.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from fwd import ui
from fwd.config import Config, ConfigError, LambdaTargetConfig, RunpodTargetConfig, SlurmTargetConfig, SshTargetConfig, TargetConfig, load_config
from fwd.output import OutputFormat
from fwd.state import SessionState, StateStore

UNSET = "—"
# Scope labels shared by removal messages and the keyword arguments understood by ``configcmd.remove_value``.
SCOPE_USER = "user"
SCOPE_PROJECT = "project"


def connection_detail(target: TargetConfig) -> str:
    """Summarize the fields that most clearly distinguish one target from another of the same backend.

    Shared with shell completion so a listed target and its Tab-completion tooltip never describe it differently.
    """
    if isinstance(target, SshTargetConfig):
        return f"{target.user + '@' if target.user else ''}{target.host or '<host unset>'}"
    if isinstance(target, RunpodTargetConfig):
        detail = target.gpu if target.compute_type == "gpu" and target.gpu else target.compute_type
        return f"{detail} · {target.cloud_type}"
    if isinstance(target, LambdaTargetConfig):
        return f"{target.instance_type or '<instance type unset>'} · {target.region or '<region unset>'}"
    if isinstance(target, SlurmTargetConfig):
        partition = f" · partition={target.partition}" if target.partition else ""
        return f"{target.login_host or '<login host unset>'}{partition}"
    return UNSET


def _project_root(project_dir: Path | None) -> Path:
    """Resolve the directory whose ``.fwd/config.toml`` participates in the merge."""
    return (project_dir or Path.cwd()).resolve()


def _load(project_dir: Path | None) -> tuple[Config, Path]:
    """Load the merged config for a project root, reporting unusable configuration as a CLI error."""
    root = _project_root(project_dir)
    try:
        return load_config(root), root
    except ConfigError as exc:
        ui.die(str(exc))


def configured_values(target: TargetConfig) -> dict[str, Any]:
    """Return only the fields where a target departs from its backend's defaults.

    This is the same "only write what was answered" contract the wizard applies when creating a target, so an update
    round-trip cannot silently pin today's defaults into the config file forever. ``image`` is compared against the
    default for the target's *own* compute type, because RunPod derives it in ``__post_init__``; every other field is
    compared against the plain class default, so an explicitly chosen ``compute_type = "gpu"`` still reads as
    configured rather than being explained away by the instance it selected.
    """
    baseline = type(target)(name=target.name)
    overrides: dict[str, Any] = {}
    compute_type = getattr(target, "compute_type", None)
    if compute_type is not None:
        overrides["image"] = getattr(type(target)(name=target.name, compute_type=compute_type), "image")
    return {field.name: getattr(target, field.name) for field in dataclass_fields(target) if field.name not in ("name", "backend") and getattr(target, field.name) != overrides.get(field.name, getattr(baseline, field.name))}


def sessions_for(name: str) -> tuple[SessionState, ...]:
    """Return locally tracked sessions launched against one configured target.

    Corrupt or unreadable state yields nothing rather than failing a config-only operation; the worst consequence is a
    removal confirmation that does not mention sessions the user can still see with ``fwd ls``.
    """
    try:
        sessions = StateStore().all()
    except Exception:
        return ()
    return tuple(session for session in sessions if session.flags.get("target") == name)


def matching_names(config: Config, substring: str | None) -> list[str]:
    """Return configured target names, optionally narrowed by a case-insensitive substring of the name."""
    names = config.target_names()
    if not substring:
        return names
    needle = substring.strip().lower()
    return [name for name in names if needle in name.lower()]


def _declaring_scopes(origins: dict[tuple[str, ...], str], name: str) -> tuple[str, ...]:
    """Return the scope labels whose file declares any key of ``[targets.NAME]``, user scope first."""
    found = {label for path, label in origins.items() if len(path) >= 2 and path[0] == "targets" and path[1] == name}
    return tuple(scope for scope in (SCOPE_USER, SCOPE_PROJECT) if ({SCOPE_USER: "global", SCOPE_PROJECT: "project"}[scope]) in found)


def _unknown(config: Config, names: tuple[str, ...]) -> None:
    """Abort with the available names when a selector does not exactly match a configured target."""
    missing = [name for name in names if name not in config.targets]
    if not missing:
        return
    available = ", ".join(config.target_names()) or "none configured"
    ui.die(f"unknown target {missing[0]!r}; available: {available}. Add one with {ui.command('targets add')!r}.")


def _empty_notice(substring: str | None) -> None:
    """Explain an empty listing and point at the command that fixes it."""
    if substring:
        ui.info(f"no configured target name contains {substring!r}")
    else:
        ui.info("no targets are configured")
    ui.show_code_examples((ui.command("targets ls"), ui.command("targets add")) if substring else (ui.command("targets add"),), heading="Manage targets:")


def ls(substring: str | None = None, *, output_format: OutputFormat | str = OutputFormat.auto, project_dir: Path | None = None) -> None:
    """List configured targets with their backend, key connection detail, and default marker.

    Deliberately not :func:`fwd.ops.machines.render`: that renderer answers "what hardware can I ask for", queries every
    provider, and adds the implicit zero-config RunPod target. This listing answers "what did I write down", so it stays
    offline and shows exactly the ``[targets]`` entries a user can update or remove.
    """
    config, _ = _load(project_dir)
    names = matching_names(config, substring)
    if not names:
        _empty_notice(substring)
        return
    rows = [(name, config.targets[name].backend, connection_detail(config.targets[name]), "yes" if name == config.default_target else "") for name in names]
    ui.table("configured targets", ("target", "backend", "connection", "default"), rows, output_format=output_format)


def info(name: str | None = None, *, output_format: OutputFormat | str = OutputFormat.auto, project_dir: Path | None = None) -> None:
    """Render one target's resolved configuration, marking which values came from a file rather than a default."""
    from fwd.ops import configcmd

    config, root = _load(project_dir)
    resolved = _selected(config, name, action="inspect")[0]
    target = config.targets[resolved]
    origins, global_path, project_path = configcmd.provenance(root)
    scopes = _declaring_scopes(origins, resolved)
    sessions = sessions_for(resolved)
    files = {SCOPE_USER: str(global_path), SCOPE_PROJECT: str(project_path)}
    fields: list[tuple[str, object]] = [
        ("name", resolved),
        ("backend", target.backend),
        ("default target", "yes" if config.default_target == resolved else "no"),
        ("declared in", ", ".join(f"{scope} ({files[scope]})" for scope in scopes) or UNSET),
        ("connection", connection_detail(target)),
        ("launch command", " ".join(config.command_for(resolved))),
    ]
    configured = configured_values(target)
    for field in dataclass_fields(target):
        if field.name in ("name", "backend"):
            continue
        value = getattr(target, field.name)
        rendered = UNSET if value in (None, "", []) else value
        fields.append((field.name, f"{rendered}" if field.name in configured else f"{rendered}  (default)"))
    fields.append(("tracked sessions", ", ".join(session.name for session in sessions) or "none"))
    ui.record(f"target {resolved}", tuple(fields), output_format=output_format)
    if sessions:
        ui.info(f"{len(sessions)} tracked session(s) use this target; inspect them with {ui.command('ls')!r}")


def prepare_update(name: str | None = None, *, project_dir: Path | None = None) -> tuple[str, str, dict[str, Any]]:
    """Resolve which target to edit and return its name, backend, and current non-default values.

    Split out from the wizard call so ``fwd targets update`` keeps one selector implementation (explicit name, picker, or
    a clear non-interactive failure) while the CLI layer owns flag merging.
    """
    config, _ = _load(project_dir)
    resolved = _selected(config, name, action="update")[0]
    target = config.targets[resolved]
    return resolved, target.backend, configured_values(target)


def remove(names: tuple[str, ...] = (), *, force: bool = False, project_dir: Path | None = None) -> int:
    """Remove ``[targets.NAME]`` entries from config, repairing ``default_target`` and never touching compute.

    Returns:
        The number of targets whose configuration was removed.
    """
    from fwd.ops import configcmd

    config, root = _load(project_dir)
    if not config.targets:
        _empty_notice(None)
        return 0
    selected = _selected(config, names, action="remove", allow_multiple=True)
    origins, global_path, project_path = configcmd.provenance(root)
    scopes = {name: _declaring_scopes(origins, name) for name in selected}
    undeclared = [name for name, found in scopes.items() if not found]
    if undeclared:
        ui.die(f"target {undeclared[0]!r} is merged from configuration fwd cannot rewrite; edit {global_path} or {project_path} directly")
    if not _confirm_removal(selected, scopes, force=force):
        return 0
    removed = 0
    for name in selected:
        for scope in scopes[name]:
            try:
                if configcmd.remove_value(f"targets.{name}", project=scope == SCOPE_PROJECT, project_dir=root, force=True):
                    removed += 1
            except ConfigError as exc:
                ui.die(str(exc))
    _repair_default_target(config, selected, project_dir=root)
    return removed


def _confirm_removal(names: tuple[str, ...], scopes: dict[str, tuple[str, ...]], *, force: bool) -> bool:
    """Confirm a config-only removal, naming the sessions that keep running and the command that destroys them."""
    from fwd import wizard

    subject = f"target {names[0]!r}" if len(names) == 1 else f"{len(names)} targets ({', '.join(names)})"
    tracked = {name: sessions_for(name) for name in names}
    live = [session.name for sessions in tracked.values() for session in sessions]
    if live:
        ui.warn(f"tracked session(s) {', '.join(live)} use these targets and will keep running; destroy that compute with {ui.command('rm ' + live[0])!r}")
    if force:
        return True
    reason = wizard.non_interactive_reason()
    if reason is not None:
        ui.die(f"refusing to remove {subject} because {reason}; re-run with --force to confirm removal non-interactively")
    written = ", ".join(sorted({scope for found in scopes.values() for scope in found}))
    if not ui.confirm(f"remove {subject} from {written} configuration? Remote compute and session state are untouched.", default=True):
        ui.info("config was not changed")
        return False
    return True


def _repair_default_target(config: Config, removed: tuple[str, ...], *, project_dir: Path) -> None:
    """Keep ``default_target`` resolvable after a removal.

    A default naming a deleted target makes every later target-less command fail, so the pointer is retargeted at the
    sole survivor when that choice is unambiguous, and otherwise dropped. Dropping it is safe: with several targets left
    fwd asks for ``--target`` instead of guessing, which is the same state a fresh multi-target config starts in.
    """
    from fwd.ops import configcmd

    if config.default_target not in removed:
        return
    survivors = [name for name in config.target_names() if name not in removed]
    try:
        if len(survivors) == 1:
            configcmd.set_value("default_target", (survivors[0],), project_dir=project_dir)
            return
        if configcmd.remove_value("default_target", project_dir=project_dir, force=True) and survivors:
            ui.info(f"default_target was cleared; choose one with {ui.command('config set default_target NAME')!r}")
    except ConfigError as exc:
        ui.warn(f"could not update default_target after removal: {exc}")


def _selected(config: Config, requested: str | tuple[str, ...] | None, *, action: str, allow_multiple: bool = False) -> tuple[str, ...]:
    """Resolve explicit target names or fall back to an interactive picker.

    Selection is centralized so ``info``, ``update``, and ``rm`` accept the same spellings and fail identically: exact
    names only (a near miss lists what exists), a numbered picker in a human terminal, and an actionable error rather
    than a hung prompt when no terminal can answer.
    """
    names = (requested,) if isinstance(requested, str) else tuple(requested or ())
    if names:
        if len(names) > 1 and not allow_multiple:
            ui.die(f"{ui.command(f'targets {action}')} accepts one target; pass a single name")
        _unknown(config, names)
        return names
    return _pick(config, action=action, allow_multiple=allow_multiple)


def _pick(config: Config, *, action: str, allow_multiple: bool) -> tuple[str, ...]:
    """Prompt for one or more configured targets from a numbered list.

    A numbered prompt rather than a full-screen selector: this list is short, the same shape as fwd's other interactive
    questions, and it degrades to a readable transcript when a terminal cannot render alternate screens.
    """
    from fwd import wizard

    names = config.target_names()
    if not names:
        ui.die(f"no targets are configured; add one with {ui.command('targets add')!r}")
    reason = wizard.non_interactive_reason()
    if reason is not None:
        ui.die(f"{ui.command(f'targets {action}')} needs a target because {reason} and no picker can be shown. Name one explicitly; list them with {ui.command('targets ls')!r}.")
    rows = [(str(index), name, config.targets[name].backend, connection_detail(config.targets[name]), "yes" if name == config.default_target else "") for index, name in enumerate(names, start=1)]
    ui.table("configured targets", ("#", "target", "backend", "connection", "default"), rows, output_format=OutputFormat.rich)
    label = f"target(s) to {action} (numbers or names, comma-separated)" if allow_multiple else f"target to {action} (number or name)"
    while True:
        answer = ui.ask(label, default=names[0] if len(names) == 1 else "").strip()
        chosen = _parse_selection(answer, names, allow_multiple=allow_multiple)
        if chosen:
            return chosen


def _parse_selection(answer: str, names: list[str], *, allow_multiple: bool) -> tuple[str, ...]:
    """Translate picker input into target names, warning and returning nothing when it cannot be resolved."""
    tokens = [token for token in answer.replace(",", " ").split() if token]
    if not tokens:
        ui.warn("choose at least one target")
        return ()
    if len(tokens) > 1 and not allow_multiple:
        ui.warn("choose exactly one target")
        return ()
    chosen: list[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(names):
            resolved = names[int(token) - 1]
        elif token in names:
            resolved = token
        else:
            ui.warn(f"{token!r} is not one of: {', '.join(names)}")
            return ()
        if resolved not in chosen:
            chosen.append(resolved)
    return tuple(chosen)
