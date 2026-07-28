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
and synced. The template fallback was verified separately by calling `agents.claude_state.make_handoff()` with `claude` off
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
  `True`, so a non-interactive `fwd attach` silently provisions the pod again. Defensible, but worth knowing.
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

  and the whole launch aborts — after the pod has been provisioned, HANDOFF generated and ssh established — printing a raw
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

---

# Round 2

Re-run on **2026-07-27** by teammate G, after every Round-1 bug (**L1**, **L2**, **L3**) had been fixed in-tree. Same
protocol as Round 1: the real CLI only, `--no-attach --handoff`, non-interactive, from a scratch project outside the
repo. Two legs were exercised — a **GPU restart leg** (reusing the pod a stalled prior attempt had left behind) and a
**CPU leg** (the `compute_type = "cpu"` path that did not exist in Round 1).

## Test rig

| Item | Value |
| --- | --- |
| Scratch project | `<scratchpad>/live-e2e/proj` — `pyproject.toml` + `uv.lock` (one dep: `six`), `CLAUDE.md`, `main.py`, `.gitignore`, git repo with one commit, junk `.venv/junk/local-only.txt` |
| Config isolation | project-level `<proj>/.fwd/config.toml` only; `~/.fwd/config.toml` never existed |
| GPU target | `[targets.fwd-test-live]` — `compute_type = "gpu"`, `cloud_type = "community"`, `gpu = "NVIDIA RTX A2000"`, `volume_gb = 20`, image `runpod/base:1.0.2-ubuntu2404`, paths under `/workspace` |
| CPU target | `[targets.fwd-test-cpu]` — `compute_type = "cpu"`, `cloud_type = "secure"`, otherwise identical |
| GPU pod | `c5q4u9qihl7rv3` / `fwd-test-gpu`, 1× RTX A2000-class, **$0.17/hr**, 20 GB volume (pre-existing from the stalled attempt) |
| CPU pod | `wplwwpisa5fgfv` / `fwd-test-cpu`, 0 GPU, **$0.06/hr**, `volumeInGb: 0` |

## Result summary

### Leg A — GPU restart leg

| # | Sub-check | Result |
| --- | --- | --- |
| A1 | `fwd up` with an empty `state.json` **reuses** pod `fwd-test-gpu` by name instead of creating a second one | **PASS** — `! reusing existing pod fwd-test-gpu`; `pod list -a` still showed exactly one `fwd-test-*` pod |
| A2 | Bootstrap marker validity check honours a genuinely healthy prefix | **PASS** — `bootstrap 1 already applied … skipping` in 0.7 s, `uv`/`claude` both verified to execute |
| A3 | Session healthy: tmux alive + claude running | **PASS** — `tmux has-session` rc 0; `capture-pane` shows the Claude Code theme picker (expected unauthenticated first-run TUI) |
| A4 | **L1 fix live:** rsync push onto the MooseFS `/workspace` volume | **PASS** — `Syncing proj to /workspace/proj 2.6s`, exit 0, zero `chown` errors. `.venv/junk/local-only.txt` correctly absent remotely |
| A5 | **L3 fix live:** claude payload lives under the tool prefix, not `$HOME` | **PASS** — `/workspace/.fwd-tools/bin/claude -> /workspace/.fwd-tools/claude/.local/bin/claude`, i.e. on the volume |
| A6 | `uv run python -c "import six"` on the pod | **PASS** — `six ok 1.17.0` |
| A7 | `fwd attach </dev/null` on a **RUNNING** pod fails gracefully | **PASS (with a caveat)** — exit 1, no traceback, but the message is tmux/ssh's own (`Pseudo-terminal will not be allocated because stdin is not a terminal` / `open terminal failed: not a terminal`) rather than an fwd-branded one. See **R2-2**. |
| A8 | `fwd stop` → pod actually stops | **PASS** — remote tmux killed, `desiredStatus: EXITED`, `Exited by user` |
| A9 | **CRITICAL:** `fwd attach </dev/null` on a STOPPED pod, **without** `--restart`, must refuse | **PASS** — `! session 'test-gpu': target is stopped` then `x refusing to restart billable compute without confirmation because this is not an interactive terminal; re-run with --restart if that is what you want`, exit 1, and the pod **stayed EXITED**. The Round-1 money hazard is closed. |
| A10 | **CRITICAL:** after restart, bootstrap detects the wiped container disk and repairs, with **no manual marker deletion** | **PASS** — see below |
| A11 | Restarted session ends with live tmux running claude | **PASS** — `tmux has-session` rc 0 and `capture-pane` shows the live Claude TUI; `claude --version` → 2.1.220, `import six` → 1.17.0 |
| A12 | `fwd rm --force` deletes the pod | **PASS** — `runpodctl pod get c5q4u9qihl7rv3` → `pod not found` (404); state entry removed |

