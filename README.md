# fwd

**Forward your coding session to a remote machine.**

You are working on your laptop and want another machine: a clean CPU VM for development, a GPU, 200 GB of RAM, or
access to your cluster's data. `fwd` moves the working environment there. It provisions (or reuses) a remote target,
mirrors your working directory, installs the requested toolchain, and starts a persistent command in remote `tmux`.
It can carry a Claude transcript across, sync Codex settings and skills, start an ordinary shell or command, and run
durable command and agent tasks whose logs can be reattached later. Close the laptop, return tomorrow, and the remote session is waiting.

When the remote CLI and account support it, agent launches also enable cross-device control. Claude starts the same
interactive conversation with Remote Control enabled for claude.ai and the Claude mobile app. Codex starts its
persistent Remote Control app-server daemon beside the terminal TUI, making the remote machine discoverable to
supported signed-in Codex clients. Missing support, authentication, or enrollment never blocks the tmux session.
Missing agent CLIs are installed from their native vendor installers: Claude uses Anthropic's native distribution,
and Codex uses OpenAI's managed standalone distribution because npm/Bun Codex installations cannot host app-server.

Existing tools either remote-*view* a session that stays pinned to your laptop, or provision machines with no session
story at all. `fwd` does the handoff itself.

```
laptop                                    remote (ssh / RunPod / Slurm)
──────                                    ────────────────────────────
your project dir  ──── rsync ──────────▶  ~/fwd/project
Claude transcript ──── optional ───────▶  ~/.claude/projects/<re-encoded>/
Codex settings    ──── optional ───────▶  ~/.codex/ + ~/.agents/skills/
                                          tmux fwd-<name> → shell / command / agent
    fwd  ◀────────── ssh -t attach ─────  (survives disconnects)
```

## Install

```sh
uv tool install git+https://github.com/Sid-MB/fwd
```

Or try it without installing:

```sh
uvx --from git+https://github.com/Sid-MB/fwd fwd --help
```

Requires Python 3.12+, plus `ssh` and `rsync` locally. On first launch, fwd installs or verifies remote `tmux` plus only the tools required by the detected Python, JavaScript, or Swift project and requested coding agent. Existing tools always win; for example, Node/npm is used when present, Codex can fall back to Bun, and Swift packages can fall back to Swiftly.

## Install as a coding-agent skill

`fwd` ships an Agent Skills-compatible workflow for Claude Code, Codex, and other supporting agents. Ask it to
"Continue this project on a remote CPU machine" and it will launch, sync, and hand the session back:

```sh
fwd
# Accept: Install the fwd skill for Codex and Claude from this local fwd package?
```

The first human terminal invocation offers this after the shell-completion prompt. fwd copies only the bundled
`SKILL.md`, references, and agent metadata from its installed wheel or editable checkout into
`~/.fwd/skill-source/fwd`, then runs `npx skills add` against that local directory for global Codex and Claude use.
No GitHub checkout is involved. Declining is remembered independently in `~/.fwd/skill-prompted`.

After an accepted install, the first interactive invocation of each updated fwd build re-materializes and re-adds
that same local source non-interactively, so both copied and linked skill installations stay current. Missing `npx`
or an unavailable `skills` package only produces a warning and never blocks the requested fwd command; failed
operations remain retryable. Agent, redirected, help/version, and shell-completion invocations never show onboarding
prompts.

Invoke it explicitly as `/fwd natural-language instructions` in Claude Code, `$fwd natural-language instructions` in
Codex, or select it from Codex's `/skills` menu. Matching natural-language requests can invoke it implicitly. The
skill teaches agents the machine-readable, non-attaching CLI workflow and hands `fwd attach` back to you only when an
interactive terminal is needed. `npx skills` is the only supported skill distribution mechanism: people who have not
installed the Python package can install directly from the repository with
`npx skills add Sid-MB/fwd --skill fwd -g -a codex -a claude-code`.

## Quickstart

```sh
cd ~/code/my-project
fwd                       # connect to this project's session; create and attach if none exists
fwd runpod                # connect to this project's RunPod session; otherwise create one interactively
fwd runpod yes            # run a durable task on that session, provisioning it interactively if needed
fwd codex                 # connect to this project's Codex session; otherwise launch Codex interactively
fwd --name demo           # connect to the exact session; otherwise create that name interactively
```

Bare `fwd` is exactly `fwd up --reuse`; root selectors such as `fwd runpod` and `fwd codex` are the corresponding
`fwd up --reuse …` forms. Without an explicit command, they are intended for a human terminal: when all selectors
match an existing session they attach, and when none matches they create and attach. With an arbitrary command, the
selectors choose or provision the session and the command uses the same durable task runner as `fwd send -- COMMAND`;
it therefore appears in `fwd send --ls` and supports the same streaming, backgrounding, cancellation, and
`--stop-after` behavior. Inside an attached session, detach with `ctrl-b d` (tmux); the primary process keeps running.

Every launch installs a remote tmux configuration at `~/.config/fwd/tmux.conf`. Fwd copies the first local config found
at `~/.tmux.conf` or `~/.config/tmux/tmux.conf`; when neither exists, it uses a dependency-free fallback with mouse
support, 100,000 lines of history, vi copy mode, fast escape handling, clipboard integration, focus events, and
selection/scroll bindings. The separate fwd path preserves any remote `~/.tmux.conf`. New tmux servers load the file
at startup, while an already-running server reloads it during `fwd up`.

`--reuse` is intentionally conservative in non-interactive mode: it neither provisions nor takes over the terminal.
Instead it prints the exact `fwd up` command without `--reuse` that an agent or script can use to create the session.
Agents should launch explicitly, for example `fwd up runpod codex` or `fwd up --target work --agent codex`, then hand
an exact `fwd attach NAME` command back to the human.

You do **not** need to run `fwd setup` before `fwd`. Setup only creates or updates saved target configuration; it never
provisions, syncs, launches, or attaches. Bare `fwd` is the complete reuse workflow: it finds a matching session or
launches the layered default command, and runs first-time setup automatically when no target exists. Use `fwd setup`
by itself when you want to add or edit a target without launching it yet.

