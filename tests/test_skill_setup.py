"""Tests for the one-time interactive coding-agent skill offer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fwd import cli, completion_setup, skill_setup


def _marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the persistent onboarding marker away from the real user home."""
    marker = tmp_path / ".fwd" / "skill-prompted"
    monkeypatch.setattr(skill_setup, "SKILL_PROMPT_PATH", marker)
    return marker


def test_offer_installs_through_npx_and_records_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(skill_setup.ui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(skill_setup.shutil, "which", lambda executable: "/usr/local/bin/npx" if executable == "npx" else None)
    monkeypatch.setattr(skill_setup.subprocess, "run", lambda command, check: (calls.append((command, check)) or SimpleNamespace(returncode=0)))

    skill_setup.offer_once()

    assert calls == [(["/usr/local/bin/npx", "skills", "add", "Sid-MB/fwd"], False)]
    assert marker.read_text(encoding="utf-8") == "installed\n"


def test_offer_records_decline_and_never_prompts_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(skill_setup.ui, "confirm", lambda prompt, **kwargs: (prompts.append(prompt) or False))
    monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("declining must not invoke npx"))

    skill_setup.offer_once()
    skill_setup.offer_once()

    assert prompts == ["Install the fwd skill for your coding agents with 'npx skills add Sid-MB/fwd'?"]
    assert marker.read_text(encoding="utf-8") == "declined\n"


@pytest.mark.parametrize("failure", ["missing", "exit", "os-error"])
def test_failed_install_is_retryable(failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(skill_setup.ui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(skill_setup.shutil, "which", lambda executable: None if failure == "missing" else "/usr/bin/npx")
    if failure == "exit":
        monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7))
    elif failure == "os-error":
        monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unavailable")))

    skill_setup.offer_once()

    assert not marker.exists()


def test_root_callback_offers_completion_then_skill_only_for_normal_interactive_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[str] = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(completion_setup, "offer_once", lambda: offered.append("completion"))
    monkeypatch.setattr(skill_setup, "offer_once", lambda: offered.append("skill"))
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    cli.main(SimpleNamespace(resilient_parsing=True, invoked_subcommand="info"))
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    assert offered == ["completion", "skill"]
