# ---------------------------------------------------------------------------
# OpenDesk — Bootstrap installer (Windows)
#
# Usage (PowerShell as Administrator):
#   iwr -useb https://opendesk.io/bootstrap.ps1 | iex
#
# What it does:
#   1. Ensures Python 3.12+ is available (suggests download if missing)
#   2. Installs OpenDesk via pip
#   3. Creates Start Menu shortcuts
#   4. Verifies installation
# ---------------------------------------------------------------------------

$Host.UI.RawUI.WindowTitle = "OpenDesk Installer"

$Bold   = @{ForegroundColor = "White"}
$Green  = @{ForegroundColor = "Green"}
$Yellow = @{ForegroundColor = "Yellow"}
$Red    = @{ForegroundColor = "Red"}

function Info  { Write-Host "→" @Green; Write-Host " $args" @Green }
function Warn  { Write-Host "⚠" @Yellow; Write-Host " $args" @Yellow }
function Err   { Write-Host "✗" @Red -NoNewline; Write-Host " $args" @Red; exit 1 }
function Section {
    Write-Host ""
    Write-Host "━━━ $args ━━━" @Bold
}

# ═══════════════════════════════════════════════════════════════════
# Python check
# ═══════════════════════════════════════════════════════════════════

Section "Python"

$python = $null

# Look for python3 then python
foreach ($cmd in @("python3", "python")) {
    $ver = & $cmd --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver -match "(\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 12) {
            $python = $cmd
            break
        }
    }
}

if (-not $python) {
    # Try py launcher
    $ver = & py --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver -match "(\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 12) {
            $python = "py"
        }
    }
}

if (-not $python) {
    Warn "Python 3.12+ not found."
    Write-Host ""
    Write-Host "  Download Python 3.12+ from: https://www.python.org/downloads/"
    Write-Host ""
    Write-Host "  Make sure to check 'Add Python to PATH' during installation."
    Write-Host ""
    Write-Host "  Or install via winget:"
    Write-Host "    winget install Python.Python.3.12"
    Write-Host ""
    $choice = Read-Host "  Press Enter after installing Python, or type 'exit' to quit"
    if ($choice -eq "exit") { exit 1 }

    # Retry
    foreach ($cmd in @("python3", "python", "py")) {
        $ver = & $cmd --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -match "(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 12) {
                $python = $cmd
                break
            }
        }
    }

    if (-not $python) {
        Err "Python 3.12+ is required. Please install it and re-run this script."
    }
}

$pyVer = & $python --version 2>&1
Info "Found: $pyVer ($python)"

# ═══════════════════════════════════════════════════════════════════
# System dependencies (ffmpeg)
# ═══════════════════════════════════════════════════════════════════

Section "System Dependencies"

$ffmpeg = $null
try { $ffmpeg = Get-Command ffmpeg -ErrorAction Stop } catch {}

if (-not $ffmpeg) {
    Warn "ffmpeg not found in PATH."
    Write-Host ""
    Write-Host "  OpenDesk needs ffmpeg for video encoding."
    Write-Host ""
    Write-Host "  Install via winget:"
    Write-Host "    winget install ffmpeg"
    Write-Host ""
    Write-Host "  Or download from: https://ffmpeg.org/download.html"
    Write-Host "  (Add ffmpeg.exe to your PATH after extracting)"
    Write-Host ""
    $choice = Read-Host "  Press Enter after installing ffmpeg, or type 'skip' to continue anyway"
    if ($choice -eq "skip") {
        Warn "Skipping ffmpeg check. Some features may not work."
    }
} else {
    Info "ffmpeg found: $($ffmpeg.Source)"
}

# ═══════════════════════════════════════════════════════════════════
# Install OpenDesk
# ═══════════════════════════════════════════════════════════════════

Section "Installing OpenDesk"

Info "Upgrading pip..."
& $python -m pip install --upgrade pip --quiet

Write-Host ""
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Info "Using pipx (isolated install)..."
    $installed = pipx list 2>$null | Select-String "opendesk"
    if ($installed) {
        pipx upgrade opendesk
    } else {
        pipx install opendesk
    }
} else {
    Info "Using pip..."
    & $python -m pip install opendesk --quiet
    Warn "Tip: Install pipx for isolated app management: pip install pipx"
}

# ═══════════════════════════════════════════════════════════════════
# Create Start Menu shortcut
# ═══════════════════════════════════════════════════════════════════

Section "Start Menu Shortcut"

$opendeskCmd = Get-Command opendesk -ErrorAction SilentlyContinue
if (-not $opendeskCmd) {
    # Try via python -m
    $opendeskCmd = $python
    $opendeskArgs = "-m opendesk"
} else {
    $opendeskArgs = ""
}

if ($opendeskCmd) {
    $startMenu = [Environment]::GetFolderPath("StartMenu")
    $shortcutDir = "$startMenu\Programs\OpenDesk"
    $shortcutPath = "$shortcutDir\OpenDesk.lnk"

    if (-not (Test-Path $shortcutDir)) {
        New-Item -ItemType Directory -Path $shortcutDir -Force | Out-Null
    }

    if (-not (Test-Path $shortcutPath)) {
        $WScriptShell = New-Object -ComObject WScript.Shell
        $shortcut = $WScriptShell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $opendeskCmd.Source
        if ($opendeskArgs) { $shortcut.Arguments = $opendeskArgs }
        $shortcut.Description = "OpenDesk Remote Desktop"
        $shortcut.WorkingDirectory = "$env:LOCALAPPDATA\OpenDesk"
        $shortcut.Save()
        Info "Shortcut created: $shortcutPath"
    } else {
        Info "Shortcut already exists."
    }
} else {
    Warn "Could not find opendesk command. Shortcut skipped."
}

# ═══════════════════════════════════════════════════════════════════
# Verify
# ═══════════════════════════════════════════════════════════════════

Section "Verification"

$cmd = Get-Command opendesk -ErrorAction SilentlyContinue
if ($cmd) {
    Info "OpenDesk is ready: $($cmd.Source)"
} else {
    Warn "OpenDesk not found in PATH. Try: python -m opendesk"
}

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" @Bold
Write-Host "✓ OpenDesk installed successfully!" @Green
Write-Host "  Run: opendesk" @Bold
Write-Host ""
