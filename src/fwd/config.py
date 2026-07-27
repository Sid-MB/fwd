"""Configuration layer — ``~/.fwd/config.toml`` deep-merged with ``<project>/.fwd/config.toml``.

Design intent
-------------
The merge happens on the *raw dicts*, before any dataclass is constructed. That ordering is the whole point: it lets a
project file override a single field of a globally-declared target::

    # ~/.fwd/config.toml
    [targets.cluster]
    backend = "slurm"
    login_host = "login.hpc.example"
    user = "sid"
    remote_base = "/scratch/sid"
    alloc = "--time=04:00:00 --cpus-per-task=4"

    # ./.fwd/config.toml  — this project needs a GPU, everything else is inherited
    [targets.cluster]
    alloc = "--time=08:00:00 --gres=gpu:1"

If we built dataclasses per file and merged objects afterwards we could not distinguish "field absent" from "field set
to its default", and the project file would silently reset ``login_host``.

Merge rules: dicts recurse, everything else (scalars, lists) is replaced wholesale. Lists are replaced rather than
concatenated because the common case is ``[sync] exclude``, where a project must be able to *shrink* the list; additive
behaviour would make that impossible. ``DEFAULT_EXCLUDES`` is applied as the starting value, not appended at use time.

Unknown keys inside a target are dropped with a warning rather than raising, so a config written for a newer fwd (or a
typo) degrades instead of blocking every command.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

import tomlkit

from fwd import ui

GLOBAL_CONFIG_PATH = Path.home() / ".fwd" / "config.toml"
PROJECT_CONFIG_RELPATH = ".fwd/config.toml"

# Excluded from sync by default: reproducible from lockfiles, huge, or platform-specific (a macOS .venv is actively
# harmful on a Linux box). ``.git`` is deliberately NOT here — the remote session needs history for diffs and commits.
#
# These are *seeded* into SyncConfig.exclude rather than appended at transfer time, so a project that sets
# ``[sync] exclude`` can shrink the list as well as grow it — e.g. a repo that genuinely ships a checked-in ``dist/``.
# The list does not rely on .gitignore: excludes must hold even for a project without one, and B found .pnpm-store in
# particular was only being skipped because the repo happened to ignore it.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".venv",
    "node_modules",
    ".pnpm-store",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    ".next",
    "dist",
    "build",
    ".DS_Store",
)

DEFAULT_RUNPOD_CPU_IMAGE = "runpod/base:0.6.2-cpu"
DEFAULT_RUNPOD_GPU_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

# Accepted values for RunpodTargetConfig.compute_type / .cloud_type, mirroring `runpodctl pod create --help`
# (`--compute-type GPU|CPU`, `--cloud-type SECURE|COMMUNITY`). Stored lower-case; the backend upper-cases for the CLI.
RUNPOD_COMPUTE_TYPES: frozenset[str] = frozenset({"gpu", "cpu"})
RUNPOD_CLOUD_TYPES: frozenset[str] = frozenset({"secure", "community"})


class ConfigError(RuntimeError):
    """Raised for unusable configuration: unknown backend, missing target, no default target."""


@dataclass(slots=True)
class SshTargetConfig:
    """A plain SSH host that already exists. ``provision()`` is only a reachability check.

    Attributes:
        remote_base: Parent directory for project checkouts; the project name is appended to form ``remote_dir``.
    """

    name: str
    backend: Literal["ssh"] = "ssh"
    host: str = ""
    user: str = ""
    port: int = 22
    key_path: str | None = None
    proxy_jump: str | None = None
    remote_base: str = "~/fwd"
    extra_opts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunpodTargetConfig:
    """A RunPod pod, created on demand and reused across launches by name.

    Attributes:
        compute_type: ``"gpu"`` or ``"cpu"``. CPU pods are far cheaper but, as the RunPod spike established, they
            silently get **no persistent volume** — ``--volume-in-gb`` is folded into the container disk and
            everything is wiped on stop. The backend detects this from the created pod and relocates ``remote_dir``
            off the volume with a warning, so a CPU target still works; it just cannot persist across a stop.
        cloud_type: ``"secure"`` or ``"community"``. Community cloud is cheaper and was verified to expose a direct
            ``ip:port`` for 22/tcp (no ``--public-ip`` needed), so rsync still works.
        volume_mount_path: The *persistent* volume. Container disk is wiped on pod stop, so both ``remote_base`` and
            ``tool_prefix`` must live under this path or every restart re-downloads all tooling.
        allow_proxy: Permit falling back to ``ssh.runpod.io`` when no direct IP for 22/tcp is available. That proxy
            cannot run rsync, so the endpoint is marked ``supports_rsync=False`` and sync degrades to tar-over-ssh.
    """

    name: str
    backend: Literal["runpod"] = "runpod"
    compute_type: str = "cpu"
    cloud_type: str = "secure"
    gpu: str = "NVIDIA GeForce RTX 4090"
    image: str = ""
    volume_gb: int = 50
    volume_mount_path: str = "/workspace"
    remote_base: str = "/workspace"
    tool_prefix: str = "/workspace/.fwd-tools"
    user: str = "root"
    port: int = 22
    key_path: str | None = None
    allow_proxy: bool = True

    def __post_init__(self) -> None:
        """Normalize and validate the two enum-ish fields at config-load time.

        Catching a typo here — rather than when ``runpodctl pod create`` rejects it two minutes into a launch — is
        the whole point. Values are lower-cased so the config file can use whichever case reads best; the backend
        upper-cases them again for the CLI, which documents ``GPU|CPU`` and ``SECURE|COMMUNITY``.
        """
        self.compute_type = str(self.compute_type).strip().lower()
        self.cloud_type = str(self.cloud_type).strip().lower()
        if self.compute_type not in RUNPOD_COMPUTE_TYPES:
            raise ConfigError(f"target {self.name!r}: compute_type must be one of {', '.join(sorted(RUNPOD_COMPUTE_TYPES))} (got {self.compute_type!r})")
        if self.cloud_type not in RUNPOD_CLOUD_TYPES:
            raise ConfigError(f"target {self.name!r}: cloud_type must be one of {', '.join(sorted(RUNPOD_CLOUD_TYPES))} (got {self.cloud_type!r})")
        if not self.image:
            self.image = DEFAULT_RUNPOD_CPU_IMAGE if self.compute_type == "cpu" else DEFAULT_RUNPOD_GPU_IMAGE


@dataclass(slots=True)
class SlurmTargetConfig:
    """A Slurm cluster reached through its login node.

    Sync, bootstrap and dependency installs all run on the *login* node: compute nodes commonly have no internet, and
    the filesystem is shared. tmux also lives on the login node, wrapping ``salloc ... srun --pty`` so the allocation
    survives a dropped ssh connection.

    Attributes:
        remote_base: Scratch directory. Required — home quotas on HPC are far too small for checkouts plus caches.
        alloc: Flags spliced into the ``salloc`` line in the generated ``job.sh``.
        env_setup: Shell lines emitted before the allocation (``module load ...``, ``export UV_CACHE_DIR=...``).
        tool_prefix: Scratch-backed root for tooling and caches, keeping inode-heavy venvs out of ``$HOME``.
    """

    name: str
    backend: Literal["slurm"] = "slurm"
    login_host: str = ""
    user: str = ""
    port: int = 22
    key_path: str | None = None
    proxy_jump: str | None = None
    remote_base: str = ""
    alloc: str = "--time=04:00:00 --cpus-per-task=4"
    env_setup: list[str] = field(default_factory=list)
    tool_prefix: str = ""
    partition: str | None = None
    account: str | None = None


TargetConfig = SshTargetConfig | RunpodTargetConfig | SlurmTargetConfig

# Backend name -> dataclass. Also the authoritative list of valid ``backend =`` values in config.
TARGET_TYPES: dict[str, type] = {
    "ssh": SshTargetConfig,
    "runpod": RunpodTargetConfig,
    "slurm": SlurmTargetConfig,
}

# Read by :func:`ssh_config_host_aliases`. Module-level so tests can point it at a fixture.
SSH_CONFIG_PATH = Path.home() / ".ssh" / "config"

# Provenance labels for implicitly-synthesized targets, surfaced by ``fwd config``.
ORIGIN_BUILTIN = "built-in default"
ORIGIN_SSH_ALIAS = "ssh config alias"
ORIGIN_SSH_INLINE = "user@host on the command line"


def ssh_config_host_aliases(path: Path | None = None) -> set[str]:
    """Return the concrete ``Host`` aliases declared in an ssh config file.

    Deliberately a five-line parser over ``Host`` lines rather than a dependency or a shell-out to ``ssh -G``: all we
    need is the set of names a user could plausibly mean, and ``ssh -G`` would have to be invoked once per candidate
    name (it answers "resolve this host", not "list your hosts"). Wildcard patterns (``Host *``, ``Host dev-?``) are
    skipped because they match everything and would make every typo look like a real target. ``Include`` directives are
    not followed — a missed alias degrades to the normal "unknown target" error, which is the safe direction.
    """
    target_path = path if path is not None else SSH_CONFIG_PATH
    try:
        text = target_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    aliases: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, rest = stripped.partition(" ")
        if key.lower() != "host":
            continue
        aliases.update(tok for tok in rest.replace("\t", " ").split() if tok and "*" not in tok and "?" not in tok)
    return aliases


def implicit_target(name: str, *, ssh_config: Path | None = None) -> tuple[TargetConfig, str] | None:
    """Synthesize a target for a name that is absent from config, or ``None`` if the name cannot be guessed.

    This is what makes ``fwd`` usable with no config file at all. Three names are inferable without asking the user
    anything:

    - ``runpod`` — every field of :class:`RunpodTargetConfig` already has a working default, so the pure-dataclass
      instance is a valid CPU-only pod. The remaining precondition (is ``runpodctl`` installed and authenticated?) is
      deliberately *not* checked here; it belongs to the backend and to ``fwd doctor``, which report it with a fix.
    - ``user@host`` — unambiguous, so it becomes an ssh target directly.
    - an ssh config ``Host`` alias — the user has already written the connection details down once, in the file whose
      job that is. ``user`` is left empty on purpose so ssh resolves it from that same block; hardcoding the local
      username here would *override* a ``User`` directive the user explicitly set.

    Slurm is excluded by design and handled by the caller: a login host, a scratch path and an allocation spec are all
    site-specific, so there is no default that would do anything but fail confusingly a minute into a launch.

    Returns:
        ``(target, origin_label)`` where the label names the provenance for ``fwd config``, or ``None`` if the name is
        not inferable.
    """
    if name in TARGET_TYPES and name != "ssh":
        # 'runpod' resolves; 'slurm' is intentionally absent from the inferable set (see above).
        if name == "runpod":
            return RunpodTargetConfig(name="runpod"), ORIGIN_BUILTIN
        return None
    if "@" in name:
        user, _, host = name.partition("@")
        if user and host:
            return SshTargetConfig(name=name, host=host, user=user), ORIGIN_SSH_INLINE
        return None
    if name in ssh_config_host_aliases(ssh_config):
        return SshTargetConfig(name=name, host=name), ORIGIN_SSH_ALIAS
    return None


@dataclass(slots=True)
class ClaudeConfig:
    """What of the local Claude Code environment to carry to the remote machine.

    Everything that touches secrets defaults to off: ``user_config`` copies dotfiles, ``creds`` lifts an OAuth token
    out of the macOS Keychain onto a remote disk.

    ``session`` defaults **on** and ``handoff`` defaults **off**. The plan originally assumed the reverse, because of
    a suspected foreign-session validation regression; the S1 spike (docs/session-transfer-notes.md) disproved it on
    claude 2.1.220 — a relocated transcript resumes with full context. Moving the real conversation is strictly better
    than moving a summary of it, and every failure mode in the transfer is soft (the functions return ``None`` and
    warn), so the ambitious default costs nothing when it does not work.
    """

    user_config: bool = False
    creds: bool = False
    session: bool = True
    handoff: bool = False


@dataclass(slots=True)
class SyncConfig:
    """File-transfer policy.

    Attributes:
        exclude: Patterns passed to rsync as ``--exclude`` (or filtered client-side in the tar fallback).
        use_gitignore: Add ``--filter=':- .gitignore'`` so the repo's own ignore rules apply per directory.
        delete: Pass ``--delete`` on push, making the remote a mirror. Off means remote-only files survive a push.
    """

    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    use_gitignore: bool = True
    delete: bool = True


@dataclass(slots=True)
class Config:
    """Fully merged configuration for one invocation.

    Attributes:
        sources: Config files that actually contributed, in precedence order. Surfaced by ``fwd doctor`` so users can
            tell which file set a surprising value.
    """

    default_target: str | None = None
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    sources: list[Path] = field(default_factory=list)

    def target(self, name: str | None = None) -> TargetConfig:
        """Resolve which target to use.

        Precedence is explicit flag, then ``default_target``, then — as a convenience for the overwhelmingly common
        single-target setup — the sole configured target.

        An explicit name absent from config falls through to :func:`implicit_target`, so ``fwd up --target runpod`` and
        ``fwd up --target sid@gpu.example.com`` work with no config file at all. **Configured targets always win**: the
        lookup in ``self.targets`` happens first, so declaring ``[targets.runpod]`` overrides the built-in rather than
        competing with it, and no implicit guess can shadow something the user wrote down.

        Raises:
            ConfigError: If nothing is configured and nothing is inferable, the name is unknown, or the choice is
                ambiguous.
        """
        if name:
            if name in self.targets:
                return self.targets[name]
            implicit = implicit_target(name)
            if implicit is not None:
                return implicit[0]
            raise ConfigError(self._unknown_target_message(name))
        # Checked before the empty-config guard so `default_target = "runpod"` alone is a complete config file.
        if self.default_target:
            if self.default_target in self.targets:
                return self.targets[self.default_target]
            implicit = implicit_target(self.default_target)
            if implicit is not None:
                return implicit[0]
            raise ConfigError(f"default_target {self.default_target!r} is not defined in [targets] and is not an inferable name")
        if not self.targets:
            raise ConfigError(
                "No target is configured or selected.\n\n"
                "Launch now without a config file:\n"
                "  fwd up --target runpod       Provision a CPU pod\n"
                "  fwd up --target user@host    Use an existing SSH machine\n\n"
                "To save a default, run 'fwd setup'. To configure manually, run 'fwd config --example' and add "
                f"[targets.<name>] to {GLOBAL_CONFIG_PATH}."
            )
        if len(self.targets) == 1:
            return next(iter(self.targets.values()))
        raise ConfigError(f"multiple targets configured; pass --target or set default_target: {', '.join(self.target_names())}")

    def target_names(self) -> list[str]:
        """Return configured target names, sorted for stable help/error output."""
        return sorted(self.targets)

    def _unknown_target_message(self, name: str) -> str:
        """Build the error for a name that is neither configured nor inferable.

        ``slurm`` gets its own sentence because it is the one backend that *looks* like it should be inferable — the
        other two names work — and a user who tried it deserves to know it is a deliberate refusal rather than a bug.
        """
        if name == "slurm":
            return (
                "target 'slurm' cannot be inferred: a cluster needs a site-specific login host, a scratch path and an "
                "allocation spec, and guessing any of them would fail a minute into a launch. Run 'fwd setup' to define "
                "one, or 'fwd config --example slurm' for a commented reference to paste into ~/.fwd/config.toml."
            )
        known = ", ".join(self.target_names()) if self.targets else "none configured"
        return (
            f"unknown target {name!r} (configured targets: {known}). A name is only inferred when it is 'runpod', looks "
            f"like user@host, or matches a Host alias in ~/.ssh/config. Run 'fwd setup' or 'fwd config --example'."
        )


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``, returning a new dict.

    Nested dicts merge key-by-key; every other type (including lists) is replaced. Neither input is mutated.
    """
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def parse_target(name: str, raw: dict[str, Any]) -> TargetConfig:
    """Build the right target dataclass from a merged ``[targets.<name>]`` table.

    Args:
        name: Target name from the table header; injected as the dataclass ``name`` so targets are self-describing.
        raw: Merged key/value mapping for this target.

    Raises:
        ConfigError: If ``backend`` is missing or not one of ``ssh``/``runpod``/``slurm``.
    """
    backend = raw.get("backend")
    if not backend:
        raise ConfigError(f"target {name!r} is missing 'backend' (one of: {', '.join(sorted(TARGET_TYPES))})")
    cls = TARGET_TYPES.get(str(backend))
    if cls is None:
        raise ConfigError(f"target {name!r} has unknown backend {backend!r}; expected one of: {', '.join(sorted(TARGET_TYPES))}")
    allowed = {f.name for f in fields(cls)} - {"name", "backend"}
    kwargs = {k: v for k, v in raw.items() if k in allowed}
    unknown = sorted(set(raw) - allowed - {"name", "backend"})
    if unknown:
        # Warn, don't fail: a typo or a key from a newer fwd shouldn't block the user's launch.
        ui.warn(f"target {name!r}: ignoring unknown option(s) {', '.join(unknown)}")
    return cls(name=name, **kwargs)


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file into plain Python containers, or ``{}`` if absent.

    Raises:
        ConfigError: If the file exists but does not parse, since silently ignoring it would hide user edits.
    """
    if not path.is_file():
        return {}
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    except Exception as exc:
        raise ConfigError(f"failed to parse {path}: {exc}") from exc


def load_config(project_dir: str | Path | None = None) -> Config:
    """Load and merge global + project configuration.

    Args:
        project_dir: Project root to look for ``.fwd/config.toml`` in; defaults to the current directory.

    Returns:
        A :class:`Config` with dataclasses built from the merged raw tables.

    Raises:
        ConfigError: On unparseable TOML or an invalid target definition.
    """
    root = Path(project_dir) if project_dir is not None else Path.cwd()
    project_path = root / PROJECT_CONFIG_RELPATH

    sources = [p for p in (GLOBAL_CONFIG_PATH, project_path) if p.is_file()]
    merged = deep_merge(_read_toml(GLOBAL_CONFIG_PATH), _read_toml(project_path))

    claude_raw = merged.get("claude", {}) or {}
    sync_raw = merged.get("sync", {}) or {}
    targets_raw = merged.get("targets", {}) or {}

    claude = ClaudeConfig(
        user_config=bool(claude_raw.get("user_config", False)),
        creds=bool(claude_raw.get("creds", False)),
        session=bool(claude_raw.get("session", True)),
        handoff=bool(claude_raw.get("handoff", False)),
    )
    sync = SyncConfig(
        exclude=list(sync_raw.get("exclude", DEFAULT_EXCLUDES)),
        use_gitignore=bool(sync_raw.get("use_gitignore", True)),
        delete=bool(sync_raw.get("delete", True)),
    )
    targets = {name: parse_target(name, raw or {}) for name, raw in targets_raw.items()}

    default_target = merged.get("default_target")
    return Config(
        default_target=str(default_target) if default_target else None,
        claude=claude,
        sync=sync,
        targets=targets,
        sources=sources,
    )
