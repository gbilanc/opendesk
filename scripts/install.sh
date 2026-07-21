#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Universal installer (Linux & macOS)
#
# Usage:
#   curl -fsSL https://your-server.com/install.sh | bash
#
# The script auto-detects the OS, downloads the correct package, and
# installs it with all required system dependencies.
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────
DOWNLOAD_BASE="${OPENDESK_DOWNLOAD_BASE:-http://gibisoft.net/dl}"
VERSION="${OPENDESK_VERSION:-latest}"
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

# ── Helpers ────────────────────────────────────────────────────────
info()  { echo -e "${GREEN}→${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*"; }
err()   { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }

# ── OS Detection ───────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux)  OS="linux" ;;
        Darwin) OS="macos" ;;
        *)      err "Unsupported OS: $(uname -s)" ;;
    esac

    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  ARCH="x86_64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)       err "Unsupported architecture: $ARCH" ;;
    esac

    info "Detected: $OS / $ARCH"
}

# ── Linux helpers ──────────────────────────────────────────────────
install_linux_deps() {
    info "Installing system dependencies..."

    # Don't fail if sudo is not available or user cancels
    set +e
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq 2>/dev/null || true
        sudo apt-get install -y -qq \
            ffmpeg \
            libx11-6 \
            libxext6 \
            libxrender1 \
            libxtst6 \
            pipewire \
            gstreamer1.0-pipewire \
            python3-gi \
            2>/dev/null || warn "Some dependencies may not have installed (non-critical)"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y \
            ffmpeg \
            libX11 \
            libXext \
            libXrender \
            libXtst \
            pipewire \
            gstreamer1-pipewire \
            2>/dev/null || warn "Some dependencies may not have installed (non-critical)"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm --needed \
            ffmpeg \
            libx11 \
            libxext \
            libxrender \
            libxtst \
            pipewire \
            gst-plugin-pipewire \
            2>/dev/null || warn "Some dependencies may not have installed (non-critical)"
    else
        warn "Could not detect package manager. Install manually: ffmpeg libx11 libxext libxtst"
    fi
    set -e
}

install_linux_package() {
    local appimage_url="$1"
    local tarball_url="$2"
    local tmpfile

    # Try AppImage first
    info "Downloading OpenDesk..."
    if curl -fSL --progress-bar -o /tmp/opendesk-$$.AppImage "$appimage_url" 2>/dev/null; then
        tmpfile="/tmp/opendesk-$$.AppImage"
        chmod +x "$tmpfile"
        mkdir -p "$HOME/.local/bin"
        info "Installing to ~/.local/bin/opendesk ..."
        mv "$tmpfile" "$HOME/.local/bin/opendesk"
    elif curl -fSL --progress-bar -o /tmp/opendesk-$$.tar.gz "$tarball_url" 2>/dev/null; then
        tmpfile="/tmp/opendesk-$$.tar.gz"
        info "Extracting..."
        mkdir -p "$HOME/.local/opendesk"
        tar -xzf "$tmpfile" -C "$HOME/.local/opendesk"
        rm -f "$tmpfile"
        # Create symlink
        mkdir -p "$HOME/.local/bin"
        ln -sf "$HOME/.local/opendesk/opendesk/opendesk" "$HOME/.local/bin/opendesk"
        # Copy icon
        if [ -f "$HOME/.local/opendesk/opendesk/_internal/opendesk/ui/resources/opendesk.svg" ]; then
            cp "$HOME/.local/opendesk/opendesk/_internal/opendesk/ui/resources/opendesk.svg" \
               "$HOME/.local/share/icons/hicolor/256x256/apps/opendesk.svg" 2>/dev/null || true
        fi
    else
        err "Download failed. Check your internet connection and try again."
    fi

    # Desktop entry
    mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/256x256/apps"
    info "Creating desktop entry..."
    cat > "$HOME/.local/share/applications/opendesk.desktop" << 'DESKTOP'
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

    info "${BOLD}OpenDesk installed successfully!${RESET}"
    info "Run with: ${BOLD}opendesk${RESET}"
    info "Or find it in your application menu."
}

# ── macOS helpers ──────────────────────────────────────────────────
install_dmg() {
    local url="$1"
    local tmpdmg="/tmp/opendesk-$$.dmg"

    info "Downloading OpenDesk DMG..."
    curl -fSL --progress-bar -o "$tmpdmg" "$url" || err "Download failed"

    info "Mounting DMG..."
    local mount_point
    mount_point=$(hdiutil attach "$tmpdmg" -nobrowse -readonly -mountpoint /dev/stdout 2>/dev/null | tail -1)

    if [ ! -d "$mount_point" ]; then
        # Fallback: let macOS choose mount point
        hdiutil attach "$tmpdmg" -nobrowse -readonly
        mount_point="/Volumes/OpenDesk"
    fi

    info "Copying to /Applications ..."
    if [ -d "/Applications/OpenDesk.app" ]; then
        rm -rf "/Applications/OpenDesk.app"
    fi
    cp -R "$mount_point/OpenDesk.app" /Applications/

    hdiutil detach "$mount_point" 2>/dev/null || true
    rm -f "$tmpdmg"

    info "${BOLD}OpenDesk installed successfully!${RESET}"
    info "Find it in your Applications folder or Launchpad."
}

# ── Main ───────────────────────────────────────────────────────────
main() {
    echo ""
    echo -e "${BOLD}OpenDesk Installer${RESET}"
    echo "==================="
    echo ""

    detect_os

    if [ "$OS" = "linux" ]; then
        APPIMAGE_URL="${DOWNLOAD_BASE}/opendesk-${VERSION}-linux-${ARCH}.AppImage"
        TARBALL_URL="${DOWNLOAD_BASE}/opendesk-${VERSION}-linux-${ARCH}.tar.gz"
        install_linux_deps
        install_linux_package "$APPIMAGE_URL" "$TARBALL_URL"
    elif [ "$OS" = "macos" ]; then
        FILENAME="opendesk-${VERSION}-macos-${ARCH}.dmg"
        URL="${DOWNLOAD_BASE}/${FILENAME}"
        install_dmg "$URL"
    fi
}

main "$@"
