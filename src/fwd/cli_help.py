"""CLI help customization for compact alias display.

Typer models aliases as separate commands, which normally renders duplicate rows (``attach`` and ``a``). fwd keeps
the alias commands hidden and teaches the root group to annotate the canonical row as ``attach (a)``. Invocation and
completion still use the real command names; only help presentation changes.
"""

from __future__ import annotations

from typing import ClassVar

from typer import _click as click
from typer.core import TyperCommand, TyperGroup
from typer._click.shell_completion import CompletionItem


class AliasHelpGroup(TyperGroup):
    """Render hidden command aliases beside their canonical command."""

    aliases: ClassVar[dict[str, tuple[str, ...]]] = {"attach": ("a",), "send": ("s",)}

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve registered commands first, then configured target/backend shorthand commands."""
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        from fwd.ops import target_alias

        if not target_alias.recognized(cmd_name):
            return None
        # Typer vendors a Click API whose base Command is abstract; dynamic commands must use its concrete command
        # implementation just like commands registered through ``app.command`` do.
        return TyperCommand(
            name=cmd_name,
            help=f"Launch the default command on target/backend {cmd_name!r} and attach.",
            callback=lambda: target_alias.forward(cmd_name),
        )

    def shell_complete(self, ctx: click.Context, incomplete: str) -> list[CompletionItem]:
        """Add dynamic target/backend commands to Click's normal root command completion."""
        results = super().shell_complete(ctx, incomplete)
        existing = {item.value for item in results}
        from fwd.ops import target_alias

        results.extend(CompletionItem(value, help=help_text) for value, help_text in target_alias.completion_candidates() if value.startswith(incomplete) and value not in existing)
        return results

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
