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
    monkeypatch.setattr(skill_setup, "LOCAL_SKILL_SOURCE", tmp_path / ".fwd" / "skill-source")
    monkeypatch.setattr(skill_setup, "_current_revision", lambda: "revision-2")
    return marker


def test_offer_installs_through_npx_and_records_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(skill_setup.ui, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(skill_setup.shutil, "which", lambda executable: "/usr/local/bin/npx" if executable == "npx" else None)

    def run(command, check, env):  # noqa: ANN001 - subprocess-shaped test double
        calls.append((command, check))
        assert env["DISABLE_TELEMETRY"] == "1"
        assert "AI_AGENT" not in env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(skill_setup.subprocess, "run", run)

    skill_setup.offer_once()

    assert calls == [
        (
            [
                "/usr/local/bin/npx",
                "skills",
                "add",
                str(tmp_path / ".fwd" / "skill-source"),
                "--global",
                "--agent",
                "codex",
                "claude-code",
                "--skill",
                "fwd",
            ],
            False,
        )
    ]
    assert marker.read_text(encoding="utf-8") == "installed:revision-2\n"
    assert (tmp_path / ".fwd" / "skill-source" / "fwd" / "SKILL.md").is_file()


def test_offer_records_decline_and_never_prompts_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(skill_setup.ui, "confirm", lambda prompt, **kwargs: (prompts.append(prompt) or False))
    monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("declining must not invoke npx"))

    skill_setup.offer_once()
    skill_setup.offer_once()

    assert prompts == [f"Install the {skill_setup.ui.command_accent()} skill for Codex and Claude using {skill_setup.ui.accent('npx skills')}?"]
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
    monkeypatch.setattr(skill_setup, "update_if_needed", lambda: offered.append("update"))
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    cli.main(SimpleNamespace(resilient_parsing=True, invoked_subcommand="info"))
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    cli.main(SimpleNamespace(resilient_parsing=False, invoked_subcommand="info"))
    assert offered == ["completion", "skill", "update"]


def test_update_refreshes_an_accepted_skill_once_per_cli_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("installed:revision-1\n", encoding="utf-8")
    calls: list[list[str]] = []
    messages: list[str] = []
    log_path = tmp_path / "update-log" / "npx-skills.log"
    log_path.parent.mkdir()
    monkeypatch.setattr(skill_setup.shutil, "which", lambda executable: "/usr/bin/npx")
    monkeypatch.setattr(skill_setup, "_new_update_log_path", lambda: log_path)
    monkeypatch.setattr(skill_setup.ui, "ok", messages.append)

    def run(command, check, stdout, stderr, env):  # noqa: ANN001 - subprocess-shaped test double
        calls.append(command)
        assert check is False
        assert stderr is skill_setup.subprocess.STDOUT
        assert env["AI_AGENT"] == "fwd"
        assert env["DISABLE_TELEMETRY"] == "1"
        stdout.write("interactive npx skills output\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(skill_setup.subprocess, "run", run)

    skill_setup.update_if_needed()
    skill_setup.update_if_needed()

    assert calls == [
        [
            "/usr/bin/npx",
            "--yes",
            "skills",
            "add",
            str(tmp_path / ".fwd" / "skill-source"),
            "--global",
            "--agent",
            "codex",
            "claude-code",
            "--skill",
            "fwd",
            "-y",
        ]
    ]
    assert marker.read_text(encoding="utf-8") == "installed:revision-2\n"
    assert log_path.read_text(encoding="utf-8") == "interactive npx skills output\n"
    assert messages == [f"updated the installed fwd coding-agent skill. logs at {log_path}"]


def test_local_skill_source_contains_only_the_agent_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tmp_path / "package"
    (payload / "references").mkdir(parents=True)
    (payload / "agents").mkdir()
    (payload / "src").mkdir()
    (payload / "SKILL.md").write_text("# fwd\n", encoding="utf-8")
    (payload / "references" / "commands.md").write_text("# Commands\n", encoding="utf-8")
    (payload / "skill_agents").mkdir()
    (payload / "skill_agents" / "openai.yaml").write_text("name: fwd\n", encoding="utf-8")
    (payload / "src" / "internal.py").write_text("secret = False\n", encoding="utf-8")
    monkeypatch.setattr(skill_setup, "_payload_root", lambda: payload)
    monkeypatch.setattr(skill_setup, "LOCAL_SKILL_SOURCE", tmp_path / "source")

    source = skill_setup._materialize_skill_source()

    assert source == tmp_path / "source"
    assert (source / "fwd" / "SKILL.md").read_text(encoding="utf-8") == "# fwd\n"
    assert (source / "fwd" / "references" / "commands.md").is_file()
    assert (source / "fwd" / "agents" / "openai.yaml").is_file()
    assert not (source / "fwd" / "src").exists()


@pytest.mark.parametrize("state", ["declined", "installed:revision-2"])
def test_update_skips_declined_or_current_skill(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text(state + "\n", encoding="utf-8")
    monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("current or declined skill must not update"))

    skill_setup.update_if_needed()


@pytest.mark.parametrize("failure", ["missing", "exit", "os-error"])
def test_failed_update_keeps_old_revision_for_retry(failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _marker(tmp_path, monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("installed:revision-1\n", encoding="utf-8")
    monkeypatch.setattr(skill_setup.shutil, "which", lambda executable: None if failure == "missing" else "/usr/bin/npx")
    if failure == "exit":
        monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=9))
    elif failure == "os-error":
        monkeypatch.setattr(skill_setup.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    skill_setup.update_if_needed()

    assert marker.read_text(encoding="utf-8") == "installed:revision-1\n"
