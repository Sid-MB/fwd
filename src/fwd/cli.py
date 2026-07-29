"""Typer CLI surface — flag parsing only, all logic in ``fwd.ops``.

Design intent
-------------
Two things make this module unusual and both are deliberate:

1. ``invoke_without_command=True`` plus a callback that dispatches through the same selector engine as ``up`` and
   ``attach`` implements bare ``fwd`` as ``fwd up --reuse``. ``no_args_is_help`` is therefore off — printing help
   would defeat the default command.
2. Every ``ops`` import happens *inside* the command body. Imports at module scope would make ``fwd --help`` pay for
   loading every operation and, while the backends are being built in parallel, would let one teammate's broken module
   break unrelated commands.

``up`` and ``launch`` are the same function registered twice, so the alias cannot drift from the primary command.

Help text is treated as primary documentation: ``--help`` is the surface an agent or a new user reads first, so every
option carries an explicit ``help=`` that states the *behavioural* consequence (what gets billed, what leaves the
laptop, what a default silently does), not just a restatement of the flag name. ``fwd up`` groups its options into rich
help panels because its flag list spans two unrelated concerns — where the machine comes from, and what Claude context
travels with it — and a single flat list of eight options buries that distinction.
"""

from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import shlex
import sys
from typing import Annotated

import typer

from fwd import __version__, command_docs, ui
from fwd.cli_completion import complete_agent, complete_backend, complete_cloud_type, complete_compute_type, complete_config_key, complete_diff_target, complete_existing_session, complete_gpu, complete_output_format, complete_runpod_image, complete_send_subject, complete_session, complete_session_selector, complete_ssh_host, complete_target
from fwd.cli_help import AliasHelpGroup
from fwd.output import OutputFormat

# Panel titles for `fwd up`. Kept as constants so the two groups are named identically everywhere they are referenced.
PANEL_TARGET = "Target & session"
PANEL_CLAUDE = "Claude context"
JsonOutputOption = Annotated[bool, typer.Option("--json", help="Render structured output as JSON; shorthand for --format json.")]
CONFIG_DOCS_URL = "https://github.com/Sid-MB/fwd#configuration"
COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "a": ("attach",),
    "s": ("send",),
    "launch": ("up",),
    "default": ("config", "set", "default_command"),
    "version": ("-V",),
}

app = typer.Typer(
    cls=AliasHelpGroup,
    name=ui.COMMAND_NAME,
    help="Move coding work to remote compute: provision or reuse a target, sync the project, and run a persistent shell, command, Claude Code, or Codex.",
    epilog=f"Bare {ui.command()!r} means {ui.command('up --reuse')!r}: attach to the unambiguous matching session, or create and attach interactively. Root selectors such as {ui.command('runpod')!r}, {ui.command('codex')!r}, and {ui.command('--name demo')!r} use the same matching grammar; add an arbitrary command to run it through the managed task runner. For a non-attaching agent launch, omit --reuse: {ui.command('up runpod codex')!r}. Learn config with {ui.command('config --example')!r} or {ui.command('config --schema')!r}; guide: {CONFIG_DOCS_URL}. Diagnose with {ui.command('doctor')!r}.",
    add_completion=True,
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
config_app = typer.Typer(
    name="config",
    help=f"Inspect or update {ui.command()}'s layered configuration.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config")


def _interactive_terminal() -> bool:
    """Return whether attaching can safely take over this process.

    Both streams must be terminals because tmux needs interactive input and users still need to see its output.
    Known agent environments are treated as non-interactive even when their command runner happens to allocate a
    pseudo-terminal: replacing an agent tool call with ``ssh -t`` would strand the caller inside tmux.
    """
    return sys.stdin.isatty() and sys.stdout.isatty() and not any(os.environ.get(name) for name in ("CLAUDECODE", "CODEX_AGENT"))


def _should_attach(command: tuple[str, ...], *, attach: bool, no_attach: bool) -> bool:
    """Resolve explicit attach flags and the interactive default for registered coding agents."""
    from fwd import agents

    if attach and no_attach:
        ui.die("--attach and --no-attach are mutually exclusive")
    return attach or (agents.resolve(command) is not None and _interactive_terminal() and not no_attach)


def _version_callback(value: bool) -> None:
    """Print the version before command dispatch, making ``fwd -V`` safe even though bare ``fwd`` performs work."""
    if value:
        ui.console.print(__version__)
        raise typer.Exit()


def _selected_output_format(output_format: OutputFormat, *, json_output: bool) -> OutputFormat:
    """Apply the reusable ``--json`` shortcut while rejecting contradictory explicit rendering requests."""
    if not json_output:
        return output_format
    if output_format not in (OutputFormat.auto, OutputFormat.json):
        ui.die(f"--json conflicts with --format {output_format.value}; pass only one output format")
    return OutputFormat.json


def _selected_ls_columns(requested: tuple[str, ...], **choices: bool) -> tuple[str, ...] | None:
    """Combine generic column names with shortcut flags, returning canonical order or the complete-table sentinel."""
    from fwd.session_columns import LS_COLUMNS, parse_columns

    try:
        selected = set(parse_columns(requested))
    except ValueError as exc:
        ui.die(str(exc))
    selected.update(column for column, enabled in choices.items() if enabled)
    return tuple(column for column in LS_COLUMNS if column in selected) or None


def _announce_root_alias(ctx: typer.Context, *, target: str | None, agent: str | None, name: str | None, restart: bool) -> None:
    """Announce static and bare-command expansions before onboarding or operation logs can obscure them."""
    invoked = ctx.invoked_subcommand
    metadata = getattr(ctx, "meta", {})
    selector_rewrite = tuple(metadata.get("fwd_selector_rewrite", ()))
    if selector_rewrite:
        canonical = tuple(metadata.get("fwd_canonical_argv", ("up", "--reuse", *selector_rewrite)))
        ui.announce_alias(shlex.join([ui.COMMAND_NAME, *canonical]), invoked=shlex.join([ui.COMMAND_NAME, *selector_rewrite]))
        return
    if invoked in COMMAND_ALIASES:
        original = tuple(metadata.get("fwd_invocation_argv", (invoked,)))
        remaining = original[1:] if original and original[0] == invoked else ()
        actual_argv = [ui.COMMAND_NAME, *COMMAND_ALIASES[invoked], *remaining]
        invoked_argv = [ui.COMMAND_NAME, *original]
        ui.announce_alias(shlex.join(actual_argv), invoked=shlex.join(invoked_argv))
        return
    if invoked is None:
        arguments = [ui.COMMAND_NAME, "up", "--reuse"]
        if target:
            arguments.extend(("--target", target))
        if agent:
            arguments.extend(("--agent", agent))
        if name:
            arguments.extend(("--name", name))
        if restart:
            arguments.append("--restart")
        ui.announce_alias(shlex.join(arguments))


@app.callback()
def main(
    ctx: typer.Context,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Bare-command target/backend selector; equivalent to fwd up --reuse --target NAME.", autocompletion=complete_target)] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Bare-command coding-agent selector; equivalent to fwd up --reuse --agent NAME.", autocompletion=complete_agent)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Bare-command session name; attach if it exists, otherwise create it interactively.", autocompletion=complete_session)] = None,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
    version: Annotated[bool, typer.Option("--version", "-V", callback=_version_callback, is_eager=True, help=f"Print the installed {ui.command()} version and exit.")] = False,
) -> None:
    """Connect to a matching session, creating and attaching interactively when none exists.

    Equivalent to 'fwd up --reuse' with the same --target, --agent, and --name selectors.
    """
    if not ctx.resilient_parsing:
        _announce_root_alias(ctx, target=target, agent=agent, name=name, restart=restart)
    if not ctx.resilient_parsing and ctx.invoked_subcommand != "uninstall" and _interactive_terminal():
        from fwd import completion_setup, skill_setup

        completion_setup.offer_once()
        skill_setup.offer_once()
        if ctx.invoked_subcommand not in {"send", "s"}:
            skill_setup.update_if_needed()
    if ctx.invoked_subcommand is not None:
        return
    create_argv = [ui.COMMAND_NAME, "up"]
    if target:
        create_argv.extend(("--target", target))
    if agent:
        create_argv.extend(("--agent", agent))
    if name:
        create_argv.extend(("--name", name))
    _run_up(
        (),
        target=target,
        agent=agent,
        name=name,
        reuse=True,
        restart=restart,
        create_argv=tuple(create_argv),
    )


