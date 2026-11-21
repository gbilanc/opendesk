"""Watchdog diagnostico per blocchi del main thread Qt.

Scopo: distinguere un *deadlock reale* del main thread da un semplice
comportamento percettivo ("sembra bloccato").  Un QTimer sul main thread
emette un beat ogni ``CHECK_INTERVAL_MS``; un thread demone verifica che
il beat sia fresco.  Se il main thread non risponde da ``_STALL_THRESHOLD``
secondi, scrive nel log lo stack di tutti i thread (una sola volta per
episodio di stallo, così il log non viene inondato).

Attivo di default nell'host: scrive in
``AppData/Roaming/OpenDesk/logs/opendesk.log`` con logger
``opendesk.hang_watchdog``.

Usage::

    from opendesk.utils.hang_watchdog import HangWatchdog
    watchdog = HangWatchdog()  # dopo QApplication
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import QTimer

logger = logging.getLogger("opendesk.hang_watchdog")

CHECK_INTERVAL_MS = 500  # beat del main thread
_WATCH_INTERVAL = 0.5  # s — periodo di controllo del thread watchdog
_STALL_THRESHOLD = 3.0  # s — main thread bloccato da almeno così → log stack


def _log_thread_stacks(stall: float) -> None:
    """Dump nel log dello stack di tutti i thread (diagnostica hang)."""
    try:
        main_tid = threading.main_thread().ident
        frames = sys._current_frames()
        lines = [f"⚠ UI thread NON risponde da {stall:.1f}s — stack dei thread:"]
        for tid, frame in frames.items():
            name = next(
                (t.name for t in threading.enumerate() if t.ident == tid),
                f"tid-{tid}",
            )
            marker = " ← MAIN" if tid == main_tid else ""
            lines.append(f"--- {name} ({tid}){marker} ---")
            # NB: senza slice — l'ULTIMO frame è quello esattamente dove il
            # thread è bloccato: è l'informazione più preziosa.
            lines.extend(line.rstrip() for line in traceback.format_stack(frame))
        logger.warning("\n".join(lines))
    except Exception:  # noqa: BLE001 — il watchdog non deve mai far crashare
        logger.exception("hang_watchdog: dump stack fallito")


def _heartbeat_path() -> str | None:
    """Path del file di heartbeat: accanto al file di log, se esiste."""
    for handler in logging.getLogger().handlers:
        filename = getattr(handler, "baseFilename", None)
        if filename:
            return os.path.join(os.path.dirname(filename), "watchdog.beat")
    return None


def _find_pyspy() -> str | None:
    """Trova l'eseguibile py-spy: PATH, oppure accanto all'interprete."""
    exe = shutil.which("py-spy")
    if exe is None:
        candidate = os.path.join(os.path.dirname(sys.executable), "py-spy.exe")
        if os.path.isfile(candidate):
            exe = candidate
    return exe


def _log_native_stack() -> None:
    """Dump stack nativo+Python via ``py-spy --native``.

    ``traceback.format_stack`` mostra solo frame Python: un hang dentro
    l'event loop Qt appare come ``app.exec()`` e il frame C++ reale
    (dispatcher Win32, clipboard, SendMessage, ...) resta invisibile.
    py-spy --native lo rivela.  Chiamato dal thread watchdog, una sola
    volta per episodio di stallo (il dump nativo costa qualche secondo).
    """
    exe = _find_pyspy()
    if exe is None:
        logger.warning(
            "hang_watchdog: py-spy non trovato — dump nativo non disponibile "
            "(installa py-spy nel venv o in PATH per il stack nativo)"
        )
        return
    try:
        proc = subprocess.run(
            [exe, "dump", "--pid", str(os.getpid()), "--native"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        if out:
            logger.warning("hang_watchdog: dump nativo (py-spy --native):\n%s", out[:8000])
        else:
            logger.warning("hang_watchdog: py-spy --native non ha prodotto output")
    except Exception:  # noqa: BLE001 — il watchdog non deve mai far crashare
        logger.exception("hang_watchdog: py-spy dump fallito")


class HangWatchdog:
    """Rileva stalli del main thread e li logga con gli stack completi."""

    def __init__(
        self,
        check_interval_ms: int = CHECK_INTERVAL_MS,
        stall_threshold: float = _STALL_THRESHOLD,
    ) -> None:
        self._stall_threshold = stall_threshold
        self._last_beat = time.monotonic()
        self._reported = False
        self._last_report = 0.0
        self._native_dumped = False
        self._lock = threading.Lock()

        self._timer = QTimer()
        self._timer.timeout.connect(self._beat)
        self._timer.start(check_interval_ms)

        self._thread = threading.Thread(target=self._run, name="HangWatchdog", daemon=True)
        self._thread.start()
        logger.info(
            "HangWatchdog attivo (beat=%dms, threshold=%.1fs)",
            check_interval_ms,
            stall_threshold,
        )

    def _beat(self) -> None:
        """Chiamato dal main thread: il beat è fresco."""
        with self._lock:
            self._last_beat = time.monotonic()
            self._reported = False
            self._native_dumped = False

    def _run(self) -> None:
        # Heartbeat su file: se il processo si congela in codice nativo col
        # GIL tenuto (es. font fallback DirectWrite, RCA 2026-11-21), questo
        # thread non può né loggare né scrivere — ma il file resta fermo al
        # momento del freeze, ed è l'unica evidenza post-mortem disponibile.
        beat_path = _heartbeat_path()
        last_hb = 0.0
        while True:
            time.sleep(_WATCH_INTERVAL)
            now = time.monotonic()
            if beat_path is not None and now - last_hb >= 1.0:
                last_hb = now
                try:
                    with open(beat_path, "w") as f:
                        f.write(f"{time.time():.3f}\n")
                except OSError:
                    pass
            with self._lock:
                stall = time.monotonic() - self._last_beat
                # Re-report ogni ~5s se lo stallo persiste (prima: una sola
                # volta per episodio — uno stallo prolungato spariva dal log).
                report = stall >= self._stall_threshold and (
                    not self._reported or stall - self._last_report >= 5.0
                )
                if report:
                    self._reported = True
                    self._last_report = stall
            if report:
                _log_thread_stacks(stall)
                if not self._native_dumped:
                    self._native_dumped = True
                    _log_native_stack()
