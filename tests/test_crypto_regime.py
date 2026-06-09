"""Tests for the crypto market-regime core (CONTRACT.md §16). Pure/offline."""

from __future__ import annotations

from makecrazypenny.analysis.crypto_regime import crypto_regime_from_bars


def _bars(closes: list[float]) -> list[dict[str, float]]:
    return [{"close": c, "high": c + 1, "low": c - 1, "open": c} for c in closes]


def test_uptrend_is_risk_on() -> None:
    bars = _bars([100 + i * 0.5 for i in range(300)])
    reg = crypto_regime_from_bars(bars, target_vol=5.0)  # high target_vol => no vol clipping
    assert reg["regime"] == "risk_on"
    assert reg["above_trend"] is True
    assert 0.0 < reg["gross_exposure"] <= 1.0


def test_downtrend_is_risk_off() -> None:
    bars = _bars([300 - i * 0.5 for i in range(300)])
    reg = crypto_regime_from_bars(bars, target_vol=5.0)
    assert reg["regime"] == "risk_off"
    assert reg["above_trend"] is False
    assert reg["gross_exposure"] <= 0.3


def test_fear_greed_overlay_trims_gross() -> None:
    bars = _bars([100 + i * 0.5 for i in range(300)])
    base = crypto_regime_from_bars(bars, target_vol=5.0, fng_value=50)
    greedy = crypto_regime_from_bars(bars, target_vol=5.0, fng_value=90)
    fearful = crypto_regime_from_bars(bars, target_vol=5.0, fng_value=10)
    assert greedy["fng_scale"] == 0.85
    assert fearful["fng_scale"] == 0.85
    assert greedy["gross_exposure"] < base["gross_exposure"]


def test_high_vol_overlay_reduces_gross() -> None:
    # Choppy series => high realized vol => vol_scale < 1 trims gross.
    bars = _bars([100 + (30 if i % 2 else -30) + i * 0.4 for i in range(300)])
    reg = crypto_regime_from_bars(bars, target_vol=0.20)
    assert reg["vol_scale"] < 1.0


def test_insufficient_history_defaults_caution() -> None:
    reg = crypto_regime_from_bars(_bars([100, 101, 102]))
    assert reg["regime"] == "caution"
