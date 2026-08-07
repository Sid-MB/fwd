"""Local OpenSSH key-pair discovery shared by provider setup and validation.

Cloud providers register only public keys, while fwd must retain the corresponding local private-key path for later non-interactive SSH probes. Matching on the algorithm plus base64 key blob avoids relying on filenames or comments, neither of which providers preserve consistently.
"""

from __future__ import annotations

import base64
import binascii
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SSHKeySafetyError(ValueError):
    """Raised without sensitive detail when material is unsafe for public-key handling or network egress."""


_PRIVATE_KEY_MARKERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "PuTTY-User-Key-File-2:",
    "PuTTY-User-Key-File-3:",
    "Private-Lines:",
)


@dataclass(frozen=True, slots=True)
class LocalSSHKey:
    """One local private key and the public value that fwd can safely register with a provider."""

    private_path: Path
    public_key: str
    identity: str
    label: str
    public_path: Path | None = None


def contains_private_key_material(value: Any) -> bool:
    """Return whether a nested value contains a recognizable private-key block or private-key payload field."""
    if isinstance(value, str):
        return any(marker in value for marker in _PRIVATE_KEY_MARKERS)
    if isinstance(value, (bytes, bytearray, memoryview)):
        material = bytes(value)
        return any(marker.encode("ascii") in material for marker in _PRIVATE_KEY_MARKERS)
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in {"private_key", "privatekey", "private_key_material"} and item is not None and item != "":
                return True
            if contains_private_key_material(item):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_private_key_material(item) for item in value)
    return False


def assert_no_private_key_material(value: Any) -> None:
    """Reject private-key material with an intentionally generic error that never repeats the sensitive input."""
    if contains_private_key_material(value):
        raise SSHKeySafetyError("refusing operation because private SSH key material must never leave the local machine")


def _parsed_public_key(public_key: str) -> tuple[str, str] | None:
    """Return normalized text and stable identity only for a structurally valid one-line OpenSSH public key."""
    if not isinstance(public_key, str) or contains_private_key_material(public_key):
        return None
    normalized = public_key.strip()
    if not normalized or "\n" in normalized or "\r" in normalized or len(normalized) > 16384:
        return None
    parts = normalized.split()
    if len(parts) < 2 or not (parts[0].startswith("ssh-") or parts[0].startswith("ecdsa-") or parts[0].startswith("sk-")):
        return None
    encoded = parts[1]
    try:
        blob = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 4:
        return None
    algorithm_length = int.from_bytes(blob[:4], "big")
    algorithm_end = 4 + algorithm_length
    try:
        algorithm = parts[0].encode("ascii")
    except UnicodeEncodeError:
        return None
    if algorithm_end > len(blob) or blob[4:algorithm_end] != algorithm:
        return None
    return normalized, f"{parts[0]} {parts[1]}"


def validated_public_key(public_key: str) -> str:
    """Return only a validated public algorithm and blob, discarding comments and rejecting without echoing input."""
    parsed = _parsed_public_key(public_key)
    if parsed is None:
        raise SSHKeySafetyError("refusing SSH-key upload because the value is not one valid OpenSSH public key; private keys are never accepted")
    return parsed[1]


def public_key_identity(public_key: str) -> str | None:
    """Return the stable OpenSSH algorithm-and-blob identity, excluding its optional comment."""
    parsed = _parsed_public_key(public_key)
    return parsed[1] if parsed is not None else None


def _read_public_key(public_path: Path) -> str | None:
    """Read and validate a small OpenSSH public-key file without accepting arbitrary file contents."""
    try:
        public_key = public_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if len(public_key) > 4096 or public_key_identity(public_key) is None:
        return None
    return public_key


