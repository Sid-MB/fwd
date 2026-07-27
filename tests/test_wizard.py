"""Setup-wizard tests for CPU-first RunPod configuration."""

from __future__ import annotations

from typing import Any

import pytest

from fwd import wizard
from fwd.backends import ConfigChoice, ConfigChoices
from fwd.backends.ssh import SshHostBackend
from fwd.config import DEFAULT_RUNPOD_CPU_IMAGE, DEFAULT_RUNPOD_GPU_IMAGE


def test_setup_launch_hint_pins_and_shell_quotes_the_new_target() -> None:
    assert wizard._launch_command("gpu work") == "fwd up --target 'gpu work'"


def test_runpod_setup_prompts_for_compute_type_first_and_skips_gpu_for_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepting every default produces a CPU target without asking a nonsensical GPU-model question."""
    prompted: list[tuple[str, Any]] = []

    def accept_default(field_name: str, current: Any, **kwargs: Any) -> Any:
        prompted.append((field_name, current))
        return current

    monkeypatch.setattr(wizard, "_prompt_value", accept_default)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: False)
    answers = wizard._prompt_target_values("runpod")

    assert answers == {}
    assert prompted[0] == ("compute_type", "cpu")
    assert ("gpu", "NVIDIA GeForce RTX 4090") not in prompted
    assert ("volume_gb", 50) not in prompted
    assert ("image", DEFAULT_RUNPOD_CPU_IMAGE) in prompted


def test_runpod_setup_switches_to_gpu_defaults_when_gpu_compute_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting GPU reveals the GPU field and changes the suggested image without duplicating those defaults in TOML."""
    prompted: list[tuple[str, Any]] = []

    def accept_default(field_name: str, current: Any, **kwargs: Any) -> Any:
        prompted.append((field_name, current))
        return current

    monkeypatch.setattr(wizard, "_prompt_value", accept_default)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: True)
    answers = wizard._prompt_target_values("runpod", {"compute_type": "gpu"})

    assert answers == {"compute_type": "gpu"}
    assert ("gpu", "NVIDIA GeForce RTX 4090") in prompted
    assert ("volume_gb", 50) in prompted
    assert ("image", DEFAULT_RUNPOD_GPU_IMAGE) in prompted


def test_runpod_advanced_gate_lists_cpu_defaults_without_gpu_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reusable gate summarizes every applicable advanced default and omits conditionally irrelevant fields."""
    confirmations: list[str] = []
    monkeypatch.setattr(wizard, "_prompt_value", lambda field_name, current, **kwargs: current)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: confirmations.append(message) or False)

    wizard._prompt_target_values("runpod")

    assert confirmations == ["Set advanced options? (Defaults: cloud_type = secure; remote_base = /workspace; tool_prefix = /workspace/.fwd-tools; user = root)"]
    assert "volume_gb" not in confirmations[0]


def test_runpod_advanced_gate_lists_gpu_volume_and_skips_fields_when_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    prompted: list[str] = []
    confirmations: list[str] = []
    monkeypatch.setattr(wizard, "_prompt_value", lambda field_name, current, **kwargs: prompted.append(field_name) or current)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: confirmations.append(message) or False)

    wizard._prompt_target_values("runpod", {"compute_type": "gpu"})

    assert "volume_gb = 50" in confirmations[0]
    assert not {"cloud_type", "volume_gb", "remote_base", "tool_prefix", "user"} & set(prompted)


def test_compute_type_has_a_noninteractive_setup_flag() -> None:
    parameters = {parameter.name: parameter for parameter in wizard._parameters("runpod")}
    assert parameters["compute_type"].flag == "--compute-type"
    assert parameters["compute_type"].allow_free_text is False


def test_closed_choices_reprompt_until_a_registered_value(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(("tpu", "gpu"))
    warnings: list[str] = []
    monkeypatch.setattr(wizard, "_ask", lambda label, default="": next(answers))
    monkeypatch.setattr(wizard.ui, "warn", warnings.append)

    value = wizard._prompt_value(
        "compute_type",
        "cpu",
        required=False,
        choices=ConfigChoices((ConfigChoice("cpu"), ConfigChoice("gpu")), allow_free_text=False),
    )

    assert value == "gpu"
    assert warnings == ["compute_type must be one of: cpu, gpu"]


def test_open_choices_accept_custom_provider_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "_ask", lambda label, default="": "future-gpu-v9")
    value = wizard._prompt_value(
        "gpu",
        "NVIDIA GeForce RTX 4090",
        required=False,
        choices=ConfigChoices((ConfigChoice("NVIDIA GeForce RTX 4090"),), allow_free_text=True),
    )
    assert value == "future-gpu-v9"


def test_ssh_advanced_fields_are_skipped_after_showing_resolved_openssh_values(monkeypatch: pytest.MonkeyPatch) -> None:
    prompted: list[tuple[str, Any]] = []
    confirmations: list[tuple[str, bool]] = []

    def answer(field_name: str, current: Any, **kwargs: Any) -> Any:
        prompted.append((field_name, current))
        return "externjohn17" if field_name == "host" else current

    monkeypatch.setattr(wizard, "_prompt_value", answer)
    monkeypatch.setattr(
        SshHostBackend,
        "advanced_config",
        classmethod(lambda cls, values: ({"user": "sid", "port": 2222, "key_path": "~/.ssh/id_work"}, ("user=sid", "port=2222", "identity files=~/.ssh/id_work", "proxy jump=none"))),
    )

    def decline(message: str, *, default: bool) -> bool:
        confirmations.append((message, default))
        return False

    monkeypatch.setattr(wizard.ui, "confirm", decline)
    answers = wizard._prompt_target_values("ssh")

    assert answers == {"host": "externjohn17"}
    assert [name for name, _ in prompted] == ["host", "remote_base"]
    assert len(confirmations) == 1
    assert "Set advanced options? (Defaults:" in confirmations[0][0]
    assert "user=sid; port=2222; identity files=~/.ssh/id_work; proxy jump=none" in confirmations[0][0]
    assert confirmations[0][1] is False


def test_ssh_advanced_fields_use_ssh_g_defaults_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    prompted: list[tuple[str, Any]] = []

    def answer(field_name: str, current: Any, **kwargs: Any) -> Any:
        prompted.append((field_name, current))
        return "server" if field_name == "host" else current

    monkeypatch.setattr(wizard, "_prompt_value", answer)
    monkeypatch.setattr(wizard.ui, "confirm", lambda message, default=False: True)
    monkeypatch.setattr(
        SshHostBackend,
        "advanced_config",
        classmethod(lambda cls, values: ({"user": "alice", "port": 2200, "key_path": "~/.ssh/work", "proxy_jump": "external"}, ("resolved",))),
    )

    wizard._prompt_target_values("ssh")

    assert ("user", "alice") in prompted
    assert ("port", 2200) in prompted
    assert ("key_path", "~/.ssh/work") in prompted
    assert ("proxy_jump", "external") in prompted
