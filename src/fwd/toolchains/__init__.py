"""Explicit built-in toolchain registry and aggregate project planning.

The registry is intentionally explicit rather than filesystem-discovered: a contributor adds one conforming class and
one import/tuple entry, while startup remains deterministic and broken optional modules cannot appear silently.
"""

from __future__ import annotations

from pathlib import Path

from fwd.toolchains.javascript import JavaScriptToolchain
from fwd.toolchains.python import PythonToolchain
from fwd.tooling import Toolchain, ToolchainPlan, merge_requirements

TOOLCHAINS: tuple[type[Toolchain], ...] = (PythonToolchain, JavaScriptToolchain)
PROJECT_SETUP_RELPATH = ".fwd/setup.sh"


def plan(project: str | Path) -> ToolchainPlan:
    """Detect every applicable ecosystem and return one ordered, deduplicated remote setup plan."""
    root = Path(project).expanduser()
    detected = tuple(toolchain for toolchain in TOOLCHAINS if toolchain.detect(root))
    requirements = merge_requirements(*(toolchain.requirements(root) for toolchain in detected))
    commands = tuple(command for toolchain in detected for command in toolchain.dependency_commands(root))
    if (root / PROJECT_SETUP_RELPATH).is_file():
        commands = (*commands, f"bash {PROJECT_SETUP_RELPATH}")
    return ToolchainPlan(names=tuple(toolchain.name for toolchain in detected), requirements=requirements, commands=commands)


__all__ = ["PROJECT_SETUP_RELPATH", "TOOLCHAINS", "JavaScriptToolchain", "PythonToolchain", "plan"]
