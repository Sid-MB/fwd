"""Default-on GitHub credential discovery, transfer, and remote Git setup.

Fwd resolves a credential from standard local abstractions instead of teaching users where each operating system
stores secrets. Resolution prefers ``GH_TOKEN``/``GITHUB_TOKEN``, then the active GitHub CLI account, Git's configured
credential helper (which covers Git Credential Manager and the macOS Keychain), and ``~/.netrc``. An interactive
launch can accept a pasted PAT as the final fallback. The secret travels to ``gh auth login --with-token`` over an OS pipe and is
never placed in argv, logs, configuration, project files, or persisted fwd session flags.
"""

from __future__ import annotations

import getpass
import netrc
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fwd import ui
from fwd.agents.remote_state import install_persistent_directory
from fwd.sshexec import SSHEndpoint, SSHError


class GitHubAuthError(RuntimeError):
    """The explicitly requested local or remote GitHub authentication setup failed."""


@dataclass(slots=True, repr=False)
class GitHubCredential:
    """Hold a transient GitHub token without exposing it through object representations."""

    token: bytearray = field(repr=False)
    source: str

    def clear(self) -> None:
        """Overwrite the retained mutable token buffer after remote installation."""
        for index in range(len(self.token)):
            self.token[index] = 0
        self.token.clear()

    def __del__(self) -> None:
        """Best-effort cleanup when an earlier launch stage exits before explicit clearing."""
        self.clear()


def _candidate(value: bytes | str | None, source: str) -> GitHubCredential | None:
    """Normalize one non-empty token candidate without rendering its value."""
    if value is None:
        return None
    raw = value.encode() if isinstance(value, str) else value
    stripped = raw.strip()
    return GitHubCredential(bytearray(stripped), source) if stripped else None


def _token_is_usable(credential: GitHubCredential) -> bool:
    """Validate a candidate through GitHub CLI when available, otherwise defer validation to the remote login."""
    if shutil.which("gh") is None:
        return True
    environment = os.environ.copy()
    environment["GH_TOKEN"] = credential.token.decode(errors="strict")
    environment.pop("GITHUB_TOKEN", None)
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "user"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
    return result.returncode == 0