```sh
fwd ls                    # what is running, and what it is costing you
fwd pull outputs/         # bring results back down
fwd stop                  # suspend compute; CPU RunPod container-disk data is wiped
fwd rm                    # destroy it
```

Run `fwd doctor` if anything misbehaves; it checks local prerequisites and every configured target.

`fwd setup` automatically switches to flag-only mode when stdout is not a terminal or `CLAUDECODE`/`CODEX_AGENT` is
present. This makes setup safe for agents and scripts: missing required values produce the exact flags needed instead
of opening a prompt. Run `fwd setup --help` for every field, or pass `--interactive` to force prompts. For example:

```sh
fwd setup --backend ssh --host my-box --target-name work
fwd setup --backend slurm --login-host login.example.edu --user myusername --remote-base /scratch/myusername/fwd
```

Interactive setup asks only for essential fields first. Backends place uncommon fields behind one reusable
`Set advanced options? (Defaults: …)` gate. RunPod's gate includes cloud type, remote paths, and user; GPU targets also
include volume size, while CPU targets omit it because RunPod CPU pods do not have persistent volumes.

## Commands

### Structured output

Read-oriented commands decouple their data from presentation. `fwd ls`, `fwd doctor`, and `fwd info` build structured
tables or records, then select a renderer:

```sh
fwd ls                         # Rich table in a terminal; Markdown when piped or run by an agent
fwd ls --all-projects          # include sessions belonging to every local project
fwd ls --json                  # structured JSON shortcut
fwd ls --format markdown       # stable GitHub-flavored Markdown table
fwd ls --format json           # JSON object with title, columns, and named row objects
fwd doctor --format json
fwd info --format json
```

`--format auto` is the default. It uses Markdown whenever stdout is not a terminal or `CLAUDECODE`/`CODEX_AGENT` is
set, even if an agent runner allocated a pseudo-terminal. Progress and errors remain on stderr; outside an interactive
terminal they use stable `info:`, `ok:`, `warning:`, and `error:` prefixes instead of terminal glyphs and styling.
`--json` is shorthand for `--format json` on `fwd ls`, `fwd doctor`, `fwd info`, and `fwd send --ls`.
Configuration output remains TOML (`fwd config` / `--example`) or JSON Schema (`fwd config --schema`) because those
formats are already directly machine-readable.

### Session completion

Shell completion for every session-selecting command is state-aware:

```sh
fwd attach <TAB>
fwd up <TAB>                 # sessions, targets/backends, and coding agents
fwd up --target <TAB>        # configured targets, RunPod, and SSH aliases
fwd up --agent <TAB>         # registered coding agents
fwd up --gpu <TAB>           # locally configured GPU identifiers
fwd rm <TAB>
fwd stop <TAB>
fwd send --name <TAB>
fwd push --name <TAB>
fwd setup --backend <TAB>    # backends and backend-specific choices
```

Suggestions come from `~/.fwd/state.json` and include help text with the backend, target, local project directory, and
last-attached time. Target and setup completion also reads local fwd configuration and `~/.ssh/config`; magic agent,
output-format, backend, compute, cloud, and image choices carry short descriptions. Completion never contacts a
provider, so pressing Tab remains fast and cannot start compute.
Shells with descriptive completion support (including Fish and appropriately configured Zsh) display that help as a
tooltip/menu description; other shells still complete the session name. Install scripts with
`fwd --install-completion` or print one for manual setup with `fwd --show-completion`.

On the first interactive invocation, fwd offers to install completion for the detected shell using Typer's standard
installer. Accepting may update the Bash/Zsh startup file; declining is remembered in
`~/.fwd/completion-prompted`. Agents, redirected commands, help/version output, and shell-completion subprocesses
never prompt. It then independently offers to install the bundled coding-agent skill with
the local installed-package payload, remembering that decision in `~/.fwd/skill-prompted`. The explicit
`fwd --install-completion` command remains available after a completion decline. Accepted skill installs are
automatically refreshed from `~/.fwd/skill-source/fwd` once per updated fwd build, without fetching fwd from GitHub.

| Command | What it does | Example |
| --- | --- | --- |
| `fwd [selector flags]` | Alias for `fwd up --reuse`: attach to a match, or create interactively | `fwd --agent codex` |
| `fwd TARGET/BACKEND/AGENT [COMMAND...]` | Connect by positional selector, or run a managed command on that session | `fwd runpod yes` |
| `fwd up [TARGET] [AGENT\|COMMAND...]` (alias `launch`) | Provision/reuse, sync, bootstrap, then start the selected or configured default command | `fwd up runpod codex` |
| `fwd up -r [selectors...]` | Reuse a match; attach without a command, or run a supplied command as a managed task | `fwd up -r work yes` |
| `fwd attach` / `fwd a [selectors...]` | Attach to the unambiguous session matching every selector; add `--raw` to recover from failed launch preparation | `fwd a work codex` |
| `fwd send` / `fwd s -- COMMAND...` | Start a durable remote command task and stream it | `fwd s -- pytest -q` |
| `fwd send agent MESSAGE...` | Send a turn to the Claude/Codex conversation running for this session | `fwd send agent "fix tests"` |
| `fwd send TASK_ID` | Reattach to a background command or agent task | `fwd send cmd-a81f` |
| `fwd send TASK_ID --stop` | Cancel one task without stopping its fwd session or machine | `fwd send cmd-a81f --stop` |
| `fwd send --ls` | List active command and agent tasks with attach/cancel instructions | `fwd send --ls --json` |
| `fwd ls [--all-projects] [column flags]` | List sessions with live status and exposed ports; column flags narrow the table while retaining names | `fwd ls --ports` |
| `fwd ports [SELECTOR] PORT...` | Open persistent loopback-only SSH forwards; `PORT` maps equally and `LOCAL:REMOTE` remaps | `fwd ports runpod 3000 8080:3000` |
| `fwd ports --ls` | Alias for `fwd ls --ports`; one selector or `--all-projects` narrows or expands the view | `fwd ports --ls --all-projects` |
| `fwd ports --close [SELECTOR] [PORT...]` | Close selected forwards, every forward for one session, or every tracked project | `fwd ports --close --all-projects` |
| `fwd push` | Re-sync local changes up | `fwd push --name work` |
| `fwd pull [paths...]` | Bring remote changes down (additive; never deletes local files) | `fwd pull --name work outputs/` |
| `fwd diff [target] [path]` | Compare local and remote synced content; exit 0 same, 1 different, 2 error | `fwd diff pod src/` |
| `fwd stop [session/target/backend]` | Kill remote tmux and suspend the target; CPU RunPod container-disk data does not survive | `fwd stop work` |
| `fwd rm [session/target/backend]` / `fwd rm --all` | Destroy one or every target and forget the session state (confirms first) | `fwd rm work` |
| `fwd uninstall` | Remove local data, skills, completions, and temporary logs, then print the package-manager removal command | `fwd uninstall` |
| `fwd setup` | Create/update a saved target without provisioning or launching; prompts in terminals and accepts every field as a flag | `fwd setup --backend ssh` |
| `fwd doctor` | Check local prerequisites and target reachability | `fwd doctor --json` |
| `fwd default COMMAND...` | Set what bare `fwd` launches; user scope by default, with project/target overrides | `fwd default codex` |
| `fwd config` | Print the effective merged config, annotated with where each value came from | `fwd config` |
| `fwd config set KEY VALUE...` | Set any config key; the general form underlying `fwd default` | `fwd config set sync.delete false` |
| `fwd config rm KEY` | Remove one value at user, project, or target scope, revealing the next-higher default | `fwd config rm default_command` |
| `fwd config --example [backend]` | Print a commented reference config generated from the schema | `fwd config --example runpod` |
| `fwd config --schema` | Print the complete machine-readable JSON Schema for editor and agent tooling | `fwd config --schema` |
| `fwd -V` | Print the installed version | `fwd -V` |
| `fwd info` | Print version plus config and state paths | `fwd info --json` |

