"""RunPod backend — pods created and reused via ``runpodctl``.

Design intent
-------------
Everything here is driven by what the real CLI does (see ``docs/runpod-notes.md`` for the captured spike). Three
observations shape the whole module:

1. **runpodctl >= 2.6 speaks JSON by default.** ``-o json`` is the default output format for every ``pod``/``ssh``
   subcommand, so there is no table scraping anywhere in fwd. Parsing is ``json.loads`` plus a handful of pure
   accessor functions that are unit-tested against captured fixtures in ``tests/fixtures/runpod/``.
2. **``pod get`` already embeds the direct SSH endpoint.** The response carries an ``ssh`` object with ``ip`` and
   ``port`` (the host-side port that 22/tcp is published on), or ``{"error": "pod not ready"}`` while the pod boots.
   That single call is therefore both the readiness poll and the endpoint resolution — we never need ``ssh info``,
   and we never need the REST API. (The REST endpoint ``/v1/pods/<id>`` exposes the same data under ``publicIp`` +
   ``portMappings``; it was evaluated during the spike and rejected as redundant. Keeping to runpodctl also means
   fwd never has to read, hold or risk logging the API key.)
3. **The container disk is wiped on every stop, and the published port churns on restart.** Both were confirmed
   empirically: files written outside the volume vanish across ``pod stop``/``pod start``, files under
   ``/workspace`` survive, and the host port for 22/tcp is reassigned. Hence ``remote_dir`` and ``tool_prefix`` both
   live under ``volume_mount_path``, and ``endpoint()`` always re-resolves instead of trusting stored state.

Syntax drift is handled by detecting the noun-first grammar once per process (``pod create --help``). The legacy
verb-first grammar (``runpodctl create pod``) is deprecated upstream, emits tables rather than JSON, and exposes no
ssh block, so implementing it would mean a second parser for strictly less information. We detect it and fail with
an actionable upgrade message instead.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

from fwd import ui
from fwd.backends.base import Backend, CheckResult, ConfigChoice, ConfigChoices, ConfigParameter, ProvisionError, TargetInfo, TargetStatus
from fwd.config import DEFAULT_RUNPOD_CPU_IMAGE, DEFAULT_RUNPOD_GPU_IMAGE, Config, RunpodTargetConfig
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState

RUNPODCTL = "runpodctl"

# Where runpodctl itself stores credentials. We only ever check for *presence* of a key here; the value is never read
# into fwd's memory, so it cannot leak into logs or tracebacks.
RUNPOD_CONFIG_PATH = Path.home() / ".runpod" / "config.toml"

# runpodctl's ssh-proxy hostname. Reachable but rsync-less, hence supports_rsync=False on any endpoint using it.
PROXY_HOST = "ssh.runpod.io"

# First release with the noun-first grammar and JSON-by-default output that this backend parses.
MIN_RUNPODCTL_VERSION = "2.6.0"

# Where work goes on a pod that turned out to have no persistent volume (CPU pods always, per the spike). Under the
# root user's home, which every RunPod image has, on the container disk that gets wiped on stop.
CONTAINER_DISK_BASE = "/root/fwd"

# Pods answer `pod get` long before sshd is up; these bound the "poll to RUNNING" loop in provision().
PROVISION_TIMEOUT = 420.0
POLL_INTERVAL = 4.0

# Seconds to wait for a TCP connect when confirming a reported address is actually live. Short on purpose: this runs
# inside the polling loop, so a slow failure would stretch every iteration.
PORT_PROBE_TIMEOUT = 4.0

# Per-process memo for the grammar probe, so `fwd ls` over N sessions still only shells out once.
_SYNTAX_OK: bool | None = None


class RunpodError(ProvisionError):
    """A runpodctl invocation failed or returned an error document. Message is user-facing."""


def _first_json(text: str) -> Any:
    """Return the first JSON value found in ``text``, or ``None``.

    runpodctl mixes a JSON document with cobra's usage text on failure paths (``pod get <missing-id>`` prints an
    error object, then the command's help, then the error again). Scanning for the first parseable prefix is more
    robust than assuming the whole stream is one document.
    """
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "[{":
            try:
                value, _ = decoder.raw_decode(text[index:])
            except (json.JSONDecodeError, ValueError):
                continue
            return value
    return None


def error_message(payload: Any) -> str | None:
    """Extract runpodctl's error string from a parsed document, or ``None`` if it is not an error.

    Errors always arrive as ``{"error": "..."}`` at the top level; a successful pod document never carries that key
    at the top level (the nested ``ssh.error`` for a not-yet-ready pod is deliberately not treated as fatal).
    """
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


def parse_pod(stdout: str) -> dict[str, Any]:
    """Parse a single-pod document (``pod get``/``create``/``start``/``stop`` all return the same shape).

    Raises:
        RunpodError: If the output is not a pod object or is an error document.
    """
    payload = _first_json(stdout)
    message = error_message(payload)
    if message:
        raise RunpodError(f"runpodctl: {message}")
    if not isinstance(payload, dict) or "id" not in payload:
        raise RunpodError(f"unexpected runpodctl output: {stdout.strip()[:400]}")
    return payload


def parse_pod_list(stdout: str) -> list[dict[str, Any]]:
    """Parse ``pod list`` into a list of pod summaries; an empty account yields ``[]``.

    Raises:
        RunpodError: On an error document. A non-list payload is treated as empty rather than fatal, because
            ``list`` is used on read-only paths (``status``, reuse lookup) that must degrade gracefully.
    """
    payload = _first_json(stdout)
    message = error_message(payload)
    if message:
        raise RunpodError(f"runpodctl: {message}")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def find_pod_by_name(pods: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Return the pod with exactly this name, preferring a RUNNING one when duplicates exist.

    RunPod does not enforce unique pod names, and a crashed launch can leave two ``fwd-<session>`` pods behind.
    Preferring the running one means the user reattaches to the live machine instead of resurrecting a stale shell.
    """
    matches = [pod for pod in pods if pod.get("name") == name]
    if not matches:
        return None
    for pod in matches:
        if str(pod.get("desiredStatus", "")).upper() == "RUNNING":
            return pod
    return matches[0]


