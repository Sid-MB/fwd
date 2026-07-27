"""Structured command output and interchangeable renderers.

Commands construct semantic elements instead of formatting strings. Renderers then choose Rich terminal tables,
plain Markdown for agents/pipes, or stable JSON. This keeps provider/command logic independent from presentation and
gives non-interactive callers a useful default without requiring terminal escape-code parsing.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table


class OutputFormat(StrEnum):
    """Supported presentation formats."""

    auto = "auto"
    rich = "rich"
    markdown = "markdown"
    json = "json"


@dataclass(frozen=True, slots=True)
class TableElement:
    """A titled rectangular data set whose rows map directly to columns."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    def __post_init__(self) -> None:
        """Reject malformed rows at construction so every renderer sees the same valid shape."""
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError(f"table {self.title!r}: expected {len(self.columns)} cells, got {len(row)}")


@dataclass(frozen=True, slots=True)
class RecordElement:
    """A named ordered mapping, rendered as a compact key/value view."""

    title: str
    fields: tuple[tuple[str, Any], ...]


OutputElement = TableElement | RecordElement

# Experimental ordered format preference used only by automatic rendering. Unknown names are intentionally ignored so
# callers can share one preference list across tools with different renderer sets.
FORMAT_PREFERENCE_ENV = "FORMATPREF"
FORMAT_PREFERENCE_NAMES = {
    "json": OutputFormat.json,
    "md": OutputFormat.markdown,
    "markdown": OutputFormat.markdown,
    "rich": OutputFormat.rich,
}


def is_machine_environment() -> bool:
    """Return whether an agent marker requests stable non-interactive output despite a possible pseudo-terminal."""
    return any(os.environ.get(name) for name in ("CLAUDECODE", "CODEX_AGENT"))


def preferred_format() -> OutputFormat | None:
    """Return the first supported concrete format in the experimental ordered environment preference."""
    for name in os.environ.get(FORMAT_PREFERENCE_ENV, "").split(","):
        supported = FORMAT_PREFERENCE_NAMES.get(name.strip().lower())
        if supported is not None:
            return supported
    return None


def resolve_format(requested: OutputFormat | str, *, terminal: bool) -> OutputFormat:
    """Resolve ``auto`` to Rich for humans and Markdown for pipes, CI, and agent environments."""
    value = requested if isinstance(requested, OutputFormat) else OutputFormat(requested)
    if value is not OutputFormat.auto:
        return value
    preference = preferred_format()
    if preference is not None:
        return preference
    return OutputFormat.rich if terminal and not is_machine_environment() else OutputFormat.markdown


def _text(value: Any) -> str:
    """Convert a cell to display text while keeping ``None`` explicit and predictable."""
    return "" if value is None else str(value)


def _markdown_cell(value: Any) -> str:
    """Escape Markdown table delimiters and collapse embedded newlines without losing their boundary."""
    return _text(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def _render_markdown(element: OutputElement) -> str:
    if isinstance(element, RecordElement):
        table = TableElement(element.title, ("field", "value"), tuple((key, value) for key, value in element.fields))
        return _render_markdown(table)
    header = "| " + " | ".join(_markdown_cell(column) for column in element.columns) + " |"
    separator = "| " + " | ".join("---" for _ in element.columns) + " |"
    rows = ["| " + " | ".join(_markdown_cell(cell) for cell in row) + " |" for row in element.rows]
    prefix = f"## {element.title}\n\n" if element.title else ""
    return prefix + "\n".join((header, separator, *rows)) + "\n"


def _json_value(element: OutputElement) -> dict[str, Any]:
    if isinstance(element, RecordElement):
        return {"type": "record", "title": element.title, "fields": {key: value for key, value in element.fields}}
    return {
        "type": "table",
        "title": element.title,
        "columns": list(element.columns),
        "rows": [{column: value for column, value in zip(element.columns, row, strict=True)} for row in element.rows],
    }


def render(element: OutputElement, *, output_format: OutputFormat | str = OutputFormat.auto, console: Console | None = None, stream: TextIO | None = None) -> None:
    """Render one structured element in the requested or environment-selected format."""
    destination = stream or sys.stdout
    rich_console = console or Console(file=destination)
    resolved = resolve_format(output_format, terminal=rich_console.is_terminal)
    if resolved is OutputFormat.json:
        destination.write(json.dumps(_json_value(element), indent=2, ensure_ascii=False) + "\n")
        return
    if resolved is OutputFormat.markdown:
        destination.write(_render_markdown(element))
        return
    table = Table(title=element.title, title_justify="left", header_style="bold")
    if isinstance(element, RecordElement):
        table.add_column("field")
        table.add_column("value")
        for key, value in element.fields:
            table.add_row(key, _text(value))
    else:
        for column in element.columns:
            table.add_column(column)
        for row in element.rows:
            table.add_row(*[_text(cell) for cell in row])
    rich_console.print(table)
