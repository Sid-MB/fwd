"""Claude Code state transfer — config, credentials, transcripts, handoff.

Design intent (owned by the Claude-state teammate, Phase 2)
-----------------------------------------------------------
Claude Code keys its per-project data by an *encoded absolute path*: ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``.
Because the remote working directory differs from the local one, moving a session is fundamentally a path-rewriting
problem, and it takes two passes: the project cwd, then the home directory (paths inside the transcript reference
both). Getting the encoding byte-exact against real ``~/.claude/projects`` naming is what the unit tests pin down.

Three escalating levels of transfer, matching the plan's flags:

- ``--handoff`` (default, always works): ask the local ``claude -p`` to write a HANDOFF.md summary. No state moves, so
  nothing can be corrupted, and the remote session starts by reading the file.
- ``--session`` (best-effort): move the actual transcript so ``claude --resume`` continues the conversation. Gated
  because claude >= 2.1.9 tightened foreign-session validation (#18645); handoff remains the documented default.
- ``--user-config`` / ``--creds`` (opt-in, security-sensitive): copy dotfiles, and lift an OAuth token from the macOS
  Keychain onto remote disk. ``CONFIG_EXCLUDE`` is a **hard denylist**, not a heuristic: ``settings.local.json`` and
  ``.credentials.json`` must never travel as part of a config bundle, only through the explicit ``--creds`` path which
  writes chmod 600 and warns the user.

S1 spike findings that shaped this module (full writeup: ``dev-docs/session-transfer-notes.md``, claude 2.1.220)
-----------------------------------------------------------------------------------------------------------
1. The encoding is ``re.sub(r"[^A-Za-z0-9]", "-", abspath)`` — verified against 34 real project directories with zero
   mismatches. ``/``, ``.``, ``_``, spaces and ``-`` all collapse to ``-``; the transform is lossy and has no inverse,
   which is fine because fwd only ever encodes.
2. Resume resolution is a pure filesystem lookup of ``projects/<encode(cwd)>/<id>.jsonl``. There is **no index file,
   no database**, so planting a transcript needs no registry update. Resuming a session id from a cwd whose encoded
   directory lacks the file fails with ``No conversation found with session ID``.
3. Claude does **not** validate the ``cwd`` recorded inside the transcript — a raw, un-rewritten copy resumed fine.
   The rewrite therefore buys content fidelity (quoted paths point at the remote tree), not acceptance. That is why
   every rewrite failure here is a warning, never a hard error.
4. ``claude -p --resume`` with no id refuses to run, so there is no headless session picker: fwd must always resolve
   a concrete session id locally (latest transcript by mtime).
5. Todo state lives in ``~/.claude/tasks/<session-id>/`` on 2.1.x and in ``~/.claude/todos/<session-id>*`` on older
   builds. The bundle carries both so it survives either layout.

Every entry point in this module is **best-effort**: a failed transcript import or config upload must degrade to a
warning and let the launch proceed, because the session itself is the thing the user actually asked for.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from fwd import ui
from fwd.sshexec import SSHEndpoint

# Never included in a --user-config bundle, regardless of flags. Machine-local settings and secrets only.
CONFIG_EXCLUDE: frozenset[str] = frozenset(
    {
        "settings.local.json",
        ".credentials.json",
        "history.jsonl",
        "statsig",
        "todos",
        "tasks",
        "projects",
        "session-env",
        "shell-snapshots",
    }
)

# Glob patterns matched against every path component of a bundle entry. Catches keys a user dropped inside skills/.
CONFIG_EXCLUDE_PATTERNS: tuple[str, ...] = ("*.pem", "*.key", ".env*", "*.p12", "id_rsa*", "id_ed25519*")

# Default allowlist for --user-config. Small on purpose: everything else is either machine-local or secret.
DEFAULT_CONFIG_INCLUDE: tuple[str, ...] = ("CLAUDE.md", "skills", "agents", "commands", "settings.json")

# Where todo/task state lives, newest layout first (see spike finding 5).
TODO_DIRS: tuple[str, ...] = ("tasks", "todos")

# Plan files written by plan mode live in ``~/.claude/plans/<slug>.md``, i.e. in the *home* directory rather than the
# synced project tree, so a plain rsync leaves them behind. The plan body is embedded in the transcript's
# ``ExitPlanMode`` tool call, so a resumed session still knows the plan — but any instruction to re-read the file by
# path breaks on the remote. :func:`referenced_plans` therefore carries exactly the plan files the transcript names.
PLANS_DIR_NAME = "plans"

# Bounds on the plan sweep. Plans are prose Markdown (a few KB each); anything far outside that is not a plan file and
# does not belong in a session bundle. Both caps are silent-drop guards, not correctness requirements.
MAX_PLAN_FILES = 20
MAX_PLAN_BYTES = 2 * 1024 * 1024

HANDOFF_PROMPT = (
    "Write a concise HANDOFF.md in the current directory summarising this working session so another engineer (or "
    "another Claude instance on a different machine) can pick it up cold. Use these sections: '## Current task state', "
    "'## Decisions made', '## Next steps', '## Files in flight'. Be specific about file paths and what is half-done. "
    "Write the file with your file-writing tool; do not print the contents back to me."
)

HANDOFF_TEMPLATE = f"""# HANDOFF

