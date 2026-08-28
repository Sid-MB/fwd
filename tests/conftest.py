"""Shared pytest fixtures for the fwd test suite.

The one job here today is making help-text assertions environment-independent. Rich's Console auto-detects CI providers
and *force-enables* terminal styling when it sees GITHUB_ACTIONS (and honours CI/FORCE_COLOR), so on a GitHub runner
Typer's `--help` output comes back with bold/dim ANSI escapes threaded through the option names. A plain
`assert "--reuse" in result.output` then fails on CI while passing locally, which is the worst possible failure mode:
invisible until it blocks a release. Stripping those variables for every test makes help rendering identical
everywhere.
"""

from __future__ import annotations

import pytest

# Variables that make Rich/Typer force terminal styling on regardless of whether stdout is a tty. GITHUB_ACTIONS and CI are the ones a runner actually sets; FORCE_COLOR is cleared too so a developer exporting it locally sees the same output the suite asserts on.
_FORCE_STYLING_ENV_VARS = ("GITHUB_ACTIONS", "CI", "FORCE_COLOR")


@pytest.fixture(autouse=True)
def _plain_help_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render CLI help without ANSI styling so substring assertions behave the same locally and on CI runners."""
    for name in _FORCE_STYLING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