**A10 detail.** The restart `fwd up` printed, in order:

```
✓ Starting stopped pod fwd-test-gpu 1.2s
✓ Waiting for pod fwd-test-gpu to expose ssh 13.1s
! pod was restarted — the container disk was wiped, only the volume survived
✓ Waiting for SSH on 87.197.146.56:40678 2.4s
· reusing HANDOFF.md from 3 min ago (delete it to force regeneration)
fwd: bootstrap v1 prefix=/workspace/.fwd-tools …
fwd: wrote /workspace/.fwd-tools/fwd-env.sh
fwd: wrote /root/.fwd-env.sh
fwd: uv present: uv 0.10.9   /   bun present: 1.3.14   /   claude present: 2.1.220 (Claude Code)
✓ Bootstrapping remote tooling 1.0s
✓ Starting remote session 'fwd-test-gpu' 2.8s
```

Two of the three Round-1 fixes are visible in that transcript. Bootstrap re-ran (rather than short-circuiting on the
surviving marker) because `bootstrap_is_valid` also requires `$HOME/.fwd-env.sh`, which the wipe removed — that is the
check doing exactly the job it was added for. And it re-ran *cheaply* (1.0 s, `claude present` rather than
`claude installed`), because the claude payload now lives on the volume under the tool prefix; Round 1 had to
re-download it. Port churn behaved as documented (`40608 → 40678`, IP unchanged) and was re-persisted to state.
`Starting remote session … 2.8s` reflects the new `tmux_new` liveness re-check. HANDOFF.md was **reused** rather than
regenerated, removing the ~60 s `claude -p` round trip Round 1 flagged as a cost note.

### Leg B — CPU leg

| # | Sub-check | Result |
| --- | --- | --- |
| B1 | `runpodctl pod create` accepts the CPU flag matrix (`--compute-type CPU`, `--cloud-type SECURE`, **no** GPU flags) | **PASS** — pod created in 1.5 s; `pod list -a` confirms `gpuCount: 0`, `$0.06/hr`. `nvidia-smi` absent on the pod, as expected |
| B2 | `volumeInGb == 0` detected, loud relocation warning emitted | **PASS** — `! pod has no persistent volume — /workspace does not exist on this pod, so files live on the container disk at /root/fwd/workspace and will be WIPED on stop (CPU pods silently ignore volume_gb; use a GPU pod to persist)`. Note the message's factual slip — see **R2-3** |
| B3 | `remote_dir` (and `tool_prefix`, scratch) relocated under `/root/fwd` | **PASS** — `remote_dir=/root/fwd/workspace/proj`, `prefix=/root/fwd/workspace/.fwd-tools`, `scratch=/root/fwd/workspace/.fwd-cache` |
| B4 | **L1 fix live** on a container-disk overlayfs | **PASS** — `Syncing proj to /root/fwd/workspace/proj 2.2s`, exit 0; `.venv/junk` correctly absent remotely |
| B5 | Full bootstrap from scratch (nothing pre-installed under the prefix) | **PASS** — 10.7 s: `installing bun into …/.fwd-tools/bun` → 1.3.14, `installing claude CLI into …/.fwd-tools/claude` → 2.1.220, `uv present`, `tmux present`, `bootstrap complete`. `command -v claude` → `/root/fwd/workspace/.fwd-tools/bin/claude` |
| B6 | `uv sync` + `uv run python -c "import six"` | **PASS** — venv created, `+ six==1.17.0`, then `six 1.17.0` on the pod |
| B7 | tmux alive with claude running | **PASS** — `tmux has-session` rc 0; `capture-pane` shows the live Claude TUI |
| B8 | `fwd push` / `fwd pull` round trip | **PASS** — push 1.7 s (probe file content matched remotely), pull 1.5 s (probe file content matched locally), local `.venv/junk/local-only.txt` untouched by the pull |
| B9 | `fwd rm --force` → pod 404 | **NOT RUN BY THIS AGENT — see caveat** |

