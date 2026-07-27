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
    """Reuse working remote tools and install only unresolved requirements.

    Requirements are deduplicated before probing, so an agent and project toolchain sharing an executable never race or
    install it twice. Every fallback is followed by the same version probe; an installer returning zero is not treated
    as success unless the requested command actually works.
    """
    for requirement in merge_requirements(requirements):
        ready, version = _probe(endpoint, requirement)
        if ready:
            ui.info(f"remote {requirement.name} present{f': {version}' if version else ''}")
            continue
        installed_by: str | None = None
        for installer in requirement.installers:
            ui.info(f"remote {requirement.name} missing; trying {installer.name}")
            result = endpoint.run_script(_installer_script(installer.script), check=False, stream=True)
            ready, version = _probe(endpoint, requirement)
            if ready:
                installed_by = installer.name
                break
            if result.returncode == 0:
                ui.warn(f"{installer.name} returned success but {requirement.command!r} still failed its version probe")
        if installed_by is not None:
            ui.ok(f"installed remote {requirement.name} with {installed_by}{f': {version}' if version else ''}")
            continue
        if requirement.required:
            hint = f" {requirement.hint}" if requirement.hint else ""
            raise SSHError(f"required remote tool {requirement.name!r} ({requirement.command}) is unavailable after {len(requirement.installers)} fallback installer(s).{hint}")
        ui.warn(f"optional remote tool {requirement.name!r} is unavailable")
