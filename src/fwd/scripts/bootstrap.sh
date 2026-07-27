#!/usr/bin/env bash
# fwd remote bootstrap.
#
# CONTRACT
# --------
# Invocation: piped to a remote shell by fwd.remote.run_bootstrap, i.e. `ssh <host> 'bash -s' < bootstrap.sh`.
# The script is never copied to the remote first, so it can never drift from the installed fwd version.
#
# Environment variables received (all absolute paths, all exported by run_bootstrap):
#   FWD_TOOL_PREFIX  Root for every tool this script installs (binaries under $FWD_TOOL_PREFIX/bin).
#                    MUST be treated as the only writable install location. Backends point it at persistent storage:
#                    /workspace on RunPod (container disk is wiped on pod stop) and scratch on Slurm (home inode
#                    quotas are too small). Installing anywhere else means re-downloading the toolchain every restart.
#   FWD_REMOTE_DIR   The synced project directory. Do NOT install into it; it is mirrored from local and `--delete`
#                    on the next push would erase anything this script put there.
#   FWD_SCRATCH      Cache/temp root. Export the tool caches here (UV_CACHE_DIR, BUN_INSTALL_CACHE_DIR, npm_config_cache).
#
# Optional:
#   FWD_BOOTSTRAP_MINIMAL=1  Skip every network install and only lay down the directory layout, fwd-env.sh and the
#                            version marker. Used by the docker integration harness so a full end-to-end run does not
#                            depend on downloading uv/bun/claude every time.
#
# Design notes
# ------------
# Idempotence is achieved on two levels. The coarse level is a version-stamped marker file: a second launch against an
# already-provisioned machine short-circuits in milliseconds. The fine level is a `command -v` guard per tool, so a
# version bump (which invalidates the marker) re-runs the script without redownloading tools that are already there.
# Everything installs user-space. The single optional root path is apt-get for tmux, because tmux is the one hard
# requirement we cannot install from a userland tarball, and containers commonly ship without it.

set -euo pipefail

# Bump when the layout or the contents of fwd-env.sh change; stale markers from other versions force a full re-run.
FWD_BOOTSTRAP_VERSION=1

: "${FWD_TOOL_PREFIX:?bootstrap requires FWD_TOOL_PREFIX}"
: "${FWD_REMOTE_DIR:?bootstrap requires FWD_REMOTE_DIR}"
FWD_SCRATCH="${FWD_SCRATCH:-$FWD_TOOL_PREFIX/scratch}"
FWD_BOOTSTRAP_MINIMAL="${FWD_BOOTSTRAP_MINIMAL:-0}"

BIN_DIR="$FWD_TOOL_PREFIX/bin"
BUN_ROOT="$FWD_TOOL_PREFIX/bun"
NPM_ROOT="$FWD_TOOL_PREFIX/npm"
CLAUDE_ROOT="$FWD_TOOL_PREFIX/claude"
ENV_FILE="$FWD_TOOL_PREFIX/fwd-env.sh"
# Fixed-location pointer sourced by fwd.remote._source_env. A remote `ssh host 'cmd'` runs a non-interactive,
# non-login shell that reads neither .bashrc nor .profile, and it has no idea what FWD_TOOL_PREFIX is — so the
# absolute prefix has to be baked into a path that never moves.
HOME_ENV_FILE="$HOME/.fwd-env.sh"
MARKER="$FWD_TOOL_PREFIX/.fwd-bootstrap-$FWD_BOOTSTRAP_VERSION"

