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
from fwd.tooling.requirements import BUN, CLAUDE, CODEX, NPM, PNPM, SWIFT, SWIFTLY, UV, YARN


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
    swift = toolchains.plan(_touch(tmp_path / "swift", "Package.swift"))

    assert (python.requirements, python.commands) == ((UV,), ("uv sync",))
    assert (bun.requirements, bun.commands) == ((BUN,), ("bun install",))
    assert (npm.requirements, npm.commands) == ((NPM,), ("npm ci",))
    assert (pnpm.requirements, pnpm.commands) == ((PNPM,), ("pnpm install --frozen-lockfile",))
    assert (yarn.requirements, yarn.commands) == ((YARN,), ("yarn --frozen-lockfile",))
    assert (swift.names, swift.requirements, swift.commands) == (("swift",), (SWIFT,), ("swift package resolve",))


def test_conforming_toolchain_class_plugs_into_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stack = ToolRequirement("Stack", "stack", ("stack", "--version"), hint="Install Stack.")

    class HaskellToolchain(Toolchain):
        name = "haskell"
        markers = ("stack.yaml",)

        @classmethod
        def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
            del project
            return (stack,)

        @classmethod
        def dependency_commands(cls, project: Path) -> tuple[str, ...]:
            del project
            return ("stack setup",)

    monkeypatch.setattr(toolchains, "TOOLCHAINS", (*toolchains.TOOLCHAINS, HaskellToolchain))
    plan = toolchains.plan(_touch(tmp_path, "stack.yaml"))

    assert plan.names == ("haskell",)
    assert plan.requirements == (stack,)
    assert plan.commands == ("stack setup",)


def test_project_setup_remains_last_after_every_detected_toolchain(tmp_path: Path) -> None:
    plan = toolchains.plan(_touch(tmp_path, "uv.lock", "bun.lock", ".fwd/setup.sh"))
    assert plan.names == ("python", "javascript")
    assert plan.requirements == (UV, BUN)
    assert plan.commands == ("uv sync", "bun install", "bash .fwd/setup.sh")


def test_agent_and_toolchain_requirements_are_deduplicated() -> None:
    assert merge_requirements((BUN, UV), (BUN, CODEX)) == (BUN, UV, CODEX)


def test_conflicting_requirements_for_one_command_fail_early() -> None:
    incompatible = ToolRequirement("Different Bun", "bun", ("bun", "version"))
    with pytest.raises(ValueError):
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


def test_resolver_fails_after_all_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = ToolRequirement("demo", "demo", ("demo", "--version"), installers=(ToolInstaller("none", "false"),), hint="Install demo yourself.")
    endpoint = _Endpoint((1,))
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: (False, ""))

    with pytest.raises(SSHError):
        ensure_tools(endpoint, (requirement,))


def test_resolver_installs_shared_prerequisites_once_before_dependent_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    prerequisite = ToolRequirement("runtime", "runtime", ("runtime", "--version"), installers=(ToolInstaller("runtime installer", "install-runtime"),))
    first = ToolRequirement("first", "first", ("first", "--version"), installers=(ToolInstaller("first installer", "install-first", requirements=(prerequisite,)),))
    second = ToolRequirement("second", "second", ("second", "--version"), installers=(ToolInstaller("second installer", "install-second", requirements=(prerequisite,)),))
    installed: set[str] = set()
    scripts: list[str] = []

    def probe(endpoint, requirement):  # noqa: ANN001 - resolver-shaped test double
        return requirement.command in installed, f"{requirement.command} 1.0" if requirement.command in installed else ""

    class Endpoint:
        def run_script(self, script, **kwargs):  # noqa: ANN001 - endpoint-shaped test double
            scripts.append(script)
            command = next(name for name in ("runtime", "first", "second") if f"install-{name}" in script)
            installed.add(command)
            return SimpleNamespace(returncode=0)

    monkeypatch.setattr(resolver, "_probe", probe)
    ensure_tools(Endpoint(), (first, second))

    assert [next(name for name in ("runtime", "first", "second") if f"install-{name}" in script) for script in scripts] == ["runtime", "first", "second"]


