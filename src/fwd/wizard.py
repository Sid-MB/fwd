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

import os
import sys
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
    # SSH user is deliberately optional: an alias in ~/.ssh/config may already specify User, IdentityFile, ProxyJump,
    # and other connection details. Passing an empty user lets OpenSSH resolve the complete alias as it normally would.
    "ssh": ("host",),
    "runpod": (),
    "slurm": ("login_host", "user", "remote_base"),
}

# Human-readable guidance shown alongside the prompt for fields whose purpose is not obvious from the name.
FIELD_HELP: dict[str, str] = {
    "host": "hostname, IP, or Host alias from ~/.ssh/config",
    "user": "remote username; SSH targets may leave this blank to defer to OpenSSH",
    "remote_base": "parent directory for project checkouts on the remote side",
    "tool_prefix": "persistent path for installed tooling (survives stop/restart)",
    "alloc": "flags passed to salloc, e.g. --time=04:00:00 --gres=gpu:1",
    "env_setup": "comma-separated shell lines run before the allocation (module load ...)",
    "proxy_jump": "bastion host to jump through, if any",
    "key_path": "explicit ssh identity file; blank uses your ssh config/agent",
    "volume_gb": "persistent volume size; tooling and your project both live here",
}

# CLI flag names for every field the interactive wizard can ask for. Kept beside ``ESSENTIAL_FIELDS`` so adding a
# prompt without a non-interactive equivalent is visible during review and can be checked before setup starts.
FIELD_FLAGS: dict[str, str] = {
    "host": "--host",
    "login_host": "--login-host",
    "user": "--user",
    "port": "--port",
    "key_path": "--key-path",
    "proxy_jump": "--proxy-jump",
    "remote_base": "--remote-base",
    "gpu": "--gpu",
    "image": "--image",
    "volume_gb": "--volume-gb",
    "tool_prefix": "--tool-prefix",
    "alloc": "--alloc",
    "partition": "--partition",
    "account": "--account",
    "env_setup": "--env-setup",
}


def _ask(label: str, default: str = "") -> str:
    """Prompt for a free-text value, allowing Click to abort cleanly on Ctrl-C or Ctrl-D.

    Kept local to the wizard rather than added to :mod:`fwd.ui` because this is the only interactive-input site in
    fwd; everything else is confirmations. Do not catch :class:`typer.Abort` or ``EOFError`` here: Click translates
    both interrupt keys into its normal ``Aborted!`` exit. Treating either as an empty answer traps users in required
    field loops, and silently accepting defaults on closed stdin can write an unintended partial configuration.
    """
    return typer.prompt(label, default=default, show_default=bool(default))


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


