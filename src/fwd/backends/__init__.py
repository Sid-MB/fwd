"""Backend registry with lazy imports.

Design intent
-------------
Backends are resolved by name through a table of ``(module, class)`` strings and imported only when actually used.
Eager imports would make every ``fwd`` invocation pay for every backend's module-level work, and would couple
startup time to the slowest backend. Lazy resolution also means a broken or half-written backend cannot break
unrelated commands to provider-specific optional code.
"""

from __future__ import annotations

import importlib

from fwd.backends.base import (
    Backend,
    CheckResult,
    ConfigChoice,
    ConfigChoices,
    ConfigParameter,
    MachineInventory,
    MachineSelectionError,
    MachineType,
    Provisioner,
    ProvisionError,
    TargetInfo,
    TargetStatus,
)
from fwd.config import Config, TargetConfig

__all__ = [
    "BACKENDS",
    "Backend",
    "CheckResult",
    "ConfigChoice",
    "ConfigChoices",
    "ConfigParameter",
    "MachineInventory",
    "MachineSelectionError",
    "MachineType",
    "ProvisionError",
    "Provisioner",
    "TargetInfo",
    "TargetStatus",
    "backend_names",
    "get_backend",
    "make_backend",
]

# Backend name (matches config's ``backend =`` value) -> (module path, class name).
BACKENDS: dict[str, tuple[str, str]] = {
    "lambda": ("fwd.backends.lambda_cloud", "LambdaCloudBackend"),
    "ssh": ("fwd.backends.ssh", "SshHostBackend"),
    "runpod": ("fwd.backends.runpod", "RunpodBackend"),
    "slurm": ("fwd.backends.slurm", "SlurmBackend"),
}


def backend_names() -> list[str]:
    """Return registered backend names, sorted for stable help and error text."""
    return sorted(BACKENDS)


def get_backend(name: str) -> type[Backend]:
    """Import and return the backend class for ``name``.

    Args:
        name: Backend identifier, e.g. ``"runpod"``.

    Raises:
        ProvisionError: If the name is unregistered, or the module/class fails to import — both are surfaced as user
            errors rather than tracebacks because a config typo is the likeliest cause.
    """
    try:
        module_path, class_name = BACKENDS[name]
    except KeyError:
        raise ProvisionError(f"unknown backend {name!r}; expected one of: {', '.join(backend_names())}") from None
    try:
        module = importlib.import_module(module_path)
        backend_class = getattr(module, class_name)
        if not isinstance(backend_class, type) or not issubclass(backend_class, Backend):
            raise TypeError(f"{backend_class!r} does not inherit Backend")
        return backend_class
    except (ImportError, AttributeError, TypeError) as exc:
        raise ProvisionError(f"backend {name!r} failed to load ({module_path}.{class_name}): {exc}") from exc


def make_backend(target: TargetConfig, config: Config) -> Backend:
    """Instantiate the backend implied by a target's ``backend`` field.

    The one-liner every caller in ``ops`` uses, so nobody repeats the lookup-then-construct dance.
    """
    return get_backend(target.backend)(target, config)
