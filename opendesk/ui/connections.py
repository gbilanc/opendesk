"""
Connection manager UI.

Provides:
- Connection dialog (enter remote ID + password)
- Recent connections list (persisted)
- Session status display
"""

from __future__ import annotations

import logging

from opendesk.core.device_registry import DeviceEntry

from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection dialog
# ---------------------------------------------------------------------------


class ConnectionDialog(QDialog):
    """Dialog for connecting to a remote computer.

    Shows a list of known devices from the relay (with online/offline
    status) alongside the manual session ID / password form.
    Emits ``connection_requested(peer_id, password)`` when the user
    clicks "Connect".
    """

    connection_requested = Signal(str, str)  # session_id, password

    WIDTH = 420
    HEIGHT = 380

    def __init__(
        self,
        devices: list[DeviceEntry] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to Remote Computer")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setModal(True)

        self._devices: list[DeviceEntry] = devices or []

        self._setup_ui()
        self._populate_device_list()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # ── Title ──
        title = QLabel("Seleziona un dispositivo")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Scegli un dispositivo dalla lista per connetterti."
        )
        subtitle.setStyleSheet("font-size: 13px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # ── Device list ──
        self._device_list = QListWidget()
        self._device_list.itemClicked.connect(self._on_device_selected)
        self._device_list.itemDoubleClicked.connect(self._on_device_double_clicked)
        layout.addWidget(self._device_list, 1)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self._cancel_btn = QPushButton("Annulla")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._connect_btn = QPushButton("Connetti")
        self._connect_btn.setEnabled(False)
        self._connect_btn.setObjectName("PrimaryButton")
        self._connect_btn.clicked.connect(self._on_connect)
        btn_layout.addWidget(self._connect_btn)

        layout.addLayout(btn_layout)

    # ── public API ──────────────────────────────────────────────────

    def update_device_list(self, devices: list[DeviceEntry]) -> None:
        """Update the displayed device list (called from MainWindow)."""
        self._devices = devices
        self._populate_device_list()

    # ── device list ────────────────────────────────────────────────

    def _populate_device_list(self) -> None:
        """Populate the device list with online/offline indicators."""
        self._device_list.clear()

        if not self._devices:
            item = QListWidgetItem("Nessun dispositivo trovato.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._device_list.addItem(item)
            self._connect_btn.setEnabled(False)
            return

        for dev in self._devices:
            status = "🟢" if dev.online else "🔴"
            display = f"{status}  {dev.device_name}"
            if dev.online:
                display += f"  — in linea"
            else:
                display += f"  — offline"

            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, dev.device_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, dev.session_id)
            item.setData(Qt.ItemDataRole.UserRole + 2, dev.trusted)

            if dev.trusted:
                item.setToolTip("Pre-autorizzato — connessione senza password")

            self._device_list.addItem(item)

    @Slot(QListWidgetItem)
    def _on_device_selected(self, item: QListWidgetItem) -> None:
        """Enable the connect button when a device is selected."""
        device_id = item.data(Qt.ItemDataRole.UserRole)
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = item.data(Qt.ItemDataRole.UserRole + 2) or False

        can_connect = bool(device_id and session_id)
        self._connect_btn.setEnabled(can_connect)

        if trusted and can_connect:
            # Auto-connect for pre-authorized devices (no password needed)
            self._on_connect()

    @Slot(QListWidgetItem)
    def _on_device_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-click to connect."""
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        if session_id:
            self._on_connect()

    @Slot()
    def _on_connect(self) -> None:
        """Connect to the selected device."""
        item = self._device_list.currentItem()
        if item is None:
            return

        device_id = item.data(Qt.ItemDataRole.UserRole) or ""
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = item.data(Qt.ItemDataRole.UserRole + 2) or False

        if not session_id:
            QMessageBox.warning(
                self, "Dispositivo offline",
                "Questo dispositivo non è attualmente connesso al relay.\n"
                "Riprova più tardi.",
            )
            return

        # Pre-authorized: use empty password (relay will skip auth)
        password = "" if trusted else self._prompt_password(device_id)
        if password is None:  # user cancelled
            return

        self.connection_requested.emit(session_id, password)
        self.accept()

    def _prompt_password(self, device_id: str) -> str | None:
        """Ask the user for the connection password.

        Returns the password or ``None`` if cancelled.
        """
        from PySide6.QtWidgets import QInputDialog

        pwd, ok = QInputDialog.getText(
            self, "Password richiesta",
            f"Inserisci la password per il dispositivo:\n{device_id[:8]}…",
            QLineEdit.EchoMode.Password,
        )
        return pwd if ok else None


# ---------------------------------------------------------------------------
# Session status widget
# ---------------------------------------------------------------------------


class SessionStatusWidget(QWidget):
    """Shows the status of the current session in the status bar area."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._indicator = QLabel("●")
        self._indicator.setStyleSheet("font-size: 16px;")
        layout.addWidget(self._indicator)

        self._label = QLabel("Disconnected")
        layout.addWidget(self._label)

    @Slot(str)
    def set_status(self, status: str, connected: bool = False) -> None:
        """Update the displayed status."""
        self._label.setText(status)
        color = "#22c55e" if connected else "#64748b"
        self._indicator.setStyleSheet(f"color: {color}; font-size: 16px;")