_Generated by `{ui.command()}` as a fallback: the local `claude -p` call failed or timed out, so this is a blank template._

## Current task state

TODO: describe what is being worked on and how far it got.

## Decisions made

TODO: list the decisions already taken so they are not relitigated.

## Next steps

TODO: the immediate next actions, in order.

## Files in flight

TODO: files that are partially edited or otherwise not in a finished state.
"""

# Seconds to let the local `claude -p` think before falling back to the template. Handoff generation is a real
# agentic run over the repo, so this is generous; the fallback keeps the launch unblocked either way.
HANDOFF_TIMEOUT = 120.0


def _claude_home() -> Path:
    """Return the Claude config root, honouring the ``FWD_CLAUDE_HOME`` override.

    The override exists so tests can build a synthetic ``~/.claude`` in ``tmp_path`` without monkeypatching ``HOME``
    (which would also redirect unrelated machinery like the ssh ControlMaster dir).
    """
    override = os.environ.get("FWD_CLAUDE_HOME")
    return Path(override) if override else Path.home() / ".claude"


def _projects_dir() -> Path:
    """Return ``<claude home>/projects``, the parent of every encoded per-project transcript directory."""
    return _claude_home() / "projects"


def _claude_version() -> str:
    """Return the local ``claude --version`` string, or ``"unknown"`` if the CLI is absent or misbehaving.

    Recorded in the bundle's ``meta.json`` purely for diagnosis: transcript schema drift across CLI versions is the
    main un-mitigated risk of ``--session``, and knowing which version wrote a transcript is how you triage it.
    """
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def encode_project_path(path: str | Path) -> str:
    """Encode an absolute path the way Claude Code names directories under ``~/.claude/projects``.

    Must match the real implementation byte-for-byte or the remote session silently starts empty; this is the single
    highest-risk function in the Claude layer and is covered by tests against real directory names.

    The rule, established empirically in the S1 spike: every character outside ``[A-Za-z0-9]`` becomes ``-``. Trailing
    slashes are stripped first so ``/a/b`` and ``/a/b/`` encode identically (they are the same directory, but a naive
    substitution would append a stray ``-``).
    """
    text = str(path)
    if len(text) > 1:
        text = text.rstrip("/")
    return re.sub(r"[^A-Za-z0-9]", "-", text)


def rewrite_jsonl(src: str | Path, dst: str | Path, replacements: dict[str, str]) -> int:
    """Copy a transcript JSONL, applying literal path replacements line by line.

    Line-oriented streaming (not a whole-file load) keeps memory flat on multi-hundred-MB transcripts and means one
    malformed line cannot invalidate the rest of the file.

    Replacement is deliberately plain string substitution on the raw line, not a JSON round-trip: the transcript
    schema is undocumented and changes between CLI releases, so parsing and re-serialising risks dropping fields or
    reordering keys, whereas substituting an absolute path inside an already-escaped JSON string is safe (POSIX paths
    contain no characters that JSON escapes).

    Args:
        replacements: Ordered mapping of old → new strings; apply the project cwd before the home directory so the
            longer, more specific prefix wins. Python dicts preserve insertion order, so the caller controls
            precedence simply by ordering the mapping — cwd first, home second.

    Returns:
        Number of lines written.
    """
    src_path, dst_path = Path(src), Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with src_path.open("r", encoding="utf-8", errors="replace") as fh, dst_path.open("w", encoding="utf-8") as out:
        for line in fh:
            for old, new in replacements.items():
                if old:
                    line = line.replace(old, new)
            out.write(line)
            written += 1
    return written


def _transcripts_for(local_cwd: str | Path) -> list[Path]:
    """Return the project's transcript files, most recently modified first."""
    project_dir = _projects_dir() / encode_project_path(Path(local_cwd).resolve())
    if not project_dir.is_dir():
        return []
    return sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _todo_paths(session_id: str) -> list[Path]:
    """Return todo/task state paths belonging to a session across both known layouts (``tasks/`` and ``todos/``)."""
    found: list[Path] = []
    for name in TODO_DIRS:
        root = _claude_home() / name
        if not root.is_dir():
            continue
        direct = root / session_id
        if direct.exists():
            found.append(direct)
        found.extend(sorted(p for p in root.glob(f"{session_id}*") if p != direct))
    return found


