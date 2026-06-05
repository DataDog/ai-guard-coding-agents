#!/bin/sh
# AI Guard bootstrap installer.
#
# Detects the current OS/arch, downloads the matching ai-guard release
# tarball from GitHub, verifies its SHA-256 checksum, extracts the
# PyInstaller onedir bundle into ~/.local/share/ai-guard, symlinks the
# launcher at ~/.local/bin/ai-guard, and hands off to `ai-guard install`
# (or `ai-guard uninstall` if --uninstall is passed) for the actual install
# logic.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-coding-agents/main/scripts/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --advanced
#   curl -fsSL .../install.sh | sh -s -- --uninstall --yes
#
# Environment overrides:
#   AI_GUARD_VERSION       version to install, without the leading "v" (default:
#                          the version baked in below, kept in sync by
#                          release-please)
#   AI_GUARD_BIN_DIR       symlink location          (default: ~/.local/bin)
#   AI_GUARD_BUNDLE_DIR    bundle extract root       (default: ~/.local/share/ai-guard)
#   AI_GUARD_BUNDLE        path to a built ai-guard tarball (same .tar.gz
#                          format the release publishes); if set, skips the
#                          GitHub download and installs from this archive
#                          instead. Build one with ``scripts/build.sh``.

set -eu

REPO="DataDog/ai-guard-coding-agents"
BIN_DIR="${AI_GUARD_BIN_DIR:-$HOME/.local/bin}"
BUNDLE_DIR="${AI_GUARD_BUNDLE_DIR:-$HOME/.local/share/ai-guard}"
DEFAULT_VERSION="0.4.0"  # x-release-please-version
VERSION="v${AI_GUARD_VERSION:-$DEFAULT_VERSION}"
LOCAL_BUNDLE="${AI_GUARD_BUNDLE:-}"

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
TARBALL="${ARTIFACT}.tar.gz"
ok "$os $arch"

# --- preflight ---------------------------------------------------------------
# Validate required tools upfront so the user sees every missing dependency at
# once rather than discovering them one failed step at a time.

section "Check requirements"

missing=0

require() {
    # require <command> [<context>]
    name="$1"; ctx="${2:-}"
    if command -v "$name" >/dev/null 2>&1; then
        ok "$name"
        return
    fi
    if [ -n "$ctx" ]; then
        err "$name not found ($ctx)"
    else
        err "$name not found"
    fi
    missing=$((missing + 1))
}

require_any() {
    # require_any <out_var> <label> <cmd1> [<cmd2> ...]
    # On success, stores the resolved executable name in <out_var> so the
    # call sites below can dispatch without re-probing the PATH.
    out="$1"; label="$2"; shift 2
    for cmd in "$@"; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ok "$label: $cmd"
            eval "$out=\$cmd"
            return
        fi
    done
    err "$label not found (need one of: $*)"
    missing=$((missing + 1))
}

# Downloader and checksum tools are only used when fetching the released
# bundle; the LOCAL_BUNDLE shortcut skips both code paths. ``tar`` and
# ``mktemp`` are needed either way to lay the bundle out.
if [ -z "$LOCAL_BUNDLE" ]; then
    require_any HTTP_TOOL "HTTP downloader" curl wget
    require_any SHA_TOOL  "SHA-256 verifier" sha256sum shasum
fi
require mktemp
require tar

# The handoff to `ai-guard ${MODE}` manages a per-user service, so the
# platform's service tool must be present.
case "$os" in
    linux) require systemctl "ai-guard runs as a systemd --user service" ;;
    macos) require launchctl "ai-guard runs as a launchd user agent" ;;
esac

if [ "$missing" -gt 0 ]; then
    die "$missing required tool(s) missing — install them and re-run"
fi

# --- helpers used by both paths ---------------------------------------------
# Hand off to ``ai-guard ${MODE}``. When this script is run via
# ``curl … | sh`` our stdin is the curl pipe — already EOF by the time we
# exec — so any prompt from the Python installer (e.g. ``Site (DD_SITE)``)
# trips Click's EOF guard and prints ``Aborted!`` before the user can type.
# Reattach stdin to /dev/tty when it's available so the handoff is
# interactive; CI flows (no controlling terminal) fall through to the
# original behaviour and are expected to pass ``--non-interactive``.
handoff() {
    if [ ! -t 0 ] && [ -r /dev/tty ]; then
        exec < /dev/tty
    fi
    exec "${BIN_DIR}/ai-guard" "${MODE}" "$@"
}

