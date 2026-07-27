"""Preflight diagnostics — ``fwd doctor``.

Design intent
-------------
Doctor exists for the moment something is already broken, which dictates two rules.

First, **nothing here may raise**. A diagnostic tool that crashes on the very misconfiguration it is meant to explain
is worse than useless, so every check runs inside :func:`_safe` and a thrown exception becomes a failed row.

Second, **"not implemented yet" is not "broken"**. A ``NotImplementedError`` from a backend means fwd cannot answer
the question, not that the user's machine is misconfigured. Those rows render as ``skip`` and do not affect the exit
code, so someone running doctor against a healthy ssh target is never told their setup is broken because an unrelated
backend is unfinished. The same applies to genuinely optional tooling: ``tmux`` is needed on the *remote* side, so its
absence locally is information, not an error.

Checks run cheapest-first — local binaries, then config parse, then per-target backend probes that may hit the
network — so the fast and most-likely-wrong things surface immediately.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from fwd import claude_state, ui
from fwd.backends import make_backend
from fwd.backends.base import CheckResult
from fwd.config import Config, ConfigError, TargetConfig, load_config
from fwd.state import StateStore

# Prefix on CheckResult.detail marking a check that could not run. Encoded in the detail string rather than as a new
# field because CheckResult is a shared contract owned by backends/base.py and must not grow doctor-specific state.
SKIP_PREFIX = "skipped:"


def _skip(name: str, detail: str) -> CheckResult:
    """Build a row for a check that could not run and must not count as a failure."""
    return CheckResult(name=name, ok=True, detail=f"{SKIP_PREFIX} {detail}")


def _is_skipped(result: CheckResult) -> bool:
    """Return whether a result represents a check that could not run rather than one that passed."""
    return result.detail.startswith(SKIP_PREFIX)


def _safe(name: str, fn: Callable[[], CheckResult], *, hint: str | None = None) -> CheckResult:
    """Run one check, converting any exception into a row instead of letting it escape."""
    try:
        return fn()
    except NotImplementedError:
        return _skip(name, "not implemented yet")
    except Exception as exc:
        return CheckResult(name=name, ok=False, detail=str(exc), hint=hint)


def _which_check(name: str, binary: str, *, hint: str = "", required: bool = True) -> CheckResult:
    """Check for a local binary on PATH.

    Args:
        required: When ``False`` a missing binary is a skip, for tools only needed remotely (tmux) or on some code
            paths (claude, which only matters for ``--handoff``).
    """
    path = shutil.which(binary)
    if path:
        return CheckResult(name=name, ok=True, detail=path)
    if not required:
        return _skip(name, f"{binary} not found locally")
    return CheckResult(name=name, ok=False, detail=f"{binary} not found on PATH", hint=hint)


def _state_check() -> CheckResult:
    """Confirm the state file is readable and report how many sessions it holds."""
    sessions = StateStore().all()
    return CheckResult(name="state", ok=True, detail=f"{len(sessions)} session(s) in ~/.fwd/state.json")


def _keychain_check() -> CheckResult:
    """Check that Claude credentials are readable from the macOS Keychain.

    Only meaningful on darwin and only relevant to ``--creds``, so a missing entry is a skip, not a failure.
    """
    if sys.platform != "darwin":
        return _skip("claude credentials", "not macOS")
    if claude_state.read_keychain_creds():
        return CheckResult(name="claude credentials", ok=True, detail="readable from Keychain")
    return _skip("claude credentials", "no Keychain entry found (needed only for --creds)")


def _config_check(project_dir: Path) -> tuple[CheckResult, Config | None]:
    """Parse the merged configuration, reporting which files contributed.

    Returns both the row and the config, because every later check needs the parsed result and re-loading it would
    risk reporting one state while checking against another.
    """
    try:
        cfg = load_config(project_dir)
    except ConfigError as exc:
        return CheckResult(name="config", ok=False, detail=str(exc), hint="fix the file or run 'fwd setup'"), None
    if not cfg.sources:
        return (
            CheckResult(
                name="config",
                ok=False,
                detail="no config file found",
                hint="run 'fwd setup' to create ~/.fwd/config.toml",
            ),
            cfg,
        )
    sources = ", ".join(str(p) for p in cfg.sources)
    return CheckResult(name="config", ok=True, detail=f"{len(cfg.targets)} target(s) from {sources}"), cfg


def _local_checks(cfg: Config | None) -> list[CheckResult]:
    """Run the checks that do not touch the network."""
    results = [
        _safe("ssh", lambda: _which_check("ssh", "ssh", hint="install OpenSSH")),
        _safe("rsync", lambda: _which_check("rsync", "rsync", hint="install rsync for fast delta transfers")),
        _safe("tar", lambda: _which_check("tar", "tar", hint="install tar (used by the no-rsync fallback)")),
        _safe("tmux", lambda: _which_check("tmux", "tmux", required=False)),
        _safe("claude", lambda: _which_check("claude", "claude", required=False)),
    ]
    # runpodctl only matters when the user actually has a RunPod target configured.
    if cfg and any(t.backend == "runpod" for t in cfg.targets.values()):
        results.append(
            _safe(
                "runpodctl",
                lambda: _which_check("runpodctl", "runpodctl", hint="install runpodctl, then run 'runpodctl config'"),
            )
        )
    results.append(_safe("state", _state_check))
    results.append(_safe("claude credentials", _keychain_check))
    return results


def _backend_checks(cfg: Config, tcfg: TargetConfig) -> list[CheckResult]:
    """Run one target's backend doctor, namespacing its rows and absorbing its failures."""
    label = f"{tcfg.name} ({tcfg.backend})"
    try:
        checks = make_backend(tcfg, cfg).doctor()
    except NotImplementedError:
        return [_skip(label, "backend checks not implemented yet")]
    except Exception as exc:
        return [CheckResult(name=label, ok=False, detail=str(exc))]
    if not checks:
        return [CheckResult(name=label, ok=True, detail="no checks reported")]
    return [
        CheckResult(name=f"{tcfg.name}: {c.name}", ok=c.ok, detail=c.detail, hint=c.hint) for c in checks
    ]


