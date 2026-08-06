# Lambda Cloud backend — API and lifecycle notes

This document records the contract implemented by `src/fwd/backends/lambda_cloud.py` against the Lambda Cloud API
documented on **2026-08-05**. It is an implementation reference, not evidence of a live billable end-to-end run. Before
advertising a release as live-verified, exercise one explicitly named instance/filesystem through create, SSH, sync,
stop, recreate, and remove while watching the Lambda console.

## Prerequisites and authentication

Create an API key and SSH key in the Lambda Cloud console. Export the API key locally as `LAMBDA_API_KEY`; configure
the registered public key's name as `ssh_key_name` and, when SSH cannot select its matching private key through the
agent or OpenSSH config, set `key_path`.

Lambda API keys grant broad account access. Fwd sends the key only as `Authorization: Bearer ...` in HTTPS request
headers. It never writes the key to TOML, session state, subprocess arguments, logs, or the remote instance. This is
also why Lambda deliberately does not implement `remote_stop_command`: `fwd up/send --stop-after` fails before work
starts instead of copying a full-account credential onto an instance. Local `fwd stop` is supported.

There is no Lambda provider CLI dependency. The backend uses the standard-library HTTP and JSON clients so unrelated
fwd commands and other backends do not acquire an SDK dependency.

## Configuration

The smallest useful target is:

```toml
[targets.lambda-gpu]
backend = "lambda"
region = "us-west-1"
instance_type = "gpu_1x_a10"
ssh_key_name = "my-public-key"
```

Use `fwd setup --backend lambda` to query current regions, instance types with capacity in the selected region,
registered SSH keys, and images. Inventory discovery is best-effort and free text remains accepted because capacity
and provider catalogs change independently of fwd releases. `fwd doctor --target lambda-gpu` verifies the API key,
required fields, current regional capacity, and SSH key registration without creating billable resources.

Defaults place durable state at:

- filesystem mount: `/home/ubuntu/fwd-data`
- project checkouts: `/home/ubuntu/fwd-data/projects/<project>`
- tools, agent state, credentials, task metadata, and caches: `/home/ubuntu/fwd-data/.fwd-tools`

When `persistent = true`, config validation requires `remote_base` and `tool_prefix` to remain at or below
`filesystem_mount_path`. This prevents a seemingly valid configuration from silently placing important state on the
instance's disposable root disk.

## API surface

All requests use `https://cloud.lambda.ai/api/v1` and unwrap Lambda's `{ "data": ... }` response envelope. The backend
uses:

| Operation | Endpoint | Purpose |
|---|---|---|
| List regions | `GET /regions` | Setup discovery |
| List instance types | `GET /instance-types` | Setup discovery, capacity, doctor |
| List images | `GET /images` | Optional image discovery |
| List SSH keys | `GET /ssh-keys` | Setup discovery and doctor |
| List instances | `GET /instances` | Idempotent name lookup before launch |
| Get instance | `GET /instances/{id}` | Status, endpoint resolution, readiness, termination wait |
| Launch | `POST /instance-operations/launch` | Create one replacement instance |
| Terminate | `POST /instance-operations/terminate` | Stop billing compute |
| List filesystems | `GET /file-systems` | Deterministic storage lookup and detach polling |
| Create filesystem | `POST /filesystems` | Create session-owned durable storage |
| Delete filesystem | `DELETE /filesystems/{id}` | Destructive `fwd rm` cleanup |

Lambda documents a general limit of one API request per second and a launch limit of one request per 12 seconds (five
per minute). A process-wide request-start gate spaces all calls by at least 1.05 seconds, including concurrent status
checks from `fwd ls`. Provision performs at most one launch after deterministic resource reconciliation, and readiness
polls every four seconds.

## Deterministic ownership and retry safety

The instance name is derived from `fwd-<session>` and the filesystem from `fwd-<session>-data`. Names are sanitized to
provider grammar and length limits; any changed or truncated value carries an eight-character SHA-256 suffix so create
and lookup cannot disagree or collide merely because punctuation was removed.

