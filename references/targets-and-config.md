# Targets and configuration

## Contents

- Configuration discovery
- Launch-time port forwarding
- Target resolution
- First-time setup
- Backend notes
- Defaults and command precedence

## Configuration discovery

Use the CLI as the source of truth:

```sh
fwd config                         # effective merged config and source files
fwd config --schema                # machine-readable JSON Schema
fwd config --example ssh           # complete SSH example with defaults/comments
fwd config --example runpod
fwd config --example slurm
```

User config is `~/.fwd/config.toml`. Project `.fwd/config.toml` deep-merges over it. Target settings override project settings, which override user settings.

Uploads are capped at 1 GB by default so accidentally running fwd from a broad directory fails before provisioning.
Raise the boundary for one known-large project with `fwd config set --project sync.max_size_gb N`; the refusal also
prints the exact project and user config paths for direct TOML editing.

## Continuous synchronization settings

`sync.continuous` (default `false`) is the global default for Mutagen-backed continuous synchronization. Every target
accepts a three-state `continuous_sync` override: absent inherits the global value, while `true` or `false` decides for
that target regardless of it.

```toml
[sync]
continuous = true

[targets.hpc]
backend = "slurm"
continuous_sync = false
```

`fwd sync on` and `fwd sync off` write this key and reconcile the running session in one step; `fwd targets add
--continuous-sync/--no-continuous-sync` sets it non-interactively. `.git` is never continuously synced. See
[commands and lifecycle](commands-and-lifecycle.md#continuous-synchronization).

## Launch-time port forwarding

Local loopback forwards can be declared per project in `.fwd/config.toml`:

```toml
[forwarding]
ports = ["3000", "8080:3000"]
```

The list is validated before provisioning and replaces the user-level list during normal deep merging. Repeat
`fwd up --ports PORT` to replace configured mappings for one invocation. Launch ensures exact mappings are active,
preserves unrelated manual forwards, and refuses conflicting or occupied local ports before starting compute.

## Target resolution

These forms need no saved target when they are unambiguous:

```sh
fwd up --target runpod                  # built-in CPU RunPod defaults
fwd up --target sid@vm.example.com      # direct SSH connection
fwd up --target my-box                  # Host alias from ~/.ssh/config
```

Configured target names win over inferred forms. Slurm is not inferred because its login host, allocation, and scratch paths are site-specific.

In a human terminal, `fwd TARGET` means `fwd up --reuse TARGET`: attach to a matching project session or create and attach when none exists. `fwd BACKEND` matches the most recently used target of that backend or offers setup when creation needs configuration. Do not use reuse/attach forms from an agent tool call; use `fwd up --target NAME` without `--reuse`.

## First-time setup

Human:

```sh
fwd targets add
```

Agent/non-interactive example:

```sh
fwd targets add --backend ssh --host my-box --target-name work
```

Missing required fields fail with the exact flags needed. `--interactive` forces the wizard and should be left to the human. Interactive setup gates uncommon fields behind an advanced-options prompt. `fwd setup` is a permanent alias for `fwd targets add`.

## Managing saved targets

`fwd targets` edits configuration; `fwd ls`/`fwd rm` manage the sessions running on those targets.

```sh
fwd targets ls [SUBSTRING]              # saved targets, backend, connection detail, default marker
fwd targets info NAME                   # resolved values, which are defaults, and tracked sessions
fwd targets update NAME --host new-box  # non-interactive single-field edit, other values preserved
fwd targets rm NAME --force             # remove the config entry only; --force is required for agents
```

These commands make no provider calls. `update` and `rm` need an explicit target from an agent tool call because no picker can be shown. Removing a target does not stop or destroy compute; use `fwd rm` for that.

## Backend notes

### SSH

The user is optional. Let OpenSSH resolve aliases, users, ports, identities, and proxy jumps from `~/.ssh/config`. Advanced setup can override those values.

### RunPod

CPU is the default. Fwd creates a dedicated Secure Cloud network volume per session by default for both CPU and GPU compute. `fwd stop` terminates the disposable Pod while retaining the volume; `fwd rm` deletes both. Set `persistent = false` only when disposable storage is intentional.

GPU selection requires an explicitly GPU-enabled target:

```toml
[targets.pod]
backend = "runpod"
compute_type = "gpu"
gpu = "NVIDIA A100 80GB PCIe"
volume_gb = 50
image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
data_center_id = "US-GA-1"
```

### Slurm

Use a scratch filesystem for `remote_base`, never a quota-constrained home directory. Allocation strings, partitions, and environment setup are cluster-specific.

```toml
[targets.hpc]
backend = "slurm"
login_host = "login.hpc.example.edu"
remote_base = "/scratch/me/fwd"
alloc = "--time=04:00:00 --cpus-per-task=8 --mem=32G"
partition = "cpu"
env_setup = ["module purge"]
```

## Defaults and command precedence

Bare `fwd` connects to an existing current-project session when possible. If it creates a session, and every `fwd up` launch without an explicit agent or command, uses Claude unless changed:

```sh
fwd default codex
fwd default --project claude
fwd default --target runpod -- python -m agent
```

This aliases `fwd config set default_command ...`. Precedence is target, then project, then user, then built-in Claude. Remove one scoped override with `fwd config rm default_command` and the matching scope flag.

## Agent runtime defaults

Each registered agent uses the same configuration shape:

```toml
[agents.claude]
full_access = true
args = []
environment = { MY_DEFAULT = "1" }

[agents.codex]
full_access = false
args = ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
environment = {}
```

`full_access` defaults to true for fwd's VM-oriented workflow. Claude receives `--permission-mode bypassPermissions`;
Codex receives `--dangerously-bypass-approvals-and-sandbox`. Set it to false when the target itself is not an adequate
isolation boundary. Explicit permission or sandbox arguments suppress fwd's full-access argument. Environment entries
are shell defaults, not overrides, and apply consistently to the primary session, restarts, and sent agent turns.

Modern Claude Code exposes background agents and agent teams by default, and modern Codex enables multi-agent support
by default. No opt-in environment variable is necessary; agent-specific future tuning belongs in `args` or
`environment`.

GitHub setup follows the same configuration-plus-invocation pattern. `[github] auth = true` is the default for
development VMs; set it to false persistently, or use `--setup-github/--no-setup-github` on `fwd up` and `fwd attach`
for one operation. Credential discovery checks `GH_TOKEN`, `GITHUB_TOKEN`, the active local gh account, Git's
credential helper, then `~/.netrc` before offering an interactive hidden PAT prompt.