### Durable remote tasks

`fwd send` (alias `fwd s`) executes from the running session's remote project directory. Every command runs in a
dedicated window inside a hidden per-session tmux task manager, writes a persistent log, and receives a task ID:

```sh
fwd send -- pwd
fwd s -- python train.py --epochs 10
fwd send --name my-session --timeout 30 -- cat results.json
fwd send --detach -- python train.py
fwd send --stop-after -- pytest -q
```

By default, output streams until the task finishes and `fwd send` returns the remote exit code. If a task lasts two
seconds in an interactive terminal, fwd prints `(Press Ctrl-C to cancel, Ctrl-B to background)`. Ctrl-C cancels only
that remote task; Ctrl-B closes the local viewer while the task continues. `--detach` backgrounds immediately.

List, reattach, or cancel tasks from any later terminal:

```sh
fwd send --ls                         # active tasks; add --all for task history
fwd send --ls --format json           # stable machine-readable task inventory
fwd send cmd-a81f                     # replay its log and continue following
fwd send cmd-a81f --stop              # cancel it; the fwd session remains alive
fwd send stopafter                    # stop remotely after every active task
fwd send cancel                       # cancel every queued (not running) task
fwd send cancel cmd-a81f              # cancel any exact queued/running task
fwd send cancel stopafter             # disarm queued remote shutdown
fwd send cancel all                   # cancel every active task
```

`--stop-after` creates a second durable lifecycle task before new work starts. It waits for that command or agent turn
to finish, then stops the session from the remote host, so closing or shutting down the local computer cannot defeat
it. `fwd send stopafter` queues the same action after all current work. The stop action and its dependencies appear
in `fwd send --ls`; `fwd ls` has a `stop after` column. RunPod uses its preinstalled pod-scoped `runpodctl`, Slurm
uses `scancel`, and SSH closes fwd's tmux sessions without powering off a machine fwd does not own.

It never provisions or restarts compute, so stopped, pending, ended, missing, and unknown targets fail with an
actionable message. Arguments are executed literally. To use shell syntax such as pipes, redirects, or globs, request
a shell explicitly:

```sh
fwd send -- bash -lc 'cat outputs/*.json | jq .'
```

For Slurm targets, command tasks run on the SSH login node, just like sync and bootstrap. Use `srun` explicitly
when a command must run inside an allocation.

### Local port forwarding

`fwd ports` exposes a remote loopback service only on this computer's `127.0.0.1`; it does not publish a RunPod or
cluster port to the internet. A bare port maps the same number on both ends, while `LOCAL:REMOTE` selects a different
local port. One optional session, target, backend, agent, or displayed tmux-session selector uses the same matching
rules as attach; `--name` is the unambiguous exact-session form. When no mapping is present, `fwd ports [selector]`
lists the selected session's forwarding.

```sh
fwd ports 3000 3210                    # current project's remote ports on the same local ports
fwd ports runpod 8080:3000             # local 8080 -> remote 127.0.0.1:3000
fwd ports fwd-desktop-4c24e0 3000      # a displayed tmux name is accepted as a session alias
fwd ports --ls --all-projects          # alias for fwd ls --all-projects --ports
fwd ports --close 3000                 # close one local port in the current project
fwd ports runpod --close               # close every forward for the matching session
fwd ports --close --all-projects       # close all locally managed forwards
fwd up --ports 3000 --ports 8080:3000  # override configured launch mappings for this invocation
```

The command probes every requested local bind before contacting SSH. If any local port is occupied or repeated, the
whole request fails and opens nothing. Successful forwards run through a dedicated background SSH control connection,
survive after the command returns, appear in the normal `fwd ls` ports column, and close automatically with `fwd stop`
or `fwd rm`. An SSH connection lost underneath a tunnel is shown as `(inactive)` instead of being reported as exposed.
When a provider changes SSH endpoints, the old forwarding master is closed and the tracked mappings are recreated
against the current machine. A failed close retains its mappings in local state instead of hiding a potentially live
tunnel. In JSON output, `ports` is an array of `{local, remote, active}` records rather than formatted display text.

Set project defaults in `.fwd/config.toml`; the project list replaces any user-level forwarding list. Repeated
`fwd up --ports/-p` values replace the configured defaults for that invocation while preserving unrelated forwards
already opened manually:

```toml
[forwarding]
ports = ["3000", "8080:3000"]
```

