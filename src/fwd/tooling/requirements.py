"""Built-in remote tool requirements shared by language toolchains and coding agents.

Installer scripts run only after a command and version probe fail. Every install is user-space and targets
``FWD_TOOL_PREFIX`` written by the core bootstrap, while an already working executable on the remote PATH always wins.
"""

from __future__ import annotations

from fwd.tooling.base import ToolInstaller, ToolRequirement

BUN_INSTALL_SCRIPT = """
curl -fsSL https://bun.sh/install | BUN_INSTALL="$FWD_TOOL_PREFIX/bun" bash >/dev/null 2>&1
""".strip()

NVM_SELECT_NODE_SCRIPT = """
unset npm_config_prefix NPM_CONFIG_PREFIX
set +u
export NVM_NO_PROGRESS=1
project_dir="${FWD_REMOTE_DIR:-$PWD}"
if [ -f "$project_dir/.nvmrc" ]; then
    cd "$project_dir"
    nvm install
    nvm use
else
    nvm install --lts
    nvm use --lts
fi
nvm alias default "$(nvm current)" >/dev/null 2>&1
node_bin="$(dirname "$(command -v node)")"
for command in node npm npx corepack; do
    [ -x "$node_bin/$command" ] || continue
    ln -sf "$node_bin/$command" "$FWD_TOOL_PREFIX/bin/$command"
done
set -u
""".strip()

NVM_INSTALL_SCRIPT = f"""
NVM_DIR="$FWD_TOOL_PREFIX/nvm"
export NVM_DIR
mkdir -p "$NVM_DIR"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | env NVM_DIR="$NVM_DIR" PROFILE=/dev/null METHOD=script bash
set +u
. "$NVM_DIR/nvm.sh"
{NVM_SELECT_NODE_SCRIPT}
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

NVM = ToolRequirement(
    name="nvm",
    command="nvm",
    version_command=("nvm", "--version"),
    installers=(
        ToolInstaller("official nvm installer", NVM_INSTALL_SCRIPT, requirements=(CURL, TAR)),
    ),
    hint="Install nvm on the remote host, or provide curl and tar so fwd can install the project's .nvmrc version persistently.",
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
        ToolInstaller("nvm Node", NVM_SELECT_NODE_SCRIPT, requirements=(NVM,)),
    ),
    hint="Install Node.js/npm on the remote host, or provide curl and tar so fwd can install the project's .nvmrc version (or Node LTS) and npm persistently through nvm.",
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

GH = ToolRequirement(
    name="GitHub CLI",
    command="gh",
    version_command=("gh", "--version"),
    installers=(
        ToolInstaller(
            "official GitHub CLI release",
            """
case "$(uname -m)" in
    x86_64|amd64) gh_arch=amd64 ;;
    aarch64|arm64) gh_arch=arm64 ;;
    *) printf '%s\n' "unsupported GitHub CLI architecture: $(uname -m)" >&2; exit 1 ;;
esac
release_url="$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/cli/cli/releases/latest)"
gh_version="${release_url##*/v}"
test -n "$gh_version"
archive="$FWD_SCRATCH/gh_${gh_version}_linux_${gh_arch}.tar.gz"
work_dir="$(mktemp -d "$FWD_SCRATCH/gh.XXXXXX")"
trap 'rm -rf "$work_dir" "$archive"' EXIT
curl -fsSL "https://github.com/cli/cli/releases/download/v${gh_version}/gh_${gh_version}_linux_${gh_arch}.tar.gz" -o "$archive"
tar -xzf "$archive" -C "$work_dir"
cp "$work_dir/gh_${gh_version}_linux_${gh_arch}/bin/gh" "$FWD_TOOL_PREFIX/bin/gh"
chmod +x "$FWD_TOOL_PREFIX/bin/gh"
""".strip(),
            requirements=(CURL, TAR),
        ),
    ),
    hint="Install GitHub CLI on the remote host, or provide curl and tar for fwd's official release installer.",
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
    ),
    hint="Install Claude Code on the remote host, or provide curl for fwd's persistent native installer.",
)

CODEX = ToolRequirement(
    name="Codex",
    command="codex",
    version_command=("codex", "--version"),
    installers=(
        ToolInstaller(
            "Codex managed standalone installer",
            """
curl -fsSL https://chatgpt.com/codex/install.sh | sh
managed_codex="$HOME/.codex/packages/standalone/current/codex"
test -x "$managed_codex"
ln -sf "$managed_codex" "$FWD_TOOL_PREFIX/bin/codex"
""".strip(),
            requirements=(CURL,),
        ),
    ),
    hint="Install Codex with its managed standalone installer, or provide curl so fwd can install the daemon-capable distribution.",
    probe_script="""
managed_codex="$HOME/.codex/packages/standalone/current/codex"
resolved_codex="$(command -v codex 2>/dev/null)" || exit 1
test -x "$managed_codex"
test "$(readlink -f "$resolved_codex")" = "$(readlink -f "$managed_codex")"
"$managed_codex" --version
""".strip(),
)
