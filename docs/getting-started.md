# Getting started

## What fwd does

`fwd` moves a local coding project to remote compute. It provisions or connects to a target, synchronizes the project, prepares detected Python, JavaScript, or Swift tooling, and starts a persistent shell, command, Claude Code, or Codex session in tmux.

The remote session survives a dropped SSH connection or closed laptop. You can reconnect later, run durable background tasks, and pull results back to the local checkout.

## Install

```sh
uv tool install fwdit
```

To try fwd without installing it:

```sh
uvx --from fwdit fwd --help
```

Local requirements are Python 3.12+, `ssh`, and `rsync`. Provider targets may require their own authentication or CLI; `fwd doctor` reports what is missing.

## Launch a session

From a project directory:

```sh
fwd                         # attach to this project's session, or create one interactively
fwd up runpod codex         # launch Codex on a RunPod target without attaching
fwd up --target work        # launch the configured default command on target "work"
fwd up -- python train.py   # launch and stream a durable command
```

Bare `fwd` is the human-friendly reuse workflow. It attaches to an unambiguous session or creates one interactively. Scripts and coding agents should use explicit, non-attaching `fwd up` forms.

Detach from tmux with `Ctrl-B D`; the remote process continues running. Reconnect with:

```sh
fwd ls
fwd attach SESSION
```

## Configure a target

You can use a direct SSH host, an OpenSSH alias, or built-in CPU RunPod defaults without saving a target:

```sh
fwd up --target user@example.com
fwd up --target my-ssh-alias
fwd up runpod
```

For reusable or provider-specific settings, run:

```sh
fwd setup
```

`fwd setup` only writes configuration; it does not provision compute. See [Configuration and backends](configuration.md) for non-interactive setup and provider requirements.

## Everyday workflow

```sh
fwd ls                     # inspect this project's sessions
fwd send -- pytest -q      # run durable work in the current remote session
fwd diff                   # compare synchronized local and remote files
fwd pull outputs/          # retrieve selected results without deleting local files
fwd stop                   # stop the session after the remote worktree is clean
fwd rm                     # permanently destroy its remote resources
```

Continue with [Commands and lifecycle](commands.md), or read [Coding agents](agents.md) when moving a Claude Code or Codex workflow.
