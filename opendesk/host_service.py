"""Servizio Windows per OpenDesk Host (SYSTEM).

Architettura (modello AnyDesk-lite, RCA 2026-11-14):

- Il SERVIZIO gira come LocalSystem e fa solo da **supervisore**: quando
  esiste una sessione interattiva attiva e nessun processo host è in
  esecuzione, riavvia l'host elevato tramite l'attività pianificata
  ``OpenDesk Host`` creata dall'installer (``/RL HIGHEST`` → processo
  elevato nella sessione utente, senza prompt UAC).
- La parte vera (relay, streaming, input injection) gira nel processo
  utente nella sessione interattiva: la cattura (GDI/mss) e SendInput
  non sono raggiungibili dalla sessione 0 del servizio.
- Perché elevato: le finestre elevate (Task Manager, UAC consent prompt)
  accettano input solo da processi a integrity level >= high (UIPI).
- Fuori scope (limiti di sistema): controllo del secure desktop del
  prompt UAC e funzionamento pre-logon richiederebbero che cattura e
  injection girino in sessione 0 (un vero servizio all'AnyDesk).

Uso:
    opendesk-host.exe --winsvc            # entrypoint del servizio (SCM)
    opendesk-host.exe --install-service   # sc create (richiede admin)
    opendesk-host.exe --remove-service    # sc stop + sc delete
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("opendesk.host_service")

SERVICE_NAME = "OpenDeskHostSvc"
_TASK_NAME = "OpenDesk Host"
_HOST_EXE = "opendesk-host.exe"
_SUPERVISOR_INTERVAL_S = 5.0
_LOG_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "OpenDesk" / "logs"

_INVALID_SESSION = 0xFFFFFFFF


# ── process/session introspection (kernel32) ───────────────────────

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _active_console_session() -> int:
    """ID della sessione interattiva attiva, 0 se nessuna (pre-logon)."""
    sid = _kernel32.WTSGetActiveConsoleSessionId()
    return sid if sid != _INVALID_SESSION else 0


def _host_process_running(session: int) -> bool:
    """True se un host gira nella sessione interattiva *session*.

    NB: il processo SERVIZIO è lui stesso ``opendesk-host.exe`` ma in
    sessione 0 — va escluso, altrimenti il supervisore non avvierà mai
    l'host nella sessione utente (bug trovato in test: RCA 2026-11-14).
    """
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == -1 or snapshot is None:
        # conservativo: meglio non spawnare a vuoto se non vediamo i processi
        logger.warning("Toolhelp32Snapshot fallito (WinError %s)", ctypes.get_last_error())
        return True
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        proc_session = wintypes.DWORD()
        ok = _kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("utf-8", errors="ignore").lower()
            if name == _HOST_EXE and entry.th32ProcessID != os.getpid():
                if _kernel32.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(proc_session)):
                    if proc_session.value == session:
                        return True
            ok = _kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return False


def _spawn_host_via_task() -> None:
    """Avvia l'host elevato nella sessione utente via attività pianificata.

    L'installer crea il task ``OpenDesk Host`` con ``/RL HIGHEST``:
    ``schtasks /Run`` lo esegue elevato nella sessione interattiva
    dell'utente, senza prompt UAC.
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", _TASK_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001 — il servizio non deve morire
        logger.error("schtasks /Run fallita: %s", e)
        return
    if proc.returncode != 0:
        logger.warning(
            "schtasks /Run '%s' fallita (rc=%s): %s",
            _TASK_NAME,
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip(),
        )
    else:
        logger.info("Host avviato via attività pianificata")


def _supervisor_loop(stop: threading.Event) -> None:
    logger.info("Supervisore attivo (intervallo %.0fs)", _SUPERVISOR_INTERVAL_S)
    was_running: bool | None = None
    while not stop.wait(_SUPERVISOR_INTERVAL_S):
        session = _active_console_session()
        if session == 0:
            if was_running is not False:
                logger.info("Nessuna sessione interattiva attiva")
            was_running = False
            continue
        running = _host_process_running(session)
        if running != was_running:
            logger.info(
                "Sessione %d: opendesk-host.exe %s",
                session,
                "in esecuzione" if running else "NON in esecuzione",
            )
            was_running = running
        if not running:
            logger.info("Sessione %d: riavvio host via task pianificata", session)
            _spawn_host_via_task()


# ── SCM (Service Control Manager) via ctypes ───────────────────────

_SERVICE_STOP = 0x00000001
_SERVICE_SHUTDOWN = 0x00000002
_SERVICE_STOPPED = 0x00000001
_SERVICE_RUNNING = 0x00000004
_SERVICE_ACCEPT_STOP = 0x00000001
_SERVICE_ACCEPT_SHUTDOWN = 0x00000004
_SERVICE_WIN32_OWN_PROCESS = 0x00000010
_NO_ERROR = 0


class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


class _SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("lpServiceName", wintypes.LPSTR),
        ("lpServiceProc", ctypes.c_void_p),
    ]


_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


