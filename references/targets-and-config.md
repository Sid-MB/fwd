# Targets and configuration

## Contents

- Configuration discovery
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
fwd setup
```

Agent/non-interactive example:

```sh
fwd setup --backend ssh --host my-box --target-name work
```

Missing required fields fail with the exact flags needed. `--interactive` forces the wizard and should be left to the human. Interactive setup gates uncommon fields behind an advanced-options prompt.

## Backend notes

### SSH

The user is optional. Let OpenSSH resolve aliases, users, ports, identities, and proxy jumps from `~/.ssh/config`. Advanced setup can override those values.

### RunPod

CPU is the default. CPU pods have no persistent volume; fwd relocates work to the ephemeral container disk, which is wiped on stop. GPU targets may configure a persistent volume and CUDA image.

GPU selection requires an explicitly GPU-enabled target:

```toml
[targets.pod]
backend = "runpod"
compute_type = "gpu"
gpu = "NVIDIA A100 80GB PCIe"
volume_gb = 50
image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
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
