
## Things we are looking for
- [ ] New targets (cloud service, HPC, etc.) you want to see
- [ ] Clearer documentation, SKILL.md, and examples
- [ ] New features
- [ ] Bug reports and fixes

[Issues](https://github.com/Sid-MB/fwd/issues) / [PRs](https://github.com/Sid-MB/fwd/pulls)

## Development

```sh
git clone https://github.com/Sid-MB/fwd.git && cd fwd
uv sync
uv run pytest
uv run fwd --help
```

Design notes for the trickier subsystems live in `docs/`: `session-transfer-notes.md` (how transcript relocation was
verified), `runpod-notes.md` (runpodctl behaviour and the volume trap), `slurm-notes.md` (job.sh, login pinning, the
`fwd-env.sh` contract).

CI runs `uv sync --frozen` + `pytest` on 3.12 and 3.13 for every push and PR to `main`
(`.github/workflows/ci.yml`). `--frozen` means a dependency bump must land with its `uv.lock` update.

### Publishing

`.github/workflows/publish.yml` publishes to PyPI over **OIDC trusted publishing** — there is no API token in this
repo and none should be added. It runs when a GitHub release is *published*, or manually via `workflow_dispatch`. The
`build` job runs `uv build` (sdist + wheel, checked to contain `bootstrap.sh`) and uploads the artifact; a separate
`publish` job holds `id-token: write` and the `pypi` environment, and does nothing but download that artifact and
upload it. Splitting them keeps the job that can mint a PyPI credential away from any project code.

One-time setup on pypi.org, under *Publishing* → *Add a new pending publisher*:

| Field | Value |
| --- | --- |
| PyPI project name | must match `project.name` in `pyproject.toml` (currently `fwd`) |
| Owner / repository | `Sid-MB` / `fwd` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Then create a matching `pypi` environment in the repo settings (a required-reviewer rule there gates every upload).

**The published version comes from `version` in `pyproject.toml`, not from the git tag.** PyPI will not overwrite an
existing version, so bump `pyproject.toml` in the same commit you tag — otherwise the `publish` job fails at the upload
step. None of this is wired into the install instructions above: until a release actually happens, the git install is
the real one.
