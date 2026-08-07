"""Provider-neutral machine discovery, target resolution, and CLI rendering.

Machine identifiers remain backend-owned because they are provider API values, not fwd aliases. This module owns the
cross-provider presentation contract: every target appears, its configured default is explicit, available and
unavailable values are separate, and fixed targets never pretend to expose selectable hardware.
"""

from __future__ import annotations

from collections.abc import Sequence

from fwd import backends, ui
from fwd.backends.base import MachineInventory, MachineType
from fwd.config import Config, ConfigError, RunpodTargetConfig, TargetConfig
from fwd.ops.session_select import TargetSelector
from fwd.output import OutputFormat


def targets_for_listing(config: Config, selected: TargetSelector | None) -> tuple[TargetConfig, ...]:
    """Resolve a scoped query or return every configured target plus the usable implicit RunPod target."""
    if selected is not None:
        try:
            return (config.target(selected.launch_name),)
        except ConfigError as exc:
            ui.die(str(exc))
    targets = [config.targets[name] for name in sorted(config.targets)]
    if not any(target.backend == "runpod" for target in targets):
        targets.append(RunpodTargetConfig(name="runpod"))
    return tuple(targets)


def inventory_for(target: TargetConfig, config: Config) -> MachineInventory:
    """Read one target's inventory while retaining its row when provider discovery fails."""
    try:
        return backends.make_backend(target, config).machine_inventory()
    except Exception as exc:
        default = getattr(target, "instance_type", None) or ("cpu" if getattr(target, "compute_type", None) == "cpu" else getattr(target, "gpu", None))
        return MachineInventory(default=default, selectable=target.backend in {"runpod", "lambda"}, error=str(exc))


def _machine_rows(values: Sequence[MachineType]) -> tuple[tuple[str, str], ...]:
    """Keep exact copyable provider strings separate from their optional human-readable details."""
    return tuple((item.value, item.detail) for item in values)


def render_target(target: TargetConfig, inventory: MachineInventory, *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """Render one scoped inventory using the same overview-first layout as an all-target query."""
    _render_inventories(((target, inventory),), output_format=output_format)


def _render_inventories(entries: Sequence[tuple[TargetConfig, MachineInventory]], *, output_format: OutputFormat | str) -> None:
    """Render target policy once, then add machine tables only for targets that actually support selection."""
    overview_rows = []
    for target, inventory in entries:
        default = inventory.default or "—"
        selection = "--machine/-m" if inventory.selectable else inventory.fixed or "fixed"
        overview_rows.append((target.name, target.backend, default, selection))
    ui.table("machine selection by target", ("target", "backend", "default machine", "selection"), overview_rows, output_format=output_format)
    errors = tuple((target.name, inventory.error) for target, inventory in entries if inventory.error)
    if errors:
        ui.table("machine inventory errors", ("target", "error"), errors, output_format=output_format)
    for target, inventory in entries:
        if not inventory.selectable:
            continue
        title = f"{target.name} ({target.backend})"
        ui.table(f"{title}: available machines", ("machine string", "details"), _machine_rows(inventory.available), output_format=output_format)
        if inventory.unavailable:
            ui.table(f"{title}: unavailable machines", ("machine string", "details"), _machine_rows(inventory.unavailable), output_format=output_format)


def render(targets: Sequence[TargetConfig], config: Config, *, output_format: OutputFormat | str = OutputFormat.auto) -> None:
    """Render inventories for every requested target in deterministic order."""
    _render_inventories(tuple((target, inventory_for(target, config)) for target in targets), output_format=output_format)
