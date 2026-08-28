"""Throttled discovery hints — ``~/.fwd/tips.json``.

Design intent
-------------
fwd has features a user will never find unless something mentions them at the moment they are relevant: continuous
sync is the first, and the natural moment to mention it is right after a manual ``fwd pull``. A hint printed on *every*
pull would stop being a hint and start being noise, so each one is keyed and rate-limited, and the last-shown timestamp
is the only thing persisted.

The file is deliberately trivial and disposable. It records ``{tip_key: iso_timestamp}`` and nothing else, it is
written atomically like :mod:`fwd.state` so a crash mid-write cannot corrupt it, and every failure — unreadable,
unwritable, malformed — degrades to "show the tip" rather than raising. Losing throttle state costs one extra hint;
raising from a hint would break the command the user actually ran.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

TIPS_PATH = Path.home() / ".fwd" / "tips.json"

# One day is long enough that a hint never repeats within a working session, short enough that someone who ignored it
# once still meets it again next week.
DEFAULT_INTERVAL = timedelta(hours=24)

# Key for the hint suggesting continuous sync after a manual pull. Named rather than inlined so the throttle record and
# the call site cannot drift apart.
CONTINUOUS_SYNC = "continuous-sync"


def _read() -> dict[str, str]:
    """Return the recorded timestamps, treating any unreadable or malformed file as empty."""
    try:
        payload = json.loads(TIPS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}


def _write(records: dict[str, str]) -> None:
    """Atomically replace the throttle file, silently giving up if the home directory is not writable."""
    try:
        TIPS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = TIPS_PATH.with_name(f".{TIPS_PATH.name}.tmp")
        temporary.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, TIPS_PATH)
    except OSError:
        pass


def should_show(key: str, *, interval: timedelta = DEFAULT_INTERVAL, now: datetime | None = None) -> bool:
    """Return whether a hint is due, without recording anything.

    Separate from :func:`mark_shown` so a caller can decide it will not print after all — for instance because the
    transport cannot support the feature being suggested — without burning the interval.
    """
    recorded = _read().get(key)
    if recorded is None:
        return True
    try:
        last = datetime.fromisoformat(recorded)
    except ValueError:
        return True
    last = last.replace(tzinfo=UTC) if last.tzinfo is None else last
    return (now or datetime.now(UTC)) - last >= interval


def mark_shown(key: str, *, now: datetime | None = None) -> None:
    """Record that a hint was just displayed, starting its throttle interval."""
    records = _read()
    records[key] = (now or datetime.now(UTC)).isoformat()
    _write(records)
