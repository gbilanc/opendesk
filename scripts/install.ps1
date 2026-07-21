# OpenDesk — Windows Installer
#
# Usage (PowerShell as Administrator):
#   iwr -useb https://your-server.com/install.ps1 | iex
#
# The script downloads the NSIS installer and runs it silently.

param(
    [string]$DownloadBase = $env:OPENDESK_DOWNLOAD_BASE,
    [string]$Version = $env:OPENDESK_VERSION
)

if (-not $DownloadBase) { $DownloadBase = "http://gibisoft.net/dl" }
if (-not $Version)     { $Version = "latest" }

$ErrorActionPreference = "Stop"

# ═══════════════════════════════════════════════════════════════════
# OS Detection
# ═══════════════════════════════════════════════════════════════════

$arch = if ([Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
Write-Host "→ Detected: Windows / $arch" -ForegroundColor Green

# ═══════════════════════════════════════════════════════════════════
# Download & Install
# ═══════════════════════════════════════════════════════════════════

$filename = "opendesk-${Version}-windows-${arch}.exe"
$url = "${DownloadBase}/${filename}"
$tmp = "$env:TEMP\opendesk-installer.exe"

Write-Host "→ Downloading OpenDesk installer..." -ForegroundColor Green
Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing

Write-Host "→ Running installer (silent)..." -ForegroundColor Green
Start-Process -FilePath $tmp -ArgumentList "/S", "/D=$env:LOCALAPPDATA\OpenDesk" -Wait

Remove-Item $tmp -Force

Write-Host ""
Write-Host "✓ OpenDesk installed successfully!" -ForegroundColor Green
Write-Host "  Find it in the Start Menu or run: $env:LOCALAPPDATA\OpenDesk\opendesk.exe" -ForegroundColor White
