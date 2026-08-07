---
name: fwd
description: Move a coding project or active Claude Code/Codex workflow to remote compute with fwd. Use for remote development, CPU or GPU VMs, SSH hosts and aliases, RunPod pods, Lambda Cloud instances, Slurm clusters, extra compute or memory, cluster-local data, persistent remote agents, durable remote commands, file synchronization, sync diffs, attaching, stopping, or destroying remote sessions.
---

# fwd remote development

Use `fwd` to provision or reuse a remote machine, synchronize the current project, bootstrap its tools, and run a persistent shell, command, Claude Code, or Codex session in tmux. The invocation of this skill indicates that the user wants `fwd` to be used.

Agent launches enable remote-control capabilities when the installed CLI and remote account support them. Claude publishes its interactive conversation to Claude web/mobile; Codex runs its managed Remote Control app-server beside the primary terminal TUI. Missing CLIs use native vendor installers; fwd migrates npm/Bun Codex to OpenAI's daemon-capable managed standalone distribution. Capability or authentication failures are informational and must not block normal launch.

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
2. When hardware is not already specified, inspect it with `fwd up --machines` for all targets or `fwd up TARGET --machines` for one target. The output marks the effective default and separates currently available from unavailable provider strings. SSH and other fixed targets appear without machine strings. Select an exact available value for a one-off RunPod or Lambda launch with `fwd up TARGET --machine/-m MACHINE`; never guess or shorten provider identifiers.
3. Read the exact session name from `fwd ls --json`, then send the preserved task with `fwd send --name SESSION agent "TASK"`. Let it stream and iterate; use `--detach` only when the user asks to background the work.
4. If setup is required, follow fwd's exact error flags. Do not preconfigure a target or guess fields.
5. When work changes project files, inspect `fwd diff SESSION`, then retrieve the accepted result with `fwd pull --name SESSION`. Report the result and the exact attach/send/stop commands the user may need.

For an explicitly requested shell command instead of coding-agent work, use `fwd send --name SESSION -- COMMAND...`. List or resume background tasks with `fwd send --ls --json` and `fwd send TASK_ID`; cancel only that task with `fwd send TASK_ID --stop`.

When the user wants disposable compute to stop after work finishes, first confirm that the selected backend supports remote stop-after. For a supported backend, arm remote-owned shutdown with `fwd up --stop-after -- COMMAND...` for initial command work or `fwd send --name SESSION --stop-after agent "TASK"` for an agent turn. To stop after work that is already active, run `fwd send --name SESSION stopafter`. Confirm the lifecycle task in `fwd send --ls --json`; disarm it before shutdown begins with `fwd send --name SESSION cancel stopafter`. Stop-after checks the remote Git worktree at execution time and remains blocked if it is dirty; never add `--force` unless the user explicitly accepts losing those changes. Lambda does not support remote stop-after because its broad API key remains local-only: do not issue or promise a stop-after command for a Lambda session, and instead tell the user that local `fwd stop SESSION` must be run from a connected machine after durable results are retrieved. For supported backends, never substitute a local delayed `fwd stop`: the local process may disconnect or shut down before it runs.

## Agent safety rules

Run `fwd doctor --json` when diagnosing prerequisites or a failed target.
- Prefer `--json` for `fwd ls`, `fwd doctor`, and `fwd info`; progress and diagnostics stay on stderr.
- Never run bare `fwd`, root-selector forms without an explicit arbitrary command such as `fwd runpod`, `fwd attach`, `fwd a`, `fwd up --reuse`, or `fwd up --attach` as a tool call because reuse/attach forms take over a human terminal. A root form with an explicit command uses managed-task behavior on an existing match, but in non-interactive mode it still refuses to provision; prefer an explicit non-attaching `fwd up` invocation in agent workflows.
- Do not pass `--restart` unless the user authorizes restarting stopped billable compute.
- Do not pass `--creds` unless the user explicitly authorizes copying the live Claude credential. GitHub setup defaults on for development VMs; use `--no-setup-github` or `[github] auth = false` when the user says credentials must stay local.
- Do not run `fwd rm --force` unless the user explicitly asks to accept the running-work and remote-data consequences printed by fwd. Never run `fwd rm --all --force` unless the user explicitly asks to destroy every tracked remote resource. Provider-confirmed-gone sessions clear only stale local state and do not prompt.
- Do not bypass a dirty or unreachable worktree refusal with `fwd stop --force`, `fwd send --force stopafter`, or `stopafter --force` unless the user explicitly accepts possible loss of VM-local changes.
- Run `fwd uninstall --force` only when the user explicitly requests local fwd removal and understands that tracked remote resources are not destroyed; prefer `fwd rm --all` first.
- Prefer `fwd diff -q` before deciding whether to push or pull. Exit 0 means synchronized, 1 means different, and 2 means an error.
- Launch and push stop uploads when their streaming size crosses `sync.max_size_gb` (1 GB by default), discard the incomplete remote stage, leave the live project unchanged, and list the largest included files or aggregate folders over 200 MB. If fwd refuses a deliberately large project, use the exact project-scoped `fwd config set` command in its error only after confirming the selected directory is intentional.
- In non-interactive environments, use explicit flags. Never invoke a setup wizard or invent a missing target.
- Missing `npx`, the optional `skills` CLI, or an unsuccessful skill refresh must not block normal fwd commands.
- If launch preparation fails after the target is running and synced, hand the human `fwd attach SESSION --raw` to open a plain recovery shell without rerunning tool or dependency installation. The human can repair the remote environment, exit the recovery shell, and rerun the normal launch; `--raw` does not authorize restarting stopped billable compute.