log() { printf 'fwd: %s\n' "$*"; }
warn() { printf 'fwd: warning: %s\n' "$*" >&2; }
fail() { printf 'fwd: error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------------------------------------------
# Layout. Created before the marker check so a machine whose scratch was wiped still gets its directories back.
# ---------------------------------------------------------------------------------------------------------------
mkdir -p "$BIN_DIR" "$FWD_SCRATCH" "$FWD_REMOTE_DIR"
# Every later step resolves tools through PATH, so put our bin dir in front immediately.
export PATH="$BIN_DIR:$BUN_ROOT/bin:$NPM_ROOT/bin:$CLAUDE_ROOT/bin:$HOME/.local/bin:$PATH"

# Tools this script is responsible for and that a launch cannot proceed without. tmux is excluded: it may legitimately
# come from the system package manager and live outside the prefix.
REQUIRED_TOOLS="uv claude"

bootstrap_is_valid() {
    # A marker alone is not proof of a working install. The marker lives under $FWD_TOOL_PREFIX, which backends point
    # at persistent storage, while the payload can sit on ephemeral container disk — on RunPod a pod stop wipes $HOME
    # and leaves the marker behind, so the coarse check would short-circuit forever on a machine with no claude at
    # all, and fwd would report a successful launch over a tmux session that dies instantly. Verify the binaries
    # actually execute before trusting the stamp.
    [ -f "$MARKER" ] || return 1
    [ -f "$HOME_ENV_FILE" ] || return 1
    # MINIMAL mode installs nothing, so there is no payload to verify.
    if [ "$FWD_BOOTSTRAP_MINIMAL" = "1" ]; then
        return 0
    fi
    local tool
    for tool in $REQUIRED_TOOLS; do
        have "$tool" || { warn "marker present but '$tool' is missing; re-running bootstrap"; return 1; }
        "$tool" --version >/dev/null 2>&1 || { warn "marker present but '$tool' does not run; re-running bootstrap"; return 1; }
    done
    return 0
}

if bootstrap_is_valid; then
    log "bootstrap $FWD_BOOTSTRAP_VERSION already applied at $FWD_TOOL_PREFIX (marker present), skipping"
    exit 0
fi
# A marker from a different version means the layout changed; drop it so we never leave two conflicting stamps.
rm -f "$FWD_TOOL_PREFIX"/.fwd-bootstrap-* 2>/dev/null || true

# ---------------------------------------------------------------------------------------------------------------
# fwd-env.sh — the single file every later step (dep installs, tmux sessions, the user's own shell) sources.
# Written before the installs so a partially failed bootstrap still yields a usable environment file.
# ---------------------------------------------------------------------------------------------------------------
write_env_file() {
    cat >"$ENV_FILE" <<EOF
# Generated by fwd bootstrap v$FWD_BOOTSTRAP_VERSION. Do not edit; it is rewritten on every version bump.
export FWD_TOOL_PREFIX="$FWD_TOOL_PREFIX"
export FWD_SCRATCH="$FWD_SCRATCH"

# Tool bin dirs go first so fwd-installed versions win over anything preinstalled on the image.
case ":\$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) export PATH="$BIN_DIR:$BUN_ROOT/bin:$NPM_ROOT/bin:$CLAUDE_ROOT/bin:\$HOME/.local/bin:\$PATH" ;;
esac

# Caches redirected to scratch: on HPC \$HOME has an inode quota that a uv cache blows through, and on RunPod the
# container disk holding \$HOME is discarded when the pod stops.
export UV_CACHE_DIR="$FWD_SCRATCH/uv-cache"
export UV_PYTHON_INSTALL_DIR="$FWD_TOOL_PREFIX/uv-python"
export BUN_INSTALL="$BUN_ROOT"
export BUN_INSTALL_CACHE_DIR="$FWD_SCRATCH/bun-cache"
export npm_config_cache="$FWD_SCRATCH/npm-cache"
export npm_config_prefix="$NPM_ROOT"
export XDG_CACHE_HOME="\${XDG_CACHE_HOME:-$FWD_SCRATCH/cache}"
EOF
    log "wrote $ENV_FILE"

    cat >"$HOME_ENV_FILE" <<EOF
# Generated by fwd bootstrap v$FWD_BOOTSTRAP_VERSION. Fixed-location pointer at a path that does not depend on the
# backend's tool prefix, so non-login remote shells (ssh host 'cmd') can find the real environment file.
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
EOF
    log "wrote $HOME_ENV_FILE"
}

# ---------------------------------------------------------------------------------------------------------------
# Shell integration. Guarded by a grep so repeated bootstraps never duplicate the line, and by a -f test at read time
# so deleting the tool prefix cannot break the user's login shell.
# ---------------------------------------------------------------------------------------------------------------
install_shell_hook() {
    local marker_comment="# fwd environment"
    local line="[ -f \"$ENV_FILE\" ] && . \"$ENV_FILE\""
    local rc
    for rc in "$HOME/.bashrc" "$HOME/.profile"; do
        touch "$rc" 2>/dev/null || continue
        if grep -qF "$ENV_FILE" "$rc" 2>/dev/null; then
            continue
        fi
        printf '\n%s\n%s\n' "$marker_comment" "$line" >>"$rc"
        log "added fwd-env hook to $rc"
    done
}

# ---------------------------------------------------------------------------------------------------------------
# Installers. Each is a no-op when its tool is already resolvable, so a version bump is cheap.
# ---------------------------------------------------------------------------------------------------------------
install_uv() {
    if have uv; then
        log "uv present: $(uv --version 2>/dev/null || echo unknown)"
        return 0
    fi
    have curl || { warn "curl missing; cannot install uv"; return 1; }
    log "installing uv into $BIN_DIR"
    # INSTALLER_NO_MODIFY_PATH: PATH is owned by fwd-env.sh, not by three competing installer-appended rc lines.
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$BIN_DIR" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
        || { warn "uv install failed"; return 1; }
    have uv || { warn "uv installed but not on PATH"; return 1; }
    log "uv installed: $(uv --version 2>/dev/null || echo unknown)"
}

install_bun() {
    if have bun; then
        log "bun present: $(bun --version 2>/dev/null || echo unknown)"
        return 0
    fi
    have curl || { warn "curl missing; skipping bun"; return 0; }
    if ! have unzip; then
        # The official installer hard-depends on unzip and we may not be able to install it without root.
        warn "unzip missing; skipping bun install (JS projects using bun.lock will fail to install deps)"
        return 0
    fi
    log "installing bun into $BUN_ROOT"
    curl -fsSL https://bun.sh/install | BUN_INSTALL="$BUN_ROOT" bash >/dev/null 2>&1 \
        || { warn "bun install failed"; return 0; }
    have bun && log "bun installed: $(bun --version 2>/dev/null || echo unknown)"
}

install_node() {
    if have node; then
        log "node present: $(node --version 2>/dev/null || echo unknown)"
        return 0
    fi
    # Node is only needed as a fallback runtime (npm-based installs, the npm claude package). bun covers most JS work,
    # so a missing node is a warning rather than a failure.
    if have mise; then
        log "installing node via mise"
        mise use -g node@lts >/dev/null 2>&1 || warn "mise node install failed"
    elif have bun; then
        warn "node not found; bun is available and will be used for JS work"
    else
        warn "node not found and no installer available; npm-based dependency installs will fail"
    fi
}

install_claude() {
    if have claude && claude --version >/dev/null 2>&1; then
        log "claude present: $(claude --version 2>/dev/null || echo unknown)"
        return 0
    fi
    if have curl; then
        log "installing claude CLI into $CLAUDE_ROOT"
        # The native installer ignores CLAUDE_INSTALL_DIR/INSTALL_DIR and hardcodes "$HOME/.local", which on RunPod is
        # container disk that a pod stop wipes — leaving a dangling symlink under the (persistent) tool prefix. So we
        # point HOME at the prefix for the duration of the install instead. Everything then lands on persistent
        # storage, and only the install sees the fake HOME: claude's runtime config still uses the real one.
        if env HOME="$CLAUDE_ROOT" CLAUDE_INSTALL_DIR="$CLAUDE_ROOT/.local/bin" INSTALL_DIR="$CLAUDE_ROOT/.local/bin" \
            bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1; then
            link_claude_binary
        fi
        have claude && claude --version >/dev/null 2>&1 \
            && { log "claude installed: $(claude --version 2>/dev/null || echo unknown)"; return 0; }
        warn "claude native install failed; falling back to npm"
    fi
    if have npm; then
        npm_config_prefix="$NPM_ROOT" npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 \
            || { warn "npm install of @anthropic-ai/claude-code failed"; return 0; }
        have claude && log "claude installed via npm"
    else
        warn "could not install claude (no working installer and no npm); launch will fail to start a session"
    fi
}

link_claude_binary() {
    # Find whatever the installer produced under the prefix and expose it as $BIN_DIR/claude. Both candidate paths are
    # inside $FWD_TOOL_PREFIX, so the link target persists across a pod stop — unlike a link into $HOME/.local/bin.
    local candidate
    for candidate in "$CLAUDE_ROOT/.local/bin/claude" "$CLAUDE_ROOT/bin/claude"; do
        if [ -x "$candidate" ]; then
            ln -sf "$candidate" "$BIN_DIR/claude"
            return 0
        fi
    done
    # Last resort: the installer wrote to the real HOME anyway. Copy the payload rather than symlink to it, so the
    # binary itself lives on persistent storage.
    if [ -x "$HOME/.local/bin/claude" ]; then
        cp -L "$HOME/.local/bin/claude" "$BIN_DIR/claude" 2>/dev/null && return 0
    fi
    return 1
}

install_tmux() {
    if have tmux; then
        log "tmux present: $(tmux -V 2>/dev/null || echo unknown)"
        return 0
    fi
    # tmux is not optional: it is what makes the remote session survive a dropped connection.
    if [ "$(id -u)" = "0" ] && have apt-get; then
        log "installing tmux via apt-get"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux >/dev/null 2>&1 || true
    fi
    have tmux || fail "tmux is required but not installed, and fwd cannot install it (no root / no apt-get). Install tmux on the remote host and retry."
}

# ---------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------
log "bootstrap v$FWD_BOOTSTRAP_VERSION prefix=$FWD_TOOL_PREFIX remote_dir=$FWD_REMOTE_DIR scratch=$FWD_SCRATCH"
write_env_file
install_shell_hook

if [ "$FWD_BOOTSTRAP_MINIMAL" = "1" ]; then
    log "FWD_BOOTSTRAP_MINIMAL=1: skipping tool installs"
else
    # Each installer is allowed to degrade (|| true) except tmux, which calls fail() itself. `set -e` would otherwise
    # abort the whole launch because a warning-level miss like "no unzip for bun" returned nonzero.
    install_uv || true
    install_bun || true
    install_node || true
    install_claude || true
    install_tmux
fi

# Marker is written last: it must only exist when the run above completed, or a failed bootstrap would be skipped
# forever on subsequent launches.
date -u +"%Y-%m-%dT%H:%M:%SZ" >"$MARKER" 2>/dev/null || : >"$MARKER"
log "bootstrap complete"
