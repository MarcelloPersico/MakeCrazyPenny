"""Tests for the sector universe + sector-scan engine (CONTRACT.md §10.5, §12).

Deterministic and OFFLINE: evidence gathering is monkeypatched so nothing touches
the network. The autouse ``hermetic_env`` fixture (conftest.py) keeps real keys out.
"""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.core import sectors
from makecrazypenny.core.types import SectorScan, TradeDecision
from makecrazypenny.orchestration import market

BULLISH_DOSSIER: dict[str, Any] = {
    "signals": {
        "signals": [
            {"name": "golden_cross", "direction": "bullish"},
            {"name": "macd_bullish_cross", "direction": "bullish"},
        ]
    },
    "sentiment": {"score": 0.5, "label": "positive"},
    "ratings": {"ratings": [{"strong_buy": 7, "buy": 5, "hold": 2, "sell": 0, "strong_sell": 0}]},
    "price_targets": {"targets": {"mean": 120.0, "current": 100.0}},
    "congress": {"trades": [{"transaction": "Purchase"}]},
    "insider": {"transactions": []},
    "cross_check": {"divergence": {"score": 0.1}},
}


# ---------------------------------------------------------------------------
# Sector universe + resolver
# ---------------------------------------------------------------------------


def test_list_sectors_has_eleven_gics() -> None:
    names = sectors.list_sectors()
    assert len(names) == 11
    assert "Technology" in names and "Health Care" in names and "Real Estate" in names


def test_resolve_sector_aliases_and_case() -> None:
    assert sectors.resolve_sector("tech") == "Technology"
    assert sectors.resolve_sector("TECHNOLOGY") == "Technology"
    assert sectors.resolve_sector("healthcare") == "Health Care"
    assert sectors.resolve_sector("reits") == "Real Estate"
    assert sectors.resolve_sector("  Energy ") == "Energy"


def test_resolve_sector_substring_and_unknown() -> None:
    assert sectors.resolve_sector("real") == "Real Estate"  # unique substring
    assert sectors.resolve_sector("nonsense") is None
    assert sectors.resolve_sector("") is None


def test_sector_constituents() -> None:
    tech = sectors.sector_constituents("tech")
    assert "AAPL" in tech and "NVDA" in tech
    assert sectors.sector_constituents("nope") == []


# ---------------------------------------------------------------------------
# aggregate_scan (pure)
# ---------------------------------------------------------------------------


def _decision(symbol: str, action: str, net: float, conv: float) -> TradeDecision:
    direction = {"BUY": "LONG", "SHORT": "SHORT", "AVOID": "FLAT"}[action]
    return TradeDecision(
        symbol=symbol,
        action=action,
        direction=direction,
        conviction=conv,
        net_score=net,
        summary=f"{action} {symbol}",
        data_quality={"n_factors": 4},
        disclaimer="x",
    )


def test_aggregate_scan_ranks_and_classifies() -> None:
    decisions = [
        _decision("AAA", "BUY", 6.0, 0.9),
        _decision("BBB", "BUY", 3.0, 0.5),
        _decision("CCC", "AVOID", 0.2, 0.05),
        _decision("DDD", "SHORT", -4.0, 0.7),
    ]
    scan = market.aggregate_scan("Technology", decisions, [], n_requested=4, top_n=2)
    assert isinstance(scan, SectorScan)
    assert scan.n_analyzed == 4
    # rankings sorted most -> least bullish
    assert [r["symbol"] for r in scan.rankings] == ["AAA", "BBB", "CCC", "DDD"]
    assert scan.breadth == {
        "buy": 2,
        "short": 1,
        "avoid": 1,
        "bullish_pct": 0.5,
        "bearish_pct": 0.25,
    }
    assert [r["symbol"] for r in scan.top_longs] == ["AAA", "BBB"]
    assert scan.top_shorts[0]["symbol"] == "DDD"
    assert scan.disclaimer  # carried


def test_aggregate_scan_stance_overweight_and_underweight() -> None:
    bullish = [_decision(s, "BUY", 5.0, 0.8) for s in ("A", "B", "C")]
    over = market.aggregate_scan("Energy", bullish, [], n_requested=3)
    assert over.stance == "overweight"
    assert over.net_tilt == 5.0

    bearish = [_decision(s, "SHORT", -5.0, 0.8) for s in ("A", "B", "C")]
    under = market.aggregate_scan("Energy", bearish, [], n_requested=3)
    assert under.stance == "underweight"


def test_aggregate_scan_empty_is_neutral() -> None:
    scan = market.aggregate_scan("Materials", [], [{"symbol": "X", "error": "boom"}], n_requested=1)
    assert scan.n_analyzed == 0
    assert scan.stance == "neutral"
    assert scan.net_tilt == 0.0


# ---------------------------------------------------------------------------
# scan_sector (engine; evidence monkeypatched)
# ---------------------------------------------------------------------------


async def test_scan_sector_all_bullish(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        return {**BULLISH_DOSSIER, "symbol": symbol}

    monkeypatch.setattr(market, "gather_evidence", fake_gather)
    scan = await market.scan_sector("tech", limit=4)
    assert scan.sector == "Technology"
    assert scan.n_requested == 4
    assert scan.n_analyzed == 4
    assert scan.breadth["buy"] == 4
    assert scan.stance == "overweight"
    assert scan.top_longs  # has long ideas
    assert scan.method == "quant"


async def test_scan_sector_unknown_returns_error_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    scan = await market.scan_sector("not-a-sector")
    assert scan.n_analyzed == 0
    assert scan.errors and "unknown sector" in scan.errors[0]["error"]


async def test_scan_sector_limit_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_gather(symbol: str, *, settings: Any = None):
        seen.append(symbol)
        return {**BULLISH_DOSSIER, "symbol": symbol}

    monkeypatch.setattr(market, "gather_evidence", fake_gather)
    scan = await market.scan_sector("Health Care", limit=3)
    assert scan.n_requested == 3
    assert len(seen) == 3


async def test_scan_sector_tolerates_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        if symbol == "NVDA":
            raise RuntimeError("provider down")
        return {**BULLISH_DOSSIER, "symbol": symbol}

    monkeypatch.setattr(market, "gather_evidence", fake_gather)
    scan = await market.scan_sector("tech", limit=5)
    assert scan.n_analyzed == 4  # 5 requested, NVDA failed
    assert any(e["symbol"] == "NVDA" for e in scan.errors)


def test_sector_scan_to_dict_round_trips() -> None:
    scan = market.aggregate_scan(
        "Technology", [_decision("AAA", "BUY", 6.0, 0.9)], [], n_requested=1
    )
    import json

    d = scan.to_dict()
    json.dumps(d)
    for key in ("sector", "stance", "breadth", "rankings", "top_longs", "top_shorts", "disclaimer"):
        assert key in d