def parse_ssh_info(payload: dict[str, Any]) -> tuple[str, int] | None:
    """Extract the direct ``(ip, port)`` for 22/tcp from a pod document or an ``ssh info`` document.

    Returns ``None`` while the pod is still booting, when it is stopped, or when RunPod published no direct address.
    Accepts both shapes because ``pod get`` nests the block under ``ssh`` while ``ssh info`` returns it bare.
    """
    block = payload.get("ssh") if isinstance(payload.get("ssh"), dict) else payload
    ip = block.get("ip")
    port = block.get("port")
    if not ip or not port:
        return None
    try:
        return str(ip), int(port)
    except (TypeError, ValueError):
        return None


def parse_proxy_target(payload: dict[str, Any]) -> str | None:
    """Extract a ``<pod>-<token>@ssh.runpod.io`` login from an ``ssh_command`` string, if RunPod offered one.

    The proxy login token is opaque and not derivable from the pod id, so it can only be lifted out of the command
    string RunPod hands back. Direct-IP pods have a plain ``root@<ip>`` command and yield ``None`` here.
    """
    block = payload.get("ssh") if isinstance(payload.get("ssh"), dict) else payload
    command = block.get("ssh_command")
    if not isinstance(command, str):
        return None
    for token in command.split():
        if token.endswith(f"@{PROXY_HOST}"):
            return token
    return None


def pod_status(payload: dict[str, Any]) -> TargetStatus:
    """Map a pod document to a :class:`~fwd.backends.base.TargetStatus`.

    ``desiredStatus`` is RunPod's *intent*, not its liveness: a pod reads ``RUNNING`` from the instant it is provisioned,
    minutes before sshd answers. We therefore downgrade RUNNING to ``PENDING`` until the ``ssh`` block carries a
    real address, which is the only signal RunPod gives that the container is actually up.
    """
    desired = str(payload.get("desiredStatus", "")).upper()
    if desired == "RUNNING":
        return TargetStatus.RUNNING if parse_ssh_info(payload) else TargetStatus.PENDING
    if desired in {"EXITED", "STOPPED", "PAUSED"}:
        return TargetStatus.STOPPED
    if desired in {"TERMINATED", "DEAD"}:
        return TargetStatus.GONE
    return TargetStatus.PENDING


