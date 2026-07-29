#!/usr/bin/env python3
"""Deterministic in-process performance benchmarks for fwd's command surface.

The command cases invoke Typer directly, so Python executable startup and shell lookup do not obscure changes in fwd itself. Provider, SSH, transfer, deletion, installation, and interactive boundaries are replaced once, outside the timed region. This makes every public command safe to repeat while retaining its parsing and local dispatch path.

Command dispatch is supplemented with workload cases for code whose cost is otherwise hidden behind a mocked safety boundary. Those cases cover session listing and state decoding, config loading/rendering, and Git upload selection. Add a workload whenever a command gains a substantial local phase; merely timing a mocked handler would not reveal a regression in that phase.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import gc
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from fwd import cli, completion_setup, config as config_mod, doctor, skill_setup, ui, wizard
from fwd.config import SyncConfig, load_config
from fwd.ops import attach, configcmd, diff as diff_ops, launch, lifecycle, send, session_select, transfer, uninstall
from fwd.output import OutputFormat
from fwd.selection import upload_manifest
from fwd.state import SessionState, StateStore

Operation = Callable[[], None]


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One repeatable operation whose setup and external boundaries live outside the timed call."""

    name: str
    group: str
    operation: Operation


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Stable summary saved to baselines; individual noisy samples are deliberately omitted."""

    name: str
    group: str
    median_ms: float
    p95_ms: float
    loops_per_sample: int
    samples: int


class _StaticStore:
    """Read-only store used by command benchmarks so listing and selection never touch the user's session state."""

    def __init__(self, sessions: Sequence[SessionState] = ()) -> None:
        self._sessions = list(sessions)

    def all(self) -> list[SessionState]:
        """Return a fresh list because production callers are allowed to sort or filter their snapshot."""
        return list(self._sessions)


class _StaticTaskStore:
    """Read-only empty task store used by ``ls`` while its actual local rendering logic remains benchmarked."""

    def all(self) -> list[Any]:
        """Return no tasks without reading the user's durable task file."""
        return []


def _session(project: Path, index: int = 0) -> SessionState:
    """Build deterministic session data representative of one stored row."""
    timestamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    return SessionState(
        name=f"benchmark-{index:03d}",
        backend="ssh",
        local_cwd=str(project),
        remote_dir=f"/remote/benchmark-{index:03d}",
        tmux_session=f"fwd-benchmark-{index:03d}",
        endpoint={"host": "benchmark.invalid", "user": "bench"},
        backend_ids={"id": str(index)},
        created_at=timestamp,
        started_at=timestamp,
        last_attached=timestamp,
    )


def _invoke(runner: CliRunner, arguments: Sequence[str]) -> Operation:
    """Return a checked in-process CLI invocation suitable for repeated timing."""

    def operation() -> None:
        result = runner.invoke(cli.app, list(arguments), color=False)
        if result.exit_code != 0:
            raise RuntimeError(f"{' '.join(arguments) or '<bare fwd>'} failed during benchmark: {result.output}") from result.exception

    return operation


def _manifest_operation(project: Path) -> Operation:
    """Return a Git selection workload that consumes the manifest before its temporary directory disappears."""

    def operation() -> None:
        with upload_manifest(project, SyncConfig()) as manifest:
            if manifest is None:
                raise RuntimeError("benchmark Git repository did not produce an upload manifest")
            manifest.read_bytes()

    return operation


def _prepare_git_project(project: Path) -> None:
    """Create a medium synthetic repository with tracked, untracked, nested-ignored, and fwd-excluded paths."""
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    (project / ".gitignore").write_text("ignored/\n**/.cache/\n", encoding="utf-8")
    (project / ".fwdignore").write_text("generated/\n", encoding="utf-8")
    for index in range(300):
        tracked = project / "src" / f"module_{index:04d}.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text(f"VALUE = {index}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", ".gitignore", "src"], check=True)
    for directory in ("ignored", "nested/.cache", "generated", "notes"):
        for index in range(100):
            path = project / directory / f"item_{index:04d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{directory}:{index}\n", encoding="utf-8")


def _assert_command_coverage(command_arguments: dict[str, tuple[str, ...]]) -> None:
    """Fail when a registered command lacks a benchmark, so the claimed full command coverage cannot silently drift."""
    covered_root = {arguments[0] for arguments in command_arguments.values() if arguments}
    expected_root = {command.name or command.callback.__name__ for command in cli.app.registered_commands}
    expected_root.add("config")
    missing_root = expected_root - covered_root
    public_config_commands = {command.name or command.callback.__name__ for command in cli.config_app.registered_commands if not command.hidden}
    covered_config_commands = {arguments[1] for arguments in command_arguments.values() if len(arguments) > 1 and arguments[0] == "config" and not arguments[1].startswith("-")}
    missing_config = public_config_commands - covered_config_commands
    if missing_root or missing_config:
        missing = sorted({*missing_root, *(f"config {name}" for name in missing_config)})
        raise RuntimeError(f"registered commands missing benchmarks: {', '.join(missing)}")


