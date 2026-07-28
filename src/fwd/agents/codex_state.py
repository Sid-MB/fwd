"""Safe transfer of portable Codex configuration to a remote machine.

Only documented, portable settings are allowlisted. Authentication is intentionally absent: ``~/.codex/auth.json``
contains bearer credentials and must be treated like a password, so users authenticate the remote Codex installation
separately. Extraction merges files into the remote home and never deletes remote-only configuration.
"""

from __future__ import annotations

import fnmatch
import subprocess
import tarfile
import tempfile
from pathlib import Path

from fwd import ui
from fwd.sshexec import SSHEndpoint

SENSITIVE_PATTERNS = ("*.pem", "*.key", ".env*", "*.p12", "id_rsa*", "id_ed25519*", "auth.json")
CODEX_ENTRIES = ("config.toml", "AGENTS.md", "rules")
AGENT_SKILL_ROOTS = ((".agents/skills", Path.home() / ".agents" / "skills"), (".codex/skills", Path.home() / ".codex" / "skills"))


def _safe(path: Path) -> bool:
    """Reject secrets by checking every path component, including files nested inside skills."""
    return not any(fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in SENSITIVE_PATTERNS)


def build_config_bundle(destination: Path, *, home: Path | None = None) -> tuple[Path, int]:
    """Build an allowlisted archive containing Codex config, profiles, rules, and skills."""
    user_home = home or Path.home()
    codex_home = user_home / ".codex"
    sources: list[tuple[str, Path]] = [(f".codex/{name}", codex_home / name) for name in CODEX_ENTRIES]
    sources.extend((f".codex/{profile.name}", profile) for profile in sorted(codex_home.glob("*.config.toml")))
    if home is None:
        sources.extend(AGENT_SKILL_ROOTS)
    else:
        sources.extend((arcname, user_home / arcname) for arcname, _ in AGENT_SKILL_ROOTS)

    count = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for arcname, source in sources:
            if not source.exists():
                continue
            paths = [source] if source.is_file() else [path for path in sorted(source.rglob("*")) if path.is_file()]
            for path in paths:
                relative = Path(arcname) if source.is_file() else Path(arcname) / path.relative_to(source)
                if not _safe(relative):
                    continue
                archive.add(path, arcname=str(relative), recursive=False)
                count += 1
    return destination, count


def upload_user_config(endpoint: SSHEndpoint) -> None:
    """Merge portable Codex settings and skills into the remote home without transferring authentication."""
    with tempfile.TemporaryDirectory(prefix="fwd-codex-") as temporary:
        bundle, count = build_config_bundle(Path(temporary) / "codex-config.tar.gz")
        if count == 0:
            ui.warn("No local Codex config or skills found to upload; starting with remote defaults.")
            return
        command = 'umask 077; mkdir -p "$HOME"; tar -xzf - -C "$HOME"'
        try:
            with bundle.open("rb") as payload:
                process = subprocess.run([*endpoint.ssh_argv(), command], stdin=payload, capture_output=True, timeout=120)
            if process.returncode != 0:
                detail = process.stderr.decode(errors="replace").strip()
                raise RuntimeError(detail or f"ssh exited {process.returncode}")
        except Exception as exc:
            ui.warn(f"Codex config upload failed ({exc}); the remote will use its own config.")
            return
    ui.ok(f"Uploaded {count} Codex config/skill file(s); authentication was not copied.")
