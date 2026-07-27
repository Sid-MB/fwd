---
name: fwd
description: Move a coding project or active Claude Code/Codex workflow to remote compute with fwd. Use for remote development, CPU or GPU VMs, SSH hosts and aliases, RunPod pods, Slurm clusters, extra compute or memory, cluster-local data, persistent remote agents, durable remote commands, file synchronization, sync diffs, attaching, stopping, or destroying remote sessions.
---

# fwd remote development

Use `fwd` to provision or reuse a remote machine, synchronize the current project, bootstrap its tools, and run a persistent shell, command, Claude Code, or Codex session in tmux.

If `fwd` is not on `PATH`, install the GitHub version with `uv tool install git+https://github.com/Sid-MB/fwd`. If `uv` is unavailable, tell the user that Python 3.12+, `uv`, `ssh`, and `rsync` are the local prerequisites instead of improvising another installer.

## Invocation

- Codex CLI/IDE: `$fwd continue this project on my RunPod CPU target`
- Claude Code: `/fwd continue this project on my RunPod CPU target`
- Any supporting agent may invoke the skill implicitly for matching natural-language requests.

Treat the text following the skill name as the user's intent. Do not require rigid syntax.

## Core workflow

1. Run `fwd doctor --json` when diagnosing prerequisites or a failed target.
2. Determine the requested target, compute type, and initial command. CPU is the default unless the user asks for a GPU.
3. Discover configuration with `fwd config`, `fwd config --schema`, or `fwd config --example BACKEND`; never guess fields.
4. Launch non-interactively with `fwd up --target TARGET --agent AGENT` or `fwd up TARGET -- COMMAND...`. Exact `claude` and `codex` agents enable their agent-specific synchronization.
5. Let fwd detect Python, JavaScript, and Swift Package Manager projects, reuse working remote project/agent tools, and install only missing declared requirements. For an unsupported or private toolchain, add an idempotent project-owned `.fwd/setup.sh`; it runs after built-in dependency setup.
6. Verify state with `fwd ls --json` and synchronization with `fwd diff -q [TARGET]`.
7. Tell the user how to attach or retrieve results. Do not take over the agent's terminal.

Use `fwd send -- COMMAND` for a durable remote command without starting or restarting compute. It streams by default;
use `--detach` for background work, `fwd send --ls --json` to discover task IDs, `fwd send TASK_ID` to follow,
and `fwd send TASK_ID --stop` to cancel only that task.

Use `fwd send agent MESSAGE` to continue the Claude/Codex conversation already running in the selected session.
Normal messages queue behind an active turn. `--immediate MESSAGE` or `--stop MESSAGE` cancels that turn and sends a
replacement; `--stop` alone cancels without ending the agent session.

## Agent safety rules

- Prefer `--json` for `fwd ls`, `fwd doctor`, and `fwd info`; progress and diagnostics stay on stderr.
- Never run bare `fwd`, root-selector forms such as `fwd runpod`, `fwd attach`, `fwd a`, `fwd up --connect`, or `fwd up --attach` as a tool call because connect/attach forms take over a human terminal. In non-interactive mode `--connect` deliberately errors instead of provisioning.
- Do not pass `--restart` unless the user authorizes restarting stopped billable compute.
- Do not pass `--creds` unless the user explicitly authorizes copying live Claude credentials in this conversation.
- Do not run `fwd rm --force` unless the user explicitly asks to destroy the remote resource. Never run `fwd rm --all --force` unless the user explicitly asks to destroy every tracked remote resource.
- Run `fwd uninstall --force` only when the user explicitly requests local fwd removal and understands that tracked remote resources are not destroyed; prefer `fwd rm --all` first.
- Prefer `fwd diff -q` before deciding whether to push or pull. Exit 0 means synchronized, 1 means different, and 2 means an error.
- In non-interactive environments, use explicit flags. Never invoke a setup wizard or invent a missing target.
- Missing `npx`, the optional `skills` CLI, or an unsuccessful skill refresh must not block normal fwd commands.

## Common operations

```sh
fwd up --target runpod                 # CPU RunPod, layered default command, stay local
fwd up --new --target runpod           # provision a separate session instead of reusing this project
fwd up --target work --agent codex     # sync Codex settings/skills and start remote Codex
fwd up work claude                     # positional target + agent; transfer the Claude transcript
fwd send -- pytest -q                  # durable task; stream output and return its exit status
fwd send --detach -- pytest -q         # start in remote tmux and return immediately
fwd send --ls --json                   # inspect active command and agent tasks
fwd send agent --detach "fix tests"    # queue work in the running remote agent
fwd diff -q                            # machine-readable sync check
fwd push                               # mirror local changes to the remote
fwd pull outputs/                      # retrieve selected remote results
fwd ls --json                          # inspect live sessions
fwd ls --all-projects --json           # inspect sessions across every local project
fwd stop                               # stop the session and suspend supported compute
```

After a successful background launch, hand back `fwd attach NAME` (or `fwd a NAME`) for the human to run.

## Detailed references

- Read [references/targets-and-config.md](references/targets-and-config.md) for target resolution, setup, defaults, and backend-specific configuration.
- Read [references/commands-and-lifecycle.md](references/commands-and-lifecycle.md) for launch, send, diff, synchronization, cancellation, stopping, and destruction semantics.
- Read [references/agent-transfer.md](references/agent-transfer.md) before launching Claude Code or Codex, especially when transcript, settings, skills, authentication, or attachment behavior matters.

`fwd --help` and `fwd COMMAND --help` are the authoritative references for the installed version.