def _target_checks(cfg: Config, target: str | None) -> list[CheckResult]:
    """Run backend doctors for one target or for every configured target."""
    if target:
        try:
            targets = [cfg.target(target)]
        except ConfigError as exc:
            return [CheckResult(name=f"target {target}", ok=False, detail=str(exc))]
    else:
        targets = [cfg.targets[name] for name in cfg.target_names()]

    results: list[CheckResult] = []
    for tcfg in targets:
        results.extend(_backend_checks(cfg, tcfg))
    return results


def run_doctor(target: str | None = None) -> int:
    """Run all diagnostics and print a results table.

    Args:
        target: Limit backend checks to one target; ``None`` checks every configured target.

    Returns:
        Process exit code: ``0`` if nothing failed, ``1`` otherwise. Skipped checks never fail the run.
    """
    config_result, cfg = _config_check(Path.cwd())

    results = _local_checks(cfg)
    results.append(config_result)
    if cfg and cfg.targets:
        results.extend(_target_checks(cfg, target))

    rows = []
    for result in results:
        if _is_skipped(result):
            mark, detail = "[dim]skip[/]", result.detail[len(SKIP_PREFIX) :].strip()
        elif result.ok:
            mark, detail = "[green]ok[/]", result.detail
        else:
            mark, detail = "[red]FAIL[/]", result.detail
            if result.hint:
                detail = f"{detail} — {result.hint}"
        rows.append([result.name, mark, detail])
    ui.table("fwd doctor", ["check", "status", "detail"], rows)

    failures = [r for r in results if not r.ok]
    if failures:
        ui.error(f"{len(failures)} check(s) failed: {', '.join(r.name for r in failures)}")
        return 1
    ui.ok("all checks passed")
    return 0
