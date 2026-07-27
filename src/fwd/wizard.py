"""Interactive setup — ``fwd setup``.

Design intent
-------------
Three decisions shape this module.

**tomlkit round-trip, never a dump.** Config files are hand-edited in practice: people add comments explaining which
cluster partition to use, or why an exclude exists. Re-running the wizard must not erase that, so an existing file is
parsed, mutated in place and written back. Clobbering a user's comments is a hostile default.

**Prompt for the essentials only.** Every target dataclass has a dozen fields, most with sensible defaults. Walking a
user through all of them to configure one SSH host is a worse experience than the config file it replaces, so each
backend declares a short ordered list of fields worth asking about; everything else keeps its dataclass default and
stays absent from the file, where it is easy to add later.

**Only write what was answered.** Values left at their default are not written out. That keeps generated files short
and — more importantly — lets fwd's own defaults improve over time without every existing config pinning the old
value forever.

The connection test at the end is optional and skips cleanly when a backend cannot answer yet, because setup must
work before the backends are finished.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import tomlkit
import typer

from fwd import ui
from fwd.backends import make_backend
from fwd.config import GLOBAL_CONFIG_PATH, TARGET_TYPES, Config, TargetConfig, load_config

# Fields worth prompting for, per backend, in the order they are asked. Everything else keeps its dataclass default.
# Required fields (no useful default) come first so an impatient user can accept the rest with Enter.
ESSENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "ssh": ("host", "user", "port", "key_path", "proxy_jump", "remote_base"),
    "runpod": ("gpu", "image", "volume_gb", "remote_base", "tool_prefix", "user"),
    "slurm": ("login_host", "user", "remote_base", "alloc", "tool_prefix", "partition", "account", "env_setup"),
}

# Fields that must be non-empty for the target to be usable at all; the wizard re-asks until they are given.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "ssh": ("host", "user"),
    "runpod": (),
    "slurm": ("login_host", "user", "remote_base"),
}

# Human-readable guidance shown alongside the prompt for fields whose purpose is not obvious from the name.
FIELD_HELP: dict[str, str] = {
    "remote_base": "parent directory for project checkouts on the remote side",
    "tool_prefix": "persistent path for installed tooling (survives stop/restart)",
    "alloc": "flags passed to salloc, e.g. --time=04:00:00 --gres=gpu:1",
    "env_setup": "comma-separated shell lines run before the allocation (module load ...)",
    "proxy_jump": "bastion host to jump through, if any",
    "key_path": "explicit ssh identity file; blank uses your ssh config/agent",
    "volume_gb": "persistent volume size; tooling and your project both live here",
}


def _ask(label: str, default: str = "") -> str:
    """Prompt for a free-text value, returning the default when input is unavailable.

    Kept local to the wizard rather than added to :mod:`fwd.ui` because this is the only interactive-input site in
    fwd; everything else is confirmations. Non-interactive stdin returns the default so ``fwd setup`` in a script
    degrades to "accept everything" instead of raising on EOF.
    """
    try:
        return typer.prompt(label, default=default, show_default=bool(default))
    except (EOFError, typer.Abort):
        return default


def _prompt_value(field_name: str, current: Any, *, required: bool) -> Any:
    """Prompt for one field, coercing the answer to the type of its default.

    Types are inferred from the dataclass default rather than from annotations because the defaults are already the
    authoritative shape (``int`` port, ``list[str]`` env_setup) and inspecting annotations would mean parsing strings
    under ``from __future__ import annotations``.
    """
    hint = FIELD_HELP.get(field_name)
    label = f"{field_name} ({hint})" if hint else field_name
    default_display = "" if current in (None, "", [], 0) else current
    if isinstance(current, list):
        default_display = ", ".join(current)

    while True:
        raw = _ask(label, default=str(default_display) if default_display != "" else "")
        raw = raw.strip()
        if not raw:
            if required:
                ui.warn(f"{field_name} is required")
                continue
            return current
        if isinstance(current, bool):
            return raw.lower() in {"1", "true", "yes", "y"}
        if isinstance(current, int) and not isinstance(current, bool):
            try:
                return int(raw)
            except ValueError:
                ui.warn(f"{field_name} must be a number")
                continue
        if isinstance(current, list):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw


def _prompt_target(name: str, backend: str) -> tuple[TargetConfig, dict[str, Any]]:
    """Prompt for a target's fields.

    Returns:
        The constructed dataclass (for the optional connection test) and the dict of values that actually differ from
        the defaults, which is all that gets written to disk.
    """
    cls = TARGET_TYPES[backend]
    defaults = {f.name: getattr(cls(name=name), f.name) for f in dataclass_fields(cls)}
    required = REQUIRED_FIELDS.get(backend, ())

    answers: dict[str, Any] = {}
    for field_name in ESSENTIAL_FIELDS.get(backend, ()):
        if field_name not in defaults:
            continue
        value = _prompt_value(field_name, defaults[field_name], required=field_name in required)
        if value != defaults[field_name]:
            answers[field_name] = value
    return cls(name=name, **answers), answers


def _test_connection(target: TargetConfig, cfg: Config) -> None:
    """Optionally run the backend's own doctor against the new target, skipping cleanly when unavailable."""
    if not ui.confirm(f"test the connection to {target.name!r} now?", default=True):
        return
    try:
        results = make_backend(target, cfg).doctor()
    except NotImplementedError:
        ui.info("this backend cannot run checks yet; skipping the connection test")
        return
    except Exception as exc:
        ui.warn(f"connection test could not run: {exc}")
        return
    for result in results:
        (ui.ok if result.ok else ui.warn)(f"{result.name}: {result.detail}")


