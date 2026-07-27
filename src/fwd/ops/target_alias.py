"""Resolve ``fwd <target-or-backend>`` without weakening the explicit command grammar.

The root Click group consults this module only after its registered commands have failed to match, so a target named
``stop`` can never shadow ``fwd stop``. Exact configured target names win over backend shorthands. A backend shorthand
selects the target of that type used by the most recently attached/launched saved session; a sole configured target is
unambiguous even before it has history.

These aliases deliberately mean the interactive bare-fwd workflow: launch the target's configured default command and
attach. Agent and redirected invocations fail before provisioning and point to the explicit, non-attaching
``fwd up --target NAME`` spelling. Missing backend targets may be configured only after an interactive confirmation;
unknown arbitrary names never trigger a setup wizard.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from fwd import ui
from fwd.config import Config, RunpodTargetConfig, SlurmTargetConfig, SshTargetConfig, TARGET_TYPES, TargetConfig, load_config
from fwd.state import SessionState, StateStore


@dataclass(frozen=True, slots=True)
class TargetSelection:
    """A resolved configured target plus the explanation shown before any connection work begins."""

    target: TargetConfig
    reason: str


def store() -> StateStore:
    """Return the state store through a seam that tests can replace."""
    return StateStore()


def interactive_terminal() -> bool:
    """Return whether an alias may safely take over the terminal with ``ssh -t``."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(os.environ.get(name) for name in ("CLAUDECODE", "CODEX_AGENT"))


def recognized(selector: str) -> bool:
    """Return whether Click should treat ``selector`` as a dynamic command.

    Config errors are allowed through as recognized selectors when the token is a backend, so invocation reports the
    real configuration problem rather than misleadingly saying the backend command does not exist.
    """
    if selector in TARGET_TYPES:
        return True
    try:
        return selector in load_config().targets
    except Exception:
        return False


def completion_candidates() -> list[tuple[str, str]]:
    """Return target/backend root-command completions without provider calls or setup side effects."""
    candidates = {backend: f"{backend} backend · use most recently used configured target" for backend in TARGET_TYPES}
    try:
        for name, target in load_config().targets.items():
            candidates[name] = f"{target.backend} target · launch configured default and attach"
    except Exception:
        pass
    return sorted(candidates.items())


def _session_recency(session: SessionState) -> str:
    """Use attachment time when available because it captures use after the original launch."""
    return session.last_attached or session.created_at


def _most_recent_target_name(config: Config, backend: str) -> str | None:
    """Return the most recently used configured target for ``backend``, or ``None`` when history cannot decide."""
    candidates = {name for name, target in config.targets.items() if target.backend == backend}
    if len(candidates) == 1:
        return next(iter(candidates))
    matching_sessions = [session for session in store().all() if session.flags.get("target") in candidates]
    if not matching_sessions:
        return None
    return max(matching_sessions, key=_session_recency).flags["target"]


def resolve(selector: str, config: Config | None = None) -> TargetSelection | None:
    """Resolve an exact target or generic backend selector using local config and session history."""
    cfg = config or load_config()
    if selector in cfg.targets:
        return TargetSelection(cfg.targets[selector], "configured target")
    if selector not in TARGET_TYPES:
        return None
    matching = sorted(name for name, target in cfg.targets.items() if target.backend == selector)
    if not matching:
        return None
    selected_name = _most_recent_target_name(cfg, selector)
    if selected_name is None:
        ui.die(f"backend {selector!r} has multiple configured targets but no usage history selects one: {', '.join(matching)}. Name one explicitly, for example 'fwd {matching[0]}'.")
    reason = "only configured target for backend" if len(matching) == 1 else "most recently used target for backend"
    return TargetSelection(cfg.targets[selected_name], reason)


def _identity(target: TargetConfig) -> str:
    """Describe the configured connection identity without exposing keys, credentials, or provider tokens."""
    if isinstance(target, SshTargetConfig):
        endpoint = f"{target.user + '@' if target.user else ''}{target.host}:{target.port}"
        return f"ssh endpoint {endpoint}"
    if isinstance(target, SlurmTargetConfig):
        endpoint = f"{target.user + '@' if target.user else ''}{target.login_host}:{target.port}"
        extras = ", ".join(part for part in (f"partition={target.partition}" if target.partition else "", f"account={target.account}" if target.account else "") if part)
        return f"slurm login {endpoint}" + (f", {extras}" if extras else "")
    detail = target.gpu if isinstance(target, RunpodTargetConfig) and target.compute_type == "gpu" else "cpu"
    return f"runpod {detail}, {target.cloud_type} cloud"


def _setup_missing_backend(selector: str) -> TargetSelection:
    """Offer the setup wizard for a known backend, then resolve the target it created."""
    if not interactive_terminal():
        ui.die(f"no configured {selector!r} target. This is non-interactive mode, so fwd will not prompt or create one. Configure it with 'fwd setup --backend {selector} --help', then run 'fwd up --target <name>'; pass '--interactive' to fwd setup to force its wizard.")
    if not ui.confirm(f"no configured {selector!r} target exists; set one up now?", default=True):
        ui.die(f"no configured {selector!r} target; run 'fwd setup --backend {selector}' when ready")
    before = set(load_config().targets)
    from fwd import wizard

    wizard.run_wizard(force_interactive=True, backend=selector)
    config = load_config()
    created = [target for name, target in config.targets.items() if name not in before and target.backend == selector]
    if created:
        return TargetSelection(created[-1], "newly configured target")
    selection = resolve(selector, config)
    if selection is None:
        ui.die(f"setup finished without creating a configured {selector!r} target")
    return selection


def forward(selector: str) -> None:
    """Launch the resolved target's configured default command and attach in a human terminal."""
    selection = resolve(selector)
    if selection is None:
        if selector not in TARGET_TYPES:
            ui.die(f"unknown target or command {selector!r}; configure a target with 'fwd setup' or list commands with 'fwd --help'")
        selection = _setup_missing_backend(selector)
    target = selection.target
    ui.info(f"selector {selector!r} resolved to target {target.name!r} ({selection.reason}; {_identity(target)})")
    if not interactive_terminal():
        ui.die(f"target alias {selector!r} attaches interactively, but fwd is running in non-interactive mode. Run 'fwd up --target {target.name}' to launch without attaching.")
    from fwd.ops import launch as launch_ops

    launch_ops.launch(target=target.name, initial_command=None, attach=True)
