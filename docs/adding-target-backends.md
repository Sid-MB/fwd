# Adding a target backend

This guide explains how to extend `fwd` with another compute provider such as Google Cloud, Lambda Cloud, or Modal. In `fwd` terminology, a **target** is one configured destination and a **backend** is the provider implementation that creates, finds, stops, and destroys targets of that type.

## Start with the compatibility boundary

`fwd` is not a generic provider SDK. Its shared launch pipeline expects every backend to eventually produce an SSH-reachable Linux machine:

1. The backend creates or reuses compute.
2. It returns an `SSHEndpoint` and an absolute remote project directory.
3. The shared launch code syncs the project, bootstraps tools, transfers the Claude session, starts remote `tmux`, and attaches over SSH.

A provider is therefore a straightforward fit when it offers a durable VM or container with:

- inbound SSH, including a provider proxy or an external host that can reach a private target;
- a writable filesystem that survives for the intended session lifetime;
- enough shell compatibility to run the bootstrap script and `tmux`;
- stable provider identifiers that can be saved locally and queried later.

Serverless jobs without SSH or a durable interactive filesystem do not satisfy the current backend contract. For example, Modal would need an SSH-capable Sandbox or a new transport abstraction; implementing its function API directly as a `Backend` would leave sync, attach, and session resume without a usable connection. Document such a design change separately instead of hiding provider-specific non-SSH behavior behind a fake endpoint.

## Architecture

The extension points are intentionally small:

| Area | File | Responsibility |
| --- | --- | --- |
| Backend contract | `src/fwd/backends/base.py` | `Backend`, config-choice metadata, `TargetInfo`, `TargetStatus`, and diagnostics |
| Provider implementation | `src/fwd/backends/<provider>.py` | Provider API/CLI calls and conversion to the shared SSH model |
| Backend registry | `src/fwd/backends/__init__.py` | Lazy mapping from backend name to implementation class |
| Config model | `src/fwd/config.py` | Typed target dataclass, parsing, defaults, and validation |
| Config discovery | `src/fwd/ops/configcmd.py` | Field descriptions, example TOML, and generated JSON Schema |
| Setup wizard | `src/fwd/wizard.py` | Generic renderer/validator for backend-owned configuration metadata |
| Launch orchestration | `src/fwd/ops/launch.py` | Provider-independent sync, bootstrap, session transfer, and state creation |
| Persistent state | `src/fwd/state.py` | Provider-neutral session data plus open-ended `backend_ids` |

The backend should know how to obtain and manage compute. It should not implement file synchronization, dependency installation, Claude transcript handling, `tmux`, prompts, or tables; those behaviors belong to the shared layers.

## 1. Define the target configuration

Add a slotted dataclass in `src/fwd/config.py`. Defaults should make common configurations short, but a default must not silently select a costly, insecure, or destructive provider option.

```python
@dataclass(slots=True)
class GcpTargetConfig:
    """A Google Compute Engine VM created or reused for one fwd session.

    Attributes:
        project: GCP project containing the VM.
        zone: Compute zone used for creation and lookup.
        machine_type: Default machine shape; may be overridden by a future CLI option only if the launch API supports it.
        remote_base: Persistent parent directory for project checkouts.
    """

    name: str
    backend: Literal["gcp"] = "gcp"
    project: str = ""
    zone: str = ""
    machine_type: str = "n1-standard-8"
    image: str = "ubuntu-2204-lts"
    user: str = ""
    remote_base: str = "~/fwd"
```

Then add the class to the `TargetConfig` union and `TARGET_TYPES`:

```python
TargetConfig = SshTargetConfig | RunpodTargetConfig | SlurmTargetConfig | GcpTargetConfig

TARGET_TYPES = {
    # Existing entries...
    "gcp": GcpTargetConfig,
}
```

Important configuration rules:

- `name` comes from `[targets.<name>]` and is never written as a field.
- `backend` is required in TOML and must be a stable lowercase identifier.
- Validate enum-like values and incompatible combinations in `__post_init__` so bad config fails before provisioning.
- Use `None` for an absent optional value. TOML has no null, so the example renderer will show a commented placeholder.
- Keep secrets out of config whenever the provider already has a credential store, environment variable, CLI login, workload identity, or keychain.
- Treat paths according to their persistence guarantees. Tool caches and project data should not default to ephemeral container storage.

Do not automatically make a provider zero-config merely because its dataclass has defaults. Add implicit resolution in `implicit_target()` only when naming the backend gives one unambiguous, reasonably safe result. RunPod can do this; a GCP project and zone generally cannot.

## 2. Implement `Backend`

Create `src/fwd/backends/<provider>.py` with one backend class inheriting `Backend`. The abstract base class rejects incomplete implementations at instantiation time.

Backends may additionally implement `remote_stop_command(session) -> str | None`. It must return a non-interactive
shell command that performs the provider half of `stop(session)` from the remote endpoint, without depending on the
local fwd process or copying broad local credentials. This enables `fwd up/send --stop-after`; leaving the default
`None` keeps the backend valid but makes those forms fail clearly. fwd itself handles delayed execution, task
dependencies, primary tmux cleanup, cancellation, and task-manager cleanup.

```python
class GcpBackend(Backend):
    """Manage the GCE VM associated with an fwd session."""

    name = "gcp"

    def __init__(self, target: GcpTargetConfig, config: Config) -> None:
        super().__init__(target, config)

    @classmethod
    def config_parameters(cls) -> tuple[ConfigParameter, ...]:
        return (
            ConfigParameter("project", "--project", "GCP project", required=True),
            ConfigParameter("zone", "--zone", "compute zone", required=True),
            ConfigParameter("machine_type", "--machine-type", "VM machine type"),
            ConfigParameter("image", "--image", "boot image"),
            ConfigParameter("user", "--user", "SSH username"),
            ConfigParameter("remote_base", "--remote-base", "parent directory for project checkouts"),
        )

    @classmethod
    def config_choices(cls, parameter: ConfigParameter, values: dict[str, Any]) -> ConfigChoices:
        """Optionally query gcloud for zones or machine types; retain free text when discovery fails."""
        ...

    def provision(self, session_name: str, project_name: str, *, gpu: str | None = None) -> TargetInfo:
        """Create or reuse the named VM, wait for SSH, and return persistent connection details."""
        ...

    def endpoint(self, session: SessionState) -> SSHEndpoint:
        """Resolve the VM's current address without creating, starting, or mutating it."""
        ...

    def status(self, session: SessionState) -> TargetStatus:
        """Return normalized live status and convert all provider/query failures to UNKNOWN."""
        ...

    def stop(self, session: SessionState) -> None:
        """Stop billing compute while preserving the provider resources promised by the target."""
        ...

    def destroy(self, session: SessionState) -> None:
        """Delete the VM and only the storage owned by this fwd session."""
        ...

    def doctor(self) -> list[CheckResult]:
        """Check provider tooling, authentication, configuration, and read-only API access."""
        ...
```

### Provisioning requirements

`provision()` is allowed to be slow, mutate provider state, and incur cost. It must:

- be idempotent for a given `session_name`;
- look up an existing provider resource before creating one;
- restart or reuse an existing stopped resource when that is the provider's normal repair path;
- avoid creating duplicates after partial failures or retries;
- wait until SSH is usable, or deliberately return `PENDING` only when the caller can safely reconcile it;
- raise `ProvisionError` with an actionable user-facing message instead of leaking raw SDK exceptions;
- return a `TargetInfo` whose `remote_dir` is absolute or safely shell-expandable according to existing SSH helpers;
- return persistent `tool_prefix` and optional `scratch` locations appropriate to the provider;
- include stable handles such as instance, region, project, or allocation IDs in `backend_ids`.

Provider resource names should derive deterministically from the fwd session name and obey the provider's length and character limits. If names are truncated or hashed, centralize that transformation so create and lookup cannot disagree.

### Endpoint requirements

