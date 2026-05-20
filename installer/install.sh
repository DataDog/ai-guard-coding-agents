#!/bin/sh
# ai-guard bootstrap installer.
#
# Detects the current OS/arch, downloads the matching ai-guard binary from
# the GitHub release, verifies its SHA-256 checksum, drops it in
# ~/.local/bin, and hands off to `ai-guard install` (or `ai-guard uninstall`
# if --uninstall is passed) for the actual install logic.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-hooks/main/installer/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --advanced
#   curl -fsSL .../install.sh | sh -s -- --uninstall --yes
#
# Environment overrides:
#   AI_GUARD_VERSION    pin a specific release tag (default: latest)
#   AI_GUARD_REPO       override the GitHub repo  (default: DataDog/ai-guard-hooks)
#   AI_GUARD_BIN_DIR    install location          (default: ~/.local/bin)

set -eu

REPO="${AI_GUARD_REPO:-DataDog/ai-guard-hooks}"
BIN_DIR="${AI_GUARD_BIN_DIR:-$HOME/.local/bin}"
VERSION="${AI_GUARD_VERSION:-latest}"

# --- colours -----------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    NC=$(printf '\033[0m')
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; NC=""
fi

step() { printf '%s%s%s %s\n' "$DIM" "$1" "$NC" "$2"; }
ok()   { printf '      %s✓%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '      %s!%s %s\n' "$YELLOW" "$NC" "$1"; }
die()  { printf '%s✗%s %s\n' "$RED" "$NC" "$1" >&2; exit 1; }

# --- mode --------------------------------------------------------------------
MODE="install"
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE="uninstall" ;;
    esac
done

printf '%sai-guard installer%s\n' "$BOLD" "$NC"

# --- platform detection ------------------------------------------------------
step "[1/4]" "detecting platform"

uname_s=$(uname -s)
uname_m=$(uname -m)

case "$uname_s" in
    Linux)  os="linux" ;;
    Darwin) os="macos" ;;
    *)      die "unsupported OS: $uname_s (Windows support coming via install.ps1)" ;;
esac

case "$uname_m" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) die "unsupported architecture: $uname_m" ;;
esac

# Reject combinations that aren't in .github/matrix.json.
case "${os}-${arch}" in
    linux-x86_64|linux-arm64|macos-arm64) : ;;
    *) die "no prebuilt binary for ${os}-${arch}" ;;
esac

ARTIFACT="ai-guard-${os}-${arch}"
ok "$os $arch"

# --- version resolution ------------------------------------------------------
step "[2/4]" "resolving release version"

if [ "$VERSION" = "latest" ]; then
    API_URL="https://api.github.com/repos/${REPO}/releases/latest"
    if command -v curl >/dev/null 2>&1; then
        VERSION=$(curl -fsSL "$API_URL" | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
    elif command -v wget >/dev/null 2>&1; then
        VERSION=$(wget -qO- "$API_URL" | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
    else
        die "neither curl nor wget is available"
    fi
    if [ -z "$VERSION" ]; then
        die "could not determine latest release; try AI_GUARD_VERSION=vX.Y.Z"
    fi
fi
ok "version $VERSION"

# --- download ----------------------------------------------------------------
step "[3/4]" "downloading $ARTIFACT"

mkdir -p "$BIN_DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

BASE="https://github.com/${REPO}/releases/download/${VERSION}"

download() {
    url="$1"; dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --proto '=https' --tlsv1.2 -o "$dest" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    else
        die "neither curl nor wget is available"
    fi
}

download "${BASE}/${ARTIFACT}"        "${TMP}/${ARTIFACT}"
download "${BASE}/${ARTIFACT}.sha256" "${TMP}/${ARTIFACT}.sha256"

# Verify the checksum. The .sha256 file uses the standard
# `<hash>  <filename>` format, which both sha256sum and shasum understand.
(
    cd "$TMP"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c "${ARTIFACT}.sha256" >/dev/null
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "${ARTIFACT}.sha256" >/dev/null
    else
        die "no SHA-256 tool available (need sha256sum or shasum)"
    fi
)
ok "checksum verified"

chmod +x "${TMP}/${ARTIFACT}"
mv "${TMP}/${ARTIFACT}" "${BIN_DIR}/ai-guard"
ok "installed to ${BIN_DIR}/ai-guard"

case ":${PATH:-}:" in
    *":${BIN_DIR}:"*) : ;;
    *) warn "${BIN_DIR} is not on PATH. Add: export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# --- handoff -----------------------------------------------------------------
step "[4/4]" "running ai-guard ${MODE}"
exec "${BIN_DIR}/ai-guard" "${MODE}" "$@"
