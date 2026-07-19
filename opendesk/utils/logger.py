"""
Centralised logging configuration.

Provides a consistent log format and level across the application.
"""

from __future__ import annotations

import logging
import os
import sys


_LOG_LEVEL_NAMES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def parse_log_level(level_name: str) -> int:
    """Convert a case-insensitive log level name to its ``logging`` constant.

    Parameters
    ----------
    level_name : str
        One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.

    Returns
    -------
    int
        The corresponding ``logging`` level constant.

    Raises
    ------
    ValueError
        If *level_name* is not a recognised level name.
    """
    upper = level_name.upper()
    if upper not in _LOG_LEVEL_NAMES:
        valid = ", ".join(_LOG_LEVEL_NAMES)
        raise ValueError(f"Unknown log level '{level_name}'. Valid: {valid}")
    return _LOG_LEVEL_NAMES[upper]


def setup_logging(level: int | None = None) -> None:
    """Configure the root logger with a standard format.

    The effective level is determined by (in order of precedence):

    1. *level* parameter (if not ``None``)
    2. ``OPENDESK_LOG_LEVEL`` environment variable (if set)
    3. ``logging.DEBUG`` (default, suitable for development)

    Parameters
    ----------
    level : int | None
        Logging level, e.g. ``logging.INFO``, ``logging.WARNING``.
        Overrides the environment variable when not ``None``.
    """
    if level is None:
        env = os.environ.get("OPENDESK_LOG_LEVEL")
        if env is not None:
            level = parse_log_level(env)
        else:
            level = logging.DEBUG
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
