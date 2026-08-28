---
name: fwd
description: Move a coding project or active Claude Code/Codex workflow to remote compute with fwd. Use for remote development, SSH, RunPod, Lambda Cloud, Slurm, extra CPU/GPU/memory, persistent remote agents, durable commands, synchronization, attaching, stopping, or destroying remote sessions.
---

# fwd remote development

Use `fwd` to provision or reuse remote compute, synchronize the current project, prepare its tools, and run a persistent coding agent or command in tmux. Invoking this skill means the user wants fwd used.

## Workflow

1. Preserve the user's task, requested target, hardware, and shutdown requirements. Use the caller's agent (`codex` from Codex, `claude` from Claude) unless the user specifies another.
2. If a provider machine must be chosen, inspect exact values before launch with `fwd up --machines` or `fwd up TARGET --machines`. Prefer CPU unless the user requests a GPU; never guess or abbreviate a provider identifier.
3. Launch without taking over the terminal: `fwd up --detach --agent AGENT`, adding `--target TARGET` and `--machine MACHINE` only when selected. Never use a bare/reuse/attach form as a tool call.
4. Read the exact session name and live state from `fwd ls --json`.
5. Send the preserved task with `fwd send --name SESSION agent "TASK"`. Stream and iterate by default; use `--detach` only when the user asks to background the task.
6. Inspect changed work with `fwd diff -q SESSION`, use `fwd diff SESSION` when details matter, and retrieve accepted files with `fwd pull --name SESSION`.
7. Report the result and exact commands the user may need, especially `fwd attach SESSION`, `fwd send --name SESSION --ls`, and `fwd stop SESSION`.

If setup is required, follow the exact flags printed by fwd. Do not open an interactive setup wizard or invent target values.

For a requested shell command instead of agent work, use `fwd send --name SESSION -- COMMAND...`. Reattach with `fwd send --name SESSION TASK_ID`; cancel only that task with `fwd send --name SESSION TASK_ID --stop`.

If `fwd` is unavailable, install the published distribution with `uv tool install fwdit`. If `uv` is unavailable, report that Python 3.12+, `uv`, `ssh`, and `rsync` are required instead of improvising another installer.

## Safe automation

- Prefer `--json` for `fwd ls`, `fwd doctor`, `fwd info`, and task listings. Diagnostics remain on stderr.
- Never run bare `fwd`, `fwd TARGET`, `fwd attach`, `fwd a`, `fwd up --reuse`, or `fwd up --attach` as a tool call; they can take over a human terminal. Hand the exact attach command to the user.
- Do not use `--restart` unless the user authorizes restarting stopped billable compute.
- Do not use `--creds` unless the user authorizes copying live Claude credentials. GitHub setup defaults on; use `--no-setup-github` when credentials must stay local.
- Never force `stop`, `rm`, or stop-after past a dirty or unreachable worktree unless the user explicitly accepts losing remote-only changes.
- Never run `fwd rm --all --force` unless the user explicitly requests destruction of every tracked remote resource.
- Run `fwd uninstall --force` only for an explicit local-uninstall request after explaining that it does not destroy remote resources; prefer `fwd rm --all` first.
- Prefer `fwd diff -q` before push or pull. Exit 0 means synchronized, 1 different, and 2 error.
- Continuous sync (`fwd sync on`) is opt-in and off by default. Enable it only when the user asks for automatic two-way syncing, and tell them `.git` is excluded from it, so `fwd push`/`fwd pull` remain necessary for repository state.
- If upload exceeds `sync.max_size_gb`, confirm the directory is intentional before using the exact project-scoped limit command printed by fwd.
- Missing `npx` or a failed optional skill refresh must not block normal fwd commands.
- If preparation fails after provisioning and sync, give the human `fwd attach SESSION --raw` for a recovery shell. This does not authorize restarting stopped compute.

## Stop after work

For supported backends, use remote-owned shutdown so it survives local disconnection:

```sh
fwd up --stop-after -- COMMAND...
fwd send --name SESSION --stop-after agent "TASK"
fwd send --name SESSION stopafter
fwd send --name SESSION cancel stopafter
```

Confirm the lifecycle task with `fwd send --name SESSION --ls --json`. Stop-after refuses a dirty remote worktree; never force it without explicit acceptance of data loss.

Lambda does not support remote stop-after because its broad API key stays local. Retrieve durable results, then tell the user to run `fwd stop SESSION` from a connected machine.

## Useful commands

```sh
fwd up --detach --target runpod --agent codex
fwd up --detach --new --target runpod --agent codex
fwd up -- COMMAND...
fwd send --name SESSION agent "TASK"
fwd send --name SESSION -- COMMAND...
fwd send --name SESSION --ls --json
fwd diff -q SESSION
fwd pull --name SESSION outputs/
fwd sync status --json
fwd ls --all-projects --json
fwd doctor --json
```

## References

- Read [targets and configuration](references/targets-and-config.md) for resolution, setup, defaults, machines, and backend behavior.
- Read [commands and lifecycle](references/commands-and-lifecycle.md) for launch, durable tasks, synchronization, ports, attachment, stopping, and destruction.
- Read [agent transfer](references/agent-transfer.md) before launching Claude Code or Codex when transcripts, settings, skills, authentication, or remote control matter.

`fwd --help` and `fwd COMMAND --help` are authoritative for the installed version.
