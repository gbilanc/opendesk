#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Bootstrap installer (Linux / macOS)
#
# Usage:
#   curl -fsSL https://opendesk.io/bootstrap.sh | bash
#
# What it does:
#   1. Ensures Python 3.12+ is available (installs via pyenv if missing)
#   2. Installs system dependencies (ffmpeg, libxtst, pipewire, etc.)
#   3. Installs OpenDesk via pip (or pipx if available)
#   4. Verifies installation
# ---------------------------------------------------------------------------

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()  { echo -e "${GREEN}→${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠${RESET} $*"; }
err()   { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }
section() {
    echo ""
    echo -e "${BOLD}━━━ $* ━━━${RESET}"
}

# ── Config ────────────────────────────────────────────────────────
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=12

# ── Helper: version compare ───────────────────────────────────────
version_ge() {
    # Returns 0 if $1 >= $2 (both in "major.minor" form)
    local IFS=.
    local -a v1=($1) v2=($2)
    (( v1[0] > v2[0] || (v1[0] == v2[0] && v1[1] >= v2[1]) ))
}

# ── OS detection ───────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux)  OS="linux" ;;
        Darwin) OS="macos" ;;
        *)      err "Unsupported OS: $(uname -s)" ;;
    esac

    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64|amd64) ARCH="x86_64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)            warn "Unknown architecture: $ARCH (assuming x86_64)"; ARCH="x86_64" ;;
    esac

    info "Detected: $OS / $ARCH"
}

# ── Python check ───────────────────────────────────────────────────
ensure_python() {
    section "Python"

    local python_cmd=""
    for candidate in python3 python3.13 python3.12; do
        if command -v "$candidate" &>/dev/null; then
            python_cmd="$candidate"
            break
        fi
    done

    if [ -z "$python_cmd" ]; then
        warn "Python 3.12+ not found."
        echo ""
        echo "  Install Python 3.12+ via one of:"
        echo "    • pyenv:   curl https://pyenv.run | bash && pyenv install 3.12"
        echo "    • conda:   https://docs.conda.io/en/latest/miniconda.html"
        echo "    • system:  sudo apt install python3.12 python3.12-venv"
        echo ""
        echo "  Then re-run this script."
        echo ""
        if [ "$OS" = "macos" ]; then
            echo "  On macOS: brew install python@3.12"
            echo ""
        fi
        err "Python 3.12+ is required."
    fi

    local py_version
    py_version=$("$python_cmd" --version 2>&1 | grep -oP '\d+\.\d+')
    if ! version_ge "$py_version" "${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}"; then
        err "Python $py_version found, but ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ is required."
    fi

    info "Python $py_version found: $python_cmd"

    # Check pip availability
    if ! "$python_cmd" -m pip --version &>/dev/null; then
        info "Installing pip..."
        "$python_cmd" -m ensurepip --upgrade
    fi

    PYTHON="$python_cmd"
}

# ── System dependencies ────────────────────────────────────────────
install_system_deps() {
    section "System Dependencies"

    if [ "$OS" = "linux" ]; then
        if command -v apt-get &>/dev/null; then
            info "Detected apt (Debian/Ubuntu/Mint)..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq \
                ffmpeg \
                libx11-6 libxext6 libxrender1 libxtst6 \
                pipewire gstreamer1.0-pipewire \
                python3-gi xdg-desktop-portal 2>/dev/null || true
        elif command -v dnf &>/dev/null; then
            info "Detected dnf (Fedora/RHEL)..."
            sudo dnf install -y \
                ffmpeg \
                libX11 libXext libXrender libXtst \
                pipewire gstreamer1-pipewire 2>/dev/null || true
        elif command -v pacman &>/dev/null; then
            info "Detected pacman (Arch Linux)..."
            sudo pacman -S --noconfirm --needed \
                ffmpeg \
                libx11 libxext libxrender libxtst \
                pipewire gst-plugin-pipewire 2>/dev/null || true
        else
            warn "Could not detect package manager."
            warn "Install manually: ffmpeg libx11 libxext libxrender libxtst pipewire"
        fi
    elif [ "$OS" = "macos" ]; then
        if command -v brew &>/dev/null; then
            info "Detected Homebrew..."
            brew install ffmpeg 2>/dev/null || true
        else
            warn "Homebrew not found. Install ffmpeg manually: https://ffmpeg.org/"
        fi
    fi

    info "System dependencies check complete."
}

# ── Install OpenDesk ───────────────────────────────────────────────
install_opendesk() {
    section "Installing OpenDesk"

    # Prefer pipx for isolated installations
    if command -v pipx &>/dev/null; then
        info "Using pipx (isolated install) ..."
        if pipx list 2>/dev/null | grep -q "opendesk"; then
            pipx upgrade opendesk
        else
            pipx install opendesk
        fi
        info "OpenDesk installed via pipx!"
        info "Run with: opendesk"
    else
        info "Using pip ..."
        $PYTHON -m pip install --upgrade pip --quiet
        $PYTHON -m pip install opendesk --quiet
        info "OpenDesk installed via pip!"
        info "Run with: opendesk"
        warn "Tip: Install pipx for isolated app management: pip install pipx"
    fi
}

# ── Verify ─────────────────────────────────────────────────────────
verify() {
    section "Verification"

    local cmd=""
    for c in opendesk opendesk-host; do
        if command -v "$c" &>/dev/null; then
            cmd="$c"
            break
        fi
    done

    if [ -n "$cmd" ]; then
        local ver
        ver=$("$cmd" --version 2>/dev/null || "$cmd" --help 2>&1 | head -1 || echo "installed")
        info "OpenDesk is ready: $cmd ($ver)"
    else
        warn "OpenDesk not found in PATH."
        warn "Try running: $PYTHON -m opendesk"
    fi
}

# ── Post-install: desktop entry ───────────────────────────────────
setup_desktop_entry() {
    section "Desktop Entry"

    # OpenDesk creates its own .desktop file on first run,
    # but we can do it here so it's ready immediately.
    $PYTHON -c "
from opendesk.app import _ensure_desktop_entry
_ensure_desktop_entry()
" 2>/dev/null && info "Desktop entry created." || warn "Desktop entry skipped (will be created on first run)."
}

# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║     OpenDesk Bootstrap Installer     ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════╝${RESET}"
    echo ""

    detect_os
    ensure_python
    install_system_deps
    install_opendesk
    setup_desktop_entry
    verify

    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${GREEN}✓ OpenDesk installed successfully!${RESET}"
    echo -e "${BOLD}  Run: opendesk${RESET}"
    echo ""
}

main
