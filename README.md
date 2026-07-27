# fwd

**Forward your coding session to a remote machine.**

You are working on your laptop and want another machine: a clean CPU VM for development, a GPU, 200 GB of RAM, or
access to your cluster's data. `fwd` moves the working environment there. It provisions (or reuses) a remote target,
mirrors your working directory, installs the requested toolchain, and starts a persistent command in remote `tmux`.
It can carry a Claude transcript across, sync Codex settings and skills, start an ordinary shell or command, and run
durable command and agent tasks whose logs can be reattached later. Close the laptop, return tomorrow, and the remote session is waiting.

Existing tools either remote-*view* a session that stays pinned to your laptop, or provision machines with no session
story at all. `fwd` does the handoff itself.

```
laptop                                    remote (ssh / RunPod / Slurm)
──────                                    ────────────────────────────
your project dir  ──── rsync ──────────▶  ~/fwd/project
Claude transcript ──── optional ───────▶  ~/.claude/projects/<re-encoded>/
Codex settings    ──── optional ───────▶  ~/.codex/ + ~/.agents/skills/
                                          tmux fwd-<name> → shell / command / agent
    fwd  ◀────────── ssh -t attach ─────  (survives disconnects)
```

## Install

```sh
uv tool install git+https://github.com/Sid-MB/fwd
```

Or try it without installing:

```sh
uvx --from git+https://github.com/Sid-MB/fwd fwd --help
```

Requires Python 3.12+, plus `ssh` and `rsync` locally. On first launch, fwd installs or verifies remote `uv`, Bun, tmux,
and the requested coding agent. Node/npm is used when present but is not required; Codex can be installed through Bun.

## Install as a coding-agent skill

`fwd` ships an Agent Skills-compatible workflow for Claude Code, Codex, and other supporting agents. Ask it to
"Continue this project on a remote CPU machine" and it will launch, sync, and hand the session back:

```sh
fwd
# Accept: Install the fwd skill for Codex and Claude from this local fwd package?
```

The first human terminal invocation offers this after the shell-completion prompt. fwd copies only the bundled
`SKILL.md`, references, and agent metadata from its installed wheel or editable checkout into
`~/.fwd/skill-source/fwd`, then runs `npx skills add` against that local directory for global Codex and Claude use.
No GitHub checkout is involved. Declining is remembered independently in `~/.fwd/skill-prompted`.

After an accepted install, the first interactive invocation of each updated fwd build re-materializes and re-adds
that same local source non-interactively, so both copied and linked skill installations stay current. Missing `npx`
or an unavailable `skills` package only produces a warning and never blocks the requested fwd command; failed
operations remain retryable. Agent, redirected, help/version, and shell-completion invocations never show onboarding
prompts.

Invoke it explicitly as `/fwd natural-language instructions` in Claude Code, `$fwd natural-language instructions` in
Codex, or select it from Codex's `/skills` menu. Matching natural-language requests can invoke it implicitly. The
skill teaches agents the machine-readable, non-attaching CLI workflow and hands `fwd attach` back to you only when an
interactive terminal is needed. The repository also includes a validated `.codex-plugin/plugin.json`, making the
same skill package ready for Codex/OpenAI plugin catalogs. `npx skills add Sid-MB/fwd` remains an optional
repository-only installation path for people who have not installed the Python package.

## Quickstart

```sh
cd ~/code/my-project
fwd runpod                # configure RunPod if needed, launch its saved default, and attach
fwd ssh                   # most recently used SSH target, or offer SSH setup when none exists
fwd work                  # configured target named "work": launch its saved default and attach
fwd                       # current-directory session: attach, launch its saved default, or start first-time setup
```

These shorthand forms are for a human terminal: they may prompt, and they attach by replacing the local process with
`ssh -t`. Inside the session, detach with `ctrl-b d` (tmux); the command keeps running. Type `fwd` again from the same
directory to reattach. Agents and scripts should use the explicit, non-attaching `fwd up --target NAME` form instead.

You do **not** need to run `fwd setup` before `fwd`. Setup only creates or updates saved target configuration; it never
provisions, syncs, launches, or attaches. Bare `fwd` is the complete smart workflow: it attaches to this project's
existing session, otherwise launches its saved target and default command, and runs first-time setup automatically
when no target exists. Use `fwd setup` by itself when you want to add or edit a target without launching it yet.

