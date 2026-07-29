"""Opt-in GitHub CLI authentication transfer and remote Git setup.

The local GitHub CLI remains the credential-store abstraction: fwd never searches keychains, ``.netrc``, or
``.git-credentials``. The active token travels from ``gh auth token`` to remote ``gh auth login --with-token`` through
an OS pipe, never through argv, logs, configuration, or persisted fwd session flags.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from fwd.agents.remote_state import install_persistent_directory
from fwd.sshexec import SSHEndpoint, SSHError


class GitHubAuthError(RuntimeError):
    """The explicitly requested local or remote GitHub authentication setup failed."""


def validate_local() -> None:
    """Fail before provisioning when the local GitHub CLI has no usable active github.com account."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status", "--active", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubAuthError("github.auth requires a working local gh CLI; install gh and run `gh auth login -h github.com`") from exc
    if result.returncode != 0:
        raise GitHubAuthError("local GitHub CLI authentication is not usable; run `gh auth login -h github.com`")


def prepare_remote_storage(endpoint: SSHEndpoint, tool_prefix: str, *, ephemeral_home: bool) -> None:
    """Persist the standard GitHub CLI config directory when the backend's normal home is disposable."""
    if ephemeral_home:
        install_persistent_directory(endpoint, tool_prefix, ".config/gh", "github")


def _identity(local_cwd: Path, key: str) -> str | None:
    """Read one effective local Git identity value without treating absence as an error."""
    result = subprocess.run(["git", "-C", str(local_cwd), "config", "--get", key], capture_output=True, text=True, timeout=15)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def install_remote(endpoint: SSHEndpoint, local_cwd: Path, remote_dir: str, tool_prefix: str) -> None:
    """Stream the local gh token to the remote, configure Git credentials, and fill missing author identity."""
    env_file = f"{tool_prefix.rstrip('/')}/fwd-env.sh"
    remote = (
        "set -eu; umask 077; "
        f". {shlex.quote(env_file)}; "
        "export GH_PROMPT_DISABLED=1; "
        "gh auth login --hostname github.com --git-protocol https --with-token; "
        "gh auth setup-git --hostname github.com"
    )
    try:
        token = subprocess.Popen(
            ["gh", "auth", "token", "--hostname", "github.com"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise GitHubAuthError("could not run local `gh auth token`") from exc
    assert token.stdout is not None
    remote_process: subprocess.Popen[bytes] | None = None
    try:
        remote_process = endpoint.popen(
            remote,
            stdin=token.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        token.stdout.close()
        _, remote_stderr = remote_process.communicate(timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        if remote_process is not None:
            remote_process.kill()
            remote_process.wait()
        token.kill()
        token.wait()
        raise GitHubAuthError(f"GitHub authentication transfer to {endpoint.ssh_target()} failed") from exc
    finally:
        token.stdout.close()
    assert remote_process is not None
    assert token.stderr is not None
    token_stderr = token.stderr.read().decode(errors="replace").strip()
    token_returncode = token.wait()
    if token_returncode != 0:
        raise GitHubAuthError(f"local `gh auth token` failed" + (f": {token_stderr}" if token_stderr else ""))
    if remote_process.returncode != 0:
        detail = remote_stderr.decode(errors="replace").strip()
        raise GitHubAuthError(f"remote GitHub authentication setup failed" + (f": {detail}" if detail else ""))

    identity_commands: list[str] = []
    for key in ("user.name", "user.email"):
        value = _identity(local_cwd, key)
        if value:
            identity_commands.append(
                f"git -C {shlex.quote(remote_dir)} config --get {shlex.quote(key)} >/dev/null 2>&1 || "
                f"git -C {shlex.quote(remote_dir)} config {shlex.quote(key)} {shlex.quote(value)}"
            )
    if identity_commands:
        try:
            endpoint.run("; ".join(identity_commands), check=True)
        except SSHError as exc:
            raise GitHubAuthError(f"GitHub authentication succeeded, but remote Git identity setup failed: {exc}") from exc