def _plan_reference_pattern() -> re.Pattern[str]:
    """Build the regex that spots plan-file paths inside a transcript line.

    Three spellings reach a transcript and all three must match: the absolute local path (what a tool result or an
    assistant message quotes), the tilde form a user typed, and the ``$HOME`` form a shell command used. The captured
    group is the path *relative to* ``plans/`` so nested layouts survive; the capture class excludes quotes, spaces
    and backslashes so a match stops at the end of the path rather than running into the surrounding JSON.
    """
    roots = "|".join(re.escape(root) for root in (str(_claude_home()), "~/.claude", "$HOME/.claude", "${HOME}/.claude"))
    return re.compile(rf"(?:{roots})/{PLANS_DIR_NAME}/([^\"'\s\\`,;:)\]]+\.md)")


def referenced_plans(transcript: str | Path) -> list[Path]:
    """Return the plan files a transcript mentions by path, in first-mention order.

    Scanning is line-by-line over the raw JSONL for the same reason :func:`rewrite_jsonl` is: the schema is
    undocumented, so pattern-matching the text is more durable than walking fields that get renamed between releases.
    It also means a plan named in a tool result, a user message or an assistant reply is found equally well.

    Every candidate is resolved and re-checked against the plans directory before it is accepted, so a crafted
    ``../../.credentials.json`` reference inside a transcript cannot pull a file out of the plans tree. Missing files
    are skipped silently — transcripts routinely name plans that were since deleted or that came from another machine.
    """
    plans_dir = _claude_home() / PLANS_DIR_NAME
    if not plans_dir.is_dir():
        return []
    root = plans_dir.resolve()
    pattern = _plan_reference_pattern()

    found: list[Path] = []
    seen: set[Path] = set()
    try:
        with Path(transcript).open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for relative in pattern.findall(line):
                    candidate = (plans_dir / relative).resolve()
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if not candidate.is_relative_to(root) or not candidate.is_file():
                        continue
                    if candidate.stat().st_size > MAX_PLAN_BYTES:
                        ui.warn(f"Skipping plan {candidate.name}: larger than {MAX_PLAN_BYTES // 1024} KiB.")
                        continue
                    found.append(candidate)
                    if len(found) >= MAX_PLAN_FILES:
                        ui.warn(f"Only the first {MAX_PLAN_FILES} referenced plan files are transferred.")
                        return found
    except OSError:
        return found
    return found


