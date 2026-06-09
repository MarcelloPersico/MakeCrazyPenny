"""Tests for the crypto screen funnel (CONTRACT.md §16). Offline (fetch monkeypatched)."""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.core.types import TradeDecision
from makecrazypenny.orchestration import crypto_screen as cs


def _decision(symbol: str, action: str, conviction: float, net: float) -> TradeDecision:
    direction = {"BUY": "LONG", "SHORT": "SHORT", "AVOID": "FLAT"}[action]
    return TradeDecision(
        symbol=symbol, action=action, direction=direction, conviction=conviction,
        net_score=net, asset_class="crypto", leverage={"suggested_leverage": 10.0, "direction": direction},
    )


async def test_prefilter_universe_ranks(monkeypatch: pytest.MonkeyPatch) -> None:
    factors = {
        "AUSDT": {"momentum_12_1": 0.20, "trend_200": 0.05, "pct_52w_high": 0.98},
        "BUSDT": {"momentum_12_1": -0.20, "trend_200": -0.05, "pct_52w_high": 0.70},
    }

    async def fake_prefilter(symbol: str, interval: str) -> dict[str, Any]:
        return factors[symbol]

    monkeypatch.setattr(cs, "_prefilter_factors", fake_prefilter)
    ranked, errors = await cs.prefilter_universe(["AUSDT", "BUSDT"], interval="15m")
    assert [e["symbol"] for e in ranked] == ["AUSDT", "BUSDT"]  # most bullish first
    assert ranked[0]["score"] > 0 > ranked[1]["score"]
    assert errors == []


async def test_screen_crypto_selects_longs_and_shorts(monkeypatch: pytest.MonkeyPatch) -> None:
    universe = ["AUSDT", "BUSDT", "CUSDT", "DUSDT"]
    scores = {"AUSDT": 0.20, "BUSDT": 0.10, "CUSDT": -0.10, "DUSDT": -0.20}

    async def fake_top(*, settings: Any = None, limit: int = 40, force_refresh: bool = False) -> dict[str, Any]:
        return {"symbols": universe, "count": len(universe), "source": "live", "as_of": None}

    async def fake_prefilter(symbol: str, interval: str) -> dict[str, Any]:
        return {"momentum_12_1": scores[symbol], "trend_200": scores[symbol], "pct_52w_high": 0.9}

    async def fake_regime(*, settings: Any = None) -> dict[str, Any]:
        return {"regime": "risk_on", "gross_exposure": 1.0}

    verdicts = {
        "AUSDT": _decision("AUSDT", "BUY", 0.5, 3.0),
        "BUSDT": _decision("BUSDT", "BUY", 0.4, 2.0),
        "CUSDT": _decision("CUSDT", "SHORT", 0.5, -3.0),
        "DUSDT": _decision("DUSDT", "SHORT", 0.3, -2.0),
    }

    async def fake_decide(symbol: str, *, interval: str = "15m", leverage_cap: Any = None, settings: Any = None) -> TradeDecision:
        return verdicts[symbol]

    monkeypatch.setattr(cs, "fetch_top_perps", fake_top)
    monkeypatch.setattr(cs, "_prefilter_factors", fake_prefilter)
    monkeypatch.setattr(cs, "crypto_regime", fake_regime)
    monkeypatch.setattr(cs, "decide_crypto", fake_decide)

    screen = await cs.screen_crypto(interval="15m", shortlist=10, top_n=3)
    d = screen.to_dict()
    assert d["universe"] == "Crypto perps"
    assert [x["symbol"] for x in d["top_longs"]] == ["AUSDT", "BUSDT"]  # ranked by conviction
    assert [x["symbol"] for x in d["top_shorts"]] == ["CUSDT", "DUSDT"]
    assert d["top_longs"][0]["leverage"]["suggested_leverage"] == 10.0
    assert d["regime"]["regime"] == "risk_on"


async def test_screen_crypto_empty_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_top(*, settings: Any = None, limit: int = 40, force_refresh: bool = False) -> dict[str, Any]:
        return {"symbols": [], "count": 0, "source": "fallback", "as_of": None}

    monkeypatch.setattr(cs, "fetch_top_perps", fake_top)
    screen = await cs.screen_crypto()
    assert screen.errors and screen.errors[0]["error"] == "empty universe"


async def test_screen_crypto_tolerates_deep_dive_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_top(*, settings: Any = None, limit: int = 40, force_refresh: bool = False) -> dict[str, Any]:
        return {"symbols": ["AUSDT", "BUSDT"], "count": 2, "source": "live", "as_of": None}

    async def fake_prefilter(symbol: str, interval: str) -> dict[str, Any]:
        return {"momentum_12_1": 0.2, "trend_200": 0.05, "pct_52w_high": 0.95}

    async def fake_regime(*, settings: Any = None) -> dict[str, Any]:
        return {"regime": "caution", "gross_exposure": 0.6}

    async def fake_decide(symbol: str, *, interval: str = "15m", leverage_cap: Any = None, settings: Any = None) -> TradeDecision:
        if symbol == "BUSDT":
            raise RuntimeError("data gap")
        return _decision("AUSDT", "BUY", 0.5, 3.0)

    monkeypatch.setattr(cs, "fetch_top_perps", fake_top)
    monkeypatch.setattr(cs, "_prefilter_factors", fake_prefilter)
    monkeypatch.setattr(cs, "crypto_regime", fake_regime)
    monkeypatch.setattr(cs, "decide_crypto", fake_decide)

    screen = await cs.screen_crypto(interval="15m")
    d = screen.to_dict()
    assert any(e.get("symbol") == "BUSDT" for e in d["errors"])
    assert [x["symbol"] for x in d["top_longs"]] == ["AUSDT"]