**B9 caveat (stated explicitly rather than guessed).** This session was interrupted by a process restart. When work
resumed, pod `wplwwpisa5fgfv` had already been deleted and the `test-cpu` entry already removed from
`~/.fwd/state.json` by something outside this agent's command history. The *end state is verified* (zero `fwd-test-*`
pods, empty `state.json` — see Cleanup) but the `fwd rm --force` **command itself was not observed on the CPU
session**. The same command *was* observed end-to-end on the GPU session (A12, → 404), so the destroy path is
validated; only the CPU repetition of it is unwitnessed.

One further CPU-leg observation could not be completed for the same reason: a clean test of whether `fwd push`'s
`--delete` actually removes a remote-only file. An earlier, sloppily-sequenced probe *suggested* a remote-only file
survived a push, but the pod was gone before a clean repro could be run. **Recorded as inconclusive, not as a bug.**

## New findings

### R2-1 — a transient `runpodctl` failure is indistinguishable from a deleted pod, and fwd offers to delete state (minor, real)

- **Where:** `src/fwd/backends/runpod.py:502-511` (`RunpodBackend.status` maps any `RunpodError` to
  `TargetStatus.GONE`) and `src/fwd/ops/launch.py:551-556` (`status_of` maps any `Exception` to `GONE`).
- **Observed:** immediately after `fwd stop` succeeded, the very next `fwd attach test-gpu </dev/null` printed
  `! the runpod target behind session 'test-gpu' no longer exists` and offered to prune the session entry — while
  `runpodctl pod get c5q4u9qihl7rv3` from the shell, seconds later, reported a perfectly healthy
  `desiredStatus: EXITED`. Re-running the same `fwd attach` then correctly reported `target is stopped` (that is the
  A9 PASS above). Calling `backend.status(session)` directly in-process at that point returned `stopped`.
