"""Tests for ``fwd targets`` — the configured-target group, as distinct from the session commands.

Three properties carry the weight here:

1. Removal is a **config edit only**. It must delete the ``[targets.NAME]`` table, leave surrounding comments and other
   targets intact, never touch session state, and leave ``default_target`` resolvable afterwards.
2. ``fwd setup``, ``fwd targets add``, and ``fwd targets update`` are one implementation. That is asserted on the
   registered callbacks rather than by comparing help text, because the whole point of the refactor is that a new flag
   cannot reach one spelling and miss another.
3. Nothing prompts when no human can answer. Every selector path must fail with an actionable message instead of
   hanging on a picker that will never be read.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import typer

from fwd import cli, config as config_mod, ui, wizard
from fwd.ops import targets as targets_ops

CONFIG = """default_target = "box"
# keep me
[targets.box]
backend = "ssh"
host = "box.example"
user = "sid"
remote_base = "~/work"

[targets.pod]
backend = "runpod"
compute_type = "gpu"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every config reader and the wizard's writer at one throwaway user config file."""
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", path)
    monkeypatch.setattr(wizard, "GLOBAL_CONFIG_PATH", path)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Return a project root with no ``.fwd/config.toml`` of its own."""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _parsed(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend a human terminal is attached so picker and confirmation paths are reachable in tests."""
    monkeypatch.setattr(wizard, "non_interactive_reason", lambda: None)


# --- listing -----------------------------------------------------------------------------------------------------


