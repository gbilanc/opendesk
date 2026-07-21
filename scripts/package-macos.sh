#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — macOS DMG packager
#
# Takes the PyInstaller output (dist/opendesk/OpenDesk.app) and wraps it
# in a .dmg disk image.
#
# Prerequisites:
#   brew install create-dmg   (or use hdiutil fallback below)
#
# Usage:
#   bash scripts/package-macos.sh 1.0.0 x86_64
# ---------------------------------------------------------------------------

set -euo pipefail

VERSION="${1:-1.0.0}"
ARCH="${2:-x86_64}"
APP="dist/opendesk/OpenDesk.app"
OUTPUT="dist/opendesk-${VERSION}-macos-${ARCH}.dmg"
VOLNAME="OpenDesk ${VERSION}"

if [ ! -d "$APP" ]; then
    echo "✗ $APP not found. Run PyInstaller first (macOS only)."
    exit 1
fi

echo "→ Creating DMG..."

if command -v create-dmg &>/dev/null; then
    create-dmg \
        --volname "$VOLNAME" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "OpenDesk.app" 150 190 \
        --app-drop-link 450 185 \
        "$OUTPUT" \
        "$APP"
else
    echo "⚠ create-dmg not found. Using hdiutil fallback (no background/icon customization)..."

    # Clean
    rm -f "$OUTPUT"

    # Create a temporary directory for the DMG contents
    TMPDIR="$(mktemp -d)"
    cp -R "$APP" "$TMPDIR/"
    ln -s /Applications "$TMPDIR/Applications"

    hdiutil create -volname "$VOLNAME" -srcfolder "$TMPDIR" -ov -format UDZO "$OUTPUT"
    rm -rf "$TMPDIR"
fi

echo "✓ DMG created: $OUTPUT"