def _derive_public_key(private_path: Path) -> str | None:
    """Derive an OpenSSH public key from an unencrypted private key without reading its material into fwd."""
    try:
        completed = subprocess.run(
            ("ssh-keygen", "-y", "-f", str(private_path)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    public_key = completed.stdout.strip()
    if completed.returncode != 0 or len(public_key) > 4096 or public_key_identity(public_key) is None:
        return None
    return public_key


def _agent_public_keys() -> tuple[str, ...]:
    """Return public identities currently exposed by ``ssh-agent``; an absent or locked agent is an empty inventory."""
    try:
        completed = subprocess.run(("ssh-add", "-L"), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(line.strip() for line in completed.stdout.splitlines() if public_key_identity(line.strip()) is not None)


def _comment_path(public_key: str) -> Path | None:
    """Return an agent key's comment as a path only when the entire comment names a real private-key file."""
    comment = " ".join(public_key.split()[2:]).strip()
    if len(comment) >= 2 and comment[0] == comment[-1] and comment[0] in "\"'":
        comment = comment[1:-1]
    if not comment or comment.startswith("|"):
        return None
    path = Path(comment).expanduser()
    return path if path.is_file() else None


def _local_key(private_path: Path, public_key: str, public_path: Path | None, agent_identities: frozenset[str]) -> LocalSSHKey:
    """Build the shared key record and a concise menu label from validated key material."""
    parts = public_key.split()
    comment = " ".join(parts[2:])
    source = str(public_path) if public_path is not None else f"{private_path} (public key derived)"
    identity = f"{parts[0]} {parts[1]}"
    agent_status = "; loaded in ssh-agent" if identity in agent_identities else ""
    label = f"{parts[0]} {comment or source}{agent_status}"
    return LocalSSHKey(private_path=private_path, public_key=public_key, identity=identity, label=label, public_path=public_path)


def local_key_pairs(directory: Path | None = None) -> tuple[LocalSSHKey, ...]:
    """Discover file-backed SSH identities using public files, ``ssh-keygen``, and the active SSH agent.

    Every valid private-key file directly under ``~/.ssh`` is eligible regardless of its filename or extension. ``ssh-keygen -y`` validates candidates and derives missing public halves without exposing private material to fwd. ``ssh-add -L`` annotates identities that are currently loaded and may contribute a file outside ``~/.ssh`` only when its comment is an existing path whose derived public key cryptographically matches the agent key. Agent-only identities are intentionally excluded because OpenSSH cannot export their private path for fwd to save. Password-protected keys remain selectable when an adjacent ``.pub`` exists because deriving them would require an interactive passphrase prompt inside setup.
    """
    root = directory or Path.home() / ".ssh"
    agent_public_keys = _agent_public_keys() if directory is None else ()
    agent_identities = frozenset(identity for public_key in agent_public_keys if (identity := public_key_identity(public_key)) is not None)
    keys: list[LocalSSHKey] = []
    private_paths: set[Path] = set()
    for public_path in sorted(root.glob("*.pub")):
        private_path = public_path.with_suffix("")
        if not private_path.is_file():
            continue
        public_key = _read_public_key(public_path)
        if public_key is None:
            continue
        keys.append(_local_key(private_path, public_key, public_path, agent_identities))
        private_paths.add(private_path)
    try:
        candidate_paths = sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() != ".pub")
    except OSError:
        candidate_paths = []
    for agent_public_key in agent_public_keys:
        comment_path = _comment_path(agent_public_key)
        if comment_path is not None and comment_path.parent != root and comment_path not in candidate_paths:
            candidate_paths.append(comment_path)
    for private_path in candidate_paths:
        if private_path in private_paths:
            continue
        public_key = _derive_public_key(private_path)
        if public_key is not None:
            identity = public_key_identity(public_key)
            agent_comment_matches = private_path.parent == root or any(_comment_path(agent_key) == private_path and public_key_identity(agent_key) == identity for agent_key in agent_public_keys)
            if agent_comment_matches:
                keys.append(_local_key(private_path, public_key, None, agent_identities))
                private_paths.add(private_path)
    return tuple(keys)


def matching_private_key(provider_public_key: str, local_keys: tuple[LocalSSHKey, ...] | None = None) -> Path | None:
    """Return the local private key whose public identity equals one provider-registered public key."""
    identity = public_key_identity(provider_public_key)
    if identity is None:
        return None
    keys = local_key_pairs() if local_keys is None else local_keys
    return next((key.private_path for key in keys if key.identity == identity), None)


def private_key_matches_public(private_path: str | Path, provider_public_key: str) -> bool:
    """Return whether a private key's adjacent or locally derived public value matches the provider key."""
    private = Path(private_path).expanduser()
    public = Path(f"{private}.pub")
    if not private.is_file():
        return False
    public_key = _read_public_key(public) if public.is_file() else _derive_public_key(private)
    local_identity = public_key_identity(public_key) if public_key is not None else None
    return local_identity is not None and local_identity == public_key_identity(provider_public_key)
