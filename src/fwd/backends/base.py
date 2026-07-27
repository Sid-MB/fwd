"""Backend contract — the ``Provisioner`` protocol every target type implements.

Design intent
-------------
A backend answers exactly one question: *how do I get an SSH-reachable machine with a working directory on it?* It
knows nothing about Claude, rsync filters or tmux — those are the same everywhere and live in ``ops/launch.py``. This
boundary is what lets ``ops`` be written once against three very different providers.

The protocol is deliberately split into provision (create/reuse, potentially slow and costly) and ``endpoint``
(re-resolve connection details, cheap). RunPod hands a pod a new IP every time it restarts, so ``attach`` must call
``endpoint(session)`` rather than trusting the address cached in state — hence the two methods instead of one.

``status`` returns a coarse enum instead of a provider string because ``ls``/``attach`` reconcile against it, and each
provider spells its states differently. ``JOB_ENDED`` exists specifically for Slurm: the login node is still perfectly
reachable and the tmux session may even be alive, but the allocation is gone and attach must offer a relaunch. That is
a genuinely different situation from ``STOPPED`` (pod exists, powered off, restartable) or ``GONE`` (deleted upstream,
state is stale).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from fwd.config import Config, TargetConfig
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState


class TargetStatus(StrEnum):
    """Reconciled liveness of a target, normalized across providers.

    Values:
        RUNNING: Reachable and ready to attach.
        STOPPED: Exists but powered off; a launch can restart it (RunPod ``pod start``).
        PENDING: Coming up — provisioning, booting, or queued in Slurm.
        GONE: No longer exists upstream; the local state entry is stale and should be pruned.
        JOB_ENDED: Slurm-specific — login node fine, allocation finished/cancelled; needs relaunch.
        UNKNOWN: The provider could not be reached or gave an unusable answer. Deliberately distinct from ``GONE``:
            the live e2e run (docs/live-e2e-report.md, R2-1) caught a transient ``runpodctl`` failure right after a
            stop being reported as ``GONE``, which invites the user to prune the state entry of a pod that is still
            running and still billing. "Cannot ask" must never be collapsed into "does not exist", so callers treat
            this as retry-able and never destructive.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    PENDING = "pending"
    GONE = "gone"
    JOB_ENDED = "job_ended"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class TargetInfo:
    """What a successful :meth:`Provisioner.provision` hands back to ``ops/launch.py``.

    Attributes:
        endpoint: How to reach the machine.
        remote_dir: Absolute remote path the project syncs into.
        status: Usually ``RUNNING``; ``PENDING`` if the caller must keep polling.
        backend_ids: Provider handles to persist in state (``pod_id``, ``job_id``, pinned ``login_host``).
        tool_prefix: Where bootstrap should install tooling. Backends set this to persistent storage — ``/workspace``
            on RunPod (container disk is wiped on stop), scratch on Slurm (home inode quotas) — so restarts and later
            launches skip re-downloading uv/bun/node/claude.
        scratch: Optional cache/temp root exported to bootstrap and dependency installs.
        notes: Human-readable warnings to surface after provisioning, e.g. "using RunPod proxy, rsync unavailable".
    """

    endpoint: SSHEndpoint
    remote_dir: str
    status: TargetStatus = TargetStatus.RUNNING
    backend_ids: dict[str, str] = field(default_factory=dict)
    tool_prefix: str | None = None
    scratch: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CheckResult:
    """One diagnostic line from ``doctor``.

    Attributes:
        name: Short check label, e.g. ``"runpodctl"``.
        ok: Whether the check passed.
        detail: What was observed (version string, error text).
        hint: Actionable remedy shown only on failure.
    """

    name: str
    ok: bool
    detail: str = ""
    hint: str | None = None


class ProvisionError(RuntimeError):
    """Raised when a target cannot be created, restarted or resolved. Message is shown directly to the user."""


@runtime_checkable
class Provisioner(Protocol):
    """The interface each backend implements.

    Implementations are constructed per invocation with their own target config plus the full config (needed for
    cross-cutting settings like sync excludes). They should be cheap to construct and do all network work inside the
    methods, so ``fwd ls`` can build one per session without slowing down.
    """

    name: ClassVar[str]

    def __init__(self, target: TargetConfig, config: Config) -> None:
        """Args:
        target: This backend's own resolved target configuration.
        config: The full merged config, for settings that are not target-scoped.
        """
        ...

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Create or reuse a target and return how to reach it.

        Must be idempotent per ``session_name``: a second launch with the same name reuses the existing pod/host
        (starting it if stopped) instead of creating a duplicate. Blocks until the machine is at least ssh-reachable
        or returns ``PENDING`` for the caller to poll.

        Args:
            session_name: fwd session name; used to derive provider resource names (``fwd-<name>``) and tmux session.
            project_name: Local project directory basename, used to build ``remote_dir`` under ``remote_base``.
            gpu: Optional GPU override from ``--gpu``; ignored by backends without a GPU concept.

        Raises:
            ProvisionError: On any failure to obtain a usable target.
        """
        ...

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Re-resolve current connection details for an existing session.

        Called by ``attach``/``push``/``pull`` before connecting, because addresses can change between invocations
        (RunPod reassigns IP and port on restart). Cheap and read-only; must not create or start anything.

        Raises:
            ProvisionError: If the target no longer exists or has no reachable address.
        """
        ...

    def status(self, session: SessionState) -> TargetStatus:
        """Query current liveness. Must never raise.

        A *confirmed* missing resource is ``GONE``; anything else that prevents an answer (API error, timeout, CLI
        failure) is ``UNKNOWN``. That distinction is load-bearing — ``GONE`` authorizes callers to offer deleting the
        session entry, so reporting it on a mere provider hiccup can strand a running, billing target.
        """
        ...

    def stop(self, session: SessionState) -> None:
        """Suspend the target while preserving data (RunPod ``pod stop``, Slurm ``scancel``).

        Must be safe to call on an already-stopped or already-gone target.
        """
        ...

    def destroy(self, session: SessionState) -> None:
        """Permanently delete the target and its volumes. Callers confirm with the user first."""
        ...

    def doctor(self) -> list[CheckResult]:
        """Check local prerequisites for this backend (CLI present, credentials set, host reachable).

        Read-only and must not raise; failures are reported as ``CheckResult`` entries with hints.
        """
        ...
