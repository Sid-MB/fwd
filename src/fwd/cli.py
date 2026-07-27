"""Typer CLI surface — flag parsing only, all logic in ``fwd.ops``.

Design intent
-------------
Two things make this module unusual and both are deliberate:

1. ``invoke_without_command=True`` plus a callback that dispatches to :func:`fwd.ops.attach.smart_default` implements
   the bare-``fwd`` behaviour: attach to this directory's session, or launch one. ``no_args_is_help`` is therefore off —
   printing help would defeat the entire point of the default command.
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
import shlex
import sys
from typing import Annotated

import typer

from fwd import __version__, ui
from fwd.cli_completion import complete_agent, complete_backend, complete_cloud_type, complete_compute_type, complete_config_key, complete_diff_target, complete_gpu, complete_output_format, complete_runpod_image, complete_send_subject, complete_session, complete_ssh_host, complete_target
from fwd.cli_help import AliasHelpGroup
from fwd.output import OutputFormat

# Panel titles for `fwd up`. Kept as constants so the two groups are named identically everywhere they are referenced.
PANEL_TARGET = "Target & session"
PANEL_CLAUDE = "Claude context"
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
    epilog=f"Bare {ui.command()!r} attaches to this directory's session, launches its saved default, or starts setup on first use. {ui.command('<target>')!r} launches that target's saved default and attaches; {ui.command('<backend>')!r} uses its most recently used configured target. For a zero-config background launch, use {ui.command('up --target runpod')!r}, {ui.command('up --target user@host')!r}, or an SSH alias. Learn config with {ui.command('config --example')!r} or {ui.command('config --schema')!r}; guide: {CONFIG_DOCS_URL}. Diagnose with {ui.command('doctor')!r}.",
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


def _announce_root_alias(ctx: typer.Context, *, restart: bool) -> None:
    """Announce static and bare-command expansions before onboarding or operation logs can obscure them."""
    invoked = ctx.invoked_subcommand
    if invoked in COMMAND_ALIASES:
        original = tuple(ctx.meta.get("fwd_invocation_argv", (invoked,)))
        remaining = original[1:] if original and original[0] == invoked else ()
        actual_argv = [ui.COMMAND_NAME, *COMMAND_ALIASES[invoked], *remaining]
        invoked_argv = [ui.COMMAND_NAME, *original]
        ui.announce_alias(shlex.join(actual_argv), invoked=shlex.join(invoked_argv))
        return
    if invoked is None:
        from fwd.ops import attach as attach_ops

        ui.announce_alias(attach_ops.smart_default_command(restart=restart))


@app.callback()
def main(
    ctx: typer.Context,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
    version: Annotated[bool, typer.Option("--version", "-V", callback=_version_callback, is_eager=True, help=f"Print the installed {ui.command()} version and exit.")] = False,
) -> None:
    """Attach to this directory's session, launching its saved default or starting setup on first use.

    For a launch without saving config, use 'fwd up --target runpod', 'fwd up --target user@host', or an SSH alias.
    """
    if not ctx.resilient_parsing:
        _announce_root_alias(ctx, restart=restart)
    if not ctx.resilient_parsing and _interactive_terminal():
        from fwd import completion_setup, skill_setup

        completion_setup.offer_once()
        skill_setup.offer_once()
        skill_setup.update_if_needed()
    if ctx.invoked_subcommand is not None:
        return
    from fwd.ops import attach as attach_ops

    attach_ops.smart_default(restart=restart)


def _up(
    command: Annotated[list[str] | None, typer.Argument(help="Initial remote command; omit for a shell, or use 'claude'/'codex' for a synced coding-agent workflow.", autocompletion=complete_agent)] = None,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Configured target to use; defaults to default_target, or the existing session's target.", autocompletion=complete_target, rich_help_panel=PANEL_TARGET)] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="Override GPU selection for an explicitly GPU-enabled target (RunPod GPU id or Slurm --gres spec).", autocompletion=complete_gpu, rich_help_panel=PANEL_TARGET)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to a stable slug derived from this directory.", autocompletion=complete_session, rich_help_panel=PANEL_TARGET)] = None,
    attach: Annotated[bool, typer.Option("--attach", "-a", help="Attach after startup; non-agent commands stay local unless this is passed.", rich_help_panel=PANEL_TARGET)] = False,
    no_attach: Annotated[bool, typer.Option("--no-attach", help="Stay local even for magic agent commands that normally auto-attach in a terminal.", rich_help_panel=PANEL_TARGET)] = False,
    session: Annotated[bool, typer.Option("--session", help="Move the real transcript so claude resumes it; already the default, pass this only to re-enable it when config disables it.", rich_help_panel=PANEL_CLAUDE)] = False,
    handoff: Annotated[bool, typer.Option("--handoff", help="Summarize into HANDOFF.md instead of moving the transcript; replaces --session entirely.", rich_help_panel=PANEL_CLAUDE)] = False,
    user_config: Annotated[bool, typer.Option("--user-config", help="Upload your ~/.claude bundle (CLAUDE.md, skills, agents, commands, settings.json); never credentials or history.", rich_help_panel=PANEL_CLAUDE)] = False,
    creds: Annotated[bool, typer.Option("--creds", help="DANGER: write your live Claude OAuth token to the remote disk; prefer logging in inside the remote session.", rich_help_panel=PANEL_CLAUDE)] = False,
) -> None:
    """Provision/reuse a target, sync and bootstrap it, then start a shell or the requested command.

    Magic commands 'claude' and 'codex' sync their agent settings and auto-attach in an interactive terminal. Startup
    is persistent in tmux. Use --no-attach for a background launch and '--' before remote command flags.

    To add a new target, run 'fwd setup'.
    """
    from fwd.ops import launch as launch_ops

    initial_command = tuple(command or ())
    effective_attach = _should_attach(initial_command, attach=attach, no_attach=no_attach)
    launch_ops.launch(
        target=target,
        gpu=gpu,
        name=name,
        initial_command=initial_command,
        session=session,
        handoff=handoff,
        user_config=user_config,
        creds=creds,
        attach=effective_attach,
    )


UP_HELP = f"""Provision/reuse a target, sync and bootstrap it, then start a shell or the requested command.

