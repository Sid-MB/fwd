# RunPod backend — ground truth notes

Everything below was measured against a live RunPod account on **2026-07-26/27** with `runpodctl 2.6.0-5516265`
(macOS, `/opt/homebrew/bin/runpodctl`). Raw command outputs are in `tests/fixtures/runpod/`; the JSON files double as
unit-test fixtures for `tests/test_runpod_parse.py`. All SSH public keys have been redacted; the API key was never
read into fwd or into any captured file.

## Summary of the S2 spike

| Question | Answer |
| --- | --- |
| Is a **direct `ip:port`** for 22/tcp available? | **Yes**, on both secure-cloud CPU pods and community-cloud GPU pods, with no `--public-ip` flag needed. `supports_rsync=True` in every case observed. |
| Does real `ssh` (BatchMode) work? | Yes, with the account's registered `~/.ssh/id_ed25519`. |
| Does `rsync` work over it? | Yes — 200 KB roundtrip up and down was byte-identical. `runpod/base:0.6.2-*` ships `rsync`, `tmux`, `git`, `curl` already. |
| Does the endpoint change across `pod stop` → `pod start`? | **Yes.** The published host port is reassigned every time (`18876 → 10992`, `40708 → 40668`). The public IP happened to stay the same in both trials, but the port alone is enough to break a cached endpoint. `endpoint()` must always re-resolve. |
| Does `/workspace` (the volume) persist? | **Yes** — `/workspace/VOLUME_MARKER.txt` and `/workspace/.fwd-tools/uv-fake` both survived a full stop/start. |
| Does the container disk persist? | **No** — `/root/CONTAINER_MARKER.txt` and an rsync'd `/root/fwdtest/` were both gone after restart. This is exactly the plan's assumption, now confirmed. |

## runpodctl 2.6.0 grammar and output

The CLI is **noun-first** (`runpodctl pod create`, `pod list`, `pod get`, `pod start`, `pod stop`, `pod delete`) and
the legacy verb-first forms (`create pod`, `get pod`, …) are marked *deprecated* in `runpodctl --help`.

**Output is JSON by default** for every one of these subcommands (`-o string  output format (json, yaml) (default "json")`).
There is no table parsing anywhere in the backend.

### Syntax-drift handling

`RunpodBackend._require_supported_cli()` probes `runpodctl pod create --help` **once per process** (memoized in the
module-level `_SYNTAX_OK`) and requires the `--ports` flag to be present in the help text. Capability probing beats
version-string parsing because it survives RunPod renumbering releases.

The legacy verb-first codepath is **not** implemented. It is not "trivially cheap": it emits tables instead of JSON
and, critically, exposes no `ssh` block, so it carries strictly less information than the modern grammar and would
require a whole second parser. **Minimum supported version: `runpodctl >= 2.6.0`.** Anything older fails fast with an
actionable message pointing at `runpodctl update`.

### `pod get` already carries the SSH endpoint — no `ssh info`, no REST API

This is the single most useful discovery. `runpodctl pod get <id>` returns:

```json
{
  "id": "nlom3h0kpps2y8",
  "name": "fwd-test-spike",
  "desiredStatus": "RUNNING",
  "ports": ["22/tcp"],
  "volumeInGb": 0,
  "volumeMountPath": "/workspace",
  "ssh": {
    "ip": "216.243.220.199",
    "port": 18876,
    "ssh_command": "ssh -i /Users/sid/.runpod/ssh/runpodctl-ssh-key root@216.243.220.199 -p 18876",
    "ssh_key": { "exists": true, "in_account": true, "fingerprint": "SHA256:..." }
  }
}
```

While the pod is booting (or is stopped) the same block reads `{"error": "pod not ready", "status": "EXITED"}` with
no `ip`/`port`. So **one call is both the readiness poll and the endpoint resolution**. `runpodctl ssh info <id>`
returns the identical block un-nested; the backend's `parse_ssh_info` accepts both shapes but the backend itself only
ever calls `pod get`.