def _argv_without_reuse(ctx: typer.Context) -> tuple[str, ...]:
    """Return the canonical ``fwd up`` invocation with ``--reuse`` removed for actionable non-interactive errors."""
    root = ctx.find_root()
    raw = tuple(root.meta.get("fwd_canonical_argv") or root.meta.get("fwd_invocation_argv") or ("up",))
    if not raw or raw[0] != "up":
        raw = ("up", *raw)
    filtered = []
    removed = False
    remote_argv = False
    for argument in raw:
        if argument == "--":
            remote_argv = True
        if not remote_argv and not removed and argument in ("--reuse", "-r"):
            removed = True
            continue
        filtered.append(argument)
    return (ui.COMMAND_NAME, *filtered)


def _reject_retired_connect_option(ctx: typer.Context) -> None:
    """Fail safely when the former reuse flag appears before the remote-command separator.

    ``fwd up`` deliberately accepts unknown options after ``--`` for remote commands. Without this migration guard,
    the removed ``--connect`` option would be interpreted as a remote command and could provision a new session.
    """
    root = ctx.find_root()
    raw = tuple(root.meta.get("fwd_canonical_argv") or root.meta.get("fwd_invocation_argv") or ())
    for argument in raw:
        if argument == "--":
            return
        if argument in ("--connect", "-c"):
            ui.die(f"{argument} was renamed to {'--reuse' if argument == '--connect' else '-r'}; run {ui.command('up --reuse')!r} to reuse and attach to a matching session")


def _configured_command(config, selector) -> tuple[str, ...]:
    """Best-effort resolution of the command used for auto-attach policy; launch owns authoritative errors/setup."""
    if selector.initial_command is not None:
        return selector.initial_command
    try:
        target = config.target(selector.target.launch_name if selector.target else None)
        return config.command_for(target.name)
    except Exception:
        return tuple(config.default_command)


