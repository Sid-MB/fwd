"""``fwd config`` — make the config schema discoverable from the CLI itself.

Design intent
-------------
Configuration is the one part of ``fwd`` a user cannot learn by running commands: the merge of global and project files
is invisible, and the field list only existed in the README and the dataclasses. Two failure modes followed from that —
a user could not tell *which file* set a surprising value, and an agent driving ``fwd`` had to guess at field names. This
module answers both by printing, not by documenting:

- :func:`show` renders the **effective** merged config as TOML, annotating every leaf with where it came from.
- :func:`render_example` renders a fully-commented reference, generated **from the dataclasses** so it cannot drift from the
  schema. Only the prose comments are static; every field name and default is introspected via ``dataclasses.fields``,
  so adding a field to a target dataclass adds it to the example automatically (with a placeholder comment if nobody
  wrote one). The emitted text is asserted to be parseable TOML *and* to survive ``parse_target`` in the test suite,
  because a reference config that does not load would be worse than none.
- :func:`render_schema` exposes those same dataclasses as JSON Schema for agents, editors, and validation tooling.

Provenance is computed by re-reading the two raw files and flattening each to leaf paths, rather than by instrumenting
:func:`~fwd.config.deep_merge`. Same answer, and it keeps the merge function free of bookkeeping that only one command
needs. A leaf present in the project file is attributed to it, else the global file, else the built-in default.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import MISSING, fields
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from fwd import config as config_mod
from fwd import ui
from fwd.config import (
    ALWAYS_SYNC_EXCLUDES,
    AgentConfig,
    BUILTIN_AGENT_NAMES,
    DEFAULT_EXCLUDES,
    PROJECT_CONFIG_RELPATH,
    RUNPOD_CLOUD_TYPES,
    RUNPOD_COMPUTE_TYPES,
    ClaudeConfig,
    Config,
    ConfigError,
    ForwardingConfig,
    GitHubConfig,
    SyncConfig,
    TARGET_TYPES,
    implicit_target,
    load_config,
)
from fwd.output import is_machine_environment

# Provenance labels. Kept short so they fit as end-of-line comments; the header legend maps them to real paths.
SRC_PROJECT = "project"
SRC_GLOBAL = "global"
SRC_DEFAULT = "default"

# One-line explanation per field, keyed by field name. Shared across target types where the meaning is identical
# (``remote_base``, ``user``, ``port``); backend-specific overrides live in TARGET_FIELD_DOCS below. Static because the
# prose is the one thing that genuinely cannot be introspected.
FIELD_DOCS: dict[str, str] = {
    "backend": "which provisioner drives this target (required)",
    "host": "hostname or IP to ssh to (required)",
    "login_host": "cluster login node; pinned on first connect so reattach lands on the same one (required)",
    "user": "remote username; leave unset to let ~/.ssh/config decide",
    "port": "ssh port",
    "key_path": "identity file; unset means your ssh agent/config supplies it",
    "proxy_jump": "external ssh -J host used to reach a non-public target, as user@host",
    "remote_base": "parent dir for checkouts; the project name is appended to form remote_dir",
    "extra_opts": "extra raw ssh options, e.g. [\"-o\", \"ServerAliveInterval=30\"]",
    "compute_type": f"one of: {' | '.join(sorted(RUNPOD_COMPUTE_TYPES))}",
    "cloud_type": f"one of: {' | '.join(sorted(RUNPOD_CLOUD_TYPES))} — network volumes require secure",
    "gpu": f"RunPod GPU id; override per launch with {ui.command('up --gpu')!r}",
    "image": "container image the pod boots",
    "persistent": "create a per-session network volume that survives Pod termination",
    "data_center_id": "RunPod datacenter for the network volume and its Pods",
    "volume_gb": "persistent network-volume size in GB",
    "volume_mount_path": "where the persistent volume is mounted",
    "tool_prefix": f"where {ui.command()} installs uv/node/bun; must be on persistent storage or every restart re-downloads",
    "allow_proxy": "permit the ssh.runpod.io fallback when no direct IP exists (that proxy cannot run rsync)",
    "region": "exact Lambda Cloud region, or auto to choose from current capacity",
    "preferred_regions": "ordered Lambda region codes or prefixes considered before other auto candidates",
    "instance_type": "Lambda instance type; launch requires current capacity in the selected region",
    "ssh_key_name": "public SSH key name already registered in Lambda Cloud",
    "image_id": "optional Lambda image id; unset uses the provider default image",
    "filesystem_mount_path": "where the session-owned Lambda filesystem is mounted",
    "alloc": "flags spliced into the salloc line",
    "env_setup": "shell lines run before the allocation, e.g. module loads",
    "partition": "slurm partition (-p)",
    "account": "slurm account (-A)",
    "continuous_sync": f"override sync.continuous for this target; toggle it with {ui.command('sync on')!r} / {ui.command('sync off')!r}",
}

# Field docs that differ by backend, because the same field name carries a different warning per provider.
TARGET_FIELD_DOCS: dict[str, dict[str, str]] = {
    "lambda": {
        "persistent": "retain a per-session Lambda filesystem after instance termination; disable only for disposable work",
        "remote_base": "parent dir for checkouts; keep under filesystem_mount_path for persistence",
        "tool_prefix": "installed tools and agent state; keep under filesystem_mount_path for persistence",
    },
    "runpod": {"remote_base": "parent dir for checkouts; keep under volume_mount_path for persistence"},
    "slurm": {
        "remote_base": "parent dir for checkouts; MUST be scratch, never $HOME (inode quotas) (required)",
        "tool_prefix": "scratch-backed tooling root; keeps inode-heavy venvs out of $HOME",
    },
}

SECTION_DOCS: dict[str, str] = {
    "user_config": "upload your ~/.claude bundle (CLAUDE.md, skills, agents, commands); never creds or history",
    "creds": "copy your Claude OAuth token to the remote disk — off for a reason",
    "session": "move the real transcript so the remote claude resumes this conversation",
    "handoff": "summarize into HANDOFF.md instead of moving the transcript",
    "auth": "copy the active local gh authentication to the remote and configure Git pushes",
    "full_access": "run without approval prompts or an agent sandbox; disable when the remote VM is not the isolation boundary",
    "args": "additional agent CLI arguments; explicit permission/sandbox arguments take precedence over full_access",
    "environment": "environment defaults applied only when the remote shell has not already set each variable",
    "exclude": "project excludes; REPLACES configurable defaults, while platform metadata remains always excluded",
    "use_gitignore": "use Git's own file enumeration so every nested .gitignore is honoured exactly",
    "delete": "push mirrors local, removing remote-only files",
    "max_size_gb": "streaming upload circuit breaker in GB; defaults to 1 GB to catch accidentally broad directories",
    "continuous": "keep projects continuously synchronized with Mutagen while a session runs; .git is never included",
    "ports": "loopback-only PORT or LOCAL:REMOTE mappings opened after launch; project values replace user defaults",
}

DEFAULT_COMMAND_DOC = f"argv launched by bare {ui.command()!r}; target_defaults.<name>.default_command takes precedence"
# A name that can be written as a bare TOML key. Also the test for whether a target can receive a
# ``[targets.NAME]`` override at all, which is why it is public: :mod:`fwd.ops.synccmd` asks the same question.
KEY_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# Fields with no usable default that the user must fill in for the target to work at all. Emitted uncommented with a
# placeholder so the example is a working skeleton; everything else optional is emitted commented out.
REQUIRED_PLACEHOLDERS: dict[str, dict[str, Any]] = {
    "lambda": {"instance_type": "gpu_1x_a10", "ssh_key_name": "my-public-key"},
    "ssh": {"host": "gpu.example.com", "user": "you"},
    "runpod": {},
    "slurm": {"login_host": "login.hpc.example.edu", "user": "you", "remote_base": "/scratch/you/fwd"},
}

# Placeholder values for optional fields whose default is None/"" — shown commented out, since emitting the real
# default is impossible (TOML has no null) and an empty string would look like a setting rather than an absence.
OPTIONAL_PLACEHOLDERS: dict[str, Any] = {
    "key_path": "~/.ssh/id_ed25519",
    "proxy_jump": "you@external.example.com",
    "partition": "gpu",
    "account": "your-account",
    "tool_prefix": "/scratch/you/.fwd-tools",
    "user": "you",
    "image_id": "ddaedf1b7a0e41ac981711504493b242",
    # Rendered commented-out and true, because the only reason to write this key at all is to differ from the
    # sync.continuous default that an absent key already inherits.
    "continuous_sync": True,
}

EXAMPLE_TARGET_NAMES: dict[str, str] = {"ssh": "box", "runpod": "pod", "lambda": "lambda-gpu", "slurm": "hpc"}


def _toml_key(value: str) -> str:
    """Render a bare TOML key when possible and a quoted key for arbitrary environment variable names."""
    if KEY_SEGMENT.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_scalar(value: Any) -> str:
    """Render a Python scalar as TOML. Only the types the config schema actually uses are supported."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(str(key))} = {_toml_scalar(item)}" for key, item in value.items()) + " }"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _flatten(data: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Flatten nested dicts to ``{path_tuple: leaf_value}``, treating lists as leaves.

    Lists are leaves because :func:`~fwd.config.deep_merge` replaces them wholesale — attributing individual list
    elements to a file would describe a merge that does not happen.
    """
    if not isinstance(data, dict):
        return {prefix: data}
    flat: dict[tuple[str, ...], Any] = {}
    for key, value in data.items():
        flat.update(_flatten(value, (*prefix, str(key))))
    return flat


def provenance(project_dir: Path) -> tuple[dict[tuple[str, ...], str], Path, Path]:
    """Return ``{leaf_path: source_label}`` plus the global and project config paths.

    Reads the same two files :func:`~fwd.config.load_config` reads, via the module attribute rather than the imported
    constant so a monkeypatched ``GLOBAL_CONFIG_PATH`` is honoured in tests exactly as it is in ``load_config``.
    """
    global_path = config_mod.GLOBAL_CONFIG_PATH
    project_path = project_dir / PROJECT_CONFIG_RELPATH
    global_flat = _flatten(config_mod._read_toml(global_path))
    project_flat = _flatten(config_mod._read_toml(project_path))
    origins = {path: SRC_GLOBAL for path in global_flat}
    origins.update({path: SRC_PROJECT for path in project_flat})
    return origins, global_path, project_path


def _annotated(key: str, value: Any, path: tuple[str, ...], origins: dict[tuple[str, ...], str], *, forced: str | None = None) -> str:
    """Render one ``key = value`` line with its provenance as a trailing comment."""
    label = forced or origins.get(path, SRC_DEFAULT)
    return f"{key} = {_toml_scalar(value)}  # {label}"


def _dataclass_items(obj: Any) -> list[tuple[str, Any]]:
    """Return ``(field_name, value)`` for a dataclass instance, minus the injected ``name``."""
    return [(f.name, getattr(obj, f.name)) for f in fields(obj) if f.name != "name"]


def render_effective(cfg: Config, project_dir: Path) -> str:
    """Render the merged config as annotated TOML.

    Every leaf carries a ``# global`` / ``# project`` / ``# default`` marker, and implicit targets are marked with the
    origin label :func:`~fwd.config.implicit_target` returned, so a target that exists only because it was named on the
    command line is visibly not from any file.
    """
    origins, global_path, project_path = provenance(project_dir)
    lines = [
        f"# Effective {ui.command()} configuration (global + project, deep-merged). Values are annotated with their source.",
        f"#   global  = {global_path}{'' if global_path.is_file() else '  (absent)'}",
        f"#   project = {project_path}{'' if project_path.is_file() else '  (absent)'}",
        f"#   default = built into {ui.command()}, not written anywhere",
        f"# Run {ui.command('config --example')!r} for a commented reference of every available field.",
        "",
    ]

    default_target = cfg.default_target
    if default_target is None:
        lines.append("# default_target is unset  # default")
    else:
        lines.append(_annotated("default_target", default_target, ("default_target",), origins))
    lines.append(_annotated("default_command", cfg.default_command, ("default_command",), origins))

    for section, obj in (("claude", cfg.claude), ("github", cfg.github), ("sync", cfg.sync)):
        lines += ["", f"[{section}]"]
        for key, value in _dataclass_items(obj):
            lines.append(_annotated(key, value, (section, key), origins))
    if cfg.forwarding.ports:
        lines += ["", "[forwarding]"]
        lines.append(_annotated("ports", cfg.forwarding.ports, ("forwarding", "ports"), origins))

    for name, agent_config in sorted(cfg.agents.items()):
        lines += ["", f"[agents.{name}]"]
        for key, value in _dataclass_items(agent_config):
            if key == "environment" and value:
                continue
            lines.append(_annotated(key, value, ("agents", name, key), origins))
        if agent_config.environment:
            lines += ["", f"[agents.{name}.environment]"]
            for key, value in sorted(agent_config.environment.items()):
                lines.append(_annotated(_toml_key(key), value, ("agents", name, "environment", key), origins))

    for name, command in sorted(cfg.target_default_commands.items()):
        lines += ["", f"[target_defaults.{name}]"]
        lines.append(_annotated("default_command", command, ("target_defaults", name, "default_command"), origins))

    if not cfg.targets:
        lines += [
            "",
            f"# No targets are configured. {ui.command('up --target runpod')!r} and {ui.command('up --target user@host')!r} still work — those",
            f"# names are inferred without config. Run {ui.command('setup')!r} or {ui.command('config --example')!r} to declare one.",
        ]
    for name in cfg.target_names():
        target = cfg.targets[name]
        lines += ["", f"[targets.{name}]"]
        for key, value in _dataclass_items(target):
            lines.append(_annotated(key, value, ("targets", name, key), origins))
    return "\n".join(lines) + "\n"


def _render_example_target(backend: str) -> list[str]:
    """Render one commented ``[targets.<name>]`` block, introspected from that backend's dataclass."""
    cls = TARGET_TYPES[backend]
    name = EXAMPLE_TARGET_NAMES[backend]
    required = REQUIRED_PLACEHOLDERS[backend]
    docs = {**FIELD_DOCS, **TARGET_FIELD_DOCS.get(backend, {})}
    instance = cls(name=name)
    lines = [f"[targets.{name}]"]
    for f in fields(cls):
        if f.name == "name":
            continue
        comment = docs.get(f.name, f"see the {cls.__name__} docstring")
        if f.name == "backend":
            lines.append(f'backend = "{backend}"  # {comment}')
        elif f.name in required:
            lines.append(f"{f.name} = {_toml_scalar(required[f.name])}  # {comment}")
        elif backend == "runpod" and f.name == "gpu" and instance.compute_type == "cpu":
            lines.append(f"# gpu = {_toml_scalar(instance.gpu)}  # optional for compute_type = \"gpu\" — {comment}")
        else:
            # Read the constructed instance rather than the raw dataclass field so normalized/dynamic defaults from
            # __post_init__ (notably RunPod's CPU-vs-GPU image) remain discoverable from the generated reference.
            value = getattr(instance, f.name)
            if value is None or value == "":
                # No emittable default: TOML has no null, and "" would read as a setting rather than an absence. Show a
                # plausible value commented out so the field is discoverable without being silently applied.
                shown = OPTIONAL_PLACEHOLDERS.get(f.name)
                rendered = _toml_scalar(shown) if shown is not None else "..."
                lines.append(f"# {f.name} = {rendered}  # optional — {comment}")
            else:
                lines.append(f"{f.name} = {_toml_scalar(value)}  # {comment}")
    return lines


def _render_example_section(section: str, obj: Any) -> list[str]:
    """Render one commented non-target configuration block from its dataclass defaults."""
    lines = [f"[{section}]"]
    for key, value in _dataclass_items(obj):
        comment = SECTION_DOCS.get(key, "")
        lines.append(f"{key} = {_toml_scalar(value)}" + (f"  # {comment}" if comment else ""))
    return lines


def render_example(which: str = "all") -> str:
    """Render a commented reference config for one backend or all of them.

    Args:
        which: ``"ssh"``, ``"runpod"``, ``"lambda"``, ``"slurm"`` or ``"all"``.

    Returns:
        Valid TOML. Every value shown is the real dataclass default (or a marked placeholder for fields that have no
        usable default), so this text can be pasted into ``~/.fwd/config.toml`` and edited down.
    """
    backends = list(EXAMPLE_TARGET_NAMES) if which == "all" else [which]
    lines = [
        f"# {ui.command()} configuration reference — generated from {ui.command()}'s own dataclasses, so it matches this exact version.",
        "# Global file: ~/.fwd/config.toml.  Per-project override: <project>/.fwd/config.toml, which DEEP-MERGES over",
        "# the global one, so a repo can change a single field of a target without restating the rest.",
        "# Commented-out lines are optional fields shown with a plausible value, not defaults being applied.",
        f"# Inspect what your own files actually resolve to with {ui.command('config')!r}.",
        "#",
        f"# You may not need any of this: {ui.command('up --target runpod')!r} provisions a CPU pod from built-in defaults, and",
        f"# {ui.command('up --target user@host')!r} (or any Host alias in ~/.ssh/config) works with no config file at all.",
        "",
        f'default_target = "{EXAMPLE_TARGET_NAMES[backends[0]]}"  # used when --target is omitted',
        f'default_command = ["claude"]  # argv launched by bare {ui.command()}; set with: {ui.command("default <command>")}',
    ]
    for backend in backends:
        lines += ["", *_render_example_target(backend)]
    lines += [
        "",
        f"[target_defaults.{EXAMPLE_TARGET_NAMES[backends[0]]}]",
        'default_command = ["codex"]  # overrides project/user default_command whenever this target is selected',
    ]
    lines += ["", *_render_example_section("claude", ClaudeConfig())]
    lines += ["", *_render_example_section("github", GitHubConfig())]
    for agent_name in BUILTIN_AGENT_NAMES:
        lines += ["", *_render_example_section(f"agents.{agent_name}", AgentConfig())]
    lines += ["", *_render_example_section("sync", SyncConfig())]
    lines += ["", *_render_example_section("forwarding", ForwardingConfig())]
    lines += [
        "",
        f"# The built-in exclude list, for reference: {', '.join(DEFAULT_EXCLUDES)}",
        f"# Always excluded platform metadata: {', '.join(ALWAYS_SYNC_EXCLUDES)}",
    ]
    return "\n".join(lines) + "\n"


def _json_type(annotation: Any) -> dict[str, Any]:
    """Translate the small set of Python annotations used by the config dataclasses into JSON Schema fragments."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return {"enum": list(args)}
    if origin is list:
        return {"type": "array", "items": _json_type(args[0])}
    if origin is dict:
        return {"type": "object", "additionalProperties": _json_type(args[1])}
    if origin in (Union, UnionType):
        non_null = [arg for arg in args if arg is not type(None)]
        if len(non_null) == 1 and len(non_null) != len(args):
            return {"anyOf": [_json_type(non_null[0]), {"type": "null"}]}
    return {"type": {str: "string", int: "integer", float: "number", bool: "boolean"}.get(annotation, "string")}


def _section_schema(cls: type, docs: dict[str, str], *, backend: str | None = None) -> dict[str, Any]:
    """Build one strict object schema from a config dataclass, including its real defaults and field descriptions."""
    hints = get_type_hints(cls)
    backend_docs = TARGET_FIELD_DOCS.get(backend or "", {})
    instance = cls(name=backend) if backend is not None else cls()
    properties: dict[str, Any] = {}
    for field_info in fields(cls):
        if field_info.name == "name":
            continue
        field_schema = _json_type(hints[field_info.name])
        field_schema["description"] = backend_docs.get(field_info.name, docs.get(field_info.name, f"See {cls.__name__}.{field_info.name}."))
        if field_info.name == "backend" and backend is not None:
            field_schema = {"const": backend, **field_schema}
        field_schema.update(field_info.metadata.get("json_schema", {}))
        effective_default = getattr(instance, field_info.name)
        if effective_default is not None:
            field_schema["default"] = effective_default
        properties[field_info.name] = field_schema
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if backend is not None:
        schema["required"] = ["backend"]
    return schema


def render_schema() -> str:
    """Return the complete fwd configuration contract as formatted JSON Schema Draft 2020-12.

    The field names, types, enum values, and defaults come from the same dataclasses used by the loader and example
    renderer. ``additionalProperties`` is intentionally false so editors and validators flag misspelled config keys
    even though fwd itself tolerates unknown keys for forward compatibility.
    """
    target_refs = [{"$ref": f"#/$defs/{backend}Target"} for backend in TARGET_TYPES]
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{ui.command()} configuration",
        "description": "Configuration for ~/.fwd/config.toml and the deep-merged per-project .fwd/config.toml override.",
        "type": "object",
        "properties": {
            "default_target": {"type": "string", "description": "Target used when --target is omitted."},
            "default_command": {"type": "array", "items": {"type": "string"}, "minItems": 1, "default": ["claude"], "description": DEFAULT_COMMAND_DOC},
            "agents": {
                "type": "object",
                "description": "Per-agent runtime policy with one consistent schema for built-in and future agents.",
                "additionalProperties": _section_schema(AgentConfig, SECTION_DOCS),
                "default": {name: {"full_access": True, "args": [], "environment": {}} for name in BUILTIN_AGENT_NAMES},
            },
            "claude": _section_schema(ClaudeConfig, SECTION_DOCS),
            "github": _section_schema(GitHubConfig, SECTION_DOCS),
            "sync": _section_schema(SyncConfig, SECTION_DOCS),
            "forwarding": _section_schema(ForwardingConfig, SECTION_DOCS),
            "targets": {"type": "object", "description": "Named remote targets.", "additionalProperties": {"oneOf": target_refs}},
            "target_defaults": {
                "type": "object",
                "description": "Target-specific settings that override both project and user defaults.",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"default_command": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": DEFAULT_COMMAND_DOC}},
                    "required": ["default_command"],
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
        "$defs": {f"{backend}Target": _section_schema(cls, FIELD_DOCS, backend=backend) for backend, cls in TARGET_TYPES.items()},
    }
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def show(which_example: str | None = None, project_dir: Path | None = None, *, schema: bool = False) -> None:
    """Print the JSON Schema, commented example config, or effective merged config.

    Writes via :func:`ui.raw` because all outputs are *data*: the intended use is
    ``fwd config --example > ~/.fwd/config.toml``, and Rich's width-based wrapping would fold trailing comments into
    invalid TOML. The no-config hints go to stderr, so a redirect still captures clean TOML.
    """
    if schema:
        ui.raw(render_schema())
        return
    if which_example is not None:
        ui.raw(render_example(which_example))
        return

    root = (project_dir or Path.cwd()).resolve()
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        ui.die(str(exc))
    if not cfg.sources:
        ui.info("no config file found (looked for ~/.fwd/config.toml and ./.fwd/config.toml)")
        ui.info(f"run {ui.command('setup')!r} for the wizard, or {ui.command('config --example')!r} for a commented reference to start from")
        ui.info(f"or skip config entirely: {ui.command('up --target runpod')!r}, or {ui.command('up --target user@host')!r}")
    ui.raw(render_effective(cfg, root))


def _config_value(key: str, values: tuple[str, ...]) -> Any:
    """Convert CLI words into a TOML value while keeping command argv lossless.

    ``default_command`` is intentionally always an array, including a one-word command. Other keys accept a convenient
    scalar form (booleans and integers are inferred through TOML) and become string arrays when multiple words are
    supplied. Users can still edit the file directly for richer TOML structures.
    """
    if not values:
        raise ConfigError("a value is required")
    if key in {"default_command", "forwarding.ports"}:
        return list(values)
    if len(values) > 1:
        return list(values)
    try:
        return config_mod.tomlkit.parse(f"value = {values[0]}\n").unwrap()["value"]
    except Exception:
        return values[0]


def _assign_path(document: Any, path: tuple[str, ...], value: Any) -> None:
    """Round-trip assign a dotted path, creating only the missing TOML tables."""
    current = document
    for segment in path[:-1]:
        existing = current.get(segment)
        if existing is None:
            existing = config_mod.tomlkit.table()
            current[segment] = existing
        if not hasattr(existing, "__setitem__"):
            raise ConfigError(f"cannot set {'.'.join(path)!r}: {segment!r} is already a scalar value")
        current = existing
    current[path[-1]] = value


def _scope_location(
    key: str,
    *,
    user: bool,
    project: bool,
    target: str | None,
    project_dir: Path | None,
) -> tuple[Path, tuple[str, ...], str]:
    """Resolve CLI scope flags to one file, one TOML path, and a user-facing scope label."""
    selected = int(user) + int(project) + int(target is not None)
    if selected > 1:
        raise ConfigError("--user, --project, and --target are mutually exclusive")
    segments = tuple(key.split("."))
    if not segments or any(not KEY_SEGMENT.fullmatch(segment) for segment in segments):
        raise ConfigError(f"invalid config key {key!r}; use dotted names containing letters, numbers, '_' or '-'")
    if target is not None:
        if key != "default_command":
            raise ConfigError("target scope currently supports only default_command")
        if not target.strip():
            raise ConfigError("--target requires a non-empty target name")
        return config_mod.GLOBAL_CONFIG_PATH, ("target_defaults", target, key), f"target {target!r}"
    if project:
        return (project_dir or Path.cwd()).resolve() / PROJECT_CONFIG_RELPATH, segments, "project"
    return config_mod.GLOBAL_CONFIG_PATH, segments, "user"


def _read_document(path: Path) -> Any:
    """Read a round-trippable TOML document, returning an empty document for an absent file."""
    try:
        return config_mod.tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else config_mod.tomlkit.document()
    except Exception as exc:
        raise ConfigError(f"failed to parse {path}: {exc}") from exc


def set_value(
    key: str,
    values: tuple[str, ...],
    *,
    user: bool = False,
    project: bool = False,
    target: str | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Set one config value at user, project, or target scope while preserving comments and unrelated settings.

    User scope writes ``~/.fwd/config.toml`` and is the default. Project scope writes ``<cwd>/.fwd/config.toml``.
    Target scope writes ``[target_defaults.<name>]`` in the user file; it is intentionally independent of
    ``[targets.<name>]`` so implicit targets can receive defaults without becoming incomplete backend declarations.
    """
    destination, path_segments, scope = _scope_location(key, user=user, project=project, target=target, project_dir=project_dir)
    document = _read_document(destination)
    stored_value = _config_value(key, values)
    _assign_path(document, path_segments, stored_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(config_mod.tomlkit.dumps(document), encoding="utf-8")
    displayed_value = shlex.join(stored_value) if key == "default_command" else (stored_value if isinstance(stored_value, str) else _toml_scalar(stored_value))
    ui.ok(f"set {key!r} for {scope} to {displayed_value!r} in {destination}")
    return destination


def _remove_path(document: Any, path: tuple[str, ...]) -> bool:
    """Remove one TOML leaf and prune empty parent tables; return whether the leaf existed."""
    current = document
    parents: list[tuple[Any, str]] = []
    for segment in path[:-1]:
        child = current.get(segment)
        if child is None or not hasattr(child, "__getitem__"):
            return False
        parents.append((current, segment))
        current = child
    if path[-1] not in current:
        return False
    del current[path[-1]]
    for parent, segment in reversed(parents):
        child = parent[segment]
        if len(child) != 0:
            break
        del parent[segment]
    return True


def _interactive_terminal() -> bool:
    """Return whether a human can see and answer a destructive config prompt."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not is_machine_environment()


def remove_value(
    key: str,
    *,
    user: bool = False,
    project: bool = False,
    target: str | None = None,
    project_dir: Path | None = None,
    force: bool = False,
) -> bool:
    """Remove one value at exactly one scope, confirming only when that value actually exists.

    Returns:
        ``True`` when a value was removed and ``False`` when no value existed at the selected scope.
    """
    destination, path_segments, scope = _scope_location(key, user=user, project=project, target=target, project_dir=project_dir)
    document = _read_document(destination)
    current = document
    for segment in path_segments:
        if not hasattr(current, "get") or current.get(segment) is None:
            ui.info(f"no {key!r} config exists for {scope} in {destination}")
            return False
        current = current.get(segment)

    if not force:
        if not _interactive_terminal():
            raise ConfigError(f"refusing to remove {key!r} for {scope} in non-interactive mode; re-run with --force")
        if not ui.confirm(f"remove {key!r} for {scope} from {destination}?", default=False):
            ui.info("config was not changed")
            return False

    removed = _remove_path(document, path_segments)
    if not removed:
        ui.info(f"no {key!r} config exists for {scope} in {destination}")
        return False
    destination.write_text(config_mod.tomlkit.dumps(document), encoding="utf-8")
    ui.ok(f"removed {key!r} for {scope} from {destination}")
    return True


def explain_target(name: str, project_dir: Path | None = None) -> str:
    """Return a one-line provenance description for a target name, including implicit resolution.

    Used by ``fwd config`` output and available to callers that want to tell a user *why* they got the target they got.
    """
    root = (project_dir or Path.cwd()).resolve()
    cfg = load_config(root)
    if name in cfg.targets:
        return f"{name}: declared in config ({', '.join(str(p) for p in cfg.sources)})"
    implicit = implicit_target(name)
    if implicit is not None:
        return f"{name}: not in config — synthesized from {implicit[1]} as a {implicit[0].backend} target"
    return f"{name}: not configured and not inferable"