Provision lists existing nonterminal instances by the exact deterministic name before launching. One match is adopted;
multiple live matches are rejected with their IDs so fwd never guesses which billable instance it owns. Persistent
filesystems are similarly looked up by exact name and configured region. A same-name filesystem in another region is
an error rather than an invitation to create a second one that hides the configuration mistake.

After launch returns an instance ID, fwd polls the instance document until status is `active`, a public `ip` exists,
and TCP port 22 accepts connections. Only then does the shared launch pipeline perform normal SSH readiness, rsync,
bootstrap, and tmux setup.

## Normalized lifecycle

| Lambda state | `TargetStatus` | Meaning in fwd |
|---|---|---|
| `active` | `RUNNING` | Resolve current public IP and connect |
| `booting` | `PENDING` | Instance is still provisioning |
| `terminating` | `PENDING` | Provider teardown has not completed |
| `terminated`, `preempted` | `STOPPED` | Recreate compute before attaching |
| Definitive missing instance plus surviving recorded filesystem | `STOPPED` | Compute is gone, durable session remains restartable |
| Definitive missing instance and no surviving recorded filesystem | `GONE` | Only stale local tracking remains |
| `unhealthy`, API/auth/network/JSON error | `UNKNOWN` | Never authorize stale-state deletion from an inconclusive query |

`endpoint(session)` always retrieves the instance by stable ID and rebuilds the direct `ubuntu@<public-ip>:22`
endpoint. It never starts or creates compute. Lambda addresses can change on replacement, so the shared attach path
updates cached endpoint state and existing SSH port-forward fingerprints prevent stale tunnels from being reused.

### `fwd stop`

1. Shared lifecycle code checks the remote Git worktree and closes tmux/tasks/port forwards.
2. The backend calls Lambda's terminate operation for the recorded instance.
3. The session entry and its filesystem ID remain local.
4. Filesystem billing continues while the filesystem exists; instance billing ends according to Lambda's termination
   lifecycle.

The next `fwd up` or confirmed `fwd attach --restart` finds the deterministic filesystem, launches a new instance with
it mounted, receives a new instance ID/IP, and runs the full repair/sync/bootstrap pipeline.

### `fwd rm`

After shared worktree protection and consequence-aware confirmation, the backend terminates the recorded instance,
waits until it is terminal or definitively absent, waits until Lambda reports the filesystem is no longer in use, and
deletes only the recorded filesystem ID. Repeated termination/deletion calls tolerate definitive absence. Provider,
authentication, timeout, and parse failures remain errors and are never treated as successful deletion.

### Interrupted provisioning

The backend records invocation ownership immediately after filesystem or instance creation. Ctrl-C cleanup terminates
only the instance created by that backend object, waits for detachment, and deletes only the filesystem created by the
same invocation. Resources adopted by deterministic lookup are never eligible for interrupted-launch cleanup.

## Persistence and disposable mode

Lambda states that instance-local, non-filesystem data is destroyed on termination and that filesystems must be
attached at launch. Persistent mode is therefore the default and is the only mode that satisfies restart continuity.
The filesystem holds the checkout, `.git`, installed language/agent tooling, relocated agent homes, GitHub credentials,
task logs, and fwd state needed by the remote session.

`persistent = false` is accepted only as an explicit choice. Fwd still uses paths under `/home/ubuntu/fwd-data`, but
they are ordinary instance-local directories and disappear at termination. Every provision warns that stop will erase
the checkout, tools, credentials, conversations, and agent state. Shared dirty-worktree protection remains a final
guard, not a persistence substitute for ignored files, non-Git data, or unpushed commits.

## Current limitations

- No remote `--stop-after`, because Lambda does not expose a pod-scoped/self identity and API keys are too broad to copy.
- No automatic fallback to another region or instance type; changing requested hardware without authorization could
  change price, architecture, GPU count, memory, and workload behavior.
- No automatic SSH-key creation or private-key storage. Key ownership remains in Lambda and the user's local SSH setup.
- No live provider fixture or billable smoke result is claimed by this implementation change. The standard backend
  boundary, generated config/schema, import checks, and existing provider-independent suite can be validated locally;
  a real lifecycle run requires an explicitly authorized Lambda account and spend.
