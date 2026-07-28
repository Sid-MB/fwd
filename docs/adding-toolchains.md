# Adding a project toolchain

This guide explains how to add built-in project support for an ecosystem such as Haskell, Rust, or Go. Swift is included as a complete reference implementation. A toolchain detects a local project, declares the remote executables it requires, and returns idempotent dependency commands. It does not provision compute, open SSH, install coding-agent settings, synchronize files, or manage tmux.

## Architecture and execution order

The launch pipeline keeps project policy separate from remote installation mechanics:

1. Every registered `Toolchain` class inspects the local project.
2. Detected toolchains return `ToolRequirement` objects and dependency commands.
3. The selected coding agent returns requirements through the same contract.
4. The shared resolver deduplicates requirements by executable.
5. For each tool, the resolver probes the remote command and version first.
6. A working remote installation is used unchanged; fallback installers run only for missing or broken tools.
7. The resolver verifies the command again after each installer.
8. Dependency commands run in the remote project.
9. `.fwd/setup.sh` runs last as the project-owned escape hatch.

The relevant extension points are:

| Area | File | Responsibility |
| --- | --- | --- |
| Shared contracts | `src/fwd/tooling/base.py` | `Toolchain`, `ToolRequirement`, `ToolInstaller`, and aggregate plans |
| Remote resolver | `src/fwd/tooling/resolver.py` | Probe, fallback installation, verification, logging, and actionable failure |
| Built-in requirements | `src/fwd/tooling/requirements.py` | Reusable uv, JS-manager, Swift/Swiftly, Claude, and Codex definitions |
| Toolchain registry | `src/fwd/toolchains/__init__.py` | Explicit ordered list and project plan aggregation |
| Ecosystem implementation | `src/fwd/toolchains/<name>.py` | Detection, requirements, and dependency commands |
| Coding-agent contract | `src/fwd/agents/base.py` | Agent commands, synchronization, sending, and shared tool requirements |
| Core bootstrap | `src/fwd/scripts/bootstrap.sh` | Persistent environment paths and tmux only |

Do not implement a language as a `Backend`. Backends acquire SSH-reachable compute; toolchains prepare projects after the provider-independent sync.

## Implement the class

Simple ecosystems declare exact top-level markers and implement two class methods. The built-in Swift integration is the smallest complete example:

```python
from pathlib import Path

from fwd.tooling import ToolRequirement, Toolchain
from fwd.tooling.requirements import SWIFT


class SwiftToolchain(Toolchain):
    """Prepare Swift packages with the system Swift toolchain or fwd's persistent Swiftly fallback."""

    name = "swift"
    markers = ("Package.swift",)

    @classmethod
    def requirements(cls, project: Path) -> tuple[ToolRequirement, ...]:
        del project
        return (SWIFT,)

    @classmethod
    def dependency_commands(cls, project: Path) -> tuple[str, ...]:
        del project
        return ("swift package resolve",)
```

Override `detect(project)` when exact filenames are insufficient. A Haskell integration may need to recognize `cabal.project`, `stack.yaml`, or a top-level `*.cabal` glob and then choose Cabal or Stack from the actual files. Keep that conditional policy inside the Haskell class rather than adding ecosystem branches to launch or the resolver.

Dependency commands must be:

- safe to repeat during `fwd up` repair;
- non-interactive;
- scoped to the remote project wherever practical;
- ordered so later commands can rely on earlier ones;
- free of provider-specific behavior.

## Reuse or define a requirement

Import an existing requirement when another toolchain or agent already uses the same executable:

```python
from fwd.tooling.requirements import BUN
```

This is the main reuse boundary. When a JavaScript toolchain and a coding agent both need the same requirement, the resolver probes and prepares it once.

Define a new `ToolRequirement` only when the command, version probe, installation policy, or failure guidance is genuinely different. The version command must return zero only for a usable executable. `command -v` alone is insufficient because a stale symlink can resolve while failing immediately.

Fallback installers are optional:

```python
from fwd.tooling import ToolInstaller, ToolRequirement
from fwd.tooling.requirements import CURL

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
    hint="Install Example on the remote host or provide curl for the persistent user-space installer.",
)
```

