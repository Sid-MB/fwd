"""Tests for semantic output elements and format-independent rendering."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from rich.console import Console

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
