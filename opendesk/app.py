"""
Application entry point.

Initialises logging, creates the QApplication, loads the stylesheet,
and launches the main window.  Supports light/dark theme switching.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from opendesk.utils.logger import setup_logging
from opendesk.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

# Paths to QSS theme files
_QSS_LIGHT = Path(__file__).parent / "ui" / "resources" / "opendesk.qss"
_QSS_DARK = Path(__file__).parent / "ui" / "resources" / "dark.qss"

# Global reference to keep theme state
_current_theme: str = "light"


def load_stylesheet(app: QApplication, theme: str = "light") -> None:
    """Load a QSS theme file.

    Parameters
    ----------
    app : QApplication
        The application instance.
    theme : str
        ``"light"`` or ``"dark"``.
    """
    global _current_theme
    qss_path = _QSS_DARK if theme == "dark" else _QSS_LIGHT
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text())
        _current_theme = theme
        logger.debug("Theme '%s' loaded from %s", theme, qss_path)


def toggle_theme(app: QApplication) -> str:
    """Switch between light and dark theme.

    Returns the new theme name.
    """
    new_theme = "dark" if _current_theme == "light" else "light"
    load_stylesheet(app, new_theme)
    return new_theme


def get_current_theme() -> str:
    """Return the current theme name."""
    return _current_theme


def main() -> None:
    """Start the OpenDesk application."""
    setup_logging()
    logger.info("Starting OpenDesk v%s", __import__("opendesk").__version__)

    app = QApplication(sys.argv)
    app.setApplicationName("OpenDesk")
    app.setOrganizationName("OpenDesk")
    app.setApplicationVersion(__import__("opendesk").__version__)

    app.setStyle("Fusion")
    load_stylesheet(app, "light")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