def _gh_token() -> GitHubCredential | None:
    """Read the active GitHub CLI token without requiring its status output to be parsed."""
    if shutil.which("gh") is None:
        return None
    try:
        status = subprocess.run(
            ["gh", "auth", "status", "--active", "--hostname", "github.com"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if status.returncode != 0:
            return None
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _candidate(result.stdout if result.returncode == 0 else None, "the active local gh account")


def _origin_credential_query(local_cwd: Path) -> bytes:
    """Build Git's credential-protocol query, including the HTTPS origin path when one is available."""
    lines = [b"protocol=https", b"host=github.com"]
    try:
        result = subprocess.run(
            ["git", "-C", str(local_cwd), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        try:
            parsed = urlparse(result.stdout.decode().strip())
        except UnicodeError:
            parsed = None
        if parsed is not None and parsed.scheme == "https" and parsed.hostname == "github.com" and parsed.path:
            lines.append(f"path={parsed.path.lstrip('/')}".encode())
    return b"\n".join(lines) + b"\n\n"


def _git_credential(local_cwd: Path) -> GitHubCredential | None:
    """Ask Git's configured credential-helper chain without allowing a terminal password prompt."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(local_cwd), "credential", "fill"],
            input=_origin_credential_query(local_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = dict(line.split(b"=", 1) for line in result.stdout.splitlines() if b"=" in line)
    return _candidate(fields.get(b"password"), "the local Git credential helper")


def _netrc_credential() -> GitHubCredential | None:
    """Read github.com's password field from the standard netrc file when present."""
    try:
        authentication = netrc.netrc().authenticators("github.com")
    except (FileNotFoundError, netrc.NetrcParseError, OSError):
        return None
    return _candidate(authentication[2] if authentication is not None else None, "the local netrc file")


def _prompt_for_pat() -> GitHubCredential:
    """Prompt for a PAT without echoing or persisting it, retrying locally invalid values."""
    ui.info("No usable GitHub credential was found in GH_TOKEN, GITHUB_TOKEN, gh, Git's credential helper, or ~/.netrc")
    ui.info("Create a fine-grained PAT if needed: https://github.com/settings/personal-access-tokens/new (grant this repository Contents: read/write)")
    ui.info("Paste a GitHub PAT for this remote session; input is hidden and the token is not saved by fwd")
    while True:
        try:
            credential = _candidate(getpass.getpass("GitHub PAT: "), "an interactively pasted PAT")
        except (EOFError, KeyboardInterrupt) as exc:
            raise GitHubAuthError("GitHub credential entry was canceled") from exc
        if credential is None:
            ui.warn("the GitHub PAT cannot be empty")
            continue
        if _token_is_usable(credential):
            return credential
        credential.clear()
        ui.warn("GitHub rejected that PAT; check its expiration, repository access, and SSO authorization")


def resolve_local_credential(local_cwd: Path, *, interactive: bool | None = None, required: bool = True) -> GitHubCredential | None:
    """Resolve the first usable local credential, optionally returning no credential in unattended best-effort mode."""
    candidate_loaders = (
        lambda: _candidate(os.environ.get("GH_TOKEN"), "GH_TOKEN"),
        lambda: _candidate(os.environ.get("GITHUB_TOKEN"), "GITHUB_TOKEN"),
        _gh_token,
        lambda: _git_credential(local_cwd),
        _netrc_credential,
    )
    for load_candidate in candidate_loaders:
        credential = load_candidate()
        if credential is None:
            continue
        if _token_is_usable(credential):
            ui.info(f"using GitHub authentication from {credential.source}")
            return credential
        credential.clear()
    if interactive is None:
        interactive = sys.stdin.isatty()
    if interactive:
        return _prompt_for_pat()
    if not required:
        return None
    raise GitHubAuthError(
        "github.auth could not find a usable GitHub credential; set GH_TOKEN, run `gh auth login -h github.com`, "
        "configure a Git credential helper or ~/.netrc, or rerun from an interactive terminal to paste a PAT"
    )


def prepare_remote_storage(endpoint: SSHEndpoint, tool_prefix: str, *, ephemeral_home: bool) -> None:
    """Persist the standard GitHub CLI config directory when the backend's normal home is disposable."""
    if ephemeral_home:
        install_persistent_directory(endpoint, tool_prefix, ".config/gh", "github")


def remote_ready(endpoint: SSHEndpoint, tool_prefix: str, remote_dir: str) -> bool:
    """Return whether remote authentication and repository-local Git transport configuration are both ready."""
    env_file = f"{tool_prefix.rstrip('/')}/fwd-env.sh"
    token_file = f"{tool_prefix.rstrip('/')}/github/token"
    helper = f"{tool_prefix.rstrip('/')}/bin/fwd-github-credential"
    result = endpoint.run(
        "set -u; "
        f". {shlex.quote(env_file)} 2>/dev/null || exit 1; "
        f"git -C {shlex.quote(remote_dir)} config --get-all url.https://github.com/.insteadOf 2>/dev/null | grep -Fxq 'git@github.com:' || exit 1; "
        "(GH_PROMPT_DISABLED=1 gh auth status --active --hostname github.com >/dev/null 2>&1 && "
        "git config --global --get-all credential.https://github.com.helper 2>/dev/null | grep -q 'gh auth git-credential') || "
        f"(test -s {shlex.quote(token_file)} && test -x {shlex.quote(helper)} && "
        f"git -C {shlex.quote(remote_dir)} config --get-all credential.https://github.com.helper 2>/dev/null | grep -Fq {shlex.quote(helper)} && "
        f"GH_TOKEN=\"$(cat {shlex.quote(token_file)})\" GH_PROMPT_DISABLED=1 gh api --hostname github.com user >/dev/null 2>&1)",
        check=False,
    )
    return result.returncode == 0


def ensure_remote(
    endpoint: SSHEndpoint,
    local_cwd: Path,
    remote_dir: str,
    tool_prefix: str,
    *,
    required: bool = False,
) -> bool:
    """Prepare GitHub authentication inside a running session without synchronizing repository content.

    This is used by attach and agent sends because either may be the first operation after GitHub setup was enabled.
    Default-on setup remains best effort in unattended callers, while an explicit ``--setup-github`` makes absence or
    rejection of a credential an actionable failure.
    """
    if remote_ready(endpoint, tool_prefix, remote_dir):
        return True
    credential = resolve_local_credential(local_cwd, required=required)
    if credential is None:
        ui.warn("GitHub setup skipped because no usable local credential was available; use --setup-github to require it")
        return False
    try:
        from fwd import remote
        from fwd.tooling.requirements import GH

        with ui.step("Preparing GitHub authentication for existing session"):
            remote.ensure_tools(endpoint, (GH,))
            install_remote(endpoint, local_cwd, remote_dir, tool_prefix, credential)
        return True
    finally:
        credential.clear()


def _identity(local_cwd: Path, key: str) -> str | None:
    """Read one effective local Git identity value without treating absence as an error."""
    result = subprocess.run(["git", "-C", str(local_cwd), "config", "--get", key], capture_output=True, text=True, timeout=15)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def install_remote(endpoint: SSHEndpoint, local_cwd: Path, remote_dir: str, tool_prefix: str, credential: GitHubCredential) -> None:
    """Stream one resolved token to remote gh, configure HTTPS Git access, and fill missing author identity."""
    env_file = f"{tool_prefix.rstrip('/')}/fwd-env.sh"
    token_dir = f"{tool_prefix.rstrip('/')}/github"
    token_file = f"{token_dir}/token"
    helper = f"{tool_prefix.rstrip('/')}/bin/fwd-github-credential"
    helper_body = """#!/bin/sh
host=
while IFS='=' read -r key value; do
    [ "$key" = host ] && host=$value
done
[ "$host" = github.com ] || exit 0
printf 'username=x-access-token\\npassword='
tr -d '\\r\\n' <TOKEN_FILE
printf '\\n\\n'
""".replace("TOKEN_FILE", shlex.quote(token_file))
    remote = (
        "set -eu; umask 077; "
        f". {shlex.quote(env_file)}; "
        "export GH_PROMPT_DISABLED=1; "
        f"mkdir -p {shlex.quote(token_dir)}; "
        f"cat >{shlex.quote(token_file)}; chmod 600 {shlex.quote(token_file)}; "
        f"GH_TOKEN=\"$(cat {shlex.quote(token_file)})\" gh api --hostname github.com user >/dev/null || "
        f"{{ rm -f {shlex.quote(token_file)}; exit 1; }}; "
        f"if cat {shlex.quote(token_file)} | env -u GH_TOKEN -u GITHUB_TOKEN gh auth login --hostname github.com --git-protocol https --with-token >/dev/null 2>&1; then "
        "gh auth setup-git --hostname github.com; "
        f"git -C {shlex.quote(remote_dir)} config --unset-all credential.https://github.com.helper {shlex.quote('^!' + helper + '$')} 2>/dev/null || true; "
        f"rm -f {shlex.quote(token_file)}; "
        "else "
        f"printf %s {shlex.quote(helper_body)} >{shlex.quote(helper)}; chmod 700 {shlex.quote(helper)}; "
        f"git -C {shlex.quote(remote_dir)} config --replace-all credential.https://github.com.helper ''; "
        f"git -C {shlex.quote(remote_dir)} config --add credential.https://github.com.helper {shlex.quote('!' + helper)}; "
        "fi; "
        f"git -C {shlex.quote(remote_dir)} config --replace-all url.https://github.com/.insteadOf git@github.com:; "
        f"git -C {shlex.quote(remote_dir)} config --add url.https://github.com/.insteadOf ssh://git@github.com/"
    )
    remote_process: subprocess.Popen[bytes] | None = None
    try:
        remote_process = endpoint.popen(
            remote,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, remote_stderr = remote_process.communicate(input=bytes(credential.token) + b"\n", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        if remote_process is not None:
            remote_process.kill()
            remote_process.wait()
        raise GitHubAuthError(f"GitHub authentication transfer to {endpoint.ssh_target()} failed") from exc
    assert remote_process is not None
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
