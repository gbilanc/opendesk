#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenDesk — Legacy installer (redirects to bootstrap.sh)
#
# Kept for backward compatibility.  New installations should use:
#   curl -fsSL https://opendesk.io/bootstrap.sh | bash
# ---------------------------------------------------------------------------

set -euo pipefail

BOLD="\033[1m"
YELLOW="\033[33m"
RESET="\033[0m"

echo ""
echo -e "${YELLOW}⚠ This install script is deprecated.${RESET}"
echo ""
echo -e "  Use the new bootstrap installer instead:"
echo ""
echo -e "  ${BOLD}curl -fsSL https://opendesk.io/bootstrap.sh | bash${RESET}"
echo ""

# Delegate to bootstrap.sh in the same directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/bootstrap.sh" ]; then
    exec bash "$SCRIPT_DIR/bootstrap.sh" "$@"
fi

echo ""
echo "  Downloading bootstrap.sh ..."
curl -fsSL https://raw.githubusercontent.com/opendesk/opendesk-client/main/scripts/bootstrap.sh | bash "$@"
