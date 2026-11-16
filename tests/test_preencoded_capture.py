"""Test della modalità pre-encoded (encoding H.264 dentro l'helper PipeWire).

Coprono i due pezzi indipendenti dal sistema (nessun GStreamer richiesto):
- il framing del protocollo helper → ``WaylandScreenCast.capture_packet_sync``
- il passthrough ``EncodedPacket`` in ``EncoderWorker`` (nessun encoder PyAV)
"""

from __future__ import annotations

import os
import queue
import struct
import threading
import time

import pytest

from opendesk.core.video_codec import EncodedPacket


class _FakeHelper:
    """Sostituto del subprocess helper: espone stdout (pipe reale) + poll()."""

    def __init__(self, payload: bytes, alive: bool = True) -> None:
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)  # EOF dopo il payload, come un helper terminato
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self._alive = alive

    def poll(self) -> int | None:
        return None if self._alive else 0


def _encoded_header(w: int, h: int) -> bytes:
    return b"ODPK" + struct.pack("<III", 1, w, h)


def _packet(payload: bytes, keyframe: bool) -> bytes:
    return struct.pack("<IB", len(payload), 1 if keyframe else 0) + payload


class TestPacketFraming:
    def _screencast(self, payload: bytes, alive: bool = True):
        pytest.importorskip("dbus_next")
        from opendesk.core.wayland_capture import WaylandScreenCast

        wsc = WaylandScreenCast(encoded=True)
        wsc._helper_process = _FakeHelper(payload, alive=alive)  # type: ignore[assignment]
        return wsc

    def test_reads_packets_with_keyframe_flag(self) -> None:
        wsc = self._screencast(_packet(b"keydata", True) + _packet(b"delta", False))
        assert wsc.capture_packet_sync() == (b"keydata", True)
        assert wsc.capture_packet_sync() == (b"delta", False)

    def test_eof_returns_none(self) -> None:
        wsc = self._screencast(_packet(b"x", True))
        assert wsc.capture_packet_sync() is not None
        assert wsc.capture_packet_sync() is None

    def test_rejects_absurd_length(self) -> None:
        wsc = self._screencast(struct.pack("<IB", 10**9, 1))
        assert wsc.capture_packet_sync() is None

    def test_helper_alive_reflects_poll(self) -> None:
        assert self._screencast(b"").helper_alive is True
        assert self._screencast(b"", alive=False).helper_alive is False


class TestPipelinePassthrough:
    def test_encoder_worker_forwards_without_encoding(self) -> None:
        from opendesk.services.pipeline import EncoderWorker, PipelineConfig

        stop = threading.Event()
        frames: queue.Queue = queue.Queue()
        pkts: queue.Queue = queue.Queue()
        sent: list[tuple] = []

        worker = EncoderWorker(
            PipelineConfig(preencoded=True),
            frames,
            pkts,
            stop,
            on_send_full_keyframe=lambda *a: sent.append(a),
        )
        worker.start()
        frame = EncodedPacket(
            data=b"\x00\x00\x00\x01idr", pts=123, is_keyframe=True, width=64, height=32
        )
        frames.put((frame, time.time()))
        deadline = time.monotonic() + 5.0
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        worker.join(timeout=5.0)

        assert sent, "il pacchetto pre-encoded non è stato inoltrato"
        data, w, h, pts, is_key = sent[0]
        assert (data, w, h, pts, is_key) == (b"\x00\x00\x00\x01idr", 64, 32, 123, True)
        assert not pkts.empty()
