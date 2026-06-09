"""Tests for the crypto capability server (CONTRACT.md §16). Offline (fake registry)."""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.servers import crypto as cx


class FakeRegistry:
    """A registry whose ``fetch`` returns canned crypto data (or raises per-cap)."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()

    async def fetch(self, capability: str, **params: Any) -> dict[str, Any]:
        if capability in self.fail:
            raise RuntimeError(f"{capability} unavailable")
        sym = params.get("symbol", "BTCUSDT")
        if capability == "crypto_ohlcv":
            n = min(params.get("limit", 500), 60)
            bars = [
                {"ts": f"2024-01-01T00:{i:02d}:00+00:00", "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10}
                for i in range(n)
            ]
            return {"provider": "binance", "cached": False, "data": {"symbol": sym, "interval": params.get("interval"), "bars": bars}}
        if capability == "funding_rate":
            return {"provider": "binance", "data": {"symbol": sym, "rate": 0.0005, "annualized": 0.5475, "mark_price": 50010, "index_price": 50000, "basis": 0.0002, "interval_hours": 8.0}}
        if capability == "open_interest":
            return {"provider": "binance", "data": {"symbol": sym, "open_interest": 1100, "history": [{"open_interest": 1000}, {"open_interest": 1100}]}}
        if capability == "long_short_ratio":
            return {"provider": "binance", "data": {"symbol": sym, "ratio": 2.5, "long_pct": 0.71, "short_pct": 0.29}}
        if capability == "crypto_sentiment":
            return {"provider": "fear_greed", "data": {"symbol": "CRYPTO", "score": 0.4, "label": "greed", "value": 70}}
        if capability == "crypto_global":
            return {"provider": "coingecko", "data": {"total_market_cap": 2.3e12, "btc_dominance": 52.1}}
        raise RuntimeError(f"unexpected capability {capability}")


def _use(monkeypatch: pytest.MonkeyPatch, reg: FakeRegistry) -> None:
    monkeypatch.setattr(cx, "get_registry", lambda: reg)


async def test_derivatives_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeRegistry())
    d = await cx.derivatives("btc", interval="5m")
    assert d["symbol"] == "BTCUSDT"
    assert d["funding"]["rate"] == 0.0005
    assert d["basis"] == 0.0002
    assert d["oi_change_pct"] == pytest.approx(0.1, rel=1e-6)
    assert d["price_change_pct"] is not None
    assert d["long_short"]["ratio"] == 2.5


async def test_derivatives_tolerates_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeRegistry(fail={"long_short_ratio"}))
    d = await cx.derivatives("BTC")
    assert "_error" in d["long_short"]  # the failed one is a marker, not a raise
    assert d["funding"]["rate"] == 0.0005  # the rest still populate


async def test_multi_timeframe_and_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeRegistry())
    mtf = await cx.multi_timeframe("ETH")
    assert set(mtf["timeframes"]) == {"5m", "15m", "1h"}
    assert mtf["timeframes"]["5m"]["trend"] == "bullish"
    sig = await cx.crypto_signals("ETH", interval="15m")
    assert "signals" in sig and "values" in sig


async def test_crypto_sentiment(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeRegistry())
    cs = await cx.crypto_sentiment()
    assert cs["fear_greed"]["value"] == 70
    assert cs["global"]["btc_dominance"] == 52.1


def test_server_and_tools_exist() -> None:
    # The server object is created (a real SDK server or the shim descriptor), and
    # every tool wrapper is present and callable. Robust across SDK presence.
    assert cx.server is not None
    for name in (
        "crypto_ohlcv_tool", "crypto_indicators_tool", "crypto_signals_tool",
        "multi_timeframe_tool", "derivatives_tool", "crypto_sentiment_tool",
    ):
        assert callable(getattr(cx, name))
