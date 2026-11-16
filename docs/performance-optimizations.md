# Ottimizzazioni performance — OpenDesk Client

Raccolta degli interventi di performance applicati al client dopo la
revisione del 2026-10.  L'obiettivo principale era eliminare i blocchi del
main thread Qt durante la creazione della sessione (vedi `stall_dump.txt`)
e ridurre il lavoro inutile nelle hot-path capture → encode → send.

## Riepilogo

| # | File | Modifica | Impatto |
|---|------|----------|---------|
| 1 | `opendesk/crypto/auth.py` | Parametri Argon2id portati al minimo OWASP (`t=2, m=19 MiB, p=1`) | Hash da ~94 ms a ~47 ms (−50%); memoria −70% |
| 2 | `opendesk/ui/session_info.py`, `opendesk/ui/main_window.py` | Rimosso il **doppio hash** e la sessione orfana nella creazione di "Nuova sessione" | Metà del lavoro Argon2 eliminato; niente `PendingSession` leakate |
| 3 | `opendesk/utils/async_task.py` (nuovo), `opendesk/ui/main_window.py` | Hash Argon2 residuo eseguito **fuori dal main thread** (`run_async`) | UI mai congelata durante la creazione sessione |
| 4 | `opendesk/services/pipeline.py` | `EncoderWorker._do_tiles` a 3 fasi: rileva i tile cambiati, decide keyframe, poi codifica | Nessun JPEG codificato e scartato quando scatta il keyframe (>30% tile) |
| 5 | `opendesk/core/video_codec.py` | `_try_open_codec` con `@lru_cache` | Probe degli encoder HW eseguito una sola volta per processo |
| 6 | `opendesk/network/relay_client.py` | `msg.encode()` spostato sul thread chiamante (`NetworkWorker`) | L'event loop asyncio esegue solo `write`/`drain` |
| 7 | `opendesk/ui/viewer.py` | Copia numpy → bytearray senza `.tobytes()` intermedio | −6 MB di temporaneo per frame @1080p |
| 8 | `opendesk/crypto/auth.py` | `secrets.choice()` al posto di `random` per session ID e OTP | Fix di sicurezza (CSPRNG), nessun impatto perf |

## Dettaglio

### 1–3. Creazione sessione (Argon2)

Il `stall_dump.txt` mostrava il main thread bloccato dentro
`argon2.low_level.hash_secret` per ~2.5 s su Windows durante
`AuthManager.create_session()`.  Tre cause concorrenti:

1. **Parametri troppo costosi.** La configurazione precedente
   (`time_cost=3, memory_cost=65536, parallelism=4`) era ~3× il minimo
   raccomandato OWASP.  Portata a `time_cost=2, memory_cost=19456,
   parallelism=1`.  Gli hash esistenti vengono ri-generati al prossimo
   login riuscito tramite `needs_rehash`.
2. **Doppio hash.** `SessionInfoWidget.refresh_session()` creava un
   `PendingSession` (hash Argon2) e subito dopo `MainWindow` ne creava un
   secondo con la stessa password: due hash sul main thread e una sessione
   orfana in `_pending_sessions`.  Ora il widget genera solo la password;
   la sessione la crea una volta `ConnectionService`.
3. **Hash sul main thread.** L'hash residuo di "Nuova sessione" viene ora
   calcolato su un thread dedicato tramite `run_async()` e consegnato al
   main thread con `QueuedConnection`.  `AuthManager.create_session()`
   accetta un `password_hash` precalcolato per evitare di ricalcolarlo.

`run_async()` mantiene un riferimento forte al `QObject` bridge finché il
risultato non è stato consegnato, per evitare che la GC Python lo raccolga
prima dell'evento queued.

### 4. Tile a 3 fasi (`EncoderWorker._do_tiles`)

Prima l'intero tile grid veniva iterato codificando in JPEG ogni tile
cambiato; se poi più del 30% dei tile era cambiato, si scartava tutto e si
inviava un keyframe.  Ora:

1. si individua l'insieme dei tile cambiati **senza codificare**;
2. si aggiorna il change ratio e, se > `_TILE_MAX_CHANGED_RATIO`, si invia
   il keyframe e si esce;
3. altrimenti si codificano e inviano solo i tile cambiati.

### 5. Cache dei probe codec

`VideoEncoder._choose_codec()` prova fino a 8 encoder hardware con
`av.add_stream()`.  Questa verifica veniva ripetuta a ogni rebuild
dell'encoder (adaptive quality boost/restore) e in `default_codec()`.
La disponibilità degli encoder è statica per processo, quindi
`_try_open_codec` è ora memoizzata con `@lru_cache(maxsize=32)`.

### 6. Serializzazione fuori dall'event loop

