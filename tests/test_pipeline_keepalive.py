"""Heartbeat idle dell'EncoderWorker.

A schermo fermo (o desktop host bloccato) non ci sono tile da inviare:
senza heartbeat il client interpreta il silenzio come stream morto e
martella di richieste keyframe da ~340 KB.  Verifica che parta un tile
1×1 circa ogni secondo e che non ci siano raffiche.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from opendesk.services.pipeline import (
    _IDLE_KEEPALIVE_INTERVAL,
    EncoderWorker,
    PipelineConfig,
    QualityLevel,
)


def make_worker() -> tuple[EncoderWorker, queue.Queue]:
    config = PipelineConfig(fps=30, quality=QualityLevel.MEDIUM, bitrate=2_000_000)
    frame_queue: queue.Queue = queue.Queue()
    pkt_queue: queue.Queue = queue.Queue()
    worker = EncoderWorker(
        config,
        frame_queue,
        pkt_queue,
        threading.Event(),
        on_send_tile=lambda *args: None,
    )
    worker._prev_frame = np.zeros((4, 4, 3), dtype=np.uint8)
    return worker, pkt_queue


def test_keepalive_requires_interval() -> None:
    worker, pkt_queue = make_worker()
    worker._maybe_send_keepalive()
    assert pkt_queue.empty(), "heartbeat immediato senza attesa"

    worker._last_packet_mono = time.monotonic() - _IDLE_KEEPALIVE_INTERVAL - 0.1
    worker._maybe_send_keepalive()
    packet = pkt_queue.get_nowait()
    assert packet[0] == "tile"
    assert packet[2:6] == (0, 0, 1, 1), "il heartbeat deve essere un tile 1×1 a (0,0)"
    assert packet[1][:2] == b"\xff\xd8", "tile non JPEG"
    assert len(packet[1]) < 2000, "heartbeat troppo grande"

    worker._maybe_send_keepalive()
    assert pkt_queue.empty(), "heartbeat non throttled"


def test_keepalive_fires_in_idle_loop() -> None:
    worker, pkt_queue = make_worker()
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        time.sleep(_IDLE_KEEPALIVE_INTERVAL * 2 + 0.7)
    finally:
        worker._stop_event.set()
        thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert pkt_queue.qsize() >= 2, f"heartbeat assenti: {pkt_queue.qsize()}"
