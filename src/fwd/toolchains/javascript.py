"""JavaScript package-manager selection and dependency setup."""

from __future__ import annotations

from pathlib import Path

from fwd.tooling import ToolRequirement, Toolchain
from fwd.tooling.requirements import BUN, NPM, NVM, PNPM, YARN


class JavaScriptToolchain(Toolchain):
    """Select one package manager and honor an independent .nvmrc runtime contract."""

    name = "javascript"
    markers = ("bun.lockb", "bun.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", ".nvmrc")
    managers = (
        ("bun.lockb", BUN, "bun install"),
        ("bun.lock", BUN, "bun install"),
        ("package-lock.json", NPM, "npm ci"),
        ("pnpm-lock.yaml", PNPM, "pnpm install --frozen-lockfile"),
        ("yarn.lock", YARN, "yarn --frozen-lockfile"),
    )

    @classmethod
    def _selection(cls, project: Path) -> tuple[ToolRequirement, str] | None:
        return next(((requirement, command) for marker, requirement, command in cls.managers if (project / marker).is_file()), None)

    @classmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        selected = cls._selection(project)
        manager = (selected[0],) if selected else ()
        return (NVM, *manager) if (project / ".nvmrc").is_file() else manager

    @classmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        selected = cls._selection(project)
        manager = (selected[1],) if selected else ()
        return ("nvm install && nvm use", *manager) if (project / ".nvmrc").is_file() else manager
