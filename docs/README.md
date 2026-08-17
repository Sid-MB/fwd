# fwd user guide

This folder contains the user documentation for `fwd`. Start with the guide that matches what you want to do:

- [Getting started](getting-started.md): install fwd, create a target, and launch your first remote session.
- [Commands and lifecycle](commands.md): launch, attach, run durable tasks, synchronize files, forward ports, stop, and remove sessions.
- [Configuration and backends](configuration.md): layered configuration, SSH, RunPod, Lambda Cloud, Slurm, defaults, and project setup.
- [Coding agents](agents.md): move Claude Code or Codex work, send follow-up turns, manage credentials, and install the fwd skill.
- [Troubleshooting](troubleshooting.md): diagnose failures, recover partial launches, and avoid data loss.

The installed CLI is the authoritative option reference:

```sh
fwd --help
fwd COMMAND --help
fwd config --example
fwd config --schema
```

The first invocation of an installed `fwd` silently synchronizes its bundled manuals to the user-local XDG man directory; package upgrades update them on the next use. Run `man fwd` or `man fwd-up` afterward. In a source checkout, preview a page directly with `man ./man/fwd.1`.

Contributing to fwd? See the [developer documentation](../dev-docs/README.md).