Use `fwd ls --columns backend,status,ports` for a focused view. The existing `--names`, `--backends`, `--statuses`,
`--stop-after`, `--running`, `--tmux`, `--local-dirs`, `--last-attached`, `--ids`, and `--ports` flags remain shortcuts
and can be combined with `--columns`; session names remain present as row identity.

### Sending agent turns

When the session was launched with `fwd up claude` or `fwd up codex`, `agent` resolves to that exact agent:

```sh
fwd send agent "Run the tests and fix failures"       # stream the turn
fwd send agent --detach "Run the long benchmark"      # return after it is queued
fwd send agent --stop                                 # cancel the active turn only
fwd send agent --stop "Try the smaller implementation"
fwd send agent --immediate "Try the smaller implementation"  # same cancel-and-send behavior
fwd send agent --stop-after "Finish the task, then stop compute"
```

Normal follow-ups serialize behind an active managed agent turn. `--stop MESSAGE` and `--immediate MESSAGE` interrupt
the active turn and start the replacement in the same remote conversation. Before any managed send exists,
`--stop` sends Ctrl-C to the original Claude/Codex pane created by `fwd up`; it does not kill the agent, tmux session,
pod, VM, or Slurm allocation. Explicit `claude` and `codex` selectors are also accepted and fail clearly if they do
not match the agent running in the selected fwd session.

Interactive terminals render agent text and tool activity concisely. Pipes, scripts, and recognized agent
environments receive the agents' original JSONL event stream.

Codex sends address the exact long-lived TUI pane created by `fwd up codex`, then follow that pane's persisted rollout
events. They do not start a second `codex exec resume --last` process, so a fresh conversation responds normally,
concurrent Codex histories cannot steal the message, and each turn avoids a second CLI/model startup.

Remote Claude Code and Codex sessions also receive a small managed user-instruction block explaining the literal
`stopafter` command. An agent can run it as its final tool action to schedule remote shutdown; `stopafter --cancel`
disarms the delay before shutdown begins. fwd installs the helper under its existing tool prefix and adds the managed
guidance to the agents' documented user-level instruction files, not to the synchronized project, so `fwd diff`
remains clean.

### Comparing local and remote content

`fwd diff` is a read-only synchronization check with the same exit contract as the standard `diff` command:

```sh
fwd diff                      # current directory's session; compare the entire synced project
fwd diff pod                  # exact session, target label, or backend selector
fwd diff pod src/model.py     # compare one project-relative file
fwd diff pod outputs/         # compare one directory recursively
fwd diff -q pod               # no diff text; inspect only the exit status
fwd diff --include-gitignored # include Git-ignored files, but retain explicit sync exclusions
fwd diff --include-unsynced   # include all ordinary unsynced files
```

Exit `0` means identical, `1` means differences were found, and `2` means resolution, transfer, or comparison failed.
Differences use familiar Git unified formatting and retain color in a human terminal; redirected output is plain text, and progress and errors stay on stderr. Exact session names,
target labels, and backend names use the same safe existing-session resolver as lifecycle and transfer commands. The comparison applies
`.gitignore`, `.fwdignore`, and configured sync exclusions, so intentionally unsynced environments and
build caches do not produce false differences; for diagnostic safety, even a tracked path matching `.gitignore` stays
hidden by default. `--include-gitignored` restores only Git-ignored content, while
`--include-unsynced` restores every ordinary sync exclusion. `.git/`, `.DS_Store`, AppleDouble `._*`, Windows
metadata, and similar permanent junk remain excluded in every mode. Both sides are copied into temporary snapshots;
neither the checkout nor the remote project is modified.

### `fwd up` flags

| Flag | Effect | Example |
| --- | --- | --- |
| `[TARGET] [AGENT\|COMMAND...]` | Optional target/backend, then a registered agent or streamed durable command; omit the command to use layered `default_command` | `fwd up pod codex` |
| `--target/-t NAME` | Which configured target to use (default: `default_target`) | `fwd up -t pod` |
| `--agent NAME` | Select a registered coding agent without positional ambiguity | `fwd up --agent codex` |
| `--gpu SPEC` | Override the GPU for this launch (RunPod GPU id, Slurm `--gres`) | `fwd up --gpu A100` |
| `--name/-n NAME` | Session name (default: derived from the directory) | `fwd up -n demo` |
| `--new` | Force a fresh session instead of reusing this directory's existing session | `fwd up --new codex` |
| `--reuse/-r` | Reuse a conjunctive match; attach when no task command is supplied, or create only interactively | `fwd up -r pod codex` |
| `--restart/-y` | With `--reuse`, authorize restarting stopped billable compute | `fwd up -r -y demo` |
| `--session` / `--handoff` | How to carry conversation context — see below | `fwd up --handoff claude` |
| `--user-config` | Upload your `~/.claude` bundle (CLAUDE.md, skills, agents, commands) | `fwd up --user-config claude` |
| `--creds` | Copy Claude credentials to the remote machine | `fwd up --creds claude` |
| `--attach/-a` | Attach directly after startup instead of streaming an explicit command | `fwd up -a -- bash` |
| `--no-attach`, `--detach` | Stay local even when an interactive agent launch would normally auto-attach | `fwd up --detach codex` |
| `--stop-after` | Stop the remote session server-side after an explicit streamed command completes | `fwd up --stop-after -- pytest -q` |

`fwd up` is also the **repair** command. Every stage is idempotent, so if a launch dies halfway through bootstrap, run
it again and it picks up where it left off rather than starting over or duplicating anything. Pass `--new` when the
duplication is intentional: fwd adds a unique suffix, provisions a separate provider resource, and keeps the existing
session available. `--new` inherits the current directory session's target unless `--target` chooses another one.

The startup forms are:

