"""Reusable rich shell completion callbacks for fwd's discoverable CLI values.

Callbacks return ``(value, help)`` tuples, the Typer representation that lets Fish and Zsh show descriptions beside
completion candidates. They only inspect local state/configuration: shell completion must stay fast, must never
provision resources, and must not turn a provider outage into a broken Tab key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from typer import _click

from fwd import ui
from fwd.agents import AGENTS
from fwd.backends import backend_names
from fwd.config import DEFAULT_RUNPOD_CPU_IMAGE, DEFAULT_RUNPOD_GPU_IMAGE, RunpodTargetConfig, SlurmTargetConfig, SshTargetConfig, load_config, ssh_config_host_aliases
from fwd.output import OutputFormat
from fwd.send_tasks import SendTaskStore
from fwd.state import SessionState, StateStore

Completion = tuple[str, str]
CompletionCallback = Callable[[_click.Context, list[str], str], list[Completion]]


def _session_store() -> StateStore:
    """Return the state store through a seam tests can replace without touching a user's real home directory."""
    return StateStore()


def _matches(items: Iterable[Completion], incomplete: str) -> list[Completion]:
    """Filter and sort rich candidates so every callback behaves consistently."""
    return sorted((item for item in items if item[0].startswith(incomplete)), key=lambda item: item[0])


