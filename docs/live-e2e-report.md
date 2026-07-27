# Live end-to-end validation of the `fwd` CLI against a real RunPod pod

Run on **2026-07-26/27** by teammate F, driving the *real* CLI (`uv run --project ~/Coding/Python/fwd fwd ...`) rather
than any backend in isolation. Everything below was measured; nothing is inferred.

## Test rig

| Item | Value |
| --- | --- |
| Scratch project | `<scratchpad>/live-e2e/proj` — `pyproject.toml` + `uv.lock` (one dep: `six`), `CLAUDE.md`, `main.py`, `.gitignore`, a git repo with one commit, and a deliberate junk dir `.venv/junk/local-only.txt` |
| Config isolation | **project-level** `<proj>/.fwd/config.toml` only. `~/.fwd/config.toml` did not exist before the run and was never created, so no user config was touched. |
| Target | `[targets.fwd-test-live]` backend `runpod`, `gpu = "NVIDIA RTX A4000"`, `image = "runpod/base:1.0.2-ubuntu2404"`, `volume_gb = 20`, `remote_base`/`tool_prefix` under `/workspace` |
| Session/pod name | `fwd up --name test-live` → session `test-live` → pod `fwd-test-live` (satisfies the `fwd-test-` prefix rule) |
| Pod | `d0dy46ht8ddtmy`, secure cloud, 1× RTX A4000, **$0.25/hr**, 20 GB network volume (`mfs#eur-is-1.runpod.net:9421`) |

**Why a GPU pod and not a CPU pod:** `RunpodTargetConfig` (src/fwd/config.py:93) exposes no `compute_type` or
`cloud_type`, and `RunpodBackend._create_pod` (src/fwd/backends/runpod.py:309) never passes `--compute-type cpu` or
`--cloud-type COMMUNITY`. Through the CLI a RunPod target is therefore *always* a secure-cloud GPU pod. The cheapest
secure GPU that was in stock (A4000, $0.25/hr) was used. See "Gaps" below.

## Result summary

| Step | Result |
| --- | --- |
| 1. Add target, `fwd doctor --target fwd-test-live` | **PASS** (all checks ok) |
| 2. `fwd up --no-attach --handoff` | **FAIL then PASS** — blocked by bug **L1** (rsync chown on the RunPod volume); passed with the workaround, exit 0 |
| 3. `fwd ls` / `fwd push` / `fwd pull` | **PASS** |
| 4. Re-run `fwd up` (idempotent repair) | **PASS** — one pod, bootstrap skipped by marker, tmux left alone |
| 5. `fwd stop` → restart via `fwd up` | **PARTIAL** — pod stops/starts and the endpoint re-resolves correctly, but the restarted session has **no working `claude`** (bug **L3**); passes only after clearing the stale bootstrap marker |
| 6. `fwd rm --force` | **PASS** |
| 7. Attach fidelity probe | **PASS** |

Three bugs found (**L1**, **L2**, **L3**); none fixed in-tree, all documented below with repro. All three are outside
the "trivial ops/cli glue" fix mandate.

---

## Step-by-step

### 1. Config + doctor — PASS

`fwd doctor --target fwd-test-live` reports ok for ssh, rsync, tar, tmux, claude, runpodctl, state, Claude
credentials, config (1 target, sourced from the project file), and for the target: runpodctl 2.6.0-5516265, supported
syntax, api key found, `31 pod(s) visible`.

### 2. `fwd up --target fwd-test-live --name test-live --no-attach --handoff`

First attempt died in the sync stage; see **L1**. With the rsync workaround in place the run finished **exit 0** in
~1 min 40 s (excluding the 60 s HANDOFF generation):

```
✓ Looking up RunPod pod fwd-test-live / Creating pod (NVIDIA RTX A4000, 20 GB volume) 1.4s
✓ Waiting for pod fwd-test-live to expose ssh 33.1s
✓ Waiting for SSH on 157.157.221.29:20660 2.7s
✓ Generating HANDOFF.md 64.1s
✓ Syncing proj to /workspace/proj 4.5s
fwd: uv present / installing bun (1.3.14) / installing claude CLI (2.1.220) / tmux present (3.4)
✓ Bootstrapping remote tooling 20.4s
✓ Installing project dependencies (1 step(s)) 3.4s     (+ six==1.17.0)
✓ Starting remote session 'fwd-test-live' 0.5s
✓ session 'test-live' ready
```

Verified on the pod:

- `/workspace/proj` contains `.fwd .git .gitignore CLAUDE.md HANDOFF.md main.py pyproject.toml uv.lock` — the local
  `.venv/junk/local-only.txt` is **absent** (`ls /workspace/proj/.venv/junk` → No such file). The remote `.venv` that
  does exist was created by the remote `uv sync`, not synced.
