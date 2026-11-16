#!/usr/bin/env python3
"""Misura il costo per stadio del path capture → encode (piano zero-copy, step 1).

Registra, per N frame catturati dal backend indicato:

  capture   -> ``ScreenCapture.capture_one()`` (grab + conversioni + cursore)
  diff      -> ``_changed_mask`` + scansione dei tile 128x128 (fase 1 di
               ``EncoderWorker._do_tiles``)
  jpeg      -> codifica JPEG dei soli tile cambiati (fase 3)
  h264      -> encode full-frame via ``VideoEncoder`` (path keyframe)

Usage:
    .venv/bin/python tools/measure_capture_encode.py --backend MSS --frames 60
    .venv/bin/python tools/measure_capture_encode.py --backend PIPEWIRE --frames 120
    .venv/bin/python tools/measure_capture_encode.py --backend MSS --json out.json

Note:
    - PIPEWIRE/PORTAL mostrano il dialog di selezione schermo: serve approvarlo.
    - Con ``--no-encode`` misura solo la cattura (utile su macchine senza
      encoder HW, dove l'H.264 software domina il tempo).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from opendesk.core.platform_config import CaptureMethod  # noqa: E402
from opendesk.core.screen_capture import ScreenCapture, _changed_mask  # noqa: E402
from opendesk.core.video_codec import (  # noqa: E402
    EncoderConfig,
    QualityLevel,
    VideoEncoder,
)

logger = logging.getLogger("measure")

_TILE = 128  # come services/pipeline.py::_TILE_SIZE
_TILE_CHANGE_RATIO = 0.0005
_JPEG_Q = 80
_DIFF_THRESHOLD = 16


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def _cpu() -> float:
    """CPU time del processo (thread inclusi): separa il costo reale
    dall'attesa bloccante su pipe/portal."""
    t = time.process_time()
    return t


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "p95": 0.0, "mean": 0.0, "n": 0}
    return {
        "median": statistics.median(values),
        "p95": _pct(values, 95),
        "mean": statistics.fmean(values),
        "n": len(values),
    }


def _tile_boxes(mask, w: int, h: int):
    """Fase 1 di ``EncoderWorker._do_tiles``: tile cambiati via cv2.integral."""
    nx = (w + _TILE - 1) // _TILE
    ny = (h + _TILE - 1) // _TILE
    m8 = mask.view(np.uint8)
    if ny * _TILE != h or nx * _TILE != w:
        pad = np.zeros((ny * _TILE, nx * _TILE), dtype=np.uint8)
        pad[:h, :w] = m8
        m8 = pad
    ii = cv2.integral(m8)
    ys = np.arange(ny) * _TILE
    xs = np.arange(nx) * _TILE
    ye = np.minimum(ys + _TILE, h)
    xe = np.minimum(xs + _TILE, w)
    counts = ii[np.ix_(ye, xe)] - ii[np.ix_(ys, xe)] - ii[np.ix_(ye, xs)] + ii[np.ix_(ys, xs)]
    areas = np.outer(ye - ys, xe - xs)
    rows, cols = np.nonzero(counts > _TILE_CHANGE_RATIO * areas)
    return [
        (int(c) * _TILE, int(r) * _TILE, int(xe[c] - xs[c]), int(ye[r] - ys[r]))
        for r, c in zip(rows, cols)
    ]


