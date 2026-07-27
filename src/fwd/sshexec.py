"""SSH execution layer — the single chokepoint for every remote interaction in fwd.

Design intent
-------------
Every other module talks to a remote machine by holding an :class:`SSHEndpoint` and calling its methods. Nothing
else in the codebase is allowed to build ``ssh`` argv by hand. This keeps three cross-cutting concerns in one place:

1. **ControlMaster multiplexing.** A launch does 6+ round trips (reachability probe, rsync, bootstrap, dep install,
   Claude state upload, tmux create, attach). Without multiplexing that is 6+ TCP+auth handshakes, which is slow and
   on some clusters triggers rate limiting. We open one master socket under ``~/.fwd/cm/`` and every subsequent call
   rides it via ``-o ControlPath``.
2. **Transport quirks per backend.** RunPod's proxy host (``ssh.runpod.io``) cannot run rsync, Slurm logins often need
   a ``ProxyJump``. Those differences live as *data* on the endpoint (``supports_rsync``, ``proxy_jump``,
   ``extra_opts``) so callers branch on flags instead of re-deriving transport rules.
3. **TTY fidelity.** Attaching to a remote tmux must feel native. :meth:`SSHEndpoint.exec_interactive` therefore
   ``execvp``s and replaces the Python process — we never proxy interactive I/O through Python buffers, which is the
   usual cause of broken resize/mouse/ctrl-C behaviour (see plan's "Attach fidelity" risk).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

# ControlMaster sockets live outside the project so they survive cwd changes and are easy to clean up wholesale.
CONTROL_DIR = Path.home() / ".fwd" / "cm"

# A Unix domain socket path is capped by sizeof(sun_path): 104 bytes on macOS/BSD, 108 on Linux. Take the smaller.
# Blowing it produces ssh's opaque "unix_listener: path ... too long for Unix domain socket" and kills every command.
SOCKET_PATH_LIMIT = 104

# While setting a master up, ssh writes to "<ControlPath>.<16 random chars>" and only renames on success, so the real
# budget is the limit minus that suffix.
SOCKET_SUFFIX_BUDGET = 20

# Applied to every invocation. BatchMode makes a missing key fail fast instead of hanging on a password prompt inside a
# spinner, and accept-new adds unknown hosts without the interactive yes/no that would deadlock a non-tty launch.
BASE_SSH_OPTS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
)

# ControlPersist keeps the master alive between the ~6 round trips of one launch, and long enough that an immediate
# follow-up command (attach right after up) reuses it too.
CONTROL_PERSIST_DEFAULT = "10m"

# Probes must fail fast so wait_for_ssh polls rather than blocking for the OS default (~2 minutes).
PROBE_CONNECT_TIMEOUT = 5


def _env_prelude(env: dict[str, str] | None) -> str:
    """Render ``env`` as a sequence of ``export K=V;`` statements.

    We prepend assignments to the remote command instead of using ssh's ``SendEnv``, because ``AcceptEnv`` on the
    remote sshd almost never lists our variables and the silently-dropped variables are very hard to debug.
    """
    if not env:
        return ""
    return "".join(f"export {key}={shlex.quote(str(value))}; " for key, value in env.items())


def control_dir() -> Path:
    """Return the directory to hold ControlMaster sockets, falling back when ``~/.fwd/cm`` is too deep.

    ``~/.fwd/cm`` is the normal answer and keeps sockets with the rest of fwd's state. But the socket path is bounded
    by ``sun_path``, and a long ``$HOME`` (CI runners, macOS ``/var/folders/...`` temp homes, deeply nested
    network homes) can consume the entire budget before we add a filename. Rather than fail every ssh call with ssh's
    cryptic "too long for Unix domain socket", we fall back to a short per-uid directory under the system temp dir.
    """
    # 17 = separator plus the 16-hex-char hashed filename, the shortest name control_path can produce.
    if len(str(CONTROL_DIR)) + 17 + SOCKET_SUFFIX_BUDGET <= SOCKET_PATH_LIMIT:
        return CONTROL_DIR
    return Path(tempfile.gettempdir()) / f"fwd-cm-{os.getuid()}"


def _ensure_control_dir() -> None:
    """Create the ControlMaster directory before any connection: ssh will not create it for us.

    Kept out of :meth:`SSHEndpoint.ssh_argv` on purpose so argv construction stays a pure function and unit tests can
    exercise it without touching the filesystem.
    """
    control_dir().mkdir(parents=True, exist_ok=True, mode=0o700)


class SSHError(RuntimeError):
    """Raised when an ssh invocation fails in a way the caller is expected to surface to the user."""


@dataclass(slots=True)
class SSHEndpoint:
    """Everything needed to reach one remote machine, and nothing about *why* we are reaching it.

    Backends produce these; ``sync``/``remote``/``claude_state``/``ops`` consume them. The dataclass is serialized
    verbatim into ``~/.fwd/state.json`` (see :func:`fwd.state.endpoint_to_dict`) so attach can reconnect in a later
    process without re-provisioning. Fields must stay JSON-primitive for that reason.

    Attributes:
        host: Hostname or IP to connect to.
        user: Remote login user.
        port: TCP port for sshd (RunPod direct-IP pods expose 22/tcp on a high random port).
        key_path: Optional explicit identity file; ``None`` defers to the user's ssh config/agent.
        proxy_jump: Optional ``-J`` value for an external host used to reach a non-public target.
        supports_rsync: ``False`` when the transport cannot run a remote rsync binary (RunPod proxy). Callers must
            fall back to tar-over-ssh (:func:`fwd.sync.tar_up`) and warn loudly.
        extra_opts: Raw extra ``-o`` style options appended last so user config can override our defaults.
    """

    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    proxy_jump: str | None = None
    supports_rsync: bool = True
    extra_opts: list[str] = field(default_factory=list)

    def ssh_target(self) -> str:
        """Return the ``user@host`` string used by both ssh and rsync."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_argv(self, *, tty: bool = False, control: bool = True) -> list[str]:
        """Build the base ``ssh`` argv (no remote command appended).

        Args:
            tty: Add ``-t`` to force a pty. Required for tmux attach, harmful for piped scripts.
            control: Include the ControlMaster options. Disabled for the very first reachability probe so a stale
                socket cannot mask an unreachable host.

        Returns:
            argv list ending with the ``user@host`` target, ready for a remote command to be appended.
        """
        argv = ["ssh", *BASE_SSH_OPTS]
        if tty:
            argv.append("-t")
        if control:
            argv += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={self.control_path()}",
                "-o",
                f"ControlPersist={CONTROL_PERSIST_DEFAULT}",
            ]
        if self.key_path:
            argv += ["-i", str(Path(self.key_path).expanduser())]
        if self.port and self.port != 22:
            argv += ["-p", str(self.port)]
        if self.proxy_jump:
            argv += ["-J", self.proxy_jump]
        # Appended last per the dataclass contract. Note ssh resolves duplicate -o keys by FIRST occurrence, so these
        # can add options we do not set but cannot override BatchMode/StrictHostKeyChecking/ControlMaster.
        argv += list(self.extra_opts)
        argv.append(self.ssh_target())
        return argv

    def rsync_shell(self) -> str:
        """Return the shell string for rsync's ``-e`` flag, mirroring :meth:`ssh_argv` options."""
        _ensure_control_dir()
        argv = self.ssh_argv()
        # rsync appends the target itself, so hand it everything except our trailing user@host.
        return shlex.join(argv[:-1])

    def control_path(self) -> Path:
        """Return this endpoint's ControlMaster socket path (``~/.fwd/cm/<user>@<host>:<port>``).

        The readable form is used whenever it fits inside the ``sun_path`` budget, because a human debugging a stuck
        master wants to see which host a socket belongs to. When it does not fit — long hostnames (RunPod pod IDs,
        cluster FQDNs) or a deep ``$HOME`` — the name collapses to a hash of the same identity, which is stable across
        processes so ``attach`` still finds the master that ``up`` opened.
        """
        directory = control_dir()
        name = f"{self.ssh_target()}:{self.port}"
        if len(str(directory / name)) + SOCKET_SUFFIX_BUDGET > SOCKET_PATH_LIMIT:
            name = hashlib.blake2b(name.encode("utf-8"), digest_size=8).hexdigest()
        return directory / name

    def run(
        self,
        cmd: str,
        *,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a single shell command remotely.

        Args:
            cmd: Shell snippet executed by the remote login shell.
            check: Raise :class:`SSHError` on nonzero exit.
            capture: Capture stdout/stderr as text; when ``False`` output streams to fwd's own stdio.
            timeout: Seconds before the local ssh process is killed.
            env: Variables exported before ``cmd`` (we prepend assignments rather than rely on ``SendEnv``, which
                most sshd configs reject).
        """
        _ensure_control_dir()
        argv = [*self.ssh_argv(), _env_prelude(env) + cmd]
        try:
            proc = subprocess.run(
                argv,
                check=False,
                text=True,
                capture_output=capture,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"ssh to {self.ssh_target()} timed out after {timeout}s running: {cmd}") from exc
        if check and proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            detail = f": {stderr}" if stderr else ""
            raise SSHError(f"remote command failed (exit {proc.returncode}) on {self.ssh_target()}: {cmd}{detail}")
        return proc

    def popen(
        self,
        cmd: str,
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a remote command without waiting for it.

        Durable task viewers use this lower-level counterpart to :meth:`run` so they can multiplex remote output with
        local control keys. The remote work itself must live outside this SSH process; terminating the returned
        process is therefore a viewer detach, not task cancellation.
        """
        _ensure_control_dir()
        return subprocess.Popen([*self.ssh_argv(), cmd], stdin=stdin, stdout=stdout, stderr=stderr)

    def run_script(
        self,
        script: str | Path,
        *,
        args: Sequence[str] = (),
        env: dict[str, str] | None = None,
        check: bool = True,
        stream: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Pipe a local script to a remote ``bash -s`` and run it.

        This is how ``bootstrap.sh`` executes: the script never has to exist on the remote side, so bootstrapping
        needs no prior file transfer and cannot drift from the installed package version.

        Args:
            script: Script source, or a path to a local file to read.
            args: Positional args passed after ``bash -s --``.
            env: Variables exported before the script (the bootstrap contract vars, e.g. ``FWD_TOOL_PREFIX``).
            check: Raise :class:`SSHError` on nonzero exit.
            stream: Forward remote output live instead of capturing (bootstrap is slow; users want progress).
        """
        source = Path(script).read_text(encoding="utf-8") if isinstance(script, Path) else script
        # The env prelude is prepended to the *script body* rather than to the remote command line: the body can be
        # arbitrarily long, while a command line is limited by ARG_MAX and would leak the values into remote `ps`.
        prelude = "".join(f"export {key}={shlex.quote(str(value))}\n" for key, value in (env or {}).items())
        payload = prelude + source
        _ensure_control_dir()
        remote_cmd = shlex.join(["bash", "-s", "--", *args])
        argv = [*self.ssh_argv(), remote_cmd]
        proc = subprocess.run(
            argv,
            input=payload,
            check=False,
            text=True,
            capture_output=not stream,
        )
        if check and proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            detail = f": {stderr}" if stderr else ""
            name = script.name if isinstance(script, Path) else "script"
            raise SSHError(f"remote {name} failed (exit {proc.returncode}) on {self.ssh_target()}{detail}")
        return proc

    def exec_interactive(self, remote_cmd: str) -> NoReturn:
        """Replace the current process with an interactive ``ssh -t ... <remote_cmd>``.

        Never returns. Used for tmux attach so terminal resize, mouse reporting and signals behave exactly as a
        hand-typed ssh would.
        """
        _ensure_control_dir()
        argv = [*self.ssh_argv(tty=True), remote_cmd]
        os.execvp(argv[0], argv)

    def _control_argv(self, *flags: str) -> list[str]:
        """Return the control-enabled argv with ``flags`` spliced in *before* the target.

        ssh stops parsing options at the hostname, so anything appended after ``user@host`` becomes the remote command
        instead of a flag. Every control-socket operation therefore has to insert rather than append.
        """
        argv = self.ssh_argv(control=True)
        argv[-1:-1] = flags
        return argv

    def open_control_master(self, *, persist: str = CONTROL_PERSIST_DEFAULT) -> None:
        """Open a background ControlMaster socket for this endpoint (idempotent; no-op if already alive)."""
        _ensure_control_dir()
        if self._control_alive():
            return
        # -M -N -f: become the master, run no command, fork into the background once authenticated. The extra
        # ControlPersist goes at the front because ssh resolves duplicate options first-wins.
        argv = self._control_argv("-M", "-N", "-f")
        argv[1:1] = ["-o", f"ControlPersist={persist}"]
        proc = subprocess.run(argv, check=False, text=True, capture_output=True)
        if proc.returncode != 0:
            raise SSHError(f"failed to open ssh control master to {self.ssh_target()}: {(proc.stderr or '').strip()}")

    def _control_alive(self) -> bool:
        """Return whether a usable master socket already exists (``ssh -O check``)."""
        if not self.control_path().exists():
            return False
        return subprocess.run(self._control_argv("-O", "check"), check=False, capture_output=True).returncode == 0

    def close_control_master(self) -> None:
        """Tear down the ControlMaster socket if present (``ssh -O exit``); never raises."""
        if not self.control_path().exists():
            return
        subprocess.run(self._control_argv("-O", "exit"), check=False, capture_output=True)
        # A killed master occasionally leaves the socket inode behind; a stale path makes later -O check lie.
        self.control_path().unlink(missing_ok=True)


def wait_for_ssh(
    endpoint: SSHEndpoint,
    *,
    timeout: float = 300.0,
    interval: float = 3.0,
    on_attempt: Callable[[int], None] | None = None,
) -> bool:
    """Poll until an endpoint accepts ssh connections and can run a trivial command.

    Freshly provisioned pods answer on port 22 before sshd is ready to authenticate, so the probe runs a real
    command (``true``) rather than only opening a socket.

    Args:
        endpoint: Target to probe. Probes deliberately bypass the ControlMaster.
        timeout: Total seconds to keep trying.
        interval: Delay between attempts.
        on_attempt: Called with the 1-based attempt number, for spinner/progress updates.

    Returns:
        ``True`` once reachable, ``False`` if the timeout expires.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        if on_attempt is not None:
            on_attempt(attempt)
        # ConnectTimeout is inserted ahead of everything else because ssh resolves duplicate options first-wins.
        argv = endpoint.ssh_argv(control=False)
        argv[1:1] = ["-o", f"ConnectTimeout={PROBE_CONNECT_TIMEOUT}"]
        argv.append("true")
        try:
            completed = subprocess.run(argv, check=False, capture_output=True, timeout=PROBE_CONNECT_TIMEOUT * 3)
        except subprocess.TimeoutExpired:
            completed = None
        if completed is not None and completed.returncode == 0:
            return True
        if time.monotonic() + interval >= deadline:
            return False
        time.sleep(interval)


def control_path_for(endpoint: SSHEndpoint) -> Path:
    """Module-level helper mirroring :meth:`SSHEndpoint.control_path`, for callers holding only loose parts."""
    return endpoint.control_path()