def _run_up(
    positional: tuple[str, ...],
    *,
    target: str | None = None,
    agent: str | None = None,
    name: str | None = None,
    gpu: str | None = None,
    new: bool = False,
    reuse: bool = False,
    restart: bool = False,
    attach: bool = False,
    no_attach: bool = False,
    session: bool = False,
    handoff: bool = False,
    user_config: bool = False,
    creds: bool = False,
    setup_github: bool | None = None,
    stop_after: bool = False,
    ports: tuple[str, ...] | None = None,
    create_argv: tuple[str, ...] | None = None,
) -> int | None:
    """Shared launch/reuse implementation, returning an explicit streamed-command exit code when applicable."""
    from fwd.ops import attach as attach_ops
    from fwd.ops import launch as launch_ops
    from fwd.ops import session_select

    if reuse and new:
        ui.die("--reuse and --new are mutually exclusive")
    if reuse and no_attach:
        ui.die("--reuse and --no-attach are mutually exclusive")
    selection = session_select.select_current(
        positional,
        target=target,
        agent=agent,
        name=name,
        gpu=gpu,
        state=launch_ops.store(),
        match_command=attach,
    )
    selector = selection.selector
    config = selection.config
    sessions = selection.sessions
    cwd = selection.cwd
    if new and selector.name is not None:
        ui.die("--new and --name are mutually exclusive")
    matches = selection.matches
    chosen_match = launch_ops.choose_session(matches, selector.describe()) if matches else None
    managed_command = selector.command is not None and not attach
    desired_ports = ports if ports is not None else tuple(config.forwarding.ports)
    github_override = {} if setup_github is None else {"setup_github": setup_github}

    if chosen_match is not None and managed_command and not new:
        from fwd.ops import send as send_ops
        from fwd.ops import ports as ports_ops

        ports_ops.preflight_launch_ports(chosen_match, desired_ports)
        ports_ops.ensure_session_ports(chosen_match, desired_ports)
        ui.info(f"selectors matched session {chosen_match.name!r}; running the command as a managed task")
        return send_ops.run_command(selector.command or (), name=chosen_match.name, stop_after=stop_after, **github_override)

    if reuse and chosen_match is not None:
        chosen = chosen_match
        ui.info(f"selectors matched session {chosen.name!r}; attaching")
        if not _interactive_terminal():
            ui.die(
                f"session {chosen.name!r} matches, but attaching requires an interactive terminal. "
                f"Run {ui.command(f'attach {chosen.name}')!r} in a terminal; agents can use {ui.command(f'send --name {chosen.name} -- COMMAND')!r}."
            )
        if desired_ports:
            attach_ops.attach(chosen.name, restart=restart, forward_ports=desired_ports, **github_override)
        else:
            attach_ops.attach(chosen.name, restart=restart, **github_override)
        return

    if reuse and not matches and not _interactive_terminal():
        creation = shlex.join(create_argv or (ui.COMMAND_NAME, "up"))
        ui.die(
            f"no session matches {selector.describe()}. This is non-interactive mode, so --reuse will not provision. "
            f"Create it explicitly without --reuse: `{creation}`"
        )

    initial_command = selector.initial_command
    if stop_after and selector.command is None:
        ui.die(f"--stop-after requires an explicit command, for example {ui.command('up --stop-after -- pytest -q')!r}; a general shell or interactive agent has no completion point")
    if managed_command:
        effective_attach = False
    elif reuse:
        effective_attach = True
    else:
        effective_attach = _should_attach(_configured_command(config, selector), attach=attach, no_attach=no_attach)
    if stop_after and effective_attach:
        ui.die(f"--stop-after cannot be combined with attachment; omit --attach so {ui.command('up')!r} can track the command as a durable task")

    launch_name = selector.name
    launch_new = new
    if not new and launch_name is None and chosen_match is not None:
        launch_name = chosen_match.name
    elif not new and launch_name is None and selector.constrained:
        has_project_session = any(Path(candidate.local_cwd).expanduser().resolve() == cwd for candidate in sessions)
        launch_new = has_project_session

    stream_command = managed_command
    state = launch_ops.launch(
        target=selector.target.launch_name if selector.target else None,
        gpu=gpu,
        name=launch_name,
        new=launch_new,
        initial_command=() if stream_command else initial_command,
        run_command_as_task=stream_command,
        session=session,
        handoff=handoff,
        user_config=user_config,
        creds=creds,
        attach=effective_attach,
        forward_ports=ports,
        **github_override,
    )
    if stream_command:
        from fwd.ops import send as send_ops

        return send_ops.run_command(initial_command or (), name=state.name, stop_after=stop_after, **github_override)
    return None


def _up(
    ctx: typer.Context,
    selectors: Annotated[list[str] | None, typer.Argument(help="Optional target/backend followed by a coding agent or command. Target names take precedence over agent names.", autocompletion=complete_session_selector)] = None,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Configured target to use; defaults to default_target, or the existing session's target.", autocompletion=complete_target, rich_help_panel=PANEL_TARGET)] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Registered coding agent to launch, such as claude or codex.", autocompletion=complete_agent, rich_help_panel=PANEL_TARGET)] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="Override GPU selection for an explicitly GPU-enabled target (RunPod GPU id or Slurm --gres spec).", autocompletion=complete_gpu, rich_help_panel=PANEL_TARGET)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to a stable slug derived from this directory.", autocompletion=complete_session, rich_help_panel=PANEL_TARGET)] = None,
    new: Annotated[bool, typer.Option("--new", help="Create a fresh session instead of reusing this directory's existing session. Cannot be combined with --name.", rich_help_panel=PANEL_TARGET)] = False,
    reuse: Annotated[bool, typer.Option("--reuse", "-r", help="Reuse a matching session; attach when no task command is supplied, or create only interactively when none exists.", rich_help_panel=PANEL_TARGET)] = False,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="With --reuse, authorize restarting stopped billable compute without prompting.", rich_help_panel=PANEL_TARGET)] = False,
    attach: Annotated[bool, typer.Option("--attach", "-a", help="Attach directly after startup instead of streaming an explicit command as a durable task.", rich_help_panel=PANEL_TARGET)] = False,
    no_attach: Annotated[bool, typer.Option("--no-attach", "--detach", help="Stay local even for magic agent commands that normally auto-attach in a terminal.", rich_help_panel=PANEL_TARGET)] = False,
    session: Annotated[bool, typer.Option("--session", help="Move the real transcript so claude resumes it; already the default, pass this only to re-enable it when config disables it.", rich_help_panel=PANEL_CLAUDE)] = False,
    handoff: Annotated[bool, typer.Option("--handoff", help="Summarize into HANDOFF.md instead of moving the transcript; replaces --session entirely.", rich_help_panel=PANEL_CLAUDE)] = False,
    user_config: Annotated[bool, typer.Option("--user-config", help="Upload your ~/.claude bundle (CLAUDE.md, skills, agents, commands, settings.json); never credentials or history.", rich_help_panel=PANEL_CLAUDE)] = False,
    creds: Annotated[bool, typer.Option("--creds", help="DANGER: write your live Claude OAuth token to the remote disk; prefer logging in inside the remote session.", rich_help_panel=PANEL_CLAUDE)] = False,
    setup_github: Annotated[bool | None, typer.Option("--setup-github/--no-setup-github", help="Set up GitHub authentication for this launch; defaults to github.auth (enabled by default).", rich_help_panel=PANEL_TARGET)] = None,
    stop_after: Annotated[bool, typer.Option("--stop-after", help="After an explicit streamed command finishes, stop the remote session from the server even if this computer disconnects.", rich_help_panel=PANEL_TARGET)] = False,
    ports: Annotated[list[str] | None, typer.Option("--ports", "-p", help="Open PORT or LOCAL:REMOTE after launch; repeat to replace project-configured defaults for this invocation.", rich_help_panel=PANEL_TARGET)] = None,
) -> None:
    """Provision/reuse a target, sync and bootstrap it, then start the selected/default command.

    Positionals are [TARGET] [AGENT|COMMAND...]. Magic agents 'claude' and 'codex' sync their settings and auto-attach
    in an interactive terminal. --reuse attaches to a matching session when no task command is supplied, or creates
    one only in a human terminal. Explicit commands use the same durable task manager as 'fwd send -- COMMAND'; pass
    --attach to enter the primary tmux session instead. Use
    --no-attach for an agent background launch and '--' before remote command flags.

    To add a new target, run 'fwd setup'.
    """
    _reject_retired_connect_option(ctx)
    code = _run_up(
        tuple(selectors or ()),
        target=target,
        agent=agent,
        gpu=gpu,
        name=name,
        new=new,
        reuse=reuse,
        restart=restart,
        attach=attach,
        no_attach=no_attach,
        session=session,
        handoff=handoff,
        user_config=user_config,
        creds=creds,
        setup_github=setup_github,
        stop_after=stop_after,
        ports=tuple(ports) if ports else None,
        create_argv=_argv_without_reuse(ctx),
    )
    if code is not None:
        raise typer.Exit(code)


