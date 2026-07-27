"""Slurm backend — allocations driven from a cluster login node.

Key constraints captured from the plan (owned by the Slurm teammate, Phase 4):

- Everything network- or install-related (sync, bootstrap, dependency installs) happens on the **login node**: compute
  nodes typically have no internet access, and the filesystem is shared anyway.
- The concrete login hostname is **pinned on first connect** and stored in ``backend_ids``. Clusters round-robin
  ``login.example.edu`` across several machines, and a tmux session only exists on the box that created it.
- tmux runs on the login node executing a generated ``job.sh``: ``env_setup`` lines, then
  ``salloc <alloc> -J fwd-<name> srun --pty bash -lc '<module loads>; cd <dir>; <claude_cmd>'``. Wrapping the
  allocation in tmux is what lets the job survive a dropped ssh connection.
- Caches and venvs are redirected to scratch (``UV_PROJECT_ENVIRONMENT``, ``UV_CACHE_DIR``, ...) because home
  directories on HPC have small inode quotas that a single ``node_modules`` can exhaust.
- The job id is tracked via ``squeue``; a finished allocation yields ``JOB_ENDED``, which attach turns into a relaunch
  offer rather than an error.

What ``ops`` calls (the whole public surface)
---------------------------------------------
``provision`` → ``TargetInfo`` (login node, ``RUNNING``, pinned host in ``backend_ids['login_host']``), then the usual
sync/bootstrap/deps against ``TargetInfo.endpoint`` — all on the login node — and finally **one** Slurm-specific call::

    tmux_cmd = backend.claude_launch_wrapper(endpoint, session_name, remote_dir, claude_cmd)
    remote.tmux_new(endpoint, f"fwd-{session_name}", remote_dir, tmux_cmd)
    job_id = backend.find_job_id(endpoint, session_name)   # after a short grace period; may be None while queueing

``claude_launch_wrapper`` writes ``<remote_dir>/.fwd/job.sh`` and returns the command tmux should run. Record the job id
(when found) as ``backend_ids['job_id']`` so ``status``/``stop`` do not have to re-scan ``squeue``.

Reattach contract: if ``status(session)`` is ``JOB_ENDED``, the login node and possibly the tmux session are still
alive but the allocation is gone. ``attach`` should offer a relaunch — kill the stale tmux session, call
``claude_launch_wrapper`` again and ``tmux_new`` again — rather than attaching to a dead pane. See
``docs/slurm-notes.md``.

Cache/inode guardrails: ``TargetInfo.tool_prefix`` and ``TargetInfo.scratch`` are both under ``remote_base`` (scratch),
never ``$HOME``. ``bootstrap.sh`` turns ``FWD_SCRATCH`` into ``fwd-env.sh`` exports (``UV_CACHE_DIR``,
``UV_PROJECT_ENVIRONMENT``, ``XDG_CACHE_HOME``, ``BUN_INSTALL_CACHE_DIR``, ``npm_config_cache``), and the generated
``job.sh`` sources that file; the exact contract is documented in ``docs/slurm-notes.md``.
"""

from __future__ import annotations

import posixpath
import shlex
import shutil
from typing import Any, ClassVar

from fwd import remote as remote_mod
from fwd.backends.base import Backend, CheckResult, ConfigChoice, ConfigChoices, ConfigParameter, ProvisionError, TargetInfo, TargetStatus
from fwd.backends.slurm_job import job_name, job_script_path, render_job_script, render_tmux_command
from fwd.config import Config, SlurmTargetConfig, ssh_config_host_aliases
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.state import SessionState

# Heredoc delimiter used to plant job.sh. Quoted at the remote shell so the body is written byte-for-byte with no
# expansion; a script containing this exact line would break the write, so we assert it does not.
HEREDOC_MARKER = "FWD_JOB_SCRIPT_EOF"

# Short timeouts everywhere: `fwd ls` builds one backend per session and must stay snappy, and a hung login node
# should surface as "unreachable", not as a 2-minute freeze.
PROBE_TIMEOUT = 20.0
QUERY_TIMEOUT = 30.0

