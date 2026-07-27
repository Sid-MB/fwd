"""Tests for class-based project toolchains and the shared remote tool resolver."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fwd import toolchains
from fwd.sshexec import SSHError
from fwd.tooling import ToolInstaller, ToolRequirement, Toolchain, ensure_tools, merge_requirements
from fwd.tooling import resolver
from fwd.tooling.requirements import BUN, CLAUDE, CODEX, NPM, PNPM, UV, YARN


def _touch(root: Path, *names: str) -> Path:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def test_builtin_toolchains_return_requirements_with_their_commands(tmp_path: Path) -> None:
    python = toolchains.plan(_touch(tmp_path / "python", "uv.lock"))
    bun = toolchains.plan(_touch(tmp_path / "bun", "bun.lock"))
    npm = toolchains.plan(_touch(tmp_path / "npm", "package-lock.json"))
    pnpm = toolchains.plan(_touch(tmp_path / "pnpm", "pnpm-lock.yaml"))
    yarn = toolchains.plan(_touch(tmp_path / "yarn", "yarn.lock"))

    assert (python.requirements, python.commands) == ((UV,), ("uv sync",))
    assert (bun.requirements, bun.commands) == ((BUN,), ("bun install",))
    assert (npm.requirements, npm.commands) == ((NPM,), ("npm ci",))
    assert (pnpm.requirements, pnpm.commands) == ((PNPM,), ("pnpm install --frozen-lockfile",))
    assert (yarn.requirements, yarn.commands) == ((YARN,), ("yarn --frozen-lockfile",))


def test_conforming_toolchain_class_plugs_into_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swift = ToolRequirement("Swift", "swift", ("swift", "--version"), hint="Install a Linux Swift toolchain.")

    class SwiftToolchain(Toolchain):
        name = "swift"
        markers = ("Package.swift",)

        @classmethod
        def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
            del project
            return (swift,)

        @classmethod
        def dependency_commands(cls, project: Path) -> tuple[str, ...]:
            del project
            return ("swift package resolve",)

    monkeypatch.setattr(toolchains, "TOOLCHAINS", (*toolchains.TOOLCHAINS, SwiftToolchain))
    plan = toolchains.plan(_touch(tmp_path, "Package.swift"))

    assert plan.names == ("swift",)
    assert plan.requirements == (swift,)
    assert plan.commands == ("swift package resolve",)


def test_project_setup_remains_last_after_every_detected_toolchain(tmp_path: Path) -> None:
    plan = toolchains.plan(_touch(tmp_path, "uv.lock", "bun.lock", ".fwd/setup.sh"))
    assert plan.names == ("python", "javascript")
    assert plan.requirements == (UV, BUN)
    assert plan.commands == ("uv sync", "bun install", "bash .fwd/setup.sh")


def test_agent_and_toolchain_requirements_are_deduplicated() -> None:
    assert merge_requirements((BUN, UV), (BUN, CODEX)) == (BUN, UV, CODEX)


def test_conflicting_requirements_for_one_command_fail_early() -> None:
    incompatible = ToolRequirement("Different Bun", "bun", ("bun", "version"))
    with pytest.raises(ValueError, match="conflicting tool requirements"):
        merge_requirements((BUN,), (incompatible,))


class _Endpoint:
    def __init__(self, returncodes: tuple[int, ...] = ()) -> None:
        self.returncodes = iter(returncodes)
        self.scripts: list[str] = []

    def run_script(self, script: str, **kwargs) -> SimpleNamespace:
        self.scripts.append(script)
        return SimpleNamespace(returncode=next(self.returncodes, 0))


class _LocalEndpoint:
    """Execute resolver shell commands in a throwaway HOME to test the real installer/probe boundary offline."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.scripts: list[str] = []

    def run(self, command: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-c", command], env=self.env, capture_output=True, text=True)

    def run_script(self, script: str, **kwargs) -> subprocess.CompletedProcess[str]:
        self.scripts.append(script)
        return subprocess.run(["bash"], input=script, env=self.env, capture_output=True, text=True)


def test_resolver_reuses_a_working_remote_tool_without_installing(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = _Endpoint()
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: (True, "uv 1.0"))

    ensure_tools(endpoint, (UV,))

    assert endpoint.scripts == []


def test_resolver_tries_fallbacks_in_order_and_verifies_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = ToolRequirement(
        "demo",
        "demo",
        ("demo", "--version"),
        installers=(ToolInstaller("first", "false"), ToolInstaller("second", "install-demo")),
    )
    probes = iter(((False, ""), (False, ""), (True, "demo 1.0")))
    endpoint = _Endpoint((1, 0))
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: next(probes))

    ensure_tools(endpoint, (requirement,))

    assert endpoint.scripts[0].endswith("false\n")
    assert endpoint.scripts[1].endswith("install-demo\n")


def test_resolver_fails_with_the_requirement_hint_after_all_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = ToolRequirement("demo", "demo", ("demo", "--version"), installers=(ToolInstaller("none", "false"),), hint="Install demo yourself.")
    endpoint = _Endpoint((1,))
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: (False, ""))

    with pytest.raises(SSHError, match="Install demo yourself"):
        ensure_tools(endpoint, (requirement,))


def test_codex_requirement_uses_bun_fallback_and_produces_a_working_persistent_wrapper(tmp_path: Path) -> None:
    home = tmp_path / "home"
    prefix = tmp_path / "tools"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    npm.chmod(0o755)
    bun = fake_bin / "bun"
    bun.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--version" ]; then echo "bun 1.0"; exit 0; fi\n'
        'if [ -f "${1:-}" ]; then entry="$1"; shift; exec "$entry" "$@"; fi\n'
        'mkdir -p "$BUN_INSTALL/install/global/node_modules/@openai/codex/bin" "$BUN_INSTALL/bin"\n'
        'cp "$0" "$BUN_INSTALL/bin/bun"\n'
        'printf \'#!/bin/sh\\necho "codex 1.0"\\n\' >"$BUN_INSTALL/install/global/node_modules/@openai/codex/bin/codex.js"\n'
        'chmod +x "$BUN_INSTALL/install/global/node_modules/@openai/codex/bin/codex.js"\n',
        encoding="utf-8",
    )
    bun.chmod(0o755)
    prefix.mkdir()
    env_file = prefix / "fwd-env.sh"
    env_file.write_text(f'export FWD_TOOL_PREFIX="{prefix}"\nexport FWD_SCRATCH="{tmp_path / "scratch"}"\nexport BUN_INSTALL="{prefix / "bun"}"\nexport PATH="{prefix / "bin"}:{prefix / "bun" / "bin"}:$PATH"\n', encoding="utf-8")
    (home / ".fwd-env.sh").write_text(f'. "{env_file}"\n', encoding="utf-8")
    endpoint = _LocalEndpoint({**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"})

    ensure_tools(endpoint, (CODEX,))

    assert len(endpoint.scripts) == 2
    wrapper = prefix / "bin" / "codex"
    assert wrapper.is_file()
    result = subprocess.run([str(wrapper), "--version"], env=endpoint.env, capture_output=True, text=True)
    assert result.returncode == 0
    assert "codex 1.0" in result.stdout


@pytest.mark.parametrize("requirement", [UV, BUN, NPM, PNPM, YARN, CLAUDE, CODEX])
def test_builtin_installer_scripts_are_valid_bash(requirement: ToolRequirement) -> None:
    for installer in requirement.installers:
        script = resolver._installer_script(installer.script)
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert result.returncode == 0, f"{requirement.name}/{installer.name}: {result.stderr}"