## Performance checks

When investigating a local timing regression, run `UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py`. The suite invokes every command in-process with external and destructive boundaries faked, then separately measures substantive local workloads. Use `--filter NAME` for one command and `--save PATH` followed by `--compare PATH` for same-machine regression comparisons; see `docs/benchmarking.md`.

## Primitives

### Backends

A backend implements one kind of remote compute: `runpod` provisions RunPod pods, `lambda` provisions Lambda Cloud instances through its API, `ssh` connects to an existing SSH host, and `slurm` submits work through a Slurm cluster. Backends define their configuration fields, defaults, setup choices, provisioning behavior, and lifecycle operations.

### Targets

A target is a named, reusable backend configuration—not a running machine or session. It describes how to provision or reach compute, including values such as an SSH alias, RunPod compute type, Lambda region and instance type, or Slurm allocation. Inspect configured targets and their source files with `fwd config`; inspect current machine strings and defaults with `fwd up --machines` or `fwd up TARGET --machines`; inspect all valid configuration fields with `fwd config --schema` or `fwd config --example BACKEND`. Add a target interactively with `fwd setup`, or select its form directly with `fwd setup BACKEND` (equivalent to `--backend BACKEND`, and neither form repeats the backend question) plus the required flags shown by `fwd setup --help`.

Built-in `runpod` defaults and direct SSH forms such as `user@host` or an OpenSSH host alias can work without a saved target. Prefer CPU compute unless the user explicitly requests a GPU.

### Sessions

A session is one locally tracked remote project runtime created by `fwd up`. It binds a local project directory to a target, provider resource or SSH endpoint, synchronized remote directory, and primary tmux session. Its session name identifies that concrete runtime; pass `--name NAME` to choose one or `--new` to create another instead of reusing the current project's saved session.

Use `fwd ls --json` to discover session names and live state. Existing-session commands accept target labels and backend names as aliases: `attach`, `stop`, `rm`, and `diff` accept them positionally, while `send`, `push`, and `pull` accept them through `--name`. `stop` and `rm` accept multiple positional selectors in one invocation. Exact session names win. A sole saved alias match is unambiguous; with several matches, fwd selects the sole running or pending target only when every other candidate's status is known, otherwise it requires an exact session name. Stopping a session ends its primary process and suspends supported compute; removing it destroys its remote resource and local tracking. `fwd ports` opens, lists, and closes loopback-only forwarding through the same selectors; `forwarding.ports` and repeated `fwd up --ports` open launch defaults, while `fwd ls --columns` and `--ports` focus inspection. See the lifecycle reference for mapping and closure details.

### Startup processes and agents

Every session has one primary persistent process started by `fwd up`. It may be a shell, the layered `default_command`, or a registered agent. An explicit arbitrary command—including a root shortcut such as `fwd runpod pytest -q`—selects or provisions the session, then uses the same durable task manager as `fwd send -- COMMAND`; it therefore appears in `fwd send --ls`. The primary pane remains a shell unless `--attach` explicitly runs the command there and enters tmux. `claude` and `codex` are registered agents with agent-specific configuration and conversation-transfer behavior; use `--agent NAME` when a positional target or command could be ambiguous.

Attaching connects the human terminal to this primary tmux session. Detaching leaves the process and remote compute running. A human using iTerm2 can run `fwd a -CC` to attach through tmux double control mode with native windows and tabs.
Launch installs `~/.config/fwd/tmux.conf` on the remote from the first existing local `~/.tmux.conf` or `~/.config/tmux/tmux.conf`; if neither exists, fwd installs a portable fallback with clickable window tabs, mouse pane selection, five-line wheel scrolling through copy mode, and deep history. The isolated path preserves the remote user's ordinary tmux config and is reloaded on repair launches.
When preparation failed before the primary tmux session was created, `fwd attach SESSION --raw` creates a plain
login-shell tmux in the synced project and attaches to it without repeating launch preparation.

### Send tasks

A send task is durable work started inside an already-running session with `fwd send`; it never provisions or restarts compute. Each command or agent turn runs through the session's remote tmux task manager and receives a task ID and log. Stream until completion by default; after two seconds Ctrl-C cancels the remote task and Ctrl-B backgrounds only the local viewer. Use `--detach` to background immediately, `fwd send TASK_ID` to reattach, `fwd send --ls --all --json` for task history, `fwd send cancel` for queued work, and `fwd send cancel all` for every active task. GitHub setup defaults on; launch prepares it, while direct pushes and sent agent turns repair older sessions in place without synchronizing over remote-only commits.

