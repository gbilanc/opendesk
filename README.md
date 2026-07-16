# OpenDesk

Multi-platform remote desktop application (TeamViewer / AnyDesk-like).

- **Platforms:** Windows, macOS, Linux
- **Tech:** Python 3.12+, PySide6 (Qt6), PyAV (FFmpeg), E2E encryption
- **Network:** TCP relay with P2P support
- **Features:** Screen sharing, remote control, file transfer, clipboard sync,
  audio, chat, multi-monitor

## Quick start (uv — recommended)

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and run
cd opendesk
uv sync          # creates venv + installs deps
uv run opendesk  # start the app
```

## Alternative (pip)

```bash
pip install -e .
opendesk
```

## Development

```bash
uv sync --dev          # install with dev dependencies
uv run pytest          # run tests (123 tests)
uv run pytest -v       # verbose
uv run black .         # format code
uv run ruff check .    # lint
uv run mypy opendesk/  # type check
```

### Optional features

```bash
# Wayland support (Linux)
uv sync --extra wayland

# Audio streaming
uv sync --extra audio

# macOS input backend
uv sync --extra macos
```

### Wayland setup (Linux)

Wayland requires both **Python packages** and **system packages**:

```bash
# 1. Python dependencies
uv sync --extra wayland      # installs dbus-next, evdev

# 2. System packages (Ubuntu/Debian)
sudo apt install gstreamer1.0-pipewire python3-gi       \
                 xdg-desktop-portal pipewire

# Optional: accurate absolute mouse positioning
sudo apt install ydotool

# Required: uinput permissions for remote input
sudo usermod -aG input $USER
# (log out and back in)
```

**Supported backends** (auto-detected in order):

| Backend | Capture | Input | Notes |
|---------|---------|-------|-------|
| **PORTAL** | D-Bus + GStreamer | — | Reuses portal session, no double dialog |
| **PIPEWIRE** | GStreamer pipewiresrc | — | Shows its own screen-selection dialog |
| **MSS** | X11 | X11 (Xlib) | Fallback via XWayland |
| **uinput** | — | evdev uinput | Requires `input` group |
| **ydotool** | — | ydotool | Absolute mouse on Wayland |

## Architecture

```
opendesk/
├── opendesk/          # Main application (45 files, ~12k LOC)
│   ├── core/          # Screen capture, input, codec, audio, recording
│   ├── network/       # Protocol, P2P, relay, NAT traversal
│   ├── crypto/        # E2E encryption (NaCl Box), Argon2 auth
│   ├── services/      # Streaming pipeline, connection service
│   ├── ui/            # PySide6 widgets + QSS themes (light/dark)
│   └── utils/         # Logging, platform detection
├── tests/             # 123 tests — unit, integration, edge cases
└── uv.lock            # Locked dependencies
```

## Video encoding

OpenDesk uses **PyAV** (FFmpeg bindings) for H.264/H.265 video encoding
with hardware acceleration support.

### Quality presets

| Level | CRF | Bitrate (legacy) | Use case |
|-------|-----|-------------------|----------|
| **LOW** | 32 | ~0.5 Mbps | Slow connections |
| **MEDIUM** | 27 | ~2 Mbps | Balanced |
| **HIGH** (default) | 23 | ~8 Mbps | Good quality |
| **LOSSLESS** | 16 | ~20+ Mbps | LAN / near-lossless |

CRF (Constant Rate Factor) is the default rate control mode, providing
consistent visual quality by dynamically allocating bits where needed.

### Codec support

OpenDesk supports multiple codecs, auto-detected in order of preference:

| Codec | Type | When available |
|-------|------|----------------|
| `hevc_nvenc` | HW (NVIDIA) | NVIDIA GPU + drivers |
| `h264_nvenc` | HW (NVIDIA) | NVIDIA GPU + drivers |
| `hevc_amf` | HW (AMD) | AMD GPU + drivers |
| `h264_amf` | HW (AMD) | AMD GPU + drivers |
| `hevc_vaapi` | HW (Intel/AMD) | VAAPI drivers (Linux) |
| `h264_vaapi` | HW (Intel/AMD) | VAAPI drivers (Linux) |
| `hevc_videotoolbox` | HW (Apple) | macOS |
| `h264` (libx264) | SW | Always available |

Select the codec in **Tools → Settings → Video → Encoder**.

### Resolution scaling

Reduce resolution before encoding to save bandwidth
(**Tools → Settings → Video → Resolution**):

- **Full (1:1)** — maximum quality
- **75%, 50%, 25%** — for slower connections

Scaling before encoding is more effective than lowering bitrate:
a smaller sharp image looks better than a larger blurry one.

## Streaming pipeline

The screen capture, encoding, and network send run on **3 independent worker threads**:

```
┌────────────────┐    queue(max=3)   ┌────────────────┐   queue(max=30)   ┌────────────────┐
│ CaptureWorker  │─── frame_queue ──►│ EncoderWorker  │─── pkt_queue ────►│ NetworkWorker  │
│ (thread)       │                   │ (thread)       │                   │ (thread)       │
│ 30fps costanti │                   │ H.264/H.265    │                   │ relay.send()   │
│ resolution     │                   │ CRF / bitrate  │                   │ frame + tile   │
│ scaling        │                   │ full keyframe  │                   │                │
└────────────────┘                   │ tile JPEG      │                   └────────────────┘
                                     └────────────────┘
```

- **Back-pressure:** if the encoder is slow, the frame queue fills up and
  frames are dropped instead of accumulating latency.
- **Watchdog:** if CaptureWorker fails (e.g. no screen access), the
  EncoderWorker detects the stall within 5 seconds and stops the pipeline.

## Incremental tile updates

When only small regions of the screen change (e.g. typing, mouse movement),
OpenDesk uses **128×128 JPEG tiles** instead of a full H.264 keyframe:

- Changed tiles are detected via vectorised NumPy diff
- Each changed tile is JPEG-encoded at the configured quality level
- The receiver composites tiles onto the last full keyframe reference
- If >30% of tiles changed, a full keyframe is sent instead (more efficient)

This approach saves bandwidth and encoding CPU for typical desktop usage.

## Commands

```bash
uv run opendesk  # Start the remote desktop client
uv run pytest    # Run all tests
```

## Relay server

Il relay server è ora un'app standalone separata in **`../opendesk-relay`** (o
[github.com/opendesk/opendesk-relay](https://github.com/opendesk/opendesk-relay)).

Documentazione completa e istruzioni nel README del progetto relay:

```bash
cd ../opendesk-relay
cat README.md
```

### Avvio rapido

```bash
cd ../opendesk-relay
uv sync
uv run relay-server --port 8474
```

### Installazione come servizio systemd (Linux)

```bash
sudo ./opendesk-relay/install-relay.sh --port 8474
# (dalla directory opendesk, o esegui dal progetto opendesk-relay)
```

### Configurazione client OpenDesk

Nelle impostazioni del client OpenDesk (Tools → Settings → Network), imposta:

| Campo | Valore |
|-------|--------|
| **Relay Host** | IP pubblico del server |
| **Relay Port** | 8474 (o la porta configurata) |
| **Enable relay** | ✅ |