```sh
fwd ls                    # what is running, and what it is costing you
fwd pull outputs/         # bring results back down
fwd stop                  # suspend compute; CPU RunPod container-disk data is wiped
fwd rm                    # destroy it
```

Run `fwd doctor` if anything misbehaves; it checks local prerequisites and every configured target.

`fwd setup` automatically switches to flag-only mode when stdout is not a terminal or `CLAUDECODE`/`CODEX_AGENT` is
present. This makes setup safe for agents and scripts: missing required values produce the exact flags needed instead
of opening a prompt. Run `fwd setup --help` for every field, or pass `--interactive` to force prompts. For example:

```sh
fwd setup --backend ssh --host my-box --target-name work
fwd setup --backend slurm --login-host login.example.edu --user myusername --remote-base /scratch/myusername/fwd
```

Interactive setup asks only for essential fields first. Backends place uncommon fields behind one reusable
`Set advanced options? (Defaults: …)` gate. RunPod's gate includes cloud type, remote paths, and user; GPU targets also
include volume size, while CPU targets omit it because RunPod CPU pods do not have persistent volumes.

## Commands

### Structured output

Read-oriented commands decouple their data from presentation. `fwd ls`, `fwd doctor`, and `fwd info` build structured
tables or records, then select a renderer:

```sh
fwd ls                         # Rich table in a terminal; Markdown when piped or run by an agent
fwd ls --format markdown       # stable GitHub-flavored Markdown table
fwd ls --format json           # JSON object with title, columns, and named row objects
fwd doctor --format json
fwd info --format json
```

`--format auto` is the default. It uses Markdown whenever stdout is not a terminal or `CLAUDECODE`/`CODEX_AGENT` is
set, even if an agent runner allocated a pseudo-terminal. Progress and errors remain on stderr; outside an interactive
terminal they use stable `info:`, `ok:`, `warning:`, and `error:` prefixes instead of terminal glyphs and styling.
Configuration output remains TOML (`fwd config` / `--example`) or JSON Schema (`fwd config --schema`) because those
formats are already directly machine-readable.

### Session completion

Shell completion for every session-selecting command is state-aware:

```sh
fwd attach <TAB>
fwd up <TAB>                 # claude/codex magic commands
fwd up --target <TAB>        # configured targets, RunPod, and SSH aliases
fwd up --gpu <TAB>           # locally configured GPU identifiers
fwd rm <TAB>
fwd stop <TAB>
fwd send --name <TAB>
fwd push --name <TAB>
fwd setup --backend <TAB>    # backends and backend-specific choices
```

Suggestions come from `~/.fwd/state.json` and include help text with the backend, target, local project directory, and
last-attached time. Target and setup completion also reads local fwd configuration and `~/.ssh/config`; magic agent,
output-format, backend, compute, cloud, and image choices carry short descriptions. Completion never contacts a
provider, so pressing Tab remains fast and cannot start compute.
Shells with descriptive completion support (including Fish and appropriately configured Zsh) display that help as a
tooltip/menu description; other shells still complete the session name. Install scripts with
`fwd --install-completion` or print one for manual setup with `fwd --show-completion`.

On the first interactive invocation, fwd offers to install completion for the detected shell using Typer's standard
installer. Accepting may update the Bash/Zsh startup file; declining is remembered in
`~/.fwd/completion-prompted`. Agents, redirected commands, help/version output, and shell-completion subprocesses
never prompt. It then independently offers to install the bundled coding-agent skill with
the local installed-package payload, remembering that decision in `~/.fwd/skill-prompted`. The explicit
`fwd --install-completion` command remains available after a completion decline. Accepted skill installs are
automatically refreshed from `~/.fwd/skill-source/fwd` once per updated fwd build, without fetching fwd from GitHub.

