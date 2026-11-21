"""
OpenDesk Host — versione solo incoming.

Si connette al relay come host, mostra device ID e password in una
finestra compatta.  Accetta connessioni in ingresso con streaming,
input remoto, chat e file transfer.  Non permette connessioni in uscita.
"""

from __future__ import annotations

import logging
import queue
import time
import traceback
import uuid
from pathlib import Path
from typing import cast

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from opendesk import __version__
from opendesk.core.clipboard_sync import ClipboardSync
from opendesk.core.device_registry import DeviceRegistry
from opendesk.core.file_transfer import FILE_MESSAGE_TYPES, FileTransferManager
from opendesk.core.platform_config import HealthSeverity, get_platform_config
from opendesk.core.unattended import UnattendedAccess
from opendesk.crypto.auth import AuthManager, generate_otp
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import (
    _RETRY_BASE_MS,
    RelayClient,
    retry_delay_ms,
)
from opendesk.services.stream_service import StreamService
from opendesk.ui.chat_panel import ChatPanel
from opendesk.ui.session_info import SessionInfoWidget
from opendesk.ui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FT_POLL_INTERVAL = 200  # ms — poll file-transfer updates queue
_FT_POLL_IDLE_INTERVAL = 1000  # ms — quando la coda è vuota (risparmio CPU/RAM)
_SINGLE_INSTANCE_NAME = "OpenDeskHostInstance"  # lock QLocalServer
# Minimo intervallo tra due tentativi di ripresa della pipeline: se la
# cattura è rotta (schermo bloccato/secure desktop) non si riavvia in loop.
_STREAM_RESTART_MIN_INTERVAL_S = 20.0


# ═══════════════════════════════════════════════════════════════════════════
# HostService
# ═══════════════════════════════════════════════════════════════════════════