UP_HELP = f"""{command_docs.UP.summary}

Positionals are [TARGET] [AGENT|COMMAND...]. Magic agents 'claude' and 'codex' sync their settings and auto-attach
in an interactive terminal. --reuse attaches to a matching session when no task command is supplied, or creates one
only in a human terminal. Explicit commands use the same managed task runner as {ui.command('send -- COMMAND')!r} and
stream back with Ctrl-C to cancel and Ctrl-B to background; pass --attach to enter their session directly, or
--stop-after to stop remote compute after the tracked command. Use '--' before remote command flags.

To add a new target, run {ui.command('setup')!r}.
"""

# Registered twice so `up` and its `launch` alias can never diverge.
app.command("up", help=UP_HELP, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_up)
app.command("launch", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_up)


def _attach(
    selectors: Annotated[list[str] | None, typer.Argument(help="Session name, or target/backend followed by an agent or exact startup command.", autocompletion=complete_session_selector)] = None,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Require a session on this target/backend.", autocompletion=complete_target)] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Require a session running this registered coding agent.", autocompletion=complete_agent)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Require this exact session name.", autocompletion=complete_session)] = None,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="If the primary tmux session is missing, start a plain recovery shell without rerunning launch preparation.")] = False,
    setup_github: Annotated[bool | None, typer.Option("--setup-github/--no-setup-github", help="Set up GitHub authentication before attaching; defaults to github.auth (enabled by default).")] = None,
) -> None:
    """Attach to the unambiguous session matching every supplied selector.

    Replaces this process with 'ssh -t', so the remote session owns the terminal outright: resize, mouse reporting and ctrl-C behave exactly as a hand-typed ssh would. Detach with tmux's ctrl-b d; the session keeps running.
    """
    from fwd.ops import attach as attach_ops
    from fwd.ops import launch as launch_ops
    from fwd.ops import session_select

    selection = session_select.select_current(
        tuple(selectors or ()),
        target=target,
        agent=agent,
        name=name,
        state=launch_ops.store(),
    )
    if not selection.matches:
        ui.die(f"no session matches {selection.selector.describe()}; inspect available sessions with {ui.command('ls')!r}")
    chosen = launch_ops.choose_session(selection.matches, selection.selector.describe())
    if not _interactive_terminal():
        ui.die(
            f"session {chosen.name!r} matches, but attaching requires an interactive terminal. "
            f"Run {ui.command(f'attach {chosen.name}')!r} in a terminal."
        )
    ui.info(f"selectors matched session {chosen.name!r}; attaching")
    github_override = {} if setup_github is None else {"setup_github": setup_github}
    if raw:
        attach_ops.attach(chosen.name, restart=restart, raw=True, **github_override)
    else:
        attach_ops.attach(chosen.name, restart=restart, **github_override)


ATTACH_HELP = f"""{command_docs.ATTACH.summary}

Replaces this process with 'ssh -t', so the remote session owns the terminal outright. Detach with tmux's ctrl-b d;
the session keeps running. If launch preparation failed before tmux started, pass --raw to enter a plain recovery
shell without rerunning tool or dependency installation.
"""

# Registered from one callback so the tmux-style `a` alias and `attach` always accept identical arguments.
app.command("attach", help=ATTACH_HELP)(_attach)
app.command("a", hidden=True)(_attach)


