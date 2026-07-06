"""
P2P networking using WebRTC (``aiortc``).

Handles:
- PeerConnection setup with ICE/STUN/TURN
- Video track (screen capture → remote viewer)
- Data channel (input, file transfer, clipboard, chat)
- Connection state management and reconnection
- Relay fallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine

from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.crypto.e2ee import E2EEncryption, EncryptedMessage, encrypt_json, decrypt_json
from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------


class ConnectionState(Enum):
    """Current state of a P2P connection."""

    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()
    FAILED = auto()


@dataclass
class PeerInfo:
    """Information about a remote peer."""

    peer_id: str
    host: str = ""
    port: int = 0
    session_id: str = ""
    public_key_b64: str = ""


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

MessageHandler = Callable[[Message], Coroutine[Any, Any, None] | None]
ConnectionHandler = Callable[[ConnectionState], Coroutine[Any, Any, None] | None]


# ---------------------------------------------------------------------------
# Data channel transport
# ---------------------------------------------------------------------------


class DataChannelTransport:
    """Wraps an aiortc data channel to send/receive Messages."""

    def __init__(
        self,
        channel: Any,  # noqa: ANN401 — aiortc.RTCDataChannel
        encryption: E2EEncryption | None = None,
    ) -> None:
        self._channel = channel
        self._encryption = encryption
        self._pending: dict[int, asyncio.Future] = {}
        self._seq: int = 0
        self._on_message: MessageHandler | None = None

    @property
    def label(self) -> str:
        return self._channel.label

    @property
    def is_open(self) -> bool:
        return self._channel.readyState == "open"

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register a callback for incoming messages."""
        self._on_message = handler
        self._channel.on("message", self._on_raw_message)

    def _on_raw_message(self, raw: bytes | str) -> None:
        """Handle raw data from the data channel."""
        try:
            if isinstance(raw, str):
                raw = raw.encode("utf-8")

            msg = Message.decode(raw)

            # Handle encrypted payloads transparently
            if msg.encrypted and self._encryption:
                # decrypt in-place
                pass  # caller handles encryption layer

            if self._on_message:
                coro = self._on_message(msg)
                if coro is not None:
                    asyncio.ensure_future(coro)
        except Exception as e:
            logger.warning("Failed to process data channel message: %s", e)

    async def send(self, msg: Message) -> None:
        """Send a Message over the data channel."""
        data = msg.encode()
        if self._channel.readyState == "open":
            self._channel.send(data)
        else:
            logger.warning("Data channel not open, dropping message %s", msg.type)

    async def close(self) -> None:
        """Close the data channel."""
        try:
            await self._channel.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Video track (sender)
# ---------------------------------------------------------------------------


