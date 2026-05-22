#!/bin/sh
# AI Guard bootstrap installer.
#
# Detects the current OS/arch, downloads the matching ai-guard binary from
# the GitHub release, verifies its SHA-256 checksum, drops it in
# ~/.local/bin, and hands off to `ai-guard install` (or `ai-guard uninstall`
# if --uninstall is passed) for the actual install logic.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-coding-agents/main/installer/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --advanced
#   curl -fsSL .../install.sh | sh -s -- --uninstall --yes
#
# Environment overrides:
#   AI_GUARD_VERSION    pin a specific release tag (default: latest)
#   AI_GUARD_BIN_DIR    install location          (default: ~/.local/bin)
#   AI_GUARD_BINARY     path to an existing ai-guard binary; if set, skips
#                       the GitHub download and uses this file instead

set -eu

REPO="DataDog/ai-guard-coding-agents"
BIN_DIR="${AI_GUARD_BIN_DIR:-$HOME/.local/bin}"
VERSION="${AI_GUARD_VERSION:-latest}"
LOCAL_BINARY="${AI_GUARD_BINARY:-}"

# --- ui primitives -----------------------------------------------------------
# Mirror src/aiguard/installer/ui.py so the bootstrap → ai-guard handoff feels
# like one coherent screen. Accent = Datadog purple #774AA4; degrades to
# 256-colour, then 16-colour magenta, then plain text in non-TTYs.

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    CYAN=$(printf '\033[36m')
    NC=$(printf '\033[0m')

    case "${COLORTERM:-}" in
        truecolor|24bit)
            ACCENT=$(printf '\033[38;2;119;74;164m')
            ACCENT_DIM=$(printf '\033[38;2;90;54;128m')
            ;;
        *)
            ncolors=$(tput colors 2>/dev/null || echo 8)
            if [ "$ncolors" -ge 256 ]; then
                ACCENT=$(printf '\033[38;5;97m')
                ACCENT_DIM=$(printf '\033[38;5;60m')
            else
                ACCENT=$(printf '\033[35m')
                ACCENT_DIM=$(printf '\033[35m')
            fi
            ;;
    esac
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
    ACCENT=""; ACCENT_DIM=""
fi

section() {
    label="$1"
    cols=$(tput cols 2>/dev/null || echo 80)
    # "Label ────────────────────────────────" — bold purple label, dim fill.
    label_len=$(printf '%s' "$label" | wc -c | tr -d ' ')
    fill=$((cols - label_len - 1))
    [ "$fill" -lt 4 ] && fill=4

    rule=""
    i=0
    while [ "$i" -lt "$fill" ]; do
        rule="${rule}─"
        i=$((i + 1))
    done

    printf '\n%s%s%s%s %s%s%s\n\n' \
        "$BOLD" "$ACCENT" "$label" "$NC" "$ACCENT_DIM" "$rule" "$NC"
}

ok()     { printf '  %s✓%s  %s\n' "$GREEN" "$NC" "$1"; }
warn()   { printf '  %s⚠%s  %s\n' "$YELLOW" "$NC" "$1"; }
err()    { printf '  %s✗%s  %s\n' "$RED" "$NC" "$1"; }
action() { printf '  %s→%s  %s\n' "$CYAN" "$NC" "$1"; }
detail() { printf '     %s%s%s\n' "$DIM" "$1" "$NC"; }
die()    { err "$1" >&2; exit 1; }

# --- mode --------------------------------------------------------------------
MODE="install"
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE="uninstall" ;;
    esac
done

# --- platform detection ------------------------------------------------------
section "Detect platform"

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

# --- local binary shortcut ---------------------------------------------------
if [ -n "$LOCAL_BINARY" ]; then
    section "Use local binary"
    [ -f "$LOCAL_BINARY" ] || die "AI_GUARD_BINARY does not point to a file: $LOCAL_BINARY"
    mkdir -p "$BIN_DIR"
    cp "$LOCAL_BINARY" "${BIN_DIR}/ai-guard"
    chmod +x "${BIN_DIR}/ai-guard"
    ok "installed to ${BIN_DIR}/ai-guard"
    detail "from ${LOCAL_BINARY}"

    case ":${PATH:-}:" in
        *":${BIN_DIR}:"*) : ;;
        *) warn "${BIN_DIR} is not on PATH"
           detail "export PATH=\"${BIN_DIR}:\$PATH\"" ;;
    esac

    exec "${BIN_DIR}/ai-guard" "${MODE}" "$@"
fi

# --- version resolution ------------------------------------------------------
section "Resolve release version"

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
ok "$VERSION"

# --- download ----------------------------------------------------------------
section "Download binary"

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

action "fetching $ARTIFACT"
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
    *) warn "${BIN_DIR} is not on PATH"
       detail "export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# --- handoff -----------------------------------------------------------------
exec "${BIN_DIR}/ai-guard" "${MODE}" "$@"