`_RelaySession.send_message()` serializza ora il messaggio con
`msg.encode()` sul thread chiamante (tipicamente `NetworkWorker`) e
schedula sul loop asyncio solo `_write_async(data)`, che esegue
`writer.write()` + `await drain()`.  In precedenza il msgpack di frame e
keyframe grandi girava sul thread dell'event loop, allungando i tempi di
drain e introducendo jitter.

### 7. Rendering senza temporaneo

`RemoteViewer.display_frame()` copiava il frame numpy in un bytearray con
`buf[:] = rgb_data.tobytes()`, creando un `bytes` temporaneo grande quanto
il frame (~6 MB @1080p, ~180 MB/s a 30 fps).  Ora la copia avviene
direttamente con `np.frombuffer(buf).reshape(...)[:] = rgb_data`.

## Verifica

- `tests/test_async_task.py` copre: consegna del risultato sul main thread,
  propagazione errori, non-blocking del chiamante, hash precalcolato e
  assenza di sessioni orfane.
- `tests/test_tile_grid_diagnostic.py` verifica qualità e assenza di deriva
  del path tile.
- Suite: `QT_QPA_PLATFORM=offscreen python -m pytest tests/` → 147 passed
  (2 fail ambientali legati a `/dev/uinput`).

## Round 2 — percorso client (video → UI)

| # | File | Modifica | Impatto |
|---|------|----------|---------|
| 9 | `network/relay_client.py` | Log `VIDEO_FRAME`/`Frame decoded` da INFO a DEBUG | 60 righe di log/s eliminati a 30 fps |
| 10 | `network/relay_client.py` | Path keyframe: `decode()` restituisce un array fresco → l'inbox prende `rgb` direttamente, una sola `.copy()` (il reference frame) invece di due | −1 copia full-frame per frame (−6 MB @1080p) |
| 11 | `ui/viewer.py` | `display_frame()` wrappa il numpy direttamente nel `QImage` (fallback ring-buffer solo per array non contiguous) | −~0.3 ms/frame di memcpy; chiusura finding 🔵 |
| 12 | `network/relay_client.py`, `ui/viewer.py`, `ui/main_window.py` | Nuovo segnale `tile_received`: i tile JPEG compongono il QPixmap con un blit di regione invece di copiare l'intero reference frame in UI per ogni tile | tile 128² = blit ~50 KB invece di copia+QPixmap da 6 MB |
| 13 | `network/relay_client.py` | `_POLL_INTERVAL_MS` 50 → 16: i frame sono già coalesced per ciclo, la latenza di presentazione non supera 16 ms | −~34 ms di latenza coda in UI |
| 14 | `services/pipeline.py` | Rilevamento tile in `_do_tiles` con `cv2.integral` (somma d'area, soglia relativa all'area reale del tile) invece del loop Python 135×slice+sum | 2.34 → 1.46 ms/frame @1080p, verificato equivalente su 60 casi random |

## Verifica Round 2

- Equivalenza semantica tile-detect: confronto loop-orig vs integral su 60
  casi random (dimensioni non multiple di 128 incluse): 0 mismatch.
