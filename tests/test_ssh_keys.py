"""Regression checks for SSH public-key discovery and the private-material egress boundary."""

from __future__ import annotations

import base64

import pytest

from fwd.backends.lambda_cloud import LambdaCloudClient, LambdaCloudError
from fwd.ssh_keys import SSHKeySafetyError, assert_no_private_key_material, contains_private_key_material, validated_public_key


def _synthetic_public_key() -> str:
    """Return a structurally valid public key containing no real credential material."""
    algorithm = b"ssh-ed25519"
    key = bytes(range(32))
    blob = len(algorithm).to_bytes(4, "big") + algorithm + len(key).to_bytes(4, "big") + key
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} test@example"


@pytest.mark.parametrize(
    "private_material",
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 1\nsecret",
    ),
)
def test_private_key_material_is_detected_without_echoing_it(private_material: str) -> None:
    """Common private-key containers must fail with a generic message that cannot itself disclose the key."""
    assert contains_private_key_material(private_material)
    with pytest.raises(SSHKeySafetyError) as exc_info:
        assert_no_private_key_material(private_material)
    assert "secret" not in str(exc_info.value)


def test_public_key_validation_rejects_malformed_and_private_values() -> None:
    """An SSH-looking prefix is insufficient unless the base64 wire blob names the same public algorithm."""
    assert validated_public_key(f"  {_synthetic_public_key()}  ") == " ".join(_synthetic_public_key().split()[:2])
    for unsafe in ("ssh-ed25519 not-base64", f"{_synthetic_public_key()} -----BEGIN PRIVATE KEY-----"):
        with pytest.raises(SSHKeySafetyError):
            validated_public_key(unsafe)
    assert contains_private_key_material(b"-----BEGIN PRIVATE KEY-----\nsecret")


def test_lambda_ssh_key_upload_rejects_private_material_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The high-level provider method must not call even a mocked request function with private material."""
    client = LambdaCloudClient(api_key="test-api-key")
    requests: list[tuple[object, ...]] = []
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: requests.append(args) or {})
    private_material = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
    with pytest.raises(LambdaCloudError, match="private keys are never accepted"):
        client.add_ssh_key("unsafe", private_material)
    assert requests == []


def test_lambda_request_has_a_second_private_material_egress_guard() -> None:
    """Direct use of the generic HTTP method must fail before request construction when callers bypass add_ssh_key."""
    client = LambdaCloudClient(api_key="test-api-key")
    with pytest.raises(LambdaCloudError, match="must never leave the local machine") as exc_info:
        client.request("POST", "/anything", {"private_key": "opaque-secret"})
    assert "opaque-secret" not in str(exc_info.value)


def test_lambda_upload_sends_only_normalized_public_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """The accepted upload payload contains only the public algorithm/blob, stripping even harmless local comments."""
    client = LambdaCloudClient(api_key="test-api-key")
    calls: list[tuple[str, str, dict[str, str]]] = []

    def request(method: str, path: str, payload: dict[str, str]) -> dict[str, str]:
        calls.append((method, path, payload))
        return {"name": payload["name"]}

    monkeypatch.setattr(client, "request", request)
    client.add_ssh_key("safe", f"  {_synthetic_public_key()}  ")
    assert calls == [("POST", "/ssh-keys", {"name": "safe", "public_key": " ".join(_synthetic_public_key().split()[:2])})]
    assert not contains_private_key_material(calls[0][2])