**REST API decision: not used.** `GET https://rest.runpod.io/v1/pods/<id>` was tested and returns the same
information under different key names (`publicIp`, `portMappings: {"22": 10992}`). Since runpodctl already gives us
everything in a friendlier shape, using REST would add an HTTP dependency and — worse — force fwd to read the API key
out of `~/.runpod/config.toml` into its own memory, where it could reach a log line or traceback. fwd therefore
**never reads the key's value**; `doctor()` only checks for the *presence* of `$RUNPOD_API_KEY` or the config file.

### Other observed behaviours

- `pod list` **hides EXITED pods by default**. The backend always passes `-a`, otherwise a stopped fwd pod looks
  `GONE` and the next launch silently creates and bills a duplicate.
- `pod list` entries are summaries only (`id`, `name`, `desiredStatus`, `imageName`, `costPerHr`, `gpuCount`,
  `volumeInGb`) — no ssh block. Reuse lookup is list-by-name, then `pod get` for details.
- `pod list --name <x>` exists as a server-side filter, but the backend filters client-side on an exact match so a
  substring behaviour change upstream cannot make fwd adopt the wrong pod.
- `pod create`, `pod start` and `pod stop` all return the **same single-pod document** as `pod get`, so one parser
  (`parse_pod`) covers all four.
- `desiredStatus` is RunPod's *intent*, not liveness: it reads `RUNNING` the instant the pod is rented, ~30–60 s
  before sshd answers. `pod_status()` therefore downgrades `RUNNING` to `PENDING` until the `ssh` block has an
  address. Ready-time observed: ~5 s (secure CPU) to ~30 s (community GPU) for the address, plus sshd startup.
- **`pod get` replays a stale ssh block right after `pod start`.** For roughly the first 20–30 s after a restart,
  `pod get` keeps serving the *pre-stop* `ip:port`. This bit the first live e2e run: `provision()` returned in 2.3 s
  with the old address, and the caller's `wait_for_ssh` then burned its full 300 s timeout on a port that no longer
  existed (it had churned `20777 → 20695`). Fixed by `port_is_open()` — `_wait_for_pod` now requires a successful
  **TCP connect** to the reported address before accepting it. The retried run waited 26.4 s, returned the correct
  new port, and passed. This is why readiness cannot be inferred from `desiredStatus` *or* from the mere presence of
  an ssh block.
- Errors: `pod get <bad-id>` exits nonzero and prints an error object, **then cobra usage text, then the error
  again**. `_first_json()` scans for the first parseable JSON value rather than assuming the stream is one document.
  A missing pod is detected via `not found` / `404` in the message and mapped to `GONE`, distinct from a real outage.
- RunPod does not enforce unique pod names. `find_pod_by_name` prefers a `RUNNING` duplicate so a crashed launch's
  stale twin is never adopted over the live machine.
- No pod in any trial fell back to the `ssh.runpod.io` proxy, so no genuine proxy response could be captured. The
  proxy-fallback parsing path is pinned by a clearly-labelled **synthetic** fixture
  (`tests/fixtures/runpod/ssh-info-proxy.json`) matching the documented `<pod>-<token>@ssh.runpod.io` shape. The
  token is opaque and not derivable from the pod id, so it can only be lifted out of `ssh_command`.

## `compute_type` and `cloud_type`

Added after the full-stack live e2e (`docs/live-e2e-report.md`) found that neither was exposed, which made CPU pods
and community-cloud pods unreachable from the CLI and left a **secure-cloud GPU as the cheapest launchable target** —
the most expensive of the three options.

`RunpodTargetConfig` now carries `compute_type` (`gpu`|`cpu`, default `gpu`) and `cloud_type`
(`secure`|`community`, default `secure`). Both are normalized to lower case and validated in `__post_init__`, so a
typo raises `ConfigError` at config-load time rather than surfacing as an opaque `runpodctl` scheduling failure two
minutes into a launch. The backend upper-cases them for the CLI, which documents `GPU|CPU` and `SECURE|COMMUNITY`.

