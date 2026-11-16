"""Feedback "copiato" condiviso dalle due finestre (client e host).

Era duplicato in ``host_app.HostWindow`` e ``ui/session_info.SessionInfoWidget``.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

_FLASH_MS = 1500


def flash_button(btn: QPushButton, text: str) -> None:
    """Sostituisce temporaneamente l'etichetta del bottone (feedback di copia)."""
    original = btn.text()
    btn.setText(text)
    btn.setEnabled(False)
    QTimer.singleShot(_FLASH_MS, lambda: _restore(btn, original))


def _restore(btn: QPushButton, text: str) -> None:
    btn.setText(text)
    btn.setEnabled(True)
