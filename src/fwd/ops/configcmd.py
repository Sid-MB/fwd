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
from dataclasses import MISSING, fields
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from fwd import config as config_mod
from fwd import ui
from fwd.config import (
    DEFAULT_EXCLUDES,
    PROJECT_CONFIG_RELPATH,
    RUNPOD_CLOUD_TYPES,
    RUNPOD_COMPUTE_TYPES,
    ClaudeConfig,
    Config,
    ConfigError,
    SyncConfig,
    TARGET_TYPES,
    implicit_target,
    load_config,
)

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
    "compute_type": f"one of: {' | '.join(sorted(RUNPOD_COMPUTE_TYPES))} — cpu pods get NO persistent volume",
    "cloud_type": f"one of: {' | '.join(sorted(RUNPOD_CLOUD_TYPES))} — community is cheaper and works fully",
    "gpu": "RunPod GPU id; override per launch with 'fwd up --gpu'",
    "image": "container image the pod boots",
    "volume_gb": "persistent volume size in GB (gpu pods only)",
    "volume_mount_path": "where the persistent volume is mounted",
    "tool_prefix": "where fwd installs uv/node/bun; must be on persistent storage or every restart re-downloads",
    "allow_proxy": "permit the ssh.runpod.io fallback when no direct IP exists (that proxy cannot run rsync)",
    "alloc": "flags spliced into the salloc line",
    "env_setup": "shell lines run before the allocation, e.g. module loads",
    "partition": "slurm partition (-p)",
    "account": "slurm account (-A)",
}

# Field docs that differ by backend, because the same field name carries a different warning per provider.
TARGET_FIELD_DOCS: dict[str, dict[str, str]] = {
    "runpod": {"remote_base": "parent dir for checkouts; MUST be under volume_mount_path or it is wiped on stop"},
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
    "exclude": "rsync excludes; REPLACES the built-in list rather than adding to it, so it can shrink",
    "use_gitignore": "also honour the repo's own .gitignore, per directory",
    "delete": "push mirrors local, removing remote-only files",
}

# Fields with no usable default that the user must fill in for the target to work at all. Emitted uncommented with a
# placeholder so the example is a working skeleton; everything else optional is emitted commented out.
REQUIRED_PLACEHOLDERS: dict[str, dict[str, Any]] = {
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
}

EXAMPLE_TARGET_NAMES: dict[str, str] = {"ssh": "box", "runpod": "pod", "slurm": "hpc"}


def _toml_scalar(value: Any) -> str:
    """Render a Python scalar as TOML. Only the types the config schema actually uses are supported."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
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


def _provenance(project_dir: Path) -> tuple[dict[tuple[str, ...], str], Path, Path]:
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
    origins, global_path, project_path = _provenance(project_dir)
    lines = [
        "# Effective fwd configuration (global + project, deep-merged). Values are annotated with their source.",
        f"#   global  = {global_path}{'' if global_path.is_file() else '  (absent)'}",
        f"#   project = {project_path}{'' if project_path.is_file() else '  (absent)'}",
        "#   default = built into fwd, not written anywhere",
        "# Run 'fwd config --example' for a commented reference of every available field.",
        "",
    ]

    default_target = cfg.default_target
    if default_target is None:
        lines.append("# default_target is unset  # default")
    else:
        lines.append(_annotated("default_target", default_target, ("default_target",), origins))

    for section, obj in (("claude", cfg.claude), ("sync", cfg.sync)):
        lines += ["", f"[{section}]"]
        for key, value in _dataclass_items(obj):
            lines.append(_annotated(key, value, (section, key), origins))

    if not cfg.targets:
        lines += [
            "",
            "# No targets are configured. 'fwd up --target runpod' and 'fwd up --target user@host' still work — those",
            "# names are inferred without config. Run 'fwd setup' or 'fwd config --example' to declare one.",
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
    """Render a commented ``[claude]`` or ``[sync]`` block from its dataclass defaults."""
    lines = [f"[{section}]"]
    for key, value in _dataclass_items(obj):
        comment = SECTION_DOCS.get(key, "")
        lines.append(f"{key} = {_toml_scalar(value)}" + (f"  # {comment}" if comment else ""))
    return lines


def render_example(which: str = "all") -> str:
    """Render a commented reference config for one backend or all of them.

    Args:
        which: ``"ssh"``, ``"runpod"``, ``"slurm"`` or ``"all"``.

    Returns:
        Valid TOML. Every value shown is the real dataclass default (or a marked placeholder for fields that have no
        usable default), so this text can be pasted into ``~/.fwd/config.toml`` and edited down.
    """
    backends = list(EXAMPLE_TARGET_NAMES) if which == "all" else [which]
    lines = [
        "# fwd configuration reference — generated from fwd's own dataclasses, so it matches this exact version.",
        "# Global file: ~/.fwd/config.toml.  Per-project override: <project>/.fwd/config.toml, which DEEP-MERGES over",
        "# the global one, so a repo can change a single field of a target without restating the rest.",
        "# Commented-out lines are optional fields shown with a plausible value, not defaults being applied.",
        "# Inspect what your own files actually resolve to with 'fwd config'.",
        "#",
        "# You may not need any of this: 'fwd up --target runpod' provisions a CPU pod from built-in defaults, and",
        "# 'fwd up --target user@host' (or any Host alias in ~/.ssh/config) works with no config file at all.",
        "",
        f'default_target = "{EXAMPLE_TARGET_NAMES[backends[0]]}"  # used when --target is omitted',
    ]
    for backend in backends:
        lines += ["", *_render_example_target(backend)]
    lines += ["", *_render_example_section("claude", ClaudeConfig())]
    lines += ["", *_render_example_section("sync", SyncConfig())]
    lines += [
        "",
        f"# The built-in exclude list, for reference: {', '.join(DEFAULT_EXCLUDES)}",
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
    if origin in (Union, UnionType):
        non_null = [arg for arg in args if arg is not type(None)]
        if len(non_null) == 1 and len(non_null) != len(args):
            return {"anyOf": [_json_type(non_null[0]), {"type": "null"}]}
    return {"type": {str: "string", int: "integer", bool: "boolean"}.get(annotation, "string")}


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
        "title": "fwd configuration",
        "description": "Configuration for ~/.fwd/config.toml and the deep-merged per-project .fwd/config.toml override.",
        "type": "object",
        "properties": {
            "default_target": {"type": "string", "description": "Target used when --target is omitted."},
            "claude": _section_schema(ClaudeConfig, SECTION_DOCS),
            "sync": _section_schema(SyncConfig, SECTION_DOCS),
            "targets": {"type": "object", "description": "Named remote targets.", "additionalProperties": {"oneOf": target_refs}},
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
        ui.info("run 'fwd setup' for the wizard, or 'fwd config --example' for a commented reference to start from")
        ui.info("or skip config entirely: 'fwd up --target runpod', or 'fwd up --target user@host'")
    ui.raw(render_effective(cfg, root))


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