def _send(
    ctx: typer.Context,
    arguments: Annotated[list[str] | None, typer.Argument(help="Remote command after '--', agent plus message, or an existing task id.", autocompletion=complete_send_subject)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name, target, or backend; defaults to this directory's session.", autocompletion=complete_existing_session)] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Cancel the remote task if it exceeds this many seconds.")] = None,
    detach: Annotated[bool, typer.Option("--detach", "-d", help="Start the task in the background and return immediately.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Stream until completion; this is the default unless --detach is passed.")] = False,
    stop: Annotated[bool, typer.Option("--stop", help="Cancel the selected task; with an agent message, cancel the active turn and send the replacement.")] = False,
    immediate: Annotated[bool, typer.Option("--immediate", help="Agent shorthand for --stop MESSAGE: cancel the active turn and immediately send this message.")] = False,
    stop_after: Annotated[bool, typer.Option("--stop-after", help="Queue a remote-owned stop action after the new command or agent turn finishes.")] = False,
    list_only: Annotated[bool, typer.Option("--ls", help="List send tasks instead of starting one.")] = False,
    include_all: Annotated[bool, typer.Option("--all", help="With --ls, include completed, failed, and canceled tasks.")] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="With --ls, choose Rich, Markdown, or JSON output.", autocompletion=complete_output_format)] = OutputFormat.auto,
    json_output: JsonOutputOption = False,
) -> None:
    """Start, follow, background, list, or cancel durable remote tasks.

    Every command and agent turn runs in remote tmux and receives a task id. During streams, Ctrl-C cancels and Ctrl-B
    backgrounds. Reattach with 'fwd send TASK_ID', cancel with 'fwd send TASK_ID --stop', and list with 'fwd send --ls'.
    Never starts or restarts compute. Use '--' before raw commands: fwd send -- python train.py --epochs 10
    """
    from fwd.ops import send as send_ops

    if wait and detach:
        ui.die("--wait and --detach are mutually exclusive")
    if include_all and not list_only:
        ui.die("--all is only valid with --ls")
    root = ctx.find_root()
    raw_argv = tuple(root.meta.get("fwd_canonical_argv") or root.meta.get("fwd_invocation_argv") or ())
    literal_command = "--" in raw_argv
    code = send_ops.dispatch(
        tuple(arguments or ()),
        name=name,
        timeout=timeout,
        detach=detach,
        stop=stop,
        immediate=immediate,
        stop_after=stop_after,
        list_only=list_only,
        include_all=include_all,
        literal_command=literal_command,
        output_format=_selected_output_format(output_format, json_output=json_output),
    )
    raise typer.Exit(code)


SEND_HELP = f"""{command_docs.SEND.summary}

Every command and agent turn runs in remote tmux and receives a task id. During streams, Ctrl-C cancels and Ctrl-B
backgrounds. Add --stop-after to stop compute remotely when new work finishes; {ui.command('send stopafter')!r}
queues it after current work and {ui.command('send cancel stopafter')!r} cancels it. Reattach with
{ui.command('send TASK_ID')!r}, cancel queued work with {ui.command('send cancel [TASK_ID|all]')!r}, and list with
{ui.command('send --ls')!r}. Never starts or restarts compute. Use '--' before raw commands:
{ui.command('send -- python train.py --epochs 10')}
"""

# Registered from one callback so the short alias cannot diverge from the primary command.
app.command("send", help=SEND_HELP, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_send)
app.command("s", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_send)


@app.command("ls", help=command_docs.LIST.summary)
def ls_cmd(
    all_projects: Annotated[bool, typer.Option("--all-projects", help="Show sessions from every locally tracked project instead of only the current project.")] = False,
    columns: Annotated[list[str] | None, typer.Option("--columns", "-c", help="Show selected columns by comma-separated name; repeat to combine. Session names remain as row identity.")] = None,
    names: Annotated[bool, typer.Option("--names", help="Show only session names.")] = False,
    backends: Annotated[bool, typer.Option("--backends", "--backend", help="Show session names and backends.")] = False,
    statuses: Annotated[bool, typer.Option("--statuses", "--status", help="Show session names and live statuses.")] = False,
    stop_after: Annotated[bool, typer.Option("--stop-after", help="Show session names and queued stop-after state.")] = False,
    running: Annotated[bool, typer.Option("--running", help="Show session names and current run durations.")] = False,
    tmux: Annotated[bool, typer.Option("--tmux", help="Show session names and remote tmux names.")] = False,
    local_dirs: Annotated[bool, typer.Option("--local-dirs", "--local-dir", help="Show session names and local project directories.")] = False,
    last_attached: Annotated[bool, typer.Option("--last-attached", help="Show session names and last-attachment times.")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Show session names and provider identifiers.")] = False,
    ports: Annotated[bool, typer.Option("--ports", help="Show session names and local port forwarding.")] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: auto uses Rich in a terminal and Markdown otherwise.", autocompletion=complete_output_format)] = OutputFormat.auto,
    json_output: JsonOutputOption = False,
) -> None:
    """List this project's managed sessions with live status queried from each backend."""
    from fwd.ops import lifecycle

    columns = _selected_ls_columns(
        tuple(columns or ()),
        name=names,
        backend=backends,
        status=statuses,
        **{
            "stop after": stop_after,
            "running": running,
            "tmux": tmux,
            "local dir": local_dirs,
            "last attached": last_attached,
            "ids": ids,
            "ports": ports,
        },
    )
    lifecycle.ls(output_format=_selected_output_format(output_format, json_output=json_output), all_projects=all_projects, columns=columns)


@app.command("ports", help=command_docs.PORTS.summary)
def ports_cmd(
    arguments: Annotated[list[str] | None, typer.Argument(help="An optional session, target, backend, agent, or tmux selector followed by PORT or LOCAL:REMOTE mappings.", autocompletion=complete_session_selector)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Exact session name; defaults to shared positional selectors or the current project.", autocompletion=complete_existing_session)] = None,
    list_only: Annotated[bool, typer.Option("--ls", help="Alias for fwd ls --ports; one optional selector narrows it to a session.")] = False,
    close: Annotated[bool, typer.Option("--close", help="Close listed local ports, or every forward for the selected/current session when no ports are listed.")] = False,
    all_projects: Annotated[bool, typer.Option("--all-projects", help="List or close forwarding across every locally tracked project.")] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="In listing mode, choose the output format.", autocompletion=complete_output_format)] = OutputFormat.auto,
    json_output: JsonOutputOption = False,
) -> None:
    """Open background SSH forwards, inspect them, or close them without stopping remote compute."""
    from fwd import port_forwarding
    from fwd.ops import ports as ports_ops

    values = tuple(arguments or ())
    selected_format = _selected_output_format(output_format, json_output=json_output)
    if list_only and close:
        ui.die("--ls and --close are mutually exclusive")
    if close:
        if output_format is not OutputFormat.auto or json_output:
            ui.die("--format and --json are available only when listing ports")
        if all_projects:
            if values or name is not None:
                ui.die("--close --all-projects cannot be combined with a session selector or port mapping")
            ports_ops.close_all_projects()
        else:
            ports_ops.close_ports(values, name=name)
        return
    contains_mapping = bool(values) and (port_forwarding.mapping_argument(values[0]) or len(values) > 1)
    if list_only or not contains_mapping:
        ports_ops.list_ports(values, name=name, all_projects=all_projects, output_format=selected_format)
        return
    if all_projects:
        ui.die("--all-projects cannot be combined with mappings")
    if output_format is not OutputFormat.auto or json_output:
        ui.die("--format and --json are available only when listing ports")
    ports_ops.open_ports(values, name=name)


