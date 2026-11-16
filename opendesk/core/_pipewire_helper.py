#!/usr/bin/env python3
"""
Helper subprocess for PipeWire screen capture via GStreamer.

Called by ``PipeWireCapture`` in ``screen_capture.py`` when running on
Wayland.  Uses GStreamer's ``pipewiresrc`` element to capture the
screen and writes raw RGB frames to stdout.

Usage
-----
.. code:: bash

    # Capture monitor 0 at 1920x1080 (or auto-detect)
    python3 _pipewire_helper.py --monitor 0 --width 1920 --height 1080

    # Output: raw RGB24 frames (width * height * 3 bytes each),
    # preceded by a 4-byte little-endian width and height header
    # on the first frame only.

    # Modalità codificata (--encode): il frame non lascia mai la GPU per
    # arrivare in CPU come raw.  L'encoder (VA-API/HW quando disponibile)
    # lavora sul DMA-BUF di PipeWire e sullo stdout passano solo pacchetti
    # H.264 già compressi, con framing ``<u32 len><u8 flags><payload>``.
    python3 _pipewire_helper.py --node-id 70 --encode auto
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

# GStreamer initialisation must happen before any other imports
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(sys.argv)  # noqa: E402

# Magic del header in modalità codificata, seguito da <III: mode, width, height>.
# In modalità raw il header resta il vecchio <II: width, height> (compatibile).
_ENCODED_MAGIC = b"ODPK"
_ENCODED_MODE = 1

# Encoder H.264 preferiti, in ordine: HW (VA-API) → SW.
_ENCODERS: dict[str, str] = {
    "vah264enc": "key-int-max=60 rate-control=cbr target-usage=7",
    "vaapih264enc": "keyframe-period=60",
    "x264enc": "tune=zerolatency speed-preset=ultrafast key-int-max=60",
}


def _pick_encoder(requested: str) -> tuple[str, str] | None:
    """Ritorna (elemento, opzioni) del primo encoder H.264 disponibile."""
    candidates = [requested] if requested and requested != "auto" else list(_ENCODERS)
    for name in candidates:
        if Gst.ElementFactory.find(name) is not None:
            return name, _ENCODERS.get(name, "")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="PipeWire screen capture helper")
    parser.add_argument("--monitor", type=int, default=0, help="Monitor index")
    parser.add_argument("--width", type=int, default=0, help="Target width (0 = auto)")
    parser.add_argument("--height", type=int, default=0, help="Target height (0 = auto)")
    parser.add_argument("--fps", type=int, default=30, help="Target framerate")
    parser.add_argument(
        "--fd",
        type=int,
        default=0,
        help="PipeWire fd from xdg-desktop-portal (optional)",
    )
    parser.add_argument(
        "--node-id",
        type=int,
        default=0,
        help="PipeWire node ID from portal Start() response (preferred)",
    )
    parser.add_argument(
        "--encode",
        nargs="?",
        const="auto",
        default=None,
        help="Codifica H.264 nell'helper (zero-copy GPU→encoder): 'auto' o nome elemento GStreamer",
    )
    args = parser.parse_args()

    # Build pipeline
    # Prefer --node-id (from D-Bus portal Start) over --fd.
    # pipewiresrc path=<node_id> connects directly to the stream
    # created by the portal, without needing a pre-opened fd.
    pipewire_extra = ""
    if args.node_id and args.node_id > 0:
        pipewire_extra = f" path={args.node_id}"
    elif args.fd and args.fd > 0:
        pipewire_extra = f" fd={args.fd}"

    encoder: tuple[str, str] | None = None
    if args.encode is not None:
        encoder = _pick_encoder(args.encode)
        if encoder is None:
            print("ERROR: nessun encoder H.264 GStreamer disponibile", file=sys.stderr)
            sys.exit(1)

    if encoder is not None:
        enc_name, enc_opts = encoder
        print(f"INFO: encoding with {enc_name}", file=sys.stderr)
        pipeline_str = (
            f"pipewiresrc{pipewire_extra} ! videoconvert ! {enc_name}"
            + (f" {enc_opts}" if enc_opts else "")
            + " ! h264parse ! video/x-h264,stream-format=byte-stream,alignment=au"
            " ! appsink name=sink max-buffers=1 drop=true sync=false"
        )
    else:
        pipeline_str = f"pipewiresrc{pipewire_extra} ! videoconvert ! video/x-raw,format=RGB"
        if args.width and args.height:
            pipeline_str += f",width={args.width},height={args.height}"
        pipeline_str += " ! appsink name=sink max-buffers=1 drop=true"

    pipeline = Gst.parse_launch(pipeline_str)
    sink = pipeline.get_by_name("sink")

    if not sink:
        print("ERROR: Could not create appsink", file=sys.stderr)
        sys.exit(1)

    # Verify the source element was created (if fd was given, it's already
    # configured via the pipeline string above).
    src = pipeline.get_by_name("pipewiresrc0")
    if src is None:
        # GStreamer may name it differently depending on version
        for elem in pipeline.iterate_all_by_name("pipewiresrc0"):
            src = elem
            break
    if src is not None:
        logger_fd = args.fd if args.fd else 0
        print(f"INFO: pipewiresrc ready (fd={logger_fd})", file=sys.stderr)

    pipeline.set_state(Gst.State.PLAYING)

    # Create a bus for error handling
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    # Flag to signal first frame (we output width/height header once)
    sent_header = False
    running = True

    def on_error(bus, message) -> None:
        nonlocal running
        err, debug = message.parse_error()
        print(f"ERROR: {err}: {debug}", file=sys.stderr)
        running = False

    def on_eos(bus, message) -> None:
        nonlocal running
        running = False

    bus.connect("message::error", on_error)
    bus.connect("message::eos", on_eos)

    # Main capture loop
    while running:
        sample = sink.emit("pull-sample")
        if sample is None:
            if running:
                time.sleep(0.001)  # avoid busy-wait
            continue

        buf = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0)

        success, width = structure.get_int("width")
        success2, height = structure.get_int("height")
        if not (success and success2):
            continue

        result, map_info = buf.map(Gst.MapFlags.READ)
        if not result:
            continue

        if encoder is not None:
            # Modalità codificata: payload = access unit H.264 già compressa.
            payload = bytes(map_info.data)
            buf.unmap(map_info)
            if not sent_header:
                sys.stdout.buffer.write(
                    _ENCODED_MAGIC + struct.pack("<III", _ENCODED_MODE, width, height)
                )
                sent_header = True
            is_key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
            sys.stdout.buffer.write(struct.pack("<IB", len(payload), 1 if is_key else 0))
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
            continue

        # Extract raw RGB data
        data = map_info.data  # bytes
        buf_size = len(data)
        expected_size = width * height * 3

        buf.unmap(map_info)

        # Write header (4-byte width, 4-byte height LE) on first frame
        if not sent_header:
            header = struct.pack("<II", width, height)
            sys.stdout.buffer.write(header)
            sent_header = True

        # Handle stride (row alignment padding).
        # GStreamer may add stride padding to GPU buffers; if the
        # buffer is larger than expected, calculate stride and
        # write only the actual pixel data row by row.
        if buf_size > expected_size:
            stride = buf_size // height
            row_size = width * 3
            for y in range(height):
                row_start = y * stride
                sys.stdout.buffer.write(data[row_start : row_start + row_size])
        else:
            sys.stdout.buffer.write(data)

        sys.stdout.buffer.flush()

    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