def test_ls_lists_every_target_and_marks_the_default(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    targets_ops.ls(project_dir=project)
    out = capsys.readouterr().out
    assert "box" in out and "pod" in out
    assert "sid@box.example" in out
    # The default marker belongs to exactly one row, not to the whole table.
    assert out.count("yes") == 1


def test_ls_filters_by_case_insensitive_name_substring(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    targets_ops.ls("PO", project_dir=project)
    out = capsys.readouterr().out
    assert "pod" in out
    assert "box" not in out


def test_ls_explains_an_empty_result_and_points_at_add(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    targets_ops.ls("nothing-matches", project_dir=project)
    captured = capsys.readouterr()
    assert "no configured target name contains" in captured.err
    assert "targets add" in captured.err


# --- inspection --------------------------------------------------------------------------------------------------


def test_info_separates_configured_values_from_defaults(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    targets_ops.info("box", project_dir=project)
    lines = {line.split("|")[1].strip(): line for line in capsys.readouterr().out.splitlines() if line.count("|") >= 3}
    assert "ssh" in lines["backend"]
    assert lines["default target"].split("|")[2].strip() == "yes"
    assert "(default)" not in lines["host"]
    assert "(default)" in lines["port"]
    assert str(home) in lines["declared in"]


def test_configured_values_ignores_the_compute_type_derived_image_default() -> None:
    """A GPU pod that never chose an image must not look like it configured one, while compute_type still counts."""
    target = config_mod.RunpodTargetConfig(name="pod", compute_type="gpu")
    assert targets_ops.configured_values(target) == {"compute_type": "gpu"}


# --- removal -----------------------------------------------------------------------------------------------------


def test_rm_removes_only_the_named_table_and_keeps_comments(home: Path, project: Path) -> None:
    assert targets_ops.remove(("pod",), force=True, project_dir=project) == 1
    text = home.read_text(encoding="utf-8")
    assert "# keep me" in text
    parsed = _parsed(home)
    assert set(parsed["targets"]) == {"box"}
    assert parsed["default_target"] == "box"


def test_rm_retargets_the_default_at_the_sole_survivor(home: Path, project: Path) -> None:
    targets_ops.remove(("box",), force=True, project_dir=project)
    assert _parsed(home)["default_target"] == "pod"


def test_rm_clears_the_default_when_no_survivor_is_obvious(home: Path, project: Path) -> None:
    home.write_text(CONFIG + '\n[targets.hpc]\nbackend = "slurm"\nlogin_host = "login.example"\nuser = "sid"\nremote_base = "/scratch/sid"\n', encoding="utf-8")
    targets_ops.remove(("box",), force=True, project_dir=project)
    parsed = _parsed(home)
    assert "default_target" not in parsed
    assert set(parsed["targets"]) == {"pod", "hpc"}


def test_rm_accepts_several_names(home: Path, project: Path) -> None:
    assert targets_ops.remove(("box", "pod"), force=True, project_dir=project) == 2
    assert "targets" not in _parsed(home)


def test_rm_requires_force_when_nothing_can_confirm(home: Path, project: Path) -> None:
    with pytest.raises(typer.Exit):
        targets_ops.remove(("pod",), project_dir=project)
    assert set(_parsed(home)["targets"]) == {"box", "pod"}


def test_rm_preserves_config_when_the_confirmation_is_declined(home: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _interactive(monkeypatch)
    monkeypatch.setattr(ui, "confirm", lambda *args, **kwargs: False)
    assert targets_ops.remove(("pod",), project_dir=project) == 0
    assert set(_parsed(home)["targets"]) == {"box", "pod"}


def test_rm_mentions_tracked_sessions_and_never_touches_them(home: Path, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Removing a target is a config edit; the confirmation must name the compute that keeps running."""
    from fwd.state import SessionState

    session = SessionState(name="demo", backend="runpod", local_cwd=str(project), remote_dir="/workspace/demo", tmux_session="fwd-demo", endpoint={}, flags={"target": "pod"})
    monkeypatch.setattr(targets_ops, "sessions_for", lambda name: (session,) if name == "pod" else ())
    targets_ops.remove(("pod",), force=True, project_dir=project)
    err = capsys.readouterr().err
    assert "demo" in err and "fwd rm demo" in err


def test_unknown_target_names_the_available_ones(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit):
        targets_ops.remove(("nope",), force=True, project_dir=project)
    err = capsys.readouterr().err
    assert "unknown target 'nope'" in err
    assert "box" in err and "pod" in err


# --- selection ---------------------------------------------------------------------------------------------------


def test_selector_refuses_to_prompt_without_a_terminal(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit):
        targets_ops.info(None, project_dir=project)
    assert "targets ls" in capsys.readouterr().err


def test_picker_resolves_numbers_and_names_and_rejects_the_rest(home: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _interactive(monkeypatch)
    answers = iter(("bogus", "2", "1, pod"))
    monkeypatch.setattr(ui, "ask", lambda label, default="": next(answers))
    config = config_mod.load_config(project)
    assert targets_ops._pick(config, action="update", allow_multiple=False) == ("pod",)
    assert targets_ops._pick(config, action="remove", allow_multiple=True) == ("box", "pod")


def test_picker_rejects_multiple_answers_for_a_single_selection() -> None:
    assert targets_ops._parse_selection("1 2", ["box", "pod"], allow_multiple=False) == ()
    assert targets_ops._parse_selection("1 1", ["box", "pod"], allow_multiple=True) == ("box",)


# --- add / update ------------------------------------------------------------------------------------------------


def test_setup_add_and_update_are_one_implementation() -> None:
    """A new setup flag must reach all three spellings, so they share the callback rather than duplicating it."""
    registered = {command.name: command.callback for command in cli.app.registered_commands if command.name == "setup"}
    group = next(group for group in cli.app.registered_groups if group.name == "targets")
    registered.update({command.name: command.callback for command in group.typer_instance.registered_commands})
    assert registered["setup"] is registered["add"] is registered["update"] is cli._setup
    assert cli.COMMAND_ALIASES["setup"] == ("targets", "add")


def test_update_prompts_default_to_the_targets_current_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefilled prompts are what make update an edit in place; an accepted default must not be re-recorded."""
    prompted: list[tuple[str, object]] = []

    def accept_default(field_name: str, current: object, **kwargs: object) -> object:
        prompted.append((field_name, current))
        return current

    monkeypatch.setattr(wizard, "_prompt_value", accept_default)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: False)
    answers = wizard._prompt_target_values("ssh", None, {"host": "box.example", "remote_base": "~/work"})

    assert answers == {}
    assert ("host", "box.example") in prompted
    assert ("remote_base", "~/work") in prompted


def test_update_merges_flag_overrides_over_existing_values(home: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-interactive editing changes only the named field and needs no flags for already-satisfied required fields."""
    monkeypatch.chdir(project)
    name, backend, existing = targets_ops.prepare_update("box", project_dir=project)
    assert (name, backend) == ("box", "ssh")
    assert existing == {"host": "box.example", "user": "sid", "remote_base": "~/work"}

    wizard.run_wizard(backend=backend, target_name=name, values={"host": "new.example"}, existing_values=existing, force=True)

    stored = _parsed(home)["targets"]["box"]
    assert stored == {"backend": "ssh", "host": "new.example", "user": "sid", "remote_base": "~/work"}
    # Editing one target must not disturb another.
    assert _parsed(home)["targets"]["pod"] == {"backend": "runpod", "compute_type": "gpu"}


def test_update_requires_a_target_when_it_cannot_prompt(home: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit):
        targets_ops.prepare_update(None, project_dir=project)
    assert "targets update" in capsys.readouterr().err