- Costo tile-detect misurato: 1.46 ms/frame (cambio piccolo, ~4 tile).
- Suite: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/` →
  155 passed, 12 failed identici al baseline pre-modifiche (environment:
  `aiortc`/`uinput` indisponibili nel container di test), nessun regresso.

## Finding non applicati

| Priorità | Posizione | Nota |
|---|---|---|
| 🟠 | `ui/main_window.py`, `host_app.py` | La sessione iniziale all'avvio è ancora creata in modo sincrono (prima che la finestra sia mostrata): impatto ~47 ms, non percepito. |
| 🟡 | `core/video_codec.py` `VideoDecoder.decode` | ~~`to_rgb().to_ndarray()` costa una conversione in più~~ → **misurato: non significativo** (0.53 vs 0.51 ms @720p, output identico). Nessun cambio. |
| 🟡 | `core/screen_capture.py` `compute_dirty_region` | `np.argwhere` alloca un array per pixel cambiato; `cv2.boundingRect` sarebbe più efficiente (funzione attualmente non usata in hot-path). |
| 🔵 | `ui/viewer.py` | ~~Il ring buffer copia ancora il frame~~ → **applicato nel Round 2 (#11)** |

## Round 3 — verso lo zero-copy (buffer GPU)

Obiettivo: non copiare il frame in RAM se il backend lo ha già in memoria
GPU (DMA-BUF su PipeWire, texture D3D11 su DXGI).  Round 3 step 1 =
baseline misurata, step 2+ = refactor (vedi piano).

### Harness di misura

`tools/measure_capture_encode.py` misura per stadio **wall time e CPU time**
(`time.process_time`, tutti i thread), separando l'attesa bloccante su
pipe/portal dal costo reale:

```bash
.venv/bin/python tools/measure_capture_encode.py --backend PORTAL --frames 30 --json out.json
.venv/bin/python tools/measure_capture_encode.py --backend MSS/MSS/... --no-encode
```

Per l'host Windows il riferimento resta `tools/measure_cpu.py`
(CPU% per thread di `opendesk-host`, scenario idle e attività).

### Baseline misurata (Linux/Wayland, 1366x768, x86_64, nessun encoder HW)

Schermo quasi statico, backend PORTAL (`wayland_capture.WaylandScreenCast`):

| Stadio | wall mediana | CPU mediana |
|---|---|---|
| capture (pipe→numpy+copy) | 47.2 ms | **3.4 ms** |
| diff + tile-detect (`_changed_mask` + `cv2.integral`) | 5.3 ms | **5.3 ms** |
| JPEG tile cambiati (0 tile in idle) | 0.0 ms | 0.0 ms |
| H.264 full frame (x264 veryfast, CRF 23) | 11.5 ms | **14.3 ms** |

Il wall time della cattura è quasi tutto **attesa** sulla pipe GStreamer
(fonte ~4-15 fps a schermo fermo, read bloccante): il costo CPU reale è la
copia del frame (3.1 MB) — è esattamente quella che il round 3 vuole
eliminare con encode-on-GPU lato helper.

### 15. `core/screen_capture.py` — PipeWire: buffer preallocato + `readinto`

`PipeWireCapture.capture_one` accumulava il frame con `bytes += chunk`: su
una pipe il kernel consegna al massimo ~64 KB per `read`, quindi un frame
1080p arrivava in ~100 pezzi e ogni concatenazione ricopiava tutto
l'accumulato (~23× il frame in memcpy; misurato: 14 letture/frame,
chunk medi 60 KB).  Ora `readinto(memoryview(buf)[offset:])` scrive
direttamente all'offset giusto: una allocazione, zero copie di accumulo.
Impatto misurato sul path PORTAL: trascurabile (il costo era il read
bloccante), ma il path PIPEWIRE diretto ne beneficia.

### Finding del Round 3

| Priorità | Posizione | Nota |
|---|---|---|
| 🟠 | `core/screen_capture.py` `_changed_mask` | `cv2.absdiff` + 2× `cv2.max`: 7.5 ms/frame @1366x768 (single-thread OpenCV). Un diff su **scala di grigi** con gray del frame precedente in cache costa 0.78 ms (10× meno), ma cambia la sensibilità dei tile: va validato su casi reali prima di applicarlo. |
| 🟡 | `core/wayland_capture.py` `capture_frame_sync` | `read(frame_size)` bloccante: latenza = periodo della sorgente e nessun controllo di frame drop (si accoda). Con encode nell'helper (step 2) il problema sparisce. |
| 🟡 | `core/screen_capture.py` `monitors()` PORTAL | ~~Riporta la geometria da `mss` mentre il flusso reale è diverso~~ → **corretto (Round 3 #16)** |

### 16. `core/screen_capture.py` — geometria PORTAL coerente

`monitors()` in modalità PORTAL restituiva la geometria enumerata da mss
(XWayland, es. 1920x1080) mentre `pipewiresrc` consegnava 1366x768: la
`region` dei frame era incoerente con i pixel (dirty-region, cursore e
scaling lato client sbagliati).  Se la sessione portal è attiva ora si
riporta la geometria reale dello stream.

### 17. `core/screen_capture.py` — copie rimosse su Windows/X11

- `_capture_dxgi` / `_loop_dxgi`: `np.ascontiguousarray(...)` incondizionato
  su ogni frame (copia full-frame ~6 MB @1080p) → ora si converte solo se
  dtype o contiguità non sono già quelli giusti (dxcam è già uint8 C-contiguo).
- `_capture_mss`: `ScreenShot.rgb` di mss è già un `bytes` **nuovo** per ogni
  grab, quindi `np.frombuffer` non aliasa nulla: la conversione forzata era
  una copia inutile.  Rimosso anche il `.copy()` (residuo del Round 2).
- `draw_cursor_on_frame`: la copia “per rendere scrivibile” avveniva **prima**
  di sapere se c'è un cursore da disegnare → su Linux/macOS (dove
  `_cursor_state()` è sempre `None`) copiava il frame intero a ogni frame per
  nulla.  Ora lo stato del cursore si legge prima.

## Round 3 — step 2: encoding dentro l'helper PipeWire

Modalità **pre-encoded** (opzionale, `QSettings video/preencoded_capture`,
default **off**): il frame non lascia la GPU, l'H.264 lo produce
l'encoder dentro l'helper GStreamer e sullo stdout passano solo pacchetti
compressi (KB invece di 3-6 MB/frame).

Implementazione:

| File | Ruolo |
|---|---|
| `core/_pipewire_helper.py` | `--encode [auto\|elemento]`: pipeline `pipewiresrc ! videoconvert ! vah264enc\|vaapih264enc\|x264enc ! h264parse ! appsink`.  Header `ODPK`+`<III mode,w,h>`, poi record `<u32 len><u8 flags>` (bit0 = keyframe) |
| `core/wayland_capture.py` | `WaylandScreenCast(encoded=True)`, `capture_packet_sync()`, `helper_alive`; `_read_exact()` con `select` + `readinto` |
| `core/screen_capture.py` | `ScreenCapture(preencoded=True)` + `capture_packet()`; su qualsiasi errore disattiva la modalità (`_disable_preencoded`) |
| `services/pipeline.py` | `PipelineConfig.preencoded`: `CaptureWorker` mette `EncodedPacket` in coda, `EncoderWorker` li inoltra **senza** encoder PyAV né tile-diff |
| `services/stream_service.py` | legge `video/preencoded_capture` |

Fallback garantito: se l'helper non parte (es. manca un encoder H.264
GStreamer, vedi `gstreamer1.0-plugins-ugly` / VA-API), `setup()` fallisce,
`preencoded` si spegne e la pipeline riparte con frame + encoder locale /
tile JPEG, senza interruzioni dello stream (verificato end-to-end).

Test: `tests/test_preencoded_capture.py` (5 casi: framing, EOF, length
assurda, `helper_alive`, passthrough `EncodedPacket`).

**Non ancora validato su hardware con encoder**: questo container non ha
né `x264enc` né VA-API, quindi il happy-path (pacchetti H.264 reali
dall'helper) va provato su una macchina con `gstreamer1.0-plugins-ugly`
o driver VA-API.

## Round 3 — step 3: X11

- Su Linux `mss` usa già `XShmGetImage` (memoria condivisa, fallback a
  `XGetImage`): nessun cambio necessario al backend X11.
- `platform_config._detect_linux_x11` ora offre **PORTAL** e **PIPEWIRE**
  oltre a MSS: su X11 è possibile scegliere lo stesso path screencast di
  Wayland (quindi anche il pre-encoded zero-copy).  Default resta **MSS**:
  il portal chiede il permesso di condivisione a ogni sessione, MSS no.

## Round 3 — step 5: encoder HW end-to-end dietro flag

L'encoder HW vive **dentro l'helper** GStreamer, non in PyAV: `--encode auto`
prova in ordine `vah264enc` → `vaapih264enc` → `x264enc`, quindi su una
macchina con VA-API il frame passa DMA-BUF → encoder HW senza mai toccare
la RAM.  Il setting `video/preencoded_encoder` (default `auto`) permette di
forzare un elemento specifico.

Non implementato (e non necessario per lo scopo): il path PyAV con
`hw_frames_ctx`/D3D11 in-process.  Motivi: PyAV supporta le hwframes solo
parzialmente, va gestita la composizione del cursore prima dell'upload e
serve un host Windows per validarlo — contro un helper GStreamer che fa la
stessa cosa con 3 righe di pipeline.

## Round 3 — step 6: lato client

Verificato che non resta nulla di significativo da togliere senza riscrivere
il rendering su texture GPU (Qt RHI/OpenGL):

- `display_frame()` wrappa il numpy nel `QImage` senza copia (Round 2 #11);
- i tile compongono il `QPixmap` con blit di regione (Round 2 #12);
- `to_rgb().to_ndarray()` del decoder: misurato 0.53 ms vs 0.51 ms — non vale
  il cambio.

Il salto successivo sarebbe decodifica HW + rendering su texture (niente
upload CPU→GPU per frame), ma è un refactor del viewer non coperto dagli
test attuali e non giustificato dai numeri attuali (0.5 ms/frame).

## Round 3 — step 7: gate e rollout

| Impostazione | Default | Effetto |
|---|---|---|
| `video/capture_method` (già esistente) | AUTO | Forza MSS / DXGI / PORTAL / PIPEWIRE |
| `video/preencoded_capture` | **False** | Encoding H.264 nell'helper (zero-copy, solo screencast portal) |
| `video/preencoded_encoder` | `auto` | Elemento GStreamer (`auto`, `vah264enc`, `x264enc`, …) |

UI: checkbox nel tab Video delle impostazioni.  Rollback: qualunque errore
(helper assente, encoder mancante, portal rifiutato, helper terminato)
disattiva la modalità e la pipeline continua col path frame + encoder locale
— verificato end-to-end con `StreamingPipeline` su una macchina senza
encoder H.264 GStreamer (stream attivo: 1 keyframe + 20 tile in 12 s).

Test: `tests/test_preencoded_capture.py` + suite completa
(`174 passed, 12 failed` = i 12 fail ambientali noti).