def export_session_bundle(local_cwd: str | Path, dest: str | Path, *, session_id: str | None = None) -> Path | None:
    """Collect the local transcript(s) for a project into a transferable bundle.

    Layout inside the tar.gz (flat and self-describing so the remote side needs no knowledge of local paths):

    - ``meta.json`` — local cwd, local home, session id, claude version
    - ``transcript/<session-id>.jsonl`` — the conversation
    - ``transcript/<session-id>/…`` — sidecars (``subagents/``, ``memory/``) when present
    - ``todos/<name>`` — matching ``tasks/``/``todos/`` state
    - ``plans/<name>.md`` — plan files the transcript references by path (see :func:`referenced_plans`)

    Args:
        session_id: Specific session to export; defaults to the most recently modified transcript for ``local_cwd``.
            Spike finding 4: ``claude -p --resume`` has no picker, so an id must always be resolved here, locally.

    Returns:
        Path to the bundle, or ``None`` if the project has no transcripts.
    """
    cwd = Path(local_cwd).resolve()
    transcripts = _transcripts_for(cwd)
    if not transcripts:
        ui.warn(f"No Claude transcript found for {cwd} — nothing to transfer (use --handoff instead).")
        return None

    if session_id:
        match = next((p for p in transcripts if p.stem == session_id), None)
        if match is None:
            ui.warn(f"Session {session_id} not found under {cwd} — nothing to transfer.")
            return None
        chosen = match
    else:
        chosen = transcripts[0]
    sid = chosen.stem

    dest_path = Path(dest)
    if dest_path.is_dir() or dest_path.suffix == "":
        dest_path.mkdir(parents=True, exist_ok=True)
        dest_path = dest_path / f"fwd-session-{sid}.tar.gz"
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "session_id": sid,
        "local_cwd": str(cwd),
        "local_home": str(Path.home()),
        "claude_version": _claude_version(),
        "encoded_local": encode_project_path(cwd),
    }

    with tempfile.TemporaryDirectory() as tmp:
        meta_path = Path(tmp) / "meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        with tarfile.open(dest_path, "w:gz") as tar:
            tar.add(meta_path, arcname="meta.json")
            tar.add(chosen, arcname=f"transcript/{chosen.name}")
            sidecar = chosen.with_suffix("")
            if sidecar.is_dir():
                tar.add(sidecar, arcname=f"transcript/{sidecar.name}")
            for todo in _todo_paths(sid):
                tar.add(todo, arcname=f"todos/{todo.name}")
            plans_root = (_claude_home() / PLANS_DIR_NAME).resolve()
            plans = referenced_plans(chosen)
            for plan in plans:
                tar.add(plan, arcname=f"{PLANS_DIR_NAME}/{plan.relative_to(plans_root)}")
    if plans:
        ui.info(f"Carrying {len(plans)} plan file(s) referenced by the session.")
    return dest_path


def _upload_file(endpoint: SSHEndpoint, local: Path, remote: str) -> None:
    """Stream one local file to a remote path over the existing ssh connection.

    ``cat local | ssh host 'cat > remote'`` rather than ``scp``: it rides the ControlMaster socket (no second auth),
    works on transports where scp/sftp is unavailable (RunPod's proxy host), and needs no remote binary beyond
    ``cat``. Bundles are single files, so there is nothing scp would buy us.
    """
    argv = endpoint.ssh_argv() + [f"mkdir -p {shlex.quote(str(Path(remote).parent))} && cat > {shlex.quote(remote)}"]
    with local.open("rb") as fh:
        proc = subprocess.run(argv, stdin=fh, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"upload of {local.name} failed: {proc.stderr.decode(errors='replace').strip()}")


def import_session_bundle(
    endpoint: SSHEndpoint,
    bundle: str | Path,
    remote_cwd: str,
    remote_home: str,
) -> str | None:
    """Upload a bundle and install it into the remote ``~/.claude/projects/<re-encoded>/``.

    Sequence, all remote-side work done by one shell script so the whole install is a single round trip:
    upload the tar.gz → extract into a temp dir → rewrite paths (cwd pass, then home pass — done with ``python3`` on
    the remote because the substitution must be byte-identical to :func:`rewrite_jsonl`) → move into the re-encoded
    project directory → restore todo state and any referenced plan files → verify the transcript landed.

    Plans are restored into ``<remote home>/.claude/plans/`` because that is exactly where the home-directory rewrite
    pass repoints the transcript's plan references, so a resumed session that re-reads its plan by path finds it.

    Verification is a file-existence check, not a ``claude --resume`` probe: per the S1 spike, resume resolution is a
    pure path lookup, and burning a remote model call to confirm what ``test -f`` already proves would be wasteful.

    Returns:
        The session id to pass to ``claude --resume``, or ``None`` if the import could not be validated. Never
        raises — a failed session transfer must degrade to a plain ``claude`` launch, not abort it.
    """
    bundle_path = Path(bundle)
    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            meta_member = tar.extractfile("meta.json")
            if meta_member is None:
                raise RuntimeError("bundle is missing meta.json")
            meta = json.loads(meta_member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, KeyError, ValueError) as exc:
        ui.warn(f"Session bundle unreadable ({exc}); continuing without --resume.")
        return None

    session_id = meta.get("session_id")
    local_cwd = meta.get("local_cwd", "")
    local_home = meta.get("local_home", str(Path.home()))
    if not session_id:
        ui.warn("Session bundle has no session id; continuing without --resume.")
        return None

    remote_cwd = remote_cwd.rstrip("/") or remote_cwd
    remote_home = remote_home.rstrip("/") or remote_home
    encoded = encode_project_path(remote_cwd)
    staging = f"/tmp/fwd-session-{uuid.uuid4().hex[:8]}"
    remote_bundle = f"{staging}/bundle.tar.gz"

    script = _IMPORT_SCRIPT.format(
        staging=shlex.quote(staging),
        bundle=shlex.quote(remote_bundle),
        home=shlex.quote(remote_home),
        encoded=shlex.quote(encoded),
        session=shlex.quote(session_id),
        local_cwd=shlex.quote(local_cwd),
        remote_cwd=shlex.quote(remote_cwd),
        local_home=shlex.quote(local_home),
        remote_home=shlex.quote(remote_home),
    )

    try:
        _upload_file(endpoint, bundle_path, remote_bundle)
        endpoint.run(script, check=True, timeout=180)
    except Exception as exc:  # noqa: BLE001 - best-effort by contract; any failure degrades to no-resume
        ui.warn(f"Could not install the session on the remote ({exc}); continuing without --resume.")
        return None
    return session_id


