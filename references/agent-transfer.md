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
fwd up claude --target TARGET
fwd up codex --target TARGET
```

Agents should run them without `--attach`. Non-interactive detection keeps the launch in the background.

## Claude Code

The default `--session` mode moves the real local transcript and asks remote Claude to resume it. Transfer failures degrade to a plain Claude launch with a warning.

`--handoff` replaces transcript transfer with a generated `HANDOFF.md`; use it only when the user requests a summary handoff. `--user-config` uploads portable Claude configuration while excluding credentials and history.

## Codex

Codex receives portable settings, configuration, and skills. It does not receive the current Codex transcript or authentication. Tell the user that the remote agent begins with the synchronized project and personal workflow configuration, not the local conversation.

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

If the user wants a result without attaching, use `fwd send` or `fwd pull`.
