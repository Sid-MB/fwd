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
from typing import Annotated

import typer

from fwd import __version__, ui

# Panel titles for `fwd up`. Kept as constants so the two groups are named identically everywhere they are referenced.
PANEL_TARGET = "Target & session"
PANEL_CLAUDE = "Claude context"
CONFIG_DOCS_URL = "https://github.com/Sid-MB/fwd#configuration"

app = typer.Typer(
    name="fwd",
    help="Forward your Claude Code session to a remote machine: provision, sync, carry the transcript, attach.",
    epilog=f"Bare 'fwd' attaches to this directory's session, launches its saved default, or starts setup on first use. For a zero-config launch, use 'fwd up --target runpod', 'fwd up --target user@host', or an SSH alias. Learn config with 'fwd config --example' or 'fwd config --schema'; guide: {CONFIG_DOCS_URL}. Diagnose with 'fwd doctor'.",
    add_completion=True,
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def main(
    ctx: typer.Context,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
) -> None:
    """Attach to this directory's session, launching its saved default or starting setup on first use.

    For a launch without saving config, use 'fwd up --target runpod', 'fwd up --target user@host', or an SSH alias.
    """
    if ctx.invoked_subcommand is not None:
        return
    from fwd.ops import attach as attach_ops

    attach_ops.smart_default(restart=restart)


def _up(
    target: Annotated[str | None, typer.Option("--target", "-t", help="Configured target to use; defaults to default_target, or the existing session's target.", rich_help_panel=PANEL_TARGET)] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="Override the GPU for this launch only (RunPod GPU id, or a Slurm --gres spec).", rich_help_panel=PANEL_TARGET)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to a stable slug derived from this directory.", rich_help_panel=PANEL_TARGET)] = None,
    no_attach: Annotated[bool, typer.Option("--no-attach", help="Provision, sync and start Claude remotely but stay local instead of attaching.", rich_help_panel=PANEL_TARGET)] = False,
    session: Annotated[bool, typer.Option("--session", help="Move the real transcript so claude resumes it; already the default, pass this only to re-enable it when config disables it.", rich_help_panel=PANEL_CLAUDE)] = False,
    handoff: Annotated[bool, typer.Option("--handoff", help="Summarize into HANDOFF.md instead of moving the transcript; replaces --session entirely.", rich_help_panel=PANEL_CLAUDE)] = False,
    user_config: Annotated[bool, typer.Option("--user-config", help="Upload your ~/.claude bundle (CLAUDE.md, skills, agents, commands, settings.json); never credentials or history.", rich_help_panel=PANEL_CLAUDE)] = False,
    creds: Annotated[bool, typer.Option("--creds", help="DANGER: write your live Claude OAuth token to the remote disk; prefer logging in inside the remote session.", rich_help_panel=PANEL_CLAUDE)] = False,
) -> None:
    """Provision (or reuse) a remote target, sync this directory, and start a Claude session there.

    Also the repair command: every stage is idempotent, so re-running after a failed launch resumes where it stopped rather than duplicating a pod, a job or a sync.
    """
    from fwd.ops import launch as launch_ops

    launch_ops.launch(
        target=target,
        gpu=gpu,
        name=name,
        session=session,
        handoff=handoff,
        user_config=user_config,
        creds=creds,
        attach=not no_attach,
    )


# Registered twice so `up` and its `launch` alias can never diverge.
app.command("up")(_up)
app.command("launch", hidden=True)(_up)


@app.command("attach")
def attach_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.")] = None,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped (billable) compute without prompting; required when stdin is not a terminal.")] = False,
) -> None:
    """Attach to a running remote session's tmux, reconciling live backend status first.

    Replaces this process with 'ssh -t', so the remote session owns the terminal outright: resize, mouse reporting and ctrl-C behave exactly as a hand-typed ssh would. Detach with tmux's ctrl-b d; the session keeps running.
    """
    from fwd.ops import attach as attach_ops

    attach_ops.attach(name, restart=restart)