Flag construction lives in the pure `create_pod_args()` so the whole matrix is unit-testable without touching the
network. The one non-obvious rule: **CPU pods get no GPU flags at all** — `--gpu-id` is omitted entirely, and an
explicit `--gpu` override is ignored rather than passed through, because `--gpu-id` on a CPU pod produces an opaque
scheduling failure. One test asserts that every `--flag` the backend emits actually appears in the captured
`pod-create-help.txt`, so inventing a flag that RunPod does not accept fails locally.

## CPU pods do not get a persistent volume

Worth knowing before anyone configures a cheap CPU target:

```
runpodctl pod create --compute-type cpu --image runpod/base:0.6.2-cpu --ports 22/tcp \
    --volume-in-gb 20 --volume-mount-path /workspace
→ "volumeInGb": 0, "containerDiskInGb": 20
```

The `--volume-in-gb` value was silently folded into the **container disk** and `volumeInGb` stayed `0`. On the pod,
`/workspace` did not exist at all; creating it just wrote to the overlay, and everything vanished on the next stop.
The identical command with `--cloud-type COMMUNITY --gpu-id "NVIDIA RTX A4000"` returned `"volumeInGb": 20` and
`/workspace` was a real separate device (`/dev/nvme0n1`).

Since fwd's whole persistence model rests on `/workspace`, `provision()` checks the **created pod's** `volumeInGb`
and, when it is zero, `resolve_paths()` relocates `remote_dir`, `tool_prefix` and `scratch` from the missing volume
onto the container disk under `/root/fwd/`, appending a loud note to `TargetInfo.notes`:

> pod has no persistent volume — /workspace is not backed by one on this pod, so anything written there would be
> WIPED on stop; using the container disk at /root/fwd/workspace instead (CPU pods silently ignore volume_gb; use a
> GPU pod to persist)

Relocating matters as much as warning, and the precise reason is worth stating: on a CPU pod `/workspace` is usually
*still there* as an ordinary writable directory on the container-disk overlay. That is what makes it dangerous —
writing to it succeeds and then silently loses everything on the next stop. An earlier version of this note claimed
the path "does not exist", which the Round-2 live run flagged as false (R2-3): a user who checks and finds the
directory sitting there would reasonably conclude fwd was confused. The check keys on what the pod *reports* rather
than on `compute_type` alone, which also catches a GPU pod whose volume request was rejected for capacity. Paths the
user has already pointed outside `volume_mount_path` are left untouched — they never relied on the volume.

The `ui.step` label for pod creation is likewise derived from the real `create_pod_args` argv rather than from the
config (`create_summary`), after the live run saw a CPU pod announce itself as
`(NVIDIA GeForce RTX 4090, 20 GB volume)` — a GPU never requested and a volume RunPod ignores (R2-4).

## Implementation notes

- All provider calls funnel through `RunpodBackend._run_ctl`, which normalizes three failure modes into `RunpodError`:
  binary absent, nonzero exit, and *zero exit with an error document* (runpodctl does the last one often).
- Reuse is keyed on the **pod name** (`fwd-<session>`), not on stored state, so a lost or pruned `state.json` can
  never orphan a pod the user is still paying for — the next launch finds it by name and adopts it.
- `provision()` sets `remote_dir = <remote_base>/<project>` and `tool_prefix = cfg.tool_prefix`, both under
  `volume_mount_path` by config default, plus `scratch = <remote_base>/.fwd-cache`. All three survive a restart.
- `stop()`/`destroy()` pass `check=False`: an already-stopped or already-deleted pod is the caller's desired end
  state, not an error.
