"""Best-effort installation of bundled manual pages into the user's local man directory.

Python package installers intentionally do not write outside their isolated environment, so a wheel cannot place
manuals in a global ``share/man`` directory at installation time. fwd instead carries its generated section-1 pages
inside the wheel and synchronizes them to the XDG user data directory on CLI startup. A versioned ownership manifest
keeps unchanged launches cheap and lets upgrades remove pages for commands that no longer exist.

Installation is deliberately silent and non-fatal: read-only homes, unsupported platforms, corrupt manifests, and
unusual man configurations must never prevent the requested fwd command from running. ``fwd uninstall`` calls the
strict removal function separately so cleanup failures can still be reported to the user.
"""

from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from fwd import __version__

MANIFEST_NAME = ".fwdit-man-pages.json"
MAN_PAGE_NAME = re.compile(r"^fwd(?:-[a-z0-9-]+)*\.1$")


def install_directory() -> Path:
    """Return the section-1 directory under the user's XDG data home."""
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    data_home = Path(configured).expanduser() if configured else Path.home() / ".local" / "share"
    if not data_home.is_absolute():
        data_home = Path.home() / ".local" / "share"
    return data_home / "man" / "man1"


def _bundled_pages() -> dict[str, Any]:
    """Return safe page names mapped to wheel resources or the repository directory for editable installs."""
    packaged_root = resources.files("fwd").joinpath("man")
    repository_root = Path(__file__).resolve().parents[2] / "man"
    root = packaged_root if packaged_root.is_dir() else repository_root
    return {page.name: page for page in root.iterdir() if page.is_file() and MAN_PAGE_NAME.fullmatch(page.name)}


def _manifest_files(document: object) -> set[str]:
    """Read only safe fwd page names from an untrusted ownership manifest."""
    if not isinstance(document, dict) or not isinstance(document.get("files"), list):
        return set()
    return {name for name in document["files"] if isinstance(name, str) and MAN_PAGE_NAME.fullmatch(name)}


def _read_manifest(path: Path) -> dict[str, object]:
    """Return the parsed ownership manifest, treating absent or malformed data as an empty prior installation."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace one user-owned manual or manifest with mode 644."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _install() -> None:
    """Synchronize bundled pages and their ownership manifest, raising filesystem/resource errors to the caller."""
    if os.name == "nt":
        return
    pages = _bundled_pages()
    if not pages:
        return
    directory = install_directory()
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / MANIFEST_NAME
    previous = _read_manifest(manifest_path)
    current_names = set(pages)
    if previous.get("version") == __version__ and _manifest_files(previous) == current_names and all((directory / name).is_file() for name in current_names):
        return
    for obsolete in _manifest_files(previous) - current_names:
        (directory / obsolete).unlink(missing_ok=True)
    for name, resource in pages.items():
        content = resource.read_bytes()
        destination = directory / name
        try:
            unchanged = destination.read_bytes() == content
        except OSError:
            unchanged = False
        if not unchanged:
            _atomic_write(destination, content)
    manifest = json.dumps({"version": __version__, "files": sorted(current_names)}, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    _atomic_write(manifest_path, manifest)


def install_silently() -> None:
    """Install or update user-local manuals without delaying or breaking the requested CLI operation on failure."""
    try:
        _install()
    except Exception:
        return


def remove_installed() -> int:
    """Remove fwd-owned pages from the current user man directory and return the number of deleted files."""
    if os.name == "nt":
        return 0
    directory = install_directory()
    manifest_path = directory / MANIFEST_NAME
    previous = _read_manifest(manifest_path)
    names = _manifest_files(previous)
    try:
        names.update(_bundled_pages())
    except Exception:
        pass
    removed = 0
    for name in names:
        path = directory / name
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed += 1
    if manifest_path.is_symlink() or manifest_path.is_file():
        manifest_path.unlink()
        removed += 1
    return removed
