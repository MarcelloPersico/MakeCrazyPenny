"""Tests for portfolio construction (CONTRACT.md §10.6). Offline/deterministic."""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.orchestration import debate, portfolio

BULLISH: dict[str, Any] = {
    "signals": {
        "signals": [
            {"name": "golden_cross", "direction": "bullish"},
            {"name": "macd_bullish_cross", "direction": "bullish"},
        ]
    },
    "ratings": {"ratings": [{"strong_buy": 7, "buy": 5, "hold": 2, "sell": 0, "strong_sell": 0}]},
    "price_targets": {"targets": {"mean": 120.0, "current": 100.0}},
    "congress": {"trades": [{"transaction": "Purchase"}]},
}


def test_weight_side_caps_and_normalizes() -> None:
    rows = [
        {"symbol": "A", "conviction": 0.9, "realized_vol": 0.2},
        {"symbol": "B", "conviction": 0.5, "realized_vol": 0.2},
        {"symbol": "C", "conviction": 0.1, "realized_vol": 0.2},
    ]
    out = portfolio._weight_side(rows, max_weight=0.5)
    assert abs(sum(x["weight"] for x in out) - 1.0) < 1e-3
    assert all(x["weight"] <= 0.5 + 1e-9 for x in out)
    # Higher conviction -> higher weight (equal vol).
    w = {x["symbol"]: x["weight"] for x in out}
    assert w["A"] > w["B"] > w["C"]


def test_weight_side_inverse_vol() -> None:
    rows = [
        {"symbol": "LOWVOL", "conviction": 0.6, "realized_vol": 0.1},
        {"symbol": "HIVOL", "conviction": 0.6, "realized_vol": 0.4},
    ]
    w = {x["symbol"]: x["weight"] for x in portfolio._weight_side(rows, 0.9)}
    assert w["LOWVOL"] > w["HIVOL"]  # inverse-vol tilts to the calmer name


async def test_build_portfolio_weights_and_regime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        vol = 0.2 if symbol == "AAA" else 0.4
        return {**BULLISH, "factors": {"realized_vol": vol}}

    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None):
        return {"regime": "risk_on", "gross_exposure": 1.0}

    monkeypatch.setattr(portfolio, "gather_evidence", fake_gather)
    monkeypatch.setattr(debate, "market_regime", fake_regime)

    result = await portfolio.build_portfolio(["AAA", "BBB", "CCC"], max_positions=10, max_weight=0.6)
    assert result["n_analyzed"] == 3
    assert len(result["longs"]) == 3 and result["shorts"] == []
    assert abs(sum(x["weight"] for x in result["longs"]) - 1.0) < 1e-3
    w = {x["symbol"]: x["weight"] for x in result["longs"]}
    assert w["AAA"] > w["BBB"]  # lower vol -> higher weight
    assert result["gross_exposure"] == 1.0
    assert result["disclaimer"]


async def test_build_portfolio_regime_scales_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        return {**BULLISH, "factors": {"realized_vol": 0.25}}

    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None):
        return {"regime": "risk_off", "gross_exposure": 0.3}

    monkeypatch.setattr(portfolio, "gather_evidence", fake_gather)
    monkeypatch.setattr(debate, "market_regime", fake_regime)

    result = await portfolio.build_portfolio(["AAA", "BBB"])
    assert result["gross_exposure"] == 0.3
    # weights sum to 1 pre-scale; long_exposure = gross x sum(weights) = 0.3
    assert abs(result["long_exposure"] - 0.3) < 1e-3


async def test_build_sector_portfolio_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await portfolio.build_sector_portfolio("not-a-sector")
    assert result["longs"] == []
    assert result["errors"]
