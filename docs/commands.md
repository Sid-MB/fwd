# Commands and lifecycle

## Launch and attach

```sh
fwd                               # human reuse workflow: attach or create interactively
fwd up                            # launch the configured default; stay local
fwd up runpod codex               # positional target and coding agent
fwd up --target work --agent codex
fwd up --new codex                # create a second session for this project
fwd up -- python train.py --epochs 10
fwd attach SESSION                # human terminal only
```

`fwd up` is idempotent and also repairs partially prepared sessions. Use `--new` only when you want a separate remote resource. Agent launches attach automatically only in a human terminal; `--detach` always stays local.

An explicit command runs as a durable task and returns its exit status. After two seconds, `Ctrl-C` cancels that task and `Ctrl-B` backgrounds the local viewer. `fwd attach` enters the primary tmux session; detach with `Ctrl-B D`.

## Durable tasks

`fwd send` runs work in an existing session and never starts or restarts compute:

```sh
fwd send -- pytest -q
fwd send --name SESSION --detach -- python train.py
fwd send --ls --json
fwd send TASK_ID                 # replay the log and continue following
fwd send TASK_ID --stop          # cancel only this task
fwd send cancel                  # cancel all queued tasks
fwd send cancel all              # cancel all active tasks
```

Use `--` before commands whose flags belong to the remote program. Invoke a shell explicitly for pipes, redirects, and globs:

```sh
fwd send -- bash -lc 'cat outputs/*.json | jq .'
```

On supported backends, `--stop-after` queues remote-owned shutdown after new work completes. `fwd send stopafter` queues shutdown after all current work, and `fwd send cancel stopafter` disarms it. Lambda does not support remote stop-after; use local `fwd stop` after retrieving results.

## Inspect sessions

```sh
fwd ls
fwd ls --all-projects
fwd ls --json
fwd ls --columns backend,status,ports
fwd doctor --json
fwd info --json
```

Rich tables are used in terminals. Redirected or agent output defaults to Markdown; `--json` provides stable machine-readable output. Progress and diagnostics stay on stderr.

Commands accept exact session names and, where unambiguous, target or backend aliases. Use selectors positionally with `attach`, `stop`, `rm`, and `diff`; use `--name` with `send`, `push`, and `pull`. `stop` and `rm` accept multiple selectors.

## Synchronize and compare

```sh
fwd diff                         # unified diff; no changes to either side
fwd diff -q                      # exit 0 same, 1 different, 2 error
fwd push                         # mirror local files to remote
fwd pull                         # additive whole-project download
fwd pull outputs/ logs/          # additive path-scoped download
```

Synchronization honors `.gitignore`, `.fwdignore`, and configured exclusions. Upload includes `.git/` so remote agents retain repository context; pull and diff exclude `.git/`, so pulling does not transfer remote commits. Export a patch or Git bundle when you need a remote commit locally.

Push mirrors the local synchronized tree and normally deletes remote-only synchronized files. Pull is always additive and never deletes local files. Uploads are capped by `sync.max_size_gb` (1 GB by default) and use a staging directory so an interrupted or over-limit upload does not replace the live remote project.

## Forward local ports

```sh
fwd ports 3000                  # local 3000 to remote 3000
fwd ports work 8080:3000        # local 8080 to remote 3000
fwd ports --ls
fwd ports --close 3000
fwd ports work --close          # close all forwards for one session
fwd up --ports 3000 --ports 8080:3000
```

Forwards bind local and remote loopback only. fwd refuses occupied local ports before changing tunnel state and tracks persistent OpenSSH control masters across CLI processes.

## Stop, remove, and uninstall

```sh
fwd stop SESSION                # stop compute; retain configured persistent storage
fwd rm SESSION                  # destroy remote resources and forget the session
fwd rm one two                  # operate on several resolved sessions
fwd rm --all                    # destroy every tracked session after confirmation
fwd uninstall                   # remove local fwd data, skill, and completion
```

`stop` and `rm` check the remote Git worktree and refuse when reachable work is dirty. Resolve that work with `fwd diff`, `fwd pull`, or a remote commit before retrying. Force flags explicitly accept possible data loss.

`fwd rm` is irreversible. `fwd uninstall` does not destroy remote resources and should normally follow `fwd rm --all`.

See [Troubleshooting](troubleshooting.md) for recovery and safety details.
