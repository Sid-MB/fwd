"""Reusable shell completion callbacks for fwd's state-aware CLI values."""

from __future__ import annotations

from pathlib import Path

from typer import _click

from fwd.state import SessionState, StateStore


def _session_store() -> StateStore:
    """Return the state store through a seam tests can replace without touching a user's real home directory."""
    return StateStore()


def _session_help(session: SessionState) -> str:
    """Build the concise description shown by shells that support completion help text."""
    target = session.flags.get("target") or "unspecified target"
    directory = Path(session.local_cwd).name or session.local_cwd
    attached = session.last_attached.replace("T", " ")[:16] if session.last_attached else "never attached"
    return f"{session.backend} · target={target} · dir={directory} · last={attached}"


def complete_session(ctx: _click.Context, args: list[str], incomplete: str) -> list[tuple[str, str]]:
    """Complete saved session names with backend/target/path/recency tooltips.

    Completion must never fail the shell or perform provider/network work. Corrupt or temporarily locked state simply
    yields no suggestions; the command itself will still report the real error if the user submits a name.
    """
    del ctx, args
    try:
        sessions = _session_store().all()
    except Exception:
        return []
    matches = (session for session in sessions if session.name.startswith(incomplete))
    return [(session.name, _session_help(session)) for session in sorted(matches, key=lambda session: session.name)]
