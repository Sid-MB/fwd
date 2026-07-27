# Commands and lifecycle

## Contents

- Launching
- One-shot commands
- Synchronization
- Session inspection
- Attachment
- Stop and destroy
- Interruptions and recovery

## Launching

```sh
fwd up                                  # persistent remote shell; stay local
fwd up codex                            # remote Codex with portable settings and skills
fwd up claude                           # remote Claude with transcript transfer
fwd up -- python train.py --epochs 10   # arbitrary persistent command
fwd up -t pod --gpu "NVIDIA A100 80GB PCIe" -- python train.py
```

`fwd up` is idempotent and doubles as repair: rerun the same launch after a partial failure. Agent commands auto-attach only in a human terminal; `CLAUDECODE`, `CODEX_AGENT`, redirected I/O, or `--no-attach` keeps them local.

## One-shot commands

`fwd send` (alias `fwd s`) runs from the remote project directory, streams stdout/stderr, and returns the remote exit status. It never starts or restarts compute.

```sh
fwd send -- pwd
fwd s -- python train.py --epochs 10
fwd send --name my-session --timeout 30 -- cat results.json
fwd send -- bash -lc 'cat outputs/*.json | jq .'
```

Invoke a shell explicitly for pipes, redirects, globs, or other shell syntax. On Slurm, `send` runs on the login node; use `srun` when the command belongs in an allocation.

## Synchronization

```sh
fwd diff                  # unified diff for the whole current session
fwd diff pod src/         # target/session/backend selector and one path
fwd diff -q pod           # status only: 0 same, 1 different, 2 error
fwd push                  # mirror local to remote, deleting remote-only files
fwd pull                  # additive whole-project download
fwd pull outputs/ logs/   # additive path-scoped download
```

`fwd diff` compares temporary filtered snapshots and changes neither side. Prefer it before choosing push or pull.

## Session inspection

```sh
fwd ls --format json
fwd doctor --format json
fwd info --format json
```

Machine-readable stdout is stable; progress and errors use stderr.

## Attachment

Bare `fwd`, `fwd attach`, and `fwd a` exec into interactive SSH/tmux. Never run them through an agent tool. Tell the human to run:

```sh
fwd attach SESSION
```

Detach with tmux `ctrl-b d`.

## Stop and destroy

```sh
fwd stop SESSION
fwd rm --force SESSION
```

Stopping kills tmux and suspends supported compute. A CPU RunPod's container-disk data is wiped. Destroying is irreversible and requires explicit user authorization. Restarting stopped billable compute requires `--restart` and explicit user authorization.

## Interruptions and recovery

If startup is interrupted after provisioning, fwd cancels setup, removes the newly created resource, and reports how many sessions remain. If cleanup itself fails, preserve the tracked session so `fwd rm` can still reach the resource. Similar long-running provisioning operations should leave no untracked billable resource.
