"""Hatch build hook that creates the manual pages included in distributions.

The generated roff files are intentionally absent from Git. Running generation inside Hatch makes direct wheels,
source distributions, PyPI releases, and installs from Git all carry manuals produced from the exact CLI source being
built. Hatch installs the runtime requirements and click-man declared in pyproject.toml before invoking this hook.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _load_generator(root: Path) -> ModuleType:
    """Load the repository generator while making the src-layout package importable in an isolated build."""
    source_directory = str(root / "src")
    sys.path.insert(0, source_directory)
    try:
        path = root / "tools" / "generate_man_pages.py"
        specification = importlib.util.spec_from_file_location("_fwd_man_page_generator", path)
        if specification is None or specification.loader is None:
            raise RuntimeError(f"unable to load manual-page generator at {path}")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(source_directory)


class CustomBuildHook(BuildHookInterface):
    """Generate fresh section-1 manuals immediately before Hatch collects either build target."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        """Regenerate the manuals from the command tree for the current source checkout."""
        root = Path(self.root)
        generator = _load_generator(root)
        status = generator.generate(root / "man", date=generator.MANUAL_DATE, check=False)
        if status:
            raise RuntimeError("manual-page generation failed")
