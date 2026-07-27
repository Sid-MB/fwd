"""CLI help customization for compact alias display and root selector rewriting.

Typer models aliases as separate commands, which normally renders duplicate rows (``attach`` and ``a``). fwd keeps
the alias commands hidden and teaches the root group to annotate the canonical row as ``attach (a)``. Invocation and
completion still use the real command names; only help presentation changes.

Unknown root tokens that are valid sessions, targets, backends, or agents are rewritten to ``up --connect`` before
Click dispatch. This makes ``fwd runpod`` and ``fwd codex`` use the exact same parser and matching logic as explicit
``fwd up --connect runpod`` rather than maintaining dynamic one-off command callbacks.
"""

from __future__ import annotations

from typing import ClassVar

from typer import _click as click
from typer.core import TyperGroup
from typer._click.shell_completion import CompletionItem


class AliasHelpGroup(TyperGroup):
    """Render hidden command aliases beside their canonical command."""

    aliases: ClassVar[dict[str, tuple[str, ...]]] = {"attach": ("a",), "send": ("s",)}

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple[str | None, click.Command | None, list[str]]:
        """Retain original argv and rewrite root selectors to the canonical connect command."""
        original = tuple(args)
        if args and super().get_command(ctx, args[0]) is None:
            from fwd.ops.session_select import recognized_root_selector

            if recognized_root_selector(args[0]):
                rewritten = ["up", "--connect", *args]
                ctx.meta["fwd_selector_rewrite"] = original
                ctx.meta["fwd_canonical_argv"] = tuple(rewritten)
                args = rewritten
        resolved = super().resolve_command(ctx, args)
        ctx.meta["fwd_invocation_argv"] = original
        return resolved

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve registered commands; root selectors are normalized in :meth:`resolve_command`."""
        return super().get_command(ctx, cmd_name)

    def shell_complete(self, ctx: click.Context, incomplete: str) -> list[CompletionItem]:
        """Add dynamic target/backend commands to Click's normal root command completion."""
        results = super().shell_complete(ctx, incomplete)
        existing = {item.value for item in results}
        from fwd import agents
        from fwd.ops import target_alias
        from fwd.state import StateStore

        candidates = dict(target_alias.completion_candidates())
        candidates.update({name: f"{name} coding agent · connect or launch for this project" for name in agents.AGENTS})
        try:
            candidates.update({session.name: f"{session.backend} session · {session.flags.get('target', 'unknown target')}" for session in StateStore().all()})
        except Exception:
            pass
        results.extend(CompletionItem(value, help=help_text) for value, help_text in sorted(candidates.items()) if value.startswith(incomplete) and value not in existing)
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
