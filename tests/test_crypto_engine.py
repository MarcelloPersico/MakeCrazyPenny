"""Tests for the crypto decision engine (CONTRACT.md §16). Offline (evidence monkeypatched)."""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.orchestration import crypto as engine


def _bullish_dossier() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "interval": "15m",
        "signals": {"signals": [
            {"name": "golden_cross", "direction": "bullish"},
            {"name": "macd_bullish_cross", "direction": "bullish"},
        ]},
        "factors": {"momentum_12_1": 0.20, "trend_200": 0.08, "pct_52w_high": 0.98, "last_close": 42000.0, "atr14": 300.0},
        "mtf": {"timeframes": {"5m": {"trend": "bullish"}, "15m": {"trend": "bullish"}, "1h": {"trend": "bullish"}}},
        "derivatives": {
            "funding": {"rate": -0.0002, "annualized": -0.20, "interval_hours": 8.0},
            "oi_change_pct": 0.05, "price_change_pct": 0.02,
            "long_short": {"ratio": 0.5},
        },
        "sentiment": {"fear_greed": {"value": 20}},
    }


def _bearish_dossier() -> dict[str, Any]:
    d = _bullish_dossier()
    d["signals"] = {"signals": [{"name": "death_cross", "direction": "bearish"}]}
    d["factors"] = {"momentum_12_1": -0.20, "trend_200": -0.08, "pct_52w_high": 0.70, "last_close": 42000.0, "atr14": 300.0}
    d["mtf"] = {"timeframes": {"5m": {"trend": "bearish"}, "15m": {"trend": "bearish"}, "1h": {"trend": "bearish"}}}
    d["derivatives"] = {"funding": {"rate": 0.0006, "annualized": 0.66}, "oi_change_pct": 0.05, "price_change_pct": -0.02, "long_short": {"ratio": 3.0}}
    d["sentiment"] = {"fear_greed": {"value": 88}}
    return d


def test_score_crypto_evidence_directions() -> None:
    bull = engine.score_crypto_evidence(_bullish_dossier())
    bear = engine.score_crypto_evidence(_bearish_dossier())
    assert bull["net_score"] > 0 > bear["net_score"]
    # Crypto-specific categories are present.
    assert {"funding", "open_interest", "positioning", "sentiment"} <= set(bull["categories"])
    assert bull["n_factors"] >= 6


def test_score_thin_dossier_is_near_zero() -> None:
    scored = engine.score_crypto_evidence({"symbol": "BTCUSDT"})
    assert scored["n_factors"] == 0
    assert scored["net_score"] == 0.0


async def test_gather_is_tolerant(monkeypatch: pytest.MonkeyPatch) -> None:
    import makecrazypenny.servers.crypto as cx

    async def ok_mtf(symbol: str) -> dict[str, Any]:
        raise RuntimeError("boom")

    async def ok_signals(symbol: str, interval: str = "15m") -> dict[str, Any]:
        return {"signals": []}

    async def ok_deriv(symbol: str, interval: str = "5m") -> dict[str, Any]:
        return {"funding": {"rate": 0.0001}}

    async def ok_sent() -> dict[str, Any]:
        return {"fear_greed": {"value": 50}}

    async def ok_ohlcv(symbol: str, interval: str = "5m", limit: int = 500) -> dict[str, Any]:
        return {"bars": []}

    monkeypatch.setattr(cx, "multi_timeframe", ok_mtf)
    monkeypatch.setattr(cx, "crypto_signals", ok_signals)
    monkeypatch.setattr(cx, "derivatives", ok_deriv)
    monkeypatch.setattr(cx, "crypto_sentiment", ok_sent)
    monkeypatch.setattr(cx, "crypto_ohlcv", ok_ohlcv)

    dossier = await engine.gather_crypto_evidence("btc", interval="15m")
    assert "_error" in dossier["mtf"]  # the failing one is captured, not raised
    assert dossier["signals"] == {"signals": []}


async def test_decide_crypto_attaches_leverage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, interval: str = "15m", settings: Any = None) -> dict[str, Any]:
        return _bullish_dossier()

    async def fake_regime(*, settings: Any = None) -> dict[str, Any]:
        return {"regime": "risk_on", "gross_exposure": 0.9, "above_trend": True}

    monkeypatch.setattr(engine, "gather_crypto_evidence", fake_gather)
    monkeypatch.setattr(engine, "crypto_regime", fake_regime)

    decision = await engine.decide_crypto("BTC", interval="15m", leverage_cap=20.0)
    d = decision.to_dict()
    assert d["asset_class"] == "crypto"
    assert d["action"] == "BUY" and d["direction"] == "LONG"
    lev = d["leverage"]
    assert 0 < lev["suggested_leverage"] <= 20.0
    assert lev["liquidation_price"] < lev["stop_price"] < lev["entry_price"]
    assert d["regime"]["regime"] == "risk_on"
    assert d["horizon"] == "intraday"  # 15m -> intraday
    assert d["disclaimer"]


async def test_decide_crypto_scalp_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, interval: str = "5m", settings: Any = None) -> dict[str, Any]:
        return _bullish_dossier()

    async def fake_regime(*, settings: Any = None) -> dict[str, Any]:
        return {"regime": "risk_on", "gross_exposure": 1.0}

    monkeypatch.setattr(engine, "gather_crypto_evidence", fake_gather)
    monkeypatch.setattr(engine, "crypto_regime", fake_regime)
    decision = await engine.decide_crypto("ETH", interval="5m")
    assert decision.horizon == "scalp"
