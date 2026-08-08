# Troubleshooting

## Start with diagnostics

```sh
fwd doctor
fwd doctor --json
fwd ls --all-projects --json
```

`fwd doctor` checks local prerequisites and configured target reachability. Structured output keeps data on stdout and diagnostics on stderr.

## Recover a partial launch

`fwd up` is idempotent. Rerun the same launch after an interrupted setup, dependency failure, or transient provider error.

If the target is running and synchronized but launch preparation cannot create the primary tmux session, open a plain recovery shell from a human terminal:

```sh
fwd attach SESSION --raw
```

Repair the remote environment, exit the recovery shell, and rerun the normal `fwd up` command. `--raw` does not authorize restarting stopped billable compute.

## Stopped compute

Non-interactive commands never restart stopped billable compute implicitly. After confirming the cost and target, authorize it explicitly:

```sh
fwd attach SESSION --restart
```

## Dirty remote worktree

`fwd stop`, `fwd rm`, and stop-after refuse to shut down a reachable session with uncommitted or untracked remote Git changes. Inspect and retrieve work before retrying:

```sh
fwd diff SESSION
fwd pull --name SESSION
```

Force flags bypass this protection and may destroy remote-only changes. Use them only after accepting that loss.

## Synchronization limits

Uploads stop when compressed outbound data exceeds `sync.max_size_gb` (1 GB by default). The incomplete staging directory is discarded and the live remote project remains unchanged. Exclude accidental datasets or build outputs in `.fwdignore`; raise the project limit only for a deliberately large project:

```sh
fwd config set --project sync.max_size_gb 4
```

## Destructive operations

`fwd rm` permanently deletes remote resources. `fwd uninstall` removes only local fwd data and can leave remote resources running and billing. Remove tracked sessions first:

```sh
fwd rm --all
fwd uninstall
```

If a command still fails, include `fwd info`, `fwd doctor`, the exact command, and its stderr when opening an [issue](https://github.com/Sid-MB/fwd/issues).
