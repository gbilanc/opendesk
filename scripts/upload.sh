#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Upload script
#
# Uploads built packages to your server via rsync.
# Customize DEST to match your server details.
#
# Usage:
#   OPENDESK_SERVER=user@your-server.com bash scripts/upload.sh
# ---------------------------------------------------------------------------

set -euo pipefail

DEST="${OPENDESK_SERVER:-root@gibisoft.net:/var/www/html}"
VERSION="${VERSION:-1.0.0}"

cd "$(dirname "$0")/.."

echo "→ Uploading to $DEST ..."

# Packages
echo "  → Packages..."
rsync -avz --progress \
    "dist/opendesk-${VERSION}-linux-x86_64.AppImage" \
    "dist/opendesk-${VERSION}-macos-x86_64.dmg" \
    "dist/opendesk-${VERSION}-windows-x86_64.exe" \
    "$DEST/dl/" 2>/dev/null || echo "  ⚠ Some packages may be missing (build on each platform first)"

# Install scripts
echo "  → Install scripts..."
rsync -avz \
    "scripts/install.sh" \
    "scripts/install.ps1" \
    "$DEST/"

# Update latest symlinks on server
echo "  → Updating latest symlinks..."
ssh "${DEST%%:*}" "cd ${DEST#*:} && \
    for pkg in dl/opendesk-${VERSION}-*; do \
        ext=\"\${pkg##*.}\"; \
        os=\$(echo \"\$pkg\" | grep -oP 'linux|macos|windows'); \
        ln -sf \"\$(basename \$pkg)\" \"dl/opendesk-latest-\${os}.\${ext}\"; \
    done"

echo ""
echo "✓ Upload complete!"
echo "  Installer URL: https://your-server.com/install.sh"
echo "  (Update DOWNLOAD_BASE in install.sh to point to your server)"
