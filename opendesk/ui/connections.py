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

from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
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

    Emits ``connection_requested(peer_id, password)`` when the user
    clicks "Connect".
    """

    connection_requested = Signal(str, str)  # peer_id, password

    WIDTH = 420
    HEIGHT = 380

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to Remote Computer")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setModal(True)

        self._setup_ui()
        self._recent = _load_recent()
        self._populate_recent()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # ── Title ──
        title = QLabel("Remote Desktop Connection")
        title.setStyleSheet("""
            font-size: 18px;
            font-weight: 700;
            color: #0f172a;
        """)
        layout.addWidget(title)

        subtitle = QLabel(
            "Enter the remote computer's session ID and password."
        )
        subtitle.setStyleSheet("font-size: 13px; color: #64748b;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # ── Form ──
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._peer_id_input = QLineEdit()
        self._peer_id_input.setPlaceholderText("e.g. 123 456 789")
        self._peer_id_input.setMinimumHeight(42)
        self._peer_id_input.setStyleSheet("""
            QLineEdit {
                padding: 10px 14px;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 600;
                letter-spacing: 2px;
            }
            QLineEdit:focus {
                border-color: #2563eb;
            }
        """)
        self._peer_id_input.textChanged.connect(self._on_input_changed)
        form.addRow("Session ID:", self._peer_id_input)

        self._password_input = QLineEdit()
        self._password_input.setPlaceholderText("One-time password")
        self._password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_input.setMinimumHeight(42)
        self._password_input.setStyleSheet("""
            QLineEdit {
                padding: 10px 14px;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #2563eb;
            }
        """)
        self._password_input.returnPressed.connect(self._on_connect)
        self._password_input.textChanged.connect(self._on_input_changed)
        form.addRow("Password:", self._password_input)

        layout.addLayout(form)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                padding: 10px 24px;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                background: #ffffff;
                font-size: 14px;
            }
            QPushButton:hover { background: #f8fafc; }
        """)
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setEnabled(False)
        self._connect_btn.setStyleSheet("""
            QPushButton {
                padding: 10px 24px;
                border: none;
                border-radius: 8px;
                background: #2563eb;
                color: white;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:disabled { background: #94a3b8; }
        """)
        self._connect_btn.clicked.connect(self._on_connect)
        btn_layout.addWidget(self._connect_btn)

        layout.addLayout(btn_layout)

        # ── Recent connections (compact) ──
        self._recent_label = QLabel("Recent connections:")
        self._recent_label.setStyleSheet("""
            font-size: 12px;
            color: #94a3b8;
            margin-top: 8px;
        """)
        layout.addWidget(self._recent_label)

        self._recent_list = QListWidget()
        self._recent_list.setMaximumHeight(100)
        self._recent_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px 10px;
                border-radius: 6px;
            }
            QListWidget::item:hover {
                background: #f1f5f9;
            }
            QListWidget::item:selected {
                background: #dbeafe;
                color: #0f172a;
            }
        """)
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
        self._indicator.setStyleSheet("color: #94a3b8; font-size: 16px;")
        layout.addWidget(self._indicator)

        self._label = QLabel("Disconnected")
        self._label.setStyleSheet("font-size: 12px; color: #64748b;")
        layout.addWidget(self._label)

    @Slot(str)
    def set_status(self, status: str, connected: bool = False) -> None:
        """Update the displayed status."""
        self._label.setText(status)
        color = "#22c55e" if connected else "#94a3b8"
        self._indicator.setStyleSheet(f"color: {color}; font-size: 16px;")
