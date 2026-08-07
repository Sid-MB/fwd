"""Shared local credential acquisition, private persistence, and transient process storage.

Provider API credentials must not enter fwd's TOML, session state, logs, or command arguments. This module gives setup
flows one consistent alternative when an expected environment variable is absent: a user enters either the secret or
a local text-file path through one hidden prompt. Pasted values are copied into fwd's private credential directory;
entered paths are recorded there as reusable source references. Every value is stripped when read and exposed to
provider clients through the same environment-first resolver.
"""

from __future__ import annotations

import importlib
import os
import re
import tempfile
from pathlib import Path

import typer

_TRANSIENT_SECRETS: dict[str, str] = {}
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_FILE_SUFFIXES = {".env", ".json", ".key", ".pem", ".secret", ".token", ".toml", ".txt"}


class CredentialInputError(ValueError):
    """A prompted secret source was empty, unreadable, or otherwise unusable."""


def set_transient_secret(environment_name: str, value: str) -> None:
    """Retain one stripped secret for this process without modifying the process environment or persistent state."""
    normalized = value.strip()
    if not normalized:
        raise CredentialInputError(f"{environment_name} cannot be empty")
    _TRANSIENT_SECRETS[environment_name] = normalized


def _credentials_dir() -> Path:
    """Return fwd's private provider-credential directory without creating it during read-only resolution."""
    return Path.home() / ".fwd" / "credentials"


def _credential_paths(environment_name: str) -> tuple[Path, Path]:
    """Return the managed secret and reusable source-reference paths for one safe environment-style identifier."""
    if not _ENVIRONMENT_NAME.fullmatch(environment_name):
        raise CredentialInputError(f"invalid credential environment name {environment_name!r}")
    directory = _credentials_dir()
    return directory / f"{environment_name}.secret", directory / f"{environment_name}.path"


def _atomic_private_write(path: Path, value: str) -> None:
    """Atomically replace one credential file with mode 600 inside a mode-700 fwd-owned directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def save_secret(environment_name: str, value: str) -> Path:
    """Copy a pasted secret into fwd-controlled storage and record that managed file as the reusable source."""
    normalized = value.strip()
    if not normalized:
        raise CredentialInputError(f"{environment_name} cannot be empty")
    secret_path, source_path = _credential_paths(environment_name)
    try:
        _atomic_private_write(secret_path, normalized)
        _atomic_private_write(source_path, str(secret_path))
    except OSError as exc:
        raise CredentialInputError(f"could not save {environment_name} under {secret_path.parent}: {exc}") from exc
    return secret_path


def save_secret_path(environment_name: str, path: Path) -> Path:
    """Record an existing user-controlled credential file as the reusable source without copying or modifying it."""
    secret_path, source_path = _credential_paths(environment_name)
    resolved = path.expanduser().resolve()
    _read_secret_file(resolved, environment_name)
    try:
        _atomic_private_write(source_path, str(resolved))
        if secret_path != resolved and secret_path.exists():
            secret_path.unlink()
    except OSError as exc:
        raise CredentialInputError(f"could not save {environment_name} source path under {source_path.parent}: {exc}") from exc
    return resolved


def forget_saved_secret(environment_name: str) -> None:
    """Remove only fwd's managed secret and source reference for one credential, never an external referenced file."""
    secret_path, source_path = _credential_paths(environment_name)
    for path in (source_path, secret_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CredentialInputError(f"could not remove saved {environment_name} credential file {path}: {exc}") from exc


def _read_secret_file(path: Path, label: str) -> str:
    """Read and strip one UTF-8 credential file, rejecting missing, unreadable, invalid, and whitespace-only files."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise CredentialInputError(f"could not read {label} from {path}: {exc}") from exc
    if not value:
        raise CredentialInputError(f"{label} file {path} is empty after stripping whitespace")
    return value


def _saved_secret(environment_name: str) -> str:
    """Resolve a saved source reference and return its current stripped contents, or an empty string when none exists."""
    _, source_path = _credential_paths(environment_name)
    if not source_path.exists():
        return ""
    source = _read_secret_file(source_path, "saved credential source")
    return _read_secret_file(Path(source).expanduser(), environment_name)


def resolve_secret(environment_name: str) -> str:
    """Return a stripped environment, saved-file, or process-only credential in descending precedence order."""
    environment_value = os.environ.get(environment_name, "").strip()
    if environment_value:
        return environment_value
    saved_value = _saved_secret(environment_name)
    return saved_value or _TRANSIENT_SECRETS.get(environment_name, "").strip()


def secret_source(environment_name: str) -> str | None:
    """Describe where a usable secret came from without returning or rendering the secret itself."""
    if os.environ.get(environment_name, "").strip():
        return "environment"
    if _saved_secret(environment_name):
        return "saved"
    if _TRANSIENT_SECRETS.get(environment_name, ""):
        return "transient"
    return None


def _enable_line_editing() -> None:
    """Activate standard terminal key handling before Click enters its hidden credential prompt."""
    try:
        importlib.import_module("readline")
    except ImportError:
        pass


def _looks_like_path(candidate: str, path: Path, *, quoted: bool) -> bool:
    """Recognize explicit syntax, separators, common credential-file suffixes, and existing relative files as paths."""
    return quoted or candidate.startswith(("/", "~", ".")) or "/" in candidate or "\\" in candidate or path.suffix.lower() in _CREDENTIAL_FILE_SUFFIXES or path.exists()


def prompt_secret(label: str, *, environment_name: str) -> str:
    """Acquire and persist a stripped secret or recognize and remember a UTF-8 file path from one hidden prompt.

    Matching single or double quotes around paths are removed. Existing files are recognized even when entered as bare
    relative names. Quoted inputs and values beginning with ``/``, ``~``, ``.``, or containing a path separator are
    treated as paths and report a read error instead of being mistaken for API keys. Pasted secrets are copied to a
    private managed file; entered file paths remain user-controlled and are saved only as source references.
    """
    _enable_line_editing()
    entered = typer.prompt(f"{label} (paste the value or enter a file path; used as {environment_name})", hide_input=True, show_default=False).strip()
    if not entered:
        raise CredentialInputError(f"{label} cannot be empty")
    quoted = entered[0] in {"'", '"'}
    if quoted:
        if len(entered) < 2 or entered[-1] != entered[0]:
            raise CredentialInputError(f"unterminated quoted path for {label}")
        candidate = entered[1:-1]
        if not candidate:
            raise CredentialInputError(f"{label} path cannot be empty")
    else:
        candidate = entered
    path = Path(os.path.expandvars(candidate)).expanduser()
    if not _looks_like_path(candidate, path, quoted=quoted):
        save_secret(environment_name, entered)
        return entered
    resolved = save_secret_path(environment_name, path)
    value = _read_secret_file(resolved, label)
    return value
