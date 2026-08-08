# Adding a project toolchain

Toolchains teach `fwd` how to recognize a project ecosystem, ensure its required executables exist on the remote
machine, and prepare its dependencies. They do not provision machines, synchronize files, manage tmux, or install
coding-agent state; those concerns are already handled by the shared launch pipeline.

For a straightforward ecosystem, adding support requires one toolchain class, one registry entry, and—only when no
reusable requirement exists—one shared tool definition.

## 1. Add the toolchain class

Create `src/fwd/toolchains/<name>.py` and subclass `Toolchain`:

```python
from pathlib import Path

from fwd.tooling import ToolRequirement, Toolchain
from fwd.tooling.requirements import EXAMPLE


class ExampleToolchain(Toolchain):
    """Prepare projects whose dependencies are described by example.toml."""

    name = "example"
    markers = ("example.toml",)

    @classmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        del project
        return (EXAMPLE,)

    @classmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        del project
        return ("example install --locked",)
```

`markers` contains exact filenames at the project root. Override `detect(project)` when detection needs globs or
conditional logic, such as choosing between `stack.yaml` and a top-level `*.cabal` file.

Requirements identify executables that must work before dependency setup begins. Dependency commands run inside the
synced remote project and must be non-interactive, idempotent, and safe to repeat when `fwd up` repairs a partial
launch.

## 2. Reuse or define the remote tool

First check `src/fwd/tooling/requirements.py` for an existing `ToolRequirement`. Toolchains and coding agents share
these definitions, and the resolver deduplicates them by executable.

If the executable is new, add a reusable definition there:

```python
from fwd.tooling import ToolInstaller, ToolRequirement

EXAMPLE = ToolRequirement(
    name="Example",
    command="example",
    version_command=("example", "--version"),
    installers=(
        ToolInstaller(
            "official installer",
            'curl -fsSL https://example.invalid/install.sh | env INSTALL_DIR="$FWD_TOOL_PREFIX/bin" sh',
            requirements=(CURL,),
        ),
    ),
    hint="Install Example on the remote host or provide curl for the user-space installer.",
)
```

The resolver always probes `version_command` first, so a working tool already installed on the external machine is
used unchanged. Installers are ordered fallbacks and run only when that probe fails. Declare installer prerequisites
with `ToolInstaller.requirements`; the resolver prepares and shares them recursively.

Installer scripts should:

- install without `sudo`, normally under `$FWD_TOOL_PREFIX`;
- put executable wrappers in `$FWD_TOOL_PREFIX/bin`;
- put temporary downloads and caches under `$FWD_SCRATCH`;
- return nonzero on failure and let the resolver perform the final version check;
- include actionable `hint` text for machines where automatic installation cannot work.

## 3. Register the class

Import the class in `src/fwd/toolchains/__init__.py` and add it to `TOOLCHAINS`:

```python
from fwd.toolchains.example import ExampleToolchain

TOOLCHAINS = (PythonToolchain, JavaScriptToolchain, SwiftToolchain, ExampleToolchain)
```

Registry order is dependency-command order for polyglot projects. Registration is explicit so startup and tests stay
deterministic.

Do not add ecosystem-specific branches to `ops/launch.py`, `remote.py`, or `scripts/bootstrap.sh`. If the class and
shared requirement contracts cannot express a new ecosystem cleanly, improve those contracts rather than bypassing
them.

## 4. Verify the integration

Add focused tests in `tests/test_tooling.py` that cover:

- intended markers are detected and unrelated projects are ignored;
- the correct requirement and dependency commands are selected;
- ambiguous lockfiles or managers resolve deterministically;
- a working remote executable prevents installer execution;
- installer prerequisites and fallback order behave as intended.

Run:

```console
uv run pytest tests/test_tooling.py
uv build --wheel
```

Confirm the wheel contains the new module. For installer shell fragments, also run them through `bash -n` in a test.

The longer architecture and installer-policy guide is in [`dev-docs/adding-toolchains.md`](../../../dev-docs/adding-toolchains.md).
Projects with private or highly specialized setup can instead commit `.fwd/setup.sh`; `fwd` runs it after all detected
toolchain dependency commands.
