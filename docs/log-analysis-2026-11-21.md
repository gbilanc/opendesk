# Analisi log host — 2026-11-21

Fonte: `opendesk.log` (163 righe, host Windows, capture MSS, H264 software).
Due episodi indipendenti, entrambi con causa individuata nel codice.

## Episodio 1 — loop di riconnessione al relay (02:33:50 → 02:37:15)

- P2P chiuso → fallback relay → 10 tentativi falliti consecutivi, ognuno
  `Connection to relay relay.gibisoft.net:8474 timed out (15.0s)`.
- Cadenza fissa (timeout 15 s + retry ogni ~20 s), nessun backoff, nessuna
  informazione di tentativo nei log.
- Il 10° tentativo riesce in <1 s: outage transitorio, non misconfigurazione.
- Ogni fallimento emetteva 4 righe INFO di teardown identiche anche senza
  nulla di attivo (40 righe di rumore su 163).

Verifica esterna (2026-11-21, dopo l'episodio):

```
$ getent hosts relay.gibisoft.net
81.2.135.97  t60n058jfzzk1f5a.myfritz.net relay.gibisoft.net casa.gibisoft.net
$ tcp/8474  → OK
```

Il relay è raggiungibile: l'outage era lato rete/servizio. Nota: lo stesso
IP compare come candidate srflx del peer in ICE (`81.2.135.97`) — host, peer
e relay escono dallo stesso NAT Fritz!Box, quindi il P2P "pubblico" resta
sulla LAN.

Fix applicati: `retry_delay_ms()` (`opendesk/network/relay_client.py`) con
backoff esponenziale 5 s → 60 s + jitter ±20%, contatore di tentativi e reset
su connessione riuscita (`host_app.py`, `services/connection_service.py`);
teardown loggati solo se c'era qualcosa da fermare.

## Episodio 2 — thrash dell'adaptive quality (06:07:53 → 06:08:15)

| ora | evento | cpu | effetto |
|---|---|---|---|
| 06:07:53 | encoder init crf=18 | – | – |
| 06:07:55 | tier 0→1 (idle=4) | 7% | rebuild encoder → crf=14 |
| 06:08:01 | tier 1→0 (idle=12) | 67% | rebuild → crf=18 |
| 06:08:04 | tier 0→1 (idle=6) | 52% | rebuild → crf=14 |
| 06:08:07 | peer chiede keyframe | | |
| 06:08:10 | tier 1→2 (idle=23) | 8% | rebuild → crf=10 |
| 06:08:13 | peer chiede keyframe | | |

Causa radice: `cpu_load_percent()` misura la CPU **totale** dell'host, che
include il costo del boost appena applicato (keyframe 1080p a CRF basso). Il
controllore quindi si auto-declassa, poi si ri-boosta: 4 flip in 17 s, ognuno
con release+re-init dell'encoder (reference frame del client invalidato → il
client richiede keyframe).

Fix applicati (`opendesk/services/pipeline.py`):

- `_CPU_TIER_EXIT = 0.75`: isteresi d'uscita (`_choose_quality_tier` accetta
  `current_tier`), con i valori del log il flip a 67% non avviene più.
- `_BOOST_WARMUP_S = 10.0`: nessun boost nei primi 10 s di sessione
  (l'avvio non è idle rappresentativo).
- `_IDLE_TIER1_FRAMES` 4 → 10 (~5 s a 2 fps idle).
- tier 2 con `pixel_format` fisso dall'utente: skip di `yuv444p` loggato
  invece di essere silenzioso.

## Verifica (simulata, 2026-11-21)

- Serie CPU/idle **reale dal log** ripassata nei due controllori:
  pre-fix `tier=[0,1,0,1,2]` (4 flip), post-fix `tier=[0,2]` (1 flip).
- Backoff su 10 tentativi falliti: cadenza 20 s fissi (≈200 s) →
  5, 11, 18, 38, 68, 57, 66, 63, 65, 64 s (≈605 s), con una riga di log per
  tentativo invece di 4 di teardown.
- Sessione idle simulata di 60 s (warm-up 10 s, CPU 5% → 55% per costo del
  boost): **1 flip** (0→1) e resto stabile; con CPU 5% → 95% (host davvero
  occupato): boost + declassamento a 0, poi fermo → nessuna oscillazione.
- Outage simulato di 12 tentativi: cadenza 6, 8, 24, 39, 51, 63, 68, 67, 69,
  52, 67, 48 s (≈744 s vs 240 s a intervallo fisso), 2 righe di log per
  tentativo invece di 6; dopo una riconnessione riuscita il contatore torna
  al minimo (4-9 s).
- `pytest tests/` 196 passed / 2 skipped, `ruff check` + `mypy` puliti.
- Non verificabile qui: sessione reale host↔client (serve il relay e un
  secondo device). Da rifare sul campo: relay irraggiungibile per 3 min
  (attesa: cadenza crescente, nessun flooding) e sessione idle 60 s
  (attesa: al massimo un flip di tier, nessuna richiesta keyframe dal client).

## Seconda tornata (run live 06:25-06:27 → correzioni)

Log: `~/.local/share/opendesk/logs/opendesk.log`. Client Linux/Wayland connesso
all'host `90763aa8…`: relay, auth, E2E, ICE e P2P tutti OK, **1 frame e poi
silenzio** (`No video activity` + `Frame timeout — no video frame received for
10 seconds`). L'host remoto non inviava frame; il client chiedeva keyframe
all'infinito (6/12/24/48 s) senza dirlo all'utente né loggare l'esito.

Correzioni:

- `_keyframe_watchdog` conta le richieste consecutive rimaste senza frame: alla
  3ª (`_VIDEO_STALL_ATTEMPTS`) logga `ERROR` con la durata reale dello stallo ed
  emette `RelayClient.video_stalled(int)` (`relay_client.py`).
- `main_window._on_video_stalled` + `RemoteViewer/ViewerWindow.set_stalled`:
  il placeholder mostrato all'utente diventa "Remote device is not sending
  video (Ns) — check OpenDesk on the other PC" invece di restare muto; il
  messaggio sparisce al primo frame utile.
- Shutdown: `stop()` conserva il future della DISCONNECT; `_drain_stop()` lo
  porta a termine (max 3 s) e i task residui vengono cancellati e attesi prima
  di `loop.close()` — sparisce `ERROR asyncio: Task was destroyed but it is
  pending!` e la sessione non resta più fantasma sul relay.

Test aggiunti: `TestSessionStop` (drain della stop pendente) e
`TestVideoStallSignal`. Suite: 199 passed / 2 skipped.

## Altri difetti corretti

- **Race di riavvio pipeline**: `StreamService.stop_streaming` azzerava
  `_pipeline` subito e faceva il join in un thread; un peer che rientrava
  subito creava una nuova pipeline mentre i vecchi worker rilasciano capture
  ed encoder. Ora `start_streaming()` attende il thread di shutdown
  (`_SHUTDOWN_JOIN_TIMEOUT_S = 10`).
- **Log periodici ambigui**: `frames=N window=5s fps=X target=Y` (prima
  "15 frames captured … target 2" stride con l'header "30 FPS").
