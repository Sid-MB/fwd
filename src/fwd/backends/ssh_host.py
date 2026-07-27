"""SSH backend — a machine that already exists.

The trivial case, and therefore the reference implementation of the protocol: ``provision`` does not create anything,
it only builds the endpoint from config and confirms reachability. ``remote_dir`` defaults to
``<remote_base>/<project>``. Owned by the core/ssh teammate.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import ClassVar

from fwd.backends.base import CheckResult, ProvisionError, TargetInfo, TargetStatus
from fwd.config import Config, SshTargetConfig
from fwd.remote import tmux_kill
from fwd.sshexec import SSHEndpoint, SSHError, wait_for_ssh
from fwd.state import SessionState

# A static host has no persistent-storage subtleties (no wiped container disk, no inode quota), so the tool prefix can
# just live in the user's home. RunPod/Slurm override this with a config-driven path.
DEFAULT_TOOL_PREFIX = "~/.fwd-tools"

# Guard for destroy(): refuse to rm -rf anything this shallow, no matter what state says.
PROTECTED_REMOTE_DIRS = frozenset({"", "/", "/root", "/home", "/workspace", "/tmp", "/usr", "/var", "/etc"})


class SshHostBackend:
    """Provisioner over a static SSH host (see :class:`fwd.backends.base.Provisioner`)."""

    name: ClassVar[str] = "ssh"

    def __init__(self, target: SshTargetConfig, config: Config) -> None:
        self.target = target
        self.config = config

    def _build_endpoint(self) -> SSHEndpoint:
        """Translate the target config into an endpoint, validating the fields ssh cannot do without."""
        if not self.target.host:
            raise ProvisionError(f"target {self.target.name!r}: 'host' is required for the ssh backend")
        return SSHEndpoint(
            host=self.target.host,
            user=self.target.user,
            port=self.target.port,
            key_path=self.target.key_path,
            proxy_jump=self.target.proxy_jump,
            supports_rsync=True,
            extra_opts=list(self.target.extra_opts),
        )

    @staticmethod
    def _expand_remote(endpoint: SSHEndpoint, path: str, home: str | None = None) -> str:
        """Resolve a leading ``~`` against the *remote* home directory.

        Paths from config (``remote_base = "~/fwd"``) are meaningless to rsync and to ``rm -rf`` unless they are
        absolute, and the local home is almost never the remote one. We resolve once per provision and reuse.
        """
        if not path.startswith("~"):
            return path
        if home is None:
            home = endpoint.run('printf "%s" "$HOME"').stdout.strip()
        if not home:
            raise ProvisionError("could not resolve remote $HOME to expand a '~' path")
        return home + path[1:]

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Build the endpoint from config, verify reachability, and ensure ``remote_dir`` exists.

        ``gpu`` is accepted for protocol conformance and ignored — a static host has whatever hardware it has.
        """
        endpoint = self._build_endpoint()
        # Shorter timeout than a cloud provisioner: a configured host is either up now or it is not, there is no boot
        # sequence to wait through.
        if not wait_for_ssh(endpoint, timeout=60.0, interval=3.0):
            raise ProvisionError(f"cannot reach {endpoint.ssh_target()} over ssh (target {self.target.name!r})")

        try:
            endpoint.open_control_master()
        except SSHError:
            # Multiplexing is a speed optimization, never a requirement; individual commands still connect fine.
            pass
        home = endpoint.run('printf "%s" "$HOME"').stdout.strip()
        remote_base = self._expand_remote(endpoint, self.target.remote_base, home)
        remote_dir = f"{remote_base.rstrip('/')}/{project_name}"
        tool_prefix = self._expand_remote(endpoint, DEFAULT_TOOL_PREFIX, home)

        endpoint.run(f"mkdir -p {shlex.quote(remote_dir)} {shlex.quote(tool_prefix)}")
        return TargetInfo(
            endpoint=endpoint,
            remote_dir=remote_dir,
            status=TargetStatus.RUNNING,
            backend_ids={},
            tool_prefix=tool_prefix,
            scratch=f"{tool_prefix.rstrip('/')}/scratch",
        )

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Return the configured endpoint; a static host's address never churns.

        Config wins over the endpoint cached in state so that editing ``~/.fwd/config.toml`` (new port, new key) takes
        effect on the next attach without forcing the user to delete the session.
        """
        return self._build_endpoint()

    def status(self, session: SessionState) -> TargetStatus:
        """``RUNNING`` if ssh succeeds, else ``STOPPED`` (the host may simply be powered down).

        Deliberately never ``GONE``: a static host that does not answer is far more likely to be rebooting or behind a
        dropped VPN than permanently deleted, and ``GONE`` invites callers to prune state that is still valid.
        """
        try:
            endpoint = self._build_endpoint()
        except ProvisionError:
            # An unbuildable endpoint means the *target config* is broken (no host), which says nothing about whether
            # the machine exists. ``UNKNOWN`` keeps this off the offer-to-prune path, matching the promise above.
            return TargetStatus.UNKNOWN
        return TargetStatus.RUNNING if wait_for_ssh(endpoint, timeout=8.0, interval=2.0) else TargetStatus.STOPPED

    def stop(self, session: SessionState) -> None:
        """Kill the session's tmux only; fwd never powers off a machine it did not create."""
        if not session.tmux_session:
            return
        try:
            tmux_kill(self._build_endpoint(), session.tmux_session)
        except (SSHError, ProvisionError):
            # Stop must be safe on an unreachable host: there is nothing left to kill if we cannot connect.
            pass

    def destroy(self, session: SessionState) -> None:
        """Remove the remote project directory; never touches the host itself.

        The path deleted comes from state, not from config, and is re-checked against ``PROTECTED_REMOTE_DIRS`` — a
        corrupt or hand-edited state entry must not be able to turn this into ``rm -rf /``.
        """
        self.stop(session)
        remote_dir = (session.remote_dir or "").rstrip("/")
        if not remote_dir or remote_dir in PROTECTED_REMOTE_DIRS or not remote_dir.startswith("/"):
            raise ProvisionError(f"refusing to delete unsafe remote_dir {session.remote_dir!r}")
        try:
            self._build_endpoint().run(f"rm -rf {shlex.quote(remote_dir)}")
        except SSHError as exc:
            raise ProvisionError(f"failed to remove {remote_dir}: {exc}") from exc

    def doctor(self) -> list[CheckResult]:
        """Check that host/user are configured, the key exists, and ssh connects."""
        results: list[CheckResult] = []
        for tool, hint in (("ssh", "install OpenSSH client"), ("rsync", "install rsync (brew install rsync / apt install rsync)")):
            path = shutil.which(tool)
            results.append(CheckResult(name=f"local {tool}", ok=path is not None, detail=path or "not found", hint=None if path else hint))

        if not self.target.host:
            results.append(CheckResult(name="config", ok=False, detail="no host set", hint=f"set host in [targets.{self.target.name}]"))
            return results
        results.append(CheckResult(name="config", ok=True, detail=f"{self.target.user or '<default user>'}@{self.target.host}:{self.target.port}"))

        if self.target.key_path:
            key = Path(self.target.key_path).expanduser()
            results.append(
                CheckResult(name="ssh key", ok=key.is_file(), detail=str(key), hint=None if key.is_file() else "key_path does not exist")
            )

        endpoint = self._build_endpoint()
        reachable = wait_for_ssh(endpoint, timeout=10.0, interval=2.0)
        results.append(
            CheckResult(
                name="ssh reachable",
                ok=reachable,
                detail=endpoint.ssh_target(),
                hint=None if reachable else "check the host is up and your key is authorized (fwd uses BatchMode, so no password prompts)",
            )
        )
        if not reachable:
            return results

        # Remote-side prerequisites. tmux is fatal at launch time; curl only gates the bootstrap installs.
        for tool, fatal in (("tmux", True), ("curl", False)):
            found = endpoint.run(f"command -v {tool}", check=False).returncode == 0
            results.append(
                CheckResult(
                    name=f"remote {tool}",
                    ok=found or not fatal,
                    detail="present" if found else "missing",
                    hint=None if found else f"install {tool} on the remote host" + ("" if fatal else " to enable automatic tool installs"),
                )
            )
        return results