@app.command("push")
def push_cmd(
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name, target, or backend; defaults to this directory's session.", autocompletion=complete_existing_session)] = None,
) -> None:
    """Mirror local changes up to the remote session; remote-only files are deleted unless sync.delete is off."""
    from fwd.ops import transfer

    transfer.push(name)


@app.command("pull")
def pull_cmd(
    paths: Annotated[list[str] | None, typer.Argument(help="Remote-relative paths to fetch; omit to pull the whole remote directory.")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name, target, or backend; defaults to this directory's session.", autocompletion=complete_existing_session)] = None,
) -> None:
    """Bring remote changes back down to the local directory, additively — a pull never deletes local files."""
    from fwd.ops import transfer

    transfer.pull(name, tuple(paths or ()))


@app.command("diff")
def diff_cmd(
    target: Annotated[str | None, typer.Argument(help="Session name, configured target, or backend; defaults to this directory's session.", autocompletion=complete_diff_target)] = None,
    path: Annotated[str | None, typer.Argument(help="Project-relative file or directory; omit to compare the entire synced project.")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Print no diff; communicate identical/different/error through exit status only.")] = False,
    include_gitignored: Annotated[bool, typer.Option("--include-gitignored", help="Also compare Git-ignored content, while retaining .fwdignore and configured sync exclusions.")] = False,
    include_unsynced: Annotated[bool, typer.Option("--include-unsynced", help="Compare all ordinarily unsynced content; .git and permanent OS metadata exclusions still apply.")] = False,
) -> None:
    """Compare local and remote content with Git-style output: exit 0 if identical, 1 if different, and 2 on errors."""
    from fwd.ops import diff as diff_ops

    try:
        code = diff_ops.diff(
            target,
            path,
            quiet=quiet,
            include_gitignored=include_gitignored,
            include_unsynced=include_unsynced,
        )
    except typer.Exit as exc:
        raise typer.Exit(max(2, exc.exit_code)) from exc
    except Exception as exc:
        ui.error(f"diff failed: {exc}")
        raise typer.Exit(2) from exc
    raise typer.Exit(code)


@app.command("stop", help=f"{command_docs.STOP.summary}\n\nRestart with {ui.command('attach --restart')!r} or another {ui.command('up')!r}. SSH/Slurm project storage remains; on RunPod only an attached persistent volume survives, and CPU pod work is wiped.")
def stop_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name, target, or backend; defaults to this directory's session.", autocompletion=complete_existing_session)] = None,
) -> None:
    """Kill remote tmux and ask the backend to suspend billable compute; storage preservation depends on the target.

    Restart with 'fwd attach --restart' or another 'fwd up'. SSH/Slurm project storage remains; on RunPod only an attached persistent volume survives, and CPU pod work is wiped.
    """
    from fwd.ops import lifecycle

    lifecycle.stop(name)


@app.command("rm", help=f"{command_docs.REMOVE.summary} Irreversible — remote data is gone.\n\nThe confirmation defaults to no, so a scripted {ui.command('rm')!r} without --force safely does nothing.")
def rm_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name, target, or backend; defaults to this directory's session.", autocompletion=complete_existing_session)] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Destroy every tracked session and its remote data. Cannot be combined with a session name.")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt; required non-interactively, where the prompt defaults to no.")] = False,
) -> None:
    """Destroy one or all session targets and forget their state. Irreversible — remote data is gone.

    The confirmation defaults to no, so a scripted 'fwd rm' without --force safely does nothing.
    """
    from fwd.ops import lifecycle

    if all_sessions and name is not None:
        ui.die(f"{ui.command('rm')} accepts either a session name or --all, not both")
    if all_sessions:
        lifecycle.remove_all(force=force)
    else:
        lifecycle.remove(name, force=force)


@app.command("uninstall")
def uninstall_cmd(
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation; if sessions remain tracked, forget them without stopping or destroying their remote resources.")] = False,
) -> None:
    """Remove local fwd data, coding-agent skills, completions, and temporary logs.

    Refuses to discard state for tracked remote resources unless --force explicitly accepts orphaning them. The
    running CLI cannot remove its own environment portably, so the final package-manager command is printed for you.
    """
    from fwd.ops import uninstall as uninstall_ops

    raise typer.Exit(uninstall_ops.uninstall(force=force))


class ExampleBackend(str, Enum):
    """Choices for ``fwd config --example``. An Enum so Typer renders and validates the list for free."""

    ssh = "ssh"
    runpod = "runpod"
    slurm = "slurm"
    all = "all"