| Command | What it does | Example |
| --- | --- | --- |
| `fwd` | Smart default: attach to this directory's session, else launch one | `fwd` |
| `fwd TARGET` | Launch that configured target's saved default command and attach | `fwd work` |
| `fwd BACKEND` | Use the most recently used configured target of that backend and attach; offer setup interactively if none exists | `fwd runpod` |
| `fwd up [COMMAND...]` (alias `launch`) | Provision/reuse, sync and bootstrap a target, then start a persistent shell or command without attaching | `fwd up codex` |
| `fwd attach` / `fwd a [name] [--restart]` | Attach to a running session, reconciling live status first | `fwd a demo` |
| `fwd send` / `fwd s -- COMMAND...` | Start a durable remote command task and stream it | `fwd s -- pytest -q` |
| `fwd send agent MESSAGE...` | Send a turn to the Claude/Codex conversation running for this session | `fwd send agent "fix tests"` |
| `fwd send TASK_ID` | Reattach to a background command or agent task | `fwd send cmd-a81f` |
| `fwd send TASK_ID --stop` | Cancel one task without stopping its fwd session or machine | `fwd send cmd-a81f --stop` |
| `fwd send --ls` | List active command and agent tasks with attach/cancel instructions | `fwd send --ls` |
| `fwd ls` | List sessions with live status queried from each backend | `fwd ls --format json` |
| `fwd push` | Re-sync local changes up | `fwd push` |
| `fwd pull [paths...]` | Bring remote changes down (additive; never deletes local files) | `fwd pull outputs/` |
| `fwd diff [target] [path]` | Compare local and remote synced content; exit 0 same, 1 different, 2 error | `fwd diff pod src/` |
| `fwd stop [name]` | Kill remote tmux and suspend the target; CPU RunPod container-disk data does not survive | `fwd stop demo` |
| `fwd rm [name]` | Destroy the target and forget the session (confirms first) | `fwd rm demo` |
| `fwd setup` | Create/update a saved target without provisioning or launching; prompts in terminals and accepts every field as a flag | `fwd setup --backend ssh` |
| `fwd doctor` | Check local prerequisites and target reachability | `fwd doctor --format json` |
| `fwd default COMMAND...` | Set what bare `fwd` launches; user scope by default, with project/target overrides | `fwd default codex` |
| `fwd config` | Print the effective merged config, annotated with where each value came from | `fwd config` |
| `fwd config set KEY VALUE...` | Set any config key; the general form underlying `fwd default` | `fwd config set sync.delete false` |
| `fwd config rm KEY` | Remove one value at user, project, or target scope, revealing the next-higher default | `fwd config rm default_command` |
| `fwd config --example [backend]` | Print a commented reference config generated from the schema | `fwd config --example runpod` |
| `fwd config --schema` | Print the complete machine-readable JSON Schema for editor and agent tooling | `fwd config --schema` |
| `fwd -V` | Print the installed version | `fwd -V` |
| `fwd info` | Print version plus config and state paths | `fwd info --format json` |

### Durable remote tasks

`fwd send` (alias `fwd s`) executes from the running session's remote project directory. Every command runs in a
dedicated window inside a hidden per-session tmux task manager, writes a persistent log, and receives a task ID:

```sh
fwd send -- pwd
fwd s -- python train.py --epochs 10
fwd send --name my-session --timeout 30 -- cat results.json
fwd send --detach -- python train.py
```

By default, output streams until the task finishes and `fwd send` returns the remote exit code. If a task lasts two
seconds in an interactive terminal, fwd prints `(Press Ctrl-C to cancel, Ctrl-B to background)`. Ctrl-C cancels only
that remote task; Ctrl-B closes the local viewer while the task continues. `--detach` backgrounds immediately.

List, reattach, or cancel tasks from any later terminal:

```sh
fwd send --ls                         # active tasks; add --all for task history
fwd send --ls --format json           # stable machine-readable task inventory
fwd send cmd-a81f                     # replay its log and continue following
fwd send cmd-a81f --stop              # cancel it; the fwd session remains alive
```

It never provisions or restarts compute, so stopped, pending, ended, missing, and unknown targets fail with an
actionable message. Arguments are executed literally. To use shell syntax such as pipes, redirects, or globs, request
a shell explicitly:

```sh
fwd send -- bash -lc 'cat outputs/*.json | jq .'
```

For Slurm targets, command tasks run on the SSH login node, just like sync and bootstrap. Use `srun` explicitly
when a command must run inside an allocation.

### Sending agent turns

When the session was launched with `fwd up claude` or `fwd up codex`, `agent` resolves to that exact agent:

```sh
fwd send agent "Run the tests and fix failures"       # stream the turn
fwd send agent --detach "Run the long benchmark"      # return after it is queued
fwd send agent --stop                                 # cancel the active turn only
fwd send agent --stop "Try the smaller implementation"
fwd send agent --immediate "Try the smaller implementation"  # same cancel-and-send behavior
```

