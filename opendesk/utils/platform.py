"""
Platform detection and system utilities.

Centralises all ``sys.platform`` checks and provides helpers for
OS-specific behaviour.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
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
    return "WAYLAND_DISPLAY" in os.environ


# ---------------------------------------------------------------------------
# Host CPU load
# ---------------------------------------------------------------------------

_CPU_SAMPLE_INTERVAL = 1.0  # s — il campionamento è memoizzato per un secondo
_cpu_last_sample_mono = 0.0
_cpu_last_value: float | None = None
_cpu_prev_times: tuple[int, int, int] | None = None


def _windows_cpu_load() -> float | None:
    """Carico CPU su Windows via ``GetSystemTimes`` (stdlib, no psutil)."""
    global _cpu_prev_times  # noqa: PLW0603 — stato del campionatore

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_uint32),
            ("dwHighDateTime", ctypes.c_uint32),
        ]

    idle = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    if not windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None

    def _value(ft: _FileTime) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    # kernel include già il tempo idle
    current = (_value(idle), _value(kernel), _value(user))
    prev = _cpu_prev_times
    _cpu_prev_times = current
    if prev is None:
        return None  # serve un delta: primo campione inutilizzabile
    delta_idle = current[0] - prev[0]
    delta_total = (current[1] - prev[1]) + (current[2] - prev[2])
    if delta_total <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - delta_idle / delta_total))


def cpu_load_percent() -> float | None:
    """Carico medio della CPU dell'host, normalizzato in ``0.0..1.0``.

    Usa ``os.getloadavg()`` su POSIX e ``GetSystemTimes`` su Windows:
    nessuna dipendenza esterna.  Il valore è memoizzato per
    ``_CPU_SAMPLE_INTERVAL`` secondi.  ``None`` significa "non
    determinabile" (es. primo campione Windows): il chiamante deve
    comportarsi come se il carico fosse sconosciuto.
    """
    global _cpu_last_sample_mono, _cpu_last_value  # noqa: PLW0603

    now = time.monotonic()
    if now - _cpu_last_sample_mono < _CPU_SAMPLE_INTERVAL:
        return _cpu_last_value
    _cpu_last_sample_mono = now

    try:
        if sys.platform.startswith("win"):
            value = _windows_cpu_load()
        else:
            cores = os.cpu_count() or 1
            value = min(1.0, os.getloadavg()[0] / cores)
    except (AttributeError, OSError):
        value = None
    _cpu_last_value = value
    return value