# Remote-side installer. Kept as a module constant so the shell is readable and reviewable in one place.
# The rewrite runs in python3 (present on every image bootstrap.sh produces) and mirrors rewrite_jsonl exactly:
# stream lines, apply cwd replacement then home replacement, in that order.
_IMPORT_SCRIPT = """
set -eu
mkdir -p {staging}/x
tar -xzf {bundle} -C {staging}/x
DEST={home}/.claude/projects/{encoded}
mkdir -p "$DEST"
python3 - {staging}/x/transcript/{session}.jsonl "$DEST/{session}.jsonl" {local_cwd} {remote_cwd} {local_home} {remote_home} <<'PYEOF'
import sys
src, dst, lcwd, rcwd, lhome, rhome = sys.argv[1:7]
pairs = [(lcwd, rcwd), (lhome, rhome)]
with open(src, encoding="utf-8", errors="replace") as fh, open(dst, "w", encoding="utf-8") as out:
    for line in fh:
        for old, new in pairs:
            if old and old != new:
                line = line.replace(old, new)
        out.write(line)
PYEOF
if [ -d {staging}/x/transcript/{session} ]; then cp -R {staging}/x/transcript/{session} "$DEST/"; fi
if [ -d {staging}/x/todos ]; then mkdir -p {home}/.claude/tasks && cp -R {staging}/x/todos/. {home}/.claude/tasks/; fi
if [ -d {staging}/x/plans ]; then mkdir -p {home}/.claude/plans && cp -R {staging}/x/plans/. {home}/.claude/plans/; fi
test -s "$DEST/{session}.jsonl"
rm -rf {staging}
"""


def make_handoff(local_cwd: str | Path) -> Path:
    """Generate ``HANDOFF.md`` for the project by invoking the local ``claude -p``.

    This is the one transfer path that must never leave the user empty-handed, so a CLI failure or a timeout is not
    an error: it writes a TODO-marked template instead and warns. A blank template the user can fill in beats an
    aborted launch, and beats a silent absence the remote session would not notice.

    Returns:
        Path to the written file. This is the fallback that always works, so failures here should be loud.
    """
    cwd = Path(local_cwd).resolve()
    target = cwd / "HANDOFF.md"
    before = target.stat().st_mtime if target.exists() else None

    try:
        proc = subprocess.run(
            ["claude", "-p", HANDOFF_PROMPT],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=HANDOFF_TIMEOUT,
        )
        failed = proc.returncode != 0
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
    except subprocess.TimeoutExpired:
        failed, detail = True, [f"timed out after {HANDOFF_TIMEOUT:.0f}s"]
    except OSError as exc:
        failed, detail = True, [str(exc)]

    # Claude may exit 0 without actually writing the file (it sometimes answers in prose), so check the artifact too.
    unwritten = not target.exists() or (before is not None and target.stat().st_mtime == before)
    if failed or unwritten:
        reason = detail[0] if failed else "claude did not write the file"
        ui.warn(f"HANDOFF.md generation fell back to a template ({reason}). Fill in the TODOs before launching.")
        target.write_text(HANDOFF_TEMPLATE, encoding="utf-8")
    return target


def _excluded(relative: Path) -> bool:
    """Return whether a path under ``~/.claude`` is denied by :data:`CONFIG_EXCLUDE` or the pattern denylist.

    Checks *every* component, not just the leaf, so an excluded directory takes its whole subtree with it and a
    stray ``skills/foo/.env`` is dropped even though its parents are allowed.
    """
    for part in relative.parts:
        if part in CONFIG_EXCLUDE:
            return True
        if any(Path(part).match(pattern) for pattern in CONFIG_EXCLUDE_PATTERNS):
            return True
    return False


