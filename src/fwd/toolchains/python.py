"""Python project detection and dependency setup."""

from __future__ import annotations

from pathlib import Path

from fwd.tooling import ToolRequirement, Toolchain
from fwd.tooling.requirements import UV


class PythonToolchain(Toolchain):
    """Use uv for locked projects, pyproject projects, and requirements files."""

    name = "python"
    markers = ("uv.lock", "pyproject.toml", "requirements.txt")

    @classmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        del project
        return (UV,)

    @classmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        if (project / "uv.lock").is_file() or (project / "pyproject.toml").is_file():
            return ("uv sync",)
        if (project / "requirements.txt").is_file():
            return ("uv venv && uv pip install -r requirements.txt",)
        return ()