# `squeue -h -o %T` states → fwd's normalized status. Anything squeue still lists but we do not recognise is treated as
# alive (default ``RUNNING``): squeue only forgets a job after MinJobAge, so a listed job is by definition not gone,
# and guessing ``JOB_ENDED`` would pop a spurious relaunch prompt on top of a perfectly good session.
SLURM_STATE_MAP: dict[str, TargetStatus] = {
    "RUNNING": TargetStatus.RUNNING,
    "COMPLETING": TargetStatus.RUNNING,
    "PENDING": TargetStatus.PENDING,
    "CONFIGURING": TargetStatus.PENDING,
    "REQUEUED": TargetStatus.PENDING,
    "RESIZING": TargetStatus.PENDING,
    "SUSPENDED": TargetStatus.PENDING,
    "COMPLETED": TargetStatus.JOB_ENDED,
    "CANCELLED": TargetStatus.JOB_ENDED,
    "FAILED": TargetStatus.JOB_ENDED,
    "TIMEOUT": TargetStatus.JOB_ENDED,
    "PREEMPTED": TargetStatus.JOB_ENDED,
    "NODE_FAIL": TargetStatus.JOB_ENDED,
    "BOOT_FAIL": TargetStatus.JOB_ENDED,
    "DEADLINE": TargetStatus.JOB_ENDED,
    "OUT_OF_MEMORY": TargetStatus.JOB_ENDED,
    "REVOKED": TargetStatus.JOB_ENDED,
    "SPECIAL_EXIT": TargetStatus.JOB_ENDED,
}

# Two-letter squeue compact codes, accepted so a caller (or a cluster with a custom default format) that hands us
# `%t` output instead of `%T` still maps correctly.
SLURM_COMPACT_MAP: dict[str, TargetStatus] = {
    "R": TargetStatus.RUNNING,
    "CG": TargetStatus.RUNNING,
    "PD": TargetStatus.PENDING,
    "CF": TargetStatus.PENDING,
    "RQ": TargetStatus.PENDING,
    "S": TargetStatus.PENDING,
    "CD": TargetStatus.JOB_ENDED,
    "CA": TargetStatus.JOB_ENDED,
    "F": TargetStatus.JOB_ENDED,
    "TO": TargetStatus.JOB_ENDED,
    "PR": TargetStatus.JOB_ENDED,
    "NF": TargetStatus.JOB_ENDED,
    "OOM": TargetStatus.JOB_ENDED,
    "DL": TargetStatus.JOB_ENDED,
}

# df use% above this triggers a doctor warning. Scratch filling up is the single most common cause of a launch that
# bootstraps fine and then fails halfway through `uv sync`.
QUOTA_WARN_PERCENT = 90


def map_slurm_state(state: str) -> TargetStatus:
    """Map one ``squeue`` state token to a :class:`~fwd.backends.base.TargetStatus`.

    Accepts both long (``%T``, ``PENDING``) and compact (``%t``, ``PD``) spellings, plus Slurm's reason suffixes
    (``CANCELLED by 1234`` → ``CANCELLED``). Unknown-but-listed states resolve to ``RUNNING``; see
    :data:`SLURM_STATE_MAP`.
    """
    token = state.strip().split()[0].upper() if state.strip() else ""
    if not token:
        return TargetStatus.JOB_ENDED
    if token in SLURM_STATE_MAP:
        return SLURM_STATE_MAP[token]
    if token in SLURM_COMPACT_MAP:
        return SLURM_COMPACT_MAP[token]
    return TargetStatus.RUNNING


def _job_sort_key(job_id: str) -> tuple[int, int]:
    """Sort key for job ids, newest last. Handles array ids (``1234_7``) by sorting on base then array index."""
    base, _, index = job_id.partition("_")
    try:
        return (int(base), int(index) if index.isdigit() else 0)
    except ValueError:
        return (0, 0)


