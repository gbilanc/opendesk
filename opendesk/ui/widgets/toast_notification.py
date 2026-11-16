"""Notifica toast sovrapposta con auto-dismiss e animazione."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)


class ToastNotification(QFrame):
    """Notifica toast sovrapposta all'angolo in alto a destra del parent.

    Usage::

        toast = ToastNotification(self, "Operazione completata!", ToastNotification.Type.SUCCESS)
        toast.show()
    """

    class Type(Enum):
        SUCCESS = "✅"
        ERROR = "❌"
        WARNING = "⚠️"
        INFO = "ℹ️"

        def __init__(self, icon: str) -> None:
            self.icon = icon

    def __init__(
        self,
        parent: QWidget,
        message: str,
        toast_type: Type = Type.INFO,
        duration_ms: int = 4000,
    ) -> None:
        super().__init__(parent)
        self._duration_ms = duration_ms

        self.setFixedHeight(44)
        self.setMinimumWidth(260)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        # Stile da QSS (opendesk.qss / dark.qss) via objectName + property,
        # così il toast segue il tema attivo.
        self.setObjectName("ToastNotification")
        self.setProperty("toastType", toast_type.name.lower())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        icon_lbl = QLabel(toast_type.icon)
        icon_lbl.setObjectName("ToastIcon")
        layout.addWidget(icon_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setObjectName("ToastMessage")
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl, 1)

        # Store parent reference for positioning
        self._target_parent = parent
        self._position()

        # ── Fade in/out animations ──
        # ``windowOpacity`` funziona solo sulle finestre top-level: per un
        # widget figlio serve un QGraphicsOpacityEffect.
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)

        self._anim_in = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._anim_in.setDuration(250)
        self._anim_in.setStartValue(0.0)
        self._anim_in.setEndValue(1.0)
        self._anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)

        self._anim_out = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._anim_out.setDuration(250)
        self._anim_out.setStartValue(1.0)
        self._anim_out.setEndValue(0.0)
        self._anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim_out.finished.connect(self.deleteLater)

    # ── public API ──────────────────────────────────────────────────

    def show(self) -> None:
        """Mostra la notifica con animazione fade-in."""
        super().show()
        self.raise_()
        self._anim_in.start()
        self._timer.start(self._duration_ms)

    # ── internal ────────────────────────────────────────────────────

    def _position(self) -> None:
        """Posiziona in alto a destra del parent."""
        parent_rect = self._target_parent.rect()
        self.adjustSize()
        x = parent_rect.width() - self.width() - 16
        y = 16
        self.move(x, y)

    def _fade_out(self) -> None:
        self._anim_out.start()

    def _reposition(self) -> None:
        """Riposiziona quando il parent viene ridimensionato (chiamare da resizeEvent)."""
        self._position()
