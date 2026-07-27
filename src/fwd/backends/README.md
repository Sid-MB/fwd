# fwd backends

A backend turns provider-specific compute into one normalized, SSH-reachable target. Every backend is one Python
module in this directory and one class inheriting `Backend` from `base.py`. Launch, attach, lifecycle, doctor, and setup
operate only through that class; provider details must not leak into those callers.

## Required interface

Implement these lifecycle methods:

- `provision(session_name, project_name, gpu=None) -> TargetInfo`: idempotently create or reuse compute and return its
  current SSH endpoint, project directory, provider IDs, tooling path, scratch path, and warnings.
- `endpoint(session) -> SSHEndpoint`: cheaply re-resolve an existing resource without starting or creating it.
- `status(session) -> TargetStatus`: normalize provider state. API failures are `UNKNOWN`, never `GONE`.
- `stop(session)`: suspend compute without deleting preserved data.
- `destroy(session)`: permanently delete the backend resource after the caller confirms.
- `doctor() -> list[CheckResult]`: return read-only prerequisite diagnostics instead of raising.

`Backend` is an abstract class, so an incomplete implementation fails when instantiated. `get_backend()` additionally
rejects registered classes that do not inherit it.

## Standardized configuration

Each backend owns setup through `config_parameters() -> tuple[ConfigParameter, ...]`. A parameter declares:

- its dataclass/config field name;
- the non-interactive CLI flag for the same value;
- user-facing help;
- whether it is required and whether setup should prompt for it;
- whether it belongs behind one optional advanced-settings confirmation;
- cheap static choices;
- whether free text outside those choices is valid.

Override `config_choices(parameter, values) -> ConfigChoices` for dynamic suggestions. `values` contains defaults plus
answers collected so far, so discovery can depend on earlier choices. Examples:

- SSH and Slurm return `Host` aliases from `~/.ssh/config` for machine fields and permit arbitrary hostnames/IPs.
- RunPod makes `cpu|gpu` and `secure|community` closed lists.
- RunPod queries `runpodctl gpu list` for GPU identifiers but permits a custom identifier because provider inventory
evolves faster than fwd.

Override `advanced_config(values)` when provider-native configuration can resolve defaults before advanced prompts.
SSH uses `ssh -G <host>` to show the effective user, port, identity files, and proxy jump, then asks once whether the
user wants to override them. Skipping that group leaves the values out of fwd config so OpenSSH remains authoritative.

Choice discovery is guidance, not a launch prerequisite. Catch provider, network, parsing, and missing-CLI failures and
return static or empty choices. Setup must still accept free text where the parameter allows it.

The target dataclass in `fwd.config` remains the runtime configuration schema. Add every prompted parameter to the
backend’s target dataclass and expose an equivalent CLI flag in `fwd setup`; tests should assert that metadata, flags,
generated examples, and JSON Schema agree.

## Adding a backend

1. Add `backends/<name>.py` containing `<Name>Backend(Backend)`.
2. Add its target dataclass and union member in `fwd.config`.
3. Register the module/class strings in `backends/__init__.py`.
4. Implement lifecycle methods and `config_parameters`; add dynamic `config_choices` where useful.
5. Add backend-specific parsing and lifecycle tests using captured provider output rather than live billable resources.
6. Add the backend to `docs/adding-target-backends.md`, CLI setup flags, generated config docs, and the user guide.

Keep helpers that belong only to that provider beside its backend. A separate helper module such as `slurm_job.py` is
appropriate for a substantial independently testable subsystem, but there must be exactly one registered backend
class and one primary `<name>.py` module per backend.
