"""Built-in remote tool requirements shared by language toolchains and coding agents.

Installer scripts run only after a command and version probe fail. Every install is user-space and targets
``FWD_TOOL_PREFIX`` written by the core bootstrap, while an already working executable on the remote PATH always wins.
"""

from __future__ import annotations

from fwd.tooling.base import ToolInstaller, ToolRequirement

BUN_INSTALL_SCRIPT = """
curl -fsSL https://bun.sh/install | BUN_INSTALL="$FWD_TOOL_PREFIX/bun" bash >/dev/null 2>&1
""".strip()

CURL = ToolRequirement(name="curl", command="curl", version_command=("curl", "--version"), hint="Install curl on the remote host.")
TAR = ToolRequirement(name="tar", command="tar", version_command=("tar", "--version"), hint="Install tar on the remote host.")
UNZIP = ToolRequirement(name="unzip", command="unzip", version_command=("unzip", "-v"), hint="Install unzip on the remote host.")
MISE = ToolRequirement(name="mise", command="mise", version_command=("mise", "--version"), hint="Install mise on the remote host.")
COREPACK = ToolRequirement(name="Corepack", command="corepack", version_command=("corepack", "--version"), hint="Install Corepack or expose npm as an alternative package-manager installer.")

UV = ToolRequirement(
    name="uv",
    command="uv",
    version_command=("uv", "--version"),
    installers=(
        ToolInstaller(
            "official uv installer",
            """
mkdir -p "$FWD_TOOL_PREFIX/bin"
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$FWD_TOOL_PREFIX/bin" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
""".strip(),
            requirements=(CURL,),
        ),
    ),
    hint="Install uv on the remote host or add it to the non-interactive SSH PATH.",
)

BUN = ToolRequirement(
    name="Bun",
    command="bun",
    version_command=("bun", "--version"),
    installers=(
        ToolInstaller("official Bun installer", BUN_INSTALL_SCRIPT, requirements=(CURL, UNZIP)),
    ),
    hint="Install Bun and expose it to non-interactive SSH commands, or provide curl and unzip for fwd's user-space installer.",
)

NPM = ToolRequirement(
    name="npm",
    command="npm",
    version_command=("npm", "--version"),
    installers=(
        ToolInstaller(
            "mise Node LTS",
            """
mise use -g node@lts >/dev/null 2>&1
""".strip(),
            requirements=(MISE,),
        ),
    ),
    hint="Install Node.js/npm on the remote host or make an existing version visible to non-interactive SSH commands.",
)

PNPM = ToolRequirement(
    name="pnpm",
    command="pnpm",
    version_command=("pnpm", "--version"),
    installers=(
        ToolInstaller("Corepack", "corepack enable && corepack prepare pnpm@latest --activate", requirements=(COREPACK,)),
        ToolInstaller("npm", 'npm_config_prefix="$FWD_TOOL_PREFIX/npm" npm install -g pnpm', requirements=(NPM,)),
    ),
    hint="Install pnpm on the remote host, enable it with Corepack, or expose npm so fwd can install it in persistent storage.",
)

YARN = ToolRequirement(
    name="Yarn",
    command="yarn",
    version_command=("yarn", "--version"),
    installers=(
        ToolInstaller("Corepack", "corepack enable && corepack prepare yarn@stable --activate", requirements=(COREPACK,)),
        ToolInstaller("npm", 'npm_config_prefix="$FWD_TOOL_PREFIX/npm" npm install -g yarn', requirements=(NPM,)),
    ),
    hint="Install Yarn on the remote host, enable it with Corepack, or expose npm so fwd can install it in persistent storage.",
)

SWIFTLY = ToolRequirement(
    name="Swiftly",
    command="swiftly",
    version_command=("swiftly", "--version"),
    installers=(
        ToolInstaller(
            "official Swiftly archive",
            """
SWIFTLY_ROOT="$FWD_TOOL_PREFIX/swiftly"
SWIFTLY_HOME_DIR="$SWIFTLY_ROOT/home"
SWIFTLY_BIN_DIR="$SWIFTLY_ROOT/bin"
SWIFTLY_TOOLCHAINS_DIR="$SWIFTLY_ROOT/toolchains"
archive="$FWD_SCRATCH/swiftly-$(uname -m).tar.gz"
work_dir="$(mktemp -d "$FWD_SCRATCH/swiftly.XXXXXX")"
trap 'rm -rf "$work_dir" "$archive"' EXIT
curl -fsSL "https://download.swift.org/swiftly/linux/swiftly-$(uname -m).tar.gz" -o "$archive"
tar -xzf "$archive" -C "$work_dir"
env SWIFTLY_HOME_DIR="$SWIFTLY_HOME_DIR" SWIFTLY_BIN_DIR="$SWIFTLY_BIN_DIR" SWIFTLY_TOOLCHAINS_DIR="$SWIFTLY_TOOLCHAINS_DIR" "$work_dir/swiftly" init --assume-yes --skip-install --no-modify-profile --quiet-shell-followup
cat >"$FWD_TOOL_PREFIX/bin/swiftly" <<EOF
#!/bin/sh
export SWIFTLY_HOME_DIR="$SWIFTLY_HOME_DIR"
export SWIFTLY_BIN_DIR="$SWIFTLY_BIN_DIR"
export SWIFTLY_TOOLCHAINS_DIR="$SWIFTLY_TOOLCHAINS_DIR"
exec "$SWIFTLY_BIN_DIR/swiftly" "\\$@"
EOF
chmod +x "$FWD_TOOL_PREFIX/bin/swiftly"
""".strip(),
            requirements=(CURL, TAR),
        ),
    ),
    hint="Install Swiftly on the remote Linux host, or provide curl and tar for fwd's persistent user-space installer.",
)

