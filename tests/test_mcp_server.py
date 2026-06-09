"""Tests for the FastMCP server (CONTRACT.md §10.4).

Deterministic and OFFLINE: the tools are AI-free, and evidence gathering is
monkeypatched so nothing touches the network. The autouse ``hermetic_env``
fixture (conftest.py) keeps real keys out.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from makecrazypenny import mcp_server as srv

BULLISH_DOSSIER: dict[str, Any] = {
    "symbol": "AAPL",
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
# Registration + prompts
# ---------------------------------------------------------------------------


async def test_tools_and_prompts_registered() -> None:
    tools = {t.name for t in await srv.mcp.list_tools()}
    prompts = {p.name for p in await srv.mcp.list_prompts()}
    assert {"decide", "gather_evidence", "finalize_decision", "technical_analysis"} <= tools
    assert {"list_sectors", "sector_constituents", "scan_sector"} <= tools
    assert {"market_regime", "backtest", "build_portfolio", "build_sector_portfolio"} <= tools
    assert {"decide", "bull_case", "bear_case", "judge", "decide_sector"} == prompts


def test_prompt_builders_normalize_symbol_and_mention_flow() -> None:
    decide = srv.build_decide_prompt("$aapl", 3)
    assert "AAPL" in decide
    assert "BULL" in decide and "BEAR" in decide and "JUDGE" in decide
    assert "finalize_decision" in decide
    assert "3 round" in decide  # rounds threaded in
    assert "NOT investment advice" in decide

    assert "AAPL" in srv.build_bull_prompt("aapl")
    assert "AAPL" in srv.build_bear_prompt("aapl")
    assert "AAPL" in srv.build_judge_prompt("aapl")


def test_decide_prompt_handles_bad_rounds() -> None:
    # Non-integer rounds must not raise; falls back to a sane default.
    text = srv.decide_prompt("AAPL", rounds="not-a-number")
    assert "AAPL" in text


# ---------------------------------------------------------------------------
# Tools (deterministic; evidence gathering monkeypatched)
# ---------------------------------------------------------------------------


async def test_decide_tool_returns_quant_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.orchestration import debate

    async def fake_gather(symbol: str, *, settings: Any = None):
        return {**BULLISH_DOSSIER, "factors": {"last_close": 100.0, "atr14": 2.0, "realized_vol": 0.25}}

    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None):
        return {"regime": "risk_on", "gross_exposure": 1.0}

    # decide_tool delegates to debate.decide, which uses these names.
    monkeypatch.setattr(debate, "gather_evidence", fake_gather)
    monkeypatch.setattr(debate, "market_regime", fake_regime)
    out = json.loads(await srv.decide_tool("$aapl"))
    assert out["symbol"] == "AAPL"
    assert out["action"] == "BUY"
    assert out["method"] == "quant"
    assert out["sizing"]["position_pct"] > 0
    assert out["regime"]["regime"] == "risk_on"
    assert out["disclaimer"]


async def test_gather_evidence_tool_returns_dossier_and_quant(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        return BULLISH_DOSSIER

    monkeypatch.setattr(srv, "gather_evidence", fake_gather)
    out = json.loads(await srv.gather_evidence_tool("AAPL"))
    assert out["symbol"] == "AAPL"
    assert "dossier" in out and "quant" in out
    assert out["quant"]["net_score"] > 0


async def test_finalize_decision_tool_applies_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.orchestration import debate

    async def fake_gather(symbol: str, *, settings: Any = None):
        return BULLISH_DOSSIER

    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None):
        return {"regime": "caution", "gross_exposure": 0.6}

    monkeypatch.setattr(srv, "gather_evidence", fake_gather)
    monkeypatch.setattr(debate, "market_regime", fake_regime)
    out = json.loads(
        await srv.finalize_decision_tool(
            "AAPL",
            action="AVOID",
            conviction=0.2,
            summary="Too rich",
            rationale=["valuation"],
            risks=["earnings"],
            invalidation="break 110",
        )
    )
    assert out["action"] == "AVOID"  # host verdict overrides the bullish quant
    assert out["direction"] == "FLAT"
    assert out["conviction"] == 0.2
    assert out["method"] == "debate"
    assert out["summary"] == "Too rich"
    # Quant scores preserved for transparency.
    assert out["net_score"] > 0


async def test_technical_analysis_tool_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.servers import technical

    async def ok(value: Any):
        return value

    monkeypatch.setattr(technical, "detect_signals", lambda s: ok({"signals": []}))
    monkeypatch.setattr(technical, "compute_indicators", lambda s: ok({"indicators": {}}))
    monkeypatch.setattr(technical, "multi_timeframe_summary", lambda s: ok({"mtf": True}))

    out = json.loads(await srv.technical_analysis_tool("aapl"))
    assert out["symbol"] == "AAPL"
    assert "signals" in out and "indicators" in out and "multi_timeframe" in out


async def test_tool_tolerates_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.servers import reports

    async def ok(value: Any):
        return value

    async def boom(_s: str):
        raise RuntimeError("down")

    monkeypatch.setattr(reports, "analyst_ratings", boom)
    monkeypatch.setattr(reports, "price_targets", lambda s: ok({"targets": {}}))
    monkeypatch.setattr(reports, "upgrades_downgrades", lambda s: ok({"events": []}))
    monkeypatch.setattr(reports, "sec_filings", lambda s: ok({"filings": []}))

    out = json.loads(await srv.analyst_reports_tool("AAPL"))
    assert out["ratings"]["_error"].startswith("RuntimeError")
    assert "price_targets" in out


# ---------------------------------------------------------------------------
# Sector tools + prompt
# ---------------------------------------------------------------------------


def test_list_sectors_tool() -> None:
    out = json.loads(srv.list_sectors_tool())
    assert out["count"] == 11
    assert out["sectors"]["Technology"] > 0


def test_sector_constituents_tool_resolves_alias() -> None:
    out = json.loads(srv.sector_constituents_tool("tech"))
    assert out["sector"] == "Technology"
    assert "AAPL" in out["constituents"]
    # Unknown sector lists the available ones.
    miss = json.loads(srv.sector_constituents_tool("zzz"))
    assert miss["sector"] is None
    assert miss["available"]


async def test_scan_sector_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.orchestration import market

    async def fake_gather(symbol: str, *, settings: Any = None):
        return {**BULLISH_DOSSIER, "symbol": symbol}

    monkeypatch.setattr(market, "gather_evidence", fake_gather)
    out = json.loads(await srv.scan_sector_tool("tech", limit=4, top_n=3))
    assert out["sector"] == "Technology"
    assert out["n_analyzed"] == 4
    assert out["stance"] == "overweight"
    assert out["disclaimer"]


def test_decide_sector_prompt_builder() -> None:
    text = srv.build_decide_sector_prompt("healthcare", 3)
    assert "Health Care" in text
    assert "scan_sector" in text
    assert "NOT investment advice" in text
    # Bad top_n must not raise.
    assert "Technology" in srv.decide_sector_prompt("tech", top_n="oops")


# ---------------------------------------------------------------------------
# Regime / backtest / portfolio tools
# ---------------------------------------------------------------------------


async def test_market_regime_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_regime(*, benchmark: str = "SPY", settings: Any = None):
        return {"benchmark": benchmark, "regime": "risk_on", "gross_exposure": 1.0}

    monkeypatch.setattr(srv, "market_regime", fake_regime)
    out = json.loads(await srv.market_regime_tool("SPY"))
    assert out["regime"] == "risk_on"
    assert out["gross_exposure"] == 1.0


async def test_backtest_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bt(symbol: str, *, period: str = "10y", cost_bps: float = 10.0, n_trials: int = 10, settings: Any = None):
        return {"symbol": symbol, "strategy": {"sharpe": 0.8}, "overfit_checks": {"deflated_sharpe": 0.6}}

    monkeypatch.setattr(srv, "run_backtest", fake_bt)
    out = json.loads(await srv.backtest_tool("AAPL"))
    assert out["symbol"] == "AAPL"
    assert out["strategy"]["sharpe"] == 0.8


async def test_build_portfolio_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(symbols, *, max_positions=10, max_weight=0.25, regime=None, settings=None):
        return {"longs": [{"symbol": s, "weight": 1.0 / len(symbols)} for s in symbols], "shorts": [], "disclaimer": "x"}

    monkeypatch.setattr(srv, "build_portfolio", fake_build)
    out = json.loads(await srv.build_portfolio_tool(["AAPL", "MSFT"], max_positions=5, max_weight=0.5))
    assert len(out["longs"]) == 2
