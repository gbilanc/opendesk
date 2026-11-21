# RCA — host congelato dopo disconnect peer (2026-11-21, 07:11–07:16)

Fonte: `opendesk.log` (344 righe, host Windows, OpenDesk v3.4.1, capture MSS,
H.264 software). L'host è rimasto bloccato: nessun log dopo le 07:13:46,
nemmeno dal HangWatchdog (soglia 3 s, re-report ogni 5 s).

## Sintomi nel log

1. **07:11:45** — UI thread bloccato 3.4 s in avvio. Il dump nativo py-spy
   mostra il main thread dentro
   `QWindowsDirectWriteFontDatabase::populateFamily` con **15 chiamate
   ripetute a `DWriteCreateFactory`**: enumerazione font DirectWrite
   patologica su quella macchina, scatenata dalla font-fallback durante la
   costruzione di `SessionInfoWidget` (pulsanti con emoji 🔄/🌙).
2. **07:12:42 → 07:13:11** — adaptive quality in oscillazione:
   tier 0→1→0→1→2→1→2 con **~9 release/re-init dell'encoder in 40 s**.
   Il peer chiede keyframe 4 volte (decoder affamato), fps 2–7.
3. **07:13:46** — P2P cade, fallback relay, peer disconnette, pipeline
   fermata **pulitamente**. Poi silenzio totale.

## Cause

### A. Freeze finale: hang nativo con GIL tenuto

Il watchdog non ha loggato nulla dopo le 07:13:46 pur essendo attivo:
impossibile per uno stallo a livello Python (lo avrebbe dumpato ogni 5 s).
Il main thread si è bloccato in codice nativo **tenendo il GIL**, affamando
anche il thread watchdog. Candidato unico coerente con l'evidenza (A1): una
font-fallback DirectWrite bloccata in `DWriteCreateFactory`, ri-scatenata
dagli aggiornamenti testo/UI dopo il disconnect (`setWindowTitle`,
status label). Il percorso di stop della pipeline è completo e bounded
(join con timeout), quindi escluso.

### B. Oscillazione adaptive quality (stream degradato)

- `_update_quality_tier()`: l'isteresi `_QUALITY_FLIP_HYSTERESIS_S` si
  applicava solo ai **downgrade**; l'upgrade era immediato → flip 1 s dopo
  il downgrade (07:13:10→07:13:11).
- `_choose_quality_tier()`: isteresi CPU solo sul confine busy→tier0, non
  sul confine tier1↔tier2 (soglia singola 0.30; CPU osservata 28–36% → flap).

Ogni flip rifaceva l'encoder x264 → keyframe storm + spike di latenza →
percezione di "schermo congelato" lato client e disconnessione.

## Fix applicati

1. `pipeline.py:_update_quality_tier` — isteresi su **ogni** cambio di tier.
2. `pipeline.py:_choose_quality_tier` — nuova soglia `_CPU_TIER2_EXIT = 0.40`:
   chi è al tier 2 lo lascia solo oltre 0.40 (ingresso resta < 0.30).
3. `ui/session_info.py` — rimossi glifi emoji dai pulsanti (icona nativa
   `SP_BrowserReload` + icona sole/luna disegnata in codice): niente
   font-fallback nel widget costruito all'avvio e toccato a ogni sessione.
4. `utils/hang_watchdog.py` — heartbeat su file `watchdog.beat` accanto al
   log: se il processo si congela col GIL tenuto, il file resta fermo
   all'istante del freeze (evidenza post-mortem).

## Verifiche lato macchina utente (step 5)

Il sottosistema font di quell'host Windows è degrado; per risanarlo:

```bat
:: 1. Ricostruire la cache font (da admin)
net stop "Windows Font Cache Service"
del /q %WinDir%\ServiceProfiles\LocalService\AppData\Local\FontCache\*
net start "Windows Font Cache Service"
:: 2. Riavviare la macchina
```

- Verificare font di terze parti corrotti: impostazioni → Caratteri, o
  convalida con `fontdrvhost`/strumenti di validazione; rimuovere font
  installati di recente prima dell'episodio.
- Installare/aggiornare i driver GPU: con NVENC/AMF attivo l'encoding non
  grava più sulla CPU (qui x264 software 1080p → CPU al 30–70%).
- Se il problema persiste: avviare l'host con il font engine alternativo
  (`QT_QPA_FONTENGINE=freetype`) e segnalarlo — diventa candidato per un
  flag persistente nelle impostazioni.
- Dopo un eventuale nuovo freeze: controllare il timestamp di
  `watchdog.beat` nella cartella log — conferma se il blocco è totale
  (GIL nativo) o solo della UI.