- **Diagnosis:** `pod_status` (:192) maps `EXITED` → `STOPPED` correctly, and `error_message` (:102) correctly ignores
  the nested `ssh.error: "pod not ready"` that a stopped pod carries. So the `GONE` came from the exception funnel:
  one transient provider call failed (RunPod's API is briefly flaky right after a stop) and both layers collapse
  "cannot ask" into "does not exist".
- **Why it matters:** the `GONE` branch's remedy is *deleting the user's session entry*. It is currently gated behind a
  `default=False` confirm, and in a non-interactive run that default means nothing is deleted — which is the only
  reason this was harmless here. An interactive user hitting the same blip would be invited to throw away a live
  session's state.
- **Suggested fix (not applied):** distinguish a confirmed 404 (`is_missing_pod_error`, already written at :209) from
  any other failure. Only the former is `GONE`; everything else should surface as "could not determine status" and
  refuse to offer destructive remedies. A single retry inside `_get_pod` would also absorb the blip.
- **Repro:** `fwd up` → `fwd stop` → immediately `fwd attach <name> </dev/null`. Racy by nature; it reproduced once in
  this run and not on the retry.

### R2-2 — non-tty `fwd attach` on a healthy target leaks ssh/tmux's error instead of fwd's (cosmetic)

- **Where:** `src/fwd/ops/launch.py` `exec_attach` / `src/fwd/remote.py` `tmux_attach_argv`.
- **Observed:** `fwd attach test-gpu </dev/null` against a RUNNING, healthy pod exits 1 with
  `Pseudo-terminal will not be allocated because stdin is not a terminal` and `open terminal failed: not a terminal`.
- **Assessment:** functionally fine — it fails, it fails fast, it exits nonzero, and it does not spend money, so A7 is
  a pass. But since `attach` already inspects `sys.stdin.isatty()` for the restart gate
  (`src/fwd/ops/attach.py:123`), the same test could produce a one-line `ui.die` such as "attach needs an interactive
  terminal; use `fwd up --no-attach` in scripts" instead of exec'ing into ssh to discover it.

### R2-3 — the no-volume relocation warning claims the mount "does not exist" when it usually does (cosmetic)

- **Where:** `src/fwd/backends/runpod.py:274-277` (the note built in `resolve_paths`).
- **Observed on the CPU pod:** the warning says `/workspace does not exist on this pod`, but `/workspace` *did* exist —
  it was a real, writable directory on the container-disk overlay (`ls -la /workspace` succeeded, `touch /workspace/x`
  succeeded, `df` showed it on the same 20 GB `overlay` as `/`). What is absent is the *persistent volume*, not the
  path.
- **Why it matters:** the relocation decision is still exactly right (writing to `/workspace` would silently lose data
  on stop, which is worse than relocating), so this is purely a wording problem — but a user who checks and finds the
  directory sitting there will reasonably conclude fwd is confused. Wording along the lines of "`/workspace` is not
  backed by a persistent volume on this pod" would match reality.

### R2-4 — the pod-create step label prints a GPU name for CPU pods (cosmetic)

- **Where:** `src/fwd/backends/runpod.py:453` —
  `ui.step(f"Creating pod {pod_name} ({gpu or cfg.gpu}, {cfg.volume_gb} GB volume)")`.
- **Observed:** creating the CPU pod printed `✓ Creating pod fwd-test-cpu (NVIDIA GeForce RTX 4090, 20 GB volume)`. Both
  halves are wrong for a CPU target: `cfg.gpu` is an unused default that `create_pod_args` (:246-249) deliberately does
  not send, and `volume_gb` is silently ignored by RunPod for CPU pods (which is precisely what B2 then warns about, one
  line later). The label should branch on `cfg.compute_type`.

## Round-1 bugs — verification status

| Bug | Status |
| --- | --- |
| **L1** (rsync `chown` on a RunPod volume aborts the launch) | **FIXED, verified live** on both a MooseFS network volume (A4) and a container-disk overlay (B4); every push/pull in this round returned 0 with no shim on PATH |
| **L2** (`_source_env` expanded an unset `$FWD_TOOL_PREFIX`) | **FIXED, verified live** — bootstrap writes `/root/.fwd-env.sh` and remote commands resolve prefix-installed tools; `command -v claude` returns the prefix path on both pods |
| **L3** (restart leaves no working `claude`; `fwd up` reports success anyway) | **FIXED, verified live** (A5, A10, A11) — payload under the prefix on the volume, marker check requires `$HOME/.fwd-env.sh` plus executable `uv`/`claude`, tmux liveness re-checked after start. No manual marker deletion was needed at any point |
| Round-1 gap: no CPU / community-cloud targets | **CLOSED** — `compute_type`/`cloud_type` land correctly in the create argv, and the no-volume relocation works end-to-end (Leg B) |
| Round-1 cost note: HANDOFF regenerated on every repair | **CLOSED** — `· reusing HANDOFF.md from 3 min ago (delete it to force regeneration)` |

## Spend and cleanup

- **Spend:** two pods. `c5q4u9qihl7rv3` (RTX A2000-class, $0.17/hr) was inherited already-running from the stalled
  attempt and was live for roughly 85 min in total across both attempts, of which ~2 min were stopped →
  **≈ $0.24**. `wplwwpisa5fgfv` (CPU, $0.06/hr) ran ~20 min → **≈ $0.02**. **Round-2 total ≈ $0.26 of compute**, plus
  one local `claude -p` HANDOFF call (the second and third launches reused the file).
- **Pods:** `runpodctl pod list -a` → **zero** `fwd-test-*` pods. `runpodctl pod get c5q4u9qihl7rv3` → 404. Every
  remaining pod on the account is pre-existing and unrelated to fwd.
- **State:** `~/.fwd/state.json` is back to `{"sessions": {}, "version": 1}` — no `fwd-test` entries.
- **Config:** `~/.fwd/config.toml` still does not exist. All target config lived in the scratch project's
  `.fwd/config.toml`, outside the repo.
- **Repo:** no source file was modified. This Round 2 section is the only change.

---

# Round 2

Run on **2026-07-27** by teammate G, after every Round-1 bug (**L1**, **L2**, **L3**) had been fixed in-tree. Same
protocol as Round 1: the real CLI (`uv run --project ~/Coding/Python/fwd fwd ...`) driven from a scratch uv project,
always `--no-attach --handoff`, always non-interactive. **No rsync shim this time** — Round 1's workaround was
deliberately removed so the L1 fix had to hold on its own.

## Test rig

| Item | Value |
| --- | --- |
| Scratch project | `<scratchpad>/live-e2e/proj` — `pyproject.toml` + `uv.lock` (one dep: `six`), `CLAUDE.md`, `main.py`, `.gitignore`, git repo with one commit, junk `.venv/junk/local-only.txt` |
| Config isolation | project-level `<proj>/.fwd/config.toml` only; `~/.fwd/config.toml` still does not exist |
| GPU target | `[targets.fwd-test-live]` — `compute_type = "gpu"`, `cloud_type = "community"`, `gpu = "NVIDIA RTX A2000"`, `volume_gb = 20`, paths under `/workspace` |
| CPU target | `[targets.fwd-test-cpu]` — `compute_type = "cpu"`, `cloud_type = "secure"`, `volume_gb = 20` (deliberately set, to prove it is ignored), paths under `/workspace` |
| GPU pod | `c5q4u9qihl7rv3` / `fwd-test-gpu`, 1× A2000-class, **$0.17/hr**, 20 GB volume — pre-existing from a stalled attempt, adopted by reuse-by-name |
| CPU pod | `wplwwpisa5fgfv` / `fwd-test-cpu`, `gpuCount: 0`, `volumeInGb: 0`, **$0.06/hr** |

## Result summary

### A — restart leg (GPU pod)

| Sub-check | Result |
| --- | --- |
| A1 `fwd up` reconciles an empty state and **reuses pod by name** instead of creating a second one | **PASS** |
| A2 First launch healthy: rsync push (no shim), bootstrap marker honoured, `uv sync`, tmux alive, claude TUI in `capture-pane` | **PASS** |
| A3 `fwd attach </dev/null` on a **RUNNING** pod fails gracefully | **PASS** (see note R2-N1) |
| A4 `fwd stop` → `runpodctl pod get` reports `EXITED` | **PASS** |
| A5 `fwd attach </dev/null` on a **STOPPED** pod **without** `--restart` REFUSES (critical) | **PASS** |
| A6 …and does not spend money — pod still `EXITED` afterwards | **PASS** |
| A7 `fwd up` restarts the pod, warns the container disk was wiped, re-resolves the new port | **PASS** |
| A8 **Bootstrap validity check detects the wiped container disk and repairs, with no manual marker deletion** (critical) | **PASS** |
| A9 After restart: live tmux running claude, verified by `capture-pane` | **PASS** |
| A10 `fwd rm --force` → pod 404 | **PASS** |

### B — CPU leg

| Sub-check | Result |
| --- | --- |
| B1 `runpodctl pod create` accepts the CPU flag matrix (`--compute-type CPU`, `--cloud-type SECURE`, **no GPU flags**) | **PASS** |
| B2 Created pod really is CPU-only: `gpuCount: 0`, `nvidia-smi` absent | **PASS** |
| B3 `volumeInGb == 0` detected and the **loud relocation warning** printed | **PASS** (message inaccurate — R2-2) |
| B4 `remote_dir` relocated under the container disk → `/root/fwd/workspace/proj`; `tool_prefix` → `/root/fwd/workspace/.fwd-tools` | **PASS** |
| B5 rsync push works with no shim (**live proof of the L1 fix**) | **PASS** |
| B6 Excludes hold: `.venv/junk/local-only.txt` absent remotely | **PASS** |
| B7 Bootstrap installs bun + claude **under the tool prefix** | **PASS** |
| B8 `uv sync` + `uv run python -c "import six"` → `six 1.17.0` | **PASS** |
| B9 tmux alive with the claude TUI | **PASS** |
| B10 `fwd push` / `fwd pull` round-trip | **PASS** |
| B11 `fwd rm --force` → pod 404 | **NOT RUN BY G** — the pod and its state entry were removed out-of-band during a process restart before G reached this step. The identical code path was exercised and passed as A10, so `rm` itself is validated; the CPU-specific run is simply missing. |

### C — teardown

| Sub-check | Result |
| --- | --- |
| C1 Zero `fwd-test-*` pods on the account (`runpodctl pod list -a`) | **PASS** |
| C2 `runpodctl pod get c5q4u9qihl7rv3` → 404 | **PASS** |
| C3 `~/.fwd/state.json` has no `fwd-test` / `test-gpu` / `test-cpu` entries (`{"sessions": {}, "version": 1}`) | **PASS** |
| C4 No source file modified; scratch config confined to the scratchpad | **PASS** |

## Round-1 fixes confirmed live

- **L1 (rsync chown) — FIXED.** Every push and pull in this round ran with the stock `rsync` on `PATH`; no shim
  existed. `RSYNC_BASE` with `--no-owner --no-group` returned 0 on the MooseFS `/workspace` volume *and* on the CPU
  pod's overlayfs. Exit 0, no warnings, no chown spam.
- **L2 (`$FWD_TOOL_PREFIX` never set on a remote command) — FIXED.** Bootstrap logs `fwd: wrote /root/.fwd-env.sh` and
  the pointer is what later steps source. Verified by hand: `. /root/.fwd-env.sh && claude --version` → `2.1.220`,
  `uv run python -c "import six"` → ok, on both pods.
- **L3 (restart leaves a claude-less session that fwd reports as ready) — FIXED, both halves.**
  1. *Payload under the prefix:* `/workspace/.fwd-tools/bin/claude → /workspace/.fwd-tools/claude/.local/bin/claude`.
     The target is on the **volume**, not `$HOME`, so a stop no longer strands the symlink. After the restart
     `claude --version` still answered `2.1.220` without reinstalling.
  2. *Marker is no longer trusted blindly:* `bootstrap_is_valid` (src/fwd/scripts/bootstrap.sh:68-86) additionally
     requires `$HOME/.fwd-env.sh` to exist and `uv`/`claude` to actually execute. The wipe removes
     `/root/.fwd-env.sh`, so the restarted pod re-ran bootstrap in full and rewrote it — **no manual
     `rm .fwd-bootstrap-1` was needed anywhere in this round.** Measured restart log:

```
✓ Starting stopped pod fwd-test-gpu 1.2s
! pod was restarted — the container disk was wiped, only the volume survived
✓ Waiting for SSH on 87.197.146.56:40678 2.4s          (port churn 40608 → 40678, re-resolved)
fwd: wrote /workspace/.fwd-tools/fwd-env.sh
fwd: wrote /root/.fwd-env.sh
fwd: uv present / bun present / claude present: 2.1.220 / tmux present
✓ Bootstrapping remote tooling 1.0s
✓ Starting remote session 'fwd-test-gpu' 2.8s
✓ session 'test-gpu' ready
```

  and `tmux has-session` → rc 0 with `capture-pane` showing the live Claude theme picker.
- **Non-tty restart gate — WORKS.** `_confirm_restart` (src/fwd/ops/attach.py:103) produced exactly the intended
  refusal, and the pod stayed `EXITED`:

```
! session 'test-gpu': target is stopped
x refusing to restart billable compute without confirmation because this is not an interactive terminal;
  re-run with --restart if that is what you want
```

- **`tmux_new` liveness re-check — WORKS.** `Starting remote session` now takes ~2.8 s (the ~2 s verify) instead of
  0.5 s, and the session was genuinely alive afterwards on every launch.
- **Bonus fix not in the Round-1 mandate:** HANDOFF.md is now reused (`· reusing HANDOFF.md from 3 min ago (delete it
  to force regeneration)`), removing the ~60-70 s `claude -p` round trip from every repair run. Round 1's cost note is
  resolved.

## New findings

### R2-1 — a transient `runpodctl` failure is reported as "the target no longer exists" (MEDIUM)

- **Where:** `RunpodBackend.status` (src/fwd/backends/runpod.py:502-511) swallows `RunpodError` into
  `TargetStatus.GONE`, and `ops/launch.status_of` (src/fwd/ops/launch.py:551-556) has a second, broader
  `except Exception: return TargetStatus.GONE`.
- **Observed:** immediately after a successful `fwd stop`, `fwd attach test-gpu </dev/null` printed
  `! the runpod target behind session 'test-gpu' no longer exists` and exited 1 — while the pod plainly existed
  (`runpodctl pod get c5q4u9qihl7rv3` → `"desiredStatus": "EXITED"`, and `pod list -a` listed it). Re-running the same
  command ~1 minute later correctly reported `stopped` and took the refuse-to-restart branch. A direct probe of
  `backend.status(session)` in between returned `stopped`, confirming the pod document was fine and the earlier answer
  came from a transient provider error.
- **Why it matters:** `GONE` is not a harmless mislabel. That branch offers to **delete the session entry**
  (`ops/attach.py:146-151`). Interactively the prompt defaults to `False`, so nothing was lost here, but a provider
  hiccup can present a healthy, *billing* pod as gone and invite the user to forget about it — leaving an orphaned pod
  running with no state entry pointing at it. This is exactly the failure mode that stranded `fwd-test-gpu` for the
  previous agent.
- **Repro:** hard to force on demand (it is a race against RunPod's API right after a state transition); reliably
  reproducible in principle by making `_run_ctl` raise, e.g. point `RUNPODCTL` at a binary that exits nonzero, then
  run `fwd attach` — fwd claims the target no longer exists rather than "could not reach RunPod".
- **Suggested fix (not applied):** distinguish "provider says 404" from "provider could not be reached". `_get_pod`
  already isolates the 404 case via `is_missing_pod_error`; only that should map to `GONE`. Any other error deserves a
  distinct status (or a re-raise that `attach` renders as "could not determine status; try again"), and the
  offer-to-delete path should never be reached on an inconclusive answer.

### R2-2 — the no-volume relocation warning states something false (LOW, message-only)

- **Where:** `resolve_paths` (src/fwd/backends/runpod.py:250-277), the note text at :271-274.
- **Says:** `pod has no persistent volume — /workspace does not exist on this pod, so files live on the container disk
  at /root/fwd/workspace and will be WIPED on stop`.
- **Actually:** on `fwd-test-cpu`, `/workspace` **did** exist and was writable — `ls -la /workspace` showed a
  `.cache/` subdir, `touch /workspace/x` succeeded, and `df -h /workspace` showed it on the same 20 GB `overlay` as
  `/`. It is simply container disk, not a volume. The *decision* to relocate is still correct (nothing under
  `/workspace` would survive a stop); only the stated reason is wrong, and a user who checks will conclude fwd is
  confused about their pod.
- **Suggested fix (not applied):** reword to "`/workspace` on this pod is container disk, not a persistent volume".

### R2-3 — the create step labels a CPU pod with a GPU name (LOW, cosmetic)

- **Where:** `src/fwd/backends/runpod.py:453` — `ui.step(f"Creating pod {pod_name} ({gpu or cfg.gpu}, {cfg.volume_gb} GB volume)")`.
- **Observed on the CPU launch:** `✓ Creating pod fwd-test-cpu (NVIDIA GeForce RTX 4090, 20 GB volume) 1.5s`. Both
  halves mislead: `cfg.gpu` is the untouched default and is *not* sent for a CPU pod (`create_pod_args` correctly
  omits every GPU flag), and the "20 GB volume" is the value RunPod is about to ignore.
- **Suggested fix (not applied):** print `CPU` when `compute_type == "cpu"`, and drop or qualify the volume clause.

### R2-N1 — note, not a bug: non-tty `fwd attach` on a RUNNING pod

`fwd attach test-gpu </dev/null` against a healthy pod exits **1** with tmux's own words:

```
Pseudo-terminal will not be allocated because stdin is not a terminal.
open terminal failed: not a terminal
```

It fails closed and does nothing harmful, which satisfies "attach-or-fail gracefully". The diagnostic is ssh's and
tmux's rather than fwd's, so a scripted caller gets no hint that `fwd attach` simply requires a terminal. A one-line
`ui.die("fwd attach needs an interactive terminal")` guard before `exec_attach` would be friendlier.

### Inconclusive (not filed)

A remote-only file (`pull-probe.txt`) appeared to survive a `fwd push` even though push passes `--delete`. G's probe
sequence was muddled by an interleaved failed invocation, and the CPU pod was destroyed out-of-band before a clean
test could be run, so this is **unverified** and may well be observer error. Worth one deliberate re-test next round:
create a remote-only file, `fwd push`, assert it is gone.

## Spend and cleanup

- **GPU pod** `c5q4u9qihl7rv3` at $0.17/hr: adopted already-running at 01:50 UTC, deleted at ~03:15 UTC. G's own
  session covered ~03:11-03:15 plus a ~1 min stopped window; the bulk of the pod's ~85 min lifetime predates G (the
  stalled attempt). **≈ $0.24 total for the pod's whole life, of which ≈ $0.02 is G's.**
- **CPU pod** `wplwwpisa5fgfv` at **$0.06/hr**: created ~03:15 UTC, gone by ~03:25 UTC. **≈ $0.01.**
- **Total for Round 2: well under $0.05 of new compute** (≈ $0.25 including the inherited GPU pod's idle time). Zero
  `claude -p` HANDOFF calls beyond the first — the reuse fix meant the file was generated once and reused three times.
- **Pods:** `runpodctl pod list -a` → **zero** `fwd-test-*` pods. The 31 remaining pods are all pre-existing and
  unrelated to fwd. `runpodctl pod get c5q4u9qihl7rv3` → `{"error":"api error: ... \"pod not found\",\"status\":404}`.
- **State:** `~/.fwd/state.json` is `{"sessions": {}, "version": 1}` — no `test-gpu`, no `test-cpu`, nothing else.
  It was also empty before the round.
- **Config:** `~/.fwd/config.toml` still does not exist. Both targets lived only in the scratchpad project's
  `.fwd/config.toml`.
- **Repo:** no source file modified. This section is the only change.

## Verdict

All three Round-1 bugs are fixed and hold up against real hardware, including the two hardest parts: rsync onto a
MooseFS volume with no shim, and a pod restart that self-repairs into a live claude session without any manual marker
surgery. The critical non-interactive restart gate refuses correctly and provably spends nothing. CPU pods now work
end to end, relocation and all. What remains is one medium-severity robustness gap (**R2-1**: an unreachable provider
is indistinguishable from a deleted pod, on a code path that offers to drop state) and two message-quality nits
(**R2-2**, **R2-3**).
