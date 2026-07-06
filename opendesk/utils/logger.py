"""
Centralised logging configuration.

Provides a consistent log format and level across the application.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: int = logging.DEBUG) -> None:
    """Configure the root logger with a standard format.

    Parameters
    ----------
    level : int
        Logging level, e.g. ``logging.INFO``, ``logging.DEBUG``.
    """
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on repeated calls
    if not root.handlers:
        root.addHandler(handler)
    else:
        root.handlers[0] = handler
