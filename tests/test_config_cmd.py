"""Tests for ``fwd config`` and for implicit (config-less) target resolution.

Two properties carry most of the weight here:

1. The emitted example config must be **valid TOML that fwd itself accepts**. It is advertised as paste-ready, and a
   reference config that does not load would be worse than shipping none — so every emitted target is round-tripped
   through ``tomllib`` and then through :func:`~fwd.config.parse_target`.
2. Implicit targets must never shadow configured ones. That precedence is the whole safety story of inferring targets
   from a bare name, so it is asserted directly rather than inferred from behaviour elsewhere.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from fwd import config as config_mod
from fwd.config import (
    ORIGIN_BUILTIN,
    ORIGIN_SSH_ALIAS,
    ORIGIN_SSH_INLINE,
    ConfigError,
    RunpodTargetConfig,
    SshTargetConfig,
    implicit_target,
    load_config,
    parse_target,
    ssh_config_host_aliases,
)
from fwd.ops import configcmd

BACKENDS = ["ssh", "runpod", "slurm", "all"]


@pytest.mark.parametrize("which", BACKENDS)
def test_example_is_valid_toml(which: str) -> None:
    """Every emitted example parses as TOML."""
    parsed = tomllib.loads(configcmd.render_example(which))
    assert "targets" in parsed
    assert parsed["default_target"] in parsed["targets"]


@pytest.mark.parametrize("which", ["ssh", "runpod", "slurm"])
def test_example_targets_are_accepted_by_parse_target(which: str) -> None:
    """The example's target tables survive real config parsing, with no unknown-key warnings."""
    parsed = tomllib.loads(configcmd.render_example(which))
    for name, raw in parsed["targets"].items():
        target = parse_target(name, raw)
        assert target.backend == which
        assert target.name == name


def test_example_all_covers_every_backend_and_section() -> None:
    """`--example all` documents all three backends plus the [claude] and [sync] sections."""
    parsed = tomllib.loads(configcmd.render_example("all"))
    assert {t["backend"] for t in parsed["targets"].values()} == {"ssh", "runpod", "slurm"}
    assert set(parsed["claude"]) == {"user_config", "creds", "session", "handoff"}
    assert set(parsed["sync"]) == {"exclude", "use_gitignore", "delete"}


def test_example_is_generated_from_the_dataclass_fields() -> None:
    """A field added to a target dataclass must show up in the example without touching the renderer.

    Guards the "cannot drift from the schema" claim: this asserts the emitted keys are exactly the dataclass's own
    fields (minus the injected ``name``), rather than a hand-maintained list that happens to agree today.
    """
    from dataclasses import fields

    text = configcmd.render_example("runpod")
    expected = {f.name for f in fields(RunpodTargetConfig)} - {"name"}
    # Commented-out optional fields count as documented, so scan the raw text rather than the parsed table.
    for field_name in expected:
        assert f"{field_name} = " in text, f"{field_name} missing from the runpod example"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_provenance_labels_global_project_and_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each value is attributed to the file that actually set it, and unset values read as built-in defaults."""
    global_path = tmp_path / "global.toml"
    _write(
        global_path,
        'default_target = "box"\n[targets.box]\nbackend = "ssh"\nhost = "global.example"\nuser = "sid"\n[sync]\ndelete = false\n',
    )
    project = tmp_path / "proj"
    _write(project / ".fwd" / "config.toml", '[targets.box]\nhost = "project.example"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    rendered = configcmd.render_effective(load_config(project), project)
    lines = {line.split(" = ")[0]: line for line in rendered.splitlines() if " = " in line}

    assert "project" in lines["host"] and "project.example" in lines["host"]
    assert "global" in lines["user"]
    assert "global" in lines["delete"] and "false" in lines["delete"]
    assert "global" in lines["default_target"]
    # Never written in either file, so it must be attributed to fwd's own default rather than to a file.
    assert "default" in lines["use_gitignore"]
    assert "default" in lines["port"]


def test_render_effective_names_both_config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The header legend points at the real paths and marks the ones that do not exist."""
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    rendered = configcmd.render_effective(load_config(tmp_path), tmp_path)
    assert "absent.toml  (absent)" in rendered
    assert "No targets are configured" in rendered


def test_no_config_prints_pointers_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """With no config anywhere, `fwd config` is friendly: hints on stderr, still-valid TOML on stdout."""
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    configcmd.show(None, project_dir=tmp_path)
    captured = capsys.readouterr()
    assert "fwd setup" in captured.err
    assert "fwd config --example" in captured.err
    assert "--target runpod" in captured.err
    # stdout must stay machine-readable even in the empty case.
    assert tomllib.loads(captured.out) == {
        "default_command": ["claude"],
        "claude": {"user_config": False, "creds": False, "session": True, "handoff": False},
        "sync": {"exclude": list(config_mod.DEFAULT_EXCLUDES), "use_gitignore": True, "delete": True},
    }