def _prompt_target_values(backend: str, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prompt for unsupplied target fields before asking for its fwd label.

    Returns:
        Values that differ from the backend defaults, which is all that gets written to disk.

    A placeholder name is used only to construct the dataclass defaults; target names do not affect field defaults.
    Asking for the label after these concrete connection details gives the user enough context to choose a useful name.
    Values supplied as CLI flags skip their corresponding prompts, making the same interface useful for partial
    interactive setup and fully non-interactive agent calls.
    """
    cls = TARGET_TYPES[backend]
    defaults = {f.name: getattr(cls(name=backend), f.name) for f in dataclass_fields(cls)}
    required = REQUIRED_FIELDS.get(backend, ())
    provided = supplied or {}

    answers: dict[str, Any] = {}
    for field_name in ESSENTIAL_FIELDS.get(backend, ()):
        if field_name not in defaults:
            continue
        value = provided[field_name] if provided.get(field_name) is not None else _prompt_value(field_name, defaults[field_name], required=field_name in required)
        if value != defaults[field_name]:
            answers[field_name] = value
    return answers


def _non_interactive_reason() -> str | None:
    """Return why setup should avoid prompts, or ``None`` for a normal interactive terminal."""
    markers = [name for name in ("CLAUDECODE", "CODEX_AGENT") if name in os.environ]
    if markers:
        return f"{', '.join(markers)} is set"
    if not sys.stdout.isatty():
        return "stdout is not a TTY"
    return None


def _validate_non_interactive(backend: str | None, values: dict[str, Any], reason: str) -> str:
    """Validate flag-only setup and return the resolved backend, otherwise exit with an actionable invocation."""
    if not backend:
        ui.die(
            f"fwd setup is running in non-interactive mode because {reason}. Missing required flag: --backend.\n"
            "Choose a backend with --backend ssh, --backend runpod, or --backend slurm. To force prompts, pass --interactive."
        )
    normalized = backend.strip().lower()
    if normalized not in TARGET_TYPES:
        ui.die(f"unknown backend {normalized!r}; expected one of: {', '.join(sorted(TARGET_TYPES))}")
    missing = [field for field in REQUIRED_FIELDS.get(normalized, ()) if values.get(field) in (None, "", [])]
    if missing:
        flags = " ".join(f"{FIELD_FLAGS[field]} VALUE" for field in missing)
        ui.die(
            f"fwd setup is running in non-interactive mode because {reason}. Missing required flag(s) for {normalized}: "
            f"{', '.join(FIELD_FLAGS[field] for field in missing)}.\n"
            f"Required form: fwd setup --backend {normalized} {flags}\n"
            "Run 'fwd setup --help' for every optional field, or pass --interactive to force prompts."
        )
    return normalized


def _test_connection(target: TargetConfig, cfg: Config, *, ask: bool = True) -> None:
    """Run the backend's doctor, optionally asking first, and skip cleanly when checks are unavailable."""
    if ask and not ui.confirm(f"test the connection to {target.name!r} now?", default=True):
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


def run_wizard(
    *,
    force_interactive: bool = False,
    backend: str | None = None,
    target_name: str | None = None,
    values: dict[str, Any] | None = None,
    make_default: bool = False,
    test_connection: bool = False,
    force: bool = False,
) -> None:
    """Create or update a target interactively or entirely from command-line values.

    Interactive mode is the default only when stdout is a terminal and no known agent environment marker is set.
    ``force_interactive`` overrides that detection. Non-interactive mode never prompts: it validates the required
    backend fields, reports the exact missing flags, writes the target, and runs provider checks only when explicitly
    requested.

    Args:
        force_interactive: Prompt even under redirected output or a known agent environment.
        backend: Provider identifier supplied by ``--backend``.
        target_name: Optional local fwd label; defaults to the backend name, with a numeric suffix on collision.
        values: Target field values supplied through CLI flags; ``None`` entries mean unspecified.
        make_default: Make this target the saved default even when another default already exists.
        test_connection: Run the backend's read-only diagnostics after writing in non-interactive mode.
        force: Overwrite an existing target with the same name without confirmation.
    """
    ui.info(f"configuring {GLOBAL_CONFIG_PATH}")
    existing = load_config(Path.cwd())
    if existing.targets:
        ui.info(f"existing targets: {', '.join(existing.target_names())}")

    provided = values or {}
    reason = None if force_interactive else _non_interactive_reason()
    interactive = reason is None
    backends_list = sorted(TARGET_TYPES)
    if interactive:
        # ssh is the default because it is the one backend that needs no account, no spend and no cluster access, so it
        # is where a first-time user should land if they just press Enter.
        default_backend = (backend or ("ssh" if "ssh" in TARGET_TYPES else backends_list[0])).strip().lower()
        resolved_backend = _ask(f"backend ({'/'.join(backends_list)})", default=default_backend).strip().lower()
        if resolved_backend not in TARGET_TYPES:
            ui.die(f"unknown backend {resolved_backend!r}; expected one of: {', '.join(backends_list)}")
    else:
        resolved_backend = _validate_non_interactive(backend, provided, reason)

    target_values = _prompt_target_values(resolved_backend, provided) if interactive else {
        key: value for key, value in provided.items() if key in ESSENTIAL_FIELDS.get(resolved_backend, ()) and value is not None
    }

    default_name = resolved_backend if resolved_backend not in existing.targets else f"{resolved_backend}-2"
    resolved_name = (target_name or "").strip()
    if interactive and not resolved_name:
        resolved_name = _ask("fwd target name (a local label for this connection)", default=default_name).strip()
    resolved_name = resolved_name or default_name
    if resolved_name in existing.targets and not force:
        if not interactive:
            ui.die(f"target {resolved_name!r} already exists; pass --force to overwrite it, or choose another name with --target-name")
        if not ui.confirm(f"target {resolved_name!r} exists; overwrite it?", default=False):
            ui.info("aborted")
            return

    target = TARGET_TYPES[resolved_backend](name=resolved_name, **target_values)
    should_make_default = make_default or not existing.default_target
    if interactive and existing.default_target and not make_default:
        should_make_default = ui.confirm(f"make {resolved_name!r} the default target?", default=not existing.targets)

    _write_config(GLOBAL_CONFIG_PATH, resolved_name, resolved_backend, target_values, make_default=should_make_default)
    ui.ok(f"wrote target {resolved_name!r} to {GLOBAL_CONFIG_PATH}")

    if interactive:
        _test_connection(target, existing)
    elif test_connection:
        _test_connection(target, existing, ask=False)
    ui.info("run 'fwd up' in a project directory to launch a session")
    # The wizard only asks about the fields it needs; point at the rest of the schema rather than pretending it is all.
    ui.info("'fwd config' shows the effective config and which file each value came from")
    ui.info("'fwd config --example' lists every available field, with defaults and comments")