@config_app.callback()
def config_cmd(
    ctx: typer.Context,
    example: Annotated[bool, typer.Option("--example", help=f"Print a commented reference config generated from {ui.command()}'s own schema instead of your effective one.")] = False,
    schema: Annotated[bool, typer.Option("--schema", help="Print the complete machine-readable JSON Schema for config files.")] = False,
) -> None:
    """Show effective config, a commented TOML reference, or machine-readable JSON Schema.

    Outputs go to stdout without terminal formatting. Guide: https://github.com/Sid-MB/fwd#configuration
    """
    if ctx.invoked_subcommand in {"set", "rm"} and (example or schema):
        ui.die(f"--example/--schema cannot be combined with 'config {ctx.invoked_subcommand}'")
    if ctx.invoked_subcommand is not None:
        return
    if example and schema:
        ui.die("--example and --schema are mutually exclusive")
    from fwd.ops import configcmd

    configcmd.show(ExampleBackend.all.value if example else None, schema=schema)


def _config_example_backend(ctx: typer.Context, backend: ExampleBackend) -> None:
    """Preserve ``fwd config --example <backend>`` while allowing real config subcommands such as ``set``."""
    parent = ctx.parent
    if parent is None or not parent.params.get("example"):
        ui.die(f"{ui.command(f'config {backend.value}')!r} is only meaningful with --example; run {ui.command(f'config --example {backend.value}')!r} for a reference, or {ui.command('config')!r} for your effective config")
    if parent.params.get("schema"):
        ui.die("--example and --schema are mutually exclusive")
    from fwd.ops import configcmd

    configcmd.show(backend.value)


def _example_backend_command(backend: ExampleBackend):
    """Create one hidden compatibility command without exposing the captured backend as a CLI parameter."""

    def command(ctx: typer.Context) -> None:
        _config_example_backend(ctx, backend)

    return command


for _example_backend in ExampleBackend:
    config_app.command(_example_backend.value, hidden=True)(_example_backend_command(_example_backend))


def _set_config_value(key: str, value: tuple[str, ...], *, user: bool, project: bool, target: str | None) -> None:
    """Shared implementation for ``config set`` and its task-focused ``default`` alias."""
    from fwd.config import ConfigError
    from fwd.ops import configcmd

    try:
        configcmd.set_value(key, value, user=user, project=project, target=target)
    except ConfigError as exc:
        ui.die(str(exc))


@config_app.command("set", help=f"Set a configuration value without opening an editor.\n\nUse '--' before values that begin with '-', for example: {ui.command('config set default_command -- python -m agent')}", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def config_set_cmd(
    key: Annotated[str, typer.Argument(help="Dotted config key, for example default_target, default_command, or sync.delete.", autocompletion=complete_config_key)],
    value: Annotated[list[str] | None, typer.Argument(help="Value words. Multiple words become an array; default_command is always stored as argv.", autocompletion=complete_agent)] = None,
    user: Annotated[bool, typer.Option("--user", help="Write ~/.fwd/config.toml; this is the default scope.")] = False,
    project: Annotated[bool, typer.Option("--project", help="Write ./.fwd/config.toml for this project.")] = False,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Set a target-specific value, which overrides project and user defaults.", autocompletion=complete_target)] = None,
) -> None:
    """Set a configuration value without opening an editor.

    Use '--' before values that begin with '-', for example: fwd config set default_command -- python -m agent
    """
    _set_config_value(key, tuple(value or ()), user=user, project=project, target=target)


@config_app.command("rm")
def config_rm_cmd(
    key: Annotated[str, typer.Argument(help="Dotted config key to remove at exactly one scope.", autocompletion=complete_config_key)],
    user: Annotated[bool, typer.Option("--user", help="Remove the user-wide value from ~/.fwd/config.toml; this is the default scope.")] = False,
    project: Annotated[bool, typer.Option("--project", help="Remove only the current project's value.")] = False,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Remove one target-specific value without changing the target itself.", autocompletion=complete_target)] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation; required in non-interactive mode when the value exists.")] = False,
) -> None:
    """Remove a configuration value at one scope, revealing the next-higher-precedence value.

    Missing values are reported as a successful no-op. Interactive removal confirms; agents and pipes must pass
    --force. Omitting --user, --project, and --target selects user scope.
    """
    from fwd.config import ConfigError
    from fwd.ops import configcmd

    try:
        configcmd.remove_value(key, user=user, project=project, target=target, force=force)
    except ConfigError as exc:
        ui.die(str(exc))


