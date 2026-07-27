"""Validate the distributable fwd skill, its Codex metadata, and its plugin package."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "SKILL.md"


def _frontmatter(path: Path = SKILL) -> dict[str, str]:
    """Parse the deliberately simple string-only skill frontmatter without adding a runtime YAML dependency."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return dict(line.split(": ", 1) for line in lines[1:end])


def test_skill_frontmatter_is_platform_neutral_and_trigger_rich() -> None:
    metadata = _frontmatter()
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "fwd"
    description = metadata["description"].lower()
    trigger_families = {
        "remote CPU development": ("remote", "cpu"),
        "GPU compute": ("gpu", "compute"),
        "SSH aliases": ("ssh", "aliases"),
        "RunPod pods": ("runpod", "pods"),
        "Slurm clusters": ("slurm", "clusters"),
        "Claude Code transfer": ("claude code", "workflow"),
        "Codex transfer": ("codex", "workflow"),
        "sync inspection": ("synchronization", "diffs"),
    }
    for family, terms in trigger_families.items():
        assert all(term in description for term in terms), f"{family} is missing from implicit-invocation metadata"


def test_skill_core_is_concise_and_links_every_reference() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 120
    for name in ("targets-and-config.md", "commands-and-lifecycle.md", "agent-transfer.md"):
        assert f"references/{name}" in text
        assert (ROOT / "references" / name).is_file()
    assert "$fwd continue this project" in text
    assert "/fwd continue this project" in text
    assert "fwd diff -q" in text
    assert "Never run bare `fwd`" in text
    assert "uv tool install git+https://github.com/Sid-MB/fwd" in text


def test_openai_metadata_enables_implicit_invocation_and_has_a_codex_prompt() -> None:
    metadata_path = ROOT / "agents" / "openai.yaml"
    metadata = metadata_path.read_text(encoding="utf-8")
    assert 'display_name: "fwd Remote Development"' in metadata
    assert 'short_description: "Move coding work to remote compute"' in metadata
    assert 'default_prompt: "Use $fwd to continue this project on a remote machine."' in metadata
    assert "allow_implicit_invocation: true" in metadata
    assert (ROOT / "skills" / "fwd" / "agents" / "openai.yaml").read_text(encoding="utf-8") == metadata


def test_plugin_manifest_packages_the_root_skill_with_matching_version() -> None:
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert manifest["name"] == "fwd"
    assert manifest["version"] == project["version"]
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "fwd Remote Development"
    assert len(manifest["interface"]["defaultPrompt"]) == 3
    plugin_skill = (ROOT / "skills" / "fwd" / "SKILL.md").read_text(encoding="utf-8")
    assert "../../SKILL.md" in plugin_skill
    assert _frontmatter(ROOT / "skills" / "fwd" / "SKILL.md") == _frontmatter()


def test_readme_documents_explicit_invocation_on_claude_and_codex() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/fwd natural-language instructions" in readme
    assert "$fwd natural-language instructions" in readme
    assert ".codex-plugin/plugin.json" in readme
    assert "never blocks the requested fwd command" in readme