Magic commands 'claude' and 'codex' sync their agent settings and auto-attach in an interactive terminal. Startup
is persistent in tmux. Use --no-attach for a background launch and '--' before remote command flags.

To add a new target, run {ui.command('setup')!r}.
"""

# Registered twice so `up` and its `launch` alias can never diverge.
app.command("up", help=UP_HELP, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_up)
app.command("launch", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_up)


def _attach(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
) -> None:
    """Attach to a running remote session's tmux, reconciling live backend status first.

    Replaces this process with 'ssh -t', so the remote session owns the terminal outright: resize, mouse reporting and ctrl-C behave exactly as a hand-typed ssh would. Detach with tmux's ctrl-b d; the session keeps running.
    """
    from fwd.ops import attach as attach_ops

    attach_ops.attach(name, restart=restart)


# Registered from one callback so the tmux-style `a` alias and `attach` always accept identical arguments.
app.command("attach")(_attach)
app.command("a", hidden=True)(_attach)


def _send(
    arguments: Annotated[list[str] | None, typer.Argument(help="Remote command after '--', agent plus message, or an existing task id.", autocompletion=complete_send_subject)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Cancel the remote task if it exceeds this many seconds.")] = None,
    detach: Annotated[bool, typer.Option("--detach", "-d", help="Start the task in the background and return immediately.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Stream until completion; this is the default unless --detach is passed.")] = False,
    stop: Annotated[bool, typer.Option("--stop", help="Cancel the selected task; with an agent message, cancel the active turn and send the replacement.")] = False,
    immediate: Annotated[bool, typer.Option("--immediate", help="Agent shorthand for --stop MESSAGE: cancel the active turn and immediately send this message.")] = False,
    list_only: Annotated[bool, typer.Option("--ls", help="List send tasks instead of starting one.")] = False,
    include_all: Annotated[bool, typer.Option("--all", help="With --ls, include completed, failed, and canceled tasks.")] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="With --ls, choose Rich, Markdown, or JSON output.", autocompletion=complete_output_format)] = OutputFormat.auto,
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
    code = send_ops.dispatch(
        tuple(arguments or ()),
        name=name,
        timeout=timeout,
        detach=detach,
        stop=stop,
        immediate=immediate,
        list_only=list_only,
        include_all=include_all,
        output_format=output_format,
    )
    raise typer.Exit(code)


SEND_HELP = f"""Start, follow, background, list, or cancel durable remote tasks.