```sh
fwd                               # equivalent to: fwd up --reuse
fwd runpod                        # equivalent to: fwd up --reuse runpod
fwd runpod yes                    # reuse/provision RunPod, then stream yes as a managed task
fwd --agent codex                 # connect to this project's Codex session, or create it interactively
fwd --name demo                   # connect to exact name, or create it interactively
fwd up                            # launch layered default_command and use default_target
fwd up runpod                     # launch layered default_command on RunPod
fwd up runpod codex               # launch Codex on RunPod
fwd up --target work --agent codex  # the fully explicit spelling
fwd up claude                       # transfer this conversation and auto-attach in a human terminal
fwd up codex                        # sync Codex settings/skills and auto-attach in a human terminal
fwd up --no-attach codex            # start Codex persistently but stay in the local terminal
fwd up -a work python train.py      # run the command in the primary pane and attach directly
fwd up -- python train.py --epochs 10  # stream a durable task; '--' protects its flags
fwd up --stop-after -- pytest -q    # stream tests, then stop remotely even if this laptop disconnects
```

By default, an explicit arbitrary command runs as a durable task after selecting or provisioning the session: fwd
uses the same task manager as `fwd send -- COMMAND`, streams its output, returns its exit status, and shows Ctrl-C to
cancel or Ctrl-B to background after two seconds. The task receives an ID and remains visible in `fwd send --ls`.
The session's primary pane remains a login shell, so the session stays attachable after a finite command completes.
Pass `--attach/-a` to run the command in the primary pane and enter tmux directly; after a successful finite attached
command, that pane falls through to a login shell, while a nonzero exit remains visible as a launch failure.

`--stop-after` is valid only for an explicit streamed command, because bare shells, direct attachments, and
interactive agents have no objective completion point. For agent work, use `fwd send agent --stop-after "MESSAGE"`,
queue `fwd send stopafter`, or tell the remote agent to run `stopafter` as its final action.

Selectors are conjunctive: `fwd up -r --name demo --target work --agent codex` attaches only when one session matches
all three values. Without an exact name, matching is scoped to the current project. A sole saved match is
unambiguous; if several match, the sole session whose target is running or pending wins only when every other status
is known. Otherwise fwd asks for an exact session name. `fwd attach` and `fwd a` use this identical parser and
precedence.

An exact stored session name is recognized first. Otherwise a configured target or backend consumes the first
positional, followed by an agent or arbitrary command. If a target and agent have the same name, the target wins and
fwd warns with `--agent NAME` plus the config location needed to rename the target. Registered top-level commands
always have higher priority, so a target called `stop` cannot shadow `fwd stop`. Unknown root words remain errors.

Backend selectors such as `ssh`, `runpod`, and `slurm` use the most recently used configured target of that type; a
sole target is unambiguous before it has history. If several targets share a backend and history cannot choose, fwd
asks for an exact target. In non-interactive mode, `--reuse` always errors with either an exact attach instruction or
the corresponding creation command without `--reuse`.

Commands that operate on an existing session accept the same target-label and backend aliases wherever they accept a
session selector: `attach`, `stop`, `rm`, and `diff` positionally, and `send`, `push`, and `pull` through `--name`.
Exact session names always win. Aliases search all saved projects outside the project-scoped reuse/attach grammar and
use the same sole-active-session disambiguation rule described above.

`fwd up codex` copies portable Codex configuration before starting the remote CLI: `~/.codex/config.toml`, named
profiles, `AGENTS.md`, rules, and skills from both `~/.agents/skills` and the legacy `~/.codex/skills` location.
Authentication is deliberately not copied: `~/.codex/auth.json` contains credentials, so run `codex login` remotely
when needed. Agent launches auto-attach only when stdin and stdout are terminals and neither `CLAUDECODE` nor
`CODEX_AGENT` marks an agent environment; scripts and agents remain non-attaching automatically.

## Carrying your Claude session across

`fwd up claude` and bare `fwd` move the **actual transcript** by default, so the remote session resumes with real
context — it remembers what you asked an hour ago, not a summary of it. This was verified empirically against claude 2.1.220
(`docs/session-transfer-notes.md`): a relocated transcript resumes in place, keeps its session id, and does not fork.

The transfer degrades gracefully rather than failing a launch. The chain is:

1. **`--session`** (default) — export the local transcript, rewrite the embedded paths for the remote cwd and home,
   install it remotely, and start `claude --resume <id>`.
2. **`--handoff`** — ask your local `claude -p` to write `HANDOFF.md`, sync it up, and start
   `claude "Read HANDOFF.md, then continue the work it describes"`. Use this when the conversation is long and you
   only need the conclusions. Passing `--handoff` explicitly *replaces* the transcript transfer.

   Generating a handoff takes ~65 seconds, so an existing `HANDOFF.md` less than 15 minutes old is **reused** rather
   than regenerated — otherwise every repair rerun of `fwd up` would pay that minute again to re-summarize a
   conversation that has not changed. Delete the file to force a fresh one.
3. **plain `claude`** — if there is no transcript for this directory, or the remote import cannot be validated, the
   session starts clean with a warning. A launch is never aborted over context transfer.

Two extras, both opt-in because they touch files you may not want leaving your laptop:

- **`--user-config`** uploads `~/.claude/CLAUDE.md`, `skills/`, `agents/`, `commands/` and `settings.json`. There is a
  hard exclusion list: `settings.local.json`, `.credentials.json` and history are never included, even if you ask.
- **`--creds`** ⚠️ lifts your Claude OAuth token out of the macOS Keychain and writes it to
  `~/.claude/.credentials.json` on the remote machine (mode 600). **This places a live credential on a machine you may
  not control** — a shared cluster login node, or a provisioned pod whose disk you do not own. Prefer logging in inside the
  remote session. `fwd` warns every time this flag is used.

Set defaults for any of these under `[claude]` in your config.

### GitHub authentication

Remote repositories include `.git`, so agents can create commits, but fwd does not copy a GitHub credential by
default. Opt in explicitly when the remote should fetch private dependencies or push commits:

```toml
[github]
auth = true
```

The command form writes the same project-local setting:

```sh
fwd config set --project github.auth true
```

This requires a working local `gh auth status --active --hostname github.com`. Before provisioning, fwd validates that
login; during launch it installs the official GitHub CLI release if needed and streams `gh auth token` directly into
remote `gh auth login --with-token`. The token is never placed in argv, logs, fwd config, or session state.
`gh auth setup-git` configures HTTPS pushes, while the effective local `user.name` and `user.email` fill only missing
repository-local values. On RunPod GPU targets, the remote gh credential store lives under the persistent tool prefix
and its standard `~/.config/gh` path is recreated after `/root` resets. Enabling this places a live GitHub credential
on the remote volume; omit the section for untrusted targets. The setting is applied by the next `fwd up`; after that,
commands such as `fwd send git push` use the remote GitHub CLI credential helper. Existing SSH Git remotes continue to
use remote SSH credentials, so use an HTTPS GitHub remote when opting into token transfer.

