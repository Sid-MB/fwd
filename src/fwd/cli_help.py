"""CLI help customization for compact alias display.

Typer models aliases as separate commands, which normally renders duplicate rows (``attach`` and ``a``). fwd keeps
the alias commands hidden and teaches the root group to annotate the canonical row as ``attach (a)``. Invocation and
completion still use the real command names; only help presentation changes.
"""

from __future__ import annotations

from typing import ClassVar

from typer import _click as click
from typer.core import TyperGroup


class AliasHelpGroup(TyperGroup):
    """Render hidden command aliases beside their canonical command."""

    aliases: ClassVar[dict[str, tuple[str, ...]]] = {"attach": ("a",), "send": ("s",)}

    @classmethod
    def display_name(cls, command_name: str) -> str:
        """Return a canonical command's help label with its aliases, if any."""
        aliases = cls.aliases.get(command_name, ())
        return f"{command_name} ({', '.join(aliases)})" if aliases else command_name

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Plain-Click fallback matching Typer's table while replacing only the displayed label."""
        commands: list[tuple[str, click.Command]] = []
        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            commands.append((command_name, command))
        if not commands:
            return
        labels = [(self.display_name(name), command) for name, command in commands]
        limit = formatter.width - 6 - max(len(label) for label, _ in labels)
        rows = [(label, command.get_short_help_str(limit)) for label, command in labels]
        with formatter.section("Commands"):
            formatter.write_dl(rows)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Temporarily annotate canonical command objects for Typer's Rich command panel.

        Typer's Rich formatter reads ``command.name`` directly, whereas its plain formatter uses registry keys. The
        temporary rename is safe because routing already uses the group's command mapping and names are restored even
        if rendering raises.
        """
        if self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)
        renamed: list[tuple[click.Command, str | None]] = []
        try:
            for canonical in self.aliases:
                command = self.get_command(ctx, canonical)
                if command is None or command.hidden:
                    continue
                renamed.append((command, command.name))
                command.name = self.display_name(canonical)
            super().format_help(ctx, formatter)
        finally:
            for command, original_name in renamed:
                command.name = original_name