def measure(backend: CaptureMethod, frames: int, encode: bool) -> dict:
    cap = ScreenCapture(method=backend)
    try:
        monitors = cap.monitors()
        if not monitors:
            raise RuntimeError("nessun monitor enumerato")
        mon = monitors[0]
        w, h = mon.size
        frame_bytes = w * h * 3
        logger.info(
            "Backend %s, monitor %s %dx%d (%.1f MB/frame raw)",
            backend.name,
            mon.name,
            w,
            h,
            frame_bytes / 1e6,
        )

        prev = None
        t_capture: list[float] = []
        c_capture: list[float] = []
        c_diff: list[float] = []
        c_jpeg: list[float] = []
        c_h264: list[float] = []
        t_diff: list[float] = []
        t_jpeg: list[float] = []
        t_h264: list[float] = []
        changed: list[int] = []
        tile_bytes: list[int] = []
        pkt_bytes: list[int] = []

        encoder = None
        if encode:
            encoder = VideoEncoder(
                EncoderConfig(
                    width=w,
                    height=h,
                    fps=30,
                    quality=QualityLevel.HIGH,
                    crf=23,
                )
            )
            logger.info("Encoder: %s", encoder.codec_name)

        errors = 0
        attempts = 0
        t_loop_start = time.perf_counter()
        actual_wh = None
        max_attempts = frames * 5
        while len(t_capture) < frames and attempts < max_attempts:
            attempts += 1
            t0 = time.perf_counter()
            c0 = _cpu()
            try:
                frame = cap.capture_one(0)
            except Exception as e:  # noqa: BLE001
                errors += 1
                logger.warning("capture error: %s", e)
                time.sleep(0.05)
                continue
            t_capture.append((time.perf_counter() - t0) * 1000)
            c_capture.append((_cpu() - c0) * 1000)

            if frame is None:  # backend senza nuovi frame (DXGI()) o portal in attesa
                time.sleep(0.005)
                continue

            cur = frame.data
            if actual_wh is None:
                actual_wh = (cur.shape[1], cur.shape[0])
                frame_bytes = cur.shape[0] * cur.shape[1] * 3

            # ── diff + tile detection ──
            t0 = time.perf_counter()
            c0 = _cpu()
            mask = _changed_mask(cur, prev, _DIFF_THRESHOLD)
            boxes = [(0, 0, w, h)] if mask is None else _tile_boxes(mask, cur.shape[1], cur.shape[0])
            t_diff.append((time.perf_counter() - t0) * 1000)
            c_diff.append((_cpu() - c0) * 1000)
            changed.append(len(boxes))

            # ── JPEG dei tile cambiati ──
            if encode:
                t0 = time.perf_counter()
                c0 = _cpu()
                size = 0
                for x, y, tw, th in boxes:
                    ok, buf = cv2.imencode(
                        ".jpg",
                        cv2.cvtColor(cur[y : y + th, x : x + tw], cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q],
                    )
                    if ok:
                        size += buf.nbytes
                t_jpeg.append((time.perf_counter() - t0) * 1000)
                c_jpeg.append((_cpu() - c0) * 1000)
                tile_bytes.append(size)

            # ── H.264 full frame ──
            if encode and encoder is not None:
                t0 = time.perf_counter()
                c0 = _cpu()
                try:
                    pkts = encoder.encode(cur)
                    pkt_bytes.append(sum(p.data.nbytes if hasattr(p.data, "nbytes") else len(p.data) for p in pkts))
                except Exception as e:  # noqa: BLE001
                    logger.warning("encode error: %s", e)
                t_h264.append((time.perf_counter() - t0) * 1000)
                c_h264.append((_cpu() - c0) * 1000)

            prev = cur

        if encoder is not None:
            encoder.release()
    finally:
        cap.release()

    elapsed = time.perf_counter() - t_loop_start
    if actual_wh:  # il backend può consegnare una risoluzione diversa da monitors()
        w, h = actual_wh
    result = {
        "backend": backend.name,
        "monitor": {"width": w, "height": h, "bytes_per_frame": frame_bytes},
        "fps_effective": len(t_capture) / elapsed if elapsed else 0,
        "frames": len(t_capture),
        "capture_errors": errors,
        "capture_ms": _stats(t_capture),
        "diff_ms": _stats(t_diff),
        "jpeg_ms": _stats(t_jpeg),
        "h264_ms": _stats(t_h264),
        "cpu_ms": {
            "capture": _stats(c_capture),
            "diff": _stats(c_diff),
            "jpeg": _stats(c_jpeg),
            "h264": _stats(c_h264),
        },
        "tiles_changed_median": statistics.median(changed) if changed else 0,
        "tile_jpeg_bytes_median": statistics.median(tile_bytes) if tile_bytes else 0,
        "h264_pkt_bytes_median": statistics.median(pkt_bytes) if pkt_bytes else 0,
    }
    result["raw_ram_mbps_capture"] = (
        frame_bytes * result["frames"] / sum(t_capture) / 1000 if t_capture and sum(t_capture) else 0
    )
    return result


def _print(result: dict) -> None:
    mon = result["monitor"]
    print(f"\n=== {result['backend']}  {mon['width']}x{mon['height']}  "
          f"({mon['bytes_per_frame']/1e6:.1f} MB/frame raw) ===")
    print(f"frames misurati: {result['frames']} (errori: {result['capture_errors']})")
    cpu = result["cpu_ms"]
    for stage, key, ckey in (
        ("capture (grab+conv+cursore)", "capture_ms", "capture"),
        ("diff + tile-detect", "diff_ms", "diff"),
        ("JPEG tile cambiati", "jpeg_ms", "jpeg"),
        ("H.264 full frame", "h264_ms", "h264"),
    ):
        s = result[key]
        if s["n"]:
            cm = cpu[ckey]["median"]
            print(
                f"  {stage:30s} wall med {s['median']:7.2f} ms  p95 {s['p95']:7.2f}"
                f"  | CPU med {cm:6.2f} ms  ({cm/s['median']*100 if s['median'] else 0:.0f}% del wall)"
            )
    print(f"  tile cambiati (mediana)      {result['tiles_changed_median']:.0f}")
    print(f"  payload JPEG (mediana)       {result['tile_jpeg_bytes_median']/1024:.1f} KB")
    print(f"  payload H.264 (mediana)      {result['h264_pkt_bytes_median']/1024:.1f} KB")
    print(f"  traffico RAM cattura         {result['raw_ram_mbps_capture']:.0f} MB/s")
    print(f"  fps effettivo (sorgente)     {result.get('fps_effective', 0):.1f}")
    total = sum(result[k]["median"] for k in ("capture_ms", "diff_ms", "jpeg_ms") if result[k]["n"])
    if total:
        print(f"  TOTALE path tile (mediana)   {total:.2f} ms/frame  → max ~{1000/total:.0f} fps")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="MSS", help="MSS | PIPEWIRE | PORTAL | DXGI | AUTO")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--no-encode", action="store_true", help="misura solo la cattura")
    ap.add_argument("--json", help="scrive il risultato in JSON")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(message)s")

    try:
        backend = CaptureMethod[args.backend.upper()]
    except KeyError:
        print(f"backend non valido: {args.backend}", file=sys.stderr)
        return 2

    result = measure(backend, args.frames, not args.no_encode)
    _print(result)

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
