"""Install a predictable tmux experience for fwd-managed remote sessions.

The user's local tmux configuration is the best expression of their preferred key bindings, status bar, plugins, and
copy-mode behavior, so fwd transfers it when present. The generated fallback stays deliberately dependency-free: a
fresh VM should get reliable mouse scrolling and a deep history without TPM, platform-specific clipboard commands, or
terminal capabilities that may be absent on a minimal Linux image.

The remote file lives under ``~/.config/fwd`` rather than replacing ``~/.tmux.conf``. New tmux servers load it with
``tmux -f``; an already-running server receives ``source-file`` so rerunning ``fwd up`` applies changes immediately.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fwd.sshexec import SSHEndpoint, SSHError

REMOTE_TMUX_CONFIG_RELPATH = ".config/fwd/tmux.conf"
LOCAL_TMUX_CONFIG_RELPATHS: tuple[str, ...] = (".tmux.conf", ".config/tmux/tmux.conf")

DEFAULT_TMUX_CONFIG = """# Generated fallback for fwd remote sessions. A local ~/.tmux.conf or ~/.config/tmux/tmux.conf replaces this content.
set -g mouse on
set -g history-limit 100000
setw -g mode-keys vi
set -s escape-time 10
set -g focus-events on
set -g set-clipboard on
set -g renumber-windows on
bind -T copy-mode-vi v send-keys -X begin-selection
bind -T copy-mode-vi y send-keys -X copy-selection-and-cancel
bind -T copy-mode-vi C-u send-keys -X halfpage-up
bind -T copy-mode-vi C-d send-keys -X halfpage-down
"""


def local_tmux_config(home: Path | None = None) -> Path | None:
    """Return the first supported local tmux config path that exists."""
    local_home = home if home is not None else Path.home()
    for relpath in LOCAL_TMUX_CONFIG_RELPATHS:
        candidate = local_home / relpath
        if candidate.is_file():
            return candidate
    return None


def config_payload(home: Path | None = None) -> tuple[bytes, str]:
    """Return the local configuration or the dependency-free fallback plus a human-readable source label."""
    local = local_tmux_config(home)
    if local is None:
        return DEFAULT_TMUX_CONFIG.encode("utf-8"), "fwd default"
    return local.read_bytes(), str(local)


def install(endpoint: SSHEndpoint, *, home: Path | None = None) -> str:
    """Upload the selected config without overwriting the remote user's normal tmux file and reload a live server.

    Returns:
        The local path used, or ``"fwd default"`` when no supported local config exists.

    Raises:
        SSHError: If upload or live-server reload fails.
        OSError: If a selected local config cannot be read.
    """
    payload, source = config_payload(home)
    remote = f"$HOME/{REMOTE_TMUX_CONFIG_RELPATH}"
    remote_dir = f"$HOME/{Path(REMOTE_TMUX_CONFIG_RELPATH).parent.as_posix()}"
    command = f'umask 077; mkdir -p "{remote_dir}" && cat > "{remote}"'
    proc = subprocess.run([*endpoint.ssh_argv(), command], input=payload, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise SSHError(f"could not upload tmux config to {endpoint.ssh_target()}" + (f": {detail}" if detail else ""))
    reload_result = endpoint.run(
        f'if tmux list-sessions >/dev/null 2>&1; then tmux source-file "{remote}"; fi',
        check=False,
    )
    if reload_result.returncode != 0:
        raise SSHError(f"uploaded tmux config from {source}, but the running remote tmux server could not load it")
    return source
