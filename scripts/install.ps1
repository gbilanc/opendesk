# ---------------------------------------------------------------------------
# OpenDesk — Legacy Windows installer (redirects to bootstrap.ps1)
#
# Kept for backward compatibility.  New installations should use:
#   iwr -useb https://opendesk.io/bootstrap.ps1 | iex
# ---------------------------------------------------------------------------

Write-Host "⚠ This install script is deprecated." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Use the new bootstrap installer instead:"
Write-Host ""
Write-Host "  iwr -useb https://opendesk.io/bootstrap.ps1 | iex"
Write-Host ""

# Delegate to bootstrap.ps1 in the same directory
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrapPath = Join-Path $scriptDir "bootstrap.ps1"

if (Test-Path $bootstrapPath) {
    & $bootstrapPath @args
} else {
    Write-Host ""
    Write-Host "  Downloading bootstrap.ps1 ..."
    iex (iwr -useb "https://raw.githubusercontent.com/opendesk/opendesk-client/main/scripts/bootstrap.ps1")
}
