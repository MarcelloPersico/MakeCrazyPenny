"""Tests for the whole-market screen funnel (CONTRACT.md §10.5).

Deterministic and OFFLINE: the two fetch points (the cheap prefilter factors and
the full ``decide`` engine) plus the universe fetch and regime are monkeypatched,
so nothing touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.core.types import MarketScreen, TradeDecision
from makecrazypenny.orchestration import screen

# Per-symbol prefilter momentum (drives the cheap composite score) and the full
# decision the deep dive would return for that name.
_MOMENTUM = {"AAA": 0.30, "BBB": 0.15, "CCC": -0.15, "DDD": -0.30, "EEE": 0.0}


def _decision(symbol: str, action: str, net: float, conv: float) -> TradeDecision:
    direction = {"BUY": "LONG", "SHORT": "SHORT", "AVOID": "FLAT"}[action]
    return TradeDecision(
        symbol=symbol, action=action, direction=direction, conviction=conv,
        net_score=net, summary=f"{action} {symbol}", disclaimer="x",
    )


_DECISIONS = {
    "AAA": _decision("AAA", "BUY", 6.0, 0.9),
    "BBB": _decision("BBB", "BUY", 3.0, 0.5),
    "CCC": _decision("CCC", "SHORT", -4.0, 0.6),
    "DDD": _decision("DDD", "SHORT", -5.0, 0.8),
    "EEE": _decision("EEE", "AVOID", 0.1, 0.05),
}


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_prefilter(symbol: str, *, settings: Any = None) -> dict[str, Any]:
        return {"momentum_12_1": _MOMENTUM[symbol], "n_bars": 600, "realized_vol": 0.25}

    async def fake_decide(symbol: str, *, settings: Any = None) -> TradeDecision:
        return _DECISIONS[symbol]

    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None) -> dict[str, Any]:
        return {"regime": "risk_on", "gross_exposure": 1.0}

    monkeypatch.setattr(screen, "_prefilter_factors", fake_prefilter)
    monkeypatch.setattr(screen, "decide", fake_decide)
    monkeypatch.setattr(screen, "market_regime", fake_regime)


# ---------------------------------------------------------------------------
# prefilter_score (pure)
# ---------------------------------------------------------------------------


def test_prefilter_score_direction() -> None:
    assert screen.prefilter_score({"momentum_12_1": 0.30}) == 2.0  # clamped * weight
    assert screen.prefilter_score({"momentum_12_1": -0.30}) == -2.0
    assert screen.prefilter_score({}) is None  # nothing computable
    # Combines the available components.
    s = screen.prefilter_score({"momentum_12_1": 0.30, "trend_200": 0.10, "pct_52w_high": 1.0})
    assert s == pytest.approx(2.0 + 1.5 + 1.0)


# ---------------------------------------------------------------------------
# prefilter_universe
# ---------------------------------------------------------------------------


async def test_prefilter_universe_ranks(patched: None) -> None:
    ranked, errors = await screen.prefilter_universe(["BBB", "AAA", "DDD", "CCC"])
    assert [e["symbol"] for e in ranked] == ["AAA", "BBB", "CCC", "DDD"]  # most->least bullish
    assert errors == []


async def test_prefilter_universe_collects_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_prefilter(symbol: str, *, settings: Any = None) -> dict[str, Any]:
        if symbol == "BAD":
            return {"_error": "boom"}
        if symbol == "THIN":
            return {"n_bars": 5}  # no factors -> score None
        return {"momentum_12_1": 0.2}

    monkeypatch.setattr(screen, "_prefilter_factors", fake_prefilter)
    ranked, errors = await screen.prefilter_universe(["OK", "BAD", "THIN"])
    assert [e["symbol"] for e in ranked] == ["OK"]
    assert {e["symbol"] for e in errors} == {"BAD", "THIN"}


# ---------------------------------------------------------------------------
# screen_market end-to-end
# ---------------------------------------------------------------------------


async def test_screen_market_selects_top_longs_and_shorts(patched: None) -> None:
    out = await screen.screen_market(symbols=list(_MOMENTUM), shortlist=10, top_n=2)
    assert isinstance(out, MarketScreen)
    assert out.universe_source == "explicit"
    assert out.regime["regime"] == "risk_on"
    assert [d["symbol"] for d in out.top_longs] == ["AAA", "BBB"]
    assert [d["symbol"] for d in out.top_shorts] == ["DDD", "CCC"]  # most negative conviction-first
    # The AVOID name is deep-dived but surfaces on neither side.
    assert "EEE" not in {d["symbol"] for d in out.top_longs + out.top_shorts}
    assert out.disclaimer
    # Each idea carries the full how-to-trade decision shape.
    assert out.top_longs[0]["direction"] == "LONG"
    assert out.top_shorts[0]["direction"] == "SHORT"


async def test_screen_market_top_n_caps_each_side(patched: None) -> None:
    out = await screen.screen_market(symbols=list(_MOMENTUM), shortlist=10, top_n=1)
    assert [d["symbol"] for d in out.top_longs] == ["AAA"]
    assert [d["symbol"] for d in out.top_shorts] == ["DDD"]


async def test_screen_market_shortlist_limits_deep_dive(monkeypatch: pytest.MonkeyPatch, patched: None) -> None:
    deep: list[str] = []

    async def tracking_decide(symbol: str, *, settings: Any = None) -> TradeDecision:
        deep.append(symbol)
        return _DECISIONS[symbol]

    monkeypatch.setattr(screen, "decide", tracking_decide)
    # shortlist=1 per side -> only the strongest long (AAA) and strongest short (DDD).
    out = await screen.screen_market(symbols=list(_MOMENTUM), shortlist=1, top_n=3)
    assert set(deep) == {"AAA", "DDD"}
    assert out.n_evaluated == 2


async def test_screen_market_uses_live_universe(monkeypatch: pytest.MonkeyPatch, patched: None) -> None:
    async def fake_fetch(*, settings: Any = None, force_refresh: bool = False) -> dict[str, Any]:
        return {"symbols": ["AAA", "DDD"], "source": "live", "count": 503, "as_of": "2026-06-09T00:00:00+00:00"}

    monkeypatch.setattr(screen, "fetch_sp500", fake_fetch)
    out = await screen.screen_market(shortlist=10, top_n=3)
    assert out.universe_source == "live"
    assert out.universe_count == 503
    assert out.as_of == "2026-06-09T00:00:00+00:00"
    assert [d["symbol"] for d in out.top_longs] == ["AAA"]


async def test_screen_market_tolerates_deep_dive_failure(monkeypatch: pytest.MonkeyPatch, patched: None) -> None:
    async def flaky_decide(symbol: str, *, settings: Any = None) -> TradeDecision:
        if symbol == "AAA":
            raise RuntimeError("provider down")
        return _DECISIONS[symbol]

    monkeypatch.setattr(screen, "decide", flaky_decide)
    out = await screen.screen_market(symbols=list(_MOMENTUM), shortlist=10, top_n=3)
    assert "AAA" not in {d["symbol"] for d in out.top_longs}  # failed name dropped
    assert any(e["symbol"] == "AAA" for e in out.errors)


async def test_screen_market_empty_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_fetch(*, settings: Any = None, force_refresh: bool = False) -> dict[str, Any]:
        return {"symbols": [], "source": "fallback", "count": 0, "as_of": None}

    monkeypatch.setattr(screen, "fetch_sp500", empty_fetch)
    out = await screen.screen_market()
    assert out.n_evaluated == 0
    assert out.errors and out.errors[0]["error"] == "empty universe"


def test_market_screen_to_dict_round_trips() -> None:
    import json

    out = MarketScreen(universe="S&P 500", universe_source="live", top_longs=[{"symbol": "AAA"}], disclaimer="x")
    d = out.to_dict()
    json.dumps(d)
    for key in ("universe", "universe_source", "top_longs", "top_shorts", "regime", "errors", "disclaimer"):
        assert key in d


# ---------------------------------------------------------------------------
# CLI (--market)
# ---------------------------------------------------------------------------


def test_format_screen_is_ascii_safe() -> None:
    from makecrazypenny.orchestration import main

    s = MarketScreen(
        universe="S&P 500", universe_source="live", universe_count=503,
        n_prefiltered=480, n_evaluated=20, regime={"regime": "risk_on", "gross_exposure": 1.0},
        top_longs=[_DECISIONS["AAA"].to_dict()], top_shorts=[_DECISIONS["DDD"].to_dict()],
        summary="ok", disclaimer="x",
    )
    text = main._format_screen(s.to_dict())
    text.encode("ascii")  # raises if a non-ASCII char slipped into the template
    assert "MARKET SCREEN" in text and "TOP LONGS" in text and "TOP SHORTS" in text


def test_cli_market_routes_to_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.orchestration import main

    captured: dict[str, Any] = {}

    async def fake_screen(*, shortlist=15, top_n=3, **_: Any) -> MarketScreen:
        captured["shortlist"] = shortlist
        captured["top_n"] = top_n
        return MarketScreen(universe="S&P 500", universe_source="live", summary="ok", disclaimer="x")

    monkeypatch.setattr(main, "run_market_screen", fake_screen)
    rc = main.cli(["--market", "--shortlist", "8", "--top", "2"])
    assert rc == main.EXIT_OK
    assert captured == {"shortlist": 8, "top_n": 2}


def test_cli_market_default_top_is_three(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.orchestration import main

    captured: dict[str, Any] = {}

    async def fake_screen(*, shortlist=15, top_n=3, **_: Any) -> MarketScreen:
        captured["top_n"] = top_n
        return MarketScreen(universe="S&P 500", universe_source="live", summary="ok", disclaimer="x")

    monkeypatch.setattr(main, "run_market_screen", fake_screen)
    main.cli(["--market"])
    assert captured["top_n"] == 3  # market mode defaults to the best 3 per side
