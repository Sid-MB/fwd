"""Lambda On-Demand Cloud backend implemented directly against the public REST API.

Lambda instances have no suspend operation and their local disks are destroyed at termination. This backend therefore
uses the same durable-compute split as persistent RunPod sessions: one deterministic filesystem belongs to each fwd
session, ``stop`` terminates only compute, and the next ``provision`` launches a replacement instance with that
filesystem mounted. ``destroy`` is the sole path that deletes the filesystem.

The API key is intentionally local-only. Lambda API keys currently grant the full account API surface, so fwd reads
``LAMBDA_API_KEY`` for each local invocation and never writes it to config, session state, subprocess argv, or the
instance. Consequently this backend inherits the safe default of no ``remote_stop_command`` and does not support
``--stop-after``; enabling that feature would otherwise require copying a broad credential onto remote compute.

The provider documents a general limit of one request per second. A process-wide start-rate gate covers backend
instances created concurrently by ``fwd ls`` while leaving network I/O outside the lock. Polling uses a longer interval
and launch is issued at most once after deterministic name reconciliation, respecting the stricter launch limit too.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

from fwd import ui
from fwd.backends.base import Backend, CheckResult, ConfigChoice, ConfigChoices, ConfigParameter, MachineInventory, MachineSelectionError, MachineType, ProvisionError, TargetInfo, TargetStatus
from fwd.config import Config, LambdaTargetConfig
from fwd.sshexec import SSHEndpoint
from fwd.state import SessionState

API_BASE_URL = "https://cloud.lambda.ai/api/v1"
API_KEY_ENV = "LAMBDA_API_KEY"
REQUEST_TIMEOUT = 30.0
MIN_REQUEST_INTERVAL = 1.05
POLL_INTERVAL = 4.0
PROVISION_TIMEOUT = 600.0
TERMINATE_TIMEOUT = 300.0
PORT_PROBE_TIMEOUT = 3.0

_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


class LambdaCloudError(ProvisionError):
    """A Lambda Cloud request failed, with structured status/code retained for safe lifecycle decisions."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def missing(self) -> bool:
        """Return whether the provider definitively reported that the requested object does not exist."""
        return self.status == 404 or self.code in {"global/not-found", "global/object-does-not-exist"}


def _provider_name(prefix: str, session_name: str, suffix: str, *, limit: int) -> str:
    """Build a deterministic provider-safe name, retaining a digest whenever truncation or sanitization occurs."""
    raw = f"{prefix}-{session_name}-{suffix}" if suffix else f"{prefix}-{session_name}"
    safe = re.sub(r"[^A-Za-z0-9-]+", "-", raw).strip("-") or prefix
    if not safe[0].isalpha():
        safe = f"{prefix}-{safe}"
    if safe == raw and len(safe) <= limit:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    stem = safe[: max(1, limit - len(digest) - 1)].rstrip("-") or prefix
    return f"{stem}-{digest}"


def instance_name_for(session_name: str) -> str:
    """Return the stable Lambda instance name used for lookup and retry adoption."""
    return _provider_name("fwd", session_name, "", limit=64)


def filesystem_name_for(session_name: str) -> str:
    """Return the stable Lambda filesystem name used to preserve a session across instance termination."""
    return _provider_name("fwd", session_name, "data", limit=60)


def _instance_status(instance: dict[str, Any]) -> TargetStatus:
    """Normalize Lambda's documented instance states without treating provider failures as resource absence."""
    status = str(instance.get("status") or "").lower()
    if status == "active":
        return TargetStatus.RUNNING
    if status in {"booting", "terminating"}:
        return TargetStatus.PENDING
    if status in {"terminated", "preempted"}:
        return TargetStatus.STOPPED
    return TargetStatus.UNKNOWN