`endpoint(session)` is read-only. Addresses and forwarded SSH ports can change after restart, so resolve them from `session.backend_ids` and the provider rather than trusting `session.endpoint`. Return an `SSHEndpoint` containing:

- host, user, port, key path, proxy jump, and extra SSH options as applicable;
- `supports_rsync=False` when the connection proxy cannot carry rsync, allowing the shared sync layer to use tar-over-SSH.

Never start a stopped instance from `endpoint()`. Attach and launch contain the user authorization and billing logic for restarts.

### Status requirements

Map provider-specific states onto `TargetStatus`:

- `RUNNING`: ready for normal use;
- `STOPPED`: exists and can be restarted;
- `PENDING`: provisioning, booting, or queued;
- `GONE`: the provider definitively confirms the resource does not exist;
- `JOB_ENDED`: an allocation-style job ended but its control/login environment still exists;
- `UNKNOWN`: authentication, network, timeout, parse, CLI, or API failures prevent a reliable answer.

`status()` must never raise. Most importantly, never translate “the provider query failed” into `GONE`: callers may treat `GONE` as permission to discard local state while the resource is still running and billing.

### Stop and destroy requirements

`stop()` must be repeatable and safe when the resource is already stopped or absent. Clearly define which costs and data survive it.

`destroy()` runs only after shared confirmation, but the backend must still scope deletion narrowly. Delete only resources created for that session and validate provider IDs before destructive calls. If disks, volumes, static IPs, or snapshots have independent lifecycles, document exactly which ones are removed.

### Persistent work is the default contract

Every provisioning backend must place `TargetInfo.remote_dir`, `tool_prefix`, agent state, and task state on storage
that survives its normal `stop()` operation. If the provider separates compute from durable disks, create or attach
session-owned persistent storage by default, retain it on `stop()`, reattach it on reprovision, and delete it only in
the explicitly destructive `destroy()` path. If durable storage is unavailable for a provider tier, require an
explicit disposable-mode configuration instead of silently falling back.

The shared lifecycle layer checks the remote Git worktree immediately before `fwd stop`, `fwd rm`, bulk removal, and
server-owned stop-after. That protection applies automatically to every backend and includes untracked files; a
backend must not bypass it or hide destructive lifecycle work elsewhere. The check is intentionally only a final
guard—non-Git files, ignored outputs, unpushed commits, credentials, and agent history still depend on the backend's
storage contract.

## 3. Register the backend

Add a lazy registry entry in `src/fwd/backends/__init__.py`:

```python
BACKENDS["gcp"] = ("fwd.backends.gcp", "GcpBackend")
```

The registry intentionally stores module and class names as strings. Preserve lazy loading so a missing optional provider dependency cannot break unrelated commands such as `fwd --help` or an SSH-only launch.

Prefer an already-installed provider CLI when it provides stable structured output and authentication. If an SDK is necessary, consider an optional dependency and import it inside the provider module or method. Report its absence through `ProvisionError` and `doctor()` rather than making every `fwd` installation pull every cloud SDK.

## 4. Make configuration discoverable

Adding the target dataclass and `TARGET_TYPES` entry automatically adds the backend definition to `fwd config --schema`. Complete the human-facing surfaces in `src/fwd/ops/configcmd.py`:

- add one-line descriptions for new fields to `FIELD_DOCS`;
- add provider-specific warnings to `TARGET_FIELD_DOCS`;
- add required example values to `REQUIRED_PLACEHOLDERS`;
- add optional example values to `OPTIONAL_PLACEHOLDERS` when a field defaults to `None` or an empty string;
- add a stable example target name to `EXAMPLE_TARGET_NAMES`;
- extend the CLI's `ExampleBackend` enum in `src/fwd/cli.py`.

Afterward, these commands must explain the backend without requiring source inspection:

```sh
fwd config --example gcp
fwd config --schema
fwd config
fwd config --help
```

The example output must remain valid TOML accepted by `load_config()`. The JSON Schema should contain every field, its real type and default, and a backend discriminator.

## 5. Add setup metadata and CLI flags