- Tooling under the prefix: `/workspace/.fwd-tools/{bin,bun,fwd-env.sh}`; `claude` 2.1.220 resolves to
  `/workspace/.fwd-tools/bin/claude`. (`uv` and `tmux` were already in the image, so bootstrap's `command -v` guards
  correctly skipped them — see **L3** for why that is not as harmless as it looks.)
- `cd /workspace/proj && uv run python -c "import six"` → `six ok 1.17.0`.
- `tmux has-session -t =fwd-test-live` → rc 0, and `tmux capture-pane` shows the live Claude Code TUI:

```
Welcome to Claude Code v2.1.220
 Let's get started.
 Choose the text style that looks best with your terminal
 ❯ 2. Dark mode ✔
```

That is the expected unauthenticated first-run state. No authentication was attempted.

**HANDOFF:** `claude -p` actually succeeded locally, so a *real* HANDOFF.md was generated (22 lines, project-specific)
and synced. The template fallback was verified separately by calling `claude_state.make_handoff()` with `claude` off
PATH: it warns `HANDOFF.md generation fell back to a template ([Errno 2] ... 'claude')` and writes the TODO-marked
template. Both branches work.

### 3. `fwd ls`, `fwd push`, `fwd pull` — PASS

- `fwd ls` → one row: `test-live | runpod | running | fwd-test-live | /private/... | pod_id=…`.
- Touched `push-probe.txt` locally → `fwd push` (2.8 s) → file readable on the pod with matching content.
- Created `/workspace/proj/pull-probe.txt` remotely → `fwd pull` (1.9 s) → file present locally with matching content,
  and the local `.venv` was not clobbered (excludes hold on the pull path too).

### 4. Idempotent re-run — PASS

A second `fwd up` with the same target/name:

```
· reusing session 'test-live' on target 'fwd-test-live'
! reusing existing pod fwd-test-live
fwd: bootstrap 1 already applied at /workspace/.fwd-tools (marker present), skipping
✓ Installing project dependencies (1 step(s)) 0.8s   (Audited 1 package)
· remote tmux session 'fwd-test-live' is already running; leaving it as is
```

`runpodctl pod list -a` still shows exactly one `fwd-*` pod. Repair semantics are correct: nothing re-provisioned,
nothing restarted, no duplicate billing.

Cost note: every `fwd up` re-runs `claude -p` for HANDOFF.md (~60–70 s and a real API call each time) even when
HANDOFF.md already exists and the session is only being repaired. Not a bug, but it dominates the runtime of an
otherwise ~10 s repair.

### 5. `fwd stop` → restart — PARTIAL (bug L3)

- `fwd stop` → kills remote tmux, `pod stop`; `runpodctl pod get` → `desiredStatus: EXITED`. **PASS.**
- `fwd attach` against the stopped pod: the STOPPED branch is reached and it *restarts* the pod. Note for scripted use:
  `ui.confirm` (src/fwd/ui.py:121) returns the **default** when stdin is not a tty, and the restart prompt defaults to
  `True`, so a non-interactive `fwd attach` silently rents the pod again. Defensible, but worth knowing.
- Port churn confirmed exactly as docs/runpod-notes.md predicts: `20660 → 20663` (IP unchanged). `fwd up` re-resolved
  it, `wait_for_ssh` succeeded on the new port, and `~/.fwd/state.json` was rewritten with `"port": 20663`. **PASS.**
- Re-sync + dep audit after the wipe: **PASS** (`/workspace` survived, `uv sync` audited 1 package).
- tmux relaunch: `fwd up` reported `✓ Starting remote session 'fwd-test-live'` and exited 0, but the session was
  **dead seconds later** (`no server running on /tmp/tmux-0/default`) because `claude` no longer exists on the
  restarted pod. This is bug **L3**. After clearing the stale bootstrap marker (workaround), a further `fwd up`
  reinstalled claude (`fwd: claude installed: 2.1.220`, bootstrap 10.4 s) and produced a live tmux session
  (`tmux has-session` rc 0) showing the Claude TUI again — so the restart flow itself is sound once bootstrap is
  allowed to run.

### 6. `fwd rm --force` — PASS

`fwd rm --force` deleted the pod (`runpodctl pod list -a` shows no `fwd-test-*`) and removed the `test-live` entry
from `~/.fwd/state.json`.

### 7. Attach fidelity — PASS

`fwd attach` ends in `os.execvp`, which cannot be held open from a script, so the exec'd argv was validated instead of
the wrapper:

1. Obtained the exact argv fwd would exec, via `remote.tmux_attach_argv(endpoint, session.tmux_session)`:
   `ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -t -o ControlMaster=auto -o ControlPath=~/.fwd/cm/... -o ControlPersist=10m -p 20660 root@157.157.221.29 '<env>; tmux attach -t =fwd-test-live'`
