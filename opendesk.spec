# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for OpenDesk.

Produces standalone executables for Linux, Windows, and macOS.

Build:
    pyinstaller opendesk.spec --clean

Output:
    dist/opendesk/opendesk          (Linux)
    dist/opendesk/opendesk.exe      (Windows)
    dist/opendesk/OpenDesk.app      (macOS bundle)
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Project root ───────────────────────────────────────────────────
ROOT = Path(SPECPATH)
RESOURCES = ROOT / "opendesk" / "ui" / "resources"

# ── Datas (non-Python files to bundle) ─────────────────────────────
datas = []
if RESOURCES.is_dir():
    for f in RESOURCES.iterdir():
        if f.is_file():
            datas.append((str(f), f"opendesk/ui/resources"))

# ── Hidden imports ─────────────────────────────────────────────────
hiddenimports = [
    # PySide6 Qt modules
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtDBus",
    # OpenDesk
    "opendesk",
    "opendesk.app",
    "opendesk.host_app",
    "opendesk.core",
    "opendesk.core.input_injection",
    "opendesk.core.screen_capture",
    "opendesk.core.platform_config",
    "opendesk.core.video_codec",
    "opendesk.core.audio_manager",
    "opendesk.core.camera_manager",
    "opendesk.core.clipboard_sync",
    "opendesk.core.file_transfer",
    "opendesk.core.keyboard_state",
    "opendesk.core.screen_recorder",
    "opendesk.core.unattended",
    "opendesk.core.benchmark",
    "opendesk.core.device_registry",
    "opendesk.core._pipewire_helper",
    "opendesk.core.wayland_capture",
    "opendesk.crypto",
    "opendesk.crypto.auth",
    "opendesk.crypto.challenge",
    "opendesk.crypto.e2ee",
    "opendesk.network",
    "opendesk.network.nat_traversal",
    "opendesk.network.protocol",
    "opendesk.network.relay_client",
    "opendesk.services",
    "opendesk.services.connection_service",
    "opendesk.services.pipeline",
    "opendesk.services.stream_service",
    "opendesk.ui",
    "opendesk.ui.main_window",
    "opendesk.ui.connections",
    "opendesk.ui.viewer",
    "opendesk.ui.chat_panel",
    "opendesk.ui.file_transfer_ui",
    "opendesk.ui.monitor_selector",
    "opendesk.ui.session_info",
    "opendesk.ui.settings_dialog",
    "opendesk.ui.widgets",
    "opendesk.ui.widgets.empty_state_widget",
    "opendesk.ui.widgets.health_status",
    "opendesk.ui.widgets.status_badge",
    "opendesk.ui.widgets.toast_notification",
    "opendesk.utils",
    "opendesk.utils.logger",
    "opendesk.utils.platform",
    # Third-party
    "nacl",
    "nacl.bindings",
    "argon2",
    "cryptography",
    "cryptography.hazmat.primitives",
    "av",
    "av.codec",
    "av.container",
    "av.format",
    "av.stream",
    "av.filter",
    "aiortc",
    "aiortc.rtcrtpsender",
    "aiortc.rtcrtpreceiver",
    "zmq",
    "zmq.backend.cython",
    "websockets",
    "msgpack",
    "mss",
    "PIL",
    "PIL.Image",
    "numpy",
    "cv2",
    "cv2.videoio_registry",
    "soundcard",
    "soundcard.mediafoundation",
    "soundcard.pulseaudio",
    "soundcard.coreaudio",
]

# ── Platform-specific ──────────────────────────────────────────────
if sys.platform.startswith("linux"):
    hiddenimports += [
        "Xlib",
        "Xlib.ext",
        "Xlib.ext.xtest",
        "Xlib.display",
        "evdev",
        "evdev.ecodes",
    ]
elif sys.platform.startswith("darwin"):
    hiddenimports += [
        "Quartz",
        "Quartz.CoreGraphics",
        "Quartz.CoreVideo",
    ]
elif sys.platform.startswith("win"):
    hiddenimports += [
        "win32api",
        "win32gui",
        "win32con",
        "win32clipboard",
        "ctypes.wintypes",
    ]

# ── Analysis ───────────────────────────────────────────────────────
a = Analysis(
    [str(ROOT / "opendesk" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pdb", "doctest", "ensurepip"],
    noarchive=False,
)

# ── PYZ ────────────────────────────────────────────────────────────
pyz = PYZ(a.pure)

# ── EXE ────────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="opendesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ── macOS .app bundle ──────────────────────────────────────────────
if sys.platform.startswith("darwin"):
    app = BUNDLE(
        exe,
        name="OpenDesk.app",
        icon=str(RESOURCES / "opendesk.svg") if (RESOURCES / "opendesk.svg").is_file() else None,
        bundle_identifier="io.opendesk.client",
        info_plist={
            "CFBundleName": "OpenDesk",
            "CFBundleDisplayName": "OpenDesk",
            "CFBundleIdentifier": "io.opendesk.client",
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "opendesk",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )

# ── COLLECT ────────────────────────────────────────────────────────
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="opendesk",
)
