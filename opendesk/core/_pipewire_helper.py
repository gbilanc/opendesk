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
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

# GStreamer initialisation must happen before any other imports
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

Gst.init(sys.argv)  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="PipeWire screen capture helper")
    parser.add_argument("--monitor", type=int, default=0, help="Monitor index")
    parser.add_argument("--width", type=int, default=0, help="Target width (0 = auto)")
    parser.add_argument("--height", type=int, default=0, help="Target height (0 = auto)")
    parser.add_argument("--fps", type=int, default=30, help="Target framerate")
    parser.add_argument("--fd", type=int, default=None, help="PipeWire FD (optional)")
    args = parser.parse_args()

    # Build pipeline
    # pipewiresrc captures the screen via PipeWire
    # If we have a node path from the portal, use it
    pipeline_str = "pipewiresrc ! videoconvert ! video/x-raw,format=RGB "
    if args.width and args.height:
        pipeline_str += f",width={args.width},height={args.height} "
    pipeline_str += "! appsink name=sink max-buffers=1 drop=true"

    pipeline = Gst.parse_launch(pipeline_str)
    sink = pipeline.get_by_name("sink")

    if not sink:
        print("ERROR: Could not create appsink", file=sys.stderr)
        sys.exit(1)

    # Set PipeWire path if provided via FD or portal
    src = pipeline.get_by_name("pipewiresrc0")
    if src and args.fd is not None:
        src.set_property("fd", args.fd)

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

        # Extract raw RGB data
        result, map_info = buf.map(Gst.MapFlags.READ)
        if not result:
            continue

        data = map_info.data  # bytes
        buf.unmap(map_info)

        # Write header (4-byte width, 4-byte height LE) on first frame
        if not sent_header:
            header = struct.pack("<II", width, height)
            sys.stdout.buffer.write(header)
            sent_header = True

        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