Prerequisites belong to the specific installer that uses them, not unconditionally to the resulting tool. For example, Codex's npm installer declares `requirements=(NPM,)`, while its Bun installer declares `requirements=(BUN,)`. The resolver first reuses an existing Codex; otherwise it recursively resolves only the current installer path, skips that path if a prerequisite cannot be prepared, and continues to the next installer. Successfully resolved prerequisites are shared across every agent and toolchain requirement in the launch. Cycles fail before running an installer and show the complete executable chain.

Installer requirements:

- use an already working remote command instead of replacing it;
- install without sudo under `FWD_TOOL_PREFIX`;
- put executables in `$FWD_TOOL_PREFIX/bin` or another directory already exported by `fwd-env.sh`;
- put caches under `FWD_SCRATCH`;
- return nonzero when installation did not succeed;
- leave final success to the resolver's version re-probe;
- declare reusable executable prerequisites through `ToolInstaller.requirements` instead of repeating `command -v` checks or embedding another tool's installer;
- provide a precise `hint` for machines where automatic installation is impossible.

Compiler distributions may additionally require system libraries that cannot live under `FWD_TOOL_PREFIX`. Keep that exception explicit and upstream-driven: the Swift installer asks Swiftly to write its platform-specific `--post-install-file`, refreshes apt metadata when that generated script uses apt, runs the script only when the remote account is already root, and otherwise prints it and fails with administrator instructions. The file remains in fwd's scratch directory until it succeeds so a repair rerun can finish an interrupted package setup without downloading Swift again. Do not silently invoke `sudo`, guess distro packages, or make system changes when an existing compiler already passes its probe.

Avoid downloading a compiler merely because fwd supports its ecosystem. Installation happens only after project detection or explicit agent selection produces the requirement.

## Register the toolchain

Import the class and add it to the explicit `TOOLCHAINS` tuple in `src/fwd/toolchains/__init__.py`:

```python
from fwd.toolchains.swift import SwiftToolchain

TOOLCHAINS = (PythonToolchain, JavaScriptToolchain, SwiftToolchain)
```

The order is the dependency-command order for polyglot repositories. Keep independent ecosystems stable and document any intentional ordering constraint.

The registry is explicit by design. Automatic module discovery and packaging entry points would complicate startup, error isolation, and test determinism without improving the normal in-repository PR workflow.

## Reuse the same machinery for agents

Coding agents subclass `fwd.agents.base.Agent` and declare requirements exactly like a toolchain:

```python
class ExampleAgent(Agent):
    name = "example"
    command = ("example",)
    tools = (EXAMPLE,)
```

The class also owns local/remote state preparation, startup construction, and send behavior. See
[`src/fwd/agents/README.md`](../src/fwd/agents/README.md) for the complete extension contract. Agent setup must not add
installer or name-specific branches to `bootstrap.sh` or `ops/launch.py`. Put reusable installation policy in
`tooling/requirements.py`, and let the resolver merge it with detected project requirements.

## Preserve the project escape hatch

Users do not need a built-in integration to use a language. A repository can commit `.fwd/setup.sh`; fwd runs it after every detected toolchain's dependency commands:

```bash
#!/usr/bin/env bash
set -euo pipefail

command -v ghcup >/dev/null 2>&1 || {
    echo "ghcup must be installed on this target" >&2
    exit 1
}

cabal update
```

Use this for private build systems, organization-specific modules, unusual compiler images, or experiments that do not yet justify a general fwd integration.

## Verification checklist

- The class cannot instantiate without its abstract methods.
- Its project markers detect intended projects and ignore unrelated installed build directories.
- Polyglot projects preserve deterministic command ordering.
- Conflicting lockfiles select one package manager when they share an installation tree.
- The requirement reuses a working remote executable without invoking an installer.
- Installer fallbacks run in order and are re-probed.
- Missing required tools fail before tmux with an actionable hint.
- Agent and toolchain requirements deduplicate.
- `.fwd/setup.sh` remains the final dependency step.
- Installer shell fragments pass `bash -n`.
- The wheel contains the new Python module and the core bootstrap remains toolchain-agnostic.
