# Adding a coding agent

Coding-agent integrations are class-based plugins inside this package. General launch, attach, and send code depends
only on `Agent`, so a new built-in agent should normally require one new Python module plus one registry entry.

## Implement the contract

Create `src/fwd/agents/<name>.py` and subclass `Agent` from `base.py`:

```python
from typing import Mapping

from fwd.agents.base import Agent
from fwd.tooling.requirements import MY_AGENT


class MyAgent(Agent):
    name = "my-agent"
    command = ("my-agent",)
    tools = (MY_AGENT,)

    def startup_command(self, flags: Mapping[str, object]) -> str:
        return "my-agent"

    def send_command(self, message: str, flags: Mapping[str, object]) -> tuple[str, ...]:
        return ("my-agent", "--print", message)
```

`name` is the magic `fwd up <name>` selector. `command` is persisted in session state and must uniquely resolve the
agent. `tools` uses the shared requirement resolver, which reuses working remote binaries before attempting installs.

Every agent automatically receives the shared `[agents.<name>]` runtime policy from `Agent.launch_flags()`:
`full_access`, `args`, and environment defaults. Use `runtime_args()`, `with_environment_defaults()`, and
`environment_command()` in the implementation so interactive, restarted, and non-interactive send commands behave
consistently. An implementation owns the exact full-access CLI option and must suppress its default when configured
arguments already select a permission or sandbox policy.

Override `prepare_local()` when an agent must create files before the project sync. The returned object is opaque to
the launcher and is passed to `prepare_remote()` after bootstrap and tool installation. Override `prepare_remote()`
to upload settings or import conversation state, and return serializable flags needed by `startup_command()` or a
later restart. Keep optional state transfer best-effort: failure to copy convenience state should not discard a
successfully provisioned machine.

Set `remote_home_entry` to the product-owned hidden directory when authentication, conversations, or managed payloads
live beneath remote `$HOME`. Backends with disposable homes ask the shared base implementation to relocate that
directory beneath the tool prefix before resolving tools; other backends leave the user's home untouched.

Override `launch_flags()` only if the agent owns special CLI/config behavior. Agent-independent orchestration should
never test `agent.name`; put that decision in the implementation instead.

Optional remote-control setup also belongs in the agent implementation because products expose different models.
Claude decorates its long-lived interactive command, while Codex starts a separate managed app-server daemon beside
the primary TUI. Probe both CLI support and compatible account authentication, degrade to the normal terminal session
when unavailable, and never make remote-control enrollment a prerequisite for launching the agent.
Codex must invoke the standalone binary at `~/.codex/packages/standalone/current/codex` for daemon probes and startup;
the npm/Bun CLI exposing the same subcommand help does not establish that the managed daemon payload is installed.

## Register and test it

Import and instantiate the class in `agents/__init__.py`. The registry is explicit so CLI startup, packaging, and
tests remain deterministic.

Add focused tests for exact command resolution, required tools, startup/send argv, and any state-transfer safety
rules. If the agent copies user files, use a strict allowlist and exclude authentication by default.

Agent-specific state helpers belong in this package (for example `claude_state.py` and `codex_state.py`), beside the
class that owns them. Do not add agent branches to `ops/launch.py`, `ops/send.py`, or the bootstrap script.
