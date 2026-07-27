"""Swift Package Manager project detection and dependency setup."""

from __future__ import annotations

from pathlib import Path

from fwd.tooling import ToolRequirement, Toolchain
from fwd.tooling.requirements import SWIFT


class SwiftToolchain(Toolchain):
    """Prepare Swift packages with the system Swift toolchain or fwd's persistent Swiftly fallback."""

    name = "swift"
    markers = ("Package.swift",)

    @classmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        """Require Swift only for projects with a top-level Package.swift manifest."""
        del project
        return (SWIFT,)

    @classmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        """Resolve package dependencies idempotently without building project products."""
        del project
        return ("swift package resolve",)