The wizard discovers the backend class through the registry and reads `config_parameters()` directly. Describe every
target dataclass field (except injected `name` and discriminator `backend`) with a `ConfigParameter`:

- set `prompt=False` for advanced fields that remain flag/config-only;
- use closed choices with `allow_free_text=False` for real provider enums;
- override `config_choices()` for machine types, regions, GPU identifiers, SSH aliases, or other dynamic suggestions;
- keep provider discovery best-effort so missing credentials or network access never prevents manual setup;
- expose `parameter.flag` from `fwd setup` so every value is available non-interactively to agents.

Do not prompt for every option. The wizard should produce the smallest useful target and leave advanced settings discoverable through `fwd config --example <backend>`.

If the provider requires authentication, the wizard should not collect or write credentials. Point users to the provider's standard login flow and let `doctor()` verify it.

## 6. Account for local diagnostics

Backend-specific checks belong in `GcpBackend.doctor()` and must be read-only. Return separate `CheckResult` rows for concerns a user can fix independently, such as:

- provider CLI or SDK availability and version;
- authentication/account selection;
- required project, region, or zone;
- API access and instance lookup;
- SSH key or OS Login configuration;
- quota or feature availability when it can be checked without provisioning.

Every failed result should include an actionable `hint`. Do not create an instance merely to prove provisioning works.

`src/fwd/doctor.py` already invokes each configured backend's `doctor()`. Add a global local-binary check there only when it improves diagnostics before config-specific checks; avoid duplicating the same provider CLI failure in two places without adding information.

## 7. Use state without changing its schema

`SessionState.backend_ids` is intentionally an open `dict[str, str]`. Store everything needed to re-resolve the resource in a later process, for example:

```python
backend_ids={
    "instance": instance_name,
    "project": self.target.project,
    "zone": self.target.zone,
}
```

Use stable provider identifiers, not display labels or transient IP addresses. The common launch pipeline persists `TargetInfo.backend_ids`; a new backend normally does not require a state migration.

Be careful when configuration can change after launch. An existing session records its backend type and target name, while current config supplies connection policy. Follow the existing backend patterns for choosing between stored identity and current user settings, and never let a repointed config turn `destroy()` into deletion of an unrelated resource.

## 8. Document provider semantics

Add `docs/<provider>-notes.md` when behavior is not obvious from the config reference. At minimum document:

- prerequisites and authentication;
- what is billable in each normalized status;
- what survives `fwd stop`;
- what `fwd rm` deletes;
- storage persistence and recommended `remote_base`/`tool_prefix`;
- SSH, proxy, firewall, and key requirements;
- GPU naming and availability rules;
- provider-specific quotas, startup delays, or cleanup caveats.

Update the README's backend list and configuration examples. Keep the CLI-generated example and schema authoritative for field names and defaults; prose should explain decisions and operational consequences.

## Verification checklist

Verification should focus on the backend's contract and destructive boundaries:

- Importing `fwd.cli` and running `fwd --help` still works without the provider SDK installed.
- `parse_target()` accepts the new target and rejects or warns clearly on invalid values.
- `fwd config --example <backend>` is valid TOML and round-trips through the real config loader.
- `fwd config --schema` includes the new backend, discriminator, fields, types, and defaults.
- Repeated `provision()` calls reuse one provider resource.
- `endpoint()` performs no mutation and handles address churn.
- Every provider state maps to the correct `TargetStatus`.
- API and authentication failures return `UNKNOWN`, never `GONE`.
- Repeated `stop()` calls are harmless and preserve documented data.
- `destroy()` deletes only the intended session resources and tolerates an already-absent target.
- `doctor()` is read-only, never raises, and gives actionable fixes.
- A focused live smoke run covers create, sync, attach, stop, restart, and remove before the backend is advertised as supported.

Do not use a live provider test as the first safety check for destructive logic. Exercise command construction and state mapping with recorded provider responses or narrow fakes first, then run one explicitly scoped end-to-end resource whose name and ownership are easy to verify in the provider console.
