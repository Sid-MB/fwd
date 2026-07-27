"""One source of truth for loading fwd's persistent environment in non-login remote shells."""

from __future__ import annotations

FWD_ENV_RELPATH = "fwd-env.sh"
HOME_ENV_RELPATH = ".fwd-env.sh"


def source_env() -> str:
    """Return the guarded shell prefix that loads the backend-specific persistent tool environment.

    A normal ``ssh host command`` shell reads neither `.bashrc` nor `.profile`, and the tool prefix differs across SSH,
    RunPod, and Slurm. Bootstrap therefore writes a fixed `$HOME/.fwd-env.sh` pointer; the explicit-prefix branch keeps
    lower-level callers working when they export `FWD_TOOL_PREFIX` themselves.
    """
    return (
        f'if [ -f "$HOME/{HOME_ENV_RELPATH}" ]; then . "$HOME/{HOME_ENV_RELPATH}"; '
        f'elif [ -n "${{FWD_TOOL_PREFIX:-}}" ] && [ -f "$FWD_TOOL_PREFIX/{FWD_ENV_RELPATH}" ]; then . "$FWD_TOOL_PREFIX/{FWD_ENV_RELPATH}"; fi; '
    )