2. Ran that argv verbatim under `script -q` (a real pty) in the background.
3. From a **second** ssh connection: `tmux list-clients -t fwd-test-live` →
   `/dev/pts/1: fwd-test-live [80x24 xterm-256color] (attached,focused,UTF-8)` — a genuine interactive client.
4. `tmux send-keys -t fwd-test-live:0.0 Down` from that second connection, then `capture-pane` before/after: the
   Claude theme-picker selection moved `❯ 2. Dark mode` → `❯ 3. Light mode`, proving the TUI is live and interactive.
5. The local pty log contains the tmux status line (`[fwd-test-0:claude*  "f1e6fa1ff6ba" 01:02 27-Jul-26`), proving
   the attach rendered locally rather than merely connecting.
6. Killed the local client; `tmux list-clients` then empty while `tmux has-session` still rc 0 — detach leaves the
   session running, which is the entire persistence promise.

One observation on the argv: it carries `-o BatchMode=yes`. That is right for the non-interactive stages, but on an
attach it also disables password and passphrase prompts, so a target that needs either cannot be attached to. Not
exercised here (RunPod uses a passphrase-less key).

---

## Bugs

### L1 — `rsync -az` fails on a RunPod network volume; any nonzero rsync exit aborts the launch (BLOCKER)

- **Where:** `src/fwd/sync.py:31` (`RSYNC_BASE = ("rsync", "-az")`) and `src/fwd/sync.py:64-68` (`_run` treats every
  nonzero exit as fatal). Surfaces through `ops/launch.py:424` (`_sync_project`) and `ops/transfer.py`.
- **Repro:** any `fwd up` onto a RunPod pod whose `/workspace` is a MooseFS network volume (observed on this A4000
  pod; docs/runpod-notes.md records the same volume type on the earlier community A4000).
- **Actual:**

```
rsync: [generator] chown "/workspace/proj/.git" failed: Operation not permitted (1)
... (one line per directory and per temp file) ...
SSHError: rsync push failed (exit 23)
```

  and the whole launch aborts — after the pod has been rented, HANDOFF generated and ssh established — printing a raw
  Rich traceback rather than a `ui.die` message.
- **Expected:** the push succeeds. The files *do* transfer; only the ownership fixups fail.
- **Cause:** `-a` implies `-o -g`. The remote is root, but the mfs volume refuses `chown` to a foreign uid. Verified
  directly on the pod: `chown root:root /workspace/x` → rc 0, `chown 1000:1000 /workspace/x` → `Operation not
  permitted`. Local files are uid 501, so every file triggers it.
- **Fix (not applied):** add `--no-owner --no-group` to `RSYNC_BASE` (owner preservation is meaningless anyway — the
  remote is a single-user container), and/or treat rsync exits 23/24 (partial transfer due to vanished/errored files)
  as warnings rather than failures. Note ordering matters: rsync applies options left to right, so the flags must come
  *after* `-a`.
- **Workaround used for the rest of this run:** a PATH shim `<scratchpad>/live-e2e/shim/rsync` that runs
  `exec /usr/bin/rsync "$@" --no-owner --no-group`. With it, every push/pull in this report returned 0. The shim is
  outside the repo and was only on PATH for the test invocations.

### L2 — `remote._source_env()` expands `$FWD_TOOL_PREFIX`, which is never set on a remote command

- **Where:** `src/fwd/remote.py:55-61`. The default argument is the literal string `"$FWD_TOOL_PREFIX"`, and *every*
  caller uses the default: `run_dep_install` (:131), `tmux_new` (:157,159), `tmux_attach_argv` (:168), `tmux_kill`
  (:174), `tmux_exists` (:180).
