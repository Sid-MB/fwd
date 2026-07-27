---
name: fwd
description: Forward the current Claude Code session to a remote machine with fwd. Use when the user wants to continue this work on a GPU machine, move or forward this Claude session to a remote server / RunPod pod / Slurm cluster, provision a remote dev box for this project, or needs more compute, memory or cluster data than the local machine has. Also covers syncing files to and from that remote session (fwd push / fwd pull) and stopping or destroying it.
---

# fwd — forward this Claude session to a remote machine

`fwd` is a CLI that relocates the *current working session* to a bigger machine. It provisions (or reuses) a remote target over ssh, RunPod, or Slurm, rsyncs the working directory up, installs the remote toolchain (uv, node, bun, claude, tmux), carries the local Claude transcript across so the remote `claude` resumes the same conversation, and leaves it running in a persistent remote tmux. Reach for it when the work needs a GPU, far more RAM, or data that only lives on a cluster — and when the user wants the *conversation* to come along, not just the files. It is not a file-sync tool or a general remote-shell wrapper; if the user only needs to copy files, use rsync/scp directly.

## Installation

If `fwd` is not on PATH:

```sh
uv tool install git+https://github.com/Sid-MB/fwd
```

Or run it once without installing: `uvx --from git+https://github.com/Sid-MB/fwd fwd --help`.

Needs Python 3.12+ locally, plus `ssh` and `rsync`. The RunPod backend additionally needs `runpodctl` >= 2.6.0 configured. Everything the *remote* needs is installed by `fwd` on first launch.

## First-time setup

```sh
fwd setup     # prompts in a terminal; automatically requires flags under CLAUDECODE/CODEX_AGENT or redirected output
fwd doctor    # checks local prerequisites and every configured target; non-zero exit on failure
```

Agents may run `fwd setup` non-interactively by supplying the fields shown by `fwd setup --help`, for example `fwd setup --backend ssh --host my-box --target-name work`. Missing required fields fail with the exact flags needed and no prompt. `--interactive` forces the wizard and should be left to the user. `fwd doctor` is safe and non-interactive, so run it first whenever anything misbehaves.

Config: `~/.fwd/config.toml` is global; a project-local `.fwd/config.toml` **deep-merges over it**, so a repo can override one field of a global target (commonly the Slurm `alloc`) without restating the rest. Writing config files directly is fine and often faster than the wizard.

**Do not guess config fields.** Run `fwd config --example <ssh|runpod|slurm|all>` to see every available field with its real default and a comment — the output is generated from fwd's own schema, so it matches the installed version. Run `fwd config --schema` for the machine-readable JSON Schema, and `fwd config` to inspect the effective merged config, annotated with which file set each value, before editing anything. The longer configuration guide is https://github.com/Sid-MB/fwd#configuration.

**You often need no config at all.** A `--target` absent from config is inferred when unambiguous, so these work on a clean machine:

```sh
fwd up --no-attach --target runpod                  # GPU pod from built-in RunPod defaults
fwd up --no-attach --target sid@gpu.example.com     # a host the user already has
fwd up --no-attach --target my-box                  # any Host alias in ~/.ssh/config
```

Configured targets always win over inferred ones. Slurm is deliberately not inferable (site-specific login host, scratch path and allocation) — for a cluster, tell the user to run `fwd setup`, or write a config using `fwd config --example slurm`.

Minimal target per backend:

```toml
default_target = "box"

[targets.box]                 # ssh — a machine the user already has
backend = "ssh"
host = "gpu.example.com"
user = "sid"
remote_base = "~/fwd"

[targets.pod]                 # runpod — provision a GPU per session
backend = "runpod"
compute_type = "gpu"          # gpu | cpu; cpu pods get NO persistent volume
cloud_type = "community"      # community is cheaper and fully works
gpu = "NVIDIA RTX A4000"
image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
volume_gb = 50
remote_base = "/workspace"    # MUST be on the volume; container disk is wiped on stop
tool_prefix = "/workspace/.fwd-tools"

[targets.hpc]                 # slurm — a university/lab cluster
backend = "slurm"
login_host = "login.hpc.example.edu"
user = "sid"
remote_base = "/scratch/sid/fwd"              # MUST be scratch, never $HOME (inode quotas)
alloc = "--time=04:00:00 --cpus-per-task=8 --mem=32G --gres=gpu:a100:1"
partition = "gpu"
env_setup = ["module purge", "module load cuda/12.4"]
```

