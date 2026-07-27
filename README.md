# fwd

**Forward your coding session to a remote machine.**

You are working with a coding agent on your laptop and want another machine: a clean CPU VM, a GPU, 200 GB of RAM, or
access to your cluster's data. `fwd` moves the whole working session there. It provisions (or reuses) a remote target,
mirrors your working directory up, installs the toolchain, carries your Claude conversation across, and drops you into
a persistent remote `tmux` session already running `claude`. Close the laptop, open it tomorrow, type `fwd` in the same
directory, and you are back in the same conversation on the same machine.

Existing tools either remote-*view* a session that stays pinned to your laptop, or provision machines with no session
story at all. `fwd` does the handoff itself.

```
laptop                                    remote (ssh / RunPod / Slurm)
──────                                    ────────────────────────────
your project dir  ──── rsync ──────────▶  ~/fwd/project
~/.claude/…       ──── transcript ─────▶  ~/.claude/projects/<re-encoded>/
                                          tmux fwd-<name> → claude --resume <id>
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

Requires Python 3.12+, plus `ssh` and `rsync` locally. Everything the *remote* needs (uv, bun, node, claude, tmux) is
installed by `fwd` on first launch.

## Install as a Claude Code skill

`fwd` ships a `SKILL.md` so your agent can drive it for you: ask it to "Continue my work on a GPU machine" and it will launch, sync and hand the session back:

```sh
npx skills add Sid-MB/fwd
```

The skill teaches Claude the safe subset of the CLI — it uses the non-attaching `fwd up claude` workflow and hands
`fwd`/`fwd attach` back to you only when an interactive terminal is needed.

## Quickstart

```sh
cd ~/code/my-project
fwd setup                 # prompts in a terminal; flag-only for agents and scripts
fwd                       # launch, sync, bootstrap, and attach
```

That is the whole loop. Inside the session you are in a normal `claude` REPL on the remote machine. Detach with
`ctrl-b d` (tmux) — the session keeps running. Type `fwd` again from the same directory to reattach.

```sh
fwd ls                    # what is running, and what it is costing you
fwd pull outputs/         # bring results back down
fwd stop                  # pause the machine, keep the data
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

| Command | What it does |
| --- | --- |
| `fwd` | Smart default: attach to this directory's session, else launch one |
| `fwd up [COMMAND...]` (alias `launch`) | Provision/reuse, sync and bootstrap a target, then start a persistent shell or command without attaching |
| `fwd attach` / `fwd a [name] [--restart]` | Attach to a running session, reconciling live status first |
| `fwd send` / `fwd s -- COMMAND...` | Execute one command remotely and return its output and exit status |
| `fwd ls` | List sessions with live status queried from each backend |
| `fwd push` | Re-sync local changes up |
| `fwd pull [paths...]` | Bring remote changes down (additive; never deletes local files) |
| `fwd stop [name]` | Kill the remote tmux and suspend the target; data is preserved |
| `fwd rm [name]` | Destroy the target and forget the session (confirms first) |
| `fwd setup` | Create/update `~/.fwd/config.toml`; prompts in terminals and accepts every field as a flag |
| `fwd doctor` | Check local prerequisites and target reachability |
| `fwd default COMMAND...` | Set what bare `fwd` launches; user scope by default, with project/target overrides |
| `fwd config` | Print the effective merged config, annotated with where each value came from |
| `fwd config set KEY VALUE...` | Set any config key; the general form underlying `fwd default` |
| `fwd config rm KEY` | Remove one value at user, project, or target scope, revealing the next-higher default |
| `fwd config --example [backend]` | Print a commented reference config generated from the schema |
| `fwd config --schema` | Print the complete machine-readable JSON Schema for editor and agent tooling |
| `fwd -V` | Print the installed version |
| `fwd info` | Print version plus config and state paths |

### One-shot remote commands

`fwd send` (alias `fwd s`) executes from the running session's remote project directory without taking over the
terminal:

```sh
fwd send -- pwd
fwd s -- python train.py --epochs 10
fwd send --name my-session --timeout 30 -- cat results.json
```

Remote stdout and stderr remain separate and stream normally; `fwd send` exits with the remote command's exit code.
It never provisions or restarts compute, so stopped, pending, ended, missing, and unknown targets fail with an
actionable message. Arguments are executed literally. To use shell syntax such as pipes, redirects, or globs, request
a shell explicitly:

```sh
fwd send -- bash -lc 'cat outputs/*.json | jq .'
```

For Slurm targets, one-shot commands run on the SSH login node, just like sync and bootstrap. Use `srun` explicitly
when a command must run inside an allocation.

### `fwd up` flags

| Flag | Effect |
| --- | --- |
| `COMMAND...` | Initial persistent command; omit for a shell, or use `claude`/`codex` for a synced coding-agent workflow |
| `--target/-t NAME` | Which configured target to use (default: `default_target`) |
| `--gpu SPEC` | Override the GPU for this launch (RunPod GPU id, Slurm `--gres`) |
| `--name/-n NAME` | Session name (default: derived from the directory) |
| `--session` / `--handoff` | How to carry conversation context — see below |
| `--user-config` | Upload your `~/.claude` bundle (CLAUDE.md, skills, agents, commands) |
| `--creds` | Copy Claude credentials to the remote machine |
| `--attach/-a` | Attach after startup |
| `--no-attach` | Stay local even when an interactive agent launch would normally auto-attach |

`fwd up` is also the **repair** command. Every stage is idempotent, so if a launch dies halfway through bootstrap, run
it again and it picks up where it left off rather than starting over or duplicating anything.

The startup forms are:

```sh
fwd up                              # provision, sync, bootstrap, start a persistent remote shell; stay local
fwd up claude                       # transfer this conversation and auto-attach in a human terminal
fwd up codex                        # sync Codex settings/skills and auto-attach in a human terminal
fwd up --no-attach codex            # start Codex persistently but stay in the local terminal
fwd up -a python train.py           # start an arbitrary command and attach
fwd up -- python train.py --epochs 10  # start an arbitrary persistent command; '--' protects its flags
```

Bare `fwd` retains the original ergonomic workflow: on first launch it is equivalent to `fwd up --attach claude`;
later it attaches to the existing session.

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

### Zero-config quickstart

You may not need a config file at all. A `--target` that is not in your config is inferred when it is unambiguous:

```sh
fwd up --target runpod              # provisions a CPU-only pod using the built-in RunPod defaults
fwd up --target sid@vm.example.com  # a machine you already have
fwd up --target my-box              # any Host alias in ~/.ssh/config
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
volume_gb = 50
volume_mount_path = "/workspace"
remote_base = "/workspace"           # MUST be on the volume
tool_prefix = "/workspace/.fwd-tools"
allow_proxy = true                   # fall back to ssh.runpod.io if no direct IP
```

Needs `runpodctl` installed and configured (>= 2.6.0). Three things worth knowing, all learned the hard way
(`docs/runpod-notes.md`):

- **The container disk is wiped on stop; only the volume survives.** That is why `remote_base` and `tool_prefix` both
  live under `/workspace` — otherwise every restart re-downloads the entire toolchain.
- **CPU-only pods silently get no persistent volume.** `--volume-in-gb` is folded into the container disk and
  `/workspace` never exists. `fwd` detects this, relocates the project to `/root/fwd/...` on the container disk, and
  warns loudly that everything there is wiped on stop. Use a GPU pod if you want persistence.
- **`cloud_type = "community"` is the cheap option and still works fully.** Community-cloud pods were verified to
  expose a direct `ip:port` for 22/tcp with no extra flags, so rsync stays available.

CPU-only is the default, including for zero-config `fwd up --target runpod` and `fwd setup`. To request a GPU target,
set `compute_type = "gpu"`, choose a `gpu`, and use an appropriate CUDA image such as
`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`.

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
