# Coding agents

## Launch Claude Code or Codex remotely

```sh
fwd up --target runpod --agent codex
fwd up work claude
fwd up --no-attach codex
```

Claude transfers the current transcript by default so the remote CLI resumes the same conversation. `--handoff` creates a summarized `HANDOFF.md` instead. `--user-config` copies portable Claude configuration; `--creds` copies live Claude authentication and should be used only when the user explicitly accepts storing it on the remote disk.

Codex copies portable configuration, rules, and skills, but not `~/.codex/auth.json`. Authenticate on the remote when required. Supported Codex installations also start the managed Remote Control app-server beside the terminal TUI. Supported Claude accounts can expose the same interactive conversation through Claude Remote Control. Missing support or authentication does not block the tmux session.

GitHub authentication setup is enabled by default for development targets. Disable it with `--no-setup-github` or `[github] auth = false` when credentials must remain local.

## Send follow-up turns

```sh
fwd send agent "continue the implementation"
fwd send agent --detach "run the long evaluation"
fwd send agent --stop "replace the current approach"
fwd send agent --stop-after "finish, save results, and stop compute"
```

Follow-up turns use the existing remote conversation and durable task log. Normal turns queue in order; `--stop MESSAGE` interrupts the active turn and sends a replacement. `--stop` without a message interrupts the turn but leaves the agent, tmux session, and compute running.

## Install the fwd skill

The package includes an Agent Skills-compatible workflow. The first interactive `fwd` invocation offers to install it for Codex and Claude through `npx skills`. You can also install it directly:

```sh
npx skills add Sid-MB/fwd --skill fwd -g -a codex -a claude-code
```

Invoke the installed skill as `$fwd ...` in Codex or `/fwd ...` in Claude Code. The skill uses non-attaching, machine-readable commands and returns an exact `fwd attach SESSION` instruction when a human terminal is needed.

## Runtime policy

Remote VMs and allocations are treated as the isolation boundary, so registered agents default to full access without an additional agent sandbox. Change that for less-trusted targets:

```toml
[agents.codex]
full_access = false
args = ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
environment = {}
```

Explicit permission arguments take precedence. Environment entries are defaults and do not replace values already exported by the remote shell.