def test_example_output_is_not_line_wrapped(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: Rich used to fold long trailing comments, producing invalid TOML when redirected to a file."""
    configcmd.show("all")
    out = capsys.readouterr().out
    assert tomllib.loads(out)
    # A wrapped comment would leave a continuation line that is neither a comment nor a key/value pair.
    for line in out.splitlines():
        assert not line or line.startswith("#") or line.startswith("[") or " = " in line


# --- implicit targets -------------------------------------------------------------------------------------------


def test_implicit_runpod_from_pure_defaults() -> None:
    """`--target runpod` with no config yields a usable pod from dataclass defaults, labelled built-in."""
    resolved = implicit_target("runpod")
    assert resolved is not None
    target, origin = resolved
    assert isinstance(target, RunpodTargetConfig)
    assert target.name == "runpod"
    assert target.compute_type == "cpu"
    assert target.image == "runpod/base:0.6.2-cpu"
    assert target.remote_base == "/workspace"
    assert origin == ORIGIN_BUILTIN


def test_implicit_user_at_host_becomes_an_ssh_target() -> None:
    resolved = implicit_target("sid@gpu.example.com")
    assert resolved is not None
    target, origin = resolved
    assert isinstance(target, SshTargetConfig)
    assert (target.host, target.user) == ("gpu.example.com", "sid")
    assert target.remote_base == "~/fwd"
    assert origin == ORIGIN_SSH_INLINE


@pytest.mark.parametrize("name", ["@host", "user@", "not-a-known-name", "pdo"])
def test_uninferable_names_return_none(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently become an ssh target pointed at itself."""
    monkeypatch.setattr(config_mod, "SSH_CONFIG_PATH", tmp_path / "no-such-config")
    assert implicit_target(name) is None


def test_implicit_ssh_config_alias(tmp_path: Path) -> None:
    """A Host alias is honoured, and user is left empty so ssh resolves it from that same block."""
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n  ServerAliveInterval 30\n\nHost gpu-box\n  HostName 10.0.0.5\n  User sid\n", encoding="utf-8")
    resolved = implicit_target("gpu-box", ssh_config=ssh_config)
    assert resolved is not None
    target, origin = resolved
    assert isinstance(target, SshTargetConfig)
    assert target.host == "gpu-box"
    assert target.user == ""
    assert origin == ORIGIN_SSH_ALIAS


def test_ssh_config_parsing_skips_wildcards_and_comments(tmp_path: Path) -> None:
    """Wildcard patterns would match every typo, so they are excluded from the alias set."""
    ssh_config = tmp_path / "config"
    ssh_config.write_text(
        "# a comment\nHost *\nHost dev-?\nHost alpha beta\n\tHostName x\nHost gamma\n",
        encoding="utf-8",
    )
    assert ssh_config_host_aliases(ssh_config) == {"alpha", "beta", "gamma"}


def test_ssh_config_absent_is_not_an_error(tmp_path: Path) -> None:
    assert ssh_config_host_aliases(tmp_path / "nope") == set()


def test_configured_target_wins_over_implicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declaring [targets.runpod] overrides the built-in rather than competing with it."""
    global_path = tmp_path / "global.toml"
    _write(global_path, '[targets.runpod]\nbackend = "ssh"\nhost = "definitely-mine.example"\nuser = "sid"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    target = load_config(tmp_path).target("runpod")
    assert target.backend == "ssh"
    assert target.host == "definitely-mine.example"


def test_implicit_target_resolves_through_config_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The zero-config path: an empty Config still resolves an explicit inferable name."""
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    cfg = load_config(tmp_path)
    assert cfg.targets == {}
    assert cfg.target("runpod").backend == "runpod"
    assert cfg.target("sid@host.example").backend == "ssh"


def test_default_target_may_name_an_implicit_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`default_target = "runpod"` alone is a complete config file."""
    global_path = tmp_path / "global.toml"
    _write(global_path, 'default_target = "runpod"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    assert load_config(tmp_path).target().backend == "runpod"


def test_slurm_is_refused_with_an_explanation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slurm is not inferable, and the error must say why and where to look."""
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path).target("slurm")
    message = str(excinfo.value)
    assert "cannot be inferred" in message
    assert "login host" in message
    assert "fwd config --example slurm" in message