`--stop-after` atomically adds a remotely owned lifecycle task after new work, while `fwd send stopafter` queues it after all current work and `fwd send cancel stopafter` disarms it. The lifecycle task and dependencies appear in `fwd send --ls`; active shutdown also appears in `fwd ls`. Immediately before shutdown it refuses a dirty Git worktree and records `blocked`; `--force-stop-after` on `fwd up`, `--force` on `fwd send`, or `stopafter --force` are explicit data-loss overrides. RunPod and Slurm stop their owned compute, while SSH stops only fwd-owned tmux sessions and does not power off an external machine. Persistent RunPod sessions terminate the disposable Pod and retain their per-session network volume; `fwd rm` deletes both. Lambda does not support remote stop-after because its broad API key remains local-only; use local `fwd stop`, which terminates compute while retaining the session filesystem. Registered remote agents receive instructions for the literal helper and may run it only as their final action after requested work and durable output are complete; `stopafter --cancel` disarms it before shutdown begins. Canceling an ordinary task stops only that work, while `fwd stop` affects the entire session and target.

### Synchronization

The sync domain is the filtered project tree governed by `.gitignore`, `.fwdignore`, and configured exclusions. Standalone Git repositories use Git's own tracked plus untracked/non-ignored manifest and remove tracked paths that still match repository ignore rules, so nested `.gitignore` behavior does not depend on the local rsync implementation. `.git/` remains included in uploads for remote agent continuity, but pull and diff never import or compare repository metadata; consequently `fwd pull && git push` cannot transfer a commit created remotely, although pulled working-tree files can be committed locally. Launch and `fwd push` mirror local content to the remote project through rsync or tar fallback; stale synchronized files are deleted while excluded remote environments remain. Linked Git worktrees whose `.git` file points outside the project are not supported. `fwd pull` copies remote results back additively. Push, pull, and launch-time upload stream every selected project-relative path to stderr as it transfers. `fwd diff` compares the same filtered content domain; `--include-gitignored` adds Git-ignored content and `--include-unsynced` adds ordinary excluded content, but permanent OS metadata such as `.DS_Store`, AppleDouble `._*`, and Windows shell metadata is excluded from push, pull, and diff in every mode. During upload, `sync.max_size_gb` limits compressed outbound wire bytes to 1 GB by default; both transports discard their remote staging directory instead of changing the live project when the limit is crossed. Interactive uploads show cumulative MB/GB and live throughput without requiring a serial size preflight.

### Toolchains and requirements

A toolchain detects a project ecosystem such as Python, JavaScript, or Swift Package Manager and declares its remote setup steps and tool requirements. Requirements reuse compatible tools already present on the remote and install only missing dependencies, including prerequisite tools. Use an idempotent `.fwd/setup.sh` for project-specific setup that no built-in toolchain covers.
JavaScript projects with `.nvmrc` receive a persistent nvm installation and selected Node version even when Bun owns `node_modules`; attached shells source that nvm environment from fwd's tool prefix. JavaScript requirements can also bootstrap npm through nvm when neither npm nor mise is available. The installer runs `nvm install` and `nvm use` against `.nvmrc` when present, otherwise it selects the latest Node LTS; pnpm, Yarn, Claude Code, and Codex can reuse that npm prerequisite.

### Agent runtime policy

Remote VMs and allocations are the isolation boundary, so registered Claude and Codex sessions default to full access without approval prompts or an additional agent sandbox. Configure every agent consistently under `[agents.<name>]` with `full_access`, `args`, and `environment`; explicit permission/sandbox arguments take precedence, and environment entries are defaults that never replace values already exported by the remote shell. The recorded policy also applies after restart and to `fwd send agent` turns. Current Claude background agents/teams and Codex multi-agent support are already enabled by their CLIs, so fwd does not inject obsolete feature environment variables.

## Common operations

```sh
fwd up --target runpod                 # CPU RunPod, layered default command, stay local
fwd up --new --target runpod           # provision a separate session instead of reusing this project
fwd up --target work_cluster --agent codex     # sync Codex settings/skills and start remote Codex
fwd up work_cluster claude                     # positional target + agent; transfer the Claude transcript
fwd up -- pytest -q                    # provision, stream a durable task, and return its exit status
fwd up --stop-after -- pytest -q       # supported backends only; unavailable on Lambda
fwd up -a -- bash                      # provision and attach directly to the primary pane
fwd send -- pytest -q                  # durable task; stream output and return its exit status
fwd send --stop-after -- pytest -q     # supported backends only; unavailable on Lambda
fwd send stopafter                     # supported backends only; queue stop after all active tasks
fwd send cancel stopafter              # cancel queued remote shutdown
fwd send --detach -- pytest -q         # start in remote tmux and return immediately
fwd send --ls --json                   # inspect active command and agent tasks
fwd send --ls --all --json             # include completed, failed, and canceled task history
fwd send cancel                        # cancel queued tasks
fwd send agent --detach "fix tests"    # queue work in the running remote agent
fwd send agent --stop "try another approach"  # interrupt the active turn and send a replacement
fwd diff                               # show local/remote project differences
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
