#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Build orchestrator
#
# Builds a standalone package for the current OS.
# Run on each target platform (Linux, macOS, Windows via Git Bash).
#
# Usage:
#   ./scripts/build.sh              # build for current OS
#   ./scripts/build.sh --version 1.2.0   # custom version
# ---------------------------------------------------------------------------

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-1.0.0}"
CLEAN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --clean)   CLEAN=true; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── Detect OS ──────────────────────────────────────────────────────
case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="macos" ;;
    MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
    *)      echo "Unsupported OS: $(uname -s)"; exit 1 ;;
esac

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  ARCH="x86_64" ;;
    aarch64|arm64) ARCH="arm64" ;;
esac

echo "========================================"
echo " OpenDesk Build"
echo " OS:      $OS"
echo " Arch:    $ARCH"
echo " Version: $VERSION"
echo "========================================"

# ── Clean ──────────────────────────────────────────────────────────
if $CLEAN; then
    echo "→ Cleaning previous builds..."
    rm -rf dist build
fi

# ── PyInstaller ────────────────────────────────────────────────────
echo "→ Building with PyInstaller..."
uv pip install pyinstaller --quiet 2>/dev/null || pip install pyinstaller --quiet
uv run pyinstaller opendesk.spec --clean --noconfirm

echo "✓ PyInstaller build complete → dist/opendesk/"

# ── Package for OS ─────────────────────────────────────────────────
case "$OS" in
    linux)
        echo "→ Packaging as AppImage..."
        bash scripts/package-linux.sh "$VERSION" "$ARCH"
        ;;
    macos)
        echo "→ Packaging as DMG..."
        bash scripts/package-macos.sh "$VERSION" "$ARCH"
        ;;
    windows)
        echo "→ Packaging as NSIS installer..."
        bash scripts/package-windows.sh "$VERSION" "$ARCH"
        ;;
esac

echo ""
echo "========================================"
echo " Build complete!"
ls -lh dist/opendesk-* 2>/dev/null || echo "  → dist/opendesk/"
echo "========================================"