def is_missing_pod_error(message: str) -> bool:
    """Return whether a runpodctl error string means "this pod no longer exists" (404) rather than a real failure."""
    lowered = message.lower()
    return "not found" in lowered or "404" in lowered


def create_pod_args(cfg: RunpodTargetConfig, pod_name: str, gpu: str | None = None) -> list[str]:
    """Build the full ``runpodctl pod create`` argv for a target. Pure, so the flag matrix is unit-testable.

    Two rules encode what the CLI actually accepts (per the captured ``pod create --help`` fixture):

    - ``--compute-type``/``--cloud-type`` are sent upper-cased, matching the documented ``GPU|CPU`` and
      ``SECURE|COMMUNITY`` spellings.
    - **CPU pods carry no GPU flags at all.** ``--gpu-id`` is meaningless there and passing it is how you get an
      opaque scheduling failure, so the ``--gpu-id`` flag (and any ``--gpu`` override) is omitted entirely.
    """
    args = [
        "pod",
        "create",
        "--name",
        pod_name,
        "--image",
        cfg.image,
        "--ports",
        "22/tcp",
        "--compute-type",
        cfg.compute_type.upper(),
        "--cloud-type",
        cfg.cloud_type.upper(),
        "--volume-mount-path",
        cfg.volume_mount_path,
        "--volume-in-gb",
        str(cfg.volume_gb),
    ]
    if cfg.compute_type != "cpu":
        gpu_id = gpu or cfg.gpu
        if gpu_id:
            args += ["--gpu-id", gpu_id]
    return args


def create_summary(cfg: RunpodTargetConfig, gpu: str | None = None) -> str:
    """Describe the pod about to be created, mentioning only values actually sent to ``runpodctl``.

    Derived from :func:`create_pod_args` rather than from the config so the progress line cannot drift from the real
    request. The live e2e run (docs/live-e2e-report.md, R2-4) saw a CPU pod announce itself as
    ``(NVIDIA GeForce RTX 4090, 20 GB volume)`` — a GPU that was never requested and a volume RunPod ignores — which
    is exactly the sort of label that sends someone debugging in the wrong direction.
    """
    args = create_pod_args(cfg, "-", gpu)
    parts: list[str] = []
    if "--gpu-id" in args:
        parts.append(args[args.index("--gpu-id") + 1])
    else:
        parts.append("CPU")
    parts.append(f"{args[args.index('--cloud-type') + 1].lower()} cloud")
    # volume_gb is silently dropped for CPU pods, so promising a volume there would be the same lie in a new place.
    parts.append(f"{cfg.volume_gb} GB volume" if cfg.compute_type != "cpu" else "container disk only")
    return ", ".join(parts)