def _write_config(path: Path, target_name: str, backend: str, values: dict[str, Any], *, make_default: bool) -> None:
    """Merge one target into the TOML file, preserving existing content, comments and formatting."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()

    targets = doc.get("targets")
    if targets is None:
        targets = tomlkit.table(is_super_table=True)
        doc["targets"] = targets

    table = tomlkit.table()
    table["backend"] = backend
    for key, value in values.items():
        table[key] = value
    targets[target_name] = table

    if make_default:
        doc["default_target"] = target_name

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    path.chmod(0o600)


def run_wizard() -> None:
    """Prompt for a target definition and merge it into the global config, preserving existing content."""
    ui.info(f"configuring {GLOBAL_CONFIG_PATH}")
    existing = load_config(Path.cwd())
    if existing.targets:
        ui.info(f"existing targets: {', '.join(existing.target_names())}")

    backends_list = sorted(TARGET_TYPES)
    # ssh is the default because it is the one backend that needs no account, no spend and no cluster access, so it
    # is where a first-time user should land if they just press Enter.
    default_backend = "ssh" if "ssh" in TARGET_TYPES else backends_list[0]
    backend = _ask(f"backend ({'/'.join(backends_list)})", default=default_backend).strip().lower()
    if backend not in TARGET_TYPES:
        ui.die(f"unknown backend {backend!r}; expected one of: {', '.join(backends_list)}")

    default_name = backend if backend not in existing.targets else f"{backend}-2"
    target_name = _ask("target name", default=default_name).strip() or default_name
    if target_name in existing.targets and not ui.confirm(f"target {target_name!r} exists; overwrite it?", default=False):
        ui.info("aborted")
        return

    target, values = _prompt_target(target_name, backend)
    make_default = not existing.default_target or ui.confirm(
        f"make {target_name!r} the default target?", default=not existing.targets
    )

    _write_config(GLOBAL_CONFIG_PATH, target_name, backend, values, make_default=make_default)
    ui.ok(f"wrote target {target_name!r} to {GLOBAL_CONFIG_PATH}")

    _test_connection(target, existing)
    ui.info("run 'fwd up' in a project directory to launch a session")
    # The wizard only asks about the fields it needs; point at the rest of the schema rather than pretending it is all.
    ui.info("'fwd config' shows the effective config and which file each value came from")
    ui.info("'fwd config --example' lists every available field, with defaults and comments")