def test_unavailable_prerequisite_skips_only_its_installer_path(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = ToolRequirement("unavailable", "unavailable", ("unavailable", "--version"))
    parent = ToolRequirement("parent", "parent", ("parent", "--version"), installers=(ToolInstaller("blocked", "blocked-installer", requirements=(unavailable,)), ToolInstaller("fallback", "fallback-installer")))
    probes = iter(((False, ""), (False, ""), (True, "parent 1.0")))
    endpoint = _Endpoint((0,))
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: next(probes))

    ensure_tools(endpoint, (parent,))

    assert len(endpoint.scripts) == 1
    assert endpoint.scripts[0].endswith("fallback-installer\n")


def test_existing_parent_tool_does_not_resolve_installer_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str] = []

    def probe(endpoint, requirement):  # noqa: ANN001 - resolver-shaped test double
        probed.append(requirement.command)
        return True, "codex 1.0"

    endpoint = _Endpoint()
    monkeypatch.setattr(resolver, "_probe", probe)

    ensure_tools(endpoint, (CODEX,))

    assert probed == ["codex"]
    assert endpoint.scripts == []


def test_resolver_reports_prerequisite_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    first = ToolRequirement("first", "first", ("first", "--version"))
    second = ToolRequirement("second", "second", ("second", "--version"))
    object.__setattr__(first, "installers", (ToolInstaller("via second", "install-first", requirements=(second,)),))
    object.__setattr__(second, "installers", (ToolInstaller("via first", "install-second", requirements=(first,)),))
    monkeypatch.setattr(resolver, "_probe", lambda endpoint, requirement: (False, ""))

    with pytest.raises(SSHError):
        ensure_tools(_Endpoint(), (first,))


def test_codex_requirement_replaces_a_legacy_cli_with_the_managed_standalone_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    prefix = tmp_path / "tools"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    fake_bin.mkdir()
    legacy_codex = fake_bin / "codex"
    legacy_codex.write_text('#!/bin/sh\necho "legacy codex 1.0"\n', encoding="utf-8")
    legacy_codex.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "cat <<'INSTALLER'\n"
        "#!/bin/sh\n"
        'managed_dir="$HOME/.codex/packages/standalone/current"\n'
        'mkdir -p "$managed_dir"\n'
        'printf \'#!/bin/sh\\necho "codex 2.0"\\n\' >"$managed_dir/codex"\n'
        'chmod +x "$managed_dir/codex"\n'
        "INSTALLER\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    prefix.mkdir()
    env_file = prefix / "fwd-env.sh"
    env_file.write_text(f'export FWD_TOOL_PREFIX="{prefix}"\nexport FWD_SCRATCH="{tmp_path / "scratch"}"\nexport PATH="{prefix / "bin"}:$PATH"\n', encoding="utf-8")
    (home / ".fwd-env.sh").write_text(f'. "{env_file}"\n', encoding="utf-8")
    endpoint = _LocalEndpoint({**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"})

    ensure_tools(endpoint, (CODEX,))

    assert len(endpoint.scripts) == 1
    wrapper = prefix / "bin" / "codex"
    assert wrapper.is_symlink()
    assert wrapper.resolve() == home / ".codex/packages/standalone/current/codex"
    result = subprocess.run([str(wrapper), "--version"], env=endpoint.env, capture_output=True, text=True)
    assert result.returncode == 0
    assert "codex 2.0" in result.stdout


def test_swift_installs_through_swiftly_without_making_swiftly_an_unconditional_root(tmp_path: Path) -> None:
    assert SWIFT.installers[0].requirements == (SWIFTLY,)
    assert SWIFTLY not in toolchains.plan(tmp_path).requirements


def test_swift_installer_captures_and_handles_swiftlys_platform_package_script() -> None:
    script = SWIFT.installers[0].script
    assert "--post-install-file" in script
    assert 'if [ "$(id -u)" = "0" ]' in script
    assert "apt-get update" in script
    assert 'bash "$post_install"' in script
    assert 'rm -f "$post_install"' in script
    assert 'rm -f "$FWD_TOOL_PREFIX/bin/$name"' in script
    assert 'exec "$candidate" "\\$@"' in script
    assert "Run this generated script as an administrator" in script


@pytest.mark.parametrize("requirement", [UV, BUN, NPM, PNPM, YARN, SWIFTLY, SWIFT, CLAUDE, CODEX])
def test_builtin_installer_scripts_are_valid_bash(requirement: ToolRequirement) -> None:
    for installer in requirement.installers:
        script = resolver._installer_script(installer.script)
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert result.returncode == 0, f"{requirement.name}/{installer.name}: {result.stderr}"
