# Commands and lifecycle

## Contents

- Launching
- Durable send tasks
- Synchronization
- Session inspection
- Attachment
- Stop and destroy
- Uninstall
- Interruptions and recovery

## Launching

```sh
fwd up                                  # layered default command on the default target; stay local
fwd up runpod                           # layered default command on RunPod
fwd up runpod codex                     # target then registered coding agent
fwd up --target pod --agent codex       # equivalent explicit flags
fwd up codex                            # remote Codex with portable settings and skills
fwd up claude                           # remote Claude with transcript transfer
fwd up --new codex                     # separate session/resource for the same project and target
fwd up -- python train.py --epochs 10   # arbitrary persistent command
fwd up -t pod --gpu "NVIDIA A100 80GB PCIe" -- python train.py
```

Arbitrary commands remain in the foreground while running. A successful finite command falls through to a login
shell in the same tmux pane, preserving its output and keeping the session attachable. A nonzero exit still fails the
launch health check.

Positionals are `[TARGET] [AGENT|COMMAND...]`; `--target`, `--agent`, and `--name` provide unambiguous flag forms. Exact session names win, then target/backend names, then registered agents or arbitrary command argv. Target names win target-agent collisions with an actionable warning. All supplied selectors match conjunctively and unnamed searches stay in the current project.

`fwd up -r/--reuse` attaches to an unambiguous matching session. A sole saved match wins; with several matches, the sole running or pending target wins only when every other candidate's status is known. Otherwise pass an exact session name. A human terminal creates and attaches when no match exists; non-interactive mode does neither and prints the exact creation command without `--reuse`. Bare `fwd` is `fwd up --reuse`, while root selectors such as `fwd runpod` rewrite to `fwd up --reuse runpod`. `fwd attach` uses the same parser and matching rules.

`fwd up` is idempotent and doubles as repair: rerun the same launch after a partial failure. `--new` opts out of reuse, generates a unique session/provider name, and retains the existing session's target unless `--target` overrides it; it cannot be combined with `--name`. Agent commands auto-attach only in a human terminal; `CLAUDECODE`, `CODEX_AGENT`, redirected I/O, or `--no-attach` keeps them local.

## Durable send tasks

`fwd send` (alias `fwd s`) runs from the remote project directory in a managed remote tmux window. Every command or agent turn receives a durable task ID and log. It never starts or restarts compute.

```sh
fwd send -- pwd
fwd s -- python train.py --epochs 10
fwd send --name my-session --timeout 30 -- cat results.json
fwd send --name work -- pytest -q
fwd send --detach -- python train.py
fwd send -- bash -lc 'cat outputs/*.json | jq .'
fwd send --ls --json
fwd send TASK_ID
fwd send TASK_ID --stop
```

Streaming commands print `(Press Ctrl-C to cancel, Ctrl-B to background)` after two seconds. Ctrl-C cancels the remote task; Ctrl-B detaches the viewer and leaves it running. `--detach` backgrounds immediately. Invoke a shell explicitly for pipes, redirects, globs, or other shell syntax. On Slurm, `send` runs on the login node; use `srun` when the command belongs in an allocation.

Agent sessions add conversation-aware forms:

```sh
fwd send agent "continue the implementation"
fwd send agent --stop
fwd send agent --stop "replace the current approach"
fwd send agent --immediate "replace the current approach"
```

Normal agent follow-ups serialize. `--stop MESSAGE` and `--immediate MESSAGE` both cancel the active turn and send the replacement. `--stop` alone leaves the agent conversation, tmux session, and remote resource alive.

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

Every existing-session operation accepts an exact session name, target label, or backend name. Use selectors
positionally with `attach`, `stop`, `rm`, and `diff`, and through `--name` with `send`, `push`, and `pull`. Exact
session names win. A sole alias match wins even when stopped so it remains restartable or removable; when several
saved sessions match, fwd selects the sole running or pending target only if every other candidate's status is known.
Otherwise it reports every candidate and requires an exact session name.

## Session inspection

```sh
fwd ls --json                 # current project
fwd ls --all-projects --json  # every locally tracked project
fwd doctor --json
fwd info --json
```

Machine-readable stdout is stable; progress and errors use stderr. In a human terminal, current-project `fwd ls`
reports when other projects have tracked sessions and points to `--all-projects`; non-interactive and agent runs have no hint.

## Attachment

Bare `fwd`, root-selector forms, `fwd up --reuse`, `fwd attach`, and `fwd a` connect through interactive SSH/tmux. Never run them through an agent tool. Tell the human to run:

```sh
fwd attach SESSION
fwd attach --target work --agent codex
```

Detach with tmux `ctrl-b d`.

## Stop and destroy

```sh
fwd stop SESSION_OR_TARGET
fwd rm --force SESSION_OR_TARGET
fwd rm --all --force
```

Stopping kills tmux and suspends supported compute. A CPU RunPod's container-disk data is wiped. Destroying is irreversible and requires explicit user authorization; `fwd rm --all` applies the same cleanup to every tracked session after one bulk confirmation. Restarting stopped billable compute requires `--restart` and explicit user authorization.

## Uninstall

`fwd uninstall` removes local fwd state/configuration, the installed coding-agent skill, fwd shell-completion files,
and fwd-prefixed temporary artifacts. When available, `npx skills remove` runs first to clean its own global skill
metadata and agent links; exact-path cleanup remains as a fallback. It does not destroy remote resources and refuses
to discard tracked session state by default; run `fwd rm --all` first. After local cleanup it prints the detected
package manager's final uninstall command, GitHub reinstall and temporary-run commands, and the GitHub issues URL.

## Interruptions and recovery

If startup is interrupted after provisioning, fwd cancels setup, removes the newly created resource, and reports how many sessions remain. If cleanup itself fails, preserve the tracked session so `fwd rm` can still reach the resource. Similar long-running provisioning operations should leave no untracked billable resource.