@app.command("ls")
def ls_cmd() -> None:
    """List every fwd session with live status and cost queried from each backend."""
    from fwd.ops import lifecycle

    lifecycle.ls()


@app.command("push")
def push_cmd(
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Mirror local changes up to the remote session; remote-only files are deleted unless sync.delete is off."""
    from fwd.ops import transfer

    transfer.push(name)


@app.command("pull")
def pull_cmd(
    paths: Annotated[list[str] | None, typer.Argument(help="Remote-relative paths to fetch; omit to pull the whole remote directory.")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Bring remote changes back down to the local directory, additively — a pull never deletes local files."""
    from fwd.ops import transfer

    transfer.pull(name, tuple(paths or ()))


@app.command("stop")
def stop_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Kill a session's remote tmux and suspend its target to stop billing; synced data is preserved.

    Restart it later with 'fwd attach --restart' or another 'fwd up'. RunPod caveat: only the volume survives a stop, so anything outside remote_base on a container disk is lost.
    """
    from fwd.ops import lifecycle

    lifecycle.stop(name)


@app.command("rm")
def rm_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.")] = None,
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


@app.command("config")
def config_cmd(
    backend: Annotated[ExampleBackend | None, typer.Argument(help="With --example, which backend to show; defaults to all.")] = None,
    example: Annotated[bool, typer.Option("--example", help="Print a commented reference config generated from fwd's own schema instead of your effective one.")] = False,
    schema: Annotated[bool, typer.Option("--schema", help="Print the complete machine-readable JSON Schema for config files.")] = False,
) -> None:
    """Show effective config, a commented TOML reference, or machine-readable JSON Schema.

    Outputs go to stdout without terminal formatting. Guide: https://github.com/Sid-MB/fwd#configuration
    """
    if example and schema:
        ui.die("--example and --schema are mutually exclusive")
    if backend is not None and not example:
        ui.die(f"'fwd config {backend.value}' is only meaningful with --example; run 'fwd config --example {backend.value}' for a reference, or 'fwd config' for your effective config")
    from fwd.ops import configcmd

    configcmd.show((backend or ExampleBackend.all).value if example else None, schema=schema)


@app.command("setup")
def setup_cmd(
    backend: Annotated[str | None, typer.Option("--backend", help="Backend to configure: ssh, runpod, or slurm. Required in non-interactive mode.")] = None,
    target_name: Annotated[str | None, typer.Option("--target-name", help="Local fwd label for this connection; defaults to the backend name.")] = None,
    host: Annotated[str | None, typer.Option("--host", help="SSH hostname, IP, or Host alias from ~/.ssh/config.")] = None,
    login_host: Annotated[str | None, typer.Option("--login-host", help="Slurm cluster login hostname or SSH alias.")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Remote username; optional for SSH aliases.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="SSH port.")] = None,
    key_path: Annotated[str | None, typer.Option("--key-path", help="SSH identity file; omit to use your SSH agent/config.")] = None,
    proxy_jump: Annotated[str | None, typer.Option("--proxy-jump", help="SSH bastion hop, as user@host.")] = None,
    remote_base: Annotated[str | None, typer.Option("--remote-base", help="Remote parent directory for project checkouts.")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="Default RunPod GPU id.")] = None,
    image: Annotated[str | None, typer.Option("--image", help="Default RunPod container image.")] = None,
    volume_gb: Annotated[int | None, typer.Option("--volume-gb", help="RunPod persistent volume size in GB.")] = None,
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
            "remote_base": remote_base,
            "gpu": gpu,
            "image": image,
            "volume_gb": volume_gb,
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
    target: Annotated[str | None, typer.Option("--target", "-t", help="Check only this target instead of every configured one.")] = None,
) -> None:
    """Check local prerequisites (ssh, rsync, backend CLIs) and the reachability of each configured target.

    Exits non-zero when a check fails, so it doubles as a preflight in scripts.
    """
    from fwd import doctor

    raise typer.Exit(doctor.run_doctor(target))


@app.command("version")
def version_cmd() -> None:
    """Print the installed fwd version and exit."""
    ui.console.print(__version__)


if __name__ == "__main__":
    app()