- **Repro:** `ssh <pod> 'echo "[$FWD_TOOL_PREFIX]"'` → `[]`. A non-interactive ssh command reads neither `.bashrc`
  (Ubuntu's returns early for non-interactive shells) nor `.profile`, so the guard `if [ -f
  "$FWD_TOOL_PREFIX/fwd-env.sh" ]` is always false and the env file is never sourced.
- **Expected:** bootstrap.sh:46-49 explicitly documents the contract — it writes `$HOME/.fwd-env.sh` as the
  "Fixed-location pointer sourced by `fwd.remote._source_env`", precisely because the prefix is unknown to a remote
  shell. `remote.py` sources the wrong path, so the pointer file is dead code.
- **Impact:** every dep-install and tmux command runs with the image's default PATH. It went unnoticed here only
  because `runpod/base` ships `uv` and `tmux` in `/usr/bin`. On an image without them, `fwd up` would fail at
  dependency install or at `tmux new-session` even though bootstrap installed both under the prefix. The Claude
  command itself is unaffected: `launch.build_tmux_command` (:189) bakes the *concrete* `tool_prefix` path in.
- **Fix (not applied):** source `$HOME/.fwd-env.sh` (as bootstrap intends), or pass the concrete prefix down from
  `ops/launch.py`. Note that on RunPod `$HOME/.fwd-env.sh` is on the container disk and is wiped by a stop, so it must
  also be rewritten on every bootstrap (it is, since bootstrap re-runs after a wipe — unless L3's marker bug prevents
  that).

### L3 — after a pod restart the session comes up without `claude`, and `fwd up` reports success anyway

- **Where:** `src/fwd/scripts/bootstrap.sh:170-186` (`install_claude`) plus the marker logic at :50 / :25-27.
- **Repro:** `fwd up` → `fwd stop` → `fwd up` on a RunPod target.
- **Actual (measured):** after the restart, `/workspace/.fwd-tools/bin/claude` is a **dangling symlink** to
  `/root/.local/bin/claude`; `/root/.local/bin` no longer exists; `bash -lc '. /workspace/.fwd-tools/fwd-env.sh;
  claude --version'` → `claude: command not found`. `fwd up` nonetheless printed
  `fwd: bootstrap 1 already applied at /workspace/.fwd-tools (marker present), skipping`, then
  `✓ Starting remote session 'fwd-test-live'` and `✓ session 'test-live' ready` with exit 0 — while the tmux session
  had already died (`no server running on /tmp/tmux-0/default`).
- **Expected:** either the tooling survives the wipe, or bootstrap notices it is gone and reinstalls.
- **Cause, two compounding parts:**
  1. The claude native installer writes to `$HOME/.local/bin` (container disk). `CLAUDE_INSTALL_DIR`/`INSTALL_DIR`
     were both passed but the installer ignored them — `$FWD_TOOL_PREFIX/claude/` was never created. bootstrap then
     symlinks the container-disk binary into the prefix, so the *pointer* persists on the volume while the *payload*
     does not. This defeats the plan's "install ALL tooling under /workspace" requirement.
  2. The version marker `.fwd-bootstrap-1` lives on the volume and therefore survives the wipe, so the coarse
     idempotence check short-circuits before the fine-grained `command -v` guards can notice the missing binary. A
     restarted pod is thus *permanently* broken: every subsequent `fwd up` skips bootstrap.
- **Secondary:** `remote.tmux_new` only checks that `tmux new-session` returned 0. A session whose command exits
  immediately still returns 0, so fwd reports a ready session that is already gone. A `tmux has-session` re-check (or
  a capture-pane) after creation would have turned this silent failure into a real error.
- **Fix (not applied):** verify the claude payload (not just a symlink) resolves before honouring the marker — e.g.
  make the marker check also require `command -v claude` and `command -v uv` to succeed — and copy the installed
  binary into `$FWD_TOOL_PREFIX/bin` instead of symlinking to `$HOME`.
- **Workaround used:** `rm /workspace/.fwd-tools/.fwd-bootstrap-1 /workspace/.fwd-tools/bin/claude` on the pod, then
  `fwd up` again — bootstrap reinstalled claude and the tmux session then stayed up with the Claude TUI running.

## Gaps observed (not bugs)

- **No CPU / community-cloud RunPod targets.** `RunpodTargetConfig` has no `compute_type`/`cloud_type`/
  `container_disk_gb`, and `_create_pod` never emits `--compute-type` or `--cloud-type`. The cheapest thing a user can
  launch through fwd today is a secure-cloud GPU pod. Since docs/runpod-notes.md already documents that CPU pods get
  no volume, a `compute_type = "cpu"` option would need to pair with a container-disk `remote_base`; a
  `cloud_type = "COMMUNITY"` option is a pure win for cost and is one flag.
- `fwd up --handoff` regenerates HANDOFF.md (a full `claude -p` round trip, ~60–70 s) on every repair run.

## Spend and cleanup

- **Spend:** one pod, `d0dy46ht8ddtmy`, RTX A4000 secure cloud at **$0.25/hr**, created 00:48 UTC and deleted 01:12
  UTC — 24 min wall, of which ~4 min were stopped (volume-only billing). **≈ $0.09 of compute**, plus five local
  `claude -p` HANDOFF calls. No other pod was created at any point.
- **Pods:** `runpodctl pod list -a` → **zero** `fwd-test-*` pods remain, and the account total is back to the 31 pods
  (all pre-existing, none fwd's) that were there before the run. `runpodctl pod get d0dy46ht8ddtmy` → `pod not found`
  (404).
- **Config:** `~/.fwd/config.toml` never existed and was never created. All target config lived in the scratch
  project's `.fwd/config.toml`, which is outside the repo.
- **State:** `~/.fwd/state.json` was empty (0 sessions) before the run; the only entry created, `test-live`, was
  removed by `fwd rm --force`. No other entries were touched.
- **Repo:** no source file was modified. This report is the only file added.
