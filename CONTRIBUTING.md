
## Things we are looking for
- [ ] New targets (cloud service, HPC, etc.) you want to see
- [ ] Clearer [user documentation](./docs/README.md), [developer documentation](./dev-docs/README.md), [README.md](./README.md), [SKILL.md](./SKILL.md), and examples
- [ ] Support other server-running coding agents (beyond Claude Code and Codex)
- [ ] New features
- [ ] Bug reports and fixes

[Issues](https://github.com/Sid-MB/fwd/issues) / [PRs](https://github.com/Sid-MB/fwd/pulls)

## Development

```sh
git clone https://github.com/Sid-MB/fwd.git && cd fwd
uv sync
# point the global 'fwd' command at this checkout
uv tool install --editable --force .  
fwd --help
uv run pytest
uv run python tools/generate_man_pages.py --check
```

The editable install makes source edits take effect on the next `fwd` invocation from any directory; rerun it after changing `pyproject.toml`, and note that `fwd --version` and the packaged man pages stay frozen at install time.

Design notes for the trickier subsystems live in [`dev-docs/`](./dev-docs/README.md), including transcript relocation, provider behavior, lifecycle contracts, and live validation evidence. End-user workflows live in [`docs/`](./docs/README.md).

The checked-in section-1 manuals are generated from the CLI with `click-man`. After changing commands, options, or their help text, run `uv run python tools/generate_man_pages.py`; see [`dev-docs/man-pages.md`](./dev-docs/man-pages.md) for the authored-section, rendering, validation, and packaging contract. CI and publishing both reject stale or invalid pages.

### Publishing

`.github/workflows/publish.yml` publishes to PyPI over **OIDC trusted publishing** — there is no API token in this
repo and none should be added. A push to `main` that changes `src/**` or `pyproject.toml`, or a manual
`workflow_dispatch`, calls the shared Interlens workflow to compute the next patch tag, build and attest the sdist and
wheel, and push the tag. The repository-local `publish` job verifies the wheel and uploads it through the protected
`pypi` environment; a separate least-privilege job creates the GitHub Release only after PyPI accepts the distributions.

One-time setup on pypi.org, under *Publishing* → *Add a new pending publisher*:

| Field | Value |
| --- | --- |
| PyPI project name | `fwdit` |
| Owner / repository | `Sid-MB` / `fwd` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Then create a matching `pypi` environment in the repo settings (a required-reviewer rule there gates every upload).
The PyPI distribution is named `fwdit` because the unrelated `fwd` project is already owned on PyPI; users still run
the `fwd` command and import the `fwd` package.

The published version comes from the `vX.Y.Z` tag created by the shared workflow through `hatch-vcs`. Do not restore a
static `project.version`: doing so would let the Git tag and immutable PyPI artifact version diverge again.

### CI Testing
CI runs `uv sync --frozen` + `pytest` on 3.12 and 3.13 for every push and PR to `main`
(`.github/workflows/ci.yml`). `--frozen` means a dependency bump must land with its `uv.lock` update.

