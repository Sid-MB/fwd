# S1 spike — can a Claude Code session transcript be relocated?

**Verdict: YES.** `--session` can ship. Verified empirically on **claude 2.1.220** (macOS, 2026-07-26).
A transcript copied into a *newly encoded* project directory resumes with full conversation context, appends
to the same session id, and needs no index/registry update.

Everything below is observed behaviour of the installed CLI, not documentation. All spike work was done on
**copies**; no existing file under `~/.claude` was modified, and no credential file was read.

## 1. Path-encoding scheme (`~/.claude/projects/<encoded-cwd>/`)

The directory name is the absolute cwd with **every character outside `[A-Za-z0-9]` replaced by `-`**.

Verified against all 38 real directories on this machine by extracting the `cwd` field from each transcript
and re-encoding it: **34 exact matches, 0 mismatches** (the remaining 4 dirs held no `cwd`-bearing lines).

Observed pairs that pin the interesting cases:

| cwd | directory |
| --- | --- |
| `/Users/sid/Coding/Python/fwd` | `-Users-sid-Coding-Python-fwd` |
| `/Users/sid/.shell/shared_scripts` | `-Users-sid--shell-shared-scripts` |
| `/Users/sid/Coding/Swift/Pocket-Congress/pocketcongress.org` | `-Users-sid-Coding-Swift-Pocket-Congress-pocketcongress-org` |
| `/Users/sid/Downloads/Believe It or Not Data` | `-Users-sid-Downloads-Believe-It-or-Not-Data` |
| `/private/tmp/claude-501/-Users-sid-Coding-Python-prov/<uuid>/scratchpad/m2-gate` | `-private-tmp-claude-501--Users-sid-Coding-Python-prov-<uuid>-scratchpad-m2-gate` |

So `/`, `.`, `_`, space and `-` all collapse to `-`; digits survive (`claude-501`). The encoding is **lossy and
not invertible** — fwd only ever needs the forward direction, which is why `encode_project_path` has no inverse.

## 2. On-disk layout (2.1.220)

```
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl     # the transcript
~/.claude/projects/<encoded-cwd>/<session-id>/          # sidecars: subagents/, memory/ (created on demand)
~/.claude/tasks/<session-id>/{1.json,2.json,.lock,...}  # todo state, keyed by session id, NOT by project
~/.claude/sessions/<pid>.json                           # live-process records (pid, sessionId, cwd, status)
```

- There is **no `sessions-index.json`, no sqlite db, no registry of any kind.** Discovery is pure filesystem
  scan of the encoded directory. Nothing has to be updated when a transcript is planted.
- `~/.claude/todos/` does **not** exist on 2.1.220; the todo state moved to `~/.claude/tasks/<session-id>/`.
  fwd ships both paths in the bundle so it keeps working across versions.
- `~/.claude/sessions/*.json` are per-process runtime records written at launch; they are irrelevant to resume.

## 3. Experiments run

Source transcript: a small 15-line scratch session (`2d8e95ba-…`) whose cwd was a disposable spike directory.

| # | Setup | Command (from the new cwd) | Result |
| - | ----- | -------------------------- | ------ |
| 1 | Copy JSONL into `projects/<enc(new cwd)>/`, rewriting the old cwd string → new cwd (12 lines changed) | `claude --resume <id> -p "Say READY and stop"` | **exit 0, "READY"**; transcript grew 15 → 23 lines in place, same session id (no fork) |
| 2 | Same dir, follow-up probe | `claude --resume <id> -p "what was the very first thing I asked…"` | **Correctly recalled the original first prompt** — real context continuity, not a fresh session |
| 3 | **Raw copy, no rewrite** (stale cwd strings inside the JSONL) into `projects/<enc(other new cwd)>/` | same probe | **Also worked**, full context recalled |
| 4 | Session id whose file is *not* in this cwd's encoded dir | `claude --resume <id> -p …` | exit 1, `No conversation found with session ID: <id>` |
| 5 | Listing sessions headlessly | `claude --resume -p "…"` | exit 1, `Error: --resume requires a valid session ID or session title when used with --print` |

### What this means

- **Directory placement is the only load-bearing step.** Resume resolution is `projects/<encode(cwd)>/<id>.jsonl`;
  it does not validate the `cwd` field recorded inside the transcript (experiment 3).
- The path rewrite is therefore about **content fidelity, not acceptance** — after rewriting, file paths the model
  quotes back and any `@file` references point at the remote tree instead of dead local paths. fwd still does it,
  two-pass (project cwd first, then home) so the longer, more specific prefix wins on overlapping paths.
- **No foreign-session rejection observed on 2.1.220.** The regression flagged in the plan's risk list
  (#18645, claude ≥ 2.1.9) did not reproduce for a relocated-but-same-machine transcript.
- Experiment 4 gives us a **cheap remote validation**: after installing the bundle, `claude --resume <id> -p` either
  finds it or prints `No conversation found` — but fwd deliberately does *not* burn a remote model call to check;
  it validates by confirming the file landed at the expected remote path.
- Experiment 5 means there is **no headless session picker**. fwd must always resolve a concrete session id locally
  (latest transcript by mtime) — it can never delegate the choice to the remote CLI.

## 4. Dry run of the shipped importer

The remote-side installer (`agents/claude_state.py::_IMPORT_SCRIPT`) was exercised locally under `bash` against a fake `$HOME`,
using a bundle produced by `export_session_bundle`: cwd `…/localproj` → `/workspace/proj`, home `/Users/sid` → the
fake home. Exit 0, transcript landed at `<fakehome>/.claude/projects/-workspace-proj/sid-e2e.jsonl`, and both passes
applied — the line's `cwd` became `/workspace/proj` and the embedded `~/.claude/CLAUDE.md` reference re-pointed at the
fake home. The only untested link in the chain is the ssh hop itself, which is teammate B's contract.

## 5. Residual risks (why `--session` stays best-effort)

1. **Untested axis: differing `$HOME`.** All experiments kept the same machine/home. On a remote box the home path
   differs (`/Users/sid` → `/root`), which is why `import_session_bundle` rewrites home as a second pass. Since
   experiment 3 proved stale absolute paths do not block resume, worst case is cosmetic.
2. **Version skew.** The remote installs the current claude; a transcript written by a much older/newer CLI may hit
   schema drift. The bundle records the local `claude --version` in `meta.json` for diagnosis.
3. **Nothing here is a documented contract** — it is reverse-engineered from 2.1.220 and can change without notice.
   Hence: every failure path in `agents/claude_state.py` warns and returns `None`, and the launch continues regardless.

   Because those failure paths are all soft, `--session` ships **enabled by default** (`ClaudeConfig.session = True`)
   rather than as an opt-in — the verdict above says relocation works, and when it does not the user lands on the
   fallback chain instead of a failed launch. `ops/launch.py` resolves that chain at runtime:
   session → handoff (if enabled) → plain `claude`, warning at each downgrade. `--handoff` is now the explicit
   *opt-out*, for when a summary is preferable to a long transcript.
