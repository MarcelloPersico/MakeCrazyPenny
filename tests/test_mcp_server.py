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
    assert {"decide", "bull_case", "bear_case", "judge"} == prompts


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
    async def fake_gather(symbol: str, *, settings: Any = None):
        return BULLISH_DOSSIER

    monkeypatch.setattr(srv, "gather_evidence", fake_gather)
    out = json.loads(await srv.decide_tool("$aapl"))
    assert out["symbol"] == "AAPL"
    assert out["action"] == "BUY"
    assert out["method"] == "quant"
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
    async def fake_gather(symbol: str, *, settings: Any = None):
        return BULLISH_DOSSIER

    monkeypatch.setattr(srv, "gather_evidence", fake_gather)
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
