# fwd

**Move your coding project and agent session to remote compute.**

`fwd` provisions or connects to a remote target, synchronizes the current project, prepares its toolchain, and starts a persistent shell, command, Claude Code, or Codex session in tmux. Disconnect your laptop and return later: the session, durable tasks, and logs remain available.

```text
laptop                                  remote SSH / RunPod / Lambda / Slurm
project  ───────────── sync ──────────▶  project checkout
agent context ──────── optional ──────▶  Claude or Codex state
                                         tmux session + durable task manager
terminal ◀────────── attach / stream ──  persistent work
```

## Install

```sh
uv tool install fwdit
```

Or try it without installing:

```sh
uvx --from fwdit fwd --help
```

Requires Python 3.12+, `ssh`, and `rsync` locally. Run `fwd doctor` to check target-specific requirements.

## Quick start

Run fwd from the project you want to move:

```sh
fwd                              # attach to this project's session, or create one interactively
fwd up runpod codex              # launch Codex on RunPod; auto-attach in a human terminal
fwd up --detach runpod codex     # launch Codex but stay in the local terminal
fwd up --target work             # use a configured SSH/cloud/HPC target
fwd up -- python train.py        # launch and stream a durable command
fwd ls                           # inspect sessions and live status
fwd attach SESSION               # reconnect from a human terminal
```

Bare `fwd` is the interactive reuse workflow. Agent launches auto-attach in a human terminal; pass `--detach` to stay local. Scripts and coding-agent environments remain non-attaching. Detach from tmux with `Ctrl-B D`.

Need a saved target first?

```sh
fwd setup                        # interactive target setup
fwd setup ssh --host my-box --target-name work
fwd config --example runpod      # current generated config reference
```

Read [Getting started](docs/getting-started.md) for the full first-session walkthrough.

## Choose a target

- **SSH:** an existing host, direct address, or OpenSSH alias.
- **RunPod:** CPU or GPU Pods with per-session persistent network volumes by default.
- **Lambda Cloud:** GPU instances with persistent filesystems and local-only API credentials.
- **Slurm:** allocations launched through persistent login-node tmux on shared scratch.

See [Configuration and backends](docs/configuration.md#target-setup) for setup, storage, and lifecycle differences.

## Common workflows

### Run durable work

```sh
fwd send -- pytest -q
fwd send --detach -- python train.py
fwd send --ls --json
fwd send TASK_ID                 # reattach to its log
fwd send TASK_ID --stop          # cancel the task, not the session
```

Every command runs in remote tmux with a durable ID and log. Streaming returns the remote exit code; `Ctrl-C` cancels and `Ctrl-B` backgrounds the viewer.

### Synchronize results

```sh
fwd diff                         # compare without changing either side
fwd push                         # mirror local synchronized files to remote
fwd pull outputs/                # additive download; never deletes local files
```

Sync honors `.gitignore`, `.fwdignore`, and configured exclusions. Upload includes `.git/` for remote agent continuity; pull and diff exclude Git metadata.

### Forward a service

```sh
fwd ports 3000                   # localhost:3000 to remote localhost:3000
fwd ports work 8080:3000
fwd ports --ls
fwd ports --close 3000
```

Forwards are loopback-only and persist through a managed SSH control connection.

### Stop or destroy compute

```sh
fwd stop SESSION                 # stop compute and retain configured persistent storage
fwd rm SESSION                   # permanently destroy remote resources
```

Both commands protect a reachable dirty remote Git worktree. `rm` is irreversible; force flags explicitly accept possible loss. See [Lifecycle safety](docs/commands.md#stop-remove-and-uninstall) before automating cleanup.

## Coding-agent skill

fwd ships an Agent Skills-compatible workflow for Codex, Claude Code, and other supporting agents. The first interactive invocation offers to install the bundled skill. It can also be installed directly:

```sh
npx skills add Sid-MB/fwd --skill fwd -g -a codex -a claude-code
```

Invoke it as `$fwd ...` in Codex or `/fwd ...` in Claude Code. See [Coding agents](docs/agents.md#install-the-fwd-skill) for behavior and credential guidance.

## Documentation

### User guide

- [Getting started](docs/getting-started.md): installation, first launch, target setup, and the everyday workflow.
- [Commands and lifecycle](docs/commands.md): durable tasks, synchronization, inspection, port forwarding, stopping, and removal.
- [Configuration and backends](docs/configuration.md): config layers, SSH, RunPod, Lambda Cloud, Slurm, toolchains, and defaults.
- [Coding agents](docs/agents.md): Claude/Codex transfer, follow-up turns, credentials, runtime policy, and skill installation.
- [Troubleshooting](docs/troubleshooting.md): diagnostics, launch recovery, dirty worktrees, sync limits, and destructive operations.
- [User documentation index](docs/README.md): the complete end-user map.

The installed CLI is the authoritative option reference:

```sh
fwd --help
fwd COMMAND --help
fwd config --example
fwd config --schema
```

### Developer guide

- [Developer documentation index](dev-docs/README.md): architecture, provider notes, validation evidence, and repository map.
- [Adding a target backend](dev-docs/adding-target-backends.md)
- [Adding a project toolchain](dev-docs/adding-toolchains.md)
- [Performance benchmarking](dev-docs/benchmarking.md)
- [Contributing](CONTRIBUTING.md)