@contextmanager
def benchmark_cases() -> Iterator[list[BenchmarkCase]]:
    """Build every safe command and workload case inside one isolated, consistently patched environment."""
    with tempfile.TemporaryDirectory(prefix="fwd-benchmarks-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        previous_cwd = Path.cwd()
        os.chdir(project)
        stack.callback(os.chdir, previous_cwd)
        global_config = root / "config.toml"
        stack.enter_context(patch.object(config_mod, "GLOBAL_CONFIG_PATH", global_config))

        primary_session = _session(project)
        selection = session_select.CurrentSelection(
            selector=session_select.SessionSelector(name=primary_session.name),
            config=config_mod.Config(),
            sessions=(primary_session,),
            cwd=project,
            matches=(primary_session,),
        )
        stack.enter_context(patch.object(cli, "_interactive_terminal", return_value=True))
        stack.enter_context(patch.object(completion_setup, "offer_once", return_value=None))
        stack.enter_context(patch.object(skill_setup, "offer_once", return_value=None))
        stack.enter_context(patch.object(skill_setup, "update_if_needed", return_value=None))
        stack.enter_context(patch.object(cli, "_run_up", return_value=None))
        stack.enter_context(patch.object(session_select, "select_current", return_value=selection))
        stack.enter_context(patch.object(attach, "attach", return_value=None))
        stack.enter_context(patch.object(send, "dispatch", return_value=0))
        stack.enter_context(patch.object(launch, "store", return_value=_StaticStore()))
        stack.enter_context(patch.object(lifecycle, "task_store", return_value=_StaticTaskStore()))
        stack.enter_context(patch.object(lifecycle, "_live_status", return_value="stopped"))
        stack.enter_context(patch.object(ui, "table", return_value=None))
        stack.enter_context(patch.object(ui, "show_code_examples", return_value=None))
        stack.enter_context(patch.object(transfer, "push", return_value=None))
        stack.enter_context(patch.object(transfer, "pull", return_value=None))
        stack.enter_context(patch.object(diff_ops, "diff", return_value=0))
        stack.enter_context(patch.object(lifecycle, "stop", return_value=None))
        stack.enter_context(patch.object(lifecycle, "remove", return_value=None))
        stack.enter_context(patch.object(lifecycle, "remove_all", return_value=None))
        stack.enter_context(patch.object(uninstall, "uninstall", return_value=0))
        stack.enter_context(patch.object(cli, "_set_config_value", return_value=None))
        stack.enter_context(patch.object(configcmd, "remove_value", return_value=None))
        stack.enter_context(patch.object(wizard, "run_wizard", return_value=None))
        stack.enter_context(patch.object(doctor, "run_doctor", return_value=0))

        runner = CliRunner()
        command_arguments: dict[str, tuple[str, ...]] = {
            "bare": (),
            "up": ("up",),
            "launch-alias": ("launch",),
            "attach": ("attach", primary_session.name),
            "attach-alias": ("a", primary_session.name),
            "send": ("send", "--ls"),
            "send-alias": ("s", "--ls"),
            "ls": ("ls", "--format", "json"),
            "push": ("push",),
            "pull": ("pull",),
            "diff": ("diff", "--quiet"),
            "stop": ("stop",),
            "rm": ("rm", "--force"),
            "rm-all": ("rm", "--all", "--force"),
            "uninstall": ("uninstall", "--force"),
            "config-show": ("config",),
            "config-schema": ("config", "--schema"),
            "config-example": ("config", "--example", "all"),
            "config-set": ("config", "set", "sync.delete", "false"),
            "config-rm": ("config", "rm", "sync.delete", "--force"),
            "default": ("default", "codex"),
            "setup": ("setup", "--backend", "runpod"),
            "doctor": ("doctor", "--format", "json"),
            "info": ("info", "--format", "json"),
            "version": ("version",),
        }
        _assert_command_coverage(command_arguments)
        cases = [BenchmarkCase(name=name, group="command", operation=_invoke(runner, arguments)) for name, arguments in command_arguments.items()]

        many_sessions = tuple(_session(project, index) for index in range(100))

        def list_many_sessions() -> None:
            with patch.object(launch, "store", return_value=_StaticStore(many_sessions)):
                lifecycle.ls(output_format=OutputFormat.json)

        state_path = root / "state.json"
        state_store = StateStore(state_path)
        for session in many_sessions:
            state_store.upsert(session)

        _prepare_git_project(project)
        cases.extend(
            [
                BenchmarkCase("ls-100-sessions", "workload", list_many_sessions),
                BenchmarkCase("state-read-100-sessions", "workload", lambda: state_store.all()),
                BenchmarkCase("config-load", "workload", lambda: load_config(project)),
                BenchmarkCase("config-render-example", "workload", lambda: configcmd.render_example("all")),
                BenchmarkCase("config-render-schema", "workload", configcmd.render_schema),
                BenchmarkCase("git-upload-manifest", "workload", _manifest_operation(project)),
            ]
        )
        yield cases


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a nearest-rank percentile without requiring enough samples for ``statistics.quantiles``."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _run_case(case: BenchmarkCase, *, warmups: int, samples: int, target_ms: float) -> BenchmarkResult:
    """Calibrate and measure one case, reporting per-operation latency rather than per-sample batch latency."""
    for _ in range(warmups):
        case.operation()
    started = time.perf_counter_ns()
    case.operation()
    elapsed_ns = max(1, time.perf_counter_ns() - started)
    target_ns = max(1, int(target_ms * 1_000_000))
    loops = max(1, min(10_000, target_ns // elapsed_ns))
    measurements: list[float] = []
    gc_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(samples):
            started = time.perf_counter_ns()
            for _ in range(loops):
                case.operation()
            measurements.append((time.perf_counter_ns() - started) / loops / 1_000_000)
    finally:
        if gc_enabled:
            gc.enable()
    return BenchmarkResult(
        name=case.name,
        group=case.group,
        median_ms=statistics.median(measurements),
        p95_ms=_percentile(measurements, 0.95),
        loops_per_sample=loops,
        samples=samples,
    )


def _load_baseline(path: Path) -> dict[str, BenchmarkResult]:
    """Load results written by this tool, tolerating added top-level metadata in future versions."""
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document["results"] if isinstance(document, dict) else document
    return {row["name"]: BenchmarkResult(**row) for row in rows}


def _document(results: Sequence[BenchmarkResult]) -> dict[str, Any]:
    """Build the stable JSON artifact used for storage and later comparisons."""
    return {
        "format_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "results": [asdict(result) for result in results],
    }


def _print_results(results: Sequence[BenchmarkResult], baseline: dict[str, BenchmarkResult] | None) -> None:
    """Render a compact table while keeping JSON output clean when requested."""
    print(f"{'benchmark':30} {'group':10} {'median':>10} {'p95':>10} {'change':>10}")
    for result in results:
        previous = baseline.get(result.name) if baseline else None
        change = f"{((result.median_ms / previous.median_ms) - 1) * 100:+.1f}%" if previous and previous.median_ms else "-"
        print(f"{result.name:30} {result.group:10} {result.median_ms:9.3f}ms {result.p95_ms:9.3f}ms {change:>10}")


def _regressions(results: Sequence[BenchmarkResult], baseline: dict[str, BenchmarkResult], *, maximum_percent: float, minimum_ms: float) -> list[str]:
    """Return only material regressions, combining a relative threshold with a noise-floor absolute threshold."""
    failures = []
    for result in results:
        previous = baseline.get(result.name)
        if previous is None or previous.median_ms <= 0:
            continue
        delta_ms = result.median_ms - previous.median_ms
        delta_percent = delta_ms / previous.median_ms * 100
        if delta_percent > maximum_percent and delta_ms > minimum_ms:
            failures.append(f"{result.name}: {previous.median_ms:.3f} ms -> {result.median_ms:.3f} ms ({delta_percent:+.1f}%)")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    """Run selected benchmarks, optionally saving or comparing the stable median summaries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", action="append", default=[], help="Run names containing this text; repeat for an OR filter.")
    parser.add_argument("--group", choices=("command", "workload"), help="Run only command dispatch or substantive local workloads.")
    parser.add_argument("--warmups", type=int, default=3, help="Untimed warmup calls per benchmark.")
    parser.add_argument("--samples", type=int, default=15, help="Timed samples per benchmark.")
    parser.add_argument("--target-ms", type=float, default=25.0, help="Approximate duration of each calibrated sample.")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result document.")
    parser.add_argument("--save", type=Path, help="Write results as a future comparison baseline.")
    parser.add_argument("--compare", type=Path, help="Compare medians with a prior --save artifact.")
    parser.add_argument("--max-regression", type=float, default=20.0, help="Fail when median regression exceeds this percentage.")
    parser.add_argument("--min-regression-ms", type=float, default=0.10, help="Ignore smaller absolute changes as timer noise.")
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.samples <= 0 or args.target_ms <= 0:
        parser.error("--warmups must be nonnegative; --samples and --target-ms must be positive")

    with benchmark_cases() as available:
        selected = [
            case
            for case in available
            if (args.group is None or case.group == args.group) and (not args.filter or any(fragment in case.name for fragment in args.filter))
        ]
        if not selected:
            parser.error("no benchmarks matched the selected group/filter")
        results = [_run_case(case, warmups=args.warmups, samples=args.samples, target_ms=args.target_ms) for case in selected]

    baseline = _load_baseline(args.compare) if args.compare else None
    document = _document(results)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        _print_results(results, baseline)
    if baseline:
        failures = _regressions(results, baseline, maximum_percent=args.max_regression, minimum_ms=args.min_regression_ms)
        if failures:
            print("\nTiming regressions:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
