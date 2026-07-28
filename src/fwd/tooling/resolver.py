"""Probe, install, and verify remote tools through one shared SSH implementation."""

from __future__ import annotations

import shlex

from fwd import ui
from fwd.remote_env import HOME_ENV_RELPATH, source_env
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.tooling.base import ToolRequirement, merge_requirements


def _probe(endpoint: SSHEndpoint, requirement: ToolRequirement) -> tuple[bool, str]:
    """Return whether an executable resolves and its version probe succeeds."""
    version = shlex.join(requirement.version_command)
    command = f"{source_env()}command -v {shlex.quote(requirement.command)} >/dev/null 2>&1 && {version}"
    result = endpoint.run(command, check=False)
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode == 0, output[0] if output else ""


def _installer_script(script: str) -> str:
    """Wrap one installer with the persistent fwd environment and strict shell behavior."""
    return f"""set -euo pipefail
if [ -f "$HOME/{HOME_ENV_RELPATH}" ]; then . "$HOME/{HOME_ENV_RELPATH}"; fi
: "${{FWD_TOOL_PREFIX:?fwd bootstrap environment is missing FWD_TOOL_PREFIX}}"
mkdir -p "$FWD_TOOL_PREFIX/bin"
{script}
"""


def ensure_tools(endpoint: SSHEndpoint, requirements: tuple[ToolRequirement, ...]) -> None:
    """Resolve root tools and installer-specific prerequisites recursively.

    Successful commands are cached by executable for the whole pass, so agent, toolchain, and prerequisite graphs
    share one probe/install result. Prerequisite failure skips only that installer path: Codex can try npm, then Bun,
    without making either an unconditional dependency. Cycles are configuration errors and fail with the full path.
    """
    roots = merge_requirements(requirements)
    resolved: set[str] = set()
    resolving: list[str] = []

    def resolve(requirement: ToolRequirement, *, fatal: bool) -> bool:
        if requirement.command in resolved:
            return True
        if requirement.command in resolving:
            start = resolving.index(requirement.command)
            cycle = (*resolving[start:], requirement.command)
            raise SSHError(f"tool prerequisite cycle detected: {' -> '.join(cycle)}")
        ready, version = _probe(endpoint, requirement)
        if ready:
            ui.info(f"remote {requirement.name} present{f': {version}' if version else ''}")
            resolved.add(requirement.command)
            return True
        resolving.append(requirement.command)
        try:
            for installer in requirement.installers:
                unavailable = tuple(prerequisite.name for prerequisite in installer.requirements if not resolve(prerequisite, fatal=False))
                if unavailable:
                    ui.info(f"skipping {requirement.name} installer {installer.name}; unavailable prerequisite(s): {', '.join(unavailable)}")
                    continue
                ui.info(f"remote {requirement.name} missing; trying {installer.name}")
                result = endpoint.run_script(_installer_script(installer.script), check=False, stream=True)
                ready, version = _probe(endpoint, requirement)
                if ready:
                    resolved.add(requirement.command)
                    ui.ok(f"installed remote {requirement.name} with {installer.name}{f': {version}' if version else ''}")
                    return True
                if result.returncode == 0:
                    ui.warn(f"{installer.name} returned success but {requirement.command!r} still failed its version probe")
                else:
                    ui.warn(f"{requirement.name} installer {installer.name} failed (exit {result.returncode})")
        finally:
            resolving.pop()
        if fatal:
            hint = f" {requirement.hint}" if requirement.hint else ""
            recovery = f" To repair the running target manually, use {ui.command('attach --raw')!r}; install the missing tool, exit the recovery shell, then rerun the normal launch."
            raise SSHError(f"required remote tool {requirement.name!r} ({requirement.command}) is unavailable after {len(requirement.installers)} installer path(s).{hint}{recovery}")
        return False

    for requirement in roots:
        if not resolve(requirement, fatal=requirement.required):
            ui.warn(f"optional remote tool {requirement.name!r} is unavailable")
