# Claude Code and Codex transfer

## Contents

- Shared behavior
- Claude Code
- Codex
- Authentication
- Human handoff

## Shared behavior

Both magic commands synchronize the project, bootstrap remote tooling, start a persistent tmux session, and auto-attach only from a human terminal:

```sh
fwd up --target TARGET --agent claude
fwd up --target TARGET --agent codex
```

Agents should run them without `--attach`. Non-interactive detection keeps the launch in the background.

After launch, communicate with the running remote conversation through durable send tasks:

```sh
fwd send agent --detach "run the tests and fix failures"
fwd send --ls --format json
fwd send TASK_ID
fwd send TASK_ID --stop
```

Use `--immediate MESSAGE` when a new instruction should cancel and replace the active turn. A plain message queues
behind an active managed turn. Send-task cancellation never stops the fwd session or its remote compute.

## Claude Code

The default `--session` mode moves the real local transcript and asks remote Claude to resume it. Transfer failures degrade to a plain Claude launch with a warning.

`--handoff` replaces transcript transfer with a generated `HANDOFF.md`; use it only when the user requests a summary handoff. `--user-config` uploads portable Claude configuration while excluding credentials and history.

## Codex

Codex receives portable settings, configuration, and skills. It does not receive the current Codex transcript or authentication. Tell the user that the remote agent begins with the synchronized project and personal workflow configuration, not the local conversation.

Once remote Codex has started, `fwd send agent MESSAGE` resumes its most recent remote project conversation through
Codex's JSONL non-interactive interface. Human terminals receive concise text/tool events; non-interactive callers
receive the original machine-readable event stream.

Use JSON output and non-attaching commands when Codex is driving fwd:

```sh
fwd doctor --format json
fwd up codex --target TARGET
fwd ls --format json
fwd diff -q TARGET
```

## Authentication

Prefer logging in on the remote machine. Never copy credentials automatically. `--creds` writes a live Claude OAuth token to remote disk and requires explicit authorization in the current conversation. Codex authentication is never copied.

## Human handoff

After launch, report the exact resolved target/session and tell the human:

```sh
fwd attach SESSION
```

If the user wants a result without attaching, use `fwd send agent`, `fwd send -- COMMAND`, or `fwd pull`.