# Copy a PyInstaller onedir bundle directory into ${BUNDLE_DIR} and symlink
# the launcher at ${BIN_DIR}/ai-guard so the user has a stable PATH entry.
install_bundle() {
    src="$1"
    [ -d "$src" ] || die "expected bundle directory, got: $src"
    [ -x "${src}/ai-guard" ] || die "no launcher at ${src}/ai-guard"

    mkdir -p "$(dirname "$BUNDLE_DIR")"
    # Clean any previous bundle so leftover files from an older release
    # don't shadow renamed ones.
    rm -rf "$BUNDLE_DIR"
    cp -R "$src" "$BUNDLE_DIR"
    chmod +x "${BUNDLE_DIR}/ai-guard"

    mkdir -p "$BIN_DIR"
    ln -sfn "${BUNDLE_DIR}/ai-guard" "${BIN_DIR}/ai-guard"
}

# --- local bundle shortcut ---------------------------------------------------
if [ -n "$LOCAL_BUNDLE" ]; then
    section "Local bundle"
    [ -f "$LOCAL_BUNDLE" ] || die "AI_GUARD_BUNDLE does not point to a file: $LOCAL_BUNDLE"

    LOCAL_TMP=$(mktemp -d)
    trap 'rm -rf "$LOCAL_TMP"' EXIT INT TERM
    tar -xzf "$LOCAL_BUNDLE" -C "$LOCAL_TMP" \
        || die "could not extract $LOCAL_BUNDLE (expected a tar.gz)"

    # The tarball's top-level directory matches the release artifact name
    # (e.g. ``ai-guard-linux-x86_64``); we don't care about the exact name,
    # only that it's the single dir inside the archive.
    extracted=$(find "$LOCAL_TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
    [ -n "$extracted" ] || die "no bundle directory inside $LOCAL_BUNDLE"

    install_bundle "$extracted"
    ok "installed bundle to ${BUNDLE_DIR}"
    detail "from ${LOCAL_BUNDLE}"
    ok "symlinked launcher at ${BIN_DIR}/ai-guard"

    case ":${PATH:-}:" in
        *":${BIN_DIR}:"*) : ;;
        *) warn "${BIN_DIR} is not on PATH"
           detail "export PATH=\"${BIN_DIR}:\$PATH\"" ;;
    esac

    handoff "$@"
fi

# --- release version ---------------------------------------------------------
section "Release version"
ok "$VERSION"

# --- download ----------------------------------------------------------------
section "Download bundle"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

BASE="https://github.com/${REPO}/releases/download/${VERSION}"

download() {
    url="$1"; dest="$2"
    case "$HTTP_TOOL" in
        curl) curl -fL --proto '=https' --tlsv1.2 -o "$dest" "$url" ;;
        wget) wget -qO "$dest" "$url" ;;
    esac
}

action "fetching $TARBALL"
download "${BASE}/${TARBALL}"        "${TMP}/${TARBALL}"
download "${BASE}/${TARBALL}.sha256" "${TMP}/${TARBALL}.sha256"

# Verify the checksum. The .sha256 file uses the standard
# `<hash>  <filename>` format, which both sha256sum and shasum understand.
(
    cd "$TMP"
    case "$SHA_TOOL" in
        sha256sum) sha256sum -c "${TARBALL}.sha256" >/dev/null ;;
        shasum)    shasum -a 256 -c "${TARBALL}.sha256" >/dev/null ;;
    esac
)
ok "checksum verified"

# Extract the onedir bundle into a sibling dir under ${TMP} and install it.
# The tarball's top-level directory is the artifact name (e.g. ``ai-guard-linux-x86_64``).
tar -xzf "${TMP}/${TARBALL}" -C "$TMP"
install_bundle "${TMP}/${ARTIFACT}"
ok "installed bundle to ${BUNDLE_DIR}"
ok "symlinked launcher at ${BIN_DIR}/ai-guard"

case ":${PATH:-}:" in
    *":${BIN_DIR}:"*) : ;;
    *) warn "${BIN_DIR} is not on PATH"
       detail "export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# --- handoff -----------------------------------------------------------------
handoff "$@"