Normal follow-ups serialize behind an active managed agent turn. `--stop MESSAGE` and `--immediate MESSAGE` interrupt
the active turn and start the replacement in the same remote conversation. Before any managed send exists,
`--stop` sends Ctrl-C to the original Claude/Codex pane created by `fwd up`; it does not kill the agent, tmux session,
pod, VM, or Slurm allocation. Explicit `claude` and `codex` selectors are also accepted and fail clearly if they do
not match the agent running in the selected fwd session.

Interactive terminals render agent text and tool activity concisely. Pipes, scripts, and recognized agent
environments receive the agents' original JSONL event stream.

### Comparing local and remote content

`fwd diff` is a read-only synchronization check with the same exit contract as the standard `diff` command:

```sh
fwd diff                      # current directory's session; compare the entire synced project
fwd diff pod                  # exact session, target label, or backend selector
fwd diff pod src/model.py     # compare one project-relative file
fwd diff pod outputs/         # compare one directory recursively
fwd diff -q pod               # no diff text; inspect only the exit status
```

Exit `0` means identical, `1` means differences were found, and `2` means resolution, transfer, or comparison failed.
Differences are normal unified recursive diff text on stdout; progress and errors stay on stderr. Target labels and
backend names select their most recently used saved session, while an exact session name wins. The comparison uses
the same `.gitignore`, `.fwdignore`, and configured exclusions as sync, so intentionally unsynced environments and
build caches do not produce false differences. Both sides are copied into temporary snapshots; neither the checkout
nor the remote project is modified.

### `fwd up` flags

| Flag | Effect | Example |
| --- | --- | --- |
| `COMMAND...` | Initial persistent command; omit for a shell, or use `claude`/`codex` for a synced coding-agent workflow | `fwd up codex` |
| `--target/-t NAME` | Which configured target to use (default: `default_target`) | `fwd up -t pod` |
| `--gpu SPEC` | Override the GPU for this launch (RunPod GPU id, Slurm `--gres`) | `fwd up --gpu A100` |
| `--name/-n NAME` | Session name (default: derived from the directory) | `fwd up -n demo` |
| `--session` / `--handoff` | How to carry conversation context — see below | `fwd up --handoff claude` |
| `--user-config` | Upload your `~/.claude` bundle (CLAUDE.md, skills, agents, commands) | `fwd up --user-config claude` |
| `--creds` | Copy Claude credentials to the remote machine | `fwd up --creds claude` |
| `--attach/-a` | Attach after startup | `fwd up -a` |
| `--no-attach` | Stay local even when an interactive agent launch would normally auto-attach | `fwd up --no-attach codex` |

`fwd up` is also the **repair** command. Every stage is idempotent, so if a launch dies halfway through bootstrap, run
it again and it picks up where it left off rather than starting over or duplicating anything.

The startup forms are:

```sh
fwd runpod                         # configure/select RunPod, run its saved default, and attach
fwd ssh                            # select the most recently used SSH target and attach
fwd work                           # select the configured target named "work" and attach
fwd up                              # provision, sync, bootstrap, start a persistent remote shell; stay local
fwd up claude                       # transfer this conversation and auto-attach in a human terminal
fwd up codex                        # sync Codex settings/skills and auto-attach in a human terminal
fwd up --no-attach codex            # start Codex persistently but stay in the local terminal
fwd up -a python train.py           # start an arbitrary command and attach
fwd up -- python train.py --epochs 10  # start an arbitrary persistent command; '--' protects its flags
```

Bare `fwd` retains the current-directory workflow: it attaches to that directory's existing session; otherwise it
launches the layered `default_command` and attaches. The built-in default is Claude, but `fwd default` can change it.

`fwd TARGET` and `fwd BACKEND` are human-terminal shortcuts, not aliases for `fwd up --target`. They run the selected
target's layered `default_command` and attach. Exact configured target names win; otherwise a recognized backend name
such as `ssh`, `runpod`, or `slurm` selects its most recently used configured target. When that backend has no target,
an interactive invocation offers backend-specific setup. Unknown names never create config, and non-interactive
invocations never prompt or provision through these shortcuts; agents should use `fwd up --target NAME`. If several
targets share a backend and none has usage history yet, fwd reports the ambiguity and asks for an exact target name.