`fwd pull && git push` is not an equivalent fallback for a commit created remotely. Pull deliberately excludes
`.git/`, so it retrieves working-tree content but not remote commits, refs, or objects. For uncommitted remote changes,
pull the files, commit locally, and push locally. For an already-created remote commit, enable GitHub authentication
and push it from the remote session, or explicitly export and apply a patch or Git bundle before pushing locally.

## Project toolchains

fwd detects Python, JavaScript, and Swift Package Manager projects from their manifests and lockfiles, then prepares only the tools required by
that project and the selected coding agent. Every requirement first probes the remote command and version, so an
existing `uv`, Bun, nvm, npm, pnpm, Yarn, Swift, Claude Code, or Codex installation is reused when it is visible to non-interactive
SSH commands. Missing tools use ordered user-space fallbacks under the target's persistent fwd tool directory; fwd
recursively prepares only the selected fallback's prerequisites, deduplicates them across agents and project
toolchains, and verifies every resulting command before running dependency setup. A JavaScript project with `.nvmrc`
gets a persistent nvm installation and its selected Node version even when Bun owns `node_modules`; fwd exposes nvm in
attached shells without depending on a machine-specific `~/.nvm` path. When npm is otherwise required but Node is
missing, the same nvm fallback selects the project's `.nvmrc` version or the latest Node LTS; pnpm and Yarn can then
install through that npm fallback.

Repositories can commit `.fwd/setup.sh` for an unsupported language, private build system, or extra setup. It runs
after detected toolchain dependency commands. Swift packages use their top-level `Package.swift`, reuse an existing Linux Swift installation, or install the latest stable toolchain through the official Swiftly installer before running `swift package resolve`; when Swiftly reports missing distro packages, fwd installs its generated prerequisites on root-owned disposable machines such as RunPod and gives non-root targets the exact administrator script. Contributors adding first-class Haskell, Rust, or another
ecosystem should read [Adding a project toolchain](docs/adding-toolchains.md): integrations conform to one
`Toolchain` class, return shared `ToolRequirement` values, and add one explicit registry entry. Coding agents use the
same resolver, so agent and project requirements are deduplicated.

## Configuration

**Run `fwd config --example` for an always-up-to-date commented reference** — it is generated from `fwd`'s own
dataclasses, so it lists every field with its real default and cannot drift from the code. `fwd config --example slurm`
narrows it to one backend, and the output is valid TOML you can redirect straight into a config file. To see what your
own files currently resolve to, and which file set each value, run `fwd config`. For agents, editors, and validators,
`fwd config --schema` emits the same contract as JSON Schema Draft 2020-12.

Provider authors should read [Adding a target backend](docs/adding-target-backends.md), which covers the SSH compatibility boundary, backend contract, config/schema registration, lifecycle safety, state, documentation, and verification. Language and build-system contributors should read [Adding a project toolchain](docs/adding-toolchains.md).

`~/.fwd/config.toml` is the global config; a project-local `.fwd/config.toml` **deep-merges over it**, so a repo can
override a single field of a globally-declared target without restating the rest.

### Default command

Bare `fwd` attaches to the current directory's existing session. When there is no session yet, it launches the
configured default command; Claude is the built-in default. Set it without editing TOML:

```sh
fwd default codex                              # user-wide default
fwd default --project claude                   # only this project
fwd default --target runpod -- python -m agent # whenever the selected target is runpod
```

The equivalent general command is `fwd config set default_command ...`:

```sh
fwd config set default_command codex
fwd config set --project default_command -- python -m agent
fwd config set sync.delete false
fwd config set --project forwarding.ports 3000 8080:3000
fwd config rm --project default_command       # confirms in a terminal
fwd config rm --target runpod default_command # removes only the target override
```

Precedence is **target > project > user > built-in `claude`**. Commands are stored as argv arrays rather than shell
strings, preserving argument boundaries:

```toml
default_command = ["codex"]

[target_defaults.runpod]
default_command = ["python", "-m", "agent"]
```

`fwd up` without an explicit agent or command uses this layered default. `fwd up claude`, `fwd up codex`, and
`fwd up -- <command>` override it for one launch. Use `--user`, `--project`, or `--target NAME` with `fwd default` /
`fwd config set`; omitting all three means `--user`.

`fwd config rm` uses the same scope flags. It reports when the selected scope has no such value and leaves the file
unchanged. Existing values require confirmation in an interactive terminal; scripts and agents must pass `--force`.
Removing an override reveals the next value in the precedence chain rather than copying that value into the file.

### Uninstall

Run `fwd uninstall` to remove `~/.fwd`, the installed Codex/Claude skill, fwd-specific shell completion, and fwd
temporary directories. When `npx` is available, fwd first uses `npx skills remove` so the skills CLI can clean up its
own links and metadata, then removes any known paths it left behind. It then prints the appropriate `uv tool
uninstall`, `pipx uninstall`, or `python -m pip uninstall` command because a running process cannot portably remove
its own environment. It also prints the matching GitHub reinstall command, an `uvx`/`pipx run` one-off command when
available, and the project issues URL.

Uninstall never destroys remote resources. When sessions remain tracked it asks you to run `fwd rm --all` first;
`fwd uninstall --force` removes local state anyway and may leave remote resources running and billing.

### Target shortcuts and zero-config launches

For a human who wants to connect to a saved target and coding environment, use a root selector:

```sh
fwd runpod                          # attach to a matching RunPod session, or create one interactively
fwd runpod yes                      # run a managed task there; it appears in fwd send --ls
fwd ssh                             # attach through the recent SSH target, or create/setup interactively
fwd pod                             # exact configured target; attach a match or create interactively
```

For a background launch without writing config, omit `--reuse` and pass an inferable target:

```sh
fwd up runpod                       # unsaved CPU pod using built-in defaults; run default_command
fwd up --target sid@vm.example.com  # a machine you already have
fwd up --target my-box              # any Host alias in your ~/.ssh/config
```

Configured targets always win — declaring `[targets.runpod]` overrides the built-in rather than competing with it.
Slurm is deliberately **not** inferable: the login host, scratch path and allocation spec are all site-specific, so
`fwd` asks you to run `fwd setup` or crib from `fwd config --example slurm` instead of guessing and failing a minute
into a launch.

### SSH — a machine you already have

```toml
default_target = "box"
default_command = ["claude"]

[targets.box]
backend = "ssh"
host = "gpu.example.com"
user = "sid"
key_path = "~/.ssh/id_ed25519"       # optional; defaults to your ssh config/agent
proxy_jump = "sid@external.example"  # optional; publicly accessible host used to reach a private target
remote_base = "~/fwd"                # projects land in <remote_base>/<project>
```

### RunPod — provision CPU or GPU compute per session

```toml
[targets.pod]
backend = "runpod"
compute_type = "cpu"                 # cpu (default) | gpu
cloud_type = "secure"                # secure | community (community is cheaper)
image = "runpod/base:0.6.2-cpu"
remote_base = "/workspace"           # GPU: persistent volume; CPU: fwd relocates to ephemeral container disk
tool_prefix = "/workspace/.fwd-tools" # same relocation rule as remote_base
allow_proxy = true                   # fall back to ssh.runpod.io if no direct IP

# GPU targets may additionally set:
# compute_type = "gpu"
# gpu = "NVIDIA GeForce RTX 4090"
# image = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# volume_gb = 50
# volume_mount_path = "/workspace"
```

Needs `runpodctl` installed and configured (>= 2.6.0). Three things worth knowing, all learned the hard way
(`docs/runpod-notes.md`):

- **The container disk is wiped on stop; only a GPU pod's persistent volume survives.** On a GPU target,
  `remote_base` and `tool_prefix` therefore belong under `volume_mount_path`. Fwd relocates the selected agent's
  mutable home (`~/.claude` or `~/.codex`) beneath that tool prefix before installation, preserving logins,
  conversations, settings, and Codex's managed app-server payload while recreating the conventional home path as a
  symlink on every full launch.
- **CPU-only pods silently get no persistent volume.** `--volume-in-gb` is folded into the container disk and
  `/workspace` never exists. `fwd` detects this, relocates the project to `/root/fwd/...` on the container disk, and
  warns loudly that everything there is wiped on stop. A restart still reruns the full sync/bootstrap/install/settings
  pipeline, but no implementation can preserve remote-only credentials or conversations without durable storage.
  `volume_gb` is irrelevant and omitted from CPU setup. Use a GPU pod if work must survive `fwd stop`.
- **`cloud_type = "community"` is the cheap option and still works fully.** Community-cloud pods were verified to
  expose a direct `ip:port` for 22/tcp with no extra flags, so rsync stays available.

CPU-only is the default, including for zero-config `fwd up --target runpod` and `fwd setup`. To request a GPU target,
set `compute_type = "gpu"`, choose a `gpu`, set `volume_gb`, and use an appropriate CUDA image such as
`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`. The interactive wizard keeps cloud, volume, remote paths,
and user behind its advanced-options gate.

Pods are reused by name across launches, restarted if stopped, and their IP/port are re-resolved on every attach
(RunPod churns both across restarts). If only the `ssh.runpod.io` proxy is reachable, `fwd` falls back to tar-over-ssh
because that transport cannot run rsync — it warns, and pushes get slower. Tar pushes still mirror synchronized
files: stale files are deleted while excluded remote environments and caches are preserved.

### Slurm — your university or lab cluster

```toml
[targets.hpc]
backend = "slurm"
login_host = "login.hpc.example.edu"
user = "me"
proxy_jump = "me@ext.example.edu"            # omit if the login node is directly reachable
key_path = "~/.ssh/id_ed25519_hpc"
remote_base = "/scratch/me/fwd"              # MUST be scratch, never $HOME
alloc = "--time=04:00:00 --cpus-per-task=8 --mem=32G"
partition = "gpu"
account = "cs-research"
env_setup = [
  "module purge",
  "module load cuda/12.4",
  "module load python/3.12",
]
```

Per-project override in `.fwd/config.toml` — inherits everything above, changes only the allocation:

```toml
[targets.hpc]
alloc = "--time=48:00:00 --cpus-per-task=16 --mem=64G --gres=gpu:a100:1"
```

Notes specific to Slurm (`docs/slurm-notes.md`):

- Sync, bootstrap and dependency installs run on the **login node** — compute nodes usually have no internet, and the
  filesystem is shared with them anyway. tmux also lives on the login node, wrapping `salloc ... srun --pty`, so your
  allocation survives a dropped connection.
- The login hostname is **pinned** on first connect. Round-robin aliases (`login.hpc` → `login1..4`) would otherwise
  land a later `fwd attach` on a node where your tmux session does not exist.
- `remote_base` must be scratch. Caches and venvs are redirected there too, because HPC home directories have inode
  quotas a single `node_modules` can exhaust.
- When your allocation ends, `fwd attach` offers a **new allocation in place** — it does not re-sync or re-bootstrap,
  since the shared filesystem still has everything.

