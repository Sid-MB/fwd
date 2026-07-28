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


def test_skill_frontmatter_has_required_fields() -> None:
    metadata = _frontmatter()
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "fwd"
    assert metadata["description"]


def test_skill_core_is_concise_and_links_every_reference() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 120
    for name in ("targets-and-config.md", "commands-and-lifecycle.md", "agent-transfer.md"):
        assert f"references/{name}" in text
        assert (ROOT / "references" / name).is_file()


def test_openai_metadata_enables_implicit_invocation_and_is_synced() -> None:
    metadata_path = ROOT / "agents" / "openai.yaml"
    metadata = metadata_path.read_text(encoding="utf-8")
    for key in ("display_name:", "short_description:", "default_prompt:"):
        assert key in metadata
    assert "allow_implicit_invocation: true" in metadata
    assert (ROOT / "skills" / "fwd" / "agents" / "openai.yaml").read_text(encoding="utf-8") == metadata


def test_plugin_manifest_packages_the_root_skill_with_matching_version() -> None:
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert manifest["name"] == "fwd"
    assert manifest["version"] == project["version"]
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"]
    assert manifest["interface"]["defaultPrompt"]
    plugin_skill = (ROOT / "skills" / "fwd" / "SKILL.md").read_text(encoding="utf-8")
    assert "../../SKILL.md" in plugin_skill
    assert _frontmatter(ROOT / "skills" / "fwd" / "SKILL.md") == _frontmatter()
