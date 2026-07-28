---
name: fwd
description: Move a coding project or active Claude Code/Codex workflow to remote compute with fwd. Use for remote development, CPU or GPU VMs, SSH hosts and aliases, RunPod pods, Slurm clusters, extra compute or memory, cluster-local data, persistent remote agents, durable remote commands, file synchronization, sync diffs, attaching, stopping, or destroying remote sessions.
---

# fwd remote development

Use `fwd` to provision or reuse a remote machine, synchronize the current project, bootstrap its tools, and run a persistent shell, command, Claude Code, or Codex session in tmux. The invocation of this skill indicates that the user wants `fwd` to be used.

## Installation
If `fwd` is not on `PATH`, install the GitHub version with `uv tool install git+https://github.com/Sid-MB/fwd`. If `uv` is unavailable, tell the user that Python 3.12+, `uv`, `ssh`, and `rsync` are the local prerequisites instead of improvising another installer.

## Invocation

- Codex CLI/IDE: `$fwd implement TODO.md`
- Codex CLI/IDE: `$fwd build, test, and iterate on runpod`
- Claude Code: `/fwd implement TODO.md`
- Any supporting agent may invoke the skill implicitly for matching natural-language requests.

Treat the text following the skill name as work to perform remotely, not as literal shell argv. Extract a named target such as `runpod`; preserve the rest as the task.

## Core workflow

1. Choose the caller's agent—`codex` from Codex or `claude` from Claude—unless the user names another. Launch it with `fwd up --agent AGENT` plus `--target TARGET` only when requested; fwd handles provisioning, sync, tools, and persistence.
2. Read the exact session name from `fwd ls --json`, then send the preserved task with `fwd send --name SESSION agent "TASK"`. Let it stream and iterate; use `--detach` only when the user asks to background the work.
3. If setup is required, follow fwd's exact error flags. Do not preconfigure a target or guess fields.
4. When work changes project files, inspect `fwd diff SESSION`, then retrieve the accepted result with `fwd pull --name SESSION`. Report the result and the exact attach/send/stop commands the user may need.

For an explicitly requested shell command instead of coding-agent work, use `fwd send --name SESSION -- COMMAND...`. List or resume background tasks with `fwd send --ls --json` and `fwd send TASK_ID`; cancel only that task with `fwd send TASK_ID --stop`.

When the user wants disposable compute to stop after work finishes, arm remote-owned shutdown with `fwd up --stop-after -- COMMAND...` for initial command work or `fwd send --name SESSION --stop-after agent "TASK"` for an agent turn. To stop after work that is already active, run `fwd send --name SESSION stopafter`. Confirm the lifecycle task in `fwd send --ls --json`; disarm it before shutdown begins with `fwd send --name SESSION cancel stopafter`. Never substitute a local delayed `fwd stop`: the local process may disconnect or shut down before it runs.

## Agent safety rules

Run `fwd doctor --json` when diagnosing prerequisites or a failed target.
- Prefer `--json` for `fwd ls`, `fwd doctor`, and `fwd info`; progress and diagnostics stay on stderr.
- Never run bare `fwd`, root-selector forms without an explicit arbitrary command such as `fwd runpod`, `fwd attach`, `fwd a`, `fwd up --reuse`, or `fwd up --attach` as a tool call because reuse/attach forms take over a human terminal. A root form with an explicit command uses managed-task behavior on an existing match, but in non-interactive mode it still refuses to provision; prefer an explicit non-attaching `fwd up` invocation in agent workflows.
- Do not pass `--restart` unless the user authorizes restarting stopped billable compute.
- Do not pass `--creds` unless the user explicitly authorizes copying live Claude credentials in this conversation.
- Do not run `fwd rm --force` unless the user explicitly asks to destroy the remote resource. Never run `fwd rm --all --force` unless the user explicitly asks to destroy every tracked remote resource.
- Run `fwd uninstall --force` only when the user explicitly requests local fwd removal and understands that tracked remote resources are not destroyed; prefer `fwd rm --all` first.
- Prefer `fwd diff -q` before deciding whether to push or pull. Exit 0 means synchronized, 1 means different, and 2 means an error.
- Launch and push reject filtered uploads above `sync.max_size_gb` (1 GB by default) before provisioning or transfer. If fwd refuses a deliberately large project, use the exact project-scoped `fwd config set` command in its error only after confirming the selected directory is intentional.
- In non-interactive environments, use explicit flags. Never invoke a setup wizard or invent a missing target.
- Missing `npx`, the optional `skills` CLI, or an unsuccessful skill refresh must not block normal fwd commands.
- If launch preparation fails after the target is running and synced, hand the human `fwd attach SESSION --raw` to
  open a plain recovery shell without rerunning tool or dependency installation. The human can repair the remote
  environment, exit the recovery shell, and rerun the normal launch; `--raw` does not authorize restarting stopped
  billable compute.

