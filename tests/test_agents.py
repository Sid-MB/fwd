"""Tests for magic-agent registration, Codex config safety, and attach defaults."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from fwd import agents, cli, codex_state
from fwd.sshexec import SSHEndpoint


def test_agent_registry_only_resolves_exact_magic_commands() -> None:
    """Arguments make a command ordinary so future CLI flags are never silently consumed by fwd semantics."""
    assert agents.resolve(("claude",)).name == "claude"
    assert agents.resolve(("codex",)).name == "codex"
    assert agents.resolve(("codex", "exec", "pwd")) is None
    assert agents.resolve(("python",)) is None
    assert agents.resolve(()) is None


@pytest.mark.parametrize("command", [("claude",), ("codex",)])
def test_magic_agents_auto_attach_in_human_terminals(monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    assert cli._should_attach(command, attach=False, no_attach=False) is True


@pytest.mark.parametrize("command", [("claude",), ("codex",)])
def test_magic_agents_do_not_auto_attach_noninteractively_or_with_no_attach(monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    assert cli._should_attach(command, attach=False, no_attach=False) is False
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    assert cli._should_attach(command, attach=False, no_attach=True) is False


def test_explicit_attach_still_works_for_an_arbitrary_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    assert cli._should_attach(("python", "train.py"), attach=True, no_attach=False) is True


def test_conflicting_attach_flags_fail() -> None:
    with pytest.raises(typer.Exit):
        cli._should_attach(("codex",), attach=True, no_attach=True)


@pytest.mark.parametrize("agent_variable", ["CLAUDECODE", "CODEX_AGENT"])
def test_agent_environment_disables_interactive_detection(monkeypatch: pytest.MonkeyPatch, agent_variable: str) -> None:
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv(agent_variable, "1")
    assert cli._interactive_terminal() is False


def test_codex_bundle_contains_portable_settings_and_both_skill_locations(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex/rules").mkdir(parents=True)
    (home / ".agents/skills/current").mkdir(parents=True)
    (home / ".codex/skills/legacy").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (home / ".codex/work.config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (home / ".codex/AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    (home / ".codex/rules/default.rules").write_text("allow\n", encoding="utf-8")
    (home / ".agents/skills/current/SKILL.md").write_text("# Current\n", encoding="utf-8")
    (home / ".codex/skills/legacy/SKILL.md").write_text("# Legacy\n", encoding="utf-8")

    bundle, count = codex_state.build_config_bundle(tmp_path / "bundle.tar.gz", home=home)
    with tarfile.open(bundle) as archive:
        names = set(archive.getnames())

    assert count == 6
    assert names == {
        ".codex/config.toml",
        ".codex/work.config.toml",
        ".codex/AGENTS.md",
        ".codex/rules/default.rules",
        ".agents/skills/current/SKILL.md",
        ".codex/skills/legacy/SKILL.md",
    }


def test_codex_bundle_hard_excludes_auth_and_nested_secrets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex/rules").mkdir(parents=True)
    (home / ".agents/skills/demo").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("token", encoding="utf-8")
    (home / ".codex/rules/id_ed25519").write_text("key", encoding="utf-8")
    (home / ".agents/skills/demo/.env.production").write_text("secret", encoding="utf-8")
    (home / ".agents/skills/demo/SKILL.md").write_text("safe", encoding="utf-8")

    bundle, count = codex_state.build_config_bundle(tmp_path / "bundle.tar.gz", home=home)
    with tarfile.open(bundle) as archive:
        names = set(archive.getnames())

    assert count == 1
    assert names == {".agents/skills/demo/SKILL.md"}


def test_codex_upload_streams_archive_to_remote_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text("model = 'x'\n", encoding="utf-8")
    monkeypatch.setattr(codex_state.Path, "home", lambda: home)
    monkeypatch.setattr(codex_state, "AGENT_SKILL_ROOTS", ())
    captured: dict[str, object] = {}

    def fake_run(argv, *, stdin, capture_output, timeout):  # noqa: ANN001 - subprocess-shaped test double
        captured["argv"] = argv
        captured["archive"] = stdin.read()
        captured["timeout"] = timeout
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(codex_state.subprocess, "run", fake_run)
    codex_state.upload_user_config(SSHEndpoint(host="remote", user="dev"))

    assert captured["argv"][-1] == 'umask 077; mkdir -p "$HOME"; tar -xzf - -C "$HOME"'
    assert captured["timeout"] == 120
    with tarfile.open(fileobj=io.BytesIO(captured["archive"]), mode="r:gz") as archive:
        assert archive.getnames() == [".codex/config.toml"]
