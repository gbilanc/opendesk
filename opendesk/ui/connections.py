"""
Connection manager UI.

Provides:
- Connection dialog (enter remote ID + password)
- Recent connections list (persisted)
- Session status display
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opendesk.core.device_registry import DeviceEntry

from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_RECENT_FILE = Path.home() / ".opendesk" / "recent_connections.json"


@dataclass
class RecentConnection:
    """A previously used connection."""

    peer_id: str
    host: str = ""
    port: int = 8474
    label: str = ""
    last_used: float = 0.0


def _load_recent() -> list[RecentConnection]:
    """Load recent connections from disk."""
    if not _RECENT_FILE.exists():
        return []
    try:
        data = json.loads(_RECENT_FILE.read_text())
        return [
            RecentConnection(**c) for c in data.get("connections", [])
        ]
    except Exception as e:
        logger.warning("Failed to load recent connections: %s", e)
        return []


def _save_recent(connections: list[RecentConnection]) -> None:
    """Save recent connections to disk."""
    _RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "connections": [
            {"peer_id": c.peer_id, "host": c.host, "port": c.port,
             "label": c.label, "last_used": c.last_used}
            for c in connections
        ]
    }
    _RECENT_FILE.write_text(json.dumps(data, indent=2))


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

    connection_requested = Signal(str, str)  # peer_id, password

    WIDTH = 480
    HEIGHT = 480

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
        self._recent = _load_recent()

        self._setup_ui()
        self._populate_device_list()
        self._populate_recent()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(20, 16, 20, 16)

        # ── Title ──
        title = QLabel("Remote Desktop Connection")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # ── Device list (online/offline) ──
        self._device_section_label = QLabel("Dispositivi conosciuti:")
        self._device_section_label.setStyleSheet(
            "font-size: 12px; font-weight: 600; margin-top: 4px;"
        )
        layout.addWidget(self._device_section_label)

        self._device_list = QListWidget()
        self._device_list.setMaximumHeight(120)
        self._device_list.itemClicked.connect(self._on_device_selected)
        layout.addWidget(self._device_list)

        # ── Separator ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("max-height: 1px; margin: 4px 0;")
        layout.addWidget(sep)

        # ── Manual form ──
        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._peer_id_input = QLineEdit()
        self._peer_id_input.setPlaceholderText("e.g. 123 456 789")
        self._peer_id_input.setMinimumHeight(38)
        self._peer_id_input.setStyleSheet("""
            font-size: 16px;
            font-weight: 600;
            letter-spacing: 2px;
        """)
        self._peer_id_input.textChanged.connect(self._on_input_changed)
        form.addRow("Session ID:", self._peer_id_input)

        self._password_input = QLineEdit()
        self._password_input.setPlaceholderText("One-time password")
        self._password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_input.setMinimumHeight(38)
        self._password_input.setStyleSheet("font-size: 14px;")
        self._password_input.returnPressed.connect(self._on_connect)
        self._password_input.textChanged.connect(self._on_input_changed)
        form.addRow("Password:", self._password_input)

        layout.addLayout(form)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setEnabled(False)
        self._connect_btn.setObjectName("PrimaryButton")
        self._connect_btn.clicked.connect(self._on_connect)
        btn_layout.addWidget(self._connect_btn)

        layout.addLayout(btn_layout)

        # ── Recent connections (compact) ──
        self._recent_label = QLabel("Connessioni recenti:")
        self._recent_label.setStyleSheet("font-size: 12px; margin-top: 4px;")
        layout.addWidget(self._recent_label)

        self._recent_list = QListWidget()
        self._recent_list.setMaximumHeight(80)
        self._recent_list.itemClicked.connect(self._on_recent_selected)
        layout.addWidget(self._recent_list)

    # ── slots ───────────────────────────────────────────────────────

    @Slot()
    def _on_input_changed(self) -> None:
        """Enable/disable connect button based on input validity."""
        peer_id = self._peer_id_input.text().strip()
        has_password = bool(self._password_input.text().strip())
        self._connect_btn.setEnabled(len(peer_id) >= 6 and has_password)

    @Slot()
    def _on_connect(self) -> None:
        """Collect input and emit signal."""
        peer_id = self._peer_id_input.text().strip()
        password = self._password_input.text().strip()

        if not peer_id or not password:
            QMessageBox.warning(
                self, "Missing Information",
                "Please enter both Session ID and Password.",
            )
            return

        self._save_recent(peer_id)
        self.connection_requested.emit(peer_id, password)
        self.accept()

    @Slot(QListWidgetItem)
    def _on_recent_selected(self, item: QListWidgetItem) -> None:
        """Fill form from a recent connection."""
        peer_id = item.data(Qt.ItemDataRole.UserRole)
        if peer_id:
            self._peer_id_input.setText(peer_id)
            self._password_input.setFocus()

    # ── device list ────────────────────────────────────────────────

    def update_device_list(self, devices: list[DeviceEntry]) -> None:
        """Update the displayed device list (called from MainWindow)."""
        self._devices = devices
        self._populate_device_list()

    def _populate_device_list(self) -> None:
        """Populate the device list with online/offline indicators."""
        self._device_list.clear()

        if not self._devices:
            self._device_section_label.hide()
            self._device_list.hide()
            return

        self._device_section_label.show()
        self._device_list.show()

        for dev in self._devices:
            status = "🟢" if dev.online else "🔴"
            display = f"{status}  {dev.device_name}"
            if dev.online:
                display += f"  (ID: {dev.session_id})"
            else:
                display += f"  — offline"

            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, dev.device_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, dev.session_id)  # session_id
            item.setData(Qt.ItemDataRole.UserRole + 2, dev.trusted)  # trusted

            if dev.trusted:
                item.setToolTip("Dispositivo pre-autorizzato — connessione senza password")

            self._device_list.addItem(item)

    @Slot(QListWidgetItem)
    def _on_device_selected(self, item: QListWidgetItem) -> None:
        """Fill the form from a selected device."""
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = item.data(Qt.ItemDataRole.UserRole + 2) or False

        self._peer_id_input.setText(session_id)

        if trusted:
            # Auto-connect for pre-authorized devices
            # Use a fixed/blank password — the relay will handle it
            self._password_input.setText("pre-authorized")
            self._on_connect()
        else:
            self._password_input.setFocus()

    # ── recent connections ──────────────────────────────────────────

    def _populate_recent(self) -> None:
        """Populate the recent connections list."""
        self._recent_list.clear()
        if not self._recent:
            self._recent_label.hide()
            self._recent_list.hide()
            return

        self._recent_label.show()
        self._recent_list.show()
        for rc in self._recent[-8:]:  # show last 8
            item = QListWidgetItem(rc.peer_id)
            item.setData(Qt.ItemDataRole.UserRole, rc.peer_id)
            if rc.label:
                item.setText(f"{rc.label} ({rc.peer_id})")
            self._recent_list.addItem(item)

    def _save_recent(self, peer_id: str) -> None:
        """Add a connection to the recent list."""
        # Remove existing entry with same peer_id
        self._recent = [c for c in self._recent if c.peer_id != peer_id]
        import time
        self._recent.append(RecentConnection(
            peer_id=peer_id,
            last_used=time.time(),
        ))
        _save_recent(self._recent)


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
