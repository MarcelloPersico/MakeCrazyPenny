"""Tests for the debate-driven decision engine (CONTRACT.md §10.3).

All tests are deterministic and OFFLINE. The LLM debate/judge are never invoked
against the network: we exercise the pure quant backbone directly and, for the
``decide`` pipeline, monkeypatch the evidence gathering and the debate/judge
coroutines. The autouse ``hermetic_env`` fixture (see ``conftest.py``) guarantees
no real API keys leak in.
"""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.core.types import DebateArgument, DebateTranscript, TradeDecision
from makecrazypenny.orchestration import debate

# ---------------------------------------------------------------------------
# Synthetic dossiers
# ---------------------------------------------------------------------------

BULLISH_DOSSIER: dict[str, Any] = {
    "symbol": "AAPL",
    "signals": {
        "signals": [
            {"name": "golden_cross", "direction": "bullish"},
            {"name": "rsi_oversold", "direction": "bullish"},
            {"name": "macd_bullish_cross", "direction": "bullish"},
        ]
    },
    "sentiment": {"score": 0.6, "label": "positive"},
    "ratings": {"ratings": [{"strong_buy": 8, "buy": 6, "hold": 2, "sell": 0, "strong_sell": 0}]},
    "price_targets": {"targets": {"mean": 120.0, "current": 100.0}},
    "congress": {"trades": [{"transaction": "Purchase"}, {"transaction": "Purchase"}]},
    "insider": {"transactions": [{"transaction": "Buy"}]},
    "cross_check": {"divergence": {"score": 0.1, "label": "aligned"}},
}

BEARISH_DOSSIER: dict[str, Any] = {
    "symbol": "XYZ",
    "signals": {
        "signals": [
            {"name": "death_cross", "direction": "bearish"},
            {"name": "rsi_overbought", "direction": "bearish"},
        ]
    },
    "sentiment": {"score": -0.5, "label": "negative"},
    "ratings": {"ratings": [{"strong_buy": 0, "buy": 1, "hold": 3, "sell": 6, "strong_sell": 4}]},
    "price_targets": {"targets": {"mean": 80.0, "current": 100.0}},
    "congress": {"trades": [{"transaction": "Sale"}, {"transaction": "Sale"}]},
    "insider": {"transactions": [{"transaction": "Sale (Full)"}]},
    "cross_check": {"divergence": {"score": 0.2}},
}


# ---------------------------------------------------------------------------
# score_evidence — the deterministic quant backbone
# ---------------------------------------------------------------------------


def test_score_evidence_bullish_is_net_positive() -> None:
    scored = debate.score_evidence(BULLISH_DOSSIER)
    assert scored["net_score"] > 0
    assert scored["bull_score"] > scored["bear_score"]
    # Every contributing category should be represented.
    assert {"technical", "sentiment", "analyst", "price_target", "congress", "insider"} <= set(
        scored["categories"]
    )


def test_score_evidence_bearish_is_net_negative() -> None:
    scored = debate.score_evidence(BEARISH_DOSSIER)
    assert scored["net_score"] < 0
    assert scored["bear_score"] > scored["bull_score"]


def test_score_evidence_tolerates_garbage_and_missing() -> None:
    # Missing blocks, error markers, and wrong shapes must not raise.
    scored = debate.score_evidence(
        {
            "symbol": "Z",
            "signals": {"_error": "boom"},
            "sentiment": "not-a-dict",
            "ratings": {"ratings": []},
            "price_targets": {"targets": {}},
        }
    )
    assert scored["factors"] == []
    assert scored["net_score"] == 0.0


def test_signal_weights_make_crosses_dominate() -> None:
    golden = debate.score_evidence({"signals": {"signals": [{"name": "golden_cross", "direction": "bullish"}]}})
    rsi = debate.score_evidence({"signals": {"signals": [{"name": "rsi_oversold", "direction": "bullish"}]}})
    assert golden["net_score"] > rsi["net_score"]


# ---------------------------------------------------------------------------
# decide_from_scores — quant decision, verdict override, fallbacks
# ---------------------------------------------------------------------------


def test_decide_quant_only_bullish_is_buy() -> None:
    scored = debate.score_evidence(BULLISH_DOSSIER)
    dec = debate.decide_from_scores("AAPL", scored, method="quant-only")
    assert isinstance(dec, TradeDecision)
    assert dec.action == "BUY"
    assert dec.direction == "LONG"
    assert 0.0 <= dec.conviction <= 1.0
    assert dec.disclaimer  # always carries the disclaimer
    assert dec.bull_case  # filled from the bullish factors


def test_decide_quant_only_bearish_is_short() -> None:
    scored = debate.score_evidence(BEARISH_DOSSIER)
    dec = debate.decide_from_scores("XYZ", scored, method="quant-only")
    assert dec.action == "SHORT"
    assert dec.direction == "SHORT"


def test_decide_thin_evidence_avoids() -> None:
    # A single weak signal is below the conviction floor -> AVOID.
    scored = debate.score_evidence({"signals": {"signals": [{"name": "rsi_oversold", "direction": "bullish"}]}})
    dec = debate.decide_from_scores("X", scored, method="quant-only")
    assert dec.action == "AVOID"
    assert dec.direction == "FLAT"