def resolve_paths(cfg: RunpodTargetConfig, project_name: str, *, has_volume: bool) -> tuple[str, str, str, list[str]]:
    """Return ``(remote_dir, tool_prefix, scratch, notes)`` for a pod, relocating off the volume when there isn't one.

    The RunPod spike showed CPU pods silently ignore ``--volume-in-gb``: ``volumeInGb`` comes back ``0`` and nothing
    persistent is mounted at ``volume_mount_path``. The path itself is typically still there as an ordinary directory
    on the container-disk overlay — writable, and therefore dangerous, because everything written to it disappears on
    the next stop. So when the created pod reports no volume we relocate to an explicit container-disk root and say so
    loudly: the pod stays perfectly usable, it just cannot survive a stop.

    Paths that the user has already pointed *outside* ``volume_mount_path`` are left alone; they were never relying
    on the volume, so silently moving them would be the surprising behaviour.
    """
    mount = cfg.volume_mount_path.rstrip("/") or "/"
    base = cfg.remote_base.rstrip("/") or "/"
    tool_prefix = cfg.tool_prefix
    notes: list[str] = []

    if not has_volume and (base == mount or base.startswith(f"{mount}/")):
        fallback = f"{CONTAINER_DISK_BASE}/{Path(mount).name or 'fwd'}"
        # Wording matters here: on a CPU pod ``mount`` usually *does* exist as an ordinary writable directory on the
        # container-disk overlay. What is missing is the persistent volume behind it, and a user who checks and finds
        # the directory sitting there would reasonably conclude fwd is confused (docs/live-e2e-report.md, R2-3).
        notes.append(
            f"pod has no persistent volume — {mount} is not backed by one on this pod, so anything written there "
            f"would be WIPED on stop; using the container disk at {fallback} instead "
            f"(CPU pods silently ignore volume_gb; use a GPU pod to persist)"
        )
        if tool_prefix == mount or tool_prefix.startswith(f"{mount}/"):
            tool_prefix = f"{fallback}{tool_prefix[len(mount):]}"
        base = fallback

    return f"{base}/{project_name}", tool_prefix, f"{base}/.fwd-cache", notes