`fwd up codex` copies portable Codex configuration before starting the remote CLI: `~/.codex/config.toml`, named
profiles, `AGENTS.md`, rules, and skills from both `~/.agents/skills` and the legacy `~/.codex/skills` location.
Authentication is deliberately not copied: `~/.codex/auth.json` contains credentials, so run `codex login` remotely
when needed. Agent launches auto-attach only when stdin and stdout are terminals and neither `CLAUDECODE` nor
`CODEX_AGENT` marks an agent environment; scripts and agents remain non-attaching automatically.

## Carrying your Claude session across

`fwd up claude` and bare `fwd` move the **actual transcript** by default, so the remote session resumes with real
context — it remembers what you asked an hour ago, not a summary of it. This was verified empirically against claude 2.1.220
(`docs/session-transfer-notes.md`): a relocated transcript resumes in place, keeps its session id, and does not fork.

The transfer degrades gracefully rather than failing a launch. The chain is:

1. **`--session`** (default) — export the local transcript, rewrite the embedded paths for the remote cwd and home,
   install it remotely, and start `claude --resume <id>`.
2. **`--handoff`** — ask your local `claude -p` to write `HANDOFF.md`, sync it up, and start
   `claude "Read HANDOFF.md, then continue the work it describes"`. Use this when the conversation is long and you
   only need the conclusions. Passing `--handoff` explicitly *replaces* the transcript transfer.

   Generating a handoff takes ~65 seconds, so an existing `HANDOFF.md` less than 15 minutes old is **reused** rather
   than regenerated — otherwise every repair rerun of `fwd up` would pay that minute again to re-summarize a
   conversation that has not changed. Delete the file to force a fresh one.
3. **plain `claude`** — if there is no transcript for this directory, or the remote import cannot be validated, the
   session starts clean with a warning. A launch is never aborted over context transfer.

Two extras, both opt-in because they touch files you may not want leaving your laptop:

- **`--user-config`** uploads `~/.claude/CLAUDE.md`, `skills/`, `agents/`, `commands/` and `settings.json`. There is a
  hard exclusion list: `settings.local.json`, `.credentials.json` and history are never included, even if you ask.
- **`--creds`** ⚠️ lifts your Claude OAuth token out of the macOS Keychain and writes it to
  `~/.claude/.credentials.json` on the remote machine (mode 600). **This places a live credential on a machine you may
  not control** — a shared cluster login node, or a provisioned pod whose disk you do not own. Prefer logging in inside the
  remote session. `fwd` warns every time this flag is used.

Set defaults for any of these under `[claude]` in your config.

## Configuration

**Run `fwd config --example` for an always-up-to-date commented reference** — it is generated from `fwd`'s own
dataclasses, so it lists every field with its real default and cannot drift from the code. `fwd config --example slurm`
narrows it to one backend, and the output is valid TOML you can redirect straight into a config file. To see what your
own files currently resolve to, and which file set each value, run `fwd config`. For agents, editors, and validators,
`fwd config --schema` emits the same contract as JSON Schema Draft 2020-12.

Provider authors should read [Adding a target backend](docs/adding-target-backends.md), which covers the SSH compatibility boundary, backend contract, config/schema registration, lifecycle safety, state, documentation, and verification.

`~/.fwd/config.toml` is the global config; a project-local `.fwd/config.toml` **deep-merges over it**, so a repo can
override a single field of a globally-declared target without restating the rest.

### Default command

Bare `fwd` attaches to the current directory's existing session. When there is no session yet, it launches the
configured default command; Claude is the built-in default. Set it without editing TOML:

```sh
fwd default codex                              # user-wide default
fwd default --project claude                   # only this project
fwd default --target runpod -- python -m agent # whenever the selected target is runpod
```

The equivalent general command is `fwd config set default_command ...`:

```sh
fwd config set default_command codex
fwd config set --project default_command -- python -m agent
fwd config set sync.delete false
fwd config rm --project default_command       # confirms in a terminal
fwd config rm --target runpod default_command # removes only the target override
```

Precedence is **target > project > user > built-in `claude`**. Commands are stored as argv arrays rather than shell
strings, preserving argument boundaries:

```toml
default_command = ["codex"]

[target_defaults.runpod]
default_command = ["python", "-m", "agent"]
```

`fwd up` remains explicit: plain `fwd up` starts a background shell, while `fwd up claude`, `fwd up codex`, and
`fwd up -- <command>` select a command for that launch. Use `--user`, `--project`, or `--target NAME` with
`fwd default`/`fwd config set`; omitting all three means `--user`.

`fwd config rm` uses the same scope flags. It reports when the selected scope has no such value and leaves the file
unchanged. Existing values require confirmation in an interactive terminal; scripts and agents must pass `--force`.
Removing an override reveals the next value in the precedence chain rather than copying that value into the file.

### Target shortcuts and zero-config launches

For a human who wants a saved target and an attached coding environment, use the backend shortcut:

```sh
fwd runpod                          # offer CPU-first RunPod setup if needed, then launch the saved default and attach
fwd ssh                             # use recent SSH config, or offer SSH setup
fwd pod                             # exact configured target name; run its saved default and attach
```

For a background launch without writing config, pass an inferable target explicitly:

```sh
fwd up --target runpod              # unsaved CPU pod using built-in defaults; start a remote shell and stay local
fwd up --target sid@vm.example.com  # a machine you already have
fwd up --target my-box              # any Host alias in your ~/.ssh/config
```

Configured targets always win — declaring `[targets.runpod]` overrides the built-in rather than competing with it.
Slurm is deliberately **not** inferable: the login host, scratch path and allocation spec are all site-specific, so
`fwd` asks you to run `fwd setup` or crib from `fwd config --example slurm` instead of guessing and failing a minute
into a launch.

### SSH — a machine you already have

```toml
default_target = "box"
default_command = ["claude"]

[targets.box]
backend = "ssh"
host = "gpu.example.com"
user = "sid"
key_path = "~/.ssh/id_ed25519"       # optional; defaults to your ssh config/agent
proxy_jump = "sid@external.example"  # optional; publicly accessible host used to reach a private target
remote_base = "~/fwd"                # projects land in <remote_base>/<project>
```

### RunPod — provision CPU or GPU compute per session

```toml
[targets.pod]
backend = "runpod"
compute_type = "cpu"                 # cpu (default) | gpu
cloud_type = "secure"                # secure | community (community is cheaper)
image = "runpod/base:0.6.2-cpu"
remote_base = "/workspace"           # GPU: persistent volume; CPU: fwd relocates to ephemeral container disk
tool_prefix = "/workspace/.fwd-tools" # same relocation rule as remote_base
allow_proxy = true                   # fall back to ssh.runpod.io if no direct IP

# GPU targets may additionally set:
# compute_type = "gpu"
# gpu = "NVIDIA GeForce RTX 4090"
# image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# volume_gb = 50
# volume_mount_path = "/workspace"
```

Needs `runpodctl` installed and configured (>= 2.6.0). Three things worth knowing, all learned the hard way
(`docs/runpod-notes.md`):

- **The container disk is wiped on stop; only a GPU pod's persistent volume survives.** On a GPU target,
  `remote_base` and `tool_prefix` therefore belong under `volume_mount_path`.
- **CPU-only pods silently get no persistent volume.** `--volume-in-gb` is folded into the container disk and
  `/workspace` never exists. `fwd` detects this, relocates the project to `/root/fwd/...` on the container disk, and
  warns loudly that everything there is wiped on stop. `volume_gb` is irrelevant and omitted from CPU setup. Use a
  GPU pod if work must survive `fwd stop`.
- **`cloud_type = "community"` is the cheap option and still works fully.** Community-cloud pods were verified to
  expose a direct `ip:port` for 22/tcp with no extra flags, so rsync stays available.

CPU-only is the default, including for zero-config `fwd up --target runpod` and `fwd setup`. To request a GPU target,
set `compute_type = "gpu"`, choose a `gpu`, set `volume_gb`, and use an appropriate CUDA image such as
`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`. The interactive wizard keeps cloud, volume, remote paths,
and user behind its advanced-options gate.

Pods are reused by name across launches, restarted if stopped, and their IP/port are re-resolved on every attach
(RunPod churns both across restarts). If only the `ssh.runpod.io` proxy is reachable, `fwd` falls back to tar-over-ssh
because that transport cannot run rsync — it warns, and pushes get slower.