SWIFT = ToolRequirement(
    name="Swift",
    command="swift",
    version_command=("swift", "--version"),
    installers=(
        ToolInstaller(
            "Swiftly latest stable toolchain",
            """
SWIFTLY_ROOT="$FWD_TOOL_PREFIX/swiftly"
SWIFTLY_HOME_DIR="$SWIFTLY_ROOT/home"
SWIFTLY_BIN_DIR="$SWIFTLY_ROOT/bin"
SWIFTLY_TOOLCHAINS_DIR="$SWIFTLY_ROOT/toolchains"
post_install="$FWD_SCRATCH/swift-post-install.sh"
if ! swiftly install latest --use --assume-yes --post-install-file "$post_install"; then
    swiftly install latest --use --assume-yes --post-install-file "$post_install"
fi
if [ -s "$post_install" ]; then
    if [ "$(id -u)" = "0" ]; then
        if grep -q 'apt-get' "$post_install"; then DEBIAN_FRONTEND=noninteractive apt-get update -qq; fi
        bash "$post_install"
        rm -f "$post_install"
    else
        printf '%s\n' "Swift requires system packages. Run this generated script as an administrator, then retry:" >&2
        cat "$post_install" >&2
        exit 1
    fi
fi
for candidate in "$SWIFTLY_BIN_DIR"/*; do
    [ -x "$candidate" ] || continue
    name="${candidate##*/}"
    [ "$name" = "swiftly" ] && continue
    rm -f "$FWD_TOOL_PREFIX/bin/$name"
    cat >"$FWD_TOOL_PREFIX/bin/$name" <<EOF
#!/bin/sh
export SWIFTLY_HOME_DIR="$SWIFTLY_HOME_DIR"
export SWIFTLY_BIN_DIR="$SWIFTLY_BIN_DIR"
export SWIFTLY_TOOLCHAINS_DIR="$SWIFTLY_TOOLCHAINS_DIR"
exec "$candidate" "\\$@"
EOF
    chmod +x "$FWD_TOOL_PREFIX/bin/$name"
done
""".strip(),
            requirements=(SWIFTLY,),
        ),
    ),
    hint="Install a Linux Swift toolchain or provide Swiftly (or curl and tar) for fwd's persistent user-space installer.",
)

CLAUDE = ToolRequirement(
    name="Claude Code",
    command="claude",
    version_command=("claude", "--version"),
    installers=(
        ToolInstaller(
            "Claude native installer",
            """
CLAUDE_ROOT="$FWD_TOOL_PREFIX/claude"
mkdir -p "$CLAUDE_ROOT" "$FWD_TOOL_PREFIX/bin"
env HOME="$CLAUDE_ROOT" CLAUDE_INSTALL_DIR="$CLAUDE_ROOT/.local/bin" INSTALL_DIR="$CLAUDE_ROOT/.local/bin" bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1
for candidate in "$CLAUDE_ROOT/.local/bin/claude" "$CLAUDE_ROOT/bin/claude"; do
    if [ -x "$candidate" ]; then ln -sf "$candidate" "$FWD_TOOL_PREFIX/bin/claude"; exit 0; fi
done
exit 1
""".strip(),
            requirements=(CURL,),
        ),
        ToolInstaller("npm", 'npm_config_prefix="$FWD_TOOL_PREFIX/npm" npm install -g @anthropic-ai/claude-code', requirements=(NPM,)),
    ),
    hint="Install Claude Code on the remote host, or provide curl/npm for fwd's persistent user-space installer.",
)

CODEX = ToolRequirement(
    name="Codex",
    command="codex",
    version_command=("codex", "--version"),
    installers=(
        ToolInstaller("npm", 'npm_config_prefix="$FWD_TOOL_PREFIX/npm" npm install -g @openai/codex', requirements=(NPM,)),
        ToolInstaller(
            "Bun",
            """
BUN_INSTALL="$FWD_TOOL_PREFIX/bun" bun install --global @openai/codex >/dev/null 2>&1
codex_entry="$FWD_TOOL_PREFIX/bun/install/global/node_modules/@openai/codex/bin/codex.js"
test -f "$codex_entry" || exit 1
cat >"$FWD_TOOL_PREFIX/bin/codex" <<EOF
#!/bin/sh
exec "$FWD_TOOL_PREFIX/bun/bin/bun" "$codex_entry" "\\$@"
EOF
chmod +x "$FWD_TOOL_PREFIX/bin/codex"
""".strip(),
            requirements=(BUN,),
        ),
    ),
    hint="Install Codex on the remote host, or provide npm/Bun (or curl and unzip) for fwd's persistent user-space installer.",
)