Every command and agent turn runs in remote tmux and receives a task id. During streams, Ctrl-C cancels and Ctrl-B
backgrounds. Reattach with {ui.command('send TASK_ID')!r}, cancel with {ui.command('send TASK_ID --stop')!r}, and list
with {ui.command('send --ls')!r}. Never starts or restarts compute. Use '--' before raw commands:
{ui.command('send -- python train.py --epochs 10')}
"""

# Registered from one callback so the short alias cannot diverge from the primary command.
app.command("send", help=SEND_HELP, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_send)
app.command("s", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(_send)


@app.command("ls")
def ls_cmd(
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: auto uses Rich in a terminal and Markdown otherwise.", autocompletion=complete_output_format)] = OutputFormat.auto,
) -> None:
    """List every managed session with live status and cost queried from each backend."""
    from fwd.ops import lifecycle

    lifecycle.ls(output_format=output_format)


@app.command("push")
def push_cmd(
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
) -> None:
    """Mirror local changes up to the remote session; remote-only files are deleted unless sync.delete is off."""
    from fwd.ops import transfer

    transfer.push(name)


@app.command("pull")
def pull_cmd(
    paths: Annotated[list[str] | None, typer.Argument(help="Remote-relative paths to fetch; omit to pull the whole remote directory.")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
) -> None:
    """Bring remote changes back down to the local directory, additively — a pull never deletes local files."""
    from fwd.ops import transfer

    transfer.pull(name, tuple(paths or ()))


@app.command("diff")
def diff_cmd(
    target: Annotated[str | None, typer.Argument(help="Session name, configured target, or backend; defaults to this directory's session.", autocompletion=complete_diff_target)] = None,
    path: Annotated[str | None, typer.Argument(help="Project-relative file or directory; omit to compare the entire synced project.")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Print no diff; communicate identical/different/error through exit status only.")] = False,
) -> None:
    """Compare local and remote content: exit 0 if identical, 1 if different, and 2 on errors."""
    from fwd.ops import diff as diff_ops

    try:
        code = diff_ops.diff(target, path, quiet=quiet)
    except typer.Exit as exc:
        raise typer.Exit(max(2, exc.exit_code)) from exc
    except Exception as exc:
        ui.error(f"diff failed: {exc}")
        raise typer.Exit(2) from exc
    raise typer.Exit(code)


@app.command("stop", help=f"Kill remote tmux and ask the backend to suspend billable compute; storage preservation depends on the target.\n\nRestart with {ui.command('attach --restart')!r} or another {ui.command('up')!r}. SSH/Slurm project storage remains; on RunPod only an attached persistent volume survives, and CPU pod work is wiped.")
def stop_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
) -> None:
    """Kill remote tmux and ask the backend to suspend billable compute; storage preservation depends on the target.

    Restart with 'fwd attach --restart' or another 'fwd up'. SSH/Slurm project storage remains; on RunPod only an attached persistent volume survives, and CPU pod work is wiped.
    """
    from fwd.ops import lifecycle

    lifecycle.stop(name)


@app.command("rm", help=f"Destroy a session's target and forget the session. Irreversible — remote data is gone.\n\nThe confirmation defaults to no, so a scripted {ui.command('rm')!r} without --force safely does nothing.")
def rm_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.", autocompletion=complete_session)] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt; required non-interactively, where the prompt defaults to no.")] = False,
) -> None:
    """Destroy a session's target and forget the session. Irreversible — remote data is gone.

    The confirmation defaults to no, so a scripted 'fwd rm' without --force safely does nothing.
    """
    from fwd.ops import lifecycle

    lifecycle.remove(name, force=force)


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
) -> None:
    """Check local prerequisites (ssh, rsync, backend CLIs) and the reachability of each configured target.

    Exits non-zero when a check fails, so it doubles as a preflight in scripts.
    """
    from fwd import doctor

    raise typer.Exit(doctor.run_doctor(target, output_format=output_format))


@app.command("info")
def info_cmd(
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: auto uses Rich in a terminal and Markdown otherwise.", autocompletion=complete_output_format)] = OutputFormat.auto,
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
        output_format=output_format,
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
