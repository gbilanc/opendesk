"""
Video encoding and decoding using PyAV (FFmpeg bindings).

Provides H.264 encoding with adaptive quality, delta-frame support,
and bandwidth-aware bitrate control.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Lock

import numpy as np

from opendesk.core.screen_capture import CapturedFrame

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quality presets
# ---------------------------------------------------------------------------


class QualityLevel(Enum):
    """Predefined quality / bandwidth trade-off levels."""

    LOW = auto()  # ~0.5 Mbps — good for slow connections
    MEDIUM = auto()  # ~2 Mbps — balanced
    HIGH = auto()  # ~8 Mbps — good image quality
    LOSSLESS = auto()  # ~20+ Mbps — near-lossless (LAN)


_QUALITY_BITRATE: dict[QualityLevel, int] = {
    QualityLevel.LOW: 500_000,
    QualityLevel.MEDIUM: 2_000_000,
    QualityLevel.HIGH: 8_000_000,
    QualityLevel.LOSSLESS: 20_000_000,
}


@dataclass
class EncoderConfig:
    """Configuration for the H.264 encoder."""

    width: int
    height: int
    fps: float = 30.0
    bitrate: int = 2_000_000  # bps
    quality: QualityLevel = QualityLevel.MEDIUM
    gop_size: int = 60  # keyframe interval (in frames)
    pixel_format: str = "yuv420p"


@dataclass
class EncodedPacket:
    """A single encoded video packet."""

    data: bytes
    pts: int  # presentation timestamp
    is_keyframe: bool
    width: int
    height: int
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


class VideoEncoder:
    """H.264 encoder wrapping PyAV.

    Usage::

        enc = VideoEncoder(width=1920, height=1080)
        packet = enc.encode(rgb_frame)
        enc.release()
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self._config = config or EncoderConfig(width=1280, height=720)
        self._lock = Lock()
        self._container: Any = None  # noqa: ANN401
        self._stream: Any = None  # noqa: ANN401
        self._pts: int = 0
        self._initialised = False
        self._actual_bitrate: int = self._config.bitrate

    # ── properties ──────────────────────────────────────────────────

    @property
    def config(self) -> EncoderConfig:
        return self._config

    @property
    def actual_bitrate(self) -> int:
        return self._actual_bitrate

    @actual_bitrate.setter
    def actual_bitrate(self, value: int) -> None:
        """Dynamically adjust bitrate (rounded to reasonable bounds)."""
        self._actual_bitrate = max(100_000, min(50_000_000, value))
        if self._initialised:
            self._reinitialise()
        logger.debug("Bitrate adjusted to %d bps", self._actual_bitrate)

    def set_quality(self, level: QualityLevel) -> None:
        """Set a predefined quality level and adjust bitrate."""
        self._config.quality = level
        self.actual_bitrate = _QUALITY_BITRATE[level]

    # ── encoding ────────────────────────────────────────────────────

    def encode(self, frame: np.ndarray) -> list[EncodedPacket]:
        """Encode an RGB (H, W, 3) numpy array into H.264 packets.

        Parameters
        ----------
        frame : np.ndarray
            RGB uint8 image.

        Returns
        -------
        list[EncodedPacket]
            Encoded packets (usually 1, but can be multiple per frame).
        """
        self._ensure_initialised(frame.shape[1], frame.shape[0])
        packets: list[EncodedPacket] = []

        # Convert RGB → YUV420P
        yuv = self._rgb_to_yuv(frame)

        # Create a VideoFrame from the YUV planes
        av_frame = self._make_av_frame(yuv)

        for packet in self._stream.encode(av_frame):
            packets.append(self._packet_from_av(packet))

        return packets

    def flush(self) -> list[EncodedPacket]:
        """Flush remaining packets from the encoder.

        Call this at the end of a stream.
        """
        packets: list[EncodedPacket] = []
        if self._stream is not None:
            for packet in self._stream.encode(None):
                packets.append(self._packet_from_av(packet))
        return packets

    def request_keyframe(self) -> None:
        """Force the next frame to be a keyframe."""
        if self._stream is not None:
            self._stream.codec_context.force_keyframe = True

    def release(self) -> None:
        """Release encoder resources."""
        with self._lock:
            if self._container is not None:
                self._container.close()
                self._container = None
                self._stream = None
                self._initialised = False
            logger.info("Video encoder released")

    # ── internal ────────────────────────────────────────────────────

    def _ensure_initialised(self, width: int, height: int) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return

            import av

            self._container = av.open(
                "pipe:", mode="w", format="h264",  # no file, just in-memory
            )
            self._stream = self._container.add_stream("h264", rate=self._config.fps)
            self._stream.width = width
            self._stream.height = height
            self._stream.pix_fmt = "yuv420p"
            self._stream.bit_rate = self._actual_bitrate
            self._stream.gop_size = self._config.gop_size
            self._stream.max_b_frames = 0  # lower latency
            self._stream.options = {
                "preset": "ultrafast",  # low latency
                "tune": "zerolatency",
                "profile": "baseline",
            }
            self._config.width = width
            self._config.height = height
            self._initialised = True
            logger.info(
                "Encoder initialised: %dx%d @ %.1f fps, %d bps",
                width, height, self._config.fps, self._actual_bitrate,
            )

    def _reinitialise(self) -> None:
        """Re-create the encoder with new settings."""
        if self._container is not None:
            self._container.close()
        self._initialised = False
        self._pts = 0
        self._ensure_initialised(self._config.width, self._config.height)

    def _rgb_to_yuv(self, rgb: np.ndarray) -> np.ndarray:
        """Convert RGB (H, W, 3) uint8 to YUV420P planar.

        Uses OpenCV for fast conversion.
        """
        import cv2

        # OpenCV uses BGR internally, but cvtColor handles RGB→YUV
        yuv = cv2.cvtColor(rgb, cv2.COLOR_RGB2YUV)
        # YUV420 planar: full Y, quarter U, quarter V
        h, w = yuv.shape[:2]
        y = yuv[:, :, 0]
        u = yuv[::2, ::2, 1]
        v = yuv[::2, ::2, 2]
        # Pack planes contiguously
        return np.ascontiguousarray(np.concatenate([y.ravel(), u.ravel(), v.ravel()]))

    def _make_av_frame(self, yuv_planes: np.ndarray) -> Any:  # noqa: ANN401
        """Build an av.VideoFrame from a YUV420P byte array."""
        import av

        h, w = self._config.height, self._config.width
        frame = av.VideoFrame(w, h, "yuv420p")
        y_size = w * h
        u_size = (w // 2) * (h // 2)
        frame.planes[0].update(yuv_planes[:y_size])
        frame.planes[1].update(yuv_planes[y_size : y_size + u_size])
        frame.planes[2].update(yuv_planes[y_size + u_size :])
        frame.pts = self._pts
        self._pts += 1
        return frame

    def _packet_from_av(self, packet: Any) -> EncodedPacket:  # noqa: ANN401
        """Convert an av.Packet to an EncodedPacket."""
        return EncodedPacket(
            data=bytes(packet),
            pts=packet.pts or 0,
            is_keyframe=packet.is_keyframe,
            width=self._config.width,
            height=self._config.height,
        )

    def __enter__(self) -> VideoEncoder:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


class VideoDecoder:
    """H.264 decoder wrapping PyAV.

    Usage::

        dec = VideoDecoder()
        frame = dec.decode(packet.data, packet.width, packet.height)
    """

    def __init__(self) -> None:
        self._codec: Any = None  # noqa: ANN401
        self._lock = Lock()

    def decode(self, data: bytes, width: int, height: int) -> np.ndarray | None:
        """Decode an H.264 packet into an RGB numpy array.

        Returns
        -------
        np.ndarray or None
            RGB uint8 (H, W, 3) or ``None`` if not enough data yet.
        """
        import av

        with self._lock:
            if self._codec is None:
                self._codec = av.CodecContext.create("h264", "r")
                self._codec.width = width
                self._codec.height = height
                self._codec.pix_fmt = "yuv420p"

            packets = av.Packet(data)
            frames = self._codec.decode(packets)
            if not frames:
                return None

            av_frame = frames[-1]
            rgb = av_frame.to_rgb().to_ndarray()
            return rgb

    def release(self) -> None:
        with self._lock:
            self._codec = None
