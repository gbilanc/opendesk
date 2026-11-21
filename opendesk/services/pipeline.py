"""Pipeline parallela di streaming: capture, encode e send su 3 thread.

Rimpiazza il timer singolo di ``StreamService._capture_and_send()``
con una pipeline multi-thread:

  [CaptureThread] ──frame_queue──► [EncoderThread] ──pkt_queue──► [NetThread]

Ogni thread è indipendente, quindi l'encoding non blocca mai la cattura
e l'invio non blocca l'encoding.  Le code hanno una dimensione massima
per evitare che un thread lento accumuli memoria indefinitamente.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import queue
import threading
import time
from collections.abc import Callable

import cv2
import numpy as np

from opendesk.core.screen_capture import ScreenCapture, _changed_mask
from opendesk.core.video_codec import (
    _QUALITY_CRF,
    _QUALITY_PRESET,
    EncodedPacket,
    EncoderConfig,
    QualityLevel,
    VideoEncoder,
)
from opendesk.utils.platform import cpu_load_percent_external

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRAME_QUEUE_MAX = 3  # max frames in flight (back-pressure)
_PKT_QUEUE_MAX = 30  # max encoded packets queued for network

# ── Tile grid / keyframe constants ─────────────────────────────────
_TILE_SIZE = 128  # tile width/height in pixels
_TILE_THRESHOLD = 16  # pixel difference threshold for change detection
# Soglia per-tile: frazione minima di pixel cambiati perché il tile sia
# inviato. 0.05% di 128×128 = ~8px: un carattere piccolo o il caret di
# testo (2×15px) superano la soglia; il rumore di 1-2px no.  La vecchia
# soglia 0.5% (~82px) lasciava passare il cursore del mouse (~256px) ma
# non i caratteri digitati — lo schermo remoto non si aggiornava.
_TILE_CHANGE_RATIO = 0.0005
_TILE_MAX_CHANGED_RATIO = 0.30  # if more tiles changed, send full frame
_KEYFRAME_INTERVAL = 60  # keyframe every N frames (~2s at 30fps) with motion

# Throttle ricreazione encoder per adaptive quality (boost/restore CRF)
_REBUILD_MIN_INTERVAL = 2.0  # s
# Isteresi anti-oscillazione dell'adattivo: dopo un flip (boost/restore)
# non ammettere il flip opposto prima di altrettanti secondi.  Senza,
# una change_ratio che oscilla attorno a 0.05 rifà l'encoder ogni 2-6 s
# (10+ rebuild/minuto: pressione GIL + CPU; RCA 2026-11-14).
_QUALITY_FLIP_HYSTERESIS_S = 5.0

# ── Boost qualità sfruttando la CPU idle dell'host ─────────────────
# Tier crescenti applicati quando lo schermo è fermo E la CPU è libera:
# il costo di un keyframe "bello" è irrilevante se nessuno sta usando
# l'host, quindi si reinveste in CRF/preset invece di sprecare il margine.
_IDLE_TIER1_FRAMES = 10  # ~5 s a _MIN_IDLE_FPS
_IDLE_TIER2_FRAMES = 20  # ~10 s a _MIN_IDLE_FPS
_CPU_FREE_TIER1 = 0.60  # carico host sotto cui parte il boost leggero
_CPU_FREE_TIER2 = 0.30  # carico host sotto cui parte il boost massimo
# Isteresi d'uscita: il campione CPU include il costo dell'encoder stesso,
# quindi un boost appena applicato si auto-declasserebbe (rebuild a catena).
_CPU_TIER_EXIT = 0.75
# Isteresi sul confine tier1↔tier2: chi è già al tier 2 lo lascia solo oltre
# questa soglia (più alta di _CPU_FREE_TIER2).  Senza, una CPU che oscilla
# attorno a 0.30 fa flap tier 1↔2 a ogni frame → release/re-init encoder
# continui (RCA 2026-11-21: 9 rebuild in 40 s, decoder del peer affamato).
_CPU_TIER2_EXIT = 0.40
# Nessun boost nei primi secondi di sessione: l'avvio (init encoder,
# primo keyframe, assestamento rete) non è rappresentativo del carico idle.
_BOOST_WARMUP_S = 10.0
_MIN_BOOST_CRF = 12  # limite di sicurezza (sotto = bitrate ingestibile + CPU)
_MAX_BOOST_KEYFRAME_MS = 300.0  # oltre: il preset lento satura l'encoder
_TIER_BLOCK_OUT_S = 30.0  # attesa dopo un downgrade per saturazione

# Scala dei preset libx264/libx265 dal più veloce al più lento.  Il boost
# scende di 1 gradino al Tier 1 e di 3 al Tier 2 (mai oltre l'ultimo).
_PRESET_LADDER = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "veryslow",
]
# Tier 2: 2 gradini (non 3) e CRF −6 (non −8): su encoder software il
# salto superfast→fast a CRF 10 portava la CPU host al 90%+ (RCA 2026-11-21).
_PRESET_TIER_STEP = {1: 1, 2: 2}
_SW_CODECS = {"h264", "libx264", "hevc", "libx265"}


def _choose_quality_tier(idle_frames: int, cpu_load: float | None, current_tier: int = 0) -> int:
    """Sceglie il tier qualità (0=base, 1=boost, 2=boost massimo).

    ``cpu_load`` è normalizzato 0..1; ``None`` (piattaforma senza
    campionatore) vale come carico sconosciuto: si resta prudenti al
    Tier 1, mai al 2.

    ``current_tier`` introduce l'isteresi d'uscita: chi è già boostato
    lascia il boost solo oltre ``_CPU_TIER_EXIT`` (soglia più alta di quella
    d'ingresso), perché il carico misurato include il costo del boost
    stesso — senza, il tier oscilla a ogni chiave (RCA 2026-11-21).
    """
    if idle_frames < _IDLE_TIER1_FRAMES:
        return 0
    busy_threshold = _CPU_TIER_EXIT if current_tier > 0 else _CPU_FREE_TIER1
    if cpu_load is not None and cpu_load >= busy_threshold:
        return 0
    if idle_frames >= _IDLE_TIER2_FRAMES and cpu_load is not None:
        tier2_threshold = _CPU_TIER2_EXIT if current_tier == 2 else _CPU_FREE_TIER2
        if cpu_load < tier2_threshold:
            return 2
    return 1


def _slower_preset(preset: str, tier: int) -> str:
    """Scende di ``_PRESET_TIER_STEP[tier]`` gradini nella scala preset."""
    idx = _PRESET_LADDER.index(preset) if preset in _PRESET_LADDER else 2
    return _PRESET_LADDER[min(idx + _PRESET_TIER_STEP.get(tier, 0), len(_PRESET_LADDER) - 1)]


# Rete di sicurezza: anche a schermo fermo (idle) invia un keyframe di
# riallineamento ogni N frame catturati — a ~1 fps idle sono ~5 minuti.
# Il traffico minimo che tiene vivo il flusso è il keepalive 1×1
# (``_IDLE_KEEPALIVE_INTERVAL``), non questo keyframe.
_KEYFRAME_IDLE_INTERVAL = 300
# Heartbeat: se non parte nulla da N secondi (desktop fermo o bloccato),
# invia un tile 1×1 del frame corrente. Senza traffico il client
# interpreta il silenzio come stream morto e martella di richieste
# keyframe da ~340 KB; il tile costa ~600 B/s e tiene vivo il flusso.
_IDLE_KEEPALIVE_INTERVAL = 1.0  # s
_BOOST_KEYFRAME_MIN_INTERVAL = 30.0  # s — max un keyframe boostato in raffica
# JPEG quality per quality preset (lossy ma ~10× più veloce del PNG).
_TILE_JPEG_QUALITY: dict[QualityLevel, int] = {
    QualityLevel.LOW: 50,
    QualityLevel.MEDIUM: 65,
    QualityLevel.HIGH: 80,
    QualityLevel.SHARP: 95,
    QualityLevel.LOSSLESS: 98,
}


@dataclasses.dataclass
class PipelineConfig:
    """Runtime configuration for the streaming pipeline.

    Snapshot of settings read from QSettings at pipeline start.
    """

    fps: int = 30
    quality: QualityLevel = QualityLevel.HIGH
    bitrate: int = 8_000_000
    resolution_scale: float = 1.0
    monitor_index: int = 0
    codec: str = ""  # auto-detect if empty
    crf: int | None = None  # None = use bitrate, int = CRF mode
    pixel_format: str = "yuv420p"  # "yuv420p" or "yuv444p"
    encoder_preset: str = ""  # empty = auto by quality level
    adaptive_fps: bool = True  # riduce l'fps di cattura a schermo fermo
    # Sfrutta la CPU idle dell'host per alzare CRF/preset (e yuv444p) sui
    # keyframe a schermo fermo.  Disattivabile dalle impostazioni.
    idle_quality_boost: bool = True
    # True se il pixel format non è stato scelto esplicitamente dall'utente
    # (in tal caso il Tier 2 può passare a yuv444p per testo più nitido).
    pixel_format_auto: bool = True
    # Override del numero di thread dell'encoder SW ("" = default codec).
    encoder_threads: str = ""
    # True = il backend consegna H.264 già codificato (encoding dentro
    # l'helper PipeWire, zero-copy GPU→encoder): niente tile-diff né
    # encoder PyAV in-process.  Viene disattivato a runtime se il backend
    # non lo supporta (vedi ``ScreenCapture.preencoded``).
    preencoded: bool = False
    # Encoder GStreamer usato dall'helper in modalità pre-encoded
    # ("auto" = VA-API se disponibile, altrimenti x264enc).
    preencoded_encoder: str = "auto"


# FPS minimo di cattura a schermo fermo.  L'encoder invia comunque il
# keepalive 1×1 ogni ~1s (serve un frame fresco al secondo) e i keyframe
# idle periodicamente riallineano il client — la qualità percepita resta
# quella di uno screenshot vivo, con una frazione del CPU (RCA 2026-11-14:
# la cattura BitBlt+conversione a 30fps costava anche da fermi).
_MIN_IDLE_FPS = 2.0


# ═══════════════════════════════════════════════════════════════════
# CaptureWorker
# ═══════════════════════════════════════════════════════════════════


class CaptureWorker(threading.Thread):
    """Thread che cattura lo schermo e invia frame all'encoder.

    Spara frame alla frequenza configurata, indipendentemente
    dalla velocità dell'encoder.  Se l'encoder è più lento,
    ``frame_queue`` si riempie fino a ``_FRAME_QUEUE_MAX`` e poi
    il worker salta frame (back-pressure naturale).

    In caso di errore critico chiama ``on_error`` (se fornito)
    per permettere al chiamante di arrestare la pipeline.
    """

    def __init__(
        self,
        config: PipelineConfig,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        on_error: Callable[[str], None] | None = None,
        shared: dict | None = None,
    ) -> None:
        super().__init__(name="CaptureWorker", daemon=True)
        self._config = config
        self._frame_queue = frame_queue
        self._stop_event = stop_event
        self._on_error = on_error
        self._capture: ScreenCapture | None = None
        self._shared = shared

    def run(self) -> None:
        logger.info("CaptureWorker started")
        try:
            self._capture = ScreenCapture(
                preencoded=self._config.preencoded,
                preencoded_encoder=self._config.preencoded_encoder,
            )
        except Exception as e:
            msg = f"ScreenCapture init failed: {e}"
            logger.error("CaptureWorker: %s", msg)
            if self._on_error:
                self._on_error(msg)
            return
        fps_target: float = max(self._config.fps, 1)

        consec_errors = 0
        max_consec_errors = 10
        frame_idx = 0
        last_status_log = time.monotonic()
        frames_at_last_log = 0

        while not self._stop_event.is_set():
            t0 = time.perf_counter()

            try:
                if self._config.preencoded:
                    # Encoding già fatto dal backend: il pacchetto va dritto
                    # al network worker, il frame non entra mai in questa
                    # macchina (né diff, né tile, né encoder PyAV).
                    pkt = self._capture.capture_packet(self._config.monitor_index)
                    if pkt is None:
                        if not self._capture.preencoded:
                            self._config.preencoded = False
                            logger.warning(
                                "Pre-encoded capture non disponibile — "
                                "torno a frame + encoder locale"
                            )
                            continue  # il prossimo giro usa capture_one()
                        time.sleep(0.01)  # schermo fermo: nessun pacchetto
                        continue
                    consec_errors = 0
                    frame_idx += 1
                    try:
                        self._frame_queue.put((pkt, pkt.timestamp), block=True, timeout=0.1)
                    except queue.Full:
                        logger.debug("CaptureWorker: frame queue full, dropping packet")
                    continue

                frame = self._capture.capture_one(self._config.monitor_index)
                if frame is None:
                    # PipeWire ancora in fase di startup
                    time.sleep(0.01)
                    continue

                # Successo — resetta contatore errori
                consec_errors = 0
                frame_idx += 1

                # Status log ogni 5 secondi
                now_mono = time.monotonic()
                if now_mono - last_status_log >= 5.0:
                    fps_eff = (frame_idx - frames_at_last_log) / 5.0
                    logger.info(
                        "CaptureWorker: frames=%d window=5s fps=%.1f target=%.1f",
                        frame_idx,
                        fps_eff,
                        fps_target,
                    )
                    frames_at_last_log = frame_idx
                    last_status_log = now_mono

                # ── Resolution scaling ──
                scale = self._config.resolution_scale
                data = frame.data
                if scale != 1.0:
                    h, w = data.shape[:2]
                    nw, nh = int(w * scale), int(h * scale)
                    if nw > 0 and nh > 0:
                        # Adattivo: INTER_AREA è nitido per il testo ma costa
                        # ~11 ms/frame a 1080p (33% del budget a 30 fps).
                        # Per downscale lievi (>= 0.7) INTER_LINEAR è quasi
                        # indistinguibile e ~5x più veloce (≈2.3 ms).
                        interpolation = cv2.INTER_AREA if scale < 0.7 else cv2.INTER_LINEAR
                        data = cv2.resize(data, (nw, nh), interpolation=interpolation)

                # ── Invia all'encoder (non bloccante) ──
                try:
                    self._frame_queue.put(
                        (data, frame.timestamp),
                        block=True,
                        timeout=0.1,
                    )
                except queue.Full:
                    # Encoder è in ritardo — salta questo frame
                    logger.debug("CaptureWorker: frame queue full, dropping frame")

            except Exception as e:
                consec_errors += 1
                if consec_errors >= max_consec_errors:
                    msg = f"CaptureWorker: {consec_errors} errori consecutivi — arresto"
                    logger.error(msg)
                    if self._on_error:
                        self._on_error(f"Screen capture fallita: {e}")
                    break
                if consec_errors == 1:
                    logger.warning("CaptureWorker error: %s", e)
                elif consec_errors <= 3:
                    logger.warning("CaptureWorker error (%d/10): %s", consec_errors, e)
                # dopo 3 errori logga solo in debug per non spammare
                elif consec_errors <= max_consec_errors:
                    logger.debug("CaptureWorker error (%d/10): %s", consec_errors, e)

            # Frame rate: fisso o adattivo al change ratio dell'EncoderWorker
            # (già calcolato per i tile — nessun diff extra).  A schermo fermo
            # l'fps scende a _MIN_IDLE_FPS: il keepalive 1×1 dell'encoder e i
            # keyframe idle tengono il flusso vivo con una frazione del CPU.
            if self._config.adaptive_fps:
                ratio = float(self._shared.get("change_ratio", 1.0)) if self._shared else 1.0
                if ratio < 0.001:
                    fps_target = _MIN_IDLE_FPS
                elif ratio < 0.01:
                    fps_target = max(_MIN_IDLE_FPS, self._config.fps * 0.3)
                else:
                    fps_target = self._config.fps
            elapsed = time.perf_counter() - t0
            sleep = max(0.0, 1.0 / fps_target - elapsed)
            if sleep > 0.001:
                time.sleep(sleep)

        if self._capture:
            self._capture.release()
        logger.info("CaptureWorker stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ═══════════════════════════════════════════════════════════════════
# EncoderWorker
# ═══════════════════════════════════════════════════════════════════


class EncoderWorker(threading.Thread):
    """Thread che prende frame dalla coda e li codifica in H.264/H.265.

    Supporta sia full keyframe che tile JPEG.
    Emette packet sulla ``pkt_queue`` per il NetworkWorker.

    Watchdog: se non arrivano frame per 5s (CaptureWorker morto),
    chiama ``on_error`` per arrestare la pipeline.
    """

    def __init__(
        self,
        config: PipelineConfig,
        frame_queue: queue.Queue,
        pkt_queue: queue.Queue,
        stop_event: threading.Event,
        on_send_full_keyframe: Callable | None = None,
        on_send_tile: Callable | None = None,
        on_error: Callable[[str], None] | None = None,
        shared: dict | None = None,
    ) -> None:
        super().__init__(name="EncoderWorker", daemon=True)
        self._config = config
        self._frame_queue = frame_queue
        self._pkt_queue = pkt_queue
        self._stop_event = stop_event
        self._on_send_full_keyframe = on_send_full_keyframe
        self._on_send_tile = on_send_tile
        self._on_error = on_error
        self._shared = shared

        self._encoder: VideoEncoder | None = None
        self._prev_frame: np.ndarray | None = None
        self._frame_count: int = 0
        self._force_full_keyframe: bool = False
        # Tile droppati consecutivamente dalla pkt_queue (rete lenta):
        # se troppi di fila, forza un keyframe che rimpiazza i pacchetti
        # stale e riallinea il decoder del client.
        self._dropped_tiles: int = 0

        # Adaptive quality when screen is idle (tier basati su CPU host)
        self._last_change_ratio: float = 1.0  # 0.0 = idle, 1.0 = full change
        self._idle_frames: int = 0
        self._last_quality_flip_mono: float = 0.0
        self._quality_tier: int = 0
        self._start_mono: float = time.monotonic()  # warm-up del boost qualità
        self._tier_block_until_mono: float = 0.0
        self._last_boost_keyframe_mono: float = 0.0

        # Valori base (pre-boost) catturati alla creazione dell'encoder
        self._base_crf: int | None = None
        self._base_preset_setting: str = ""
        self._base_preset_effective: str = "veryfast"
        self._base_pixel_format: str = "yuv420p"
        self._base_threads: str = ""
        self._software_encoder: bool = False
        self._boost_bases_captured: bool = False

        # Throttle ricreazione encoder: il ping-pong boost/restore CRF può
        # arrivare a ricostruire il contesto PyAV 3-4 volte al secondo,
        # bloccando il flusso dei frame in output (spike di latenza) e
        # bruciando CPU.  Minimo 2s tra un rebuild e l'altro.
        self._last_rebuild_mono: float = 0.0
        self._rebuild_pending: bool = False

        # Ultimo pacchetto effettivamente accodato (heartbeat idle).
        self._last_packet_mono: float = time.monotonic()

    def run(self) -> None:
        logger.info("EncoderWorker started")
        last_frame_time = time.monotonic()
        encoded_count = 0
        frames_at_last_log = 0
        last_status_log = time.monotonic()

        while not self._stop_event.is_set():
            try:
                data, timestamp = self._frame_queue.get(block=True, timeout=0.5)
                last_frame_time = time.monotonic()
                encoded_count += 1

                # Status log ogni 5 secondi
                now_mono = time.monotonic()
                if now_mono - last_status_log >= 5.0:
                    logger.info(
                        "EncoderWorker: frames=%d window=5s fps=%.1f tier=%d",
                        encoded_count,
                        (encoded_count - frames_at_last_log) / 5.0,
                        self._quality_tier,
                    )
                    frames_at_last_log = encoded_count
                    last_status_log = now_mono
            except queue.Empty:
                # Schermo fermo/bloccato: nessun tile da inviare.  Un
                # heartbeat tiene vivo il flusso senza banda apprezzabile.
                self._maybe_send_keepalive()
                # Watchdog: se non arrivano frame per 60s, CaptureWorker e' morto
                # Il timeout è alto per coprire l'avvio lento di Wayland
                # (PORTAL dialog 30s + PIPEWIRE startup 10s).
                if time.monotonic() - last_frame_time > 60.0:
                    msg = "CaptureWorker died — no frames for 60s"
                    logger.error("EncoderWorker: %s", msg)
                    if self._on_error:
                        self._on_error(msg)
                    break
                continue

            if data is None:
                break

            if isinstance(data, EncodedPacket):
                # Pre-encoded dal backend: nessun lavoro di encoding qui.
                self._frame_count += 1
                self._last_packet_mono = time.monotonic()
                if self._queue_packet(data, keyframe=data.is_keyframe) and (
                    self._on_send_full_keyframe
                ):
                    self._on_send_full_keyframe(
                        data.data, data.width, data.height, data.pts, data.is_keyframe
                    )
                continue

            # Ricrea l'encoder (CRF cambiato da boost/restore) solo quando
            # il throttle lo consente
            if self._rebuild_pending:
                self._try_rebuild_encoder()

            h, w = data.shape[:2]
            pts = int(timestamp * 1000)
            self._frame_count += 1

            try:
                # Lazy init encoder
                if self._encoder is None:
                    crf = self._config.crf
                    if crf is None:
                        crf = _QUALITY_CRF.get(self._config.quality)
                    # Build options: preset/threads solo se espliciti, così
                    # video_codec applica la mappa preset-per-qualità.
                    enc_opts: dict[str, str] = {}
                    if self._config.encoder_preset:
                        enc_opts["preset"] = self._config.encoder_preset
                    if self._config.encoder_threads:
                        enc_opts["threads"] = self._config.encoder_threads

                    self._encoder = VideoEncoder(
                        EncoderConfig(
                            width=w,
                            height=h,
                            fps=self._config.fps,
                            bitrate=self._config.bitrate,
                            quality=self._config.quality,
                            codec=self._config.codec,
                            crf=crf,
                            pixel_format=self._config.pixel_format,
                            options=enc_opts,
                        )
                    )
                    if self._encoder.codec_name:
                        logger.info(
                            "EncoderWorker: using %s CRF=%s",
                            self._encoder.codec_name,
                            crf or "(bitrate)",
                        )
                    # Cattura i valori base per il boost (una sola volta)
                    if not self._boost_bases_captured:
                        self._base_crf = crf
                        self._base_preset_setting = self._config.encoder_preset
                        self._base_preset_effective = self._config.encoder_preset or (
                            _QUALITY_PRESET.get(self._config.quality, "veryfast")
                        )
                        self._base_pixel_format = self._config.pixel_format
                        self._base_threads = self._config.encoder_threads
                        self._software_encoder = self._encoder.codec_name in _SW_CODECS
                        self._boost_bases_captured = True
                    # La creazione conta come rebuild per il throttle
                    self._last_rebuild_mono = time.monotonic()

                # ── Adaptive quality: idle counter + tier basato su CPU ──
                if self._last_change_ratio < 0.05:
                    self._idle_frames += 1
                else:
                    self._idle_frames = 0
                self._update_quality_tier()

                # ── Decidi se inviare full keyframe o tile ──
                needs_keyframe = (
                    self._prev_frame is None
                    or self._force_full_keyframe
                    or (self._frame_count >= _KEYFRAME_INTERVAL and self._last_change_ratio > 0.0)
                    # Riallineamento periodico anche a schermo fermo:
                    # il client recupera da tile persi senza attendere
                    # il prossimo movimento (vedi _KEYFRAME_IDLE_INTERVAL)
                    or self._frame_count >= _KEYFRAME_IDLE_INTERVAL
                )

                if needs_keyframe:
                    # Resetta il contatore solo quando il keyframe parte
                    # davvero (altrimenti l'intervallo idle non scatta mai)
                    self._frame_count = 0
                    self._do_full_keyframe(data, w, h, pts)
                    # Il frame arriva esclusivamente da qui (frame_queue):
                    # nessun'altra componente lo riutilizza, quindi possiamo
                    # riferenciarlo direttamente senza fare una copia full-frame
                    # (~6 MB a 1080p per ogni keyframe, 30/s in movimento).
                    self._prev_frame = data
                    self._force_full_keyframe = False
                else:
                    self._do_tiles(data, w, h, pts)
            except Exception as e:
                logger.exception("EncoderWorker: encode error: %s", e)
                if self._encoder:
                    self._encoder.release()
                    self._encoder = None

        # Flush only on a natural end. During shutdown the network worker
        # may already be stopped, therefore blocking puts would deadlock.
        if self._encoder:
            if not self._stop_event.is_set():
                for pkt in self._encoder.flush():
                    self._queue_packet(pkt, keyframe=True)
            self._encoder.release()

        logger.info("EncoderWorker stopped")

    def request_keyframe(self) -> None:
        self._force_full_keyframe = True

    def _maybe_send_keepalive(self) -> None:
        """Invia un tile 1×1 se non parte nulla da ``_IDLE_KEEPALIVE_INTERVAL``.

        Serve solo a dire al client "lo stream è vivo": il tile contiene il
        pixel reale del frame corrente, quindi la composizione sul reference
        frame del client resta coerente e non si vede alcun artefatto.
        """
        prev = self._prev_frame
        if prev is None or time.monotonic() - self._last_packet_mono < _IDLE_KEEPALIVE_INTERVAL:
            return
        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                cv2.cvtColor(prev[0:1, 0:1], cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, _TILE_JPEG_QUALITY[self._config.quality]],
            )
        except Exception as e:  # noqa: BLE001 — heartbeat cosmetico
            logger.debug("EncoderWorker: keepalive encode failed: %s", e)
            return
        if not ok:
            return
        data = encoded.tobytes()
        pts = int(time.time() * 1000)
        if self._queue_packet(("tile", data, 0, 0, 1, 1, pts)) and self._on_send_tile:
            self._on_send_tile(data, 0, 0, 1, 1, pts)

    def _queue_packet(self, packet: object, *, keyframe: bool = False) -> bool:
        """Queue output without letting congestion block encoder shutdown.

        Delta tiles may be dropped under congestion because a subsequent tile
        refreshes the same region. A keyframe instead replaces stale queued
        packets, preserving a fresh decoder reference frame.
        """
        try:
            self._pkt_queue.put(packet, timeout=0.1)
            self._last_packet_mono = time.monotonic()
            if not keyframe:
                self._dropped_tiles = 0
            return True
        except queue.Full:
            if not keyframe:
                self._dropped_tiles += 1
                if self._dropped_tiles >= 5:
                    logger.warning(
                        "EncoderWorker: %d tile droppati consecutivamente — "
                        "forzo un keyframe per riallineare il client",
                        self._dropped_tiles,
                    )
                    self._force_full_keyframe = True
                else:
                    logger.debug("EncoderWorker: packet queue full, dropping tile")
                return False
            while True:
                try:
                    self._pkt_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._pkt_queue.put_nowait(packet)
                logger.debug("EncoderWorker: replaced stale packets with keyframe")
                return True
            except queue.Full:
                return False

    # ── internals ──────────────────────────────────────────────────

    def _do_full_keyframe(self, rgb: np.ndarray, w: int, h: int, pts: int) -> None:
        if self._encoder is None:
            return
        self._encoder.request_keyframe()
        t0 = time.perf_counter()
        packets = self._encoder.encode(rgb)
        encode_ms = (time.perf_counter() - t0) * 1000.0
        # Freno anti-saturazione: un preset lento che impiega troppo per un
        # keyframe boostato viene declassato e bloccato per un po'.
        if self._quality_tier > 0 and encode_ms > _MAX_BOOST_KEYFRAME_MS:
            logger.warning(
                "Adaptive quality: keyframe boostato %.0fms > %.0fms (tier %d) — "
                "downgrade e blocco boost per %.0fs",
                encode_ms,
                _MAX_BOOST_KEYFRAME_MS,
                self._quality_tier,
                _TIER_BLOCK_OUT_S,
            )
            self._tier_block_until_mono = time.monotonic() + _TIER_BLOCK_OUT_S
            self._apply_quality_tier(self._quality_tier - 1)
        for pkt in packets:
            if self._queue_packet(pkt, keyframe=True) and self._on_send_full_keyframe:
                self._on_send_full_keyframe(pkt.data, w, h, pts, pkt.is_keyframe)

    def _do_tiles(self, current: np.ndarray, w: int, h: int, pts: int) -> None:
        prev = self._prev_frame
        if prev is None or prev.shape != current.shape:
            self._do_full_keyframe(current, w, h, pts)
            self._prev_frame = current
            return

        tile_size = _TILE_SIZE
        threshold = _TILE_THRESHOLD
        quality = self._config.quality
        jpeg_q = _TILE_JPEG_QUALITY[quality]

        # Full-frame diff mask via il helper condiviso (cv2.absdiff + cv2.max,
        # un unico temporaneo single-channel invece di (H,W,3) bool di numpy).
        any_changed = _changed_mask(current, prev, threshold)
        assert any_changed is not None

        # Fase 1: individua i tile cambiati senza codificarli.  Così, se
        # il cambiamento supera ``_TILE_MAX_CHANGED_RATIO``, scegliamo il
        # keyframe senza sprecare l'encoding JPEG di tutti i tile (che
        # verrebbe comunque scartato).
        #
        # Block-or via somma d'area (cv2.integral): un'unica passata
        # O(H*W) + gather dei 4 angoli per tile, ~2.5× più veloce del
        # loop Python nx*ny (135 slice+sum a 1080p: 2.2 ms → 0.86 ms).
        # Soglia RELATIVA all'area reale del tile (bordo destro/basso
        # parziale), identica al loop originale.
        nx = (w + tile_size - 1) // tile_size
        ny = (h + tile_size - 1) // tile_size
        m8 = any_changed.view(np.uint8)
        if ny * tile_size != h or nx * tile_size != w:
            pad = np.zeros((ny * tile_size, nx * tile_size), dtype=np.uint8)
            pad[:h, :w] = m8
            m8 = pad
        ii = cv2.integral(m8)
        ys = np.arange(ny) * tile_size
        xs = np.arange(nx) * tile_size
        ye = np.minimum(ys + tile_size, h)
        xe = np.minimum(xs + tile_size, w)
        counts = ii[np.ix_(ye, xe)] - ii[np.ix_(ys, xe)] - ii[np.ix_(ye, xs)] + ii[np.ix_(ys, xs)]
        areas = np.outer(ye - ys, xe - xs)
        rows, cols = np.nonzero(counts > _TILE_CHANGE_RATIO * areas)
        changed_boxes = [
            (
                int(c) * tile_size,
                int(r) * tile_size,
                int(xe[c] - xs[c]),
                int(ye[r] - ys[r]),
            )
            for r, c in zip(rows, cols, strict=True)
        ]
        total_tiles = nx * ny

        # Come per il path keyframe: il frame è di proprietà del consumer
        # della coda, nessuna copia full-frame necessaria.
        self._prev_frame = current

        # Aggiorna change ratio per adaptive quality
        self._last_change_ratio = len(changed_boxes) / total_tiles if total_tiles else 0.0
        if self._shared is not None:
            self._shared["change_ratio"] = self._last_change_ratio

        # Fase 2: se troppi tile cambiati, meglio un full keyframe
        if total_tiles > 0 and self._last_change_ratio > _TILE_MAX_CHANGED_RATIO:
            self._do_full_keyframe(current, w, h, pts)
            self._frame_count = 0
            return

        # Fase 3: codifica e invia solo i tile effettivamente cambiati.
        for x, y, tw, th in changed_boxes:
            cur_tile = current[y : y + th, x : x + tw]
            tile_bgr = cv2.cvtColor(cur_tile, cv2.COLOR_RGB2BGR)
            success, encoded = cv2.imencode(
                ".jpg",
                tile_bgr,
                [cv2.IMWRITE_JPEG_QUALITY, jpeg_q],
            )
            if not success:
                continue
            tile_data = encoded.tobytes()
            packet = ("tile", tile_data, x, y, tw, th, pts)
            if self._queue_packet(packet) and self._on_send_tile:
                self._on_send_tile(tile_data, x, y, tw, th, pts)

    # ── adaptive quality ────────────────────────────────────────────

    def _update_quality_tier(self) -> None:
        """Aggiorna il tier qualità in base a idle schermo e carico CPU host."""
        if self._encoder is None:
            return
        if not self._config.idle_quality_boost:
            target, cpu = 0, None
        elif time.monotonic() - self._start_mono < _BOOST_WARMUP_S:
            # Warm-up: il carico dei primi secondi non è idle credibile
            return
        else:
            # Carico SENZA il nostro processo: quello totale include
            # l'encoder, quindi il boost si auto-declasserebbe (flapping)
            cpu = cpu_load_percent_external()
            target = _choose_quality_tier(self._idle_frames, cpu, self._quality_tier)
        if target == self._quality_tier:
            return
        # Isteresi anti-oscillazione su OGNI cambio di tier, non solo sui
        # downgrade: prima l'upgrade era ammesso subito, quindi una CPU sul
        # confine tier1↔2 (o idle_frames sul confine) ri-flippava 1-2 s dopo
        # il downgrade → rebuild encoder a catena (RCA 2026-11-21).
        if time.monotonic() - self._last_quality_flip_mono < _QUALITY_FLIP_HYSTERESIS_S:
            return
        if target > self._quality_tier and time.monotonic() < self._tier_block_until_mono:
            return
        self._apply_quality_tier(target, cpu)

    def _apply_quality_tier(self, tier: int, cpu_load: float | None = None) -> None:
        """Applica il tier: CRF più basso, preset più lento, più thread.

        Il costo extra è accettabile solo perché il boost parte a schermo
        fermo, quando l'host non sta facendo altro.  Un preset scelto
        esplicitamente dall'utente non viene mai modificato.
        """
        crf = self._base_crf
        if crf is not None:
            step = {1: 4, 2: 6}.get(tier, 0)
            crf = max(_MIN_BOOST_CRF, crf - step)

        preset = self._base_preset_effective
        if tier and self._software_encoder and not self._base_preset_setting:
            preset = _slower_preset(self._base_preset_effective, tier)

        pixel_format = self._base_pixel_format
        if tier == 2 and self._software_encoder and self._config.pixel_format_auto:
            # yuv444p: chroma a piena risoluzione, testo più nitido.  Vale
            # solo su keyframe rari (idle), quindi il costo CPU è irrilevante.
            pixel_format = "yuv444p"
        elif tier == 2 and self._config.pixel_format_auto is False:
            # Skip silenzioso: un pixel_format scelto dall'utente resta com'è
            logger.debug(
                "Adaptive quality: yuv444p non applicato (pixel_format fisso a %s)",
                pixel_format,
            )

        threads = self._base_threads
        if tier == 2 and self._software_encoder:
            threads = str(max(2, min(4, (os.cpu_count() or 2) - 1)))

        self._config.crf = crf
        self._config.encoder_preset = preset if tier else self._base_preset_setting
        self._config.pixel_format = pixel_format
        self._config.encoder_threads = threads

        logger.info(
            "Adaptive quality: tier %d→%d (idle=%d frames, cpu=%s, crf=%s, "
            "preset=%s, threads=%s, pix_fmt=%s)",
            self._quality_tier,
            tier,
            self._idle_frames,
            f"{cpu_load * 100:.0f}%" if cpu_load is not None else "n/d",
            crf,
            preset or "auto",
            threads or "auto",
            pixel_format,
        )
        self._quality_tier = tier
        self._last_quality_flip_mono = time.monotonic()
        self._try_rebuild_encoder()

        # Il client vede subito il frame migliore: forza un keyframe, ma
        # senza raffiche (al massimo uno ogni _BOOST_KEYFRAME_MIN_INTERVAL).
        now = time.monotonic()
        if tier > 0 and now - self._last_boost_keyframe_mono >= _BOOST_KEYFRAME_MIN_INTERVAL:
            self._force_full_keyframe = True
            self._last_boost_keyframe_mono = now

    def _try_rebuild_encoder(self, force: bool = False) -> None:
        """Rilascia l'encoder per ricrearlo (lazy) con il CRF corrente.

        Con ``force=False`` rinvia se l'ultimo rebuild è recente
        (``_REBUILD_MIN_INTERVAL``): la richiesta resta pendente e viene
        applicata al prossimo frame in cui il throttle lo consente.
        """
        now = time.monotonic()
        if not force and now - self._last_rebuild_mono < _REBUILD_MIN_INTERVAL:
            self._rebuild_pending = True
            return
        self._rebuild_pending = False
        self._last_rebuild_mono = now
        if self._encoder is not None:
            self._encoder.release()
            self._encoder = None  # sarà ricreato al prossimo frame


# ═══════════════════════════════════════════════════════════════════
# NetworkWorker
# ═══════════════════════════════════════════════════════════════════


class NetworkWorker(threading.Thread):
    """Thread che invia i pacchetti codificati al relay.

    Svuota la ``pkt_queue`` e chiama le funzioni di invio del relay.
    """

    def __init__(
        self,
        pkt_queue: queue.Queue,
        stop_event: threading.Event,
        send_frame_fn: Callable,
        send_tile_fn: Callable,
    ) -> None:
        super().__init__(name="NetworkWorker", daemon=True)
        self._pkt_queue = pkt_queue
        self._stop_event = stop_event
        self._send_frame = send_frame_fn
        self._send_tile = send_tile_fn

    def run(self) -> None:
        logger.info("NetworkWorker started")
        while not self._stop_event.is_set():
            try:
                item = self._pkt_queue.get(block=True, timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                break

            try:
                if isinstance(item, tuple) and item[0] == "tile":
                    # Tile JPEG
                    _, data, x, y, tw, th, pts = item
                    if not self._send_tile(data, x, y, tw, th, pts):
                        logger.debug("NetworkWorker: tile dropped (backpressure)")
                else:
                    # Full H.264 frame (EncodedPacket-like)
                    data = item.data
                    if not self._send_frame(
                        data,
                        item.width,
                        item.height,
                        item.pts,
                        keyframe=item.is_keyframe,
                    ):
                        logger.debug(
                            "NetworkWorker: %s dropped (backpressure)",
                            "keyframe" if item.is_keyframe else "frame",
                        )
            except Exception as e:
                logger.warning("NetworkWorker send error: %s", e)

        logger.info("NetworkWorker stopped")


# ═══════════════════════════════════════════════════════════════════
# Pipeline orchestrator
# ═══════════════════════════════════════════════════════════════════


class StreamingPipeline:
    """Orchestratore della pipeline multi-thread.

    Usage::

        pipeline = StreamingPipeline(config, relay)
        pipeline.start()
        ...
        pipeline.stop()
    """

    def __init__(
        self,
        config: PipelineConfig,
        send_frame_fn: Callable,
        send_tile_fn: Callable,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._frame_queue: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAX)
        self._pkt_queue: queue.Queue = queue.Queue(maxsize=_PKT_QUEUE_MAX)
        self._stop_event = threading.Event()

        # Stato condiviso fra i worker (lettura/scrittura atomica sotto GIL).
        # Il CaptureWorker modula l'fps sul change ratio che l'EncoderWorker
        # calcola comunque per i tile: nessun diff aggiuntivo.
        self._shared = {"change_ratio": 1.0}

        self._capture_worker = CaptureWorker(
            config,
            self._frame_queue,
            self._stop_event,
            on_error=on_error,
            shared=self._shared,
        )
        self._encoder_worker = EncoderWorker(
            config,
            self._frame_queue,
            self._pkt_queue,
            self._stop_event,
            on_send_full_keyframe=self._on_send_full_keyframe,
            on_send_tile=self._on_send_tile,
            on_error=on_error,
            shared=self._shared,
        )
        self._network_worker = NetworkWorker(
            self._pkt_queue,
            self._stop_event,
            send_frame_fn,
            send_tile_fn,
        )

        # Callback hooks (collegati da StreamService)
        self.on_keyframe_sent: Callable | None = None
        self.on_tile_sent: Callable | None = None

    def start(self) -> None:
        """Avvia tutti e 3 i worker thread."""
        logger.info("Starting streaming pipeline (3 threads)")
        self._stop_event.clear()
        self._capture_worker.start()
        self._encoder_worker.start()
        self._network_worker.start()

    def stop(self) -> None:
        """Arresta tutti i worker (con drain code)."""
        logger.info("Stopping streaming pipeline")
        self._stop_event.set()
        # Svuota le code per sbloccare i thread in attesa
        self._drain_queue(self._frame_queue)
        self._drain_queue(self._pkt_queue)
        # Invia sentinelle None per sbloccare i get()
        self._frame_queue.put((None, 0))
        self._pkt_queue.put(None)
        # Join con timeout
        for t in (self._capture_worker, self._encoder_worker, self._network_worker):
            t.join(timeout=3.0)
            if t.is_alive():
                logger.warning("Worker %s did not stop cleanly", t.name)
        logger.info("Streaming pipeline stopped")

    def request_keyframe(self) -> None:
        """Forza un keyframe al prossimo frame encode."""
        self._encoder_worker.request_keyframe()

    def update_config(self, config: PipelineConfig) -> None:
        """Aggiorna la configurazione a caldo."""
        self._config = config

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _on_send_full_keyframe(
        self, data: bytes, w: int, h: int, pts: int, is_keyframe: bool
    ) -> None:
        if self.on_keyframe_sent:
            self.on_keyframe_sent(data, w, h, pts, is_keyframe)

    def _on_send_tile(self, data: bytes, x: int, y: int, tw: int, th: int, pts: int) -> None:
        if self.on_tile_sent:
            self.on_tile_sent(data, x, y, tw, th, pts)
