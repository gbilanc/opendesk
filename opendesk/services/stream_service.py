"""Servizio di streaming — capture, encode e invio frame.

Gestisce:
- ScreenCapture (PipeWire / MSS)
- VideoEncoder (H.264)
- InputBackend (remote input injection)
- Stream timer e bandwidth adaptation
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot

from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.core.video_codec import VideoEncoder, EncoderConfig, QualityLevel, _QUALITY_BITRATE
from opendesk.core.input_injection import (
    InputBackend,
    MouseButton,
    KeyState,
    create_input_backend,
)
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayClient

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# StreamService
# ═══════════════════════════════════════════════════════════════════


class StreamService(QObject):
    """Servizio di streaming video.

    Signals::

        frame_ready(rgb: np.ndarray, width: int, height: int)
        bitrate_changed(kbps: float)
        error(error_msg: str)
    """

    frame_ready = Signal(object, int, int)  # np.ndarray, width, height
    bitrate_changed = Signal(float)
    error = Signal(str)

    def __init__(self, relay: RelayClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._relay = relay
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # Capture
        self._capture: ScreenCapture | None = None
        self._capture_running = False
        self._encoder: VideoEncoder | None = None
        self._input_backend: InputBackend | None = None

        # Timers
        self._stream_timer = QTimer(self)
        self._stream_timer.timeout.connect(self._capture_and_send)
        self._bw_timer = QTimer(self)
        self._bw_timer.timeout.connect(self._update_bitrate)

        # Bandwidth estimation
        self._bw_measure_bytes: int = 0
        self._bw_measure_time: float = 0.0
        self._bw_estimated_kbps: float = 0.0

        # React when the remote peer requests a keyframe
        self._relay.host_keyframe_requested.connect(self._force_keyframe)

    # ── properties ──────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._capture_running

    @property
    def input_backend(self) -> InputBackend | None:
        return self._input_backend

    @property
    def capture(self) -> ScreenCapture | None:
        return self._capture

    # ── streaming lifecycle ─────────────────────────────────────────

    def _lazy_init_encoder(self, width: int, height: int) -> bool:
        """Create the encoder on first successful capture, if not already done."""
        if self._encoder is not None:
            return True
        try:
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "MEDIUM")
            quality = getattr(QualityLevel, quality_name, QualityLevel.MEDIUM)
            self._encoder = VideoEncoder(
                EncoderConfig(
                    width=width,
                    height=height,
                    fps=fps,
                    bitrate=_QUALITY_BITRATE[quality],
                    quality=quality,
                )
            )
            logger.info("Encoder lazy-init: %dx%d @ %s", width, height, quality_name)
            return True
        except Exception as e:
            logger.warning("Encoder init failed: %s", e)
            return False

    def start_streaming(self) -> None:
        """Avvia la cattura schermo, encoding e streaming."""
        try:
            self._capture = ScreenCapture()
            self._capture_running = True

            # Input backend (non bloccante)
            try:
                self._input_backend = create_input_backend()
            except Exception as e:
                logger.warning("Input backend unavailable — remote input disabled: %s", e)
                self._input_backend = None
                # Emit a non-fatal error so the UI can show a warning
                self.error.emit(f"Remote input disabled: {e}")

            # Reset bandwidth
            self._bw_measure_bytes = 0
            self._bw_measure_time = time.time()
            self._bw_estimated_kbps = 0.0

            # Settings
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "MEDIUM")
            quality = getattr(QualityLevel, quality_name, QualityLevel.MEDIUM)

            # Capture first frame — if it succeeds, init encoder immediately
            first = self._capture.capture_one(0)
            if first is not None:
                self._lazy_init_encoder(first.width, first.height)
            else:
                logger.warning(
                    "First capture returned None — encoder will be initialised "
                    "lazily on the first successful capture"
                )

            # Start timers (always, even if encoder not yet ready)
            self._stream_timer.start(int(1000 / fps))
            self._bw_timer.start(3000)
            logger.info("Streaming started at %d FPS", fps)
        except Exception as e:
            logger.exception("Failed to start streaming: %s", e)
            self.error.emit(str(e))
            self.stop_streaming()

    def stop_streaming(self) -> None:
        """Ferma cattura, encoding e timer."""
        if not self._capture_running and self._capture is None and self._encoder is None:
            return  # already stopped
        self._stream_timer.stop()
        self._bw_timer.stop()
        self._capture_running = False
        if self._capture:
            self._capture.release()
            self._capture = None
        if self._encoder:
            self._encoder.release()
            self._encoder = None
        if self._input_backend:
            self._input_backend.release()
            self._input_backend = None
        self._bw_measure_bytes = 0
        logger.info("Streaming stopped")

    @Slot()
    def _force_keyframe(self) -> None:
        """Force the next video frame to be a keyframe (IDR).

        Called when the remote peer signals that it needs a fresh
        keyframe to re-sync the video decoder.
        """
        if self._encoder is not None:
            self._encoder.request_keyframe()
            logger.info("Forcing keyframe on next encode per peer request")

    # ── capture / encoder ───────────────────────────────────────────

    @Slot()
    def _capture_and_send(self) -> None:
        """Cattura un frame, codifica e invia via relay."""
        if not self._capture_running or self._capture is None:
            return
        try:
            frame = self._capture.capture_one(0)
            if frame is None:
                return

            # Lazy encoder initialisation — if the first capture returned
            # None, the encoder may not exist yet.  Create it now.
            if self._encoder is None:
                if not self._lazy_init_encoder(frame.width, frame.height):
                    return

            pts = int(frame.timestamp * 1000)
            packets = self._encoder.encode(frame.data)
            for pkt in packets:
                self._relay.send_frame(
                    pkt.data, pkt.width, pkt.height, pts, keyframe=pkt.is_keyframe,
                )
                self._bw_measure_bytes += len(pkt.data)
        except Exception as e:
            logger.warning("Capture/encode error: %s", e)

    # ── bandwidth adaptation ────────────────────────────────────────

    @Slot()
    def _update_bitrate(self) -> None:
        """Aggiorna il bitrate in base alla banda misurata."""
        if not self._encoder or not self._capture_running:
            return

        now = time.time()
        elapsed = now - self._bw_measure_time
        if elapsed < 2.0 or self._bw_measure_bytes < 1024:
            return

        measured_kbps = (self._bw_measure_bytes * 8) / (elapsed * 1000)
        self._bw_measure_bytes = 0
        self._bw_measure_time = now

        if self._bw_estimated_kbps == 0:
            self._bw_estimated_kbps = measured_kbps
        else:
            self._bw_estimated_kbps = self._bw_estimated_kbps * 0.7 + measured_kbps * 0.3

        target_bitrate = int(self._bw_estimated_kbps * 1000 * 0.8)
        target_bitrate = max(100_000, min(50_000_000, target_bitrate))

        current = self._encoder.actual_bitrate
        if abs(target_bitrate - current) > current * 0.2:
            logger.info("Adaptive bitrate: %.0f kbps → %d kbps", self._bw_estimated_kbps, target_bitrate // 1000)
            self._encoder.actual_bitrate = target_bitrate
            self.bitrate_changed.emit(self._bw_estimated_kbps)

    # ── input injection ─────────────────────────────────────────────

    def inject_mouse(self, msg: Message) -> None:
        """Inietta un evento mouse (chiamato dal relay)."""
        if self._input_backend is None:
            return
        payload = msg.payload
        x = payload.get("x", 0)
        y = payload.get("y", 0)
        button = payload.get("button")
        pressed = payload.get("pressed")
        absolute = payload.get("absolute", True)

        if button:
            btn = MouseButton(button)
            state = KeyState.PRESSED if pressed else KeyState.RELEASED
            self._input_backend.move_mouse(x, y, absolute)
            self._input_backend.click_mouse(btn, state)
        else:
            self._input_backend.move_mouse(x, y, absolute)

    def inject_keyboard(self, msg: Message) -> None:
        """Inietta un evento tastiera (chiamato dal relay)."""
        if self._input_backend is None:
            return
        payload = msg.payload
        key = payload.get("key", "")
        pressed = payload.get("pressed", False)
        state = KeyState.PRESSED if pressed else KeyState.RELEASED
        if key:
            self._input_backend.key_event(key, state)
