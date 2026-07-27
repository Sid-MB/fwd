# fwd

**Forward your Claude Code session to a remote machine.**

You are working with Claude Code on your laptop and hit a wall: the model needs a GPU, or 200 GB of RAM, or your
cluster's data. `fwd` moves the whole working session somewhere bigger. It provisions (or reuses) a remote target,
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

`fwd` ships a `SKILL.md` at the repo root, so Claude can drive it for you — ask it to "continue this on a GPU machine"
and it will launch, sync and hand the session back:

```sh
npx skills add Sid-MB/fwd
```

That installs the skill into your agent's skills directory (`.claude/skills/fwd/` for Claude Code); pass
`-a claude-code -y` for a non-interactive install, or `--all` when installing into several agents. The skill teaches
Claude the safe subset of the CLI — it uses `fwd up --no-attach` since attaching is an interactive terminal takeover,
and hands `fwd`/`fwd attach` back to you. Installing the skill does not install the `fwd` binary; do that above.

## Quickstart

```sh
fwd setup                 # prompts in a terminal; flag-only for agents and scripts
cd ~/code/my-project
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
fwd setup --backend slurm --login-host login.example.edu --user sid --remote-base /scratch/sid/fwd
```

## Commands

| Command | What it does |
| --- | --- |
| `fwd` | Smart default: attach to this directory's session, else launch one |
| `fwd up` (alias `launch`) | Provision/reuse a target, sync, bootstrap, start Claude, attach |
| `fwd attach [name] [--restart]` | Attach to a running session, reconciling live status first |
| `fwd ls` | List sessions with live status queried from each backend |
| `fwd push` | Re-sync local changes up |
| `fwd pull [paths...]` | Bring remote changes down (additive; never deletes local files) |
| `fwd stop [name]` | Kill the remote tmux and suspend the target; data is preserved |
| `fwd rm [name]` | Destroy the target and forget the session (confirms first) |
| `fwd setup` | Create/update `~/.fwd/config.toml`; prompts in terminals and accepts every field as a flag |
| `fwd doctor` | Check local prerequisites and target reachability |
| `fwd config` | Print the effective merged config, annotated with where each value came from |
| `fwd config --example [backend]` | Print a commented reference config generated from the schema |
| `fwd config --schema` | Print the complete machine-readable JSON Schema for editor and agent tooling |

### `fwd up` flags

| Flag | Effect |
| --- | --- |
| `--target/-t NAME` | Which configured target to use (default: `default_target`) |
| `--gpu SPEC` | Override the GPU for this launch (RunPod GPU id, Slurm `--gres`) |
| `--name/-n NAME` | Session name (default: derived from the directory) |
| `--session` / `--handoff` | How to carry conversation context — see below |
| `--user-config` | Upload your `~/.claude` bundle (CLAUDE.md, skills, agents, commands) |
| `--creds` | Copy Claude credentials to the remote machine |
| `--no-attach` | Set everything up but stay local |

`fwd up` is also the **repair** command. Every stage is idempotent, so if a launch dies halfway through bootstrap, run
it again and it picks up where it left off rather than starting over or duplicating anything.

## Carrying your Claude session across

By default `fwd` moves the **actual transcript**, so the remote session resumes with real context — it remembers what
you asked an hour ago, not a summary of it. This was verified empirically against claude 2.1.220
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
  not control** — a shared cluster login node, or a rented pod whose disk you do not own. Prefer logging in inside the
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

### Zero-config quickstart

You may not need a config file at all. A `--target` that is not in your config is inferred when it is unambiguous:

```sh
fwd up --target runpod              # rents a GPU pod using the built-in RunPod defaults
fwd up --target sid@gpu.example.com # a machine you already have
fwd up --target my-box              # any Host alias in ~/.ssh/config
```

Configured targets always win — declaring `[targets.runpod]` overrides the built-in rather than competing with it.
Slurm is deliberately **not** inferable: the login host, scratch path and allocation spec are all site-specific, so
`fwd` asks you to run `fwd setup` or crib from `fwd config --example slurm` instead of guessing and failing a minute
into a launch.

### SSH — a machine you already have

```toml
default_target = "box"

[targets.box]
backend = "ssh"
host = "gpu.example.com"
user = "sid"
key_path = "~/.ssh/id_ed25519"       # optional; defaults to your ssh config/agent
proxy_jump = "sid@bastion.example"   # optional
remote_base = "~/fwd"                # projects land in <remote_base>/<project>
```

### RunPod — rent a GPU per session

```toml
[targets.pod]
backend = "runpod"
compute_type = "gpu"                 # gpu | cpu
cloud_type = "secure"                # secure | community (community is cheaper)
gpu = "NVIDIA RTX A4000"
image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
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

For a throwaway session where nothing needs to survive a stop, `compute_type = "cpu"` with
`cloud_type = "secure"` is the cheapest thing that boots.

Pods are reused by name across launches, restarted if stopped, and their IP/port are re-resolved on every attach
(RunPod churns both across restarts). If only the `ssh.runpod.io` proxy is reachable, `fwd` falls back to tar-over-ssh
because that transport cannot run rsync — it warns, and pushes get slower.

### Slurm — your university or lab cluster

```toml
[targets.hpc]
backend = "slurm"
login_host = "login.hpc.example.edu"
user = "sid"
proxy_jump = "sid@bastion.example.edu"        # omit if the login node is directly reachable
key_path = "~/.ssh/id_ed25519_hpc"
remote_base = "/scratch/sid/fwd"              # MUST be scratch, never $HOME
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
alloc = "--time=08:00:00 --cpus-per-task=16 --mem=64G --gres=gpu:a100:1"
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
  stopped compute** without a terminal — otherwise a cron job attaching to a stopped pod would silently start renting
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

```sh
uv sync
uv run pytest
uv run fwd --help
```

Design notes for the trickier subsystems live in `docs/`: `session-transfer-notes.md` (how transcript relocation was
verified), `runpod-notes.md` (runpodctl behaviour and the volume trap), `slurm-notes.md` (job.sh, login pinning, the
`fwd-env.sh` contract).

CI runs `uv sync --frozen` + `pytest` on 3.12 and 3.13 for every push and PR to `main`
(`.github/workflows/ci.yml`). `--frozen` means a dependency bump must land with its `uv.lock` update.

### Publishing

`.github/workflows/publish.yml` publishes to PyPI over **OIDC trusted publishing** — there is no API token in this
repo and none should be added. It runs when a GitHub release is *published*, or manually via `workflow_dispatch`. The
`build` job runs `uv build` (sdist + wheel, checked to contain `bootstrap.sh`) and uploads the artifact; a separate
`publish` job holds `id-token: write` and the `pypi` environment, and does nothing but download that artifact and
upload it. Splitting them keeps the job that can mint a PyPI credential away from any project code.

One-time setup on pypi.org, under *Publishing* → *Add a new pending publisher*:

| Field | Value |
| --- | --- |
| PyPI project name | must match `project.name` in `pyproject.toml` (currently `fwd`) |
| Owner / repository | `Sid-MB` / `fwd` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Then create a matching `pypi` environment in the repo settings (a required-reviewer rule there gates every upload).

**The published version comes from `version` in `pyproject.toml`, not from the git tag.** PyPI will not overwrite an
existing version, so bump `pyproject.toml` in the same commit you tag — otherwise the `publish` job fails at the upload
step. None of this is wired into the install instructions above: until a release actually happens, the git install is
the real one.
