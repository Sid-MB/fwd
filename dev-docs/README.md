# fwd developer documentation

This folder contains architecture, extension, validation, and implementation notes for contributors working on fwd.

## Extend fwd

- [Adding a target backend](adding-target-backends.md): backend contract, configuration schema, lifecycle safety, registration, and verification.
- [Adding a project toolchain](adding-toolchains.md): detection, requirements, installation, registration, and testing.

## Understand provider implementations

- [RunPod notes](runpod-notes.md): observed `runpodctl` behavior, endpoint churn, persistence, and fixtures.
- [Lambda Cloud notes](lambda-notes.md): API, credential handling, deterministic ownership, storage, and lifecycle.
- [Slurm notes](slurm-notes.md): login-node tmux, allocation scripts, path guards, and shared scratch.

## Validation and performance

- [Live end-to-end report](live-e2e-report.md): dated RunPod validation evidence and discovered regressions.
- [Session transfer notes](session-transfer-notes.md): Claude transcript relocation experiment and encoding rules.
- [Performance benchmarking](benchmarking.md): in-process command benchmarks and baseline comparison.

## Repository map

- `src/fwd/backends/`: provider lifecycle implementations.
- `src/fwd/toolchains/` and `src/fwd/tooling.py`: project detection and remote requirements.
- `src/fwd/ops/`: launch, attach, synchronization, lifecycle, and task orchestration.
- `src/fwd/agents/`: Claude and Codex transfer/runtime integrations.
- `tests/`: offline unit and integration tests; provider fixtures are under `tests/fixtures/`.
- `references/`: compact references packaged with the coding-agent skill, not the end-user documentation site.

## Development workflow

```sh
uv sync
uv run pytest
uv run fwd --help
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution and publishing policy. User-facing behavior belongs in [docs](../docs/README.md), and concise project orientation belongs in the [top-level README](../README.md).
