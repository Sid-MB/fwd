"""Backend contract shared by provisioning, setup discovery, and diagnostics.

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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

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
        ephemeral_home: Whether ``$HOME`` is erased across target stops even when ``tool_prefix`` persists. Agent
            integrations use this signal to relocate their mutable home directories before installing or launching.
    """

    endpoint: SSHEndpoint
    remote_dir: str
    status: TargetStatus = TargetStatus.RUNNING
    backend_ids: dict[str, str] = field(default_factory=dict)
    tool_prefix: str | None = None
    scratch: str | None = None
    notes: list[str] = field(default_factory=list)
    ephemeral_home: bool = False


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


@dataclass(frozen=True, slots=True)
class ConfigChoice:
    """One provider-suggested value for a setup parameter."""

    value: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigChoices:
    """Choices discovered for a setup field and whether values outside that list remain valid."""

    values: tuple[ConfigChoice, ...] = ()
    allow_free_text: bool = True


@dataclass(frozen=True, slots=True)
class ConfigParameter:
    """Standard setup metadata owned by a backend.

    ``choices`` contains cheap static suggestions. Backends override :meth:`Backend.config_choices` for provider or
    machine-derived values such as SSH aliases, RunPod GPU identifiers, or cloud machine types. ``prompt_when`` keeps
    conditional setup knowledge in the backend rather than teaching the generic wizard about provider fields; flags
    and schema discovery remain available even when a field is irrelevant to the current interactive choices.
    """

    name: str
    flag: str
    help: str
    required: bool = False
    prompt: bool = True
    advanced: bool = False
    choices: tuple[ConfigChoice, ...] = ()
    allow_free_text: bool = True
    prompt_when: tuple[tuple[str, str], ...] = ()


class Backend(ABC):
    """Abstract base class every backend must implement.

    Runtime methods provide a normalized lifecycle over SSH-reachable compute. Class-level setup methods describe the
    backend before a target exists, allowing both humans and agents to discover the same configuration contract.
    """

    name: ClassVar[str]

    def __init__(self, target: TargetConfig, config: Config) -> None:
        """Args:
        target: This backend's own resolved target configuration.
        config: The full merged config, for settings that are not target-scoped.
        """
        self.target = target
        self.config = config

    @classmethod
    @abstractmethod
    def config_parameters(cls) -> tuple[ConfigParameter, ...]:
        """Return ordered setup fields, including flags, help, requiredness, and static choice policy."""

    @classmethod
    def config_choices(cls, parameter: ConfigParameter, values: dict[str, Any]) -> ConfigChoices:
        """Return current choices for ``parameter``.

        Called while setup is in progress, so ``values`` contains fields already answered. Provider discovery must be
        best-effort and return the static choices on failure rather than making setup depend on network availability.
        """
        return ConfigChoices(parameter.choices, parameter.allow_free_text)

    @classmethod
    def advanced_config(cls, values: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Return resolved defaults and summary lines shown before optional advanced prompts.

        The default has no provider inspection. SSH overrides this with ``ssh -G``; future cloud backends can use the
        same hook for account, region, network, or machine defaults without teaching the wizard provider details.
        """
        return {}, ()

    def cleanup_interrupted_provision(self, session_name: str) -> bool:
        """Remove a resource this backend created during the current, interrupted :meth:`provision` call.

        Returns ``True`` only when an invocation-owned resource was removed. The conservative default does nothing:
        static SSH machines and pre-existing provider resources must never be destroyed merely because their launch
        was interrupted. Provisioning backends should record ownership as soon as creation succeeds, before readiness
        polling, so Ctrl-C during a long boot wait cannot orphan billable compute.
        """
        del session_name
        return False

    @abstractmethod
    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Create or reuse a target and return how to reach it.

        Must be idempotent per ``session_name``: a second launch with the same name reuses the existing pod/host
        (starting it if stopped) instead of creating a duplicate. Blocks until the machine is at least ssh-reachable
        or returns ``PENDING`` for the caller to poll.

        The returned ``remote_dir`` and ``tool_prefix`` must use storage that survives this backend's normal
        :meth:`stop` operation by default. A backend whose provider cannot offer durable storage must require an
        explicit disposable-storage opt-out and surface that limitation loudly; it must never silently place work on
        a resettable root or container disk. Shared lifecycle code independently refuses stop/remove while the remote
        Git worktree is dirty, but that is a last line of defense rather than a substitute for persistent storage.

        Args:
            session_name: fwd session name; used to derive provider resource names (``fwd-<name>``) and tmux session.
            project_name: Local project directory basename, used to build ``remote_dir`` under ``remote_base``.
            gpu: Optional GPU override from ``--gpu``; ignored by backends without a GPU concept.

        Raises:
            ProvisionError: On any failure to obtain a usable target.
        """

    @abstractmethod
    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Re-resolve current connection details for an existing session.

        Called by ``attach``/``push``/``pull`` before connecting, because addresses can change between invocations
        (RunPod reassigns IP and port on restart). Cheap and read-only; must not create or start anything.

        Raises:
            ProvisionError: If the target no longer exists or has no reachable address.
        """

    @abstractmethod
    def status(self, session: SessionState) -> TargetStatus:
        """Query current liveness. Must never raise.

        A *confirmed* missing resource is ``GONE``; anything else that prevents an answer (API error, timeout, CLI
        failure) is ``UNKNOWN``. That distinction is load-bearing — ``GONE`` authorizes callers to offer deleting the
        session entry, so reporting it on a mere provider hiccup can strand a running, billing target.
        """

    def list_status(self, session: SessionState) -> TargetStatus:
        """Return status for the latency-sensitive session table.

        The default preserves the authoritative :meth:`status` behavior. Backends whose ordinary probe deliberately
        waits through provisioning delays should override this with a shorter read-only check: listing many unrelated
        sessions must not multiply one unreachable target's timeout across the whole table.
        """
        return self.status(session)

    @abstractmethod
    def stop(self, session: SessionState) -> None:
        """Suspend the target while preserving data (RunPod ``pod stop``, Slurm ``scancel``).

        Must be safe to call on an already-stopped or already-gone target.
        """

    def remote_stop_command(self, session: SessionState) -> str | None:
        """Return a shell command that reproduces the provider half of :meth:`stop` from the remote endpoint.

        Stop-after executes after the local computer may be gone, so it cannot call a local SDK or CLI. Backends that can safely self-stop return a non-interactive command; unsupported third-party backends inherit ``None`` and the CLI rejects ``--stop-after`` before starting work.
        """
        del session
        return None

    @abstractmethod
    def destroy(self, session: SessionState) -> None:
        """Permanently delete the target and its volumes. Callers confirm with the user first."""

    @abstractmethod
    def doctor(self) -> list[CheckResult]:
        """Check local prerequisites for this backend (CLI present, credentials set, host reachable).

        Read-only and must not raise; failures are reported as ``CheckResult`` entries with hints.
        """
        raise NotImplementedError


# Compatibility alias for callers and third-party code written against fwd's original protocol name.
Provisioner = Backend