def _port_is_open(host: str, port: int) -> bool:
    """Return whether Lambda's reported SSH address currently accepts a TCP connection."""
    try:
        with socket.create_connection((host, port), timeout=PORT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


class LambdaCloudClient:
    """Small JSON client for the Lambda Cloud endpoints fwd needs, with secrets confined to HTTP headers."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")).strip()
        if not self.api_key:
            raise LambdaCloudError(f"{API_KEY_ENV} is not set; create an API key in the Lambda Cloud console and export it locally")

    @staticmethod
    def _rate_limit() -> None:
        """Serialize request start times across backend instances to honor Lambda's one-request-per-second limit."""
        global _LAST_REQUEST_AT
        with _RATE_LOCK:
            delay = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
            if delay > 0:
                time.sleep(delay)
            _LAST_REQUEST_AT = time.monotonic()

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT) -> Any:
        """Send one authenticated request and unwrap Lambda's ``data`` envelope into Python values."""
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{API_BASE_URL}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "fwd-lambda-backend",
            },
        )
        self._rate_limit()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            code, message, suggestion = self._error_detail(raw)
            detail = message or exc.reason or f"HTTP {exc.code}"
            if suggestion:
                detail = f"{detail} ({suggestion})"
            raise LambdaCloudError(f"Lambda Cloud API {method} {path} failed: {detail}", status=exc.code, code=code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LambdaCloudError(f"Lambda Cloud API {method} {path} failed: {exc}") from exc
        try:
            document = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise LambdaCloudError(f"Lambda Cloud API {method} {path} returned invalid JSON") from exc
        if not isinstance(document, dict) or "data" not in document:
            code, message, suggestion = self._error_detail(raw)
            detail = message or "response did not contain a data object"
            if suggestion:
                detail = f"{detail} ({suggestion})"
            raise LambdaCloudError(f"Lambda Cloud API {method} {path} failed: {detail}", code=code)
        return document["data"]

    @staticmethod
    def _error_detail(raw: str) -> tuple[str | None, str | None, str | None]:
        """Extract Lambda's structured error fields without ever including request headers or credentials."""
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None, raw.strip()[:400] or None, None
        error = document.get("error") if isinstance(document, dict) else None
        if not isinstance(error, dict):
            return None, raw.strip()[:400] or None, None
        return (
            str(error.get("code")) if error.get("code") else None,
            str(error.get("message")) if error.get("message") else None,
            str(error.get("suggestion")) if error.get("suggestion") else None,
        )

    def list_instances(self) -> list[dict[str, Any]]:
        """Return current account instances visible through Lambda's instance collection."""
        data = self.request("GET", "/instances")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        """Return one instance, or ``None`` only after a definitive provider 404."""
        try:
            data = self.request("GET", f"/instances/{urllib.parse.quote(instance_id, safe='')}")
        except LambdaCloudError as exc:
            if exc.missing:
                return None
            raise
        if not isinstance(data, dict):
            raise LambdaCloudError(f"Lambda Cloud API returned an invalid instance document for {instance_id}")
        return data

    def list_filesystems(self) -> list[dict[str, Any]]:
        """Return persistent filesystems; Lambda uses ``file-systems`` only for this list endpoint."""
        data = self.request("GET", "/file-systems")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def create_filesystem(self, name: str, region: str) -> dict[str, Any]:
        """Create one region-bound persistent filesystem and return its provider document."""
        data = self.request("POST", "/filesystems", {"name": name, "region": region})
        if not isinstance(data, dict) or not data.get("id"):
            raise LambdaCloudError("Lambda Cloud filesystem creation returned no filesystem id")
        return data

    def delete_filesystem(self, filesystem_id: str) -> None:
        """Delete one filesystem by stable id; an already-absent filesystem is a successful no-op."""
        try:
            self.request("DELETE", f"/filesystems/{urllib.parse.quote(filesystem_id, safe='')}")
        except LambdaCloudError as exc:
            if not exc.missing:
                raise

    def launch_instance(self, payload: dict[str, Any]) -> str:
        """Launch exactly one instance and return the provider id from the operation response."""
        data = self.request("POST", "/instance-operations/launch", payload, timeout=120.0)
        ids = data.get("instance_ids") if isinstance(data, dict) else None
        if not isinstance(ids, list) or len(ids) != 1 or not ids[0]:
            raise LambdaCloudError("Lambda Cloud launch returned anything other than one instance id")
        return str(ids[0])

    def terminate_instance(self, instance_id: str) -> None:
        """Terminate one instance; definitive absence is a successful no-op for repeatable lifecycle calls."""
        try:
            self.request("POST", "/instance-operations/terminate", {"instance_ids": [instance_id]}, timeout=120.0)
        except LambdaCloudError as exc:
            if not exc.missing:
                raise


class LambdaCloudBackend(Backend):
    """Provision Lambda GPU instances while retaining fwd state on session-owned filesystems."""

    name: ClassVar[str] = "lambda"

    def __init__(self, target: LambdaTargetConfig, config: Config) -> None:
        super().__init__(target, config)
        self._client: LambdaCloudClient | None = None
        self._created_instance_id: str | None = None
        self._created_filesystem_id: str | None = None
        self._selected_machine: str | None = None

    @property
    def client(self) -> LambdaCloudClient:
        """Construct the credential-bearing client lazily so imports and help never require authentication."""
        if self._client is None:
            self._client = LambdaCloudClient()
        return self._client

    @property
    def effective_instance_type(self) -> str:
        """Return the per-launch override when present, otherwise the target's configured default instance type."""
        return self._selected_machine or self.target.instance_type

    def machine_inventory(self) -> MachineInventory:
        """Split Lambda's current instance-type catalog by capacity in this target's configured region."""
        try:
            data = self.client.request("GET", "/instance-types")
        except LambdaCloudError as exc:
            unavailable = (MachineType(self.target.instance_type, "configured default; availability could not be checked"),) if self.target.instance_type else ()
            return MachineInventory(default=self.target.instance_type or None, selectable=True, unavailable=unavailable, error=str(exc))
        if not isinstance(data, dict):
            unavailable = (MachineType(self.target.instance_type, "configured default; availability could not be checked"),) if self.target.instance_type else ()
            return MachineInventory(default=self.target.instance_type or None, selectable=True, unavailable=unavailable, error="Lambda Cloud returned an invalid instance-type inventory")
        available: list[MachineType] = []
        unavailable: list[MachineType] = []
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            spec = item.get("instance_type") if isinstance(item.get("instance_type"), dict) else {}
            value = str(spec.get("name") or key).strip()
            if not value:
                continue
            regions = item.get("regions_with_capacity_available")
            region_names = {str(region.get("name")) for region in regions if isinstance(region, dict) and region.get("name")} if isinstance(regions, list) else set()
            gpu = str(spec.get("gpu_description") or spec.get("description") or "").strip()
            price = spec.get("price_cents_per_hour")
            detail = ", ".join(part for part in (gpu, f"{price} cents/hour" if price is not None else "") if part)
            (available if self.target.region in region_names else unavailable).append(MachineType(value, detail or f"no capacity in {self.target.region}"))
        values = {item.value for item in (*available, *unavailable)}
        if self.target.instance_type and self.target.instance_type not in values:
            unavailable.append(MachineType(self.target.instance_type, "configured default was not returned by Lambda Cloud"))
        default = self.target.instance_type or None
        return MachineInventory(default=default, selectable=True, available=tuple(sorted(available, key=lambda item: (item.value != default, item.value))), unavailable=tuple(sorted(unavailable, key=lambda item: (item.value != default, item.value))))

    def select_machine(self, machine: str) -> None:
        """Validate regional Lambda capacity and retain the exact instance-type name for this launch."""
        inventory = self.machine_inventory()
        if inventory.error:
            raise MachineSelectionError(f"could not validate Lambda machine {machine!r} for target {self.target.name!r}: {inventory.error}", inventory, self.target)
        if machine in {item.value for item in inventory.unavailable}:
            raise MachineSelectionError(f"Lambda machine {machine!r} is currently unavailable in {self.target.region} for target {self.target.name!r}", inventory, self.target)
        if machine not in {item.value for item in inventory.available}:
            raise MachineSelectionError(f"unknown Lambda machine {machine!r} for target {self.target.name!r}", inventory, self.target)
        self._selected_machine = machine

    @classmethod
    def config_parameters(cls) -> tuple[ConfigParameter, ...]:
        """Describe required account choices first and keep path/image overrides behind the advanced gate."""
        return (
            ConfigParameter("region", "--region", "Lambda Cloud region", required=True),
            ConfigParameter("instance_type", "--instance-type", "Lambda instance type with current regional capacity", required=True),
            ConfigParameter("ssh_key_name", "--ssh-key-name", "Lambda Cloud SSH key name", required=True),
            ConfigParameter("persistent", "--persistent", "retain a session-owned filesystem when compute terminates", choices=(ConfigChoice("true"), ConfigChoice("false")), allow_free_text=False),
            ConfigParameter("image_id", "--image-id", "optional Lambda image id", advanced=True),
            ConfigParameter("filesystem_mount_path", "--fs-mount-path", "absolute mount path for persistent session data", advanced=True),
            ConfigParameter("remote_base", "--remote-base", "parent directory for project checkouts", advanced=True),
            ConfigParameter("tool_prefix", "--tool-prefix", "persistent path for installed tools, agent state, and caches", advanced=True),
            ConfigParameter("user", "--user", "remote SSH username", advanced=True),
            ConfigParameter("port", "--port", "SSH port", prompt=False),
            ConfigParameter("key_path", "--key-path", "local private key matching ssh_key_name", advanced=True),
        )

    @classmethod
    def config_choices(cls, parameter: ConfigParameter, values: dict[str, Any]) -> ConfigChoices:
        """Discover current regions, capacity, keys, and images without making setup depend on API availability."""
        try:
            client = LambdaCloudClient()
            if parameter.name == "region":
                data = client.request("GET", "/regions")
                choices = tuple(ConfigChoice(str(item["name"]), str(item.get("description") or "") or None) for item in data if isinstance(item, dict) and item.get("name")) if isinstance(data, list) else ()
                return ConfigChoices(choices, allow_free_text=True)
            if parameter.name == "instance_type":
                data = client.request("GET", "/instance-types")
                region = str(values.get("region") or "")
                choices: list[ConfigChoice] = []
                if isinstance(data, dict):
                    for key, item in data.items():
                        if not isinstance(item, dict):
                            continue
                        regions = item.get("regions_with_capacity_available")
                        region_names = {str(entry.get("name")) for entry in regions if isinstance(entry, dict) and entry.get("name")} if isinstance(regions, list) else set()
                        if region and region not in region_names:
                            continue
                        detail = item.get("instance_type") if isinstance(item.get("instance_type"), dict) else {}
                        name = str(detail.get("name") or key)
                        description = str(detail.get("description") or detail.get("gpu_description") or "") or None
                        choices.append(ConfigChoice(name, description))
                return ConfigChoices(tuple(choices), allow_free_text=True)
            if parameter.name == "ssh_key_name":
                data = client.request("GET", "/ssh-keys")
                choices = tuple(ConfigChoice(str(item["name"])) for item in data if isinstance(item, dict) and item.get("name")) if isinstance(data, list) else ()
                return ConfigChoices(choices, allow_free_text=True)
            if parameter.name == "image_id":
                data = client.request("GET", "/images")
                region = str(values.get("region") or "")
                choices = []
                if isinstance(data, list):
                    for item in data:
                        if not isinstance(item, dict) or not item.get("id"):
                            continue
                        item_region = item.get("region") if isinstance(item.get("region"), dict) else {}
                        if region and str(item_region.get("name") or "") != region:
                            continue
                        choices.append(ConfigChoice(str(item["id"]), str(item.get("description") or item.get("name") or "") or None))
                return ConfigChoices(tuple(choices), allow_free_text=True)
        except (LambdaCloudError, KeyError, TypeError, ValueError):
            pass
        return super().config_choices(parameter, values)

    def _find_instance(self, name: str) -> dict[str, Any] | None:
        """Find one nonterminal instance by deterministic name, rejecting ambiguous duplicates before mutation."""
        matches = [instance for instance in self.client.list_instances() if instance.get("name") == name and str(instance.get("status") or "").lower() not in {"terminated", "preempted"}]
        if len(matches) > 1:
            ids = ", ".join(str(instance.get("id") or "<unknown>") for instance in matches)
            raise LambdaCloudError(f"multiple live Lambda instances named {name!r} exist ({ids}); terminate the duplicate before retrying")
        return matches[0] if matches else None

    def _find_filesystem(self, name: str) -> dict[str, Any] | None:
        """Find the session filesystem in the configured region, rejecting duplicates and cross-region collisions."""
        matches = [filesystem for filesystem in self.client.list_filesystems() if filesystem.get("name") == name]
        in_region = [filesystem for filesystem in matches if str((filesystem.get("region") or {}).get("name") if isinstance(filesystem.get("region"), dict) else filesystem.get("region") or "") == self.target.region]
        if len(in_region) > 1:
            ids = ", ".join(str(filesystem.get("id") or "<unknown>") for filesystem in in_region)
            raise LambdaCloudError(f"multiple Lambda filesystems named {name!r} exist in {self.target.region} ({ids}); delete the duplicate before retrying")
        if matches and not in_region:
            regions = ", ".join(sorted({str((filesystem.get("region") or {}).get("name") if isinstance(filesystem.get("region"), dict) else filesystem.get("region") or "unknown") for filesystem in matches}))
            raise LambdaCloudError(f"Lambda filesystem {name!r} exists in {regions}, but target {self.target.name!r} uses {self.target.region}; use the original region or remove the conflicting filesystem")
        return in_region[0] if in_region else None

    def _ensure_filesystem(self, session_name: str, existing: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Reuse prior session storage even after config drift, or create it when persistent policy requires one."""
        if existing is not None:
            return existing
        if not self.target.persistent:
            return None
        name = filesystem_name_for(session_name)
        filesystem = self.client.create_filesystem(name, self.target.region)
        self._created_filesystem_id = str(filesystem["id"])
        return filesystem

    def _validate_existing_instance(self, instance: dict[str, Any], filesystem: dict[str, Any] | None) -> None:
        """Refuse live reuse when config or mounted storage differs, because correcting it would terminate active work."""
        cfg = self.target
        instance_region = instance.get("region") if isinstance(instance.get("region"), dict) else {}
        actual_region = str(instance_region.get("name") or "")
        instance_type = instance.get("instance_type") if isinstance(instance.get("instance_type"), dict) else {}
        actual_type = str(instance_type.get("name") or "")
        if actual_region and actual_region != cfg.region:
            raise LambdaCloudError(f"live Lambda instance {instance.get('name')!r} is in {actual_region}, but target {cfg.name!r} now selects {cfg.region}; stop the session before changing regions or restore the original config")
        if actual_type and actual_type != self.effective_instance_type:
            raise LambdaCloudError(f"live Lambda instance {instance.get('name')!r} uses {actual_type}, but this launch selects {self.effective_instance_type}; stop the session before changing hardware or restore the original selection")
        if cfg.persistent and filesystem is None:
            raise LambdaCloudError(f"live Lambda instance {instance.get('name')!r} has no deterministic session filesystem; refusing to claim its local disk is persistent—stop/remove it or set persistent = false explicitly")
        if filesystem is None:
            return
        mounts = instance.get("file_system_mounts")
        mounted = any(
            isinstance(mount, dict)
            and str(mount.get("file_system_id") or "") == str(filesystem.get("id") or "")
            and str(mount.get("mount_point") or "").rstrip("/") == cfg.filesystem_mount_path
            for mount in mounts
        ) if isinstance(mounts, list) else False
        if not mounted:
            raise LambdaCloudError(f"live Lambda instance {instance.get('name')!r} is not using filesystem {filesystem.get('id')!r} at {cfg.filesystem_mount_path}; Lambda cannot attach storage after launch, so stop the session before retrying")

    def _launch_instance(self, name: str, filesystem: dict[str, Any] | None) -> str:
        """Issue one launch with the configured key/image and optional filesystem mounted at the durable path."""
        cfg = self.target
        payload: dict[str, Any] = {
            "region_name": cfg.region,
            "instance_type_name": self.effective_instance_type,
            "ssh_key_names": [cfg.ssh_key_name],
            "name": name,
        }
        if cfg.image_id:
            payload["image"] = {"id": cfg.image_id}
        if filesystem is not None:
            payload["file_system_mounts"] = [{"file_system_id": str(filesystem["id"]), "mount_point": cfg.filesystem_mount_path}]
        instance_id = self.client.launch_instance(payload)
        self._created_instance_id = instance_id
        return instance_id

    def _wait_for_instance(self, instance_id: str, *, timeout: float = PROVISION_TIMEOUT, probe_port: bool = True) -> dict[str, Any]:
        """Poll until the instance is active, has a public IP, and optionally accepts TCP on the SSH port."""
        deadline = time.monotonic() + timeout
        last_status = "unknown"
        while time.monotonic() < deadline:
            instance = self.client.get_instance(instance_id)
            if instance is None:
                raise LambdaCloudError(f"Lambda instance {instance_id} disappeared while starting")
            last_status = str(instance.get("status") or "unknown")
            status = _instance_status(instance)
            host = str(instance.get("ip") or "")
            if status is TargetStatus.RUNNING and host and (not probe_port or _port_is_open(host, self.target.port)):
                return instance
            if str(instance.get("status") or "").lower() in {"unhealthy", "terminated", "preempted"}:
                raise LambdaCloudError(f"Lambda instance {instance_id} entered state {last_status!r} while starting")
            time.sleep(POLL_INTERVAL)
        raise LambdaCloudError(f"Lambda instance {instance_id} did not expose SSH within {timeout:.0f}s (last state: {last_status})")

    def _endpoint_from_instance(self, instance: dict[str, Any]) -> SSHEndpoint:
        """Build a direct rsync-capable SSH endpoint from an active Lambda instance document."""
        host = str(instance.get("ip") or "")
        if not host:
            raise LambdaCloudError(f"Lambda instance {instance.get('id') or '<unknown>'} has no public IP")
        return SSHEndpoint(host=host, user=self.target.user, port=self.target.port, key_path=self.target.key_path, supports_rsync=True)

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Reuse live compute or launch a replacement around the session's deterministic persistent filesystem."""
        del gpu
        cfg = self.target
        if not cfg.region or not self.effective_instance_type or not cfg.ssh_key_name:
            missing = [name for name, value in (("region", cfg.region), ("instance_type", self.effective_instance_type), ("ssh_key_name", cfg.ssh_key_name)) if not value]
            raise LambdaCloudError(f"target {cfg.name!r} is missing required Lambda setting(s): {', '.join(missing)}; rerun `fwd setup --backend lambda --force` with the required flags")
        name = instance_name_for(session_name)
        notes: list[str] = []
        with ui.step(f"Looking up Lambda instance {name}"):
            instance = self._find_instance(name)
        with ui.step(f"Preparing Lambda storage for {name}"):
            filesystem = self._find_filesystem(filesystem_name_for(session_name))
        if instance is None:
            filesystem = self._ensure_filesystem(session_name, filesystem)
            with ui.step(f"Launching Lambda instance {name} ({self.effective_instance_type} in {cfg.region})"):
                instance_id = self._launch_instance(name, filesystem)
        else:
            self._validate_existing_instance(instance, filesystem)
            instance_id = str(instance.get("id") or "")
            if not instance_id:
                raise LambdaCloudError(f"Lambda instance {name!r} has no provider id")
            notes.append(f"reusing existing Lambda instance {name}")
        with ui.step(f"Waiting for Lambda instance {name} to expose SSH"):
            instance = self._wait_for_instance(instance_id)
        endpoint = self._endpoint_from_instance(instance)
        filesystem_id = str(filesystem["id"]) if filesystem is not None else None
        if filesystem_id:
            notes.append(f"persistent session data is mounted at {cfg.filesystem_mount_path}; stopping terminates compute but retains this filesystem")
            if not cfg.persistent:
                notes.append("target now sets persistent = false, but an existing session filesystem was retained and reattached to avoid silently abandoning durable data; remove the session before creating a truly disposable replacement")
        else:
            notes.append("disposable Lambda target: stopping terminates the instance and permanently erases the remote checkout, tools, credentials, and agent state")
        return TargetInfo(
            endpoint=endpoint,
            remote_dir=f"{cfg.remote_base.rstrip('/')}/{project_name}",
            status=TargetStatus.RUNNING,
            backend_ids={
                "instance_id": instance_id,
                "instance_name": name,
                "region": cfg.region,
                **({"filesystem_id": filesystem_id, "filesystem_name": str(filesystem.get("name") or filesystem_name_for(session_name))} if filesystem_id and filesystem is not None else {}),
            },
            tool_prefix=cfg.tool_prefix,
            ephemeral_home=True,
            notes=notes,
        )

    def cleanup_interrupted_provision(self, session_name: str) -> bool:
        """Remove only compute and storage created by this backend object during the interrupted launch."""
        del session_name
        removed = bool(self._created_instance_id or self._created_filesystem_id)
        if self._created_instance_id:
            self.client.terminate_instance(self._created_instance_id)
            self._wait_until_terminated(self._created_instance_id)
            self._created_instance_id = None
        if self._created_filesystem_id:
            self._wait_until_filesystem_detached(self._created_filesystem_id)
            self.client.delete_filesystem(self._created_filesystem_id)
            self._created_filesystem_id = None
        return removed

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Re-resolve the active instance IP from its stable provider id without starting or creating compute."""
        instance_id = self._instance_id(session)
        instance = self.client.get_instance(instance_id)
        if instance is None:
            raise LambdaCloudError(f"Lambda instance for session {session.name!r} no longer exists; run {ui.command(f'attach {session.name} --restart')!r} to recreate it")
        if _instance_status(instance) is not TargetStatus.RUNNING:
            raise LambdaCloudError(f"Lambda instance for session {session.name!r} is {instance.get('status')!r}; run {ui.command(f'attach {session.name} --restart')!r} after it finishes stopping")
        return self._endpoint_from_instance(instance)

    def _filesystem_exists(self, filesystem_id: str) -> bool:
        """Confirm whether the persisted session filesystem remains visible to the account."""
        return any(str(filesystem.get("id") or "") == filesystem_id for filesystem in self.client.list_filesystems())

    def status(self, session: SessionState) -> TargetStatus:
        """Return normalized Lambda state; API/auth/network failures are always ``UNKNOWN``, never ``GONE``."""
        try:
            instance = self.client.get_instance(self._instance_id(session))
            if instance is not None:
                return _instance_status(instance)
            filesystem_id = session.backend_ids.get("filesystem_id")
            if filesystem_id and self._filesystem_exists(filesystem_id):
                return TargetStatus.STOPPED
            return TargetStatus.GONE
        except (LambdaCloudError, KeyError) as exc:
            ui.warn(f"could not determine Lambda status for session {session.name!r}: {exc}")
            return TargetStatus.UNKNOWN

    def list_status(self, session: SessionState) -> TargetStatus:
        """Use the authoritative mapping; each API request already has a short hard timeout."""
        return self.status(session)

    def stop(self, session: SessionState) -> None:
        """Terminate billable compute while retaining the independently managed session filesystem."""
        instance_id = session.backend_ids.get("instance_id")
        if instance_id:
            self.client.terminate_instance(instance_id)

    def _wait_until_terminated(self, instance_id: str, *, timeout: float = TERMINATE_TIMEOUT) -> None:
        """Wait until Lambda reports a terminal state or definitive absence before deleting attached storage."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            instance = self.client.get_instance(instance_id)
            if instance is None or str(instance.get("status") or "").lower() in {"terminated", "preempted"}:
                return
            time.sleep(POLL_INTERVAL)
        raise LambdaCloudError(f"Lambda instance {instance_id} did not terminate within {timeout:.0f}s")

    def _wait_until_filesystem_detached(self, filesystem_id: str, *, timeout: float = TERMINATE_TIMEOUT) -> None:
        """Wait until the filesystem is absent or no longer mounted, because Lambda rejects deletion while in use."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = next((filesystem for filesystem in self.client.list_filesystems() if str(filesystem.get("id") or "") == filesystem_id), None)
            if match is None or not bool(match.get("is_in_use")):
                return
            time.sleep(POLL_INTERVAL)
        raise LambdaCloudError(f"Lambda filesystem {filesystem_id} remained mounted for more than {timeout:.0f}s after instance termination")

    def destroy(self, session: SessionState) -> None:
        """Terminate the instance and irreversibly delete only the fwd-owned filesystem recorded for this session."""
        instance_id = session.backend_ids.get("instance_id")
        if instance_id:
            self.client.terminate_instance(instance_id)
            self._wait_until_terminated(instance_id)
        filesystem_id = session.backend_ids.get("filesystem_id")
        if filesystem_id:
            self._wait_until_filesystem_detached(filesystem_id)
            self.client.delete_filesystem(filesystem_id)

    def doctor(self) -> list[CheckResult]:
        """Check local credentials, required target fields, API access/capacity, and the configured SSH key."""
        checks: list[CheckResult] = []
        has_key = bool(os.environ.get(API_KEY_ENV, "").strip())
        checks.append(CheckResult("lambda api key", has_key, f"{API_KEY_ENV} set" if has_key else f"{API_KEY_ENV} is not set", None if has_key else "create an API key in the Lambda Cloud console and export LAMBDA_API_KEY locally"))
        required = {"region": self.target.region, "instance_type": self.target.instance_type, "ssh_key_name": self.target.ssh_key_name}
        missing = [name for name, value in required.items() if not value]
        checks.append(CheckResult("lambda target", not missing, f"{self.target.instance_type or '<instance type>'} in {self.target.region or '<region>'}" if not missing else f"missing {', '.join(missing)}", None if not missing else "rerun fwd setup --backend lambda with --region, --instance-type, and --ssh-key-name"))
        if not has_key:
            return checks
        try:
            data = self.client.request("GET", "/instance-types")
            item = data.get(self.target.instance_type) if isinstance(data, dict) else None
            regions = item.get("regions_with_capacity_available") if isinstance(item, dict) else []
            available_regions = {str(region.get("name")) for region in regions if isinstance(region, dict) and region.get("name")} if isinstance(regions, list) else set()
            available = bool(self.target.instance_type and self.target.region in available_regions)
            detail = f"{self.target.instance_type} has capacity in {self.target.region}" if available else f"{self.target.instance_type or '<unset>'} has no reported capacity in {self.target.region or '<unset>'}"
            checks.append(CheckResult("lambda capacity", available, detail, None if available else "run fwd setup again and select an instance type and region with current capacity"))
        except LambdaCloudError as exc:
            checks.append(CheckResult("lambda api", False, str(exc), "check LAMBDA_API_KEY and network connectivity"))
            return checks
        try:
            data = self.client.request("GET", "/ssh-keys")
            names = {str(item.get("name")) for item in data if isinstance(item, dict) and item.get("name")} if isinstance(data, list) else set()
            found = bool(self.target.ssh_key_name and self.target.ssh_key_name in names)
            checks.append(CheckResult("lambda ssh key", found, f"found {self.target.ssh_key_name!r}" if found else f"key {self.target.ssh_key_name or '<unset>'!r} not found", None if found else "add the public key in Lambda Cloud or update ssh_key_name"))
        except LambdaCloudError as exc:
            checks.append(CheckResult("lambda ssh key", False, str(exc), "check Lambda Cloud API access"))
        return checks

    @staticmethod
    def _instance_id(session: SessionState) -> str:
        """Return the stable provider instance id recorded in session state."""
        instance_id = session.backend_ids.get("instance_id")
        if not instance_id:
            raise LambdaCloudError(f"session {session.name!r} has no recorded Lambda instance_id")
        return instance_id
