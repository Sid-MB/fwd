# Performance benchmarking

`benchmarks/benchmark_commands.py` measures fwd in-process, excluding shell lookup and Python executable startup. It has two complementary groups:

- `command` invokes every public command and compatibility alias through Typer while replacing provider, SSH, transfer, deletion, installation, and interactive boundaries with deterministic fakes. This catches parsing, import, and local dispatch regressions without spending money or changing user state.
- `workload` times substantive local paths that command-boundary fakes would hide: listing and decoding 100 sessions, loading and rendering config, and constructing a Git upload manifest with nested ignored content.

Run the complete suite:

```console
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py
```

Limit a local investigation:

```console
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py --filter ls
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py --group workload
```

Save a baseline on the same machine, checkout, Python version, and power mode used for the later comparison:

```console
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py --save .benchmarks/main.json
UV_CACHE_DIR=.uv-cache uv run python benchmarks/benchmark_commands.py --compare .benchmarks/main.json
```

Comparison fails when a median is both more than 20% and more than 0.10 ms slower. Adjust `--max-regression` or `--min-regression-ms` for a noisier machine. The two-threshold rule avoids treating a sub-millisecond timer fluctuation as a meaningful percentage regression.

Benchmark artifacts are machine-specific and should normally remain uncommitted. For reliable review measurements, close high-load applications, run enough samples, and compare commits without changing the environment.
