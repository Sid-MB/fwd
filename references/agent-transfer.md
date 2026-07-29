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

On RunPod, fwd prepares agent state before tool installation because `/root` is erased on every stop. GPU pods keep
`~/.claude` or `~/.codex` beneath the persistent tool prefix and recreate the home symlink on relaunch, so remote
authentication, conversations, settings, and Codex's managed standalone payload survive. CPU pods have no persistent
volume; their full relaunch reconstructs local inputs but cannot retain state that existed only on the stopped pod.

After launch, communicate with the running remote conversation through durable send tasks:

```sh
fwd send agent --detach "run the tests and fix failures"
fwd send --ls --json
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
fwd doctor --json
fwd up codex --target TARGET
fwd ls --json
fwd diff -q TARGET
```

## Authentication

Prefer logging in on the remote machine for coding-agent authentication. `--creds` writes a live Claude OAuth token to remote disk and requires explicit authorization in the current conversation. Codex authentication is never copied.

GitHub authentication defaults on for development VMs and can be disabled with `[github] auth = false` or
`--no-setup-github`. Fwd resolves `GH_TOKEN`, `GITHUB_TOKEN`, the active local gh account, Git's credential helper,
then `~/.netrc`; an interactive caller can paste a PAT as the final fallback. It streams the selected credential to remote standard
input, configures HTTPS Git access, and persists the remote credential on RunPod volumes. The token never enters
project files, argv, logs, config, or session state. A direct `fwd send git push`, any sent coding-agent turn, and
`fwd attach` can repair an older session in place. Do not describe `fwd pull && git push` as a way to transfer a remote
commit: pull intentionally omits `.git/`. It can retrieve uncommitted files for a new local commit; preserving an
existing remote commit requires a remote push or an explicitly exported patch or Git bundle.

## Human handoff

After launch, report the exact resolved target/session and tell the human:

```sh
fwd attach SESSION
```

If the user wants a result without attaching, use `fwd send agent`, `fwd send -- COMMAND`, or `fwd pull`.
