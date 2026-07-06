"""
Platform detection and system utilities.

Centralises all ``sys.platform`` checks and provides helpers for
OS-specific behaviour.
"""

from __future__ import annotations

import sys
from enum import Enum, auto


class Platform(Enum):
    """Supported desktop platforms."""

    WINDOWS = auto()
    MACOS = auto()
    LINUX = auto()
    UNKNOWN = auto()


def current_platform() -> Platform:
    """Detect the current operating system.

    Returns
    -------
    Platform
        One of ``WINDOWS``, ``MACOS``, ``LINUX``, or ``UNKNOWN``.
    """
    if sys.platform.startswith("win"):
        return Platform.WINDOWS
    if sys.platform.startswith("darwin"):
        return Platform.MACOS
    if sys.platform.startswith("linux"):
        return Platform.LINUX
    return Platform.UNKNOWN


def platform_name() -> str:
    """Human-readable platform name."""
    return current_platform().name.lower()


def is_wayland() -> bool:
    """Check if running under Wayland (Linux only).

    Returns
    -------
    bool
        ``True`` if the ``WAYLAND_DISPLAY`` environment variable is set.
    """
    if current_platform() != Platform.LINUX:
        return False
    import os
    return "WAYLAND_DISPLAY" in os.environ