## Primitives

### Backends

A backend implements one kind of remote compute: `runpod` provisions RunPod pods, `ssh` connects to an existing SSH host, and `slurm` submits work through a Slurm cluster. Backends define their configuration fields, defaults, setup choices, provisioning behavior, and lifecycle operations.

### Targets

A target is a named, reusable backend configuration—not a running machine or session. It describes how to provision or reach compute, including values such as an SSH alias, RunPod compute type, or Slurm allocation. Inspect configured targets and their source files with `fwd config`; inspect all valid fields with `fwd config --schema` or `fwd config --example BACKEND`. Add a target interactively with `fwd setup`, or non-interactively with `fwd setup --backend BACKEND` plus the required flags shown by `fwd setup --help`.

Built-in `runpod` defaults and direct SSH forms such as `user@host` or an OpenSSH host alias can work without a saved target. Prefer CPU compute unless the user explicitly requests a GPU.

### Sessions

A session is one locally tracked remote project runtime created by `fwd up`. It binds a local project directory to a target, provider resource or SSH endpoint, synchronized remote directory, and primary tmux session. Its session name identifies that concrete runtime; pass `--name NAME` to choose one or `--new` to create another instead of reusing the current project's saved session.

Use `fwd ls --json` to discover session names and live state. Existing-session commands accept target labels and backend names as aliases: `attach`, `stop`, `rm`, and `diff` accept them positionally, while `send`, `push`, and `pull` accept them through `--name`. Exact session names win. A sole saved alias match is unambiguous; with several matches, fwd selects the sole running or pending target only when every other candidate's status is known, otherwise it requires an exact session name. Stopping a session ends its primary process and suspends supported compute; removing it destroys its remote resource and local tracking.

### Startup processes and agents

Every session has one primary persistent process started by `fwd up`. It may be a shell, the layered `default_command`, or a registered agent. An explicit arbitrary command—including a root shortcut such as `fwd runpod pytest -q`—selects or provisions the session, then uses the same durable task manager as `fwd send -- COMMAND`; it therefore appears in `fwd send --ls`. The primary pane remains a shell unless `--attach` explicitly runs the command there and enters tmux. `claude` and `codex` are registered agents with agent-specific configuration and conversation-transfer behavior; use `--agent NAME` when a positional target or command could be ambiguous.

Attaching connects the human terminal to this primary tmux session. Detaching leaves the process and remote compute running.
When preparation failed before the primary tmux session was created, `fwd attach SESSION --raw` creates a plain
login-shell tmux in the synced project and attaches to it without repeating launch preparation.

### Send tasks

A send task is durable work started inside an already-running session with `fwd send`; it never provisions or restarts compute. Each command or agent turn runs through the session's remote tmux task manager and receives a task ID and log. Stream until completion by default; after two seconds Ctrl-C cancels the remote task and Ctrl-B backgrounds only the local viewer. Use `--detach` to background immediately, `fwd send TASK_ID` to reattach, `fwd send --ls --all --json` for task history, `fwd send cancel` for queued work, and `fwd send cancel all` for every active task.

