"""
Dual-pane file transfer UI with local/remote file browser.

Provides:
- ``FileSystemModel`` — QAbstractItemModel for remote filesystem browsing
- ``TransferListModel`` — QAbstractListModel for transfer jobs
- ``TransferDelegate`` — QStyledItemDelegate with progress bar
- ``FileBrowserDock`` — dual-pane window with local (left) and remote (right) browser
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QDir,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileSystemModel,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.file_transfer import TransferDirection, TransferJob, TransferState

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

_MAX_VISIBLE_ITEMS = 20
_ITEM_HEIGHT = 72
_PROGRESS_BAR_HEIGHT = 6

# Velocità stimata (media esponenziale) e ETA
_SPEED_SAMPLE_MIN_DT = 0.15  # s — sotto questa soglia il campione è troppo rumoroso
_SPEED_SMOOTHING = 0.3  # peso del campione nuovo nella media


# ═══════════════════════════════════════════════════════════════════
# Remote file system model
# ═══════════════════════════════════════════════════════════════════


class RemoteFileSystemModel(QAbstractItemModel):
    """Modello della directory remota corrente (una sola cartella per volta).

    Il pannello mostra sempre il contenuto dell'ultima directory richiesta:
    la navigazione avviene cambiando ``set_directory_contents()``, non
    espandendo nodi.  Così non serve ricostruire/agganciare un albero (e i
    path remoti di un altro OS — es. ``C:\\Users\\mario`` — non vanno
    interpretati localmente).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_path: str = ""
        self._nodes: list[dict] = []

    # ── QAbstractItemModel interface ────────────────────────────────

    def rowCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._nodes)

    def columnCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:  # noqa: N802
        return 0 if parent.isValid() else 4  # name, size, type, modified

    def index(
        self, row: int, column: int, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> QModelIndex:
        if parent.isValid() or not (0 <= row < len(self._nodes)) or not (0 <= column < 4):
            return QModelIndex()
        return self.createIndex(row, column, self._nodes[row])

    def parent(self, index: QModelIndex | QPersistentModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        return QModelIndex()  # modello piatto

    def hasChildren(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> bool:  # noqa: N802
        return not parent.isValid() and bool(self._nodes)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if not isinstance(node, dict):
            return None

        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            match col:
                case 0:
                    return node.get("name", "")
                case 1:
                    size = node.get("size", 0)
                    return self._format_size(size) if not node.get("is_dir") else ""
                case 2:
                    return "Directory" if node.get("is_dir") else "File"
                case 3:
                    mtime = node.get("mtime", 0)
                    if mtime:
                        from datetime import datetime

                        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    return ""
                case _:
                    return ""

        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return self._icon_for_node(node)

        if role == Qt.ItemDataRole.ToolTipRole:
            return node.get("path", node.get("name", ""))

        if role == Qt.ItemDataRole.UserRole:
            return node

        # Foreground color for directories
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            if node.get("is_dir"):
                return QColor("#2563eb")

        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            headers = ["Name", "Size", "Type", "Modified"]
            if 0 <= section < len(headers):
                return headers[section]
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags

    # ── public API ──────────────────────────────────────────────────

    def set_directory_contents(self, path: str, entries: list[dict]) -> None:
        """Sostituisce il contenuto mostrato con ``entries`` di ``path``."""
        base = path.rstrip("/\\")  # path POSIX o Windows
        nodes: list[dict] = []
        for entry in entries:
            name = str(entry.get("name", ""))
            if not name:
                continue
            nodes.append(
                {
                    "name": name,
                    "is_dir": bool(entry.get("is_dir", False)),
                    "size": entry.get("size", 0),
                    "mtime": entry.get("mtime", 0.0),
                    "path": f"{base}/{name}" if base else f"/{name}",
                }
            )
        # Directory prima, poi file, entrambi in ordine alfabetico
        nodes.sort(key=lambda n: (not n["is_dir"], n["name"].lower()))

        self.beginResetModel()
        self._current_path = path
        self._nodes = nodes
        self.endResetModel()

    def clear(self) -> None:
        """Svuota la lista (disconnessione)."""
        self.beginResetModel()
        self._current_path = ""
        self._nodes = []
        self.endResetModel()

    @property
    def current_path(self) -> str:
        return self._current_path

    def node_at(self, index: QModelIndex) -> dict | None:
        """Get the node dict for a given index."""
        if not index.isValid():
            return None
        node = index.internalPointer()
        return node if isinstance(node, dict) else None

    def path_for_index(self, index: QModelIndex) -> str:
        """Get the full path for a given index."""
        node = self.node_at(index)
        if node:
            return node.get("path", node.get("name", ""))
        return ""

    # ── internal ────────────────────────────────────────────────────

    @staticmethod
    def _icon_for_node(node: dict):
        """Icona dal tema di sistema (niente emoji: non tutti i font le hanno)."""
        pm = (
            QStyle.StandardPixmap.SP_DirIcon
            if node.get("is_dir")
            else QStyle.StandardPixmap.SP_FileIcon
        )
        return QApplication.style().standardIcon(pm)

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ═══════════════════════════════════════════════════════════════════
# Transfer list model
# ═══════════════════════════════════════════════════════════════════


class TransferListModel(QAbstractListModel):
    """Model-View compliant model for a list of transfer jobs."""

    countChanged = Signal(int)  # noqa: N815

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._jobs: list[TransferJob] = []
        # job_id -> (timestamp, bytes, velocità B/s stimata)
        self._samples: dict[str, tuple[float, int, float]] = {}

    # ── QAbstractListModel interface ────────────────────────────────

    def rowCount(  # noqa: N802
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._jobs)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if not index.isValid():
            return None
        job = self._jobs[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return job.file_info.name
        if role == Qt.ItemDataRole.UserRole:
            return job.id
        if role == Qt.ItemDataRole.UserRole + 1:
            return job.state
        if role == Qt.ItemDataRole.UserRole + 2:
            return job.progress
        if role == Qt.ItemDataRole.UserRole + 3:
            return job.file_info.size
        if role == Qt.ItemDataRole.UserRole + 4:
            return job.bytes_transferred
        if role == Qt.ItemDataRole.UserRole + 5:
            return job.direction
        if role == Qt.ItemDataRole.UserRole + 6:
            return job.error
        if role == Qt.ItemDataRole.UserRole + 7:
            return self._samples.get(job.id, (0.0, 0, 0.0))[2]
        if role == Qt.ItemDataRole.UserRole + 8:
            speed = self._samples.get(job.id, (0.0, 0, 0.0))[2]
            if job.state != TransferState.IN_PROGRESS or speed <= 0:
                return -1.0
            remaining = max(0, job.file_info.size - job.bytes_transferred)
            return remaining / speed
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{job.file_info.name} — {job.state.name.replace('_', ' ').title()}"
            if job.error:
                tip += f"\n{job.error}"
            return tip
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ── public API ──────────────────────────────────────────────────

    def upsert(self, job: TransferJob) -> None:
        """Add a new job or update an existing one (by ID)."""
        self._update_speed_sample(job)
        for i, existing in enumerate(self._jobs):
            if existing.id == job.id:
                self._jobs[i] = job
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [])
                return
        # New job
        row = len(self._jobs)
        self.beginInsertRows(QModelIndex(), row, row)
        self._jobs.append(job)
        self.endInsertRows()
        self.countChanged.emit(len(self._jobs))

    def remove(self, job_id: str) -> bool:
        """Remove a job by ID."""
        for i, job in enumerate(self._jobs):
            if job.id == job_id:
                self._samples.pop(job_id, None)
                self.beginRemoveRows(QModelIndex(), i, i)
                self._jobs.pop(i)
                self.endRemoveRows()
                self.countChanged.emit(len(self._jobs))
                return True
        return False

    def clear_completed(self) -> bool:
        """Remove all completed/failed/cancelled jobs.

        Returns
        -------
        bool
            ``True`` if at least one job was removed, ``False`` otherwise.
        """
        completed_states = {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
        to_remove = [i for i, j in enumerate(self._jobs) if j.state in completed_states]
        for i in reversed(to_remove):
            self._samples.pop(self._jobs[i].id, None)
            self.beginRemoveRows(QModelIndex(), i, i)
            self._jobs.pop(i)
            self.endRemoveRows()
        if to_remove:
            self.countChanged.emit(len(self._jobs))
        return len(to_remove) > 0

    def job_at(self, row: int) -> TransferJob | None:
        if 0 <= row < len(self._jobs):
            return self._jobs[row]
        return None

    @property
    def active_count(self) -> int:
        return sum(1 for j in self._jobs if j.state == TransferState.IN_PROGRESS)

    @property
    def completed_count(self) -> int:
        """Job in stato terminale, candidati a ``clear_completed``."""
        return sum(
            1
            for j in self._jobs
            if j.state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED)
        )

    def _update_speed_sample(self, job: TransferJob) -> None:
        """Aggiorna la stima di velocità (media esponenziale) per il job."""
        if job.state != TransferState.IN_PROGRESS:
            return
        now = time.monotonic()
        prev = self._samples.get(job.id)
        if prev is None:
            self._samples[job.id] = (now, job.bytes_transferred, 0.0)
            return
        dt = now - prev[0]
        if dt < _SPEED_SAMPLE_MIN_DT:
            return
        instant = max(0.0, (job.bytes_transferred - prev[1]) / dt)
        speed = (
            instant
            if prev[2] <= 0
            else prev[2] * (1 - _SPEED_SMOOTHING) + instant * _SPEED_SMOOTHING
        )
        self._samples[job.id] = (now, job.bytes_transferred, speed)


# ═══════════════════════════════════════════════════════════════════
# Transfer delegate — custom rendering with progress bar
# ═══════════════════════════════════════════════════════════════════


class TransferDelegate(QStyledItemDelegate):
    """Rendering di un transfer: nome, badge di stato, metadati, barra progresso.

    I colori vengono presi da ``QPalette`` (non da costanti hardcoded) così la
    lista resta leggibile sia nel tema chiaro sia in quello scuro.
    """

    _STATE_LABELS: dict[TransferState, str] = {
        TransferState.PENDING: "Queued",
        TransferState.ACCEPTED: "Accepted",
        TransferState.IN_PROGRESS: "Transferring",
        TransferState.PAUSED: "Paused",
        TransferState.COMPLETED: "Completed",
        TransferState.FAILED: "Failed",
        TransferState.CANCELLED: "Cancelled",
    }

    # Colori semantici, variante chiara e scura
    _STATE_COLORS_LIGHT: dict[TransferState, str] = {
        TransferState.PENDING: "#b45309",
        TransferState.ACCEPTED: "#1d4ed8",
        TransferState.IN_PROGRESS: "#1d4ed8",
        TransferState.PAUSED: "#b45309",
        TransferState.COMPLETED: "#15803d",
        TransferState.FAILED: "#b91c1c",
        TransferState.CANCELLED: "#475569",
    }
    _STATE_COLORS_DARK: dict[TransferState, str] = {
        TransferState.PENDING: "#fbbf24",
        TransferState.ACCEPTED: "#60a5fa",
        TransferState.IN_PROGRESS: "#60a5fa",
        TransferState.PAUSED: "#fbbf24",
        TransferState.COMPLETED: "#4ade80",
        TransferState.FAILED: "#f87171",
        TransferState.CANCELLED: "#94a3b8",
    }

    def _state_color(self, state: TransferState, dark: bool) -> QColor:
        table = self._STATE_COLORS_DARK if dark else self._STATE_COLORS_LIGHT
        return QColor(table.get(state, "#64748b"))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pal = option.palette
        # NB: la lista ha ``background: transparent`` in QSS, quindi il role
        # Base e' trasparente (alpha 0) e dipingerebbe nero: usiamo Window,
        # che e' il colore del pannello che ospita la lista.
        base = pal.color(QPalette.ColorRole.Window)
        dark = base.lightness() < 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        text_color = pal.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        muted_color = pal.color(QPalette.ColorRole.PlaceholderText)
        if not muted_color.isValid() or muted_color == text_color:
            muted_color = pal.color(QPalette.ColorRole.Mid)

        rect = option.rect
        painter.fillRect(rect, pal.color(QPalette.ColorRole.Highlight) if selected else base)
        separator = pal.color(QPalette.ColorRole.Mid)
        separator.setAlpha(50)
        painter.setPen(QPen(separator, 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        content = rect.adjusted(14, 8, -14, -8)

        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        state: TransferState = index.data(Qt.ItemDataRole.UserRole + 1)
        direction: TransferDirection = index.data(Qt.ItemDataRole.UserRole + 5)
        total = index.data(Qt.ItemDataRole.UserRole + 3) or 0
        done = index.data(Qt.ItemDataRole.UserRole + 4) or 0
        progress = float(index.data(Qt.ItemDataRole.UserRole + 2) or 0.0)
        error = index.data(Qt.ItemDataRole.UserRole + 6) or ""
        speed = float(index.data(Qt.ItemDataRole.UserRole + 7) or 0.0)
        eta = index.data(Qt.ItemDataRole.UserRole + 8)

        state_color = self._state_color(state, dark)

        # ── Badge di stato (destra) ──
        badge_font = QFont(option.font)
        badge_font.setPointSizeF(max(8.0, option.font.pointSizeF() - 1.0))
        badge_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(badge_font)
        fm = painter.fontMetrics()
        label = self._STATE_LABELS.get(state, state.name.replace("_", " ").title())
        pill_w = fm.horizontalAdvance(label) + 20
        pill_h = fm.height() + 6
        pill_rect = QRect(content.right() - pill_w, content.top(), pill_w, pill_h)
        pill_bg = QColor(state_color)
        pill_bg.setAlpha(40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(pill_bg))
        painter.drawRoundedRect(pill_rect, pill_h / 2, pill_h / 2)
        painter.setPen(state_color)
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, label)

        # ── Nome file (sinistra, eliso al centro) ──
        name_font = QFont(option.font)
        name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        painter.setPen(text_color)
        fm = painter.fontMetrics()
        arrow = "\u2191" if direction == TransferDirection.SEND else "\u2193"
        name_rect = QRect(
            content.left(), content.top(), max(0, pill_rect.left() - content.left() - 12), pill_h
        )
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fm.elidedText(f"{arrow}  {name}", Qt.TextElideMode.ElideMiddle, name_rect.width()),
        )

        # ── Riga metadati: percentuale · byte · velocità · ETA (o errore) ──
        meta_font = QFont(option.font)
        meta_font.setPointSizeF(max(8.0, option.font.pointSizeF() - 1.0))
        painter.setFont(meta_font)
        meta_rect = QRect(
            content.left(),
            content.top() + pill_h + 4,
            content.width(),
            int(meta_font.pointSizeF()) + 12,
        )
        if state == TransferState.FAILED and error:
            painter.setPen(state_color)
            meta = f"\u2715  {error}"
        else:
            painter.setPen(muted_color)
            parts = [
                f"{int(progress * 100)}%",
                f"{self._fmt_size(done)} / {self._fmt_size(total)}",
            ]
            if speed > 0 and state == TransferState.IN_PROGRESS:
                parts.append(f"{self._fmt_size(int(speed))}/s")
            if eta is not None and eta >= 0 and state == TransferState.IN_PROGRESS:
                parts.append(f"ETA {self._fmt_eta(eta)}")
            meta = "   ·   ".join(parts)
        painter.drawText(
            meta_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width()),
        )

        # ── Barra di progresso ──
        bar_top = content.bottom() - _PROGRESS_BAR_HEIGHT
        bar_rect = QRect(content.left(), bar_top - 4, content.width(), _PROGRESS_BAR_HEIGHT)
        track = QColor(muted_color)
        track.setAlpha(60)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(track))
        painter.drawRoundedRect(bar_rect, 3, 3)

        fill_w = int(bar_rect.width() * min(1.0, max(0.0, progress)))
        if fill_w > 0:
            painter.setBrush(QBrush(state_color))
            painter.drawRoundedRect(
                QRect(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height()), 3, 3
            )

        painter.restore()

    def sizeHint(  # noqa: N802
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:  # noqa: N802
        return QSize(320, _ITEM_HEIGHT)

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        s = int(max(0, seconds))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60:02d}s"
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"


# ═══════════════════════════════════════════════════════════════════
# File browser dock — main dual-pane widget
# ═══════════════════════════════════════════════════════════════════


class FileBrowserDock(QDialog):
    """Dual-pane file browser for local and remote filesystems.

    Supports:
    - Local filesystem navigation (left pane)
    - Remote filesystem navigation (right pane)
    - Upload: select local files → send to remote
    - Download: select remote files → receive to local
    - Active transfer list with progress
    """

    # Signals
    file_upload_requested = Signal(list, str)  # (local_paths, remote_dest_dir)
    file_download_requested = Signal(list, str)  # (remote_paths, local_dest_dir)
    remote_listing_requested = Signal(str)  # remote path to list
    navigate_local = Signal(str)  # local path to navigate to

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Transfer")
        self.setMinimumSize(900, 550)
        self.resize(1100, 650)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # ── Models ──
        self._transfer_model = TransferListModel(self)
        self._local_model = QFileSystemModel(self)
        self._local_model.setRootPath(QDir.rootPath())
        self._local_model.setFilter(
            QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        )
        self._remote_model = RemoteFileSystemModel(self)

        # Current paths
        self._local_root = str(Path.home())
        self._remote_path = "/"

        self._build_ui()
        self._connect_signals()

        # Shortcut: Escape to clear selection
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc_shortcut.activated.connect(self._clear_selection)

    # ── UI construction ─────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build the complete UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Toolbar ──
        toolbar = self._build_toolbar()
        layout.addWidget(toolbar)

        # ── Dual-pane splitter ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        # Left: Local filesystem
        left_panel = self._build_pane(
            title="Local",
            model=self._local_model,
            is_local=True,
        )
        splitter.addWidget(left_panel)

        # Right: Remote filesystem
        right_panel = self._build_pane(
            title="Remote",
            model=self._remote_model,
            is_local=False,
        )
        splitter.addWidget(right_panel)

        splitter.setSizes([450, 450])
        layout.addWidget(splitter, 3)

        # ── Transfer list section ──
        transfers_section = self._build_transfers_section()
        layout.addWidget(transfers_section, 2)

        # ── Status bar ──
        self._status_bar = QStatusBar()
        self._status_bar.setObjectName("fileStatusBar")
        self._status_label = QLabel("Ready")
        self._status_bar.addWidget(self._status_label, 1)

        # Selection info with inline clear button
        self._selection_info = QLabel("")
        self._selection_info.setProperty("class", "accent")

        # Riepilogo trasferimenti (attivi / percentuale complessiva)
        self._transfer_summary = QLabel("")
        self._transfer_summary.setObjectName("transferSummary")
        self._status_bar.addPermanentWidget(self._transfer_summary)
        self._status_bar.addPermanentWidget(self._selection_info)

        self._clear_sel_btn = QPushButton("✕")
        self._clear_sel_btn.setFixedSize(20, 20)
        self._clear_sel_btn.setVisible(False)
        self._clear_sel_btn.setProperty("class", "pill")
        self._status_bar.addPermanentWidget(self._clear_sel_btn)
        layout.addWidget(self._status_bar)

    def _build_toolbar(self) -> QWidget:
        """Build the action toolbar."""
        toolbar = QWidget()
        toolbar.setObjectName("fileBrowserToolbar")

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        # Upload button
        self._upload_btn = QPushButton("↑ Upload")
        self._upload_btn.setMinimumHeight(32)
        self._upload_btn.setProperty("class", "primary")
        self._upload_btn.setEnabled(False)
        layout.addWidget(self._upload_btn)

        # Download button
        self._download_btn = QPushButton("↓ Download")
        self._download_btn.setMinimumHeight(32)
        self._download_btn.setProperty("class", "success")
        self._download_btn.setEnabled(False)
        layout.addWidget(self._download_btn)

        layout.addStretch()

        # Refresh remote button
        self._refresh_btn = QPushButton("\u27f3 Refresh")
        self._refresh_btn.setMinimumHeight(32)
        self._refresh_btn.setProperty("class", "secondary")
        layout.addWidget(self._refresh_btn)

        # Clear completed
        self._clear_btn = QPushButton("Clear completed")
        self._clear_btn.setMinimumHeight(32)
        self._clear_btn.setProperty("class", "secondary")
        self._clear_btn.setEnabled(False)
        self._clear_btn.setToolTip(
            "Remove completed, failed and cancelled transfers from the list"
        )
        layout.addWidget(self._clear_btn)

        return toolbar

    def _build_pane(self, title: str, model, is_local: bool) -> QWidget:
        """Build one side of the dual-pane browser."""
        pane = QWidget()
        pane.setObjectName("filePane")

        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Pane header ──
        header = QWidget()
        header.setObjectName("filePaneHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)

        title_label = QLabel(title)
        title_label.setObjectName("filePaneTitle")
        header_layout.addWidget(title_label)

        # Path bar
        path_bar = QLineEdit()
        path_bar.setObjectName("filePathBar")
        header_layout.addWidget(path_bar, 1)

        # Home button
        home_btn = QPushButton("\u2302")
        home_btn.setFixedSize(28, 28)
        home_btn.setObjectName("fileHomeBtn")
        home_btn.setToolTip("Go to home folder" if is_local else "Go to remote root")
        header_layout.addWidget(home_btn)

        # Up button
        up_btn = QPushButton("\u2191")
        up_btn.setFixedSize(28, 28)
        up_btn.setObjectName("fileHomeBtn")
        up_btn.setToolTip("Up one level")
        header_layout.addWidget(up_btn)

        if is_local:
            self._local_path_bar = path_bar
            self._local_home_btn = home_btn
            self._local_up_btn = up_btn
            path_bar.setText(self._local_root)
        else:
            self._remote_path_bar = path_bar
            self._remote_home_btn = home_btn
            self._remote_up_btn = up_btn
            path_bar.setPlaceholderText("/")

        layout.addWidget(header)

        # ── Tree view ──
        tree = QTreeView()
        tree.setModel(model)
        tree.setAnimated(True)
        tree.setIndentation(20)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tree.setObjectName("fileTree")

        # Hide size/type/modified columns for local (use QFileSystemModel defaults)
        if is_local:
            tree.setRootIndex(self._local_model.index(self._local_root))
            # Show only filename column initially, hide others
            tree.setColumnWidth(0, 250)
            for col in range(1, self._local_model.columnCount()):
                tree.setColumnWidth(col, 80)

        # Configure column widths for remote model
        if not is_local:
            header_view = tree.header()
            header_view.setStretchLastSection(True)
            header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(1, 80)
            header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(2, 70)
            header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(3, 120)

        tree.setObjectName(f"tree_{'local' if is_local else 'remote'}")
        layout.addWidget(tree, 1)

        if is_local:
            self._local_tree = tree
        else:
            self._remote_tree = tree

        return pane

    def _build_transfers_section(self) -> QWidget:
        """Build the active transfers section at the bottom."""
        section = QWidget()
        section.setObjectName("transferSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QLabel("Active transfers")
        header.setObjectName("filePaneTitle")
        self._transfers_header = header
        layout.addWidget(header)

        self._transfer_list = QListView()
        self._transfer_list.setModel(self._transfer_model)
        self._transfer_list.setItemDelegate(TransferDelegate(self))
        self._transfer_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._transfer_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transfer_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._transfer_list.setFrameShape(QFrame.Shape.NoFrame)
        self._transfer_list.setMinimumHeight(80)
        self._transfer_list.setMaximumHeight(260)
        self._transfer_list.setObjectName("transferList")
        self._transfer_list.setVisible(False)
        layout.addWidget(self._transfer_list, 1)

        # Empty state — mostrato al posto della lista quando non c'e' nulla
        self._transfers_empty = QLabel("No active transfers")
        self._transfers_empty.setObjectName("transferEmpty")
        self._transfers_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._transfers_empty.setMinimumHeight(80)
        self._transfers_empty.setMaximumHeight(260)
        layout.addWidget(self._transfers_empty, 1)

        return section

    # ── signal connections ──────────────────────────────────────────

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        # Local tree: double-click to enter directory / select files
        self._local_tree.doubleClicked.connect(self._on_local_double_clicked)
        local_sel = self._local_tree.selectionModel()
        if local_sel is not None:
            local_sel.selectionChanged.connect(self._on_local_selection_changed)

        # Remote tree: double-click to enter directory / select files
        self._remote_tree.doubleClicked.connect(self._on_remote_double_clicked)
        remote_sel = self._remote_tree.selectionModel()
        if remote_sel is not None:
            remote_sel.selectionChanged.connect(self._on_remote_selection_changed)

        # Path bars
        self._local_path_bar.returnPressed.connect(self._on_local_path_entered)
        self._remote_path_bar.returnPressed.connect(self._on_remote_path_entered)

        # Home buttons
        self._local_home_btn.clicked.connect(self._go_local_home)
        self._remote_home_btn.clicked.connect(self._go_remote_home)
        self._local_up_btn.clicked.connect(self._go_local_up)
        self._remote_up_btn.clicked.connect(self._go_remote_up)

        # Action buttons
        self._upload_btn.clicked.connect(self._on_upload_clicked)
        self._download_btn.clicked.connect(self._on_download_clicked)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._clear_btn.clicked.connect(self._clear_completed)
        self._clear_sel_btn.clicked.connect(self._clear_selection)

        # Transfer list state (header count, empty state, "Clear" abilitato)
        self._transfer_model.countChanged.connect(self._update_transfers_ui)
        self._transfer_model.dataChanged.connect(self._update_transfer_summary)
        self._update_transfers_ui()

    # ── local filesystem handlers ───────────────────────────────────

    @Slot(QModelIndex)
    def _on_local_double_clicked(self, index: QModelIndex) -> None:
        """Double-click on local tree: enter directory or select file."""
        if self._local_model.isDir(index):
            self._local_tree.setRootIndex(index)
            path = self._local_model.filePath(index)
            self._local_path_bar.setText(path)
            self._local_root = path
            self.navigate_local.emit(path)

    @Slot()
    def _on_local_path_entered(self) -> None:
        """Navigate local path bar."""
        path = self._local_path_bar.text().strip()
        p = Path(path)
        if p.exists() and p.is_dir():
            index = self._local_model.index(str(p))
            self._local_tree.setRootIndex(index)
            self._local_root = str(p)

    @Slot()
    def _go_local_home(self) -> None:
        """Navigate local tree to home directory."""
        self._navigate_local_to(Path.home())

    @Slot()
    def _go_local_up(self) -> None:
        """Navigate the local pane one level up."""
        self._navigate_local_to(Path(self._local_root).parent)

    def _navigate_local_to(self, path: str | Path) -> None:
        """Point the local pane at ``path`` (se e' una directory esistente)."""
        target = Path(path)
        if not target.is_dir():
            return
        p = str(target)
        self._local_path_bar.setText(p)
        self._local_tree.setRootIndex(self._local_model.index(p))
        self._local_root = p
        self.navigate_local.emit(p)

    @Slot()
    def _on_local_selection_changed(self) -> None:
        """Update UI when local selection changes."""
        self._update_action_buttons()

    # ── remote filesystem handlers ──────────────────────────────────

    @Slot(QModelIndex)
    def _on_remote_double_clicked(self, index: QModelIndex) -> None:
        """Double-click on remote tree."""
        node = self._remote_model.node_at(index)
        if node and node.get("is_dir"):
            path = node.get("path", "")
            self._navigate_remote_to(path)
        # If it's a file, just select it (selection tracks it)

    def _navigate_remote_to(self, path: str) -> None:
        """Request remote directory listing and update path bar."""
        self._remote_path = path
        self._remote_path_bar.setText(path)
        self.remote_listing_requested.emit(path)

    @Slot()
    def _on_remote_path_entered(self) -> None:
        """Navigate remote path bar (vuoto = radice del peer, path nativi accettati)."""
        self._navigate_remote_to(self._remote_path_bar.text().strip())

    @Slot()
    def _go_remote_home(self) -> None:
        """Navigate remote to its root ("" = radice del peer: C:\\ o /)."""
        self._navigate_remote_to("")

    @Slot()
    def _go_remote_up(self) -> None:
        """Navigate the remote pane one level up (path POSIX o Windows)."""
        current = (self._remote_path or "").rstrip("/\\")
        # gia' alla radice: "" (POSIX) oppure "C:" (Windows)
        if not current or (len(current) <= 2 and current.endswith(":")):
            return
        sep = "/" if "/" in current else "\\"
        self._navigate_remote_to(current.rsplit(sep, 1)[0] or "/")

    @Slot()
    def _on_remote_selection_changed(self) -> None:
        """Update UI when remote selection changes."""
        self._update_action_buttons()

    # ── action buttons ──────────────────────────────────────────────

    @Slot()
    def _on_upload_clicked(self) -> None:
        """Upload selected local files to the current remote folder."""
        indexes = self._local_tree.selectionModel().selectedIndexes()
        paths = set()
        for idx in indexes:
            if idx.column() == 0:  # Only first column
                path = self._local_model.filePath(idx)
                if not self._local_model.isDir(idx):
                    paths.add(path)

        if paths:
            remote_dest = self._remote_path or "/"
            self.file_upload_requested.emit(list(paths), remote_dest)
            self._status_label.setText(f"Uploading {len(paths)} file(s) to {remote_dest}...")

    @Slot()
    def _on_download_clicked(self) -> None:
        """Download selected remote files to the current local folder."""
        indexes = self._remote_tree.selectionModel().selectedIndexes()
        paths = set()
        for idx in indexes:
            if idx.column() == 0:
                node = self._remote_model.node_at(idx)
                if node and not node.get("is_dir"):
                    paths.add(node.get("path", ""))

        if paths:
            local_dest = self._local_root or str(Path.home())
            self.file_download_requested.emit(list(paths), local_dest)
            self._status_label.setText(f"Downloading {len(paths)} file(s) to {local_dest}...")

    def _on_refresh_clicked(self) -> None:
        """Refresh the current remote directory."""
        self._status_label.setText("Refreshing remote directory...")
        self._navigate_remote_to(self._remote_path)

    def _clear_completed(self) -> None:
        """Clear completed transfers."""
        cleared = self._transfer_model.clear_completed()
        if cleared:
            self._status_label.setText("Cleared completed transfers")
        else:
            self._status_label.setText("No completed transfers to clear")
        # Re-evaluate button states after clearing
        self._update_action_buttons()

    def _clear_selection(self) -> None:
        """Clear all selections in both panes."""
        local_sel = self._local_tree.selectionModel()
        remote_sel = self._remote_tree.selectionModel()
        if local_sel is not None:
            local_sel.clearSelection()
        if remote_sel is not None:
            remote_sel.clearSelection()
        self._selection_info.setText("")
        self._clear_sel_btn.setVisible(False)

    # ── public API ──────────────────────────────────────────────────

    @property
    def transfer_model(self) -> TransferListModel:
        return self._transfer_model

    @property
    def remote_model(self) -> RemoteFileSystemModel:
        return self._remote_model

    def add_transfer(self, job: TransferJob) -> None:
        """Add or update a transfer in the list."""
        self._transfer_model.upsert(job)

    def remove_transfer(self, job_id: str) -> None:
        """Remove a transfer by ID."""
        self._transfer_model.remove(job_id)

    def set_remote_listing(self, path: str, entries: list[dict], error: str = "") -> None:
        """Update remote file listing."""
        if error:
            self._status_label.setText(f"⚠ Remote listing error: {error}")
            return
        self._remote_path = path
        self._remote_path_bar.setText(path)
        self._remote_model.set_directory_contents(path, entries)
        self._status_label.setText(f"Remote: {path}  \u00b7  {len(entries)} items")

    def set_status(self, message: str) -> None:
        """Set the status bar message."""
        self._status_label.setText(message)

    def set_connected(self, connected: bool) -> None:
        """Enable/disable remote-related controls based on connection."""
        self._refresh_btn.setEnabled(connected)
        self._download_btn.setEnabled(connected)
        self._remote_path_bar.setEnabled(connected)
        self._remote_home_btn.setEnabled(connected)
        if not connected:
            self._remote_model.clear()
            self._remote_path = ""
            self._remote_path_bar.setText("")
            self._status_label.setText("Disconnected — remote browsing unavailable")
        else:
            self._navigate_remote_to("")

    # ── internal ────────────────────────────────────────────────────

    def _update_transfers_ui(self) -> None:
        """Aggiorna header, empty state e pulsante "Clear" della lista transfer."""
        count = self._transfer_model.rowCount()
        active = self._transfer_model.active_count
        header = "Active transfers"
        if active:
            header += f"  ·  {active} running"
        elif count:
            header += f"  ·  {count}"
        self._transfers_header.setText(header)

        self._transfer_list.setVisible(count > 0)
        self._transfers_empty.setVisible(count == 0)
        self._clear_btn.setEnabled(self._transfer_model.completed_count > 0)
        self._update_transfer_summary()

    def _update_transfer_summary(self) -> None:
        """Riepilogo compatto nella status bar: attivi e avanzamento complessivo."""
        jobs = [
            job
            for i in range(self._transfer_model.rowCount())
            if (job := self._transfer_model.job_at(i)) is not None
        ]
        running = [j for j in jobs if j.state == TransferState.IN_PROGRESS]
        if not running:
            self._transfer_summary.setText("")
            return
        total = sum(j.file_info.size for j in running) or 1
        done = sum(j.bytes_transferred for j in running)
        self._transfer_summary.setText(f"{len(running)} active  ·  {int(100 * done / total)}%")

    def _update_action_buttons(self) -> None:
        """Enable/disable upload/download buttons based on selections."""
        # Guard: selectionModel might be None during initialization
        local_sel = self._local_tree.selectionModel()
        remote_sel = self._remote_tree.selectionModel()

        # Collect selected file names from local tree
        local_names: list[str] = []
        local_indexes = local_sel.selectedIndexes() if local_sel else []
        has_local_files = False
        seen_local = set()
        for idx in local_indexes:
            if idx.column() == 0 and not self._local_model.isDir(idx):
                path = self._local_model.filePath(idx)
                if path not in seen_local:
                    seen_local.add(path)
                    local_names.append(self._local_model.fileName(idx))
                    has_local_files = True
        self._upload_btn.setEnabled(has_local_files)

        # Collect selected file names from remote tree
        remote_names: list[str] = []
        remote_indexes = remote_sel.selectedIndexes() if remote_sel else []
        has_remote_files = False
        seen_remote = set()
        for idx in remote_indexes:
            if idx.column() == 0:
                node = self._remote_model.node_at(idx)
                if node and not node.get("is_dir"):
                    name = node.get("name", "")
                    if name not in seen_remote:
                        seen_remote.add(name)
                        remote_names.append(name)
                        has_remote_files = True
        self._download_btn.setEnabled(has_remote_files and self._refresh_btn.isEnabled())

        # ── Update selection info with file names ──
        parts = []
        if local_names:
            label = f"↑ {len(local_names)}: {', '.join(local_names[:3])}"
            if len(local_names) > 3:
                label += f" …(+{len(local_names) - 3})"
            parts.append(label)
        if remote_names:
            label = f"↓ {len(remote_names)}: {', '.join(remote_names[:3])}"
            if len(remote_names) > 3:
                label += f" …(+{len(remote_names) - 3})"
            parts.append(label)

        has_any_selection = bool(local_names or remote_names)
        self._selection_info.setText(" | ".join(parts) if parts else "")
        self._clear_sel_btn.setVisible(has_any_selection)