@app.command("default", help=f"Set the command bare {ui.command()!r} launches; shorthand for {ui.command('config set default_command ...')!r}.\n\nUse '--' before command flags, for example: {ui.command('default --project -- python -m my_agent')}", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def default_cmd(
    command: Annotated[list[str] | None, typer.Argument(help="Default command argv, such as claude, codex, or python -m agent.", autocompletion=complete_agent)] = None,
    user: Annotated[bool, typer.Option("--user", help="Set the user-wide default in ~/.fwd/config.toml; this is the default scope.")] = False,
    project: Annotated[bool, typer.Option("--project", help="Set the default only for the current project.")] = False,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Set the default for one target; target settings override project and user settings.", autocompletion=complete_target)] = None,
) -> None:
    """Set the command bare ``fwd`` launches; shorthand for ``fwd config set default_command ...``.

    Use '--' before command flags, for example: fwd default --project -- python -m my_agent
    """
    _set_config_value("default_command", tuple(command or ()), user=user, project=project, target=target)


@app.command("setup")
def setup_cmd(
    backend: Annotated[str | None, typer.Option("--backend", help="Backend to configure: ssh, runpod, or slurm. Required in non-interactive mode.", autocompletion=complete_backend)] = None,
    target_name: Annotated[str | None, typer.Option("--target-name", help=f"Local {ui.command()} label for this connection; defaults to the backend name.", autocompletion=complete_target)] = None,
    host: Annotated[str | None, typer.Option("--host", help="SSH hostname, IP, or Host alias from ~/.ssh/config.", autocompletion=complete_ssh_host)] = None,
    login_host: Annotated[str | None, typer.Option("--login-host", help="Slurm cluster login hostname or SSH alias.", autocompletion=complete_ssh_host)] = None,
    user: Annotated[str | None, typer.Option("--user", help="Remote username; optional for SSH aliases.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="SSH port.")] = None,
    key_path: Annotated[str | None, typer.Option("--key-path", help="SSH identity file; omit to use your SSH agent/config.")] = None,
    proxy_jump: Annotated[str | None, typer.Option("--proxy-jump", help="External SSH host used to reach a non-public target, as user@host.", autocompletion=complete_ssh_host)] = None,
    extra_ssh_option: Annotated[list[str] | None, typer.Option("--extra-ssh-option", help="Additional raw SSH argv entry; repeat to preserve argument boundaries.")] = None,
    remote_base: Annotated[str | None, typer.Option("--remote-base", help="Remote parent directory for project checkouts.")] = None,
    compute_type: Annotated[str | None, typer.Option("--compute-type", help="RunPod compute type: cpu (default) or gpu.", autocompletion=complete_compute_type)] = None,
    cloud_type: Annotated[str | None, typer.Option("--cloud-type", help="RunPod cloud pool: secure (default) or community.", autocompletion=complete_cloud_type)] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="Default RunPod GPU id; used only with --compute-type gpu.", autocompletion=complete_gpu)] = None,
    image: Annotated[str | None, typer.Option("--image", help="Default RunPod container image.", autocompletion=complete_runpod_image)] = None,
    volume_gb: Annotated[int | None, typer.Option("--volume-gb", help="RunPod persistent volume size in GB.")] = None,
    volume_mount_path: Annotated[str | None, typer.Option("--volume-mount-path", help="RunPod persistent volume mount path.")] = None,
    allow_proxy: Annotated[bool | None, typer.Option("--allow-proxy/--no-allow-proxy", help="Allow the RunPod SSH proxy fallback.")] = None,
    tool_prefix: Annotated[str | None, typer.Option("--tool-prefix", help="Persistent remote directory for installed tools and caches.")] = None,
    alloc: Annotated[str | None, typer.Option("--alloc", help="Slurm salloc flags.")] = None,
    partition: Annotated[str | None, typer.Option("--partition", help="Slurm partition.")] = None,
    account: Annotated[str | None, typer.Option("--account", help="Slurm account.")] = None,
    env_setup: Annotated[list[str] | None, typer.Option("--env-setup", help="Slurm shell line to run before allocation; repeat for multiple lines.")] = None,
    make_default: Annotated[bool, typer.Option("--make-default", help="Make this target the saved default. The first target is always made default.")] = False,
    test_connection: Annotated[bool, typer.Option("--test-connection", help="Run read-only provider diagnostics after non-interactive setup.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing target with the same name without prompting.")] = False,
    interactive: Annotated[bool, typer.Option("--interactive", help="Force prompts even when stdout is redirected or an agent environment is detected.")] = False,
) -> None:
    """Create or update ~/.fwd/config.toml interactively or entirely from flags.

    Setup automatically becomes non-interactive when stdout is not a TTY or CLAUDECODE/CODEX_AGENT is set. Missing
    required flags produce an exact invocation; pass --interactive to force prompts.
    """
    from fwd import wizard

    wizard.run_wizard(
        force_interactive=interactive,
        backend=backend,
        target_name=target_name,
        values={
            "host": host,
            "login_host": login_host,
            "user": user,
            "port": port,
            "key_path": key_path,
            "proxy_jump": proxy_jump,
            "extra_opts": extra_ssh_option,
            "remote_base": remote_base,
            "compute_type": compute_type,
            "cloud_type": cloud_type,
            "gpu": gpu,
            "image": image,
            "volume_gb": volume_gb,
            "volume_mount_path": volume_mount_path,
            "allow_proxy": allow_proxy,
            "tool_prefix": tool_prefix,
            "alloc": alloc,
            "partition": partition,
            "account": account,
            "env_setup": env_setup,
        },
        make_default=make_default,
        test_connection=test_connection,
        force=force,
    )


@app.command("doctor")
def doctor_cmd(
    target: Annotated[str | None, typer.Option("--target", "-t", help="Check only this target instead of every configured one.", autocompletion=complete_target)] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: auto uses Rich in a terminal and Markdown otherwise.", autocompletion=complete_output_format)] = OutputFormat.auto,
    json_output: JsonOutputOption = False,
) -> None:
    """Check local prerequisites (ssh, rsync, backend CLIs) and the reachability of each configured target.

    Exits non-zero when a check fails, so it doubles as a preflight in scripts.
    """
    from fwd import doctor

    raise typer.Exit(doctor.run_doctor(target, output_format=_selected_output_format(output_format, json_output=json_output)))


@app.command("info")
def info_cmd(
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: auto uses Rich in a terminal and Markdown otherwise.", autocompletion=complete_output_format)] = OutputFormat.auto,
    json_output: JsonOutputOption = False,
) -> None:
    """Print installed version and the local paths used for configuration and session state."""
    from fwd.config import GLOBAL_CONFIG_PATH, PROJECT_CONFIG_RELPATH
    from fwd.state import STATE_PATH

    ui.record(
        ui.command("info"),
        (
            ("version", __version__),
            ("global config", str(GLOBAL_CONFIG_PATH)),
            ("project config", f"<project>/{PROJECT_CONFIG_RELPATH}"),
            ("session state", str(STATE_PATH)),
        ),
        output_format=_selected_output_format(output_format, json_output=json_output),
    )


@app.command("version", hidden=True)
def version_cmd() -> None:
    """Compatibility alias for ``fwd -V``."""
    ui.console.print(__version__)


def entrypoint() -> None:
    """Run the Typer application with the centralized display name.

    Console-script wrappers otherwise pass their on-disk filename as Click's ``prog_name``, which would leave Usage
    lines hard-coded to the installed executable even after every other UI string adopted :func:`fwd.ui.command`.
    """
    app(prog_name=ui.COMMAND_NAME)


if __name__ == "__main__":
    entrypoint()