class ScreenVideoTrack:
    """An aiortc VideoStreamTrack that captures the screen.

    Attach this to a PeerConnection's ``addTrack()`` to send screen
    captures as a video stream.
    """

    def __init__(
        self, screen_capture: ScreenCapture, monitor_index: int = 0
    ) -> None:
        super().__init__()
        self._capture = screen_capture
        self._monitor = monitor_index
        self._running = False
        self._frame_queue: asyncio.Queue[CapturedFrame] = asyncio.Queue(maxsize=2)

    async def start(self) -> None:
        """Start the capture loop in a background thread."""
        self._running = True
        loop = asyncio.get_event_loop()

        def _capture_loop() -> None:
            import av
            import numpy as np

            for frame in self._capture.capture_loop(self._monitor):
                if not self._running:
                    break
                # Convert numpy frame to av.VideoFrame
                rgb = frame.data
                av_frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                av_frame.pts = int(frame.timestamp * 1000)  # ms
                # Push into the async queue
                try:
                    loop.call_soon_threadsafe(
                        self._frame_queue.put_nowait, av_frame
                    )
                except asyncio.QueueFull:
                    pass  # drop frame if consumer is slow

        import threading
        self._thread = threading.Thread(target=_capture_loop, daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        """Stop the capture loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    async def recv(self) -> Any:  # noqa: ANN401
        """Return an av.VideoFrame (required by aiortc VideoStreamTrack API)."""
        import av

        frame = await self._frame_queue.get()
        return frame


# ---------------------------------------------------------------------------
# P2P connection manager
# ---------------------------------------------------------------------------


class P2PConnection:
    """Manages a single WebRTC peer connection.

    Usage::

        conn = P2PConnection()
        conn.on_message = my_handler
        await conn.connect(peer_info)
        await conn.send(Message.chat_message("Hello!"))
        await conn.disconnect()
    """

    def __init__(
        self,
        stun_servers: list[str] | None = None,
        turn_servers: list[dict] | None = None,
    ) -> None:
        self._pc: Any = None  # noqa: ANN401 — aiortc.RTCPeerConnection
        self._data_channel: DataChannelTransport | None = None
        self._video_track: ScreenVideoTrack | None = None
        self._encryption: E2EEncryption | None = None
        self._state = ConnectionState.DISCONNECTED
        self._peer_info: PeerInfo | None = None
        self._loop = asyncio.get_event_loop()

        # Default STUN servers (Google's public ones)
        self._ice_servers: list[dict] = [
            {"urls": s} for s in (stun_servers or [
                "stun:stun.l.google.com:19302",
                "stun:stun1.l.google.com:19302",
            ])
        ]
        if turn_servers:
            self._ice_servers.extend(turn_servers)

        # Callbacks
        self.on_message: MessageHandler | None = None
        self.on_state_change: ConnectionHandler | None = None
        self.on_error: Callable[[Exception], None] | None = None

        # Reconnection
        self._max_retries: int = 3
        self._retry_delay: float = 2.0
        self._retry_count: int = 0

    # ── properties ──────────────────────────────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def peer_info(self) -> PeerInfo | None:
        return self._peer_info

    # ── connection lifecycle ────────────────────────────────────────

    async def connect(self, peer_info: PeerInfo) -> None:
        """Establish a WebRTC connection to a peer.

        Parameters
        ----------
        peer_info : PeerInfo
            Information about the remote peer (including SDP/ICE info
            obtained via signalling).
        """
        self._set_state(ConnectionState.CONNECTING)
        self._peer_info = peer_info

        from aiortc import RTCPeerConnection, RTCSessionDescription

        self._pc = RTCPeerConnection(
            iceServers=self._ice_servers,
        )

        self._setup_pc_handlers()

        # Create data channel
        dc = self._pc.createDataChannel("opendesk-control")
        self._data_channel = DataChannelTransport(dc, self._encryption)

        @dc.on("open")
        def _on_dc_open() -> None:
            logger.info("Data channel opened")

        self._data_channel.set_message_handler(self._on_message)

        # Create and add video track
        # (caller sets up the track externally via set_video_track)

        # If we have an offer/answer from signalling, use it
        if peer_info.host and "sdp" in peer_info.host:
            offer = RTCSessionDescription(
                sdp=peer_info.host,
                type="offer" if "o=" in peer_info.host.split("\n")[0] else "answer",
            )
            await self._pc.setRemoteDescription(offer)

            if offer.type == "offer":
                answer = await self._pc.createAnswer()
                await self._pc.setLocalDescription(answer)
                # The SDP should be sent back via signalling
                peer_info.host = self._pc.localDescription.sdp

        self._set_state(ConnectionState.CONNECTED)
        logger.info("P2P connection established to %s", peer_info.peer_id)

    async def create_offer(self) -> str:
        """Create an SDP offer for the other peer.

        Returns
        -------
        str
            The local SDP description (to be sent via signalling).
        """
        from aiortc import RTCPeerConnection, RTCSessionDescription

        self._pc = RTCPeerConnection(iceServers=self._ice_servers)
        self._setup_pc_handlers()

        dc = self._pc.createDataChannel("opendesk-control")
        self._data_channel = DataChannelTransport(dc, self._encryption)
        self._data_channel.set_message_handler(self._on_message)

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        return self._pc.localDescription.sdp  # type: ignore[return-value]

    async def accept_offer(self, sdp: str) -> str:
        """Accept an SDP offer and return the answer SDP.

        Parameters
        ----------
        sdp : str
            The offer SDP from the remote peer.

        Returns
        -------
        str
            The answer SDP to send back.
        """
        from aiortc import RTCPeerConnection, RTCSessionDescription

        self._pc = RTCPeerConnection(iceServers=self._ice_servers)
        self._setup_pc_handlers()

        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self._pc.setRemoteDescription(offer)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        return self._pc.localDescription.sdp  # type: ignore[return-value]

    async def disconnect(self) -> None:
        """Gracefully close the connection."""
        if self._state == ConnectionState.DISCONNECTED:
            return

        logger.info("Disconnecting P2P connection")
        try:
            if self._data_channel:
                await self._data_channel.close()

            if self._video_track:
                await self._video_track.stop()

            if self._pc:
                await self._pc.close()
        except Exception as e:
            logger.warning("Error during disconnect: %s", e)

        self._set_state(ConnectionState.DISCONNECTED)
        self._pc = None
        self._data_channel = None
        self._video_track = None

    # ── encryption ──────────────────────────────────────────────────

    def set_encryption(self, encryption: E2EEncryption) -> None:
        """Enable E2E encryption for data channel messages."""
        self._encryption = encryption

    # ── sending ─────────────────────────────────────────────────────

    async def send(self, msg: Message) -> None:
        """Send a message over the data channel.

        If E2E encryption is enabled and the message is not a
        handshake message, it will be automatically encrypted.
        """
        if self._data_channel is None or not self._data_channel.is_open:
            logger.warning("Cannot send — data channel not open")
            return

        # Encrypt if E2E is active and this is an app-level message
        if self._encryption and msg.type not in (
            MessageType.HELLO,
            MessageType.HELLO_ACK,
            MessageType.KEY_EXCHANGE,
            MessageType.KEY_EXCHANGE_ACK,
            MessageType.PING,
            MessageType.PONG,
        ):
            # Encrypt the payload
            from opendesk.crypto.e2ee import encrypt_json
            encrypted = encrypt_json(self._encryption, msg.payload)
            msg.payload = {
                "encrypted": encrypted.encode().hex(),
            }
            msg.encrypted = True

        await self._data_channel.send(msg)

    # ─── video ──────────────────────────────────────────────────────

    def set_video_track(self, track: ScreenVideoTrack) -> None:
        """Attach a screen capture video track to the connection.

        Must be called before connecting.
        """
        self._video_track = track
        if self._pc:
            self._pc.addTrack(track)

    # ── internal ────────────────────────────────────────────────────

    def _setup_pc_handlers(self) -> None:
        if self._pc is None:
            return

        @self._pc.on("iceconnectionstatechange")
        def _on_ice_state() -> None:
            assert self._pc is not None
            state = self._pc.iceConnectionState
            logger.debug("ICE state: %s", state)
            if state in ("failed", "disconnected"):
                self._set_state(ConnectionState.FAILED)
                asyncio.ensure_future(self._on_connection_lost())
            elif state == "connected":
                self._set_state(ConnectionState.CONNECTED)

        @self._pc.on("datachannel")
        def _on_data_channel(channel: Any) -> None:  # noqa: ANN401
            logger.debug("Received data channel: %s", channel.label)
            self._data_channel = DataChannelTransport(channel, self._encryption)
            self._data_channel.set_message_handler(self._on_message)

        @self._pc.on("connectionstatechange")
        def _on_connection_state() -> None:
            assert self._pc is not None
            cs = self._pc.connectionState
            logger.debug("Connection state: %s", cs)
            if cs == "failed":
                self._set_state(ConnectionState.FAILED)
            elif cs == "closed":
                self._set_state(ConnectionState.DISCONNECTED)

    async def _on_message(self, msg: Message) -> None:
        """Handle incoming messages with optional E2E decryption."""
        # Decrypt if needed
        if msg.encrypted and self._encryption:
            try:
                from opendesk.crypto.e2ee import EncryptedMessage
                em = EncryptedMessage.decode(bytes.fromhex(msg.payload["encrypted"]))
                plain = self._encryption.decrypt(em)
                import json
                msg.payload = json.loads(plain.decode("utf-8"))
                msg.encrypted = False
            except Exception as e:
                logger.error("Failed to decrypt message: %s", e)
                return

        # Route to handler
        if self.on_message:
            coro = self.on_message(msg)
            if coro is not None:
                await coro

    def _set_state(self, state: ConnectionState) -> None:
        if self._state != state:
            old = self._state
            self._state = state
            logger.info("P2P state: %s → %s", old.name, state.name)
            if self.on_state_change:
                coro = self.on_state_change(state)
                if coro is not None:
                    asyncio.ensure_future(coro)

    async def _on_connection_lost(self) -> None:
        """Handle connection loss with reconnection attempts."""
        if self._retry_count >= self._max_retries:
            logger.error("Max reconnection retries reached")
            self._set_state(ConnectionState.FAILED)
            return

        self._retry_count += 1
        self._set_state(ConnectionState.RECONNECTING)
        logger.info(
            "Reconnecting (attempt %d/%d)...",
            self._retry_count, self._max_retries,
        )

        await asyncio.sleep(self._retry_delay * self._retry_count)

        if self._peer_info:
            try:
                await self.disconnect()
                await self.connect(self._peer_info)
                logger.info("Reconnection successful")
                self._retry_count = 0
            except Exception as e:
                logger.error("Reconnection failed: %s", e)
                await self._on_connection_lost()


# ---------------------------------------------------------------------------
# Signalling client (WebSocket-based)
# ---------------------------------------------------------------------------


class SignallingClient:
    """WebSocket-based signalling for exchanging SDP offers/answers.

    In a real deployment this would connect to a signalling server.
    This lightweight version handles direct SDP exchange via
    a shared channel (WebSocket, relay, or manual copy-paste).
    """

    def __init__(self, url: str = "") -> None:
        self._url = url
        self._ws: Any = None  # noqa: ANN401

    async def connect(self, url: str | None = None) -> None:
        """Connect to the signalling server."""
        import websockets

        target = url or self._url
        if not target:
            logger.warning("No signalling server URL — using direct SDP exchange")
            return
        self._ws = await websockets.connect(target)
        logger.info("Connected to signalling server: %s", target)

    async def send_sdp(self, sdp: str, peer_id: str) -> None:
        """Send an SDP description for a peer."""
        if self._ws:
            await self._ws.send(json.dumps({
                "type": "sdp",
                "peer_id": peer_id,
                "sdp": sdp,
            }))

    async def receive_sdp(self) -> tuple[str, str] | None:
        """Receive an SDP description (peer_id, sdp)."""
        if self._ws:
            data = await self._ws.recv()
            obj = json.loads(data)
            if obj.get("type") == "sdp":
                return obj["peer_id"], obj["sdp"]
        return None

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
