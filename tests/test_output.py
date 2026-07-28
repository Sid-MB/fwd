"""Tests for semantic output elements and format-independent rendering."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from rich.console import Console

from fwd import ui
from fwd.output import OutputFormat, RecordElement, TableElement, render, resolve_format


def test_markdown_table_escapes_delimiters_and_newlines() -> None:
    stream = StringIO()
    render(TableElement("sessions", ("name", "detail"), (("demo", "a|b\nc"),)), output_format=OutputFormat.markdown, stream=stream)
    assert stream.getvalue() == "## sessions\n\n| name | detail |\n| --- | --- |\n| demo | a\\|b<br>c |\n"


def test_json_table_is_an_array_of_named_records() -> None:
    stream = StringIO()
    render(TableElement("sessions", ("name", "status"), (("demo", "running"), ("old", "stopped"))), output_format=OutputFormat.json, stream=stream)
    assert json.loads(stream.getvalue()) == {
        "type": "table",
        "title": "sessions",
        "columns": ["name", "status"],
        "rows": [{"name": "demo", "status": "running"}, {"name": "old", "status": "stopped"}],
    }


def test_json_record_is_a_named_object() -> None:
    stream = StringIO()
    render(RecordElement("info", (("version", "1.2.3"), ("path", "/tmp/x"))), output_format="json", stream=stream)
    assert json.loads(stream.getvalue())["fields"] == {"version": "1.2.3", "path": "/tmp/x"}


def test_rich_renderer_uses_the_same_element() -> None:
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=100)
    render(TableElement("sessions", ("name",), (("demo",),)), output_format="rich", console=console, stream=stream)
    assert "sessions" in stream.getvalue()
    assert "demo" in stream.getvalue()


def test_auto_uses_markdown_for_pipes_and_agent_environments(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_format("auto", terminal=False) is OutputFormat.markdown
    assert resolve_format("auto", terminal=True) is OutputFormat.rich
    monkeypatch.setenv("CODEX_AGENT", "1")
    assert resolve_format("auto", terminal=True) is OutputFormat.markdown


def test_table_rejects_rows_with_the_wrong_shape() -> None:
    with pytest.raises(ValueError, match="expected 2 cells"):
        TableElement("bad", ("a", "b"), (("only one",),))


def test_code_fragments_are_colored_interactively_and_backtick_fenced_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "_tty", lambda: False)
    assert ui.code("fwd ls") == "`fwd ls`"
    assert ui.code("echo `x`") == "`` echo `x` ``"

    monkeypatch.setattr(ui, "_tty", lambda: True)
    styled = ui.code("fwd ls")
    assert "\x1b[" in styled
    assert "fwd ls" in styled
    assert "`" not in styled


def test_code_examples_render_as_one_compact_line_in_terminal_and_machine_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    terminal_stream = StringIO()
    monkeypatch.setattr(ui, "err_console", Console(file=terminal_stream, force_terminal=True, color_system="standard", width=100))
    monkeypatch.setattr(ui, "_tty", lambda: True)
    ui.show_code_examples(("fwd ls", "fwd stop demo"))
    terminal_output = terminal_stream.getvalue()
    assert "\x1b[" in terminal_output
    assert "\x1b\x1b[" not in terminal_output
    assert terminal_output.count("\n") == 1
    assert " | " in terminal_output
    assert "`fwd ls`" not in terminal_output

    machine_stream = StringIO()
    monkeypatch.setattr(ui, "err_console", Console(file=machine_stream, force_terminal=False, width=100))
    monkeypatch.setattr(ui, "_tty", lambda: False)
    ui.show_code_examples(("fwd ls", "fwd stop demo"))
    machine_output = machine_stream.getvalue()
    assert machine_output.count("\n") == 1
    assert "`fwd ls` | `fwd stop demo`" in machine_output
