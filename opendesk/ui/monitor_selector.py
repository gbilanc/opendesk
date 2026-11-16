"""
Monitor selection widget.

Allows the user to select which remote monitor to view,
with preview thumbnails and names.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.screen_capture import MonitorInfo

logger = logging.getLogger(__name__)


class MonitorSelector(QDialog):
    """Dialog to select a monitor to view."""

    monitor_selected = Signal(int)  # monitor index

    def __init__(
        self,
        monitors: list[MonitorInfo],
        current_index: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Monitor")
        self.setMinimumWidth(400)
        self.setModal(True)

        self._monitors = monitors
        self._selected_index = current_index

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Remote Monitors")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        subtitle = QLabel(f"{len(self._monitors)} monitor(s) detected on the remote computer.")
        subtitle.setObjectName("DialogSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Monitor list
        self._list = QListWidget()
        self._list.setObjectName("MonitorList")
        self._list.currentRowChanged.connect(self._on_row_changed)

        for i, mon in enumerate(self._monitors):
            size_str = f"{mon.width}×{mon.height}"
            primary_str = " (Primary)" if mon.is_primary else ""
            text = f"🖥  {mon.name}{primary_str}\n   {size_str} at ({mon.left},{mon.top})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)

        layout.addWidget(self._list, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setProperty("class", "secondary")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._select_btn = QPushButton("Show Monitor")
        self._select_btn.setProperty("class", "primary")
        self._select_btn.clicked.connect(self._on_select)
        btn_layout.addWidget(self._select_btn)

        layout.addLayout(btn_layout)

        # Select current monitor
        if 0 <= self._selected_index < self._list.count():
            self._list.setCurrentRow(self._selected_index)

    @Slot(int)
    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._monitors):
            self._selected_index = row

    @Slot()
    def _on_select(self) -> None:
        self.monitor_selected.emit(self._selected_index)
        self.accept()