def port_is_open(host: str, port: int, *, timeout: float = PORT_PROBE_TIMEOUT) -> bool:
    """Return whether a TCP connect to ``host:port`` succeeds.

    This exists because of a race observed live: for a few seconds after ``pod start``, ``pod get`` keeps serving the
    *pre-stop* ``ssh`` block. Trusting it means handing back an address that may never come up (the published port is
    usually reassigned on restart), and the caller then burns its whole ``wait_for_ssh`` timeout on a dead endpoint.
    A bare TCP connect is the cheapest way to tell a stale address from a live one — no auth, no ssh binary, and it
    also covers the ordinary "sshd has not finished starting" case.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pod_name_for(session_name: str) -> str:
    """Return the RunPod pod name owned by an fwd session. The ``fwd-`` prefix is what makes reuse-by-name safe."""
    return f"fwd-{session_name}"


class RunpodBackend(Backend):
    """Provisioner over RunPod pods (see :class:`fwd.backends.base.Provisioner`)."""

    name: ClassVar[str] = "runpod"

    def __init__(self, target: RunpodTargetConfig, config: Config) -> None:
        super().__init__(target, config)
        self._created_pod_id: str | None = None

    @classmethod
    def config_parameters(cls) -> tuple[ConfigParameter, ...]:
        """Describe RunPod setup, with provider enums closed and resource identifiers extensible."""
        return (
            ConfigParameter("compute_type", "--compute-type", "compute kind; CPU-only is the default", choices=(ConfigChoice("cpu", "CPU only"), ConfigChoice("gpu", "GPU")), allow_free_text=False),
            ConfigParameter("gpu", "--gpu", "GPU identifier; used only for GPU compute", prompt_when=(("compute_type", "gpu"),)),
            ConfigParameter("image", "--image", "container image", choices=(ConfigChoice(DEFAULT_RUNPOD_CPU_IMAGE, "CPU base"), ConfigChoice(DEFAULT_RUNPOD_GPU_IMAGE, "GPU/PyTorch")), allow_free_text=True),
            ConfigParameter("cloud_type", "--cloud-type", "RunPod cloud pool", advanced=True, choices=(ConfigChoice("secure"), ConfigChoice("community")), allow_free_text=False),
            ConfigParameter("volume_gb", "--volume-gb", "persistent volume size in GB; GPU pods only", advanced=True, prompt_when=(("compute_type", "gpu"),)),
            ConfigParameter("volume_mount_path", "--volume-mount-path", "persistent volume mount path", prompt=False),
            ConfigParameter("remote_base", "--remote-base", "parent directory for project checkouts", advanced=True),
            ConfigParameter("tool_prefix", "--tool-prefix", "path for installed tooling and caches", advanced=True),
            ConfigParameter("user", "--user", "remote username", advanced=True),
            ConfigParameter("port", "--port", "SSH port", prompt=False),
            ConfigParameter("key_path", "--key-path", "explicit SSH identity file", prompt=False),
            ConfigParameter("allow_proxy", "--allow-proxy", "allow ssh.runpod.io fallback when direct SSH is unavailable", prompt=False, choices=(ConfigChoice("true"), ConfigChoice("false")), allow_free_text=False),
        )

    @classmethod
    def config_choices(cls, parameter: ConfigParameter, values: dict[str, Any]) -> ConfigChoices:
        """Discover GPU identifiers from runpodctl; failures retain the configured/default value and free text."""
        if parameter.name != "gpu" or str(values.get("compute_type", "cpu")).lower() != "gpu":
            return super().config_choices(parameter, values)
        choices: list[ConfigChoice] = [ConfigChoice("NVIDIA GeForce RTX 4090")]
        try:
            process = subprocess.run([RUNPODCTL, "gpu", "list"], capture_output=True, text=True, timeout=20)
            payload = _first_json(process.stdout) if process.returncode == 0 else None
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, str):
                        value, label = item, None
                    elif isinstance(item, dict):
                        value = str(item.get("id") or item.get("gpuTypeId") or item.get("displayName") or item.get("name") or "")
                        label = str(item.get("displayName") or item.get("name") or "") or None
                    else:
                        continue
                    if value and value not in {choice.value for choice in choices}:
                        choices.append(ConfigChoice(value, label))
        except (OSError, RunpodError, subprocess.SubprocessError, ValueError):
            pass
        return ConfigChoices(tuple(choices), allow_free_text=True)

    def _run_ctl(self, args: list[str], *, check: bool = True, timeout: float = 120.0) -> str:
        """Run ``runpodctl <args>`` and return stdout.

        The single chokepoint for every provider call. It normalizes three failure modes into one exception type:
        the binary being absent, the process failing, and the process succeeding while printing an error document
        (runpodctl does the latter more often than you would like).

        Args:
            check: Raise on a nonzero exit. ``False`` is used by best-effort paths (``stop``/``destroy``) that must
                tolerate an already-gone pod.

        Raises:
            RunpodError: On any of the above.
        """
        argv = [RUNPODCTL, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RunpodError(f"{RUNPODCTL} not found on PATH; install it from https://github.com/runpod/runpodctl") from exc
        except subprocess.TimeoutExpired as exc:
            raise RunpodError(f"{RUNPODCTL} {' '.join(args)} timed out after {timeout:.0f}s") from exc
        if proc.returncode != 0 and check:
            payload = _first_json(proc.stdout) or _first_json(proc.stderr)
            detail = error_message(payload) or (proc.stderr.strip() or proc.stdout.strip())
            raise RunpodError(f"{RUNPODCTL} {' '.join(args)} failed: {detail[:400]}")
        return proc.stdout

    def _require_supported_cli(self) -> None:
        """Verify the noun-first JSON grammar once per process, failing with an upgrade hint otherwise.

        Probing ``pod create --help`` rather than parsing ``--version`` means we test the capability we actually
        depend on, which survives RunPod renumbering releases.
        """
        global _SYNTAX_OK
        if _SYNTAX_OK:
            return
        if shutil.which(RUNPODCTL) is None:
            raise RunpodError(f"{RUNPODCTL} not found on PATH; install it from https://github.com/runpod/runpodctl")
        probe = subprocess.run([RUNPODCTL, "pod", "create", "--help"], capture_output=True, text=True)
        if probe.returncode != 0 or "--ports" not in probe.stdout:
            version = subprocess.run([RUNPODCTL, "--version"], capture_output=True, text=True).stdout.strip()
            raise RunpodError(
                f"this runpodctl ({version or 'unknown version'}) does not support the 'runpodctl pod create' syntax fwd "
                f"requires; upgrade to >= {MIN_RUNPODCTL_VERSION} with 'runpodctl update' or brew"
            )
        _SYNTAX_OK = True

    def _list_pods(self) -> list[dict[str, Any]]:
        """Return every pod on the account, including stopped ones.

        ``-a`` is mandatory: without it ``pod list`` hides EXITED pods, and a stopped fwd pod would look GONE and be
        silently duplicated on the next launch.
        """
        return parse_pod_list(self._run_ctl(["pod", "list", "-a"]))

    def _get_pod(self, pod_id: str) -> dict[str, Any] | None:
        """Fetch one pod, returning ``None`` if RunPod says it no longer exists.

        Raises:
            RunpodError: On failures other than a 404, so genuine outages are not misreported as a deleted pod.
        """
        try:
            return parse_pod(self._run_ctl(["pod", "get", pod_id], check=False))
        except RunpodError as exc:
            if is_missing_pod_error(str(exc)):
                return None
            raise

    def _create_pod(self, pod_name: str, gpu: str | None) -> dict[str, Any]:
        """Create a pod from the target config, always publishing 22/tcp and mounting the persistent volume."""
        return parse_pod(self._run_ctl(create_pod_args(self.target, pod_name, gpu), timeout=300.0))

    def _wait_for_pod(self, pod_id: str, *, timeout: float = PROVISION_TIMEOUT, probe: bool = True) -> dict[str, Any]:
        """Poll ``pod get`` until the pod reports an ssh address that actually accepts TCP connections.

        The TCP probe is what makes this safe on the restart path, where ``pod get`` briefly replays the pre-stop
        address (see :func:`port_is_open`). Proxy-only pods have no address to probe, so they short-circuit on the
        provider's own status.

        Returns the final pod document.

        Raises:
            RunpodError: If the pod disappears, enters a terminal state, or never becomes reachable in time.
        """
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            pod = self._get_pod(pod_id)
            if pod is None:
                raise RunpodError(f"pod {pod_id} disappeared while starting")
            last = pod
            status = pod_status(pod)
            if status is TargetStatus.GONE:
                raise RunpodError(f"pod {pod_id} entered state {pod.get('desiredStatus')!r} while starting")
            if status is TargetStatus.RUNNING:
                address = parse_ssh_info(pod)
                if not probe or address is None or port_is_open(*address):
                    return pod
            time.sleep(POLL_INTERVAL)
        detail = (last or {}).get("lastStatusChange", "")
        raise RunpodError(f"pod {pod_id} did not become reachable within {timeout:.0f}s ({detail})")

    def _endpoint_from_pod(self, pod: dict[str, Any]) -> tuple[SSHEndpoint, list[str]]:
        """Build an :class:`~fwd.sshexec.SSHEndpoint` from a pod document, preferring the direct address.

        Returns the endpoint plus any warnings to surface. The proxy branch is deliberately gated on
        ``allow_proxy``: it disables rsync (forcing slow tar-over-ssh transfers for every push and pull), so silently
        degrading to it would look like fwd was simply broken.

        Raises:
            RunpodError: If neither a direct address nor a permitted proxy login is available.
        """
        cfg = self.target
        direct = parse_ssh_info(pod)
        if direct:
            host, port = direct
            return SSHEndpoint(host=host, user=cfg.user, port=port, key_path=cfg.key_path, supports_rsync=True), []
        proxy = parse_proxy_target(pod)
        if proxy and cfg.allow_proxy:
            user, _, host = proxy.partition("@")
            endpoint = SSHEndpoint(host=host, user=user, port=22, key_path=cfg.key_path, supports_rsync=False)
            return endpoint, [f"no direct IP for 22/tcp; using the {PROXY_HOST} proxy — rsync is unavailable, transfers fall back to tar-over-ssh"]
        if proxy:
            raise RunpodError(f"pod {pod.get('id')} only offers the {PROXY_HOST} proxy and allow_proxy is disabled for target {cfg.name!r}")
        raise RunpodError(f"pod {pod.get('id')} has no reachable ssh address; ensure the pod exposes 22/tcp")

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Reuse-or-create pod ``fwd-<session_name>``, poll to RUNNING, and resolve an SSH endpoint.

        Reuse is keyed on the pod *name* rather than stored state so that a lost or pruned ``state.json`` still
        cannot orphan a paid-for pod: the next launch finds it by name and adopts it.

        Args:
            gpu: Overrides the target's configured ``gpu`` id for this launch.
        """
        self._require_supported_cli()
        cfg = self.target
        pod_name = pod_name_for(session_name)
        notes: list[str] = []

        with ui.step(f"Looking up RunPod pod {pod_name}"):
            existing = find_pod_by_name(self._list_pods(), pod_name)

        if existing is None:
            with ui.step(f"Creating pod {pod_name} ({create_summary(cfg, gpu)})"):
                pod = self._create_pod(pod_name, gpu)
            # Record ownership before the readiness wait: Ctrl-C during that wait must delete this invocation's pod,
            # while a pod discovered by name remains categorically off-limits to interruption cleanup.
            self._created_pod_id = str(pod["id"])
        else:
            pod = existing
            pod_id = str(pod["id"])
            if pod_status(pod) is TargetStatus.STOPPED:
                with ui.step(f"Starting stopped pod {pod_name}"):
                    pod = parse_pod(self._run_ctl(["pod", "start", pod_id], timeout=180.0))
                notes.append("pod was restarted — the container disk was wiped, only the volume survived")
            else:
                notes.append(f"reusing existing pod {pod_name}")

        pod_id = str(pod["id"])
        with ui.step(f"Waiting for pod {pod_name} to expose ssh"):
            pod = self._wait_for_pod(pod_id)

        endpoint, endpoint_notes = self._endpoint_from_pod(pod)
        notes += endpoint_notes

        # Driven by what the pod actually reports rather than by compute_type alone: that catches a CPU pod (always
        # volume-less) and equally a GPU pod whose volume request was rejected for capacity.
        has_volume = bool(int(pod.get("volumeInGb") or 0))
        remote_dir, tool_prefix, scratch, path_notes = resolve_paths(cfg, project_name, has_volume=has_volume)
        notes += path_notes

        return TargetInfo(
            endpoint=endpoint,
            remote_dir=remote_dir,
            status=TargetStatus.RUNNING,
            backend_ids={"pod_id": pod_id, "pod_name": pod_name},
            tool_prefix=tool_prefix,
            scratch=scratch,
            notes=notes,
        )

    def cleanup_interrupted_provision(self, session_name: str) -> bool:
        """Delete only the pod created by this backend instance for the interrupted launch."""
        del session_name
        pod_id = self._created_pod_id
        if not pod_id:
            return False
        # A failed delete must propagate so the caller never claims cleanup succeeded while a pod keeps billing.
        self._run_ctl(["pod", "delete", pod_id], check=True)
        self._created_pod_id = None
        return True

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Re-resolve the pod's current direct ``IP:port`` for 22/tcp, falling back to the proxy if permitted.

        Always hits the provider: RunPod republishes 22/tcp on a fresh host port after every restart, so the address
        cached in ``state.json`` is only ever a hint.
        """
        pod = self._get_pod(self._pod_id(session))
        if pod is None:
            raise RunpodError(f"pod for session {session.name!r} no longer exists; run 'fwd rm {session.name}' or launch again")
        if pod_status(pod) is TargetStatus.STOPPED:
            raise RunpodError(f"pod for session {session.name!r} is stopped; run 'fwd up' to restart it")
        endpoint, _ = self._endpoint_from_pod(pod)
        return endpoint

    def status(self, session: SessionState) -> TargetStatus:
        """Map RunPod pod state to :class:`~fwd.backends.base.TargetStatus`.

        Never raises — ``fwd ls`` renders one row per session and a single provider hiccup must not blank the table.

        Only a **confirmed** 404 yields ``GONE``; every other failure yields ``UNKNOWN``. The live e2e run
        (docs/live-e2e-report.md, R2-1) hit exactly this: RunPod's API is briefly flaky right after ``pod stop``, the
        transient error was reported as ``GONE``, and ``fwd attach`` then offered to delete the state entry of a pod
        that still existed. Since ``GONE`` is what unlocks that destructive prompt, "cannot ask" must stay distinct
        from "does not exist" — a wrong ``GONE`` is how a paid-for pod gets orphaned.
        """
        try:
            pod = self._get_pod(self._pod_id(session))
        except RunpodError as exc:
            # _get_pod already converted a 404 into None, so reaching here means a transport/API/CLI failure. The
            # is_missing_pod_error check is belt-and-braces for a 404 surfacing from a different call.
            if is_missing_pod_error(str(exc)):
                return TargetStatus.GONE
            # The only place the provider's actual error text reaches the user: callers see a bare UNKNOWN, and in a
            # `fwd ls` table an unexplained "unknown" row is impossible to act on.
            ui.warn(f"could not determine RunPod status for session {session.name!r}: {exc}")
            return TargetStatus.UNKNOWN
        except KeyError:
            # State written by a different backend, or a truncated entry — not evidence that the pod is gone.
            return TargetStatus.UNKNOWN
        return TargetStatus.GONE if pod is None else pod_status(pod)

    def stop(self, session: SessionState) -> None:
        """``runpodctl pod stop`` — halts billing for compute while keeping the volume.

        Safe to call on an already-stopped or already-deleted pod; both are the caller's desired end state.
        """
        pod_id = session.backend_ids.get("pod_id")
        if pod_id:
            self._run_ctl(["pod", "stop", pod_id], check=False)

    def destroy(self, session: SessionState) -> None:
        """``runpodctl pod delete`` — deletes the pod and its volume irreversibly."""
        pod_id = session.backend_ids.get("pod_id")
        if pod_id:
            self._run_ctl(["pod", "delete", pod_id], check=False)

    def doctor(self) -> list[CheckResult]:
        """Check ``runpodctl`` presence, supported syntax, API key configuration, and a live ``pod list``."""
        checks: list[CheckResult] = []

        path = shutil.which(RUNPODCTL)
        if path is None:
            checks.append(CheckResult("runpodctl", False, "not found on PATH", "install from https://github.com/runpod/runpodctl or 'brew install runpod/runpodctl/runpodctl'"))
            return checks
        version = subprocess.run([RUNPODCTL, "--version"], capture_output=True, text=True).stdout.strip()
        checks.append(CheckResult("runpodctl", True, f"{version or 'installed'} ({path})"))

        try:
            self._require_supported_cli()
            checks.append(CheckResult("runpodctl syntax", True, "noun-first ('pod create') with JSON output"))
        except RunpodError as exc:
            checks.append(CheckResult("runpodctl syntax", False, str(exc), f"upgrade to runpodctl >= {MIN_RUNPODCTL_VERSION}"))
            return checks

        # Presence only: the key's value is never read into fwd, so it can never reach a log line or traceback.
        has_key = bool(os.environ.get("RUNPOD_API_KEY")) or RUNPOD_CONFIG_PATH.is_file()
        checks.append(
            CheckResult(
                "runpod api key",
                has_key,
                "RUNPOD_API_KEY set" if os.environ.get("RUNPOD_API_KEY") else (f"found {RUNPOD_CONFIG_PATH}" if has_key else "no key configured"),
                None if has_key else "run 'runpodctl doctor' to store a key, or export RUNPOD_API_KEY",
            )
        )

        try:
            pods = self._list_pods()
            checks.append(CheckResult("runpod api", True, f"{len(pods)} pod(s) visible"))
        except RunpodError as exc:
            checks.append(CheckResult("runpod api", False, str(exc), "check the api key and network connectivity"))
        return checks

    def _pod_id(self, session: SessionState) -> str:
        """Return the session's pod id.

        Raises:
            RunpodError: If state never recorded one, which means the session was written by a different backend.
        """
        pod_id = session.backend_ids.get("pod_id")
        if not pod_id:
            raise RunpodError(f"session {session.name!r} has no recorded pod_id")
        return pod_id
