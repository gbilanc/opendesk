#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Windows NSIS installer packager
#
# Takes the PyInstaller output (dist/opendesk/) and bundles it into an
# NSIS installer .exe.
#
# Prerequisites:
#   Install NSIS: https://nsis.sourceforge.io/Download
#   (or on Linux: sudo apt install nsis)
#
# Usage:
#   bash scripts/package-windows.sh 1.0.0 x86_64
# ---------------------------------------------------------------------------

set -euo pipefail

VERSION="${1:-1.0.0}"
ARCH="${2:-x86_64}"
DIST="dist/opendesk"
OUTPUT="dist/opendesk-${VERSION}-windows-${ARCH}.exe"
NSI="scripts/opendesk.nsi"

if [ ! -d "$DIST" ]; then
    echo "✗ $DIST not found. Run PyInstaller first."
    exit 1
fi

echo "→ Building NSIS installer..."

# Ensure opendesk.nsi exists with correct version
if [ ! -f "$NSI" ]; then
    echo "✗ $NSI not found."
    exit 1
fi

# Replace version placeholder in NSI
sed "s/!define VERSION \".*\"/!define VERSION \"${VERSION}\"/" "$NSI" > build/opendesk_temp.nsi

if command -v makensis &>/dev/null; then
    makensis build/opendesk_temp.nsi
    mv "dist/opendesk-installer.exe" "$OUTPUT" 2>/dev/null || true
elif command -v makensis.exe &>/dev/null; then
    makensis.exe build/opendesk_temp.nsi
    mv "dist/opendesk-installer.exe" "$OUTPUT" 2>/dev/null || true
else
    echo "⚠ makensis not found in PATH."
    echo "  Install NSIS from: https://nsis.sourceforge.io/Download"
    echo "  Or on Linux: sudo apt install nsis"
    echo ""
    echo "  For now, creating a zip fallback..."
    if command -v zip &>/dev/null; then
        cd dist && zip -r "opendesk-${VERSION}-windows-${ARCH}.zip" opendesk/
        echo "  ✓ Created dist/opendesk-${VERSION}-windows-${ARCH}.zip"
    else
        echo "  ✗ zip not found either. Nothing done."
    fi
    exit 0
fi

if [ -f "$OUTPUT" ]; then
    echo "✓ Installer created: $OUTPUT"
else
    echo "✓ NSIS build complete (output may be in dist/)"
fi