def static_completer(items: Iterable[Completion]) -> CompletionCallback:
    """Build a Typer callback for a fixed set of documented choices."""
    choices = tuple(items)

    def complete(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
        del ctx, args
        return _matches(choices, incomplete)

    return complete


def _session_help(session: SessionState) -> str:
    """Build the concise description shown by shells that support completion help text."""
    target = session.flags.get("target") or "unspecified target"
    directory = Path(session.local_cwd).name or session.local_cwd
    attached = session.last_attached.replace("T", " ")[:16] if session.last_attached else "never attached"
    return f"{session.backend} · target={target} · dir={directory} · last={attached}"


def complete_session(ctx: _click.Context, args: list[str], incomplete: str) -> list[tuple[str, str]]:
    """Complete saved session names with backend/target/path/recency tooltips.

    Completion must never fail the shell or perform provider/network work. Corrupt or temporarily locked state simply
    yields no suggestions; the command itself will still report the real error if the user submits a name.
    """
    del ctx, args
    try:
        sessions = _session_store().all()
    except Exception:
        return []
    return _matches(((session.name, _session_help(session)) for session in sessions), incomplete)


def complete_session_selector(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Complete the shared session/target/backend/agent positional grammar used by ``up`` and ``attach``."""
    candidates: dict[str, str] = {}
    try:
        candidates.update({session.name: _session_help(session) for session in _session_store().all()})
    except Exception:
        pass
    candidates.update(dict(complete_target(ctx, args, incomplete)))
    candidates.update(dict(complete_agent(ctx, args, incomplete)))
    return _matches(candidates.items(), incomplete)


def complete_existing_session(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Complete exact sessions plus target-label and backend aliases for commands that manage existing sessions."""
    del ctx, args
    try:
        sessions = _session_store().all()
    except Exception:
        return []
    candidates: dict[str, str] = {}
    target_sessions: dict[str, list[str]] = {}
    backend_sessions: dict[str, list[str]] = {}
    for session in sessions:
        candidates[session.name] = _session_help(session)
        target = str(session.flags.get("target") or "")
        if target:
            target_sessions.setdefault(target, []).append(session.name)
        backend_sessions.setdefault(session.backend, []).append(session.name)
    for target, names in target_sessions.items():
        detail = f"session={names[0]}" if len(names) == 1 else f"{len(names)} saved sessions"
        candidates.setdefault(target, f"configured target alias · {detail}")
    for backend, names in backend_sessions.items():
        detail = f"session={names[0]}" if len(names) == 1 else f"{len(names)} saved sessions"
        candidates.setdefault(backend, f"{backend} backend alias · {detail}")
    return _matches(candidates.items(), incomplete)


complete_diff_target = complete_existing_session


def complete_send_subject(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Complete durable task IDs and agent selectors for attach/stop/send operations."""
    del ctx
    canceling = bool(args and args[0] == "cancel")
    if len(args) > (2 if canceling else 1):
        return []
    if canceling:
        candidates: dict[str, str] = {"stopafter": "cancel queued remote shutdown", "all": "cancel every active task"}
    else:
        candidates = {
            "agent": f"agent running in this {ui.command()} session",
            "stopafter": "queue remote shutdown after all active tasks",
            "cancel": "cancel queued tasks or stop-after",
        }
        candidates.update({name: f"explicit {name} agent selector" for name in AGENTS})
    try:
        for task in SendTaskStore().all():
            if task.active:
                candidates[task.id] = f"{task.agent or task.kind} · {task.status} · session={task.session} · {task.label}"
    except Exception:
        pass
    return _matches(candidates.items(), incomplete)


def _target_help(target: SshTargetConfig | RunpodTargetConfig | SlurmTargetConfig) -> str:
    """Describe a configured target with the fields that most clearly distinguish it."""
    if isinstance(target, SshTargetConfig):
        return f"ssh · {target.user + '@' if target.user else ''}{target.host or '<host unset>'}"
    if isinstance(target, RunpodTargetConfig):
        detail = target.gpu if target.compute_type == "gpu" and target.gpu else target.compute_type
        return f"runpod · {detail} · {target.cloud_type}"
    partition = f" · partition={target.partition}" if target.partition else ""
    return f"slurm · {target.login_host or '<login host unset>'}{partition}"


def complete_target(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Complete configured targets plus zero-config RunPod and OpenSSH-alias targets."""
    del ctx, args
    candidates: dict[str, str] = {"runpod": "built-in RunPod target · CPU by default"}
    try:
        config = load_config()
        candidates.update({name: _target_help(target) for name, target in config.targets.items()})
    except Exception:
        pass
    try:
        for alias in ssh_config_host_aliases():
            candidates.setdefault(alias, "OpenSSH Host alias · zero-config SSH target")
    except Exception:
        pass
    return _matches(candidates.items(), incomplete)


def complete_ssh_host(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Complete concrete aliases from ``~/.ssh/config`` while still allowing arbitrary hosts."""
    del ctx, args
    try:
        aliases = ((alias, "OpenSSH Host alias") for alias in ssh_config_host_aliases())
        return _matches(aliases, incomplete)
    except Exception:
        return []


def complete_gpu(ctx: _click.Context, args: list[str], incomplete: str) -> list[Completion]:
    """Suggest locally configured GPU strings without making a provider API call."""
    del ctx, args
    candidates: dict[str, str] = {"NVIDIA GeForce RTX 4090": "RunPod GPU identifier · free text also accepted"}
    try:
        for target in load_config().targets.values():
            if isinstance(target, RunpodTargetConfig) and target.gpu:
                candidates[target.gpu] = f"GPU configured by target {target.name!r}"
    except Exception:
        pass
    return _matches(candidates.items(), incomplete)


_AGENT_HELP = {
    "claude": "Claude Code · sync context and auto-attach in a terminal",
    "codex": "Codex · sync settings, config, and skills; auto-attach in a terminal",
}
complete_agent = static_completer((name, _AGENT_HELP.get(name, "registered coding agent · auto-attach in a terminal")) for name in AGENTS)
complete_backend = static_completer((name, f"{name} backend") for name in backend_names())
complete_compute_type = static_completer((("cpu", "CPU-only compute · default"), ("gpu", "GPU compute")))
complete_cloud_type = static_completer((("secure", "RunPod Secure Cloud · default"), ("community", "RunPod Community Cloud")))
complete_runpod_image = static_completer(((DEFAULT_RUNPOD_CPU_IMAGE, "default CPU image"), (DEFAULT_RUNPOD_GPU_IMAGE, "default GPU/PyTorch image")))
complete_output_format = static_completer(
    (
        (OutputFormat.auto.value, "Rich in a terminal; Markdown for agents and pipes"),
        (OutputFormat.rich.value, "styled terminal table"),
        (OutputFormat.markdown.value, "plain Markdown table"),
        (OutputFormat.json.value, "structured JSON"),
    )
)
complete_example_backend = static_completer(
    (
        ("all", "complete example for every backend"),
        ("ssh", "SSH target example"),
        ("runpod", "RunPod target example"),
        ("slurm", "Slurm target example"),
    )
)
complete_config_key = static_completer(
    (
        ("default_command", f"argv launched by bare {ui.command()}"),
        ("default_target", "target used when --target is omitted"),
        ("claude.user_config", "sync ~/.claude settings and extensions"),
        ("claude.creds", "copy Claude OAuth credentials to the remote"),
        ("claude.session", "transfer and resume the Claude transcript"),
        ("claude.handoff", "use HANDOFF.md instead of the transcript"),
        ("sync.exclude", "replacement list of sync exclusion patterns"),
        ("sync.use_gitignore", "honor per-directory .gitignore rules"),
        ("sync.delete", "delete remote-only files while pushing"),
    )
)