## Using fwd from inside a Claude session

### Launching

```sh
fwd up --no-attach                      # provision, sync, transfer the transcript, start remote claude, stay local
fwd up --no-attach -t pod --gpu "NVIDIA A100 80GB PCIe"   # pick a target and override its GPU for this launch
```

Always pass `--no-attach` when *you* run it: attaching is an interactive terminal takeover and will hang a tool call. `fwd up` is idempotent and doubles as the **repair** command — if a launch dies halfway, run the same command again rather than cleaning up first.

Context transfer:
- **Default (`--session`)** moves the real transcript, so the remote session resumes with genuine context. This is already on; only pass `--session` explicitly if the user's config set `session = false`.
- **`--handoff`** instead has the local `claude -p` write `HANDOFF.md` and points the remote session at it. Passing it *replaces* the transcript transfer. Use it only when the user asks for a summary handoff or the conversation is long and only conclusions matter. It costs ~65s; an existing `HANDOFF.md` under 15 minutes old is reused.
- Transfer degrades gracefully to plain `claude` with a warning; it never aborts a launch.

- **`--user-config`** uploads the user's `~/.claude` bundle (CLAUDE.md, skills, agents, commands, settings.json). Credentials and history are hard-excluded. Only pass it if the user wants their global config on the remote.
- **`--creds`** ⚠️ writes the user's live Claude OAuth token to the remote disk. **Do not pass this unless the user explicitly asks for it in this conversation** — it places a live credential on a machine they may not control. Default to letting them log in inside the remote session.

### Checking, syncing, lifecycle

```sh
fwd ls                        # sessions with live per-backend status and cost — safe, run this to orient
fwd push                      # re-sync local changes up (mirrors: deletes remote-only files)
fwd pull                      # bring the whole remote dir down (additive; never deletes local files)
fwd pull outputs/ logs/       # path-scoped pull, the usual way to fetch results
fwd stop                      # kill the remote tmux and suspend the target; synced data survives
fwd rm --force                # destroy the target and forget the session; irreversible
```

- `fwd rm` prompts and its prompt defaults to **no**, so a non-interactive `fwd rm` does nothing — pass `--force` when the user has asked for destruction.
- `fwd attach` refuses to restart **stopped, billable** compute without a terminal. `--restart` (`-y`) authorizes it explicitly. Never pass `--restart` on your own initiative; restarting a pod resumes billing.
- On RunPod, a stop wipes everything outside the volume. On Slurm, when an allocation ends `fwd attach` offers a new allocation in place without re-syncing.

### What you must hand back to the user

`fwd` (bare) and `fwd attach` `exec` into `ssh -t` and take over the terminal, so they cannot be run as tool calls. When the session is ready, tell the user to run it themselves — from within Claude Code the fastest route is the bash-passthrough:

```
!fwd
```

That attaches to this directory's session (or launches one if there is none). Detach with tmux's `ctrl-b d`; typing `fwd` again in the same directory returns to the same conversation on the same machine.

## Notes and failure modes

- Every command defaults to "this directory's session", resolved from the cwd, so run `fwd` from the project root. Override with `-n/--name` (or the positional name on `attach`/`stop`/`rm`).
- State lives in `~/.fwd/state.json`. If it is lost, `fwd` degrades to an empty session list rather than failing, and `fwd up` re-finds existing pods and jobs by name.
- `fwd --help` and `fwd <cmd> --help` are fully documented and are the authoritative flag reference; read them if unsure rather than guessing a flag.
- Longer design notes live in the repo's `docs/`: `session-transfer-notes.md`, `runpod-notes.md`, `slurm-notes.md`.
