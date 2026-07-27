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
"""

from __future__ import annotations

from typing import Annotated

import typer

from fwd import __version__, ui

app = typer.Typer(
    name="fwd",
    help="Forward your Claude Code session to a remote machine.",
    add_completion=True,
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def main(
    ctx: typer.Context,
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped compute without a prompt (required when not on a terminal).")] = False,
) -> None:
    """Run the smart default when no subcommand is given: attach to this directory's session, else launch one."""
    if ctx.invoked_subcommand is not None:
        return
    from fwd.ops import attach as attach_ops

    attach_ops.smart_default(restart=restart)


def _up(
    target: Annotated[str | None, typer.Option("--target", "-t", help="Target name from config.")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu", help="GPU type/id override for the backend.")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name (defaults to the directory name).")] = None,
    session: Annotated[bool, typer.Option("--session", help="Transfer the live transcript so claude resumes it (best-effort).")] = False,
    handoff: Annotated[bool, typer.Option("--handoff", help="Generate HANDOFF.md and have the remote session read it.")] = False,
    user_config: Annotated[bool, typer.Option("--user-config", help="Upload your ~/.claude config bundle (CLAUDE.md, skills, agents, commands).")] = False,
    creds: Annotated[bool, typer.Option("--creds", help="Copy local Claude credentials to the remote machine (writes a token to remote disk).")] = False,
    no_attach: Annotated[bool, typer.Option("--no-attach", help="Set everything up but do not attach.")] = False,
) -> None:
    """Provision (or reuse) a remote target, sync this directory, and start a Claude session there."""
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
    restart: Annotated[bool, typer.Option("--restart", "-y", help="Authorize restarting stopped compute without a prompt (required when not on a terminal).")] = False,
) -> None:
    """Attach to a running remote session's tmux."""
    from fwd.ops import attach as attach_ops

    attach_ops.attach(name, restart=restart)


@app.command("ls")
def ls_cmd() -> None:
    """List sessions with live status from each backend."""
    from fwd.ops import lifecycle

    lifecycle.ls()


@app.command("push")
def push_cmd(
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Sync local changes up to the remote session."""
    from fwd.ops import transfer

    transfer.push(name)


@app.command("pull")
def pull_cmd(
    paths: Annotated[list[str] | None, typer.Argument(help="Specific remote-relative paths; default pulls everything.")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Bring remote changes back down to the local directory."""
    from fwd.ops import transfer

    transfer.pull(name, tuple(paths or ()))


@app.command("stop")
def stop_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.")] = None,
) -> None:
    """Stop a session's remote tmux and suspend its target (data is preserved)."""
    from fwd.ops import lifecycle

    lifecycle.stop(name)


@app.command("rm")
def rm_cmd(
    name: Annotated[str | None, typer.Argument(help="Session name; defaults to this directory's session.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Destroy a session's target and forget it. Irreversible."""
    from fwd.ops import lifecycle

    lifecycle.remove(name, force=force)


@app.command("setup")
def setup_cmd() -> None:
    """Interactively create or update ~/.fwd/config.toml."""
    from fwd import wizard

    wizard.run_wizard()


@app.command("doctor")
def doctor_cmd(
    target: Annotated[str | None, typer.Option("--target", "-t", help="Check only this target.")] = None,
) -> None:
    """Check local prerequisites and target reachability."""
    from fwd import doctor

    raise typer.Exit(doctor.run_doctor(target))


@app.command("version")
def version_cmd() -> None:
    """Print the fwd version."""
    ui.console.print(__version__)


if __name__ == "__main__":
    app()
