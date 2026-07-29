"""Persistent remote-home relocation for agents running on targets whose normal home is ephemeral.

RunPod erases ``/root`` on every stop, including authentication, conversations, settings, and Codex's managed
standalone payload. A GPU pod's fwd tool prefix lives on its persistent volume, so an agent can keep its entire
product-owned home directory there while retaining the conventional ``$HOME/.codex`` or ``$HOME/.claude`` path via a
symlink. CPU pods use the same mechanism even though their tool prefix is also ephemeral; they still rebuild cleanly,
but cannot promise persistence when the provider offers no durable storage.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from fwd.sshexec import SSHEndpoint


def install_persistent_directory(endpoint: SSHEndpoint, tool_prefix: str, home_relative: str, state_name: str) -> None:
    """Relocate one home-relative directory beneath ``tool_prefix`` without discarding pre-existing remote data.

    A first launch moves the existing directory intact. If persistent state and a freshly-created home directory both
    exist, missing files are merged into persistent state and the original directory is retained as a timestamped
    migration backup. An unrelated symlink or non-directory fails rather than overwriting a remote user's choice.
    """
    relative = PurePosixPath(home_relative)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"persistent home path must be a safe relative path, got {home_relative!r}")
    if not state_name or "/" in state_name or state_name in {".", ".."}:
        raise ValueError(f"persistent state name must be one path component, got {state_name!r}")
    state_dir = f"{tool_prefix.rstrip('/')}/agent-state/{state_name}"
    quoted_state = shlex.quote(state_dir)
    quoted_relative = shlex.quote(home_relative)
    script = f"""
set -euo pipefail
state_dir={quoted_state}
home_path="$HOME"/{quoted_relative}
state_parent="${{state_dir%/*}}"
mkdir -p "$state_parent" "$(dirname "$home_path")"
if [ -L "$home_path" ]; then
    current_target="$(readlink "$home_path")"
    if [ "$current_target" = "$state_dir" ]; then
        mkdir -p "$state_dir"
        chmod 700 "$state_dir"
        exit 0
    fi
    printf '%s\\n' "refusing to replace unrelated agent-home symlink $home_path -> $current_target" >&2
    exit 1
fi
if [ -e "$home_path" ] && [ ! -d "$home_path" ]; then
    printf '%s\\n' "refusing to replace non-directory agent home $home_path" >&2
    exit 1
fi
if [ -d "$home_path" ] && [ ! -e "$state_dir" ]; then
    mv "$home_path" "$state_dir"
elif [ -d "$home_path" ]; then
    mkdir -p "$state_dir"
    cp -an "$home_path"/. "$state_dir"/
    backup="$home_path.fwd-migrated-$(date +%s)"
    mv "$home_path" "$backup"
    printf '%s\\n' "preserved pre-existing agent home at $backup" >&2
else
    mkdir -p "$state_dir"
fi
chmod 700 "$state_dir"
ln -s "$state_dir" "$home_path"
""".strip()
    endpoint.run(script, check=True, timeout=120)


def install_persistent_home(endpoint: SSHEndpoint, tool_prefix: str, home_entry: str) -> None:
    """Relocate one product-owned hidden directory using its basename as the persistent state name."""
    if not home_entry.startswith(".") or "/" in home_entry:
        raise ValueError(f"agent home entry must be one hidden path component, got {home_entry!r}")
    install_persistent_directory(endpoint, tool_prefix, home_entry, home_entry.removeprefix("."))