`--stop-after` atomically adds a remotely owned lifecycle task after new work, while `fwd send stopafter` queues it after all current work and `fwd send cancel stopafter` disarms it. The lifecycle task and dependencies appear in `fwd send --ls`; active shutdown also appears in `fwd ls`. RunPod and Slurm stop their owned compute, while SSH stops only fwd-owned tmux sessions and does not power off an external machine. Registered remote agents receive instructions for the literal `stopafter` helper and may run it only as their final action after requested work and durable output are complete; `stopafter --cancel` disarms it before shutdown begins. Canceling an ordinary task stops only that work, while `fwd stop` affects the entire session and target.

### Synchronization

The sync domain is the filtered project tree governed by `.gitignore`, `.fwdignore`, and configured exclusions. Launch and `fwd push` mirror local content to the remote project through rsync or tar fallback; stale synchronized files are deleted while excluded remote environments remain. Linked Git worktrees whose `.git` file points outside the project are not supported. `fwd pull` copies remote results back additively; `fwd diff` compares filtered snapshots without changing either side. Before any upload, `sync.max_size_gb` limits this tree to 1 GB by default. Normal measurement matches rsync's filters; tar fallback runs a second conservative check that also counts files hidden only by `.gitignore`.

### Toolchains and requirements

A toolchain detects a project ecosystem such as Python, JavaScript, or Swift Package Manager and declares its remote setup steps and tool requirements. Requirements reuse compatible tools already present on the remote and install only missing dependencies, including prerequisite tools. Use an idempotent `.fwd/setup.sh` for project-specific setup that no built-in toolchain covers.


## Common operations

```sh
fwd up --target runpod                 # CPU RunPod, layered default command, stay local
fwd up --new --target runpod           # provision a separate session instead of reusing this project
fwd up --target work_cluster --agent codex     # sync Codex settings/skills and start remote Codex
fwd up work_cluster claude                     # positional target + agent; transfer the Claude transcript
fwd up -- pytest -q                    # provision, stream a durable task, and return its exit status
fwd up --stop-after -- pytest -q       # run tests, then stop remotely even if the local computer disconnects
fwd up -a -- bash                      # provision and attach directly to the primary pane
fwd send -- pytest -q                  # durable task; stream output and return its exit status
fwd send --stop-after -- pytest -q     # queue remote stop after this task
fwd send stopafter                     # queue remote stop after all active tasks
fwd send cancel stopafter              # cancel queued remote shutdown
fwd send --detach -- pytest -q         # start in remote tmux and return immediately
fwd send --ls --json                   # inspect active command and agent tasks
fwd send --ls --all --json             # include completed, failed, and canceled task history
fwd send cancel                        # cancel queued tasks
fwd send cancel all                    # cancel every active task without stopping the session
fwd send agent --detach "fix tests"    # queue work in the running remote agent
fwd send agent --stop "try another approach"  # interrupt the active turn and send a replacement
fwd diff                               # show local/remote project differences
fwd diff -q                            # machine-readable sync check
fwd push                               # mirror local changes to the remote
fwd pull outputs/                      # retrieve selected remote results
fwd ls --json                          # inspect live sessions
fwd ls --all-projects --json           # inspect sessions across every local project
fwd stop                               # stop the session and suspend supported compute
fwd --help
```

After a successful background launch, hand back `fwd attach NAME` (or `fwd a NAME`) for the human to run.

## Detailed references

- Read [references/targets-and-config.md](references/targets-and-config.md) for target resolution, setup, defaults, and backend-specific configuration.
- Read [references/commands-and-lifecycle.md](references/commands-and-lifecycle.md) for launch, send, diff, synchronization, cancellation, stopping, and destruction semantics.
- Read [references/agent-transfer.md](references/agent-transfer.md) before launching Claude Code or Codex, especially when transcript, settings, skills, authentication, or attachment behavior matters.

`fwd --help` and `fwd COMMAND --help` are the authoritative references for the installed version.
