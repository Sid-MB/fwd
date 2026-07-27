#!/usr/bin/env bash
# End-to-end harness for fwd's ssh plumbing, run against a throwaway sshd container.
#
#   bash tests/harness/docker-sshd/run_integration.sh
#
# What it does: builds the image with a freshly generated throwaway keypair baked into authorized_keys, runs it on
# localhost:2299, writes a temporary ~/.fwd/config.toml with an `ssh` target pointing at it, then runs two drivers:
#
#   checks.py     plumbing — sshexec run/run_script, sync_up/sync_down/tar roundtrips, minimal bootstrap, tmux
#   scenarios.py  the real use case — forwarding an in-progress uv / bun / pnpm project, resolving only each detected
#                 toolchain's requirements, installing from its lockfile, and proving the dependency is importable
#
# Exits 0 with a SKIP message when docker is unavailable, so it is safe to wire into a broader test run; the unit
# tests in tests/test_sync.py and tests/test_remote.py cover the same logic without docker.
#
# Everything is scoped to a temp directory: HOME is redirected so the harness cannot touch the developer's real
# ~/.fwd state, config or ControlMaster sockets (all of those resolve from Path.home() at import time).
#
# Environment knobs:
#   FWD_BOOTSTRAP_MINIMAL=1   Mode used by checks.py only (default 1); scenarios.py always runs the full bootstrap.
#   FWD_SKIP_SCENARIOS=1      Run checks.py only. Much faster and fully offline after the image build.
#   FWD_KEEP=1                Leave the container and temp dir behind for debugging.
#   FWD_SSH_PORT=2299         Host port to publish.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

IMAGE=fwd-test-sshd
CONTAINER=fwd-test-sshd
PORT="${FWD_SSH_PORT:-2299}"
export FWD_BOOTSTRAP_MINIMAL="${FWD_BOOTSTRAP_MINIMAL:-1}"

log() { printf '\n== %s\n' "$*"; }

skip() { printf 'SKIP: %s\n' "$*"; exit 0; }

# Skip rather than fail: docker is optional for this repo's test story, and the unit tests cover the same code paths.
command -v docker >/dev/null 2>&1 || skip "docker not found on PATH; run 'uv run pytest' for the offline coverage"
docker info >/dev/null 2>&1 || skip "docker daemon is not running (start Docker Desktop and retry)"

WORKDIR="$(mktemp -d)"
cleanup() {
    if [ "${FWD_KEEP:-0}" = "1" ]; then
        printf '\nharness: keeping container %s and workdir %s\n' "$CONTAINER" "$WORKDIR"
        return
    fi
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------------------------------------------
# Throwaway key. Generated per run so a leaked harness key is worthless and no developer key ever reaches the image.
# ---------------------------------------------------------------------------------------------------------------
KEY="$WORKDIR/id_harness"
ssh-keygen -q -t ed25519 -N '' -C fwd-harness -f "$KEY"
chmod 600 "$KEY"

log "building $IMAGE"
docker build -q --build-arg PUBKEY="$(cat "$KEY.pub")" -t "$IMAGE" "$HERE" >/dev/null

log "starting $CONTAINER on localhost:$PORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p "$PORT:22" "$IMAGE" >/dev/null

# ---------------------------------------------------------------------------------------------------------------
# Sandboxed HOME + config. UserKnownHostsFile=/dev/null matters: every run generates new host keys on the same
# localhost:PORT, which would otherwise trip host-key mismatch on the second run.
# ---------------------------------------------------------------------------------------------------------------
# uv's cache must stay in the developer's real home, or every run re-downloads the interpreter and dependencies.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
export HOME="$WORKDIR/home"
mkdir -p "$HOME/.fwd"
cat >"$HOME/.fwd/config.toml" <<EOF
default_target = "docker"

[targets.docker]
backend = "ssh"
host = "127.0.0.1"
user = "dev"
port = $PORT
key_path = "$KEY"
remote_base = "~/fwd"
extra_opts = ["-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
EOF

# ---------------------------------------------------------------------------------------------------------------
# Fixture project. Shaped to exercise every filter source at once: a .gitignore rule, a .fwdignore rule, a default
# exclude, a lockfile for dep detection, and a .git dir that must NOT be excluded.
# ---------------------------------------------------------------------------------------------------------------
PROJ="$WORKDIR/project"
mkdir -p "$PROJ/.git" "$PROJ/node_modules" "$PROJ/.fwd" "$PROJ/src"
printf 'print("hello from fwd")\n' >"$PROJ/main.py"
printf 'x\n' >"$PROJ/src/mod.py"
printf 'ref: refs/heads/main\n' >"$PROJ/.git/HEAD"
printf 'junk\n' >"$PROJ/node_modules/junk.js"
printf 'noise\n' >"$PROJ/ignored.log"
printf 'blob\n' >"$PROJ/secret-fixture.bin"
printf '*.log\n' >"$PROJ/.gitignore"
printf 'secret-fixture.bin\n' >"$PROJ/.fwdignore"
printf 'version = 1\n' >"$PROJ/uv.lock"
printf '[project]\nname = "harness"\n' >"$PROJ/pyproject.toml"
printf 'echo setup ran\n' >"$PROJ/.fwd/setup.sh"

log "waiting for sshd"
SSHD_READY=0
for _ in $(seq 1 60); do
    if ssh -i "$KEY" -p "$PORT" \
        -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=2 dev@127.0.0.1 true >/dev/null 2>&1; then
        SSHD_READY=1
        break
    fi
    sleep 1
done
if [ "$SSHD_READY" != "1" ]; then
    # Fail here rather than letting the drivers report a confusing "cannot reach" from deep inside provision().
    printf 'harness error: sshd in %s never accepted the harness key. Container log:\n' "$CONTAINER" >&2
    docker logs "$CONTAINER" 2>&1 | tail -30 >&2
    exit 1
fi

log "running plumbing checks (FWD_BOOTSTRAP_MINIMAL=$FWD_BOOTSTRAP_MINIMAL)"
FWD_HARNESS_WORKDIR="$WORKDIR" uv run --project "$REPO_ROOT" python "$HERE/checks.py"

if [ "${FWD_SKIP_SCENARIOS:-0}" = "1" ]; then
    log "FWD_SKIP_SCENARIOS=1: skipping the uv/bun/pnpm project scenarios"
    exit 0
fi

log "running in-progress project scenarios (uv, bun, pnpm) with the full bootstrap"
FWD_HARNESS_WORKDIR="$WORKDIR" uv run --project "$REPO_ROOT" python "$HERE/scenarios.py"