def run_service() -> None:
    """Entry point del servizio: chiamato dall'exe con ``--winsvc``.

    Si registra su SCM, segnala RUNNING e esegue il supervisore fino a
    STOP/SHUTDOWN.  Il log va su ``ProgramData\\OpenDesk\\logs`` (il
    servizio gira col profilo SYSTEM, non con quello dell'utente).
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(_LOG_DIR / "service.log"),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Servizio avviato (exe=%s)", sys.executable)

    stop = threading.Event()
    status = _SERVICE_STATUS()
    svc_handle = ctypes.c_void_p()

    HandlerEx = ctypes.WINFUNCTYPE(
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
    )

    def _handler(ctrl: int, _etype: int, _edata: int, _ctx: int) -> int:
        if ctrl in (_SERVICE_STOP, _SERVICE_SHUTDOWN):
            logger.info("Stop richiesto (control=%d)", ctrl)
            stop.set()
        return _NO_ERROR

    handler_ref = HandlerEx(_handler)
    # Handle a 64 bit: senza restype esplicito ctypes tronca a int32 e
    # SetServiceStatus riceve un handle invalido (stato START_PENDING eterno).
    _advapi32.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    _advapi32.RegisterServiceCtrlHandlerExW.argtypes = [
        wintypes.LPCWSTR,
        HandlerEx,
        ctypes.c_void_p,
    ]
    _advapi32.SetServiceStatus.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SERVICE_STATUS),
    ]

    def _set_state(handle: int | None, state: int, accepted: int = 0) -> None:
        if handle is None:
            return
        status.dwServiceType = _SERVICE_WIN32_OWN_PROCESS
        status.dwCurrentState = state
        status.dwControlsAccepted = accepted
        status.dwWin32ExitCode = _NO_ERROR
        status.dwServiceSpecificExitCode = 0
        status.dwCheckPoint = 0
        status.dwWaitHint = 0
        _advapi32.SetServiceStatus(handle, ctypes.byref(status))

    ServiceMain = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.LPSTR)

    def service_main(_argc: int, _argv) -> int:
        svc_handle.value = _advapi32.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler_ref, None)
        if not svc_handle.value:
            logger.error(
                "RegisterServiceCtrlHandlerExW fallita (WinError %s)", ctypes.get_last_error()
            )
            return 1
        _set_state(
            svc_handle.value, _SERVICE_RUNNING, _SERVICE_ACCEPT_STOP | _SERVICE_ACCEPT_SHUTDOWN
        )
        logger.info("Stato: RUNNING — supervisore in avvio")
        _supervisor_loop(stop)
        _set_state(svc_handle.value, _SERVICE_STOPPED)
        logger.info("Stato: STOPPED")
        return 0

    service_main_ref = ServiceMain(service_main)  # tienilo vivo!

    table = (_SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME.encode("ascii")
    table[0].lpServiceProc = ctypes.cast(service_main_ref, ctypes.c_void_p)
    ret = _advapi32.StartServiceCtrlDispatcherA(table)
    if not ret:
        err = ctypes.get_last_error()
        # ERROR_FAILED_SERVICE_CONTROLLER_CONNECT (1063): avvio da console
        if err == 1063:
            print(
                "Questo processo è un servizio: installarlo con --install-service "
                "oppure avviare l'host normalmente (senza --winsvc)."
            )
        else:
            print(f"StartServiceCtrlDispatcher fallita (WinError {err})")
        sys.exit(1)


# ── install / remove (sc.exe) ──────────────────────────────────────


def install_service() -> None:
    """Registra il servizio (richiede admin).  Solo per il bundle installato."""
    exe = Path(sys.executable)
    if not getattr(sys, "frozen", False):
        print(
            "Il servizio richiede il pacchetto installato (opendesk-host.exe).\n"
            "In sviluppo usa l'installer oppure crea manualmente:\n"
            f'  sc create {SERVICE_NAME} binPath= "{exe} --winsvc" start= auto'
        )
        sys.exit(1)
    bin_path = f"{exe} --winsvc"
    proc = subprocess.run(
        [
            "sc",
            "create",
            SERVICE_NAME,
            "binPath=",
            bin_path,
            "start=",
            "auto",
            "DisplayName=",
            "OpenDesk Host Service",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or "FAILED" in (proc.stdout + proc.stderr).upper():
        print(f"sc create fallita:\n{proc.stdout}{proc.stderr}")
        sys.exit(1)
    subprocess.run(
        [
            "sc",
            "description",
            SERVICE_NAME,
            "Supervisore OpenDesk Host: mantiene l'host elevato nella sessione utente.",
        ],
        capture_output=True,
        text=True,
    )
    print(f"Servizio {SERVICE_NAME} installato ({bin_path})")
    print("Avvio: sc start " + SERVICE_NAME)


def remove_service() -> None:
    """Ferma e rimuove il servizio (richiede admin)."""
    subprocess.run(["sc", "stop", SERVICE_NAME], capture_output=True, text=True)
    proc = subprocess.run(["sc", "delete", SERVICE_NAME], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    print(out or f"Servizio {SERVICE_NAME} rimosso")