### [your tool here]: Contribute a target!
[Open an issue](https://github.com/Sid-MB/fwd/issues/new) and tag me or write a PR!

### Global options

```toml
default_target = "box"

[claude]
session = true        # move the real transcript (default)
handoff = false       # generate HANDOFF.md instead
user_config = false   # upload ~/.claude bundle
creds = false         # copy credentials to the remote machine

[github]
auth = false          # opt in to local gh authentication transfer and remote Git pushes

[agents.claude]
full_access = true    # VM is the isolation boundary; launch with bypassPermissions
args = []             # extra Claude CLI arguments
environment = {}      # defaults only: an existing remote shell value wins

[agents.codex]
full_access = true    # VM is the isolation boundary; bypass approvals and the Codex sandbox
args = []             # extra Codex CLI arguments
environment = {}      # e.g. { MY_AGENT_TUNING = "1" }

[sync]
exclude = [".venv", "node_modules", "dist"]   # replaces the defaults; see below
use_gitignore = true                          # honour the repo's own .gitignore
delete = true                                 # push mirrors local (removes remote-only files)
max_size_gb = 1.0                             # stop unexpectedly broad uploads while streaming
```

Registered agents run in full-access mode by default because fwd's remote VM or allocation is already the security
boundary. Set `agents.<name>.full_access = false` for a less trusted target. An explicit permission/sandbox option in
`args` takes precedence over the built-in full-access option, and `environment` entries are exported only when the
remote shell has not already defined that name. The same settings are recorded with the session and apply to the
interactive TUI, restarts, and `fwd send agent` turns.

Claude Code's current background-agent and agent-team features are enabled by default; fwd therefore does not set the
obsolete, undocumented `ENABLE_BACKGROUND_TASKS` switch. Likewise, current Codex enables multi-agent support by
default. Use per-agent `args` or `environment` only for a real override rather than pinning defaults that the installed
agent already supplies.

`exclude` is **seeded** with sensible configurable defaults (`.venv`, `node_modules`, `.pnpm-store`, `__pycache__`,
`.next`, `dist`, `build`, `.turbo`, and the various caches) and setting it *replaces* that list rather than adding to
it — so a project that genuinely ships a checked-in `dist/` can shrink the list, not just grow it. Platform metadata
such as `.DS_Store`, AppleDouble `._*`, `Thumbs.db`, and `Desktop.ini` is always excluded and cannot be re-enabled by
configuration. Push includes `.git` because the remote agent needs history, branches, and an index; pull never imports
remote `.git` state into the local checkout. Run `fwd` from a standalone checkout; linked Git worktrees whose `.git`
file points outside the project directory are not currently supported.

For standalone Git repositories, Git itself enumerates tracked files plus untracked, non-ignored WIP with
`git ls-files --cached --others --exclude-standard`; this avoids implementation-specific nested `.gitignore` bugs in
macOS openrsync. Fwd also applies repository ignore rules to tracked paths, so an accidentally committed credential or
generated artifact that still matches `.gitignore` is not synchronized. It then applies `sync.exclude` and
`.fwdignore`, and explicitly retains `.git/` on upload. A self-ignored nested `.gitignore` is retained as a narrow
exception so the remote mirror can preserve ignored remote-only state. Non-Git directories fall back to
transport-native filtering.

During `fwd up` and `fwd push`, `sync.max_size_gb` (1 GB by default) acts as a streaming circuit breaker instead of a
serial full-tree preflight. Both rsync and tar-over-SSH count their compressed outbound wire bytes. Uploads land in a
sibling staging directory, and fwd only applies that stage to the live remote project after
the stream completes under budget. Crossing the limit stops the stream, removes the incomplete stage, prints the exact
project `.fwdignore` path for excluding unintended entries, and provides a project-scoped command such as
`fwd config set --project sync.max_size_gb 4` plus the project/user config paths.

Push, pull, and launch-time upload show the five most recently selected project-relative paths beneath the progress
bar in an interactive terminal. This rolling window is transient and disappears when the transfer finishes, leaving
only the compact completion line in scrollback. Redirected, CI, and agent stderr retains the complete path listing
because no live terminal is available and durable logs are preferable there. Paths never contaminate structured stdout.
Only after a limit failure, fwd reuses the transport filters to list up to ten included files or aggregate folders
larger than 200 MB, making accidental dataset, checkpoint, or build-tree uploads visible without slowing successful
uploads.
In an interactive terminal, the sync step shows an indeterminate progress bar with cumulative MB/GB and live upload
speed; the final line records the transferred amount, average speed, and elapsed time.

## Notes

- **Ctrl-C cleans up what this invocation owns.** If a launch creates a new provider resource and is interrupted before
  startup finishes, fwd removes that new resource, deletes its state entry, and reports how many sessions remain.
  Reused targets are never destroyed by cancellation. If `fwd stop` is interrupted while closing tmux, fwd still
  completes the provider stop before exiting so compute is not left billing.
- **Push mirrors, pull does not.** `fwd push` uses `--delete` so the remote matches local exactly. `fwd pull` is
  additive and path-scoped, because a mirroring pull could delete local work you had not pushed yet. Uploads that
  cross `sync.max_size_gb` are stopped and their remote stage is discarded; downloads are not capped.
- **Destructive and billable actions never happen on a default.** `fwd rm`, including `fwd rm --all`, needs `--force`
  when non-interactive: its prompt defaults to `no`, so a scripted removal safely does nothing. Likewise `fwd attach` will **refuse to restart
  stopped compute** without a terminal — otherwise a cron job attaching to a stopped pod would silently start provisioning
  hardware again. Pass `--restart` (`-y`) to authorize it explicitly:

  ```sh
  fwd attach my-session --restart    # required in CI/scripts; prompts interactively without it
  ```
- **Attach never proxies your terminal.** `fwd` `exec`s into `ssh -t`, replacing itself, so resize, mouse reporting
  and ctrl-C behave exactly as a hand-typed ssh would.
- **Failed launch preparation is recoverable from inside the target.** If tool or dependency preparation fails after
  provisioning and sync but before the primary tmux session starts, run `fwd attach --raw` (or `fwd a --raw`). On a
  running target this creates a plain login-shell tmux in the synced project without rerunning sync, tool resolution,
  dependency installation, project setup, or agent startup. Install or repair what is missing, exit the recovery
  shell so its temporary tmux session closes, then rerun the normal `fwd` launch. `--raw` does not bypass restart
  confirmation for stopped billable compute.
- **State lives in `~/.fwd/state.json`**, locked with `flock` and written atomically. If it is ever lost or corrupted,
  `fwd` degrades to an empty session list rather than failing — your pods and jobs still exist, and `fwd up` will find
  and reuse them by name.

## Development

Measure local command timing without provisioning or SSH using the in-process benchmark suite:

```console
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py
```

It covers every public command's parsing and dispatch plus representative local workloads, and it can save and compare JSON baselines for regression checks. See [docs/benchmarking.md](docs/benchmarking.md).

See [CONTRIBUTING.md](CONTRIBUTING.md).
