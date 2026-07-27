"""Setup-wizard tests for CPU-first RunPod configuration."""

from __future__ import annotations

from typing import Any

import pytest

from fwd import wizard
from fwd.config import DEFAULT_RUNPOD_CPU_IMAGE, DEFAULT_RUNPOD_GPU_IMAGE


def test_runpod_setup_prompts_for_compute_type_first_and_skips_gpu_for_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepting every default produces a CPU target without asking a nonsensical GPU-model question."""
    prompted: list[tuple[str, Any]] = []

    def accept_default(field_name: str, current: Any, *, required: bool) -> Any:
        prompted.append((field_name, current))
        return current

    monkeypatch.setattr(wizard, "_prompt_value", accept_default)
    answers = wizard._prompt_target_values("runpod")

    assert answers == {}
    assert prompted[0] == ("compute_type", "cpu")
    assert ("gpu", "NVIDIA GeForce RTX 4090") not in prompted
    assert ("image", DEFAULT_RUNPOD_CPU_IMAGE) in prompted


def test_runpod_setup_switches_to_gpu_defaults_when_gpu_compute_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting GPU reveals the GPU field and changes the suggested image without duplicating those defaults in TOML."""
    prompted: list[tuple[str, Any]] = []

    def accept_default(field_name: str, current: Any, *, required: bool) -> Any:
        prompted.append((field_name, current))
        return current

    monkeypatch.setattr(wizard, "_prompt_value", accept_default)
    answers = wizard._prompt_target_values("runpod", {"compute_type": "gpu"})

    assert answers == {"compute_type": "gpu"}
    assert ("gpu", "NVIDIA GeForce RTX 4090") in prompted
    assert ("image", DEFAULT_RUNPOD_GPU_IMAGE) in prompted


def test_compute_type_has_a_noninteractive_setup_flag() -> None:
    assert wizard.FIELD_FLAGS["compute_type"] == "--compute-type"
    assert "compute_type" in wizard.ESSENTIAL_FIELDS["runpod"]
