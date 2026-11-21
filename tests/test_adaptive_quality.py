"""Boost qualità basato sulla CPU idle dell'host.

Verifica la scelta del tier (schermo fermo + carico CPU) e la scala dei
preset usata dall'``EncoderWorker``, oltre al sampler CPU stdlib.
"""

from __future__ import annotations

import pytest

from opendesk.services.pipeline import (
    _IDLE_TIER1_FRAMES,
    _IDLE_TIER2_FRAMES,
    PipelineConfig,
    _choose_quality_tier,
    _slower_preset,
)
from opendesk.utils import platform as platform_utils


def test_tier_zero_when_screen_moves() -> None:
    assert _choose_quality_tier(0, 0.0) == 0
    assert _choose_quality_tier(_IDLE_TIER1_FRAMES - 1, 0.0) == 0


def test_tier_escalates_when_idle_and_cpu_free() -> None:
    assert _choose_quality_tier(_IDLE_TIER1_FRAMES, 0.10) == 1
    assert _choose_quality_tier(_IDLE_TIER2_FRAMES, 0.10) == 2


def test_busy_cpu_suppresses_boost() -> None:
    assert _choose_quality_tier(_IDLE_TIER2_FRAMES, 0.80) == 0
    # Carico medio: niente tier massimo, ma il boost leggero resta utile
    assert _choose_quality_tier(_IDLE_TIER2_FRAMES, 0.50) == 1


def test_unknown_cpu_load_stays_conservative() -> None:
    assert _choose_quality_tier(_IDLE_TIER1_FRAMES, None) == 1
    assert _choose_quality_tier(_IDLE_TIER2_FRAMES, None) == 1


def test_preset_ladder_moves_slower_and_clamps() -> None:
    assert _slower_preset("superfast", 1) == "veryfast"
    assert _slower_preset("superfast", 2) == "fast"
    assert _slower_preset("veryfast", 2) == "medium"
    assert _slower_preset("veryslow", 2) == "veryslow"
    assert _slower_preset("unknown-preset", 1) == "faster"


def test_idle_quality_boost_enabled_by_default() -> None:
    assert PipelineConfig().idle_quality_boost is True


def test_cpu_load_uses_load_average(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_utils.sys, "platform", "linux")
    monkeypatch.setattr(platform_utils.os, "getloadavg", lambda: (1.6, 0.0, 0.0), raising=False)
    monkeypatch.setattr(platform_utils.os, "cpu_count", lambda: 8)
    # Forza un nuovo campione (la cache è di 1 s)
    monkeypatch.setattr(platform_utils, "_cpu_last_sample_mono", 0.0)
    monkeypatch.setattr(platform_utils, "_cpu_last_value", None)

    assert platform_utils.cpu_load_percent() == pytest.approx(0.2)


def test_cpu_load_returns_none_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_utils.sys, "platform", "linux")
    monkeypatch.setattr(platform_utils, "_cpu_last_sample_mono", 0.0)
    monkeypatch.setattr(platform_utils, "_cpu_last_value", None)

    def _boom() -> tuple[float, float, float]:
        raise OSError("no loadavg")

    monkeypatch.setattr(platform_utils.os, "getloadavg", _boom, raising=False)
    assert platform_utils.cpu_load_percent() is None


def test_cpu_hysteresis_prevents_boost_self_oscillation() -> None:
    """Regressione RCA 2026-11-21: il campione CPU include il costo del boost.

    Sequenza reale dal log: idle=4 -> tier 1 -> cpu 67% -> tier 0 -> cpu 52%
    -> tier 1 -> ... Con l'isteresi d'uscita il boost resta finché il carico
    non supera ``_CPU_TIER_EXIT``.
    """
    from opendesk.services.pipeline import _CPU_TIER_EXIT

    idle = _IDLE_TIER2_FRAMES
    # Ingresso dal tier 0: basta superare _CPU_FREE_TIER1
    assert _choose_quality_tier(idle, 0.80, 0) == 0
    assert _choose_quality_tier(idle, 0.59, 0) == 1
    # Già boostati: 0.67 (< _CPU_TIER_EXIT) non declassa, oltre sì
    assert _choose_quality_tier(idle, 0.67, 1) == 1
    assert _choose_quality_tier(idle, _CPU_TIER_EXIT, 1) == 0
    # Schermo che si muove declassa comunque subito
    assert _choose_quality_tier(0, 0.10, 2) == 0


def test_boost_warmup_constant_is_finite() -> None:
    from opendesk.services.pipeline import _BOOST_WARMUP_S

    assert 0 < _BOOST_WARMUP_S <= 30