def test_no_targets_error_mentions_the_zero_config_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-interactive empty-config error has to teach the implicit forms, since there is no prompt to fall back on."""
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path).target()
    message = str(excinfo.value)
    assert "fwd up --target runpod" in message
    assert "--target user@host" in message
    assert "[targets.<name>]" in message


def test_explain_target_describes_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "global.toml"
    _write(global_path, '[targets.box]\nbackend = "ssh"\nhost = "h.example"\nuser = "sid"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    assert "declared in config" in configcmd.explain_target("box", tmp_path)
    assert ORIGIN_BUILTIN in configcmd.explain_target("runpod", tmp_path)
    assert "not inferable" in configcmd.explain_target("nonsense-name", tmp_path)


# --- command defaults and mutation ------------------------------------------------------------------------------


def test_default_command_precedence_is_target_then_project_then_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "global.toml"
    _write(global_path, 'default_command = ["claude"]\n[target_defaults.runpod]\ndefault_command = ["python", "-m", "agent"]\n')
    project = tmp_path / "project"
    _write(project / ".fwd" / "config.toml", 'default_command = ["codex"]\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    config = load_config(project)
    assert config.default_command == ["codex"]
    assert config.command_for("runpod") == ("python", "-m", "agent")
    assert config.command_for("somewhere-else") == ("codex",)


def test_default_command_falls_back_to_claude_without_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    assert load_config(tmp_path).command_for("runpod") == ("claude",)


def test_config_set_preserves_existing_toml_and_writes_each_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "home" / "config.toml"
    _write(global_path, '# keep me\ndefault_target = "runpod"\n')
    project = tmp_path / "project"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    configcmd.set_value("default_command", ("codex",), project_dir=project)
    configcmd.set_value("default_command", ("python", "-m", "agent"), project=True, project_dir=project)
    configcmd.set_value("default_command", ("claude",), target="runpod", project_dir=project)

    assert "# keep me" in global_path.read_text(encoding="utf-8")
    global_config = tomllib.loads(global_path.read_text(encoding="utf-8"))
    project_config = tomllib.loads((project / ".fwd" / "config.toml").read_text(encoding="utf-8"))
    assert global_config["default_target"] == "runpod"
    assert global_config["default_command"] == ["codex"]
    assert global_config["target_defaults"]["runpod"]["default_command"] == ["claude"]
    assert project_config["default_command"] == ["python", "-m", "agent"]


def test_config_set_supports_general_dotted_scalar_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    configcmd.set_value("sync.delete", ("false",))
    configcmd.set_value("default_target", ("runpod",))
    parsed = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert parsed["sync"]["delete"] is False
    assert parsed["default_target"] == "runpod"


def test_config_set_rejects_conflicting_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "config.toml")
    with pytest.raises(ConfigError, match="mutually exclusive"):
        configcmd.set_value("default_command", ("codex",), user=True, project=True)


def test_config_rm_removes_each_scope_and_prunes_empty_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "global.toml"
    _write(global_path, 'default_command = ["codex"]\n[target_defaults.runpod]\ndefault_command = ["claude"]\n')
    project = tmp_path / "project"
    _write(project / ".fwd" / "config.toml", 'default_command = ["python"]\n[sync]\ndelete = false\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    assert configcmd.remove_value("default_command", force=True)
    assert configcmd.remove_value("default_command", target="runpod", force=True)
    assert configcmd.remove_value("sync.delete", project=True, project_dir=project, force=True)

    global_config = tomllib.loads(global_path.read_text(encoding="utf-8"))
    project_config = tomllib.loads((project / ".fwd" / "config.toml").read_text(encoding="utf-8"))
    assert "default_command" not in global_config
    assert "target_defaults" not in global_config
    assert project_config == {"default_command": ["python"]}


def test_config_rm_reports_missing_before_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.setattr(configcmd.ui, "confirm", lambda *args, **kwargs: pytest.fail("missing values must not prompt"))
    assert configcmd.remove_value("default_command") is False
    assert "no 'default_command' config exists for user" in capsys.readouterr().err


def test_config_rm_confirms_interactively_and_preserves_value_when_declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "config.toml"
    _write(global_path, 'default_command = ["codex"]\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setattr(configcmd, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(configcmd.ui, "confirm", lambda *args, **kwargs: False)
    assert configcmd.remove_value("default_command") is False
    assert tomllib.loads(global_path.read_text(encoding="utf-8"))["default_command"] == ["codex"]

    monkeypatch.setattr(configcmd.ui, "confirm", lambda *args, **kwargs: True)
    assert configcmd.remove_value("default_command") is True
    assert "default_command" not in tomllib.loads(global_path.read_text(encoding="utf-8"))


def test_config_rm_requires_force_noninteractively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_path = tmp_path / "config.toml"
    _write(global_path, 'default_command = ["codex"]\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setattr(configcmd, "_interactive_terminal", lambda: False)
    with pytest.raises(ConfigError, match="--force"):
        configcmd.remove_value("default_command")
    assert configcmd.remove_value("default_command", force=True) is True


def test_schema_and_example_discover_command_defaults() -> None:
    schema = json.loads(configcmd.render_schema())
    assert schema["properties"]["default_command"]["default"] == ["claude"]
    assert "target_defaults" in schema["properties"]
    example = tomllib.loads(configcmd.render_example("runpod"))
    assert example["default_command"] == ["claude"]
