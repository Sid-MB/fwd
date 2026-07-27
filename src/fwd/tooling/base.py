"""Small class-based contracts for discovering projects and describing their remote tools.

Toolchains own project-specific decisions, while the resolver owns all remote probing and installation mechanics. This
keeps a new language integration focused on its markers, required commands, and dependency steps without duplicating
SSH, PATH, persistence, logging, or error handling. Coding agents return the same ``ToolRequirement`` values, so shared
dependencies are probed once and installed consistently.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ToolInstaller:
    """One ordered, user-space fallback for a missing remote executable."""

    name: str
    script: str


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    """A remote executable, its health probe, and ordered installation fallbacks."""

    name: str
    command: str
    version_command: tuple[str, ...]
    installers: tuple[ToolInstaller, ...] = ()
    required: bool = True
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class ToolchainPlan:
    """The requirements and dependency commands produced by detected toolchains."""

    names: tuple[str, ...] = ()
    requirements: tuple[ToolRequirement, ...] = ()
    commands: tuple[str, ...] = ()


class Toolchain(ABC):
    """Base class for one project ecosystem.

    Simple toolchains declare ``markers`` and implement only requirements and dependency commands. Toolchains with
    globbed or conditional manifests can override ``detect`` while retaining the same launch integration.
    """

    name: ClassVar[str]
    markers: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def detect(cls, project: Path) -> bool:
        """Return whether this toolchain applies to ``project`` using exact top-level markers by default."""
        return any((project / marker).is_file() for marker in cls.markers)

    @classmethod
    @abstractmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        """Return remote tools needed by this project."""

    @classmethod
    @abstractmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        """Return idempotent dependency setup commands in execution order."""


def merge_requirements(*groups: tuple[ToolRequirement, ...]) -> tuple[ToolRequirement, ...]:
    """Deduplicate compatible requirements by executable while rejecting ambiguous definitions."""
    merged: dict[str, ToolRequirement] = {}
    for group in groups:
        for requirement in group:
            existing = merged.get(requirement.command)
            if existing is not None and existing != requirement:
                raise ValueError(f"conflicting tool requirements for {requirement.command!r}: {existing.name!r} and {requirement.name!r}")
            merged[requirement.command] = requirement
    return tuple(merged.values())
