#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Linux AppImage packager
#
# Takes the PyInstaller output (dist/opendesk/) and bundles it into an
# AppImage using appimagetool.
#
# Prerequisites:
#   sudo apt install appimagetool  (or download from GitHub)
#
# Usage:
#   bash scripts/package-linux.sh 1.0.0 x86_64
# ---------------------------------------------------------------------------

set -euo pipefail

VERSION="${1:-1.0.0}"
ARCH="${2:-x86_64}"
APPDIR="dist/opendesk.AppDir"
OUTPUT="dist/opendesk-${VERSION}-linux-${ARCH}.AppImage"

echo "→ Creating AppDir structure..."

# Clean previous
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output
cp -a dist/opendesk/* "$APPDIR/usr/bin/"

# Create wrapper script (AppRun)
cat > "$APPDIR/AppRun" << 'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/opendesk" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# Desktop entry
cat > "$APPDIR/usr/share/applications/opendesk.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=OpenDesk
Comment=Remote Desktop Application
Icon=opendesk
Exec=opendesk
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=opendesk
DESKTOP

# Copy icon (SVG or fallback)
if [ -f opendesk/ui/resources/opendesk.svg ]; then
    cp opendesk/ui/resources/opendesk.svg "$APPDIR/usr/share/icons/hicolor/256x256/apps/opendesk.svg"
    cp opendesk/ui/resources/opendesk.svg "$APPDIR/opendesk.svg"
    # .DirIcon for AppImage
    cp opendesk/ui/resources/opendesk.svg "$APPDIR/.DirIcon"
fi

# Symlink for desktop integration
ln -sf usr/share/applications/opendesk.desktop "$APPDIR/opendesk.desktop"
ln -sf usr/share/icons/hicolor/256x256/apps/opendesk.svg "$APPDIR/opendesk.svg"

# ── Build AppImage ─────────────────────────────────────────────────
echo "→ Building AppImage with appimagetool..."

if command -v appimagetool &>/dev/null; then
    appimagetool "$APPDIR" "$OUTPUT"
elif command -v appimagetool-x86_64.AppImage &>/dev/null; then
    appimagetool-x86_64.AppImage "$APPDIR" "$OUTPUT"
else
    echo "⚠ appimagetool not found in PATH."
    echo "  Download it from: https://github.com/AppImage/AppImageKit/releases"
    echo "  Then run: chmod +x appimagetool && ./appimagetool $APPDIR $OUTPUT"
    echo ""
    echo "  For now, creating a tar.gz fallback..."
    tar -czf "dist/opendesk-${VERSION}-linux-${ARCH}.tar.gz" -C dist opendesk
    echo "  ✓ Created dist/opendesk-${VERSION}-linux-${ARCH}.tar.gz"
    exit 0
fi

chmod +x "$OUTPUT"
echo "✓ AppImage created: $OUTPUT"
