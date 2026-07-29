"""Canonical session-list columns and user-facing spellings.

The table renderer, ``--columns`` parser, and shortcut flags share this registry so adding a column cannot silently
give the generic and convenience interfaces different names or ordering.
"""

from __future__ import annotations

LS_COLUMNS = ("name", "backend", "status", "stop after", "running", "tmux", "local dir", "last attached", "ids", "ports")

_ALIASES = {
    "name": "name",
    "names": "name",
    "backend": "backend",
    "backends": "backend",
    "status": "status",
    "statuses": "status",
    "stop-after": "stop after",
    "running": "running",
    "tmux": "tmux",
    "local-dir": "local dir",
    "local-dirs": "local dir",
    "last-attached": "last attached",
    "id": "ids",
    "ids": "ids",
    "port": "ports",
    "ports": "ports",
}


def parse_columns(values: tuple[str, ...]) -> tuple[str, ...]:
    """Parse repeated or comma-separated column names and return them once in canonical table order."""
    requested: set[str] = set()
    unknown: set[str] = set()
    for value in values:
        for raw_name in value.split(","):
            name = raw_name.strip().lower().replace("_", "-").replace(" ", "-")
            if not name:
                continue
            canonical = _ALIASES.get(name)
            if canonical is None:
                unknown.add(raw_name.strip())
            else:
                requested.add(canonical)
    if unknown:
        choices = ", ".join(column.replace(" ", "-") for column in LS_COLUMNS)
        raise ValueError(f"unknown session column(s): {', '.join(sorted(unknown))}; choose from: {choices}")
    if values and not requested:
        raise ValueError("--columns requires at least one column name")
    return tuple(column for column in LS_COLUMNS if column in requested)
