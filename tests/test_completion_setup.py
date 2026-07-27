"""Tests for the one-time interactive shell-completion offer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fwd import cli, completion_setup, skill_setup


def _marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    marker = tmp_path / ".fwd" / "completion-prompted"
    monkeypatch.setattr(completion_setup, "COMPLETION_PROMPT_PATH", marker)
    monkeypatch.setattr(completion_setup, "_completion_path", lambda shell: tmp_path / f"completion-{shell}")
    monkeypatch.setattr(completion_setup, "_shell_name", lambda: "zsh")
    return marker


def test_offer_installs_through_typer_and_records_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    installed_path = tmp_path / "_fwd"
    calls: list[tuple[str | None, str | None, str | None]] = []
    monkeypatch.setattr(completion_setup.ui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(completion_setup.completion, "install", lambda shell=None, prog_name=None, complete_var=None: (calls.append((shell, prog_name, complete_var)) or ("zsh", installed_path)))

    completion_setup.offer_once()

    assert calls == [("zsh", "fwd", "_FWD_COMPLETE")]
    assert marker.read_text(encoding="utf-8") == "installed:zsh\n"


def test_offer_records_decline_and_never_prompts_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(completion_setup.ui, "confirm", lambda prompt, **kwargs: (prompts.append(prompt) or False))
    monkeypatch.setattr(completion_setup.completion, "install", lambda **kwargs: pytest.fail("declining must not install"))

    completion_setup.offer_once()
    completion_setup.offer_once()

    assert len(prompts) == 1
    assert marker.read_text(encoding="utf-8") == "declined:zsh\n"


def test_offer_detects_an_existing_completion_before_prompting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    completion_path = tmp_path / "completion-zsh"
    completion_path.write_text("#compdef fwd\n", encoding="utf-8")
    monkeypatch.setattr(completion_setup.ui, "confirm", lambda *args, **kwargs: pytest.fail("installed completion must not prompt"))

    completion_setup.offer_once()

    assert marker.read_text(encoding="utf-8") == "already-installed:zsh\n"


def test_failed_install_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(completion_setup.ui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(completion_setup.completion, "install", lambda **kwargs: (_ for _ in ()).throw(OSError("read-only")))

    completion_setup.offer_once()

    assert not marker.exists()


def test_unwritable_decision_marker_does_not_block_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(completion_setup.ui, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only")))
    completion_setup.offer_once()


def test_root_callback_offers_only_for_normal_interactive_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[str] = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(completion_setup, "offer_once", lambda: offered.append("offer"))
    monkeypatch.setattr(skill_setup, "offer_once", lambda: None)
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    cli.main(SimpleNamespace(resilient_parsing=True, invoked_subcommand="info"))
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    assert offered == ["offer"]
