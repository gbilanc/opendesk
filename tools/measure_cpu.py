#!/usr/bin/env python3
"""Misurazione CPU di opendesk-host su Windows (piano CPU step 1).

Usage:
    python tools/measure_cpu.py --duration 300 --interval 2 --out cpu_report.csv
    python tools/measure_cpu.py --pid 12345 --duration 120

Registra ogni *interval* secondi il CPU% totale del processo opendesk-host
e il CPU% dei thread principali (MainThread, CaptureWorker, EncoderWorker,
NetworkWorker) in CSV.  Con --threads nome_thread filtra i thread tracciati.

Procedura di test raccomandata (macchina host reale):
  1. Avvia opendesk-host e misura 5 min senza connessioni   (idle)
  2. Connetti un client, stream attivo a schermo in movimento (attività)
  3. Riesegui dopo il deploy delle patch e confronta i delta.
Criterio di successo (parità con AnyDesk su CPU senza HW encoder):
  idle < 2% CPU, attività < 10-15%.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time

try:
    import psutil  # pip install psutil
except ImportError:
    sys.exit("psutil richiesto: pip install psutil")

_TRACKED = ("MainThread", "CaptureWorker", "EncoderWorker", "NetworkWorker")


def find_opendesk_host_process() -> psutil.Process | None:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if "opendesk" in name or "opendesk" in cmdline:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def thread_cpu_pct(proc: psutil.Process) -> dict[str, float]:
    """CPU% per thread (somma fra campionamenti; un core = 100%)."""
    times = proc.thread_time()  # (user, system) — richiede psutil>=5
    out: dict[str, float] = {}
    for th in proc.threads():
        name = getattr(th, "name", "") or f"tid-{th.id}"
        for tracked in _TRACKED:
            if tracked in name or name.startswith(tracked):
                out.setdefault(tracked, 0.0)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="PID del processo da monitorare")
    parser.add_argument("--duration", type=int, default=300, help="Durata misura in s")
    parser.add_argument("--interval", type=float, default=2.0, help="Campionamento in s")
    parser.add_argument("--out", default="cpu_report.csv")
    args = parser.parse_args()

    proc = psutil.Process(args.pid) if args.pid else find_opendesk_host_process()
    if proc is None:
        sys.exit("Processo opendesk-host non trovato (usa --pid)")

    print(f"Monitoraggio PID {proc.pid}: {proc.name()}")
    proc.cpu_percent(interval=None)  # primo campione di riferimento

    # thread_time del processo: (user, system) totali — per il totale usiamo
    # cpu_percent (incl. iowait escluso), per i thread lo snapshot dei tempi.
    last_thread_times: dict[int, float] = {}
    fieldnames = ["t", "cpu_pct", "threads"]
    fieldnames += [f"thr_{n.lower()}" for n in _TRACKED]

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        t0 = time.time()
        while time.time() - t0 < args.duration:
            cpu = proc.cpu_percent(interval=None)
            # thread_time() del processo restituisce la SOMMA dei tempi;
            # per il per-thread usiamo ThreadTime su psutil>=5.6 via
            # proc.threads() che espone .user_time/.system_time.
            row = [round(time.time() - t0, 1), round(cpu, 1), proc.num_threads()]
            row += [0.0] * len(_TRACKED)
            snap: dict[int, float] = {}
            for th in proc.threads():
                name = getattr(th, "name", "") or ""
                snap[th.id] = th.user_time + th.system_time
                for tracked in _TRACKED:
                    if tracked in name:
                        prev = last_thread_times.get(th.id, th.user_time + th.system_time)
                        delta = (th.user_time + th.system_time) - prev
                        idx = fieldnames.index(f"thr_{tracked.lower()}")
                        row[idx] = round(row[idx] + delta * 100 / (args.interval * 1000), 1)
            last_thread_times.update(snap)
            writer.writerow(row)
            f.flush()
            time.sleep(args.interval)
    print(f"Report scritto in {args.out}")


if __name__ == "__main__":
    main()