def build_config_bundle(dest: Path, *, include: Sequence[str] | None = None) -> tuple[Path, int]:
    """Build the ``--user-config`` tar.gz locally and return ``(path, entry count)``.

    Split out from :func:`upload_user_config` so the filtering rules — the security-critical part — are unit-testable
    without any ssh involvement.
    """
    names = list(include) if include is not None else list(DEFAULT_CONFIG_INCLUDE)
    home = _claude_home()
    added = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for name in names:
            if name in CONFIG_EXCLUDE:
                ui.warn(f"Refusing to upload ~/.claude/{name}: it is on the hard exclusion list.")
                continue
            source = home / name
            if not source.exists():
                continue
            if source.is_file():
                if not _excluded(Path(name)):
                    tar.add(source, arcname=name)
                    added += 1
                continue
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(home)
                if _excluded(relative):
                    continue
                tar.add(path, arcname=str(relative))
                added += 1
    return dest, added


def upload_user_config(endpoint: SSHEndpoint, *, include: Sequence[str] | None = None) -> None:
    """Upload the user's Claude config bundle (``CLAUDE.md``, ``skills/``, ``agents/``, ``commands/``, ``settings.json``).

    Extraction *merges* into the remote ``~/.claude`` — it never deletes. The remote box may be a long-lived dev
    machine with its own config, and silently clobbering it would be a nasty surprise; tar's default overwrite-per-file
    behaviour is the right amount of destructive.

    Args:
        include: Explicit allowlist of names under ``~/.claude``; defaults to the standard set. Entries in
            :data:`CONFIG_EXCLUDE` are dropped even if explicitly requested.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bundle, count = build_config_bundle(Path(tmp) / "claude-config.tar.gz", include=include)
        if count == 0:
            ui.warn("No Claude user config found to upload.")
            return
        remote_bundle = f"/tmp/fwd-config-{uuid.uuid4().hex[:8]}.tar.gz"
        try:
            _upload_file(endpoint, bundle, remote_bundle)
            endpoint.run(
                f"mkdir -p ~/.claude && tar -xzf {shlex.quote(remote_bundle)} -C ~/.claude "
                f"&& rm -f {shlex.quote(remote_bundle)}",
                check=True,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            ui.warn(f"Claude user-config upload failed ({exc}); the remote will use its own config.")
            return
    ui.ok(f"Uploaded {count} Claude config file(s).")


def read_keychain_creds() -> str | None:
    """Read the Claude Code OAuth credentials from the macOS Keychain.

    Runs ``security find-generic-password -s "Claude Code-credentials" -w``. On Linux (and on macOS installs that keep
    credentials on disk instead) it falls back to reading ``~/.claude/.credentials.json``.

    The value returned is a live OAuth token; callers must not log it, and it is never written to fwd's own state.

    Returns:
        The raw JSON string, or ``None`` on a non-macOS host or when no entry exists.
    """
    if platform.system() == "Darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    fallback = _claude_home() / ".credentials.json"
    if fallback.is_file():
        try:
            return fallback.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def upload_creds(endpoint: SSHEndpoint, creds_json: str) -> None:
    """Write credentials to remote ``~/.claude/.credentials.json`` with mode 600.

    Callers must have warned the user first: this places a live token on a machine fwd does not control.

    The token is piped over stdin rather than interpolated into the command line — argv is visible in the remote
    ``ps`` table and in shell history, stdin is not. ``umask 077`` before the write closes the window where the file
    exists world-readable, and the explicit ``chmod 600`` covers pre-existing files.
    """
    ui.warn(
        "Copying live Claude credentials to the remote machine. Anyone with root there can read your token — "
        "revoke with `claude logout` on the remote when you are done."
    )
    script = "umask 077 && mkdir -p ~/.claude && cat > ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json"
    argv = endpoint.ssh_argv() + [script]
    proc = subprocess.run(argv, input=creds_json.encode(), capture_output=True)
    if proc.returncode != 0:
        ui.warn(f"Credential upload failed: {proc.stderr.decode(errors='replace').strip()}")
        return
    ui.ok("Credentials installed at remote ~/.claude/.credentials.json (mode 600).")