class SlurmBackend(Backend):
    """Provisioner over a Slurm cluster (see :class:`fwd.backends.base.Provisioner`)."""

    name: ClassVar[str] = "slurm"

    def __init__(self, target: SlurmTargetConfig, config: Config) -> None:
        super().__init__(target, config)
        # Remembered from provision so a later job_script/claude_launch_wrapper call in the same launch keeps the
        # --gpu the user asked for without ops having to thread it through.
        self._gpu: str | None = None

    @classmethod
    def config_parameters(cls) -> tuple[ConfigParameter, ...]:
        """Describe portable Slurm fields; cluster-specific values intentionally remain free text."""
        return (
            ConfigParameter("login_host", "--login-host", "cluster login hostname or SSH alias", required=True),
            ConfigParameter("user", "--user", "remote username", required=True),
            ConfigParameter("port", "--port", "SSH port", prompt=False),
            ConfigParameter("key_path", "--key-path", "explicit SSH identity file", prompt=False),
            ConfigParameter("proxy_jump", "--proxy-jump", "SSH bastion hop", prompt=False),
            ConfigParameter("remote_base", "--remote-base", "scratch parent for project checkouts", required=True),
            ConfigParameter("alloc", "--alloc", "flags passed to salloc"),
            ConfigParameter("tool_prefix", "--tool-prefix", "scratch-backed tooling and cache root"),
            ConfigParameter("partition", "--partition", "Slurm partition"),
            ConfigParameter("account", "--account", "Slurm account"),
            ConfigParameter("env_setup", "--env-setup", "shell lines run before allocation"),
        )

    @classmethod
    def config_choices(cls, parameter: ConfigParameter, values: dict[str, Any]) -> ConfigChoices:
        """Offer SSH aliases as login-node candidates while allowing cluster hostnames not present in SSH config."""
        if parameter.name == "login_host":
            return ConfigChoices(tuple(ConfigChoice(alias) for alias in sorted(ssh_config_host_aliases())), allow_free_text=True)
        return super().config_choices(parameter, values)

    # ------------------------------------------------------------------ helpers

    def _endpoint_for(self, host: str) -> SSHEndpoint:
        """Build an endpoint for a login host using this target's auth settings.

        Single place where auth fields are read, so ``provision``, ``endpoint`` and ``doctor`` cannot drift. ``proxy_jump``
        is preserved for the pinned host too: pinning only changes *which* login node we land on, never how we get to
        the cluster.
        """
        return SSHEndpoint(
            host=host,
            user=self.target.user,
            port=self.target.port,
            key_path=self.target.key_path,
            proxy_jump=self.target.proxy_jump,
            supports_rsync=True,
        )

    def _tool_prefix(self) -> str:
        """Resolve the tool prefix: config value, else ``<remote_base>/.fwd-tools`` on scratch."""
        return self.target.tool_prefix or posixpath.join(self.target.remote_base.rstrip("/"), ".fwd-tools")

    def _scratch(self) -> str:
        """Resolve the cache root exported to bootstrap as ``FWD_SCRATCH`` (``<remote_base>/.fwd-cache``)."""
        return posixpath.join(self.target.remote_base.rstrip("/"), ".fwd-cache")

    def _require_config(self) -> None:
        """Validate the two settings with no sane default. Raises :class:`ProvisionError` with a copy-pasteable fix."""
        if not self.target.login_host:
            raise ProvisionError(
                f"target {self.target.name!r}: 'login_host' is required for the slurm backend "
                f"(e.g. login_host = \"login.hpc.example.edu\")"
            )
        if not self.target.remote_base:
            raise ProvisionError(
                f"target {self.target.name!r}: 'remote_base' is required for the slurm backend — it must point at "
                f"scratch, not $HOME, because HPC home directories have inode quotas a single node_modules can "
                f'exhaust (e.g. remote_base = "/scratch/$USER/fwd")'
            )

    def _pinned_host(self, session: SessionState) -> str:
        """Return the pinned login host recorded at provision time, falling back to the configured alias."""
        return session.backend_ids.get("login_host") or self.target.login_host

    # ------------------------------------------------------------------ protocol

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Connect to the login node, pin its concrete hostname, and prepare scratch dirs.

        The allocation itself is *not* submitted here: it is started by the tmux command in ``ops/launch.py`` so the
        interactive ``srun --pty`` is owned by the tmux session the user attaches to. Bootstrap and dependency installs
        also run against the endpoint returned here — i.e. on the **login node** — because compute nodes usually have
        no outbound internet and the filesystem is shared with them anyway.

        Login-node pinning: the configured ``login_host`` is typically a round-robin alias over several machines. tmux
        sessions are per-machine, so a second ``fwd`` invocation that lands on ``login2`` would not see the session
        created on ``login1``. We therefore run ``hostname -f`` on first connect and store the concrete name in
        ``backend_ids['login_host']``. Some clusters do not publish per-node DNS from outside; if the pinned name is not
        directly reachable we fall back to the alias, record that in ``backend_ids['pin']`` and warn, since attach may
        then land on the wrong node.

        Args:
            gpu: When set, appended to the ``alloc`` template as a ``--gres=gpu:`` request.
        """
        self._require_config()
        self._gpu = gpu
        notes: list[str] = []

        alias_ep = self._endpoint_for(self.target.login_host)
        try:
            proc = alias_ep.run("hostname -f 2>/dev/null || hostname", timeout=PROBE_TIMEOUT)
        except SSHError as exc:
            raise ProvisionError(f"cannot reach login host {self.target.login_host!r}: {exc}") from exc
        pinned = (proc.stdout or "").strip().splitlines()[-1].strip() if (proc.stdout or "").strip() else ""

        if not pinned:
            pinned, pin_mode = self.target.login_host, "alias"
        elif pinned == self.target.login_host:
            pin_mode = "hostname"
        else:
            try:
                self._endpoint_for(pinned).run("true", timeout=PROBE_TIMEOUT)
                pin_mode = "hostname"
            except SSHError:
                notes.append(
                    f"pinned login node {pinned!r} is not directly reachable; using the alias "
                    f"{self.target.login_host!r} instead — a later 'fwd attach' may land on a different login node "
                    f"and not find the tmux session"
                )
                pinned, pin_mode = self.target.login_host, "alias"

        endpoint = self._endpoint_for(pinned)
        remote_dir = posixpath.join(self.target.remote_base.rstrip("/"), project_name)
        tool_prefix = self._tool_prefix()
        scratch = self._scratch()

        mkdir = " ".join(shlex.quote(p) for p in (remote_dir, posixpath.join(remote_dir, ".fwd"), tool_prefix, scratch))
        try:
            endpoint.run(f"mkdir -p {mkdir}", timeout=QUERY_TIMEOUT)
        except SSHError as exc:
            raise ProvisionError(f"cannot create {remote_dir!r} under remote_base {self.target.remote_base!r}: {exc}") from exc

        return TargetInfo(
            endpoint=endpoint,
            remote_dir=remote_dir,
            status=TargetStatus.RUNNING,
            backend_ids={"login_host": pinned, "login_alias": self.target.login_host, "pin": pin_mode},
            tool_prefix=tool_prefix,
            scratch=scratch,
            notes=notes,
        )

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Return an endpoint for the *pinned* login host from ``backend_ids``, not the round-robin alias.

        Auth fields come from current config rather than the serialized endpoint, so editing ``key_path`` or
        ``proxy_jump`` in config takes effect on the next attach without recreating the session.
        """
        host = self._pinned_host(session)
        if not host:
            raise ProvisionError(f"session {session.name!r} has no login host recorded and target {self.target.name!r} sets no login_host")
        return self._endpoint_for(host)

    def status(self, session: SessionState) -> TargetStatus:
        """``squeue -j <job_id>``: running → ``RUNNING``, queued → ``PENDING``, absent → ``JOB_ENDED``.

        Two levels of liveness: the login node first (unreachable → ``GONE``, the state entry is useless), then the
        allocation. A session whose job id was never recorded — launch raced ahead of ``squeue``, or the user returned
        from the default non-attaching ``fwd up`` before the job appeared — reports ``RUNNING`` after one best-effort rescan, because the
        login node *is* up and there is nothing to relaunch yet.
        """
        # An unreachable login node is never ``GONE``: a dropped VPN or a cluster in maintenance is far more likely
        # than a deleted account, and the job itself may still be queued or running. ``GONE`` would invite the user to
        # prune a session whose allocation is alive and consuming budget (same hazard class as R2-1).
        try:
            endpoint = self.endpoint(session)
        except ProvisionError:
            return TargetStatus.UNKNOWN
        try:
            endpoint.run("true", timeout=PROBE_TIMEOUT)
        except SSHError:
            return TargetStatus.UNKNOWN

        job_id = session.backend_ids.get("job_id") or self.find_job_id(endpoint, session.name)
        if not job_id:
            return TargetStatus.RUNNING

        try:
            proc = endpoint.run(f"squeue -j {shlex.quote(job_id)} -h -o %T", check=False, timeout=QUERY_TIMEOUT)
        except SSHError:
            # squeue itself failed to run — we still cannot tell whether the allocation is alive.
            return TargetStatus.UNKNOWN
        output = (proc.stdout or "").strip()
        # A completed job is purged from squeue: nonzero exit ("Invalid job id specified") and empty output both mean
        # the allocation is over, which is JOB_ENDED — the login node is fine, attach should offer a relaunch.
        if proc.returncode != 0 or not output:
            return TargetStatus.JOB_ENDED
        return map_slurm_state(output.splitlines()[0])

    def stop(self, session: SessionState) -> None:
        """``scancel`` the tracked job so the allocation stops billing against the account.

        Cancels by job id when known, otherwise by name (``scancel -u $USER -n fwd-<session>``) — losing the id must
        not leave an allocation burning the user's compute budget. The tmux session is killed too, since without the
        job it would only hold a dead shell. Never raises: ``fwd stop`` on an already-finished job is normal.
        """
        try:
            endpoint = self.endpoint(session)
        except ProvisionError:
            return
        job_id = session.backend_ids.get("job_id")
        cancel = f"scancel {shlex.quote(job_id)}" if job_id else f'scancel -u "$USER" -n {shlex.quote(job_name(session.name))}'
        try:
            endpoint.run(cancel, check=False, timeout=QUERY_TIMEOUT)
        except SSHError:
            pass
        try:
            remote_mod.tmux_kill(endpoint, session.tmux_session or job_name(session.name))
        except (SSHError, NotImplementedError):
            pass

    def destroy(self, session: SessionState) -> None:
        """Cancel the job and remove the scratch project directory.

        The ``rm -rf`` is guarded: the recorded ``remote_dir`` must be an absolute path strictly *inside* the currently
        configured ``remote_base``. A state file edited by hand, a config repointed at another cluster, or a truncated
        path must never turn ``fwd rm`` into ``rm -rf /`` or ``rm -rf $HOME``.

        Raises:
            ProvisionError: If the recorded directory fails the guard; nothing is deleted and the message tells the
                user which path to remove by hand.
        """
        self.stop(session)
        remote_dir = self._guarded_remote_dir(session)
        try:
            endpoint = self.endpoint(session)
        except ProvisionError:
            return
        try:
            endpoint.run(f"rm -rf -- {shlex.quote(remote_dir)}", check=False, timeout=QUERY_TIMEOUT)
        except SSHError:
            pass

    def _guarded_remote_dir(self, session: SessionState) -> str:
        """Validate the recorded remote directory before any destructive command touches it.

        Raises:
            ProvisionError: If the path is empty, relative, equal to ``remote_base``, or outside it.
        """
        base = posixpath.normpath(self.target.remote_base.rstrip("/")) if self.target.remote_base else ""
        path = posixpath.normpath(session.remote_dir.rstrip("/")) if session.remote_dir else ""
        problem: str | None = None
        if not path or not base:
            problem = "no remote_dir/remote_base recorded"
        elif not path.startswith("/") or not base.startswith("/"):
            problem = "paths must be absolute"
        elif path == base or not path.startswith(base + "/"):
            problem = f"{path!r} is not inside remote_base {base!r}"
        if problem:
            raise ProvisionError(
                f"refusing to delete remote directory for session {session.name!r}: {problem}. "
                f"Remove it by hand on {self._pinned_host(session) or self.target.login_host!r} if it still exists."
            )
        return path

    def doctor(self) -> list[CheckResult]:
        """Check login-host reachability, ``squeue``/``salloc`` availability, and that ``remote_base`` is writable.

        Read-only and never raises; each remote probe short-circuits the rest when the login node is unreachable so
        ``fwd doctor`` does not sit through five ssh timeouts.
        """
        results: list[CheckResult] = [
            CheckResult(
                name="ssh",
                ok=shutil.which("ssh") is not None,
                detail=shutil.which("ssh") or "not found on PATH",
                hint="install OpenSSH client" if shutil.which("ssh") is None else None,
            )
        ]

        if not self.target.login_host or not self.target.remote_base:
            results.append(
                CheckResult(
                    name="config",
                    ok=False,
                    detail=f"login_host={self.target.login_host!r} remote_base={self.target.remote_base!r}",
                    hint="both login_host and remote_base are required; remote_base should be scratch, not $HOME",
                )
            )
            return results
        results.append(CheckResult(name="config", ok=True, detail=f"{self.target.login_host} -> {self.target.remote_base}"))

        endpoint = self._endpoint_for(self.target.login_host)
        try:
            proc = endpoint.run("hostname -f 2>/dev/null || hostname", timeout=PROBE_TIMEOUT)
            host = (proc.stdout or "").strip()
        except SSHError as exc:
            results.append(
                CheckResult(
                    name="login-host",
                    ok=False,
                    detail=str(exc),
                    hint=f"check 'ssh {self.target.login_host}' works, including any ProxyJump and key",
                )
            )
            return results
        results.append(CheckResult(name="login-host", ok=True, detail=f"reachable, pins to {host}"))

        results.append(self._remote_check(endpoint, "squeue", "squeue --version", hint="Slurm client tools missing on the login node"))
        results.append(self._remote_check(endpoint, "tmux", "tmux -V", hint="tmux is installed by bootstrap.sh; a system tmux is preferred"))
        results.append(
            self._remote_check(
                endpoint,
                "remote_base",
                f"mkdir -p {shlex.quote(self.target.remote_base)} && test -w {shlex.quote(self.target.remote_base)} && echo writable",
                hint=f"{self.target.remote_base} must exist and be writable",
            )
        )
        results.append(self._quota_check(endpoint))
        return results

    def _remote_check(self, endpoint: SSHEndpoint, label: str, cmd: str, *, hint: str) -> CheckResult:
        """Run one remote probe and turn its exit status into a :class:`CheckResult`; never raises."""
        try:
            proc = endpoint.run(cmd, check=False, timeout=QUERY_TIMEOUT)
        except SSHError as exc:
            return CheckResult(name=label, ok=False, detail=str(exc), hint=hint)
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        return CheckResult(name=label, ok=proc.returncode == 0, detail=detail[0] if detail else "", hint=None if proc.returncode == 0 else hint)

    def _quota_check(self, endpoint: SSHEndpoint) -> CheckResult:
        """Warn when the scratch filesystem is nearly full (``df -P``), the usual cause of a half-finished bootstrap."""
        try:
            proc = endpoint.run(f"df -P {shlex.quote(self.target.remote_base)}", check=False, timeout=QUERY_TIMEOUT)
        except SSHError as exc:
            return CheckResult(name="scratch-space", ok=True, detail=f"unknown ({exc})")
        lines = (proc.stdout or "").strip().splitlines()
        if proc.returncode != 0 or len(lines) < 2:
            return CheckResult(name="scratch-space", ok=True, detail="unknown")
        used = next((f for f in lines[-1].split() if f.endswith("%")), "")
        try:
            percent = int(used.rstrip("%"))
        except ValueError:
            return CheckResult(name="scratch-space", ok=True, detail=lines[-1].strip())
        return CheckResult(
            name="scratch-space",
            ok=percent < QUOTA_WARN_PERCENT,
            detail=f"{self.target.remote_base} is {percent}% full",
            hint="free space on scratch; bootstrap and dependency installs need several GB" if percent >= QUOTA_WARN_PERCENT else None,
        )

    # ------------------------------------------------------------------ job.sh + job tracking (called by ops)

    def job_script(
        self,
        session_name: str,
        remote_dir: str,
        claude_cmd: str,
        *,
        tool_prefix: str | None = None,
        gpu: str | None = None,
    ) -> str:
        """Render ``job.sh`` for this session without touching the network.

        Args:
            claude_cmd: Exactly what ops would have passed to ``tmux_new`` on any other backend, e.g.
                ``claude --resume abc`` — here it runs inside the allocation instead of on the login node.
            tool_prefix: Defaults to the same value ``provision`` reported in ``TargetInfo.tool_prefix``.
            gpu: Defaults to the ``--gpu`` value seen by ``provision`` on this instance.
        """
        return render_job_script(
            self.target,
            session_name,
            remote_dir,
            tool_prefix or self._tool_prefix(),
            claude_cmd,
            gpu=gpu if gpu is not None else self._gpu,
        )

    def write_job_script(
        self,
        endpoint: SSHEndpoint,
        session_name: str,
        remote_dir: str,
        claude_cmd: str,
        *,
        tool_prefix: str | None = None,
        gpu: str | None = None,
    ) -> str:
        """Write ``<remote_dir>/.fwd/job.sh`` on the login node and return its absolute path.

        The script is planted with a *quoted* heredoc (``<<'EOF'``) so the remote shell performs no expansion — the
        file lands byte-identical to :meth:`job_script`, which is what makes the quoting testable offline.

        Raises:
            ProvisionError: If the write fails, or in the pathological case where the rendered script contains the
                heredoc marker.
        """
        script = self.job_script(session_name, remote_dir, claude_cmd, tool_prefix=tool_prefix, gpu=gpu)
        if HEREDOC_MARKER in script:
            raise ProvisionError(f"refusing to write job.sh: command contains the heredoc marker {HEREDOC_MARKER}")
        path = job_script_path(remote_dir)
        cmd = (
            f"mkdir -p {shlex.quote(posixpath.dirname(path))} && "
            f"cat > {shlex.quote(path)} <<'{HEREDOC_MARKER}'\n{script}{HEREDOC_MARKER}\n"
            f"chmod +x {shlex.quote(path)}"
        )
        try:
            endpoint.run(cmd, timeout=QUERY_TIMEOUT)
        except SSHError as exc:
            raise ProvisionError(f"failed to write {path}: {exc}") from exc
        return path

    def claude_launch_wrapper(
        self,
        endpoint: SSHEndpoint,
        session_name: str,
        remote_dir: str,
        claude_cmd: str,
        *,
        tool_prefix: str | None = None,
        gpu: str | None = None,
    ) -> str:
        """Write ``job.sh`` and return the command ``ops`` must hand to :func:`fwd.remote.tmux_new`.

        **This is the one Slurm-specific call in the launch path.** On other backends ops runs ``claude_cmd`` in tmux
        directly; here it runs ``bash <remote_dir>/.fwd/job.sh``, which allocates a compute node and starts
        ``claude_cmd`` inside it via ``salloc ... srun --pty``. Same call is used for a relaunch after ``JOB_ENDED``.
        """
        self.write_job_script(endpoint, session_name, remote_dir, claude_cmd, tool_prefix=tool_prefix, gpu=gpu)
        return render_tmux_command(remote_dir)

    def find_job_id(self, endpoint: SSHEndpoint, session_name: str) -> str | None:
        """Return the newest Slurm job id named ``fwd-<session_name>``, or ``None`` if the job is not queued yet.

        Matching is by *job name*, not by parsing salloc's stdout: salloc's "Granted job allocation N" line is buried
        inside a tmux pane we do not read. Newest wins because a relaunch after ``JOB_ENDED`` leaves the previous id in
        squeue for a few minutes (MinJobAge).
        """
        want = job_name(session_name)
        try:
            proc = endpoint.run('squeue -u "$USER" -h -o "%i %j"', check=False, timeout=QUERY_TIMEOUT)
        except SSHError:
            return None
        if proc.returncode != 0:
            return None
        ids = [
            parts[0]
            for line in (proc.stdout or "").splitlines()
            if len(parts := line.split(None, 1)) == 2 and parts[1].strip() == want
        ]
        return max(ids, key=_job_sort_key) if ids else None
