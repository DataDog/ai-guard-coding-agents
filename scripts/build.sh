#!/bin/sh
# Build the ai-guard onedir bundle locally and tar it up into the same
# ``ai-guard-<os>-<arch>.tar.gz`` shape the GitHub release publishes, so the
# bootstrap installer (``AI_GUARD_BUNDLE=...``) can consume it unchanged.
#
# Usage:
#   sh scripts/build.sh
#   AI_GUARD_BUNDLE=$(pwd)/dist/ai-guard.tar.gz sh scripts/install.sh
#
# Requires ``uv``; runs pyinstaller through ``uv run`` so the build env
# matches whatever ``[project.optional-dependencies].build`` declares.

set -eu

cd "$(dirname "$0")/.."

uname_s=$(uname -s)
uname_m=$(uname -m)
case "$uname_s" in
    Linux)  os="linux" ;;
    Darwin) os="macos" ;;
    *)      echo "unsupported OS: $uname_s" >&2; exit 1 ;;
esac
case "$uname_m" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) echo "unsupported architecture: $uname_m" >&2; exit 1 ;;
esac

artifact="ai-guard-${os}-${arch}"
tarball="${artifact}.tar.gz"

uv run pyinstaller ai-guard.spec --noconfirm --clean

# PyInstaller drops the bundle at ``dist/ai-guard``. Keep that path intact
# (the binary-proxy test suite looks for ``dist/ai-guard/ai-guard``) and
# stage a same-name copy for the tarball top-level — that matches the
# release artifact layout install.sh expects to unpack.
rm -rf "dist/${artifact}"
cp -R dist/ai-guard "dist/${artifact}"
( cd dist && tar -czf "${tarball}" "${artifact}" )
rm -rf "dist/${artifact}"

# Convenience alias so callers don't need to know the platform suffix.
( cd dist && ln -sf "${tarball}" ai-guard.tar.gz )

echo
echo "Built dist/${tarball}"
echo "      dist/ai-guard.tar.gz -> ${tarball}"
echo
echo "Install with:"
echo "  AI_GUARD_BUNDLE=\"\$(pwd)/dist/ai-guard.tar.gz\" sh scripts/install.sh"
