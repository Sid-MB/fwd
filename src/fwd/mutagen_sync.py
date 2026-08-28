"""Continuous file synchronization built on Mutagen — opt-in steady-state mirroring for a running session.

Design intent
-------------
``fwd push`` / ``fwd pull`` are deliberate, bounded, one-shot transfers. Continuous mode adds the other half: while a
session runs, a Mutagen session keeps the local project and ``remote_dir`` converged in both directions without anyone
typing a command. It is off by default because it is a long-lived background daemon holding an SSH connection, which is
not something a file-transfer tool should start without being asked.

Four decisions shape this module.

**Two-way-safe, never destructive.** The sync mode is ``two-way-safe``: Mutagen propagates changes in both directions
but refuses to resolve a genuine conflict by picking a winner. Conflicts surface through ``fwd sync status`` and are
resolved by the user editing one side. A "resolved" mode would silently discard work.

**``.git`` is never continuously synced.** The one-shot push intentionally carries ``.git`` so remote agents have
history. Continuous two-way sync of a repository database is a different and much worse proposition: both sides run Git
concurrently, and a half-propagated index, packfile, or ref update corrupts the repository. Mutagen is therefore
launched with ``--ignore-vcs`` *and* an explicit ``.git`` pattern, and Git state keeps moving through ``fwd push`` /
``fwd pull`` only.

**Initial bulk transfer stays on rsync.** Launch already performs a size-bounded rsync push. Mutagen is started after
that and only ever has deltas to move, so the upload circuit breaker (``sync.max_size_gb``) still governs the one
transfer that could be enormous.

**SSH fidelity through a shim, not through the URL.** Mutagen invokes ``ssh``/``scp`` from ``MUTAGEN_SSH_PATH`` and a
Mutagen URL carries only ``user@host:port`` — it cannot express fwd's ``key_path``, ``proxy_jump``, or ``extra_opts``.
fwd therefore points ``MUTAGEN_SSH_PATH`` at :data:`SHIM_DIR`, which holds generated ``ssh`` and ``scp`` wrappers that
look the destination up in :data:`ENDPOINTS_PATH` and exec the real binary with that endpoint's options injected. The
wrappers dispatch *per host* rather than baking one endpoint's options in, because the Mutagen daemon captures its
environment at start and a single fwd-owned daemon serves every session; per-session shim directories would require a
daemon restart (and a different ``MUTAGEN_SSH_PATH``) for each endpoint.

The daemon is isolated from any daemon the user runs themselves by ``MUTAGEN_DATA_DIRECTORY=~/.fwd/mutagen``. That
path is also kept short on purpose: the daemon's control socket lives beneath it and a Unix socket path over ~104
bytes fails to bind, which is exactly how a temp-directory data root fails.

Nothing here is imported unless continuous sync is actually enabled, and the ``mutagen`` binary is never required
otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from fwd import ui
from fwd.config import ALWAYS_SYNC_EXCLUDES, SyncConfig
from fwd.sshexec import SSHEndpoint

# fwd-owned Mutagen state. Deliberately short: the daemon's control socket lives under this directory and Unix domain
# socket paths are capped near 104 bytes, so a long root makes every daemon start time out with an opaque error.
DATA_DIR = Path.home() / ".fwd" / "mutagen"
SHIM_DIR = DATA_DIR / "shims"
ENDPOINTS_PATH = DATA_DIR / "endpoints.json"

MUTAGEN_BINARY = "mutagen"
BREW_FORMULA = "mutagen-io/mutagen/mutagen"
INSTALL_URL = "https://mutagen.io/documentation/introduction/installation"

# Mutagen session names accept lower-case alphanumerics and dashes and must begin with a letter or digit.
_SESSION_NAME_ALLOWED = re.compile(r"[^a-z0-9-]+")
_SESSION_LABEL = "fwd-session"

# Statuses Mutagen reports for a session that is connected and doing its job. Anything else is worth showing the user.
HEALTHY_STATUSES = frozenset({"watching", "scanning", "reconciling", "staging-alpha", "staging-beta", "transitioning", "saving"})


class MutagenError(RuntimeError):
    """Raised when a Mutagen invocation fails in a way the caller should surface to the user."""


@dataclass(frozen=True, slots=True)
class BetaEndpoint:
    """The remote side of a Mutagen session, as Mutagen reports it.

    Kept structured (rather than only as the ``host:path`` label ``fwd sync status`` prints) so fwd can answer one
    question a display string cannot: is this existing session still pointed at the machine the caller is about to use?
    """

    user: str
    host: str
    port: int
    path: str

    def matches(self, endpoint: SSHEndpoint, remote_dir: str) -> bool:
        """Return whether this session's remote side is the same place as ``endpoint``'s ``remote_dir``.

        Deliberately lenient about fields Mutagen may not report: an absent user or port compares equal rather than
        forcing a needless re-create, while a *reported* mismatch is decisive. The point is to catch a target that
        moved — a re-provisioned pod on a new address or port — not to demand a byte-identical URL.
        """
        if self.host and self.host != endpoint.host:
            return False
        if self.user and endpoint.user and self.user != endpoint.user:
            return False
        if self.port and self.port != endpoint.port:
            return False
        return not self.path or self.path.rstrip("/") == remote_dir.rstrip("/")


@dataclass(frozen=True, slots=True)
class SyncSessionStatus:
    """One Mutagen session reduced to the fields ``fwd sync status`` renders.

    Kept as a dataclass rather than passing Mutagen's raw JSON around so the rendering layer and the tests share one
    shape, and so a future Mutagen output change is absorbed in :func:`_status_from_payload` alone.
    """

    name: str
    identifier: str
    status: str
    paused: bool
    alpha: str
    beta: str
    conflicts: int
    problems: tuple[str, ...]
    beta_endpoint: BetaEndpoint | None = None

    @property
    def healthy(self) -> bool:
        """Return whether the session is running normally with nothing awaiting human resolution."""
        return not self.paused and not self.conflicts and not self.problems and self.status in HEALTHY_STATUSES


# --------------------------------------------------------------------------------------------- local binary handling


@lru_cache(maxsize=1)
def binary_path() -> str | None:
    """Return the local ``mutagen`` executable, or ``None`` when continuous sync cannot run on this machine.

    Memoized because every ``mutagen`` invocation and every status probe asks for it, and a PATH lookup stats each
    directory on ``PATH``. The one event that can change the answer within a process is fwd installing Mutagen itself,
    which clears the cache explicitly in :func:`ensure_installed`.
    """
    return shutil.which(MUTAGEN_BINARY)


def install_instructions() -> str:
    """Return the platform-appropriate manual installation guidance shown when fwd cannot install Mutagen itself."""
    if shutil.which("brew"):
        return f"install it with {ui.code(f'brew install {BREW_FORMULA}')}"
    return f"install Mutagen from {INSTALL_URL}, then re-run this command"


def ensure_installed() -> str:
    """Return the path to ``mutagen``, offering to install it when it is missing.

    Continuous sync is the only feature that needs Mutagen, so the dependency is resolved lazily and interactively at
    the moment it is first required rather than being a hard install-time prerequisite of fwd. A non-interactive caller
    is never left hanging on a prompt it cannot answer: it exits with the exact command to run.

    Raises:
        typer.Exit: Through :func:`fwd.ui.die` when Mutagen is absent and could not be installed.
    """
    found = binary_path()
    if found:
        return found
    brew = shutil.which("brew")
    ui.warn("continuous sync needs Mutagen (https://mutagen.io), which is not installed")
    # ``ui.confirm`` answers with its *default* when nothing can be prompted, so the interactive check has to happen
    # first: without it a scripted or agent-driven run would install a system package that nobody agreed to.
    if brew and ui.interactive_terminal() and ui.confirm(f"install it now with 'brew install {BREW_FORMULA}'?", default=True):
        with ui.step("Installing Mutagen with Homebrew"):
            completed = subprocess.run([brew, "install", BREW_FORMULA], check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            ui.die(f"brew could not install Mutagen: {detail[-1] if detail else 'unknown error'}. Install it manually from {INSTALL_URL}")
        binary_path.cache_clear()
        found = binary_path()
        if found:
            ui.ok(f"installed Mutagen at {found}")
            return found
    ui.die(f"Mutagen is required for continuous sync but is not installed; {install_instructions()}")


def supports_continuous(endpoint: SSHEndpoint) -> bool:
    """Return whether a transport can carry a Mutagen session.

    Mutagen installs its remote agent by copying a binary with ``scp`` and then executing it. The RunPod SSH proxy
    exposes neither, which is the same limitation that already sets ``supports_rsync`` to ``False``, so that one flag
    answers both questions rather than fwd inventing a second capability bit that would always agree with it.
    """
    return endpoint.supports_rsync


# ----------------------------------------------------------------------------------------------------- shim handling


def _shim_source(tool: str) -> str:
    """Return the body of one generated ``ssh``/``scp`` wrapper.

    Written in Python and launched with fwd's own interpreter so the wrapper can parse argv properly instead of
    guessing in shell, and so it does not depend on whatever ``python3`` happens to be on the daemon's PATH.

    The wrapper matches the destination against the endpoints fwd itself recorded, which makes host detection exact
    without reimplementing OpenSSH's option grammar: any argv token that equals a known ``[user@]host`` — or whose
    ``host:path`` prefix does, as scp spells it — selects that endpoint's options. An unknown destination falls through
    to the real binary unmodified, so a Mutagen session fwd does not own is never altered.

    The destination alone is not a unique key. Two RunPod pods behind one public IP differ only by port, so the port
    Mutagen passes (``-p`` for ssh, ``-P`` for scp, derived from the port in the beta URL) is combined with the
    destination to select the entry — otherwise one pod's session would silently be reconnected through the other
    pod's port and key. A destination seen without a port flag falls back to the bare ``user@host`` entry, which is
    what a session created by an older fwd (whose URL carried no port) still needs.
    """
    real = shutil.which(tool) or f"/usr/bin/{tool}"
    port_flag = "-P" if tool == "scp" else "-p"
    return f'''#!{sys.executable}
"""Generated by fwd. Injects an fwd endpoint's SSH options into Mutagen's {tool} invocations. Do not edit."""

import json
import os
import sys

REAL = {real!r}
TOOL = {tool!r}
PORT_FLAG = {port_flag!r}
ENDPOINTS = {str(ENDPOINTS_PATH)!r}

try:
    with open(ENDPOINTS, encoding="utf-8") as handle:
        endpoints = json.load(handle)
except Exception:
    endpoints = {{}}

arguments = sys.argv[1:]
port = None
destinations = []
index = 0
while index < len(arguments):
    token = arguments[index]
    if token == PORT_FLAG and index + 1 < len(arguments):
        port = arguments[index + 1]
        index += 2
        continue
    if token.startswith(PORT_FLAG) and token[2:].isdigit():
        port = token[2:]
    elif not token.startswith("-"):
        destinations.append(token)
    index += 1

options = []
for token in destinations:
    for host in (token, token.split(":", 1)[0]):
        for key in ([f"{{host}}:{{port}}"] if port else []) + [host]:
            entry = endpoints.get(key)
            if isinstance(entry, dict) and entry.get(TOOL):
                options = list(entry[TOOL])
                break
        if options:
            break
    if options:
        break

argv = [REAL, *options, *arguments]
os.execv(REAL, argv)
'''


def _scp_options(ssh_options: Sequence[str]) -> list[str]:
    """Translate ssh option argv into the equivalent scp argv.

    Only one flag genuinely differs: ssh selects a port with ``-p`` while scp uses ``-P`` (lower-case ``-p`` means
    "preserve times" to scp, so passing it through unchanged would both lose the port and corrupt the transfer).
    ``-i``, ``-J``, and ``-o`` are spelled identically by both tools.
    """
    translated: list[str] = []
    arguments = list(ssh_options)
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-p" and index + 1 < len(arguments):
            translated += ["-P", arguments[index + 1]]
            index += 2
            continue
        translated.append(argument)
        index += 1
    return translated


def endpoint_options(endpoint: SSHEndpoint) -> list[str]:
    """Return the SSH options Mutagen must use to reach one endpoint, minus the destination itself.

    Built from :meth:`fwd.sshexec.SSHEndpoint.ssh_argv` so key, port, jump host, and ``extra_opts`` cannot drift from
    what every other fwd command uses. ControlMaster options are deliberately excluded (``control=False``): Mutagen's
    connection is long-lived, while fwd's multiplex master expires on its own ``ControlPersist`` timer and is torn down
    outright by ``fwd stop``, so riding it would kill an otherwise healthy sync session.
    """
    argv = endpoint.ssh_argv(control=False)
    # argv[0] is "ssh" and argv[-1] is the destination; Mutagen supplies both itself.
    return argv[1:-1]


def endpoint_key(endpoint: SSHEndpoint) -> str:
    """Return the registry key one endpoint's SSH options are recorded under: ``user@host:port``.

    The port is part of the key, not decoration. Several RunPod direct-IP pods commonly share one public IP and differ
    only by port, so a ``user@host`` key would let a second pod overwrite the first pod's key and options — and the
    daemon would then reconnect the first session through the second pod, two-way syncing a project onto the wrong
    machine. :func:`remote_url` puts the same port in the beta URL so the shim can reproduce this key.
    """
    return f"{endpoint.ssh_target()}:{endpoint.port}"


def write_shims(endpoints: Sequence[SSHEndpoint] = ()) -> None:
    """Regenerate the shim wrappers and record each endpoint's options, creating the fwd Mutagen data directory.

    Idempotent and cheap, so every operation that could face a changed endpoint (launch, attach, ``fwd sync on``) calls
    it unconditionally rather than trying to detect staleness. Recorded endpoints accumulate by
    :func:`endpoint_key` because one daemon serves every session; an entry is simply overwritten when that host's
    options change. A bare ``user@host`` alias is recorded alongside it purely so a session created by an older fwd —
    whose beta URL carried no port, leaving the shim nothing to key on — keeps resolving to *an* endpoint's options.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for tool in ("ssh", "scp"):
        shim = SHIM_DIR / tool
        shim.write_text(_shim_source(tool), encoding="utf-8")
        shim.chmod(0o700)
    try:
        recorded = json.loads(ENDPOINTS_PATH.read_text(encoding="utf-8")) if ENDPOINTS_PATH.is_file() else {}
    except (OSError, ValueError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    for endpoint in endpoints:
        options = endpoint_options(endpoint)
        entry = {"ssh": options, "scp": _scp_options(options)}
        recorded[endpoint_key(endpoint)] = entry
        recorded[endpoint.ssh_target()] = entry
    _atomic_write(ENDPOINTS_PATH, json.dumps(recorded, indent=2, sort_keys=True) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Write a small fwd-owned JSON file atomically, matching the durability contract of the session state store."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def environment() -> dict[str, str]:
    """Return the process environment every ``mutagen`` invocation runs under.

    Both variables must be present on *every* call, not just the one that happens to start the daemon: the CLI locates
    its daemon through ``MUTAGEN_DATA_DIRECTORY``, and whichever invocation auto-starts the daemon is the one whose
    ``MUTAGEN_SSH_PATH`` the daemon inherits for the rest of its life.
    """
    return {**os.environ, "MUTAGEN_DATA_DIRECTORY": str(DATA_DIR), "MUTAGEN_SSH_PATH": str(SHIM_DIR)}


# ------------------------------------------------------------------------------------------------- session utilities


def session_name(fwd_session_name: str) -> str:
    """Return a Mutagen-legal session name for an fwd session name.

    fwd names are already ``[a-z0-9_-]`` slugs, so this is normally an identity mapping; it exists so a hand-written
    ``--name`` containing anything else degrades into a usable name instead of an opaque Mutagen validation error.
    """
    sanitized = _SESSION_NAME_ALLOWED.sub("-", fwd_session_name.strip().lower()).strip("-")
    if not sanitized:
        sanitized = "fwd-session"
    if not sanitized[0].isalnum():
        sanitized = f"fwd-{sanitized}"
    return sanitized


def remote_url(endpoint: SSHEndpoint, remote_dir: str) -> str:
    """Return the Mutagen beta URL for a session's remote directory.

    Mutagen's SSH URL grammar is ``[user@]host[:port]:path``, and the port is always spelled out: it is what makes the
    daemon pass ``-p``/``-P`` to the shim, which is the only way the shim can tell two endpoints sharing one
    ``user@host`` apart (see :func:`endpoint_key`). Every other connection detail — key, jump host, ``extra_opts`` —
    still reaches ssh through the shim, since no URL can express them.
    """
    return f"{endpoint.ssh_target()}:{endpoint.port}:{remote_dir}"


def _run(arguments: Sequence[str], *, check: bool = True, timeout: float | None = 120.0) -> subprocess.CompletedProcess[str]:
    """Run one ``mutagen`` subcommand under fwd's isolated daemon environment."""
    binary = binary_path()
    if binary is None:
        raise MutagenError(f"mutagen is not installed; {install_instructions()}")
    try:
        completed = subprocess.run([binary, *arguments], check=False, capture_output=True, text=True, timeout=timeout, env=environment())
    except subprocess.TimeoutExpired as exc:
        raise MutagenError(f"mutagen {' '.join(arguments)} timed out after {timeout}s") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise MutagenError(f"mutagen {' '.join(arguments)} failed: {detail[-1] if detail else f'exit {completed.returncode}'}")
    return completed


# ----------------------------------------------------------------------------------------------- ignore translation


def _clean_pattern(line: str) -> tuple[str, str] | None:
    """Split one gitignore line into its ``!`` negation prefix and pattern body, or ``None`` if it is not a rule.

    Blank lines and ``#`` comments are dropped; an escaped ``\\#`` is a literal ``#`` and is preserved, as Git defines.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("\\#"):
        stripped = stripped[1:]
    if stripped.startswith("!"):
        return "!", stripped[1:]
    return "", stripped


def _clean_patterns(lines: Iterable[str]) -> list[str]:
    """Apply gitignore line semantics to raw rule text, dropping everything that is not an actual pattern.

    Needed because ``--ignore`` takes patterns, not gitignore file lines: ``mutagen sync create --ignore '# comment'``
    is accepted and installs a literal pattern for a file named ``# comment``. The one-shot path renders the same text
    into a real gitignore file that Git parses, so this is where the two domains would otherwise diverge — and why the
    stripping lives here rather than in :func:`fwd.selection.custom_ignore_patterns`, whose output must stay raw.
    """
    cleaned: list[str] = []
    for line in lines:
        parsed = _clean_pattern(line)
        if parsed is None:
            continue
        negation, pattern = parsed
        cleaned.append(f"{negation}{pattern}")
    return cleaned


def _dedup_keeping_last(patterns: Iterable[str]) -> list[str]:
    """De-duplicate an ignore list while keeping each pattern's *last* position.

    Ignore lists are evaluated last-match-wins, so a repeated pattern's final occurrence is the one that decides the
    outcome. Keeping the first occurrence instead would move a rule ahead of a later ``!`` negation that was meant to
    reverse it — a ``.gitignore`` ``dist`` plus a ``.fwdignore`` ``!dist`` plus the built-in ``dist`` exclusion would
    collapse to ``[dist, !dist]`` and re-include ``dist``, disagreeing with what ``fwd push`` does with the same rules.
    """
    ordered = list(patterns)
    return list(reversed(list(dict.fromkeys(reversed(ordered)))))


def _relocate(pattern: str, directory: str) -> str:
    """Rewrite one gitignore pattern so it means the same thing relative to the synchronization root.

    Git evaluates a nested ``.gitignore`` relative to its own directory, and Mutagen has no equivalent notion, so every
    nested rule has to be rewritten as an equivalent root-relative one. Git's own anchoring rules decide how:

    - a leading ``/`` anchors to the rule file's directory, so it becomes ``<dir>/<pattern>``;
    - any other interior ``/`` also anchors it (Git's "contains a slash" rule), giving the same rewrite;
    - a pattern with no interior slash matches at *any* depth below the rule file, which is ``<dir>/**/<pattern>``.

    ``**/`` matches zero or more directories in Mutagen, so the last form still matches ``<dir>/<pattern>`` itself.
    A trailing ``/`` (directory-only) is preserved throughout because both syntaxes spell it the same way.
    """
    if not directory:
        return pattern
    prefix = directory.strip("/")
    body = pattern.lstrip("/") if pattern.startswith("/") else pattern
    anchored = pattern.startswith("/") or "/" in body.rstrip("/")
    return f"{prefix}/{body}" if anchored else f"{prefix}/**/{body}"


def flatten_gitignore(rule_files: Sequence[tuple[str, str]]) -> list[str]:
    """Flatten every ``.gitignore`` in a worktree into one root-relative Mutagen ignore list.

    Mutagen does not read ``.gitignore``, so continuous sync would otherwise propagate build output, virtualenvs, and
    local caches that every other fwd transfer excludes. Translating the rules keeps the continuous domain identical to
    the one-shot domain instead of introducing a second, wider notion of "the project".

    Args:
        rule_files: ``(directory, text)`` pairs, where ``directory`` is the rule file's worktree-relative directory
            (``""`` for the root file) and ``text`` is its contents. Ordering is preserved because gitignore semantics
            are last-match-wins and a later ``!`` negation must still be able to re-include an earlier exclusion.

    Returns:
        Root-relative patterns in evaluation order, de-duplicated while keeping each pattern's last occurrence so
        last-match-wins evaluation is preserved (see :func:`_dedup_keeping_last`).
    """
    patterns: list[str] = []
    for directory, text in rule_files:
        for line in text.splitlines():
            parsed = _clean_pattern(line)
            if parsed is None:
                continue
            negation, pattern = parsed
            patterns.append(f"{negation}{_relocate(pattern, directory)}")
    return _dedup_keeping_last(pattern for pattern in patterns if pattern not in ("", "!"))


def _gitignore_files(source: Path) -> list[tuple[str, str]]:
    """Collect every ``.gitignore`` that applies to a worktree, root file first.

    ``git ls-files`` is asked for the tracked and untracked-but-not-ignored rule files rather than walking the tree,
    so a ``node_modules`` full of nested ``.gitignore`` files costs nothing. The root file is added explicitly because
    a repository may legitimately ignore or simply not track its own ``.gitignore``.

    A *nested* rule file can ignore itself too (the Convex local-state layout does exactly this), and
    ``--exclude-standard`` hides those. They are re-added through :func:`fwd.selection.ignored_rule_files`, the same
    enumeration the one-shot upload path uses, so the continuous ignore domain matches the push domain instead of
    silently propagating whatever a self-ignored rule file was excluding.
    """
    from fwd.selection import ignored_rule_files

    collected: dict[str, str] = {}
    root_file = source / ".gitignore"
    if root_file.is_file():
        collected[""] = root_file.read_text(encoding="utf-8", errors="replace")
    proc = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".gitignore", "*/.gitignore"],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return list(collected.items())
    try:
        self_ignored = ignored_rule_files(source)
    except Exception:
        # Enumerating the exception is a refinement, not a prerequisite: a failure here must not cost the rules we have.
        self_ignored = []
    for raw in sorted(path for path in [*(item for item in proc.stdout.split(b"\0") if item), *self_ignored]):
        relative = os.fsdecode(raw)
        directory = str(Path(relative).parent) if "/" in relative else ""
        directory = "" if directory == "." else directory
        if directory in collected:
            continue
        try:
            collected[directory] = (source / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    # Root rules first so nested rules, which are more specific, win under last-match-wins evaluation.
    return sorted(collected.items(), key=lambda item: item[0].count("/") if item[0] else -1)


def ignore_patterns(source: str | Path, sync_cfg: SyncConfig) -> list[str]:
    """Build the complete Mutagen ignore list for one project.

    Layered in increasing precedence, matching gitignore's last-match-wins evaluation and the ordering
    :func:`fwd.selection.custom_ignore_patterns` already applies to one-shot transfers, so a file excluded from a push
    is excluded from continuous sync too:

    1. flattened ``.gitignore`` rules (when ``sync.use_gitignore`` is on),
    2. ``.fwdignore`` and the configured ``sync.exclude`` list,
    3. platform metadata that no project setting may re-enable,
    4. ``.git`` — see the module docstring; continuous two-way sync of a live repository database risks corrupting it.
    """
    from fwd.selection import custom_ignore_patterns

    root = Path(source).expanduser().resolve()
    patterns: list[str] = []
    if sync_cfg.use_gitignore and (root / ".git").exists():
        patterns.extend(flatten_gitignore(_gitignore_files(root)))
    # ``custom_ignore_patterns`` returns raw ``.fwdignore`` lines because the one-shot path feeds them to Git as a
    # gitignore file; Mutagen takes patterns, so comments and blank lines have to be resolved here (see
    # :func:`_clean_patterns`) or they would install literal ignore rules for files named "# comment".
    patterns.extend(_clean_patterns(custom_ignore_patterns(root, sync_cfg)))
    patterns.extend(ALWAYS_SYNC_EXCLUDES)
    patterns.append("/.git")
    return _dedup_keeping_last(pattern for pattern in patterns if pattern)


# ---------------------------------------------------------------------------------------------- session lifecycle


def create_arguments(name: str, local_dir: Path, beta_url: str, patterns: Sequence[str]) -> list[str]:
    """Return the exact ``mutagen sync create`` argv for one session.

    Split out from :func:`create` so the argv contract — mode, labels, and the VCS guard — is unit-testable without a
    Mutagen daemon or a network.
    """
    arguments = [
        "sync",
        "create",
        "--name",
        name,
        "--label",
        f"{_SESSION_LABEL}={name}",
        "--mode",
        "two-way-safe",
        # Belt and braces with the explicit "/.git" pattern below: this also covers .svn/.hg, and it is the mechanism
        # Mutagen itself documents for keeping a version-control database out of a two-way session.
        "--ignore-vcs",
    ]
    for pattern in patterns:
        arguments += ["--ignore", pattern]
    arguments += [str(local_dir), beta_url]
    return arguments


def status(name: str) -> SyncSessionStatus | None:
    """Return one fwd-owned Mutagen session's state, or ``None`` when no such session exists.

    Reads Mutagen's Go-template JSON output rather than scraping its human table: the table is explicitly a display
    format, while ``{{json .}}`` is stable and carries the conflict and problem lists ``fwd sync status`` needs.
    """
    return next((entry for entry in status_all() if entry.name == name), None)


def status_all() -> list[SyncSessionStatus]:
    """Return every Mutagen session fwd owns, ignoring any the user created themselves.

    Sessions are selected by fwd's own label rather than by name prefix so a user-created session that happens to be
    called ``fwd-something`` is never reported, paused, or terminated by fwd.
    """
    if binary_path() is None:
        return []
    try:
        completed = _run(["sync", "list", "--label-selector", _SESSION_LABEL, "--template", "{{json .}}"], timeout=30.0)
    except MutagenError:
        return []
    try:
        payload = json.loads(completed.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    return [_status_from_payload(entry) for entry in payload if isinstance(entry, dict)]


def _endpoint_description(payload: Any) -> str:
    """Render one Mutagen endpoint payload as a compact ``host:path`` (or bare path) label."""
    if not isinstance(payload, dict):
        return "?"
    path = str(payload.get("path") or "?")
    host = str(payload.get("host") or "")
    return f"{host}:{path}" if host else path


def _beta_endpoint(payload: Any) -> BetaEndpoint | None:
    """Extract the structured remote side of a session, or ``None`` for a local endpoint or unreadable payload."""
    if not isinstance(payload, dict) or not payload.get("host"):
        return None
    try:
        port = int(payload.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return BetaEndpoint(user=str(payload.get("user") or ""), host=str(payload["host"]), port=port, path=str(payload.get("path") or ""))


def _status_from_payload(payload: dict[str, Any]) -> SyncSessionStatus:
    """Translate one Mutagen session JSON object into fwd's rendering shape.

    Every field is read defensively: a status command must keep working across Mutagen versions, and reporting
    "unknown" beats raising inside the command a user runs precisely because something looks wrong.
    """
    problems: list[str] = []
    for side in ("alpha", "beta"):
        endpoint = payload.get(side)
        if not isinstance(endpoint, dict):
            continue
        if endpoint.get("connected") is False:
            problems.append(f"{side} disconnected")
        for key in ("scanProblems", "transitionProblems"):
            for problem in endpoint.get(key) or ():
                if isinstance(problem, dict):
                    problems.append(f"{side}: {problem.get('path') or '?'}: {problem.get('error') or 'unknown error'}")
    if payload.get("lastError"):
        problems.append(str(payload["lastError"]))
    conflicts = payload.get("conflicts")
    return SyncSessionStatus(
        name=str(payload.get("name") or payload.get("identifier") or "?"),
        identifier=str(payload.get("identifier") or "?"),
        status="paused" if payload.get("paused") else str(payload.get("status") or "unknown"),
        paused=bool(payload.get("paused")),
        alpha=_endpoint_description(payload.get("alpha")),
        beta=_endpoint_description(payload.get("beta")),
        conflicts=len(conflicts) if isinstance(conflicts, list) else 0,
        problems=tuple(problems),
        beta_endpoint=_beta_endpoint(payload.get("beta")),
    )


def ensure_session(endpoint: SSHEndpoint, local_dir: str | Path, remote_dir: str, name: str, sync_cfg: SyncConfig) -> SyncSessionStatus | None:
    """Create, or resume, the Mutagen session for one fwd session and return its resulting state.

    Idempotent by design so launch, attach, and ``fwd sync on`` can all call it without checking first: an existing
    session is resumed (which also reconnects one whose daemon was restarted), and only a genuinely absent one is
    created. The shims and endpoint options are rewritten first because a re-provisioned target may have moved.

    An existing session is matched by name *and* by where its remote side actually points. Names are stable across a
    re-provision (a pod stopped outside fwd, a reclaimed spot instance, a failed terminate) while addresses are not, so
    matching by name alone would resume a session whose beta URL still names the dead host and report success while
    nothing synchronizes. A session pointed somewhere else is terminated here and recreated against the live endpoint.

    Raises:
        MutagenError: If Mutagen refuses to create or resume the session. Callers decide whether that is fatal.
    """
    write_shims([endpoint])
    existing = status(name)
    if existing is not None and existing.beta_endpoint is not None and not existing.beta_endpoint.matches(endpoint, remote_dir):
        _run(["sync", "terminate", name], check=False)
        existing = None
    if existing is not None:
        if existing.paused:
            _run(["sync", "resume", name])
            return status(name)
        return existing
    patterns = ignore_patterns(local_dir, sync_cfg)
    _run(create_arguments(name, Path(local_dir).expanduser().resolve(), remote_url(endpoint, remote_dir), patterns), timeout=300.0)
    return status(name)


def terminate(name: str) -> bool:
    """Terminate one fwd-owned Mutagen session, returning whether one existed.

    Never raises for an absent session: this runs during ``fwd stop`` and ``fwd rm``, where failing to tear down a
    background sync must not be allowed to abort the billing-critical half of the operation. Termination is attempted
    directly rather than after a ``status`` probe, because each probe is one more daemon round-trip during teardown and
    Mutagen already reports "no sessions matched" for a session that is not there.
    """
    if binary_path() is None:
        return False
    completed = _run(["sync", "terminate", name], check=False)
    if completed.returncode == 0:
        return True
    detail = (completed.stderr or completed.stdout or "").strip()
    # Verified against Mutagen: an absent session fails with 'specification "NAME" did not match any sessions'. Matching
    # on the word rather than the full sentence keeps this tolerant of the exact phrasing changing between versions.
    if "match" in detail.lower():
        return False
    raise MutagenError(f"mutagen sync terminate {name} failed: {detail.splitlines()[-1] if detail else f'exit {completed.returncode}'}")


def flush(name: str, *, timeout: float = 120.0) -> None:
    """Force one synchronization cycle and wait for it, so a caller can assert the two sides have converged."""
    _run(["sync", "flush", name], timeout=timeout)


def describe_command(name: str) -> str:
    """Return the raw ``mutagen`` invocation for one fwd session, for users who want to drive Mutagen directly."""
    return shlex.join([MUTAGEN_BINARY, "sync", "list", "--long", name])