class HostService(QObject):
    """Servizio host-only: relay, streaming, file transfer, chat.

    Signals
    -------
    status_changed(text: str)
        Testo descrittivo dello stato corrente (connessione, auth, etc.).
    device_info_changed(device_id: str, password: str)
        Le credenziali di sessione sono cambiate (nuova sessione).
    peer_connected(peer_name: str)
        Un client remoto si e' autenticato.
    peer_disconnected()
        Il client remoto si e' disconnesso.
    streaming_started()
        Lo streaming screen capture e' iniziato.
    streaming_stopped()
        Lo streaming e' stato fermato.
    chat_message_received(text: str, is_remote: bool)
        Messaggio chat ricevuto dal/sul peer remoto.
    chat_open_requested(open: bool)
        Il peer remoto ha aperto/chiuso la chat.
    """

    status_changed = Signal(str)
    device_info_changed = Signal(str, str)
    peer_connected = Signal(str)
    peer_disconnected = Signal()
    streaming_started = Signal()
    streaming_stopped = Signal()
    chat_message_received = Signal(str, bool)
    chat_open_requested = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # ── Device identity (persistent UUID) ──
        self._device_id = cast(str, self._settings.value("device/id", "", type=str))
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
            self._settings.setValue("device/id", self._device_id)
        self._device_name = cast(str, self._settings.value("device/name", "", type=str))
        if not self._device_name:
            self._device_name = f"Desktop-{self._device_id[:8]}"
            self._settings.setValue("device/name", self._device_name)

        # ── Auth / session ──
        self._auth = AuthManager()
        self._session_id: str = ""
        self._password: str = ""

        # ── Relay client (host only) ──
        self._relay = RelayClient(self)
        self._relay.host_connected.connect(self._on_host_connected)
        self._relay.host_disconnected.connect(self._on_host_disconnected)
        self._relay.host_peer_joined.connect(self._on_host_peer_joined)
        self._relay.host_peer_disconnected.connect(self._on_host_peer_disconnected)
        self._relay.host_auth_result.connect(self._on_host_auth_result)
        self._relay.host_keyframe_requested.connect(self._on_host_keyframe_requested)
        self._relay.message_received.connect(self._on_relay_message)
        self._relay.error.connect(self._on_relay_error)
        self._relay.device_list_received.connect(self._on_device_list)

        # ── Streaming ──
        self._stream: StreamService | None = None

        # ── File transfer ──
        self._file_transfer = FileTransferManager()
        self._ft_poll_timer = QTimer(self)
        self._ft_poll_timer.timeout.connect(self._poll_file_transfer)
        self._ft_poll_active_polls = 0
        self._ft_poll_timer.start(_FT_POLL_IDLE_INTERVAL)

        # ── Clipboard sync (host ↔ client) ──
        # Attivo solo mentre un client è autenticato: vedi _on_host_auth_result.
        self._clipboard_sync = ClipboardSync(self)

        # ── Device registry (pre-authorization) ──
        self._device_registry = DeviceRegistry()

        # ── Accesso non presidiato (allowlist di device) ──
        self._unattended = UnattendedAccess()

        # ── State ──
        self._peer_connected = False
        self._peer_device_id: str = ""

        # Retry host → relay: backoff esponenziale (vedi retry_delay_ms)
        self._reconnect_attempts = 0
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(_RETRY_BASE_MS)
        self._reconnect_timer.timeout.connect(self._try_reconnect_host)

        self._last_stream_restart_mono: float = 0.0

        # Guardia: nessuna pipeline attiva senza un peer autenticato.
        # Copre i disconnect persi (relay caduto, eventi mancanti): senza
        # client la cattura+encode continuerebbe a bruciare CPU a vuoto.
        self._peer_watchdog = QTimer(self)
        self._peer_watchdog.setInterval(5000)
        self._peer_watchdog.timeout.connect(self._check_peer_stream)

    # ── properties ──────────────────────────────────────────────────────

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def device_name(self) -> str:
        return self._device_name

    @device_name.setter
    def device_name(self, value: str) -> None:
        self._device_name = value
        self._settings.setValue("device/name", value)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def password(self) -> str:
        return self._password

    @property
    def relay(self) -> RelayClient:
        return self._relay

    @property
    def stream(self) -> StreamService | None:
        return self._stream

    @property
    def file_transfer(self) -> FileTransferManager:
        return self._file_transfer

    @property
    def device_registry(self) -> DeviceRegistry:
        return self._device_registry

    @property
    def is_peer_connected(self) -> bool:
        return self._peer_connected

    @property
    def is_hosting(self) -> bool:
        return self._relay.is_hosting()

    # ── lifecycle ───────────────────────────────────────────────────────

    def create_session(self, password: str | None = None) -> tuple[str, str]:
        """Crea una nuova sessione.  Restituisce (session_id, password)."""
        if password is None:
            password = generate_otp()
        session = self._auth.create_session(password, one_time=False)
        self._session_id = session.session_id
        self._password = password
        self.device_info_changed.emit(self._session_id, self._password)
        logger.info("New session created: %s", self._session_id)
        return self._session_id, self._password

    def start(self) -> None:
        """Connetti al relay e avvia l'hosting."""
        if not self._session_id:
            self.create_session()
        host, port = self._get_relay_config()
        clean_id = self._session_id.replace(" ", "")
        self.status_changed.emit(f"Connecting to relay {host}:{port}...")
        logger.info("Starting host on relay %s:%s session=%s", host, port, clean_id)
        self._relay.start_hosting(
            host,
            port,
            clean_id,
            self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_devices=self._device_registry.trusted_secrets(),
            allowed_peer_ids=self._unattended_allowlist(),
        )
        self._reconnect_timer.start()
        self._peer_watchdog.start()

    def _unattended_allowlist(self) -> set[str]:
        """Device autorizzati dall'accesso non presidiato (vuoto = nessun filtro)."""
        if not self._unattended.enabled:
            return set()
        return set(self._unattended.config.allowed_peers)

    def stop(self) -> None:
        """Ferma l'hosting e disconnetti dal relay."""
        self._reconnect_timer.stop()
        self._peer_watchdog.stop()
        self._stop_streaming()
        self._relay.disconnect()
        self._peer_connected = False
        self.status_changed.emit("Disconnected")

    def regenerate_session(self, password: str | None = None) -> None:
        """Genera nuova sessione (ID + password) e ri-avvia l'hosting.

        ``password`` permette alla UI di proporre una password già mostrata
        all'utente, evitando che il valore visibile diverga da quello attivo.
        """
        logger.info(
            "regenerate_session chiamato (caller=\n%s)",
            "".join(traceback.format_stack()[-4:-1]),
        )
        self._relay.stop_hosting()
        self.create_session(password)
        self.start()

    # ── relay config ────────────────────────────────────────────────────

    def _get_relay_config(self) -> tuple[str, int]:
        host = cast(str, self._settings.value("network/relay_host", "", type=str)).strip()
        # Tolerate a URL pasted instead of a bare host ("https://host/...").
        host = host.removeprefix("https://").removeprefix("http://").strip("/")
        if host in {"gibisoft.net", "www.gibisoft.net"}:
            # Legacy default written by previous Windows installers.
            host = "relay.gibisoft.net"
        if not host:
            host = "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        try:
            port = cast(int, self._settings.value("network/relay_port", 8474, type=int))
        except (ValueError, TypeError):
            port = 8474
        return host, port

    # ── streaming ───────────────────────────────────────────────────────

    def _start_streaming(self) -> None:
        """Avvia screen capture + streaming verso il peer remoto."""
        if self._stream is not None and self._stream.is_streaming:
            return
        if self._stream is None:
            self._stream = StreamService(self._relay, self)
            self._stream.error.connect(self._on_stream_error)
            self._stream.input_unavailable.connect(self._on_input_unavailable)
            self._stream.input_blocked.connect(self._on_input_blocked)
        self._stream.start_streaming()
        self.streaming_started.emit()
        logger.info("Host streaming started")

    def _stop_streaming(self) -> None:
        """Ferma lo streaming (no-op silenzioso se non è attivo)."""
        if self._stream is not None and self._stream.is_streaming:
            self._stream.stop_streaming()
            self.streaming_stopped.emit()
            logger.info("Host streaming stopped")

    @Slot()
    def _check_peer_stream(self) -> None:
        """Allinea la pipeline allo stato del peer.

        - peer assente: ferma la pipeline (senza destinatario catturerebbe
          ed encoderebbe a 30 fps per niente, anche dopo un disconnect perso);
        - peer autenticato con pipeline ferma: la riprende.  Prima non esisteva
          il secondo caso, quindi un errore transitorio di capture/encode
          lasciava l'host muto per sempre: sessione relay e path P2P restavano
          su, il client chiedeva keyframe e nessuno riavviava lo streaming
          (RCA 2026-11-21: 1 frame poi silenzio totale).
        """
        if self._stream is not None and self._stream.is_streaming:
            if self._peer_connected:
                return
            logger.info("No authenticated peer — stopping idle streaming pipeline")
            self._stop_streaming()
            return
        if self._peer_connected:
            self._maybe_restart_streaming("peer still connected but pipeline is down")

    def _maybe_restart_streaming(self, reason: str) -> None:
        """Riprende la pipeline con throttling (peer autenticato presente)."""
        now = time.monotonic()
        if now - self._last_stream_restart_mono < _STREAM_RESTART_MIN_INTERVAL_S:
            return
        self._last_stream_restart_mono = now
        logger.warning("Peer ancora connesso ma pipeline ferma — riavvio streaming (%s)", reason)
        self._start_streaming()

    # ── retry (ciclico ogni 10 s) ───────────────────────────────────────

    @Slot()
    def _try_reconnect_host(self) -> None:
        """Riconnetti l'hosting al relay con backoff finché non riesce."""
        if not self._session_id or self._relay.is_hosting():
            return
        host, port = self._get_relay_config()
        logger.info(
            "Reconnecting host to relay %s:%s (attempt #%d)",
            host,
            port,
            self._reconnect_attempts + 1,
        )
        self.status_changed.emit("Reconnecting to relay...")
        self.start()

    # ── file transfer polling ──────────────────────────────────────────

    def _poll_file_transfer(self) -> None:
        """Drena la coda degli aggiornamenti file transfer (main thread).

        L'host non ha UI di file transfer: gli eventi servono solo a
        evitare che la coda cresca senza limite.
        """
        try:
            got_event = False
            while True:
                self._file_transfer.updates.get_nowait()
                got_event = True
        except queue.Empty:
            pass
        finally:
            # Timer adattivo: 1000 ms a riposo, 200 ms durante i trasferimenti
            new_interval = _FT_POLL_INTERVAL if got_event else _FT_POLL_IDLE_INTERVAL
            if new_interval != self._ft_poll_timer.interval():
                self._ft_poll_timer.start(new_interval)

    def respond_to_incoming_file(self, job_id: str, accepted: bool) -> None:
        """Accept or reject a file request without host confirmation."""
        job = self._file_transfer.get_job(job_id)
        if job is None:
            return
        if accepted and self._file_transfer.accept_incoming(job_id):
            self._relay.send_message(Message.file_accept(job_id))
            return

        reason = job.error or "Rejected by local user"
        self._file_transfer.reject_incoming(job_id, reason)
        self._relay.send_message(Message.file_reject(job_id, reason))

    # ── host event handlers ─────────────────────────────────────────────

    @Slot(str, str)
    def _on_host_connected(self, role: str, session_id: str) -> None:
        self.status_changed.emit("Connected to relay — waiting for incoming connection...")
        logger.info("Host connected to relay: %s", session_id)
        # Connessione riuscita: si riparte dal ritardo minimo
        if self._reconnect_attempts:
            self._reconnect_attempts = 0
            self._reconnect_timer.setInterval(_RETRY_BASE_MS)

    @Slot()
    def _on_host_disconnected(self) -> None:
        self._reconnect_attempts += 1
        delay_ms = retry_delay_ms(self._reconnect_attempts)
        self._reconnect_timer.setInterval(delay_ms)
        logger.warning(
            "Host disconnected from relay (attempt #%d) — retrying in %.0fs",
            self._reconnect_attempts,
            delay_ms / 1000.0,
        )
        self._peer_connected = False
        self._clipboard_sync.stop()
        # Senza sessione relay send_frame non ha destinazione: la pipeline
        # continuerebbe a catturare/encodare a vuoto (CPU alta da idle).
        self._stop_streaming()
        self.status_changed.emit("Disconnected from relay — reconnecting...")

    @Slot()
    def _on_host_peer_joined(self) -> None:
        self.status_changed.emit("Remote peer joined — authenticating...")

    @Slot()
    def _on_host_peer_disconnected(self) -> None:
        logger.info("Remote peer disconnected")
        self._peer_connected = False
        self._peer_device_id = ""
        self._clipboard_sync.stop()
        self._stop_streaming()
        self.peer_disconnected.emit()
        self.status_changed.emit("Remote client disconnected — waiting for connections...")

    @Slot(bool, str)
    def _on_host_auth_result(self, success: bool, message: str) -> None:
        if success:
            self._peer_connected = True
            self._start_streaming()
            # Clipboard bidirezionale: poll della clipboard locale e invio al
            # client; i messaggi CLIPBOARD_* in arrivo vengono applicati in
            # _dispatch_relay_message.
            self._clipboard_sync.start(self._relay.send_message)
            self.peer_connected.emit(self._peer_device_id or "Remote")
            self.status_changed.emit("Remote peer authenticated — streaming...")
        else:
            self._peer_connected = False
            self.status_changed.emit(f"Authentication failed: {message}")

    @Slot()
    def _on_host_keyframe_requested(self) -> None:
        logger.debug("Keyframe requested by remote peer")
        # Una richiesta di keyframe è prova che il peer è vivo e autenticato:
        # se la pipeline è ferma va ripresa, altrimenti l'host resta muto e
        # ogni richiesta successiva cade nel vuoto.
        if self._stream is None or not self._stream.is_streaming:
            self._peer_connected = True
            self._maybe_restart_streaming("keyframe request from peer")

    @Slot(str)
    def _on_stream_error(self, error_msg: str) -> None:
        logger.error("Stream error: %s", error_msg)
        self.status_changed.emit(f"⚠ Stream error: {error_msg}")
        self._stop_streaming()

    @Slot(str)
    def _on_input_unavailable(self, error_msg: str) -> None:
        logger.warning("Input backend unavailable: %s", error_msg)
        self.status_changed.emit(f"⚠ Remote input disabled: {error_msg}")

    @Slot(str)
    def _on_input_blocked(self, message: str) -> None:
        """UIPI: finestra elevata in focus (Task Manager/UAC) blocca SendInput."""
        logger.warning("Input remoto bloccato: %s", message)
        self.status_changed.emit(f"⚠ {message}")

    @Slot(str)
    def _on_relay_error(self, error_msg: str) -> None:
        if "Peer disconnected" in error_msg:
            logger.info("Peer disconnected (via error handler)")
            self._stop_streaming()
            self._peer_connected = False
            self.peer_disconnected.emit()
            self.status_changed.emit("Remote client disconnected — waiting for connections...")
            return
        logger.error("Relay error: %s", error_msg)
        self.status_changed.emit(f"⚠ Error: {error_msg}")

    @Slot(list)
    def _on_device_list(self, devices: list[dict]) -> None:
        """Aggiorna il device registry con la lista ricevuta dal relay."""
        self._device_registry.merge_from_relay(devices)

    @Slot(object)
    def _on_relay_message(self, msg: Message) -> None:
        """Gestisce tutti i messaggi in arrivo dal relay."""
        t0 = time.perf_counter()
        try:
            self._dispatch_relay_message(msg)
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000
            if dt_ms > 100:
                logger.warning(
                    "SLOW _on_relay_message: %.0fms (type=%s) — input iniettato "
                    "sul main thread: stalli qui bloccano mouse/tastiera remota",
                    dt_ms,
                    getattr(msg.type, "name", msg.type),
                )

    def _dispatch_relay_message(self, msg: Message) -> None:
        """Dispatch interno di _on_relay_message (vedi lì per il timing)."""
        t = msg.type

        # ── Input injection ──
        if t == MessageType.MOUSE_EVENT:
            if self._stream and self._stream.input_backend:
                logger.debug(
                    "Host received MOUSE_EVENT: x=%d y=%d button=%s pressed=%s",
                    msg.payload.get("x"),
                    msg.payload.get("y"),
                    msg.payload.get("button"),
                    msg.payload.get("pressed"),
                )
                self._stream.inject_mouse(msg)
            else:
                logger.warning(
                    "Host received MOUSE_EVENT but input_backend is None — remote input disabled"
                )
        elif t == MessageType.KEYBOARD_EVENT and self._stream and self._stream.input_backend:
            logger.debug(
                "Host received KEYBOARD_EVENT: key=%s pressed=%s",
                msg.payload.get("key"),
                msg.payload.get("pressed"),
            )
            self._stream.inject_keyboard(msg)
        elif t == MessageType.CAPS_LOCK_STATE and self._stream and self._stream.input_backend:
            self._stream.sync_remote_caps_lock(msg.payload.get("active", False))

        # ── Chat ──
        elif t == MessageType.CHAT_MESSAGE:
            text = msg.payload.get("text", "")
            self.chat_message_received.emit(text, True)
        elif t == MessageType.CHAT_OPEN:
            is_open = msg.payload.get("open", False)
            self.chat_open_requested.emit(is_open)

        # ── Audio (playback dal peer remoto) ──
        elif t == MessageType.AUDIO_FRAME:
            data = msg.payload.get("data", b"")
            if data and self._stream:
                self._stream.play_audio_frame(data)

        # ── File transfer ──
        elif t == MessageType.FILE_REQUEST:
            job_id = self._file_transfer.handle_file_request(msg)
            if job_id:
                # Auto-accept: trasferimento in background, nessuna conferma host.
                self.respond_to_incoming_file(job_id, True)
        elif t in FILE_MESSAGE_TYPES:
            self._file_transfer.dispatch(msg, self._relay.send_message)

        # ── Ignored ──
        elif t in (MessageType.CAMERA_FRAME, MessageType.CAMERA_START):
            # Siamo host: la webcam remota non viene mostrata (nessun viewer)
            pass
        elif t in (
            MessageType.CLIPBOARD_TEXT,
            MessageType.CLIPBOARD_IMAGE,
            MessageType.CLIPBOARD_SYNC,
        ):
            self._clipboard_sync.receive_from_remote(msg)

    # ── helpers ─────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════