- `status()` never raises, so one bad row cannot blank the whole `fwd ls` table — but it is careful about *which*
  failure means what. Only a **confirmed 404** becomes `GONE`; every other failure becomes `TargetStatus.UNKNOWN`
  plus a warning carrying the provider's own error text. This is not pedantry: `GONE` is the value that unlocks
  `attach`'s offer to **delete the user's session entry**, and the Round-2 live run caught a transient `runpodctl`
  error one second after a successful `fwd stop` presenting a healthy, *billing* pod as gone (R2-1). Collapsing
  "cannot ask" into "does not exist" is how a paid-for pod gets orphaned with no state entry pointing at it.
- `endpoint()` refuses on a stopped pod rather than returning the stale cached address, so `attach` gets an
  actionable "run `fwd up` to restart it" instead of a connection timeout.

## Live end-to-end result

`RunpodBackend` was driven directly (bypassing ops/cli, which other teammates were still building) against a real
community-cloud A4000 pod named `fwd-test-e2e`, using teammate B's `SSHEndpoint.run`/`wait_for_ssh`. Full pass:

```
=== doctor
  [ok] runpodctl: runpodctl 2.6.0-5516265 (/opt/homebrew/bin/runpodctl)
  [ok] runpodctl syntax: noun-first ('pod create') with JSON output
  [ok] runpod api key: found /Users/sid/.runpod/config.toml
  [ok] runpod api: 32 pod(s) visible
=== provision            (reuse path — pod already existed)
  ✓ Looking up RunPod pod fwd-test-e2e 1.0s
  ✓ Waiting for pod fwd-test-e2e to expose ssh 2.5s
  endpoint=SSHEndpoint(host='157.157.221.29', port=20777, supports_rsync=True)
  remote_dir=/workspace/fwd tool_prefix=/workspace/.fwd-tools scratch=/workspace/.fwd-cache
  backend_ids={'pod_id': 'vdjavga1myohnx', 'pod_name': 'fwd-test-e2e'}
  note: reusing existing pod fwd-test-e2e
=== wait_for_ssh + SSHEndpoint.run    -> Linux x86_64, /workspace mounted
=== status while running -> running
=== stop                 -> stopped
=== endpoint() on a stopped pod
  correctly refused: pod for session 'test-e2e' is stopped; run 'fwd up' to restart it
=== re-provision (restart path)
  ✓ Starting stopped pod fwd-test-e2e 1.3s
  ✓ Waiting for pod fwd-test-e2e to expose ssh 26.4s
  old=157.157.221.29:20777  new=157.157.221.29:20695   -> endpoint CHANGED
  note: pod was restarted — the container disk was wiped, only the volume survived
=== endpoint() re-resolution matches -> 157.157.221.29:20695
=== volume survived the restart      -> marker file read back from /workspace/.fwd-tools
=== destroy              -> status after destroy=gone
=== E2E PASSED
```

Note the first attempt of this run **failed**, and usefully so: it exposed the stale-`ssh`-block race documented
above. The TCP probe was added in response and the retry passed.

Also observed: this community A4000 pod's `/workspace` was a **network volume**
(`mfs#eur-is-1.runpod.net:9421`, 1.6 P) rather than a local NVMe like the earlier pod. Both persist across
stop/start; the network-backed one will be slower for inode-heavy work such as `uv sync`, which is worth remembering
if tooling installs ever feel sluggish.

Cost of the entire spike plus e2e: a handful of cents (CPU pod $0.06/hr, community A4000 $0.17–0.25/hr, minutes of
runtime). All `fwd-test-*` pods were deleted afterwards and the account verified clean.

## Reproducing

The spike and e2e scripts are throwaway and live in the session scratchpad, not the repo. To redo the e2e, construct
a `RunpodTargetConfig` with a community GPU (`gpu="NVIDIA RTX A4000"`, `volume_gb=20`) and drive
`provision → stop → status → provision → endpoint → destroy` directly; a CPU pod will *not* exercise the volume path.
