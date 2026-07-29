"""Local SSH port-forward primitives owned by one fwd session.

Each session receives a dedicated OpenSSH control socket instead of borrowing fwd's short-lived launch multiplexer.
That separation lets forwards survive after ``fwd ports`` returns, gives later CLI invocations a reliable liveness
probe, and lets ``fwd stop`` close only the tunnels for the selected session.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fwd.sshexec import SOCKET_PATH_LIMIT, SOCKET_SUFFIX_BUDGET, SSHControlMaster, SSHEndpoint, SSHError

PORT_CONTROL_DIR = Path.home() / ".fwd" / "ports"
PORT_SPEC_PATTERN = re.compile(r"^(?P<local>[0-9]+)(?::(?P<remote>[0-9]+))?$")


class PortForwardError(RuntimeError):
    """A port specification, local bind, or OpenSSH forwarding operation failed."""


@dataclass(frozen=True, slots=True)
class PortMapping:
    """One loopback-only ``local_port -> remote_port`` SSH forwarding."""

    local: int
    remote: int

    @classmethod
    def parse(cls, value: str) -> PortMapping:
        """Parse ``PORT`` or ``LOCAL:REMOTE`` and validate the TCP port range."""
        match = PORT_SPEC_PATTERN.fullmatch(value)
        if match is None:
            raise PortForwardError(f"invalid port mapping {value!r}; use PORT or LOCAL:REMOTE")
        local = int(match.group("local"))
        remote = int(match.group("remote") or local)
        for label, port in (("local", local), ("remote", remote)):
            if not 1 <= port <= 65535:
                raise PortForwardError(f"{label} port must be between 1 and 65535 (got {port})")
        return cls(local=local, remote=remote)

    @classmethod
    def from_dict(cls, value: dict[str, int]) -> PortMapping:
        """Rebuild a mapping from tolerant session-state data."""
        return cls(local=int(value["local"]), remote=int(value["remote"]))

    def to_dict(self) -> dict[str, int]:
        """Return the stable JSON shape stored with a session."""
        return {"local": self.local, "remote": self.remote}

    def ssh_spec(self) -> str:
        """Return an OpenSSH ``-L`` value bound only to the local IPv4 loopback interface."""
        return f"127.0.0.1:{self.local}:127.0.0.1:{self.remote}"

    def summary(self) -> str:
        """Return the compact table representation."""
        return str(self.local) if self.local == self.remote else f"{self.local}→{self.remote}"


def parse_mappings(values: tuple[str, ...]) -> tuple[PortMapping, ...]:
    """Parse mappings and reject duplicate local ports before any socket or SSH side effect."""
    mappings = tuple(PortMapping.parse(value) for value in values)
    duplicates = sorted({mapping.local for mapping in mappings if sum(item.local == mapping.local for item in mappings) > 1})
    if duplicates:
        raise PortForwardError(f"local port(s) repeated in one request: {', '.join(map(str, duplicates))}")
    return mappings


def mapping_argument(value: str) -> bool:
    """Return whether a positional token belongs to the mapping side of the public command grammar, including malformed mapping-like values that should receive a port error."""
    return bool(PORT_SPEC_PATTERN.fullmatch(value)) or value[:1].isdigit() or ":" in value


def mappings_from_state(values: list[dict[str, int]]) -> tuple[PortMapping, ...]:
    """Decode valid persisted mappings while ignoring malformed forward-compatible entries."""
    mappings: list[PortMapping] = []
    for value in values:
        try:
            mapping = PortMapping.from_dict(value)
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= mapping.local <= 65535 and 1 <= mapping.remote <= 65535:
            mappings.append(mapping)
    return tuple(mappings)


def unavailable_local_ports(mappings: tuple[PortMapping, ...]) -> tuple[int, ...]:
    """Return requested loopback ports that cannot be bound, closing every probe socket before returning."""
    unavailable: list[int] = []
    for mapping in mappings:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", mapping.local))
        except OSError:
            unavailable.append(mapping.local)
        finally:
            probe.close()
    return tuple(unavailable)


def control_path(session_name: str) -> Path:
    """Return a deterministic per-session control socket path within the Unix ``sun_path`` budget."""
    directory = PORT_CONTROL_DIR
    if len(str(directory)) + 23 + SOCKET_SUFFIX_BUDGET > SOCKET_PATH_LIMIT:
        directory = Path(tempfile.gettempdir()) / f"fwd-ports-{os.getuid()}"
    readable = directory / f"{session_name}.sock"
    if len(str(readable)) + SOCKET_SUFFIX_BUDGET <= SOCKET_PATH_LIMIT:
        return readable
    digest = hashlib.blake2b(session_name.encode("utf-8"), digest_size=8).hexdigest()
    return directory / f"{digest}.sock"


def _master(endpoint: SSHEndpoint, session_name: str) -> SSHControlMaster:
    """Return the SSH-layer controller for one session's dedicated forwarding master."""
    return SSHControlMaster(endpoint=endpoint, path=control_path(session_name))


def active(endpoint: SSHEndpoint, session_name: str) -> bool:
    """Return whether the session's dedicated forwarding master is alive."""
    return _master(endpoint, session_name).active()


def open_forwards(endpoint: SSHEndpoint, session_name: str, mappings: tuple[PortMapping, ...], *, master_active: bool) -> None:
    """Open all mappings, either on the existing master or in a new persistent background master."""
    master = _master(endpoint, session_name)
    try:
        if master_active:
            master.forward(tuple(mapping.ssh_spec() for mapping in mappings))
        else:
            master.open(tuple(mapping.ssh_spec() for mapping in mappings))
    except SSHError as exc:
        raise PortForwardError(f"SSH could not open the requested port forwarding: {exc}") from exc


def cancel_forwards(endpoint: SSHEndpoint, session_name: str, mappings: tuple[PortMapping, ...], *, check: bool = True) -> None:
    """Cancel selected mappings on a running master, optionally surfacing OpenSSH rejection."""
    try:
        _master(endpoint, session_name).cancel(tuple(mapping.ssh_spec() for mapping in mappings))
    except SSHError as exc:
        if check:
            raise PortForwardError(f"SSH could not close the requested port forwarding: {exc}") from exc


def close(endpoint: SSHEndpoint, session_name: str) -> None:
    """Close the forwarding master, raising instead of forgetting a still-active tunnel."""
    try:
        _master(endpoint, session_name).close()
    except SSHError as exc:
        raise PortForwardError(f"SSH could not close local port forwarding: {exc}") from exc


def summary(endpoint: SSHEndpoint, session_name: str, mappings: tuple[PortMapping, ...], *, master_active: bool | None = None) -> str:
    """Render mappings with an explicit inactive marker when their SSH master has disappeared."""
    if not mappings:
        return "-"
    rendered = ", ".join(mapping.summary() for mapping in mappings)
    is_active = active(endpoint, session_name) if master_active is None else master_active
    return rendered if is_active else f"{rendered} (inactive)"