# HostWindow
# ═══════════════════════════════════════════════════════════════════════════


def _toggle_topmost_win32(hwnd: int, topmost: bool) -> None:
    """Attiva/disattiva lo z-order top-most SENZA ricreare la finestra.

    Sostituisce il toggle via ``setWindowFlags`` (che distrugge e ricrea
    la finestra nativa, corrompendo il dispatcher eventi Qt durante le
    sessioni remote — vedi ``HostWindow.changeEvent``).
    """
    import ctypes
    from ctypes import wintypes

    hwnd_topmost = -1  # HWND_TOPMOST
    hwnd_notopmost = -2  # HWND_NOTOPMOST
    swp_nomove = 0x0002
    swp_nosize = 0x0001
    swp_noactivate = 0x0010
    insert_after = hwnd_topmost if topmost else hwnd_notopmost
    ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        0,
        0,
        0,
        0,
        swp_nomove | swp_nosize | swp_noactivate,
    )


class HostWindow(QMainWindow):
    """Finestra principale compatta per OpenDesk Host (solo incoming).

    Mostra device ID, password e stato connessione.  Permette di
    avviare chat, file transfer e modificare le impostazioni.
    """

    WINDOW_TITLE = "OpenDesk Host"
    # La SessionInfoWidget ha sizeHint ~534px: con MIN_WIDTH più basso la
    # riga credenziali si schiaccia e i widget si sovrappongono (RCA probe
    # Qt: sizeHint 534 vs finestra 440).  580 = 534 + margini (40).
    MIN_WIDTH = 580
    MIN_HEIGHT = 420

    # ── Design tokens ───────────────────────────────────────────────────
    _C_PRIMARY = "#2563eb"
    _C_PRIMARY_DARK = "#1e40af"
    _C_SUCCESS = "#22c55e"
    _C_WARNING = "#eab308"
    _C_DANGER = "#ef4444"
    _C_TEXT = "#0f172a"
    _C_TEXT_SECONDARY = "#64748b"
    _C_BORDER = "#e2e8f0"
    _C_SURFACE = "#ffffff"
    _FONT_MONO = "'Courier New', 'Consolas', monospace"

    def __init__(self, service: HostService, start_minimized: bool = False) -> None:
        super().__init__()
        self._service = service
        self._start_minimized = start_minimized
        self._force_quit = False
        self._tray: QSystemTrayIcon | None = None
        self._tray_hint_shown = False
        self._adjusting_topmost = False  # guardia anti-ricorsione changeEvent

        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowIcon(self._app_icon())

        # ── Sub-windows (lazy) ──
        self._chat_panel: ChatPanel | None = None

        # ── Build UI ──
        self._setup_central_widget()
        self._setup_menu()
        self._setup_tray()
        self._wire_service()

        # Health check all'avvio
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, self._check_platform_health_startup)

    # ═══════════════════════════════════════════════════════════════════════
    # UI setup
    # ═══════════════════════════════════════════════════════════════════════

    def _setup_central_widget(self) -> None:
        """Costruisce il widget centrale con design rinnovato."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 1. Header bar ──────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("HostHeader")
        header.setFixedHeight(96)
        header.setStyleSheet(f"""
            QFrame#HostHeader {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 {self._C_PRIMARY_DARK},
                    stop:1 {self._C_PRIMARY}
                );
            }}
        """)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 10, 20, 10)
        header_layout.setSpacing(2)

        # Title
        title = QLabel("OpenDesk Host")
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #ffffff;")
        header_layout.addWidget(title)

        # Status row inside header
        status_row = QHBoxLayout()
        status_row.setSpacing(6)

        # ── Health indicator ──
        from opendesk.ui.widgets.health_status import HealthStatusWidget

        self._health_widget = HealthStatusWidget(self)
        self._health_widget.setToolTip("Stato piattaforma — click per dettagli")
        status_row.addWidget(self._health_widget)

        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"font-size: 12px; color: {self._C_WARNING};")
        status_row.addWidget(self._status_dot)

        self._status_label = QLabel("Initializing...")
        self._status_label.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: rgba(255,255,255,0.85);"
        )
        status_row.addWidget(self._status_label)
        status_row.addStretch()

        header_layout.addLayout(status_row)
        layout.addWidget(header)

        # ── 2. Content area ────────────────────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 16)
        content_layout.setSpacing(16)

        # ── 2a. Credentials (widget condiviso con il client) ───────────
        self._session_info = SessionInfoWidget(
            device_id=self._service.device_id,
            device_name=self._service.device_name,
            theme_toggle=False,  # l'host usa sempre il tema chiaro
            parent=self,
        )
        self._session_info.session_refreshed.connect(self._on_session_refreshed)
        self._session_info.device_name_changed.connect(self._on_device_name_changed)
        content_layout.addWidget(self._session_info)

        # ── 2b. Device UUID info ───────────────────────────────────────
        uuid_info = QLabel(f"Device UUID: {self._service.device_id}")
        uuid_info.setStyleSheet(f"font-size: 10px; color: {self._C_TEXT_SECONDARY};")
        uuid_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        uuid_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        uuid_info.setToolTip(
            "Usa questo UUID in Settings → Security per pre-autorizzare questo dispositivo"
        )
        content_layout.addWidget(uuid_info)

        # ── 2c. Action buttons ─────────────────────────────────────────
        # Solo Settings: Chat e File Transfer si attivano automaticamente
        # quando il computer remoto li richiede.  "New Session" è nel widget
        # credenziali.
        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(8)

        actions_layout.addStretch()

        self._settings_btn = QPushButton("⚙  Settings")
        self._settings_btn.setFixedHeight(36)
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setStyleSheet(self._secondary_button_style())
        self._settings_btn.clicked.connect(self._on_settings)
        actions_layout.addWidget(self._settings_btn)

        content_layout.addLayout(actions_layout)
        content_layout.addStretch()

        layout.addWidget(content, 1)

        self.setCentralWidget(central)

    # ── Button styles ───────────────────────────────────────────────────

    @staticmethod
    def _secondary_button_style() -> str:
        return """
            QPushButton {
                padding: 6px 18px;
                font-size: 13px;
                font-weight: 600;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                background: #ffffff;
                color: #475569;
            }
            QPushButton:hover {
                background: #f1f5f9;
                border-color: #2563eb;
                color: #2563eb;
            }
            QPushButton:pressed {
                background: #e2e8f0;
            }
        """

    def _setup_menu(self) -> None:
        """Menu minimale (File > Quit, Help > About)."""
        menubar = self.menuBar()
        if menubar is None:
            return

        file_menu = menubar.addMenu("&File")
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self._quit)
        file_menu.addAction(act_quit)

        help_menu = menubar.addMenu("&Help")
        act_about = QAction("&About OpenDesk Host", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    # ── System tray ────────────────────────────────────────────────

    @staticmethod
    def _app_icon() -> QIcon:
        """Load the OpenDesk application/tray icon."""
        icon_path = Path(__file__).parent / "ui" / "resources" / "opendesk.svg"
        return QIcon(str(icon_path))

    def _setup_tray(self) -> None:
        """Crea l'icona nella system tray con menu contestuale.

        Quando la tray non e' disponibile (es. sessione senza StatusNotifier)
        la finestra resta il canale principale e la chiusura termina l'app.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray not available — running window-only")
            return

        tray = QSystemTrayIcon(self._app_icon(), self)
        tray.setToolTip(f"{self.WINDOW_TITLE}\nDevice: {self._service.device_id}")

        menu = QMenu(self)

        act_show = QAction("Show window", self)
        act_show.triggered.connect(self._show_from_tray)
        menu.addAction(act_show)

        act_new = QAction("New session", self)
        act_new.triggered.connect(self._service.regenerate_session)
        menu.addAction(act_new)

        menu.addSeparator()

        act_copy_id = QAction("Copy ID", self)
        act_copy_id.triggered.connect(self._copy_session_id)
        menu.addAction(act_copy_id)

        act_copy_pwd = QAction("Copy password", self)
        act_copy_pwd.triggered.connect(self._copy_password)
        menu.addAction(act_copy_pwd)

        menu.addSeparator()

        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Click sulla tray: mostra/nascondi la finestra."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_from_tray()

    @Slot()
    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ── Platform health ────────────────────────────────────────────

    def _check_platform_health_startup(self) -> None:
        """Mostra notifica se ci sono criticità all'avvio."""
        from opendesk.ui.widgets.toast_notification import ToastNotification

        cfg = get_platform_config()
        issues = cfg.check_health()

        critical = [i for i in issues if i.severity == HealthSeverity.CRITICAL]
        warnings = [i for i in issues if i.severity == HealthSeverity.WARNING]

        if critical:
            msg = f"🔴 {len(critical)} problema critico: " + ", ".join(
                c.message.split(".")[0] for c in critical[:2]
            )
            ToastNotification(self, msg, ToastNotification.Type.ERROR, duration_ms=6000).show()
        elif warnings:
            msg = f"🟡 {len(warnings)} avviso: " + ", ".join(
                w.message.split(".")[0] for w in warnings[:2]
            )
            ToastNotification(self, msg, ToastNotification.Type.WARNING, duration_ms=5000).show()

        if hasattr(self, "_health_widget"):
            self._health_widget._refresh()

    # ── Signal wiring ──────────────────────────────────────────────

    def _wire_service(self) -> None:
        """Collega i segnali del servizio ai gestori UI."""

        # Click sull'health widget mostra dettagli
        if hasattr(self, "_health_widget"):
            from PySide6.QtWidgets import QMessageBox

            def show_health() -> None:
                cfg = get_platform_config()
                issues = cfg.check_health()
                msg = f"<b>Piattaforma: {cfg.display_name}</b><br><br>"
                if not issues:
                    msg += "✅ Nessun problema rilevato."
                else:
                    for i in issues:
                        icon = {
                            HealthSeverity.CRITICAL: "🔴",
                            HealthSeverity.WARNING: "🟡",
                            HealthSeverity.INFO: "ℹ️",
                        }[i.severity]
                        msg += f"{icon} <b>{i.component}</b>: {i.message}<br>"
                        if i.fix:
                            msg += f"&nbsp;&nbsp;&nbsp;→ {i.fix}<br>"
                        msg += "<br>"
                QMessageBox.information(self, "Stato piattaforma", msg)

            self._health_widget.clicked.connect(show_health)
        svc = self._service
        svc.status_changed.connect(self._on_status_changed)
        svc.device_info_changed.connect(self._on_device_info_changed)
        svc.peer_connected.connect(self._on_peer_connected)
        svc.peer_disconnected.connect(self._on_peer_disconnected)
        svc.chat_message_received.connect(self._on_chat_message)
        svc.chat_open_requested.connect(self._on_chat_open)

        # Stato iniziale: la sessione può esistere già prima della finestra.
        if svc.session_id:
            self._session_info.set_session(svc.session_id, svc.password)

    # ── Slots: stato ─────────────────────────────────────────────────────

    @Slot(str)
    def _on_status_changed(self, status: str) -> None:
        self._status_label.setText(status)
        # Aggiorna colore pallino in base allo stato
        lower = status.lower()
        if "error" in lower or "fail" in lower or "unavailable" in lower:
            color = self._C_DANGER
        elif "connect" in lower or "authenticat" in lower or "join" in lower:
            color = self._C_WARNING
        elif "stream" in lower or "connected" in lower or "wait" in lower:
            color = self._C_SUCCESS
        else:
            color = self._C_WARNING
        self._status_dot.setStyleSheet(f"font-size: 12px; color: {color};")

    @Slot(str, str)
    def _on_device_info_changed(self, session_id: str, password: str) -> None:
        self._session_info.set_session(session_id, password)

    @Slot(str)
    def _on_peer_connected(self, peer_name: str) -> None:
        self.setWindowTitle("OpenDesk Host — Connected")

    @Slot()
    def _on_peer_disconnected(self) -> None:
        self.setWindowTitle(self.WINDOW_TITLE)

    # ── Slots: azioni ───────────────────────────────────────────────────

    @Slot(str, str)
    def _on_session_refreshed(self, _session_id: str, password: str) -> None:
        """Nuova sessione richiesta dal widget credenziali.

        La password è già quella mostrata all'utente: la passiamo a
        ``regenerate_session`` così UI e stato del servizio non divergono.
        """
        self._service.regenerate_session(password)

    @Slot(str)
    def _on_device_name_changed(self, new_name: str) -> None:
        self._service.device_name = new_name

    # Chat e File Transfer si aprono SOLO su richiesta del computer remoto.
    # L'host non ha pulsanti per avviarli manualmente.

    @Slot()
    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            device_registry=self._service.device_registry,
            parent=self,
        )
        dialog.exec()

    # ── Chat handlers ───────────────────────────────────────────────────

    @Slot(str, bool)
    def _on_chat_message(self, text: str, is_remote: bool) -> None:
        if self._chat_panel is None:
            self._chat_panel = ChatPanel(self)
            self._chat_panel.message_sent.connect(self._on_chat_message_sent)
        self._chat_panel.add_message("Remote", text, is_remote=True)
        if not self._chat_panel.isVisible():
            self._chat_panel.show()
            self._chat_panel.raise_()

    @Slot(bool)
    def _on_chat_open(self, is_open: bool) -> None:
        if is_open:
            if self._chat_panel is None:
                self._chat_panel = ChatPanel(self)
                self._chat_panel.message_sent.connect(self._on_chat_message_sent)
            if not self._chat_panel.isVisible():
                self._chat_panel.show()
                self._chat_panel.raise_()
                self._chat_panel.activateWindow()
        else:
            if self._chat_panel is not None and self._chat_panel.isVisible():
                self._chat_panel.hide()

    @Slot(str)
    def _on_chat_message_sent(self, text: str) -> None:
        self._service.relay.send_message(Message.chat_message(text))

    # ── Copy ────────────────────────────────────────────────────────────

    def _copy_session_id(self) -> None:
        """Copia il session ID (senza spazi) negli appunti."""
        raw = self._service.session_id.replace(" ", "")
        QApplication.clipboard().setText(raw)

    def _copy_password(self) -> None:
        QApplication.clipboard().setText(self._service.password)

    # ── About ───────────────────────────────────────────────────────────

    @Slot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About OpenDesk Host",
            "<h3>OpenDesk Host v" + __version__ + "</h3>"
            "<p>Remote desktop host — accepts incoming connections.</p>"
            "<p>Built with Python, PySide6.</p>"
            "<hr>"
            "<p style='font-size:12px;color:#64748b'>MIT License — 2026</p>",
        )

    # ── Close ───────────────────────────────────────────────────────────

    @Slot()
    def _quit(self) -> None:
        """Richiesta esplicita di uscita (menu File, tray o Ctrl+Q)."""
        self._force_quit = True
        self.close()

    def changeEvent(self, event) -> None:  # noqa: N802, ANN001
        """Log degli eventi di stato finestra + quirk Windows top-most.

        Su Windows una finestra ``WindowStaysOnTopHint`` minimizzata
        spesso non è iripristinabile dal taskbar (nessun WM_ACTIVATE):
        il flag viene rimosso alla minimizzazione e riapplicato al
        ripristino, così la finestra non risulta mai "bloccata".

        NB: il toggle usa ``SetWindowPos`` (win32) e NON
        ``setWindowFlags``: quest'ultimo DISTRUGGE E RICREA la finestra
        nativa — durante una sessione remota ha corrotto il dispatcher
        eventi Qt (``QEventDispatcherWin32`` in attesa infinita: timer
        morti, UI congelata anche a streaming terminato — RCA
        2026-10-02).  ``SetWindowPos`` cambia solo lo z-order: nessuna
        ricreazione, nessun effetto sul dispatcher.
        """
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self._adjusting_topmost:
            return
        minimized = bool(self.windowState() & Qt.WindowState.WindowMinimized)
        logger.info("HostWindow state change: minimized=%s", minimized)
        has_top = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if minimized == has_top:
            self._adjusting_topmost = True
            try:
                _toggle_topmost_win32(int(self.winId()), not minimized)
            except Exception as e:
                logger.debug("toggle top-most win32 fallito (ignorato): %s", e)
            finally:
                self._adjusting_topmost = False

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Chiudi la finestra: se la tray e' attiva, riduci a icona.

        Una semplice chiusura (X) non termina l'host: la sessione continua
        in background nella system tray.  Solo *Quit* termina davvero.
        """
        if self._tray is not None and not self._force_quit:
            logger.info(
                "HostWindow close → hide to tray (peer_connected=%s)",
                self._service.is_peer_connected,
            )
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.showMessage(
                    self.WINDOW_TITLE,
                    "OpenDesk Host continua in background nella system tray.\n"
                    "Click sull'icona per riaprire, menu → Quit per uscire.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            return

        # Sessione remota attiva senza tray: un QMessageBox modale in un
        # contesto remoto non ha risposta (appare un hang della UI) —
        # minimizza invece di chiedere.
        if self._service.is_peer_connected and not self._force_quit:
            logger.info("HostWindow close with active remote session and no tray → minimize")
            event.ignore()
            self.showMinimized()
            return

        logger.info(
            "HostWindow close → quit (force=%s, peer=%s)",
            self._force_quit,
            self._service.is_peer_connected,
        )
        self._service.stop()
        if self._tray is not None:
            self._tray.hide()
        if self._chat_panel:
            self._chat_panel.close()
            self._chat_panel.deleteLater()
            self._chat_panel = None
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def _parse_host_args(argv: list[str]) -> tuple[list[str], bool, int | None]:
    """Estrai dagli argomenti opzioni host-spécifiche prima di Qt.

    Returns
    -------
    (remaining, start_minimized, log_level)
    """
    import sys

    from opendesk.utils.logger import parse_log_level

    remaining: list[str] = []
    start_minimized = False
    log_level: int | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--minimized", "--tray", "--start-minimized", "--minimized-only"):
            start_minimized = True
        elif arg.startswith(("--minimized=", "--tray=")):
            value = arg.split("=", 1)[1].strip().lower()
            start_minimized = value not in ("0", "false", "no", "off")
        elif arg == "--log-level" and i + 1 < len(argv):
            log_level = parse_log_level(argv[i + 1])
            i += 1
        elif arg.startswith("--log-level="):
            log_level = parse_log_level(arg.split("=", 1)[1])
        else:
            remaining.append(arg)
        i += 1

    return [sys.argv[0], *remaining], start_minimized, log_level


def _acquire_single_instance() -> QLocalServer | None:
    """Garanzia di istanza singola (Windows/Linux/macOS).

    Se esiste già un'istanza viva (server locale ``OpenDeskHostInstance``),
    la avvisa di mostrarsi e restituisce ``None``; altrimenti crea il
    server lock e restituisce l'oggetto da tenere vivo.

    Necessario: due istanze condividono lo stesso device_id (QSettings) e
    si kickano a vicenda dal relay in un ping-pong di riconnessioni
    infinito (kick reciproco ogni 20–90s, sessione mai stabile).
    """
    probe = QLocalSocket()
    probe.connectToServer(_SINGLE_INSTANCE_NAME)
    if probe.waitForConnected(300):
        probe.write(b"show\n")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return None
    # Rende l'eventuale lock orfano di un crash precedente (Windows)
    QLocalServer.removeServer(_SINGLE_INSTANCE_NAME)
    server = QLocalServer()
    server.listen(_SINGLE_INSTANCE_NAME)
    return server


def _on_second_instance(window: HostWindow, server: QLocalServer) -> None:
    """Secondo avvio rilevato: scarica le connessioni pendenti e porta in
    primo piano la finestra esistente.

    Il ``server`` arriva dalla closure del segnale: non usiamo
    ``window.sender()`` perché il segnale è collegato a una lambda generica
    (non a uno slot di ``window``), quindi ``sender()`` non lo restituisce.
    """
    while server.hasPendingConnections():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(conn.deleteLater)
            conn.deleteLater()
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()


def main_host() -> None:
    """Start the OpenDesk Host application.

    Supporta ``--minimized`` / ``--tray`` per avviare l'app nascosta nella
    system tray (usato dall'autostart di sistema).
    """
    import sys

    from opendesk.services.pipeline import _QUALITY_FLIP_HYSTERESIS_S
    from opendesk.utils.logger import setup_logging

    # Flag servizio Windows: gestione SCM/autostart elevato (host_service)
    if "--winsvc" in sys.argv:
        from opendesk.host_service import run_service

        run_service()
        return
    if "--install-service" in sys.argv:
        from opendesk.host_service import install_service

        install_service()
        return
    if "--remove-service" in sys.argv:
        from opendesk.host_service import remove_service

        remove_service()
        return

    sys.argv[:], start_minimized, cli_level = _parse_host_args(sys.argv[1:])

    setup_logging(level=cli_level)

    # Mitigazione GIL starvation (RCA 2026-11-14): vedi opendesk/app.py.
    sys.setswitchinterval(0.0005)

    version = __import__("opendesk").__version__
    logger.info(
        "Starting OpenDesk Host v%s (minimized=%s, switch_interval=%.1fms, "
        "quality_hysteresis=%.1fs)",
        version,
        start_minimized,
        sys.getswitchinterval() * 1000,
        _QUALITY_FLIP_HYSTERESIS_S,
    )

    # Log platform configuration
    from opendesk.core.platform_config import get_platform_config

    get_platform_config()  # detect + log summary

    app = QApplication(sys.argv)
    app.setApplicationName("OpenDesk Host")
    app.setOrganizationName("OpenDesk")
    app.setApplicationVersion(version)

    # ── Istanza singola ──────────────────────────────────────────
    # Bisogna crearla dopo QApplication (QLocalServer dipende da Qt).
    lock_server = _acquire_single_instance()
    if lock_server is None:
        logger.info("OpenDesk Host già in esecuzione — attivazione istanza esistente")
        QMessageBox.information(
            None,
            "OpenDesk Host",
            "OpenDesk Host è già in esecuzione (icona nella system tray).",
        )
        return

    app.setWindowIcon(HostWindow._app_icon())
    app.setStyle("Fusion")

    # La finestra puo' essere nascosta nella tray: non uscire quando non
    # resta nessuna finestra visibile.
    app.setQuitOnLastWindowClosed(False)

    # Applica il tema chiaro (riusa dalla app principale)
    from opendesk.app import load_stylesheet

    load_stylesheet(app, "light")

    # Crea servizio e finestra
    service = HostService()
    window = HostWindow(service, start_minimized=start_minimized)

    # Diagnostica: logga (con stack) gli stalli del main thread.  Avviato
    # dopo la costruzione della finestra: la prima enumerazione dei font
    # DirectWrite blocca il main thread per alcuni secondi anche senza
    # alcun deadlock (RCA 2026-11-21), e veniva segnalata come hang.
    from opendesk.utils.hang_watchdog import HangWatchdog

    _hang_watchdog = HangWatchdog()  # noqa: F841 — tenuto vivo dal riferimento globale

    # Istante singolo: un secondo avvio riporta in primo piano questa finestra
    lock_server.newConnection.connect(lambda: _on_second_instance(window, lock_server))

    # Inizializza sessione e avvia
    service.create_session()

    if start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
        logger.info("Starting minimized to system tray")
        window.hide()
    else:
        if start_minimized:
            logger.warning(
                "--minimized richiesto ma system tray non disponibile — mostro la finestra"
            )
        window.show()

    service.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main_host()