### Slurm — your university or lab cluster

```toml
[targets.hpc]
backend = "slurm"
login_host = "login.hpc.example.edu"
user = "me"
proxy_jump = "me@ext.example.edu"            # omit if the login node is directly reachable
key_path = "~/.ssh/id_ed25519_hpc"
remote_base = "/scratch/me/fwd"              # MUST be scratch, never $HOME
alloc = "--time=04:00:00 --cpus-per-task=8 --mem=32G"
partition = "gpu"
account = "cs-research"
env_setup = [
  "module purge",
  "module load cuda/12.4",
  "module load python/3.12",
]
```

Per-project override in `.fwd/config.toml` — inherits everything above, changes only the allocation:

```toml
[targets.hpc]
alloc = "--time=48:00:00 --cpus-per-task=16 --mem=64G --gres=gpu:a100:1"
```

Notes specific to Slurm (`docs/slurm-notes.md`):

- Sync, bootstrap and dependency installs run on the **login node** — compute nodes usually have no internet, and the
  filesystem is shared with them anyway. tmux also lives on the login node, wrapping `salloc ... srun --pty`, so your
  allocation survives a dropped connection.
- The login hostname is **pinned** on first connect. Round-robin aliases (`login.hpc` → `login1..4`) would otherwise
  land a later `fwd attach` on a node where your tmux session does not exist.
- `remote_base` must be scratch. Caches and venvs are redirected there too, because HPC home directories have inode
  quotas a single `node_modules` can exhaust.
- When your allocation ends, `fwd attach` offers a **new allocation in place** — it does not re-sync or re-bootstrap,
  since the shared filesystem still has everything.

### [your tool here]: Contribute a target!
[Open an issue](https://github.com/Sid-MB/fwd/issues/new) and tag me or write a PR!

### Global options

```toml
default_target = "box"

[claude]
session = true        # move the real transcript (default)
handoff = false       # generate HANDOFF.md instead
user_config = false   # upload ~/.claude bundle
creds = false         # copy credentials to the remote machine

[sync]
exclude = [".venv", "node_modules", "dist"]   # replaces the defaults; see below
use_gitignore = true                          # honour the repo's own .gitignore
delete = true                                 # push mirrors local (removes remote-only files)
```

`exclude` is **seeded** with sensible defaults (`.venv`, `node_modules`, `.pnpm-store`, `__pycache__`, `.next`,
`dist`, `build`, `.turbo`, the various caches, `.DS_Store`) and setting it *replaces* the list rather than adding to
it — so a project that genuinely ships a checked-in `dist/` can shrink the list, not just grow it. `.git` is never
excluded: the remote session needs history to diff, blame and commit.

## Notes

- **Ctrl-C cleans up what this invocation owns.** If a launch creates a new provider resource and is interrupted before
  startup finishes, fwd removes that new resource, deletes its state entry, and reports how many sessions remain.
  Reused targets are never destroyed by cancellation. If `fwd stop` is interrupted while closing tmux, fwd still
  completes the provider stop before exiting so compute is not left billing.
- **Push mirrors, pull does not.** `fwd push` uses `--delete` so the remote matches local exactly. `fwd pull` is
  additive and path-scoped, because a mirroring pull could delete local work you had not pushed yet.
- **Destructive and billable actions never happen on a default.** `fwd rm` needs `--force` when non-interactive: its
  prompt defaults to `no`, so a scripted `fwd rm` safely does nothing. Likewise `fwd attach` will **refuse to restart
  stopped compute** without a terminal — otherwise a cron job attaching to a stopped pod would silently start provisioning
  hardware again. Pass `--restart` (`-y`) to authorize it explicitly:

  ```sh
  fwd attach my-session --restart    # required in CI/scripts; prompts interactively without it
  ```
- **Attach never proxies your terminal.** `fwd` `exec`s into `ssh -t`, replacing itself, so resize, mouse reporting
  and ctrl-C behave exactly as a hand-typed ssh would.
- **State lives in `~/.fwd/state.json`**, locked with `flock` and written atomically. If it is ever lost or corrupted,
  `fwd` degrades to an empty session list rather than failing — your pods and jobs still exist, and `fwd up` will find
  and reuse them by name.

## Development
See [CONTRIBUTING.md](CONTRIBUTING.md).
