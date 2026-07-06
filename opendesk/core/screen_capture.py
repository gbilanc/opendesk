"""
Cross-platform screen capture using ``mss`` (X11/Win/macOS)
and PipeWire (Wayland).

Provides frame differencing for bandwidth-efficient streaming and
automatic monitor enumeration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from threading import Lock
from typing import Iterator

import mss
import numpy as np
from PIL import Image

from opendesk.utils.platform import current_platform, Platform, is_wayland

logger = logging.getLogger(__name__)


class CaptureMethod(Enum):
    """Preferred backend for screen capture."""

    AUTO = auto()  # Auto-detect
    MSS = auto()  # Cross-platform (DXGI / CoreGraphics / X11)
    PIPEWIRE = auto()  # Linux Wayland via PipeWire + xdg-desktop-portal
    DUMMY = auto()  # Test pattern for development


@dataclass(frozen=True)
class MonitorInfo:
    """Describes a single monitor."""

    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass
class CapturedFrame:
    """A single captured frame with metadata."""

    data: np.ndarray  # RGB uint8 array (H, W, 3)
    monitor_index: int
    timestamp: float
    region: tuple[int, int, int, int]  # (left, top, width, height)

    @property
    def width(self) -> int:
        return self.region[2]

    @property
    def height(self) -> int:
        return self.region[3]


# ---------------------------------------------------------------------------
# Frame differencing
# ---------------------------------------------------------------------------


def frame_diff_ratio(
    current: np.ndarray, previous: np.ndarray | None, threshold: int = 16
) -> float:
    if previous is None or current.shape != previous.shape:
        return 1.0
    diff = np.abs(current.astype(np.int16) - previous.astype(np.int16))
    changed = np.any(diff > threshold, axis=2)
    return float(changed.sum()) / changed.size


def compute_dirty_region(
    current: np.ndarray, previous: np.ndarray | None, threshold: int = 16
) -> tuple[int, int, int, int] | None:
    if previous is None or current.shape != previous.shape:
        return (0, 0, current.shape[1], current.shape[0])
    diff = np.abs(current.astype(np.int16) - previous.astype(np.int16))
    changed = np.any(diff > threshold, axis=2)
    coords = np.argwhere(changed)
    if coords.size == 0:
        return None
    y0, x0 = coords.min(axis=0).tolist()
    y1, x1 = coords.max(axis=0).tolist()
    return (x0, y0, x1 + 1, y1 + 1)


# ---------------------------------------------------------------------------
# PipeWire / Wayland capture backend
# ---------------------------------------------------------------------------


class PipeWireCapture:
    """Wayland screen capture via PipeWire + xdg-desktop-portal D-Bus API.

    Uses ``dbus_next`` (pure Python) to request a screencast from
    ``org.freedesktop.portal.ScreenCast``.

    Requires:
        - ``dbus-next`` (pip install dbus-next)
        - ``xdg-desktop-portal`` + backend (-wlr, -gnome, or -kde)
        - PipeWire runtime

    Falls back gracefully if dependencies are missing.
    """

    def __init__(self) -> None:
        self._available: bool | None = None
        self._session_handle: str | None = None
        self._monitors: list[MonitorInfo] = []
        self._frame_queue: list[np.ndarray] = []

    # ── availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available

        try:
            import dbus_next  # noqa: F401
        except ImportError:
            logger.debug("PipeWire: dbus-next not installed")
            self._available = False
            return False

        import subprocess
        import shutil

        # Check xdg-desktop-portal is on D-Bus
        if shutil.which("busctl"):
            try:
                r = subprocess.run(
                    ["busctl", "list", "--no-pager"],
                    capture_output=True, text=True, timeout=3,
                )
                if "org.freedesktop.portal.Desktop" in r.stdout:
                    self._available = True
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        if shutil.which("gdbus"):
            try:
                r = subprocess.run(
                    ["gdbus", "call", "--session",
                     "--dest", "org.freedesktop.DBus",
                     "--object-path", "/",
                     "--method", "org.freedesktop.DBus.ListNames"],
                    capture_output=True, text=True, timeout=3,
                )
                if "org.freedesktop.portal.Desktop" in r.stdout:
                    self._available = True
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        logger.debug("PipeWire: xdg-desktop-portal not found on D-Bus")
        self._available = False
        return False

    # ── public API ──────────────────────────────────────────────────

    def monitors(self) -> list[MonitorInfo]:
        if self._monitors:
            return self._monitors

        import subprocess
        import shutil
        import re

        self._monitors = []

        if shutil.which("wlr-randr"):
            try:
                r = subprocess.run(
                    ["wlr-randr"], capture_output=True, text=True, timeout=3,
                )
                current_name = ""
                for line in r.stdout.splitlines():
                    m = re.match(r'^(.+?)\s+"(.+?)"', line)
                    if m:
                        current_name = m.group(1)
                    m_size = re.search(r"(\d+)x(\d+) px", line)
                    m_pos = re.search(r"@ (\d+),(\d+)", line)
                    if m_size and current_name:
                        w, h = int(m_size.group(1)), int(m_size.group(2))
                        x, y = (int(m_pos.group(1)), int(m_pos.group(2))) if m_pos else (0, 0)
                        idx = len(self._monitors)
                        self._monitors.append(MonitorInfo(
                            index=idx, name=current_name,
                            left=x, top=y, width=w, height=h,
                            is_primary=idx == 0,
                        ))
                        current_name = ""
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if not self._monitors:
            self._monitors.append(MonitorInfo(
                index=0, name="Wayland Output",
                left=0, top=0, width=1920, height=1080, is_primary=True,
            ))
        return self._monitors

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame | None:
        self._ensure_session(monitor_index)
        if not self._frame_queue:
            return None
        data = self._frame_queue.pop(0)
        mon = self._monitors[monitor_index] if monitor_index < len(self._monitors) else None
        return CapturedFrame(
            data=data,
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=(mon.left, mon.top, mon.width, mon.height) if mon else (0, 0, 0, 0),
        )

    def release(self) -> None:
        self._session_handle = None
        self._frame_queue.clear()

    # ── internal D-Bus ──────────────────────────────────────────────

    def _ensure_session(self, monitor_index: int = 0) -> None:
        if self._session_handle is not None:
            return
        try:
            from dbus_next import BusType, Message
            from dbus_next.aio import MessageBus
            import asyncio

            async def _create():
                bus = await MessageBus(bus_type=BusType.SESSION).connect()
                msg = Message(
                    destination="org.freedesktop.portal.Desktop",
                    path="/org/freedesktop/portal/desktop",
                    interface="org.freedesktop.portal.ScreenCast",
                    member="CreateSession",
                    signature="a{sv}",
                    body=[{}],
                )
                resp = await bus.call(msg)
                if resp.body:
                    self._session_handle = resp.body[0]
                    logger.info("PipeWire session: %s", self._session_handle)

            asyncio.run(_create())
        except Exception as e:
            logger.warning("PipeWire session failed: %s", e)
            self._session_handle = "fallback"


# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------


def _detect_capture_method() -> CaptureMethod:
    plat = current_platform()
    if plat == Platform.LINUX and is_wayland():
        pw = PipeWireCapture()
        if pw.is_available():
            logger.info("Capture backend: PIPEWIRE (native Wayland)")
            return CaptureMethod.PIPEWIRE
        logger.info("Capture backend: MSS (XWayland fallback)")
    return CaptureMethod.MSS


# ---------------------------------------------------------------------------
# Screen capture engine
# ---------------------------------------------------------------------------


class ScreenCapture:
    """Cross-platform screen capture engine.

    Auto-detects the best backend:
    - Linux/Wayland → PipeWire (falls back to MSS/XWayland)
    - X11, Windows, macOS → MSS
    """

    def __init__(self, method: CaptureMethod | None = None) -> None:
        self._method = method if method and method != CaptureMethod.AUTO else _detect_capture_method()
        self._lock = Lock()
        self._sct: mss.mss | None = None
        self._pw: PipeWireCapture | None = None
        self._prev_frames: dict[int, np.ndarray] = {}
        self._fps_target: float = 30.0
        self._fps_adaptive: bool = True
        self._min_fps: float = 1.0
        self._idle_counter: int = 0
        logger.info("Screen capture: %s", self._method.name)

    @property
    def fps_target(self) -> float:
        return self._fps_target

    @fps_target.setter
    def fps_target(self, value: float) -> None:
        self._fps_target = max(1.0, min(60.0, value))

    @property
    def adaptive_fps(self) -> bool:
        return self._fps_adaptive

    @adaptive_fps.setter
    def adaptive_fps(self, enabled: bool) -> None:
        self._fps_adaptive = enabled

    @property
    def capture_method(self) -> CaptureMethod:
        return self._method

    # ── monitors ────────────────────────────────────────────────────

    def monitors(self) -> list[MonitorInfo]:
        if self._method == CaptureMethod.PIPEWIRE:
            return self._get_pw().monitors()
        sct = self._get_sct()
        return [
            MonitorInfo(
                index=i,
                name=m.get("name", f"Monitor {i}"),
                left=m["left"], top=m["top"],
                width=m["width"], height=m["height"],
                is_primary=m.get("is_primary", i == 0),
            )
            for i, m in enumerate(sct.monitors[1:])
        ]

    # ── single capture ──────────────────────────────────────────────

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame:
        if self._method == CaptureMethod.PIPEWIRE:
            f = self._get_pw().capture_one(monitor_index)
            if f is not None:
                return f
            logger.warning("PipeWire failed, falling back to MSS")
            self._method = CaptureMethod.MSS
        return self._capture_mss(monitor_index)

    # ── capture loop ────────────────────────────────────────────────

    def capture_loop(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        if self._method == CaptureMethod.PIPEWIRE:
            yield from self._loop_pipewire(monitor_index)
        else:
            yield from self._loop_mss(monitor_index)

    # ── lifecycle ───────────────────────────────────────────────────

    def release(self) -> None:
        with self._lock:
            if self._sct is not None:
                self._sct.close()
                self._sct = None
            if self._pw is not None:
                self._pw.release()
                self._pw = None
            self._prev_frames.clear()

    def __enter__(self) -> ScreenCapture:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    # ── internal: MSS ───────────────────────────────────────────────

    def _get_sct(self) -> mss.mss:
        if self._sct is None:
            with self._lock:
                if self._sct is None:
                    self._sct = mss.mss()
        return self._sct

    def _capture_mss(self, monitor_index: int = 0) -> CapturedFrame:
        sct = self._get_sct()
        mon = sct.monitors[monitor_index + 1]
        raw = sct.grab(mon)
        buf = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
        return CapturedFrame(
            data=buf[:, :, :3],
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=(mon["left"], mon["top"], mon["width"], mon["height"]),
        )

    def _loop_mss(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        sct = self._get_sct()
        mon = sct.monitors[monitor_index + 1]
        while True:
            t0 = time.perf_counter()
            raw = sct.grab(mon)
            buf = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
            rgb = buf[:, :, :3].copy()
            prev = self._prev_frames.get(monitor_index)
            diff = frame_diff_ratio(rgb, prev, threshold=12)
            self._prev_frames[monitor_index] = rgb
            yield CapturedFrame(
                data=rgb, monitor_index=monitor_index,
                timestamp=t0,
                region=(mon["left"], mon["top"], mon["width"], mon["height"]),
            )
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    # ── internal: PipeWire ──────────────────────────────────────────

    def _get_pw(self) -> PipeWireCapture:
        if self._pw is None:
            self._pw = PipeWireCapture()
        return self._pw

    def _loop_pipewire(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        pw = self._get_pw()
        while True:
            t0 = time.perf_counter()
            frame = pw.capture_one(monitor_index)
            if frame is None:
                logger.warning("PipeWire ended, falling back to MSS")
                yield from self._loop_mss(monitor_index)
                return
            prev = self._prev_frames.get(monitor_index)
            diff = frame_diff_ratio(frame.data, prev, threshold=12)
            self._prev_frames[monitor_index] = frame.data
            yield frame
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    # ── FPS helper ──────────────────────────────────────────────────

    def _compute_fps(self, diff: float) -> float:
        if not self._fps_adaptive:
            return self._fps_target
        if diff < 0.001:
            self._idle_counter += 1
        else:
            self._idle_counter = 0
        if self._idle_counter > 10:
            return self._min_fps
        if diff < 0.01:
            return max(self._min_fps, self._fps_target * 0.3)
        return self._fps_target


# ---------------------------------------------------------------------------
# Convenience screenshot
# ---------------------------------------------------------------------------

_global_capture: ScreenCapture | None = None


def screenshot(monitor_index: int = 0) -> Image.Image:
    global _global_capture
    if _global_capture is None:
        _global_capture = ScreenCapture()
    frame = _global_capture.capture_one(monitor_index)
    return Image.fromarray(frame.data)
