"""
Caps Lock / keyboard modifier state detection.

Provides a cross-platform helper to check whether Caps Lock is
currently active.  Uses Xlib on X11, Win32 API on Windows, and
falls back to a subprocess call on other platforms.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform-specific checkers
# ---------------------------------------------------------------------------

_Checker = Callable[[], bool] | None
_checker: _Checker = None


def _check_x11() -> bool:
    """Check Caps Lock via Xlib (X11 / Linux)."""
    try:
        from Xlib import display as xdisplay  # type: ignore[import-untyped]
        d = xdisplay.Display()
        state = d.get_keyboard_control()._data["led_mask"]  # noqa: SLF001
        d.close()
        # Bit 0 → Caps Lock, Bit 1 → Num Lock, Bit 2 → Scroll Lock
        return bool(state & 1)
    except Exception:
        logger.debug("Xlib Caps Lock check failed, falling back", exc_info=True)
        return _check_subprocess()


def _check_win32() -> bool:
    """Check Caps Lock via Win32 API."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:
        logger.debug("Win32 Caps Lock check failed", exc_info=True)
        return False


def _check_subprocess() -> bool:
    """Fallback: parse ``xset -q`` output (X11)."""
    try:
        out = subprocess.check_output(
            ["xset", "-q"], stderr=subprocess.STDOUT, timeout=2, text=True,
        )
        for line in out.splitlines():
            if "Caps Lock" in line:
                return "on" in line.lower()
    except Exception:
        logger.debug("xset -q Caps Lock check failed", exc_info=True)
    return False


def _init_checker() -> _Checker:
    """Select the best Caps Lock checker for the current platform."""
    system = platform.system()
    if system == "Windows":
        return _check_win32
    # Linux / X11 — try Xlib first, fall back to xset
    try:
        from Xlib import display  # noqa: F401
        return _check_x11
    except ImportError:
        pass
    # Fallback: subprocess
    try:
        subprocess.check_output(["xset", "-q"], stderr=subprocess.DEVNULL, timeout=1)
        return _check_subprocess
    except Exception:
        pass
    logger.warning("No Caps Lock detection available on this platform")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def caps_lock_active() -> bool:
    """Return ``True`` if Caps Lock is currently on."""
    global _checker  # noqa: PLW0603
    if _checker is None:
        _checker = _init_checker()
    if _checker is None:
        return False
    try:
        return _checker()
    except Exception:
        logger.debug("Caps Lock check failed", exc_info=True)
        return False