def test_verdict_overrides_quant_direction_and_conviction() -> None:
    scored = debate.score_evidence(BULLISH_DOSSIER)  # quant says BUY
    verdict = {
        "action": "AVOID",
        "conviction": 0.15,
        "horizon": "position",
        "summary": "Too rich here",
        "rationale": ["valuation stretched"],
        "risks": ["earnings miss"],
        "invalidation": "breakout over 110",
        "bull_case": ["momentum"],
        "bear_case": ["valuation"],
    }
    dec = debate.decide_from_scores("AAPL", scored, verdict=verdict, method="debate+judge")
    assert dec.action == "AVOID"  # judge wins over the quant backbone
    assert dec.direction == "FLAT"
    assert dec.conviction == 0.15
    assert dec.horizon == "position"
    assert dec.summary == "Too rich here"
    assert dec.invalidation == "breakout over 110"
    # Quant scores are preserved for transparency even when the judge overrides.
    assert dec.net_score == scored["net_score"]


def test_invalid_verdict_action_falls_back_to_quant() -> None:
    scored = debate.score_evidence(BULLISH_DOSSIER)
    dec = debate.decide_from_scores("AAPL", scored, verdict={"action": "MOON"}, method="debate+judge")
    assert dec.action == "BUY"  # garbage action ignored, quant stands


def test_cases_fall_back_to_transcript_when_verdict_silent() -> None:
    scored = debate.score_evidence({"symbol": "T"})  # no quant factors
    transcript = DebateTranscript(
        symbol="T",
        rounds=1,
        arguments=[
            DebateArgument(side="bull", round=1, thesis="up", key_points=["bp1", "bp2"]),
            DebateArgument(side="bear", round=1, thesis="down", key_points=["rp1"]),
        ],
    )
    dec = debate.decide_from_scores("T", scored, transcript=transcript, verdict={}, method="debate+judge")
    assert dec.bull_case == ["bp1", "bp2"]
    assert dec.bear_case == ["rp1"]


# ---------------------------------------------------------------------------
# gather_evidence — fan-out across servers, tolerant of failures
# ---------------------------------------------------------------------------


async def test_gather_evidence_assembles_dossier(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.servers import congress, reports, sentiment, synthesis, technical

    async def ok(value: Any):
        return value

    monkeypatch.setattr(technical, "detect_signals", lambda s: ok({"signals": []}))
    monkeypatch.setattr(technical, "multi_timeframe_summary", lambda s: ok({"mtf": True}))
    monkeypatch.setattr(sentiment, "aggregate_sentiment", lambda s: ok({"score": 0.0}))
    monkeypatch.setattr(congress, "congress_trades", lambda s: ok({"trades": []}))
    monkeypatch.setattr(congress, "insider_transactions", lambda s: ok({"transactions": []}))
    monkeypatch.setattr(reports, "analyst_ratings", lambda s: ok({"ratings": []}))
    monkeypatch.setattr(reports, "price_targets", lambda s: ok({"targets": {}}))
    monkeypatch.setattr(reports, "upgrades_downgrades", lambda s: ok({"events": []}))
    monkeypatch.setattr(synthesis, "cross_check", lambda s: ok({"divergence": {}}))

    dossier = await debate.gather_evidence("$aapl")
    assert dossier["symbol"] == "AAPL"
    for key in ("signals", "mtf", "sentiment", "congress", "insider", "ratings", "price_targets", "upgrades", "cross_check"):
        assert key in dossier


async def test_gather_evidence_marks_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from makecrazypenny.servers import technical

    async def boom(_s: str):
        raise RuntimeError("provider down")

    monkeypatch.setattr(technical, "detect_signals", boom)
    dossier = await debate.gather_evidence("AAPL")
    assert "_error" in dossier["signals"]
    assert "RuntimeError" in dossier["signals"]["_error"]


# ---------------------------------------------------------------------------
# decide — top-level deterministic quant pipeline (no AI, no network)
# ---------------------------------------------------------------------------


async def test_decide_is_quant_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(symbol: str, *, settings: Any = None):
        return BULLISH_DOSSIER

    monkeypatch.setattr(debate, "gather_evidence", fake_gather)
    dec = await debate.decide("AAPL")
    assert dec.method == "quant"
    assert dec.action == "BUY"
    assert dec.transcript is None
    assert dec.disclaimer


# ---------------------------------------------------------------------------
# TradeDecision serialization + CLI formatting
# ---------------------------------------------------------------------------


def test_trade_decision_to_dict_round_trips() -> None:
    scored = debate.score_evidence(BULLISH_DOSSIER)
    dec = debate.decide_from_scores("AAPL", scored, method="quant")
    d = dec.to_dict()
    import json

    json.dumps(d)  # must be JSON-serializable
    for key in ("symbol", "action", "direction", "conviction", "factors", "method", "disclaimer"):
        assert key in d


def test_format_decision_is_ascii_safe() -> None:
    # The CLI must print on any console (incl. cp437/ascii). Our template strings
    # — summary, factor details, notes — must therefore be strictly ASCII.
    from makecrazypenny.orchestration import main

    scored = debate.score_evidence(BULLISH_DOSSIER)
    dec = debate.decide_from_scores("AAPL", scored, method="quant")
    text = main._format_decision(dec.to_dict())
    text.encode("ascii")  # raises if a non-ASCII char slipped into our template
