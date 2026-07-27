"""In-progress-project scenarios for the docker-sshd harness: uv, bun and pnpm, end to end.

Design intent
-------------
``checks.py`` proves the plumbing works. This proves the *use case* works: a user forwards a directory they have been
working in for hours, so it already contains a built ``.venv``/``node_modules`` and uncommitted edits. The remote must
receive the sources and the lockfile, must NOT receive the platform-specific installed tree, and must be able to
rebuild that tree from the lockfile and actually run the dependency.

Each scenario runs in three phases:

1. **Lock.** Lockfiles cannot be committed to the harness (they would pin versions and rot, and generating them locally
   would require uv/bun/pnpm on every developer's laptop). Instead we push the bare manifest to a scratch directory in
   the container, generate the lockfile there with the real tool, and pull it back with ``sync_down`` — which
   incidentally exercises the download path with a path-scoped pull.
2. **Push.** ``detect_dep_commands`` now sees the lockfile; ``sync_up`` ships the project, and we assert the junk
   installed tree stayed home.
3. **Install and run.** ``run_dep_install`` executes the detected command remotely, then a verification command imports
   the dependency for real. That is the only assertion that cannot be faked by a plausible-looking install log.

Requires the non-minimal bootstrap (real uv/bun installs), so this is the harness's live test of ``bootstrap.sh``.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from fwd.backends.ssh import SshHostBackend
from fwd.config import SyncConfig, load_config
from fwd.remote import detect_toolchain_plan, ensure_tools, run_bootstrap, run_dep_install
from fwd.sshexec import SSHEndpoint, SSHError
from fwd.sync import sync_down, sync_up
from fwd.tooling.requirements import BUN, UV

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion, printing immediately so a hang is attributable to the last line printed."""
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}", flush=True)
    if not condition:
        _failures.append(name)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One package-manager scenario.

    Attributes:
        name: Short label, also the remote directory suffix.
        files: Manifest and source files written into the local fixture (relative path -> content).
        junk: Already-installed tree present locally that must never reach the remote.
        lock_cmd: Command run remotely in the prep dir to generate the lockfile.
        lockfiles: Candidate lockfile names; the first one that appears after ``lock_cmd`` is pulled back.
        expect_command: The install command ``detect_dep_commands`` must produce once the lockfile is local.
        verify_cmd: Remote command that must succeed and print ``verify_expect`` after the install.
        verify_expect: Substring proving the dependency is genuinely importable.
        prep_cmd: Optional command run once before the scenario, to install the manager itself.
    """

    name: str
    files: dict[str, str]
    junk: dict[str, str]
    lock_cmd: str
    lockfiles: tuple[str, ...]
    expect_command: str
    verify_cmd: str
    verify_expect: str
    prep_cmd: str | None = None


UV = Scenario(
    name="uv",
    files={
        # package = false keeps this a "virtual" project, so uv sync installs the dependency without needing a build
        # backend — exactly how most forwarded working directories are shaped.
        "pyproject.toml": '[project]\nname = "fwd-scenario-uv"\nversion = "0.1.0"\nrequires-python = ">=3.9"\ndependencies = ["six"]\n\n[tool.uv]\npackage = false\n',
        "src/app.py": "import six\n\nprint('six version', six.__version__)\n",
        "src/wip_feature.py": "# uncommitted work in progress\n",
        ".gitignore": ".venv/\n__pycache__/\n",
    },
    junk={
        ".venv/pyvenv.cfg": "home = /nonexistent\n",
        ".venv/lib/python3.12/site-packages/stale.py": "# from the laptop, must not travel\n",
        "src/__pycache__/app.cpython-312.pyc": "bytecode\n",
    },
    lock_cmd="uv lock",
    lockfiles=("uv.lock",),
    expect_command="uv sync",
    verify_cmd="uv run python -c \"import six; print('SIX-OK', six.__version__)\"",
    verify_expect="SIX-OK",
)

BUN = Scenario(
    name="bun",
    files={
        "package.json": '{\n  "name": "fwd-scenario-bun",\n  "module": "index.ts",\n  "dependencies": { "left-pad": "1.3.0" }\n}\n',
        "index.ts": 'import leftPad from "left-pad";\nconsole.log("BUN-OK", leftPad("x", 4, "-"));\n',
        "wip.ts": "// uncommitted work in progress\n",
        ".gitignore": "node_modules/\n",
    },
    junk={
        "node_modules/left-pad/index.js": "throw new Error('stale copy from the laptop')\n",
        "node_modules/.bin/stale": "#!/bin/sh\n",
    },
    lock_cmd="bun install",
    # bun >=1.2 writes a text bun.lock; older versions write the binary bun.lockb. Both are valid detection signals.
    lockfiles=("bun.lock", "bun.lockb"),
    expect_command="bun install",
    verify_cmd="bun run index.ts",
    verify_expect="BUN-OK",
)

PNPM = Scenario(
    name="pnpm",
    files={
        "package.json": '{\n  "name": "fwd-scenario-pnpm",\n  "version": "1.0.0",\n  "dependencies": { "left-pad": "1.3.0" }\n}\n',
        "index.js": 'const leftPad = require("left-pad");\nconsole.log("PNPM-OK", leftPad("x", 4, "-"));\n',
        "wip.js": "// uncommitted work in progress\n",
        ".gitignore": "node_modules/\n.pnpm-store/\n",
    },
    junk={
        "node_modules/.pnpm/left-pad@1.3.0/node_modules/left-pad/index.js": "throw new Error('stale')\n",
        ".pnpm-store/v3/files/00/abcdef": "blob\n",
    },
    # pnpm itself is not something fwd installs: detect_dep_commands assumes the manager exists on the remote, the same
    # assumption it makes for npm and yarn. The harness therefore provisions it the way a real user would.
    # Pinned to pnpm 9: the container's Ubuntu-packaged node is v18, and pnpm 10+ requires node >= 22.13.
    prep_cmd="npm install -g pnpm@9",
    lock_cmd="pnpm install --lockfile-only",
    lockfiles=("pnpm-lock.yaml",),
    expect_command="pnpm install --frozen-lockfile",
    verify_cmd="node index.js",
    verify_expect="PNPM-OK",
)

SCENARIOS = (UV, BUN, PNPM)


def _write_fixture(root: Path, scenario: Scenario) -> None:
    """Materialize a mid-development working copy: real sources plus an already-installed tree."""
    for relpath, content in {**scenario.files, **scenario.junk}.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _remote(endpoint: SSHEndpoint, tool_prefix: str, cwd: str, command: str, *, check_: bool = True):
    """Run a command remotely with fwd-env.sh sourced, mirroring what run_dep_install does."""
    env_file = f"{tool_prefix}/fwd-env.sh"
    wrapped = f'[ -f {shlex.quote(env_file)} ] && . {shlex.quote(env_file)}; cd {shlex.quote(cwd)} && {command}'
    return endpoint.run(wrapped, check=check_)


def run_scenario(endpoint: SSHEndpoint, scenario: Scenario, *, workdir: Path, remote_base: str, tool_prefix: str) -> None:
    """Drive one scenario through lock -> push -> install -> run."""
    print(f"\n--- scenario: {scenario.name} ---", flush=True)
    local_dir = workdir / "scenarios" / scenario.name
    local_dir.mkdir(parents=True, exist_ok=True)
    _write_fixture(local_dir, scenario)

    sync_cfg = SyncConfig()

    if scenario.prep_cmd:
        _remote(endpoint, tool_prefix, remote_base, scenario.prep_cmd)

    # -- phase 1: generate the lockfile remotely and pull it into the local working copy --------------------
    prep_dir = f"{remote_base}/prep-{scenario.name}"
    sync_up(endpoint, local_dir, prep_dir, sync_cfg)
    _remote(endpoint, tool_prefix, prep_dir, scenario.lock_cmd)
    present = [name for name in scenario.lockfiles if _remote(endpoint, tool_prefix, prep_dir, f"test -f {name}", check_=False).returncode == 0]
    check(f"{scenario.name}: lockfile generated remotely", bool(present), ",".join(scenario.lockfiles))
    if not present:
        return
    sync_down(endpoint, prep_dir, local_dir, paths=[present[0]])
    check(f"{scenario.name}: lockfile pulled back with sync_down", (local_dir / present[0]).is_file(), present[0])

    # -- phase 2: detection now sees a locked, in-progress project -----------------------------------------
    plan = detect_toolchain_plan(local_dir)
    detected = list(plan.commands)
    check(f"{scenario.name}: toolchain commands", detected == [scenario.expect_command], str(detected))

    remote_dir = f"{remote_base}/scenario-{scenario.name}"
    sync_up(endpoint, local_dir, remote_dir, sync_cfg)

    # Assert on the exact junk paths, not their top-level directory: src/__pycache__/ is junk while src/ itself is a
    # legitimate source tree, so a top-level check would demand that src/ not be shipped.
    for junk_path in scenario.junk:
        absent = _remote(endpoint, tool_prefix, remote_dir, f"test -e {shlex.quote(junk_path)}", check_=False).returncode != 0
        check(f"{scenario.name}: {junk_path} not shipped", absent)
    for source in scenario.files:
        check(f"{scenario.name}: {source} shipped", _remote(endpoint, tool_prefix, remote_dir, f"test -e {shlex.quote(source)}", check_=False).returncode == 0)
    check(
        f"{scenario.name}: lockfile shipped",
        _remote(endpoint, tool_prefix, remote_dir, f"test -f {present[0]}", check_=False).returncode == 0,
        present[0],
    )

    # -- phase 3: install from the lockfile and actually use the dependency ---------------------------------
    try:
        ensure_tools(endpoint, plan.requirements)
        run_dep_install(endpoint, remote_dir, detected)
    except SSHError as exc:
        check(f"{scenario.name}: run_dep_install", False, str(exc))
        return
    check(f"{scenario.name}: run_dep_install", True, " && ".join(detected))

    result = _remote(endpoint, tool_prefix, remote_dir, scenario.verify_cmd, check_=False)
    check(
        f"{scenario.name}: dependency importable on the remote",
        scenario.verify_expect in result.stdout,
        (result.stdout + result.stderr).strip()[-300:],
    )


def main() -> int:
    workdir = Path(os.environ["FWD_HARNESS_WORKDIR"])
    config = load_config(workdir / "project")
    backend = SshHostBackend(config.target("docker"), config)
    info = backend.provision("fwd-scenarios", "scenarios")
    endpoint = info.endpoint
    tool_prefix = info.tool_prefix or ""

    # checks.py already ran bootstrap in MINIMAL mode and left its marker. Drop it so this pass verifies tmux too.
    endpoint.run(f"rm -f {shlex.quote(tool_prefix)}/.fwd-bootstrap-*", check=False)
    run_bootstrap(endpoint, tool_prefix=tool_prefix, remote_dir=info.remote_dir, scratch=info.scratch)
    # The harness intentionally starts without local lockfiles and generates them remotely, so its fixture-preparation
    # phase needs these managers before normal lockfile-driven detection can occur.
    ensure_tools(endpoint, (UV, BUN))

    remote_base = info.remote_dir
    for scenario in SCENARIOS:
        try:
            run_scenario(endpoint, scenario, workdir=workdir, remote_base=remote_base, tool_prefix=tool_prefix)
        except SSHError as exc:
            check(f"{scenario.name}: scenario completed", False, str(exc))

    endpoint.close_control_master()
    print()
    if _failures:
        print(f"{len(_failures)} scenario check(s) FAILED: {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("all scenario checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
