"""Tests for the Layer-1 capability servers (CONTRACT.md §9, §11).

Each server's module-level ``get_registry()`` is monkeypatched to a deterministic
fake registry returning canned, JSON-serializable data. The pure async logic
functions are then called directly (bypassing MCP) and their result/content
shapes are asserted. Everything here is deterministic and fully offline: no
network, no API keys, and no Claude Agent SDK required.

The fake registry mirrors the real :meth:`ProviderRegistry.fetch` contract:
``fetch(capability, *, ttl=None, **params) -> {"provider", "data", "cached"}``
where ``data`` is already a core-type ``to_dict()`` payload (or list thereof).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from makecrazypenny.servers import (
    congress,
    orchestration,
    reports,
    sentiment,
    synthesis,
    technical,
)
from makecrazypenny.servers._sdk import SDK_AVAILABLE

# ---------------------------------------------------------------------------
# Fake provider registry
# ---------------------------------------------------------------------------


class FakeRegistry:
    """A canned, offline stand-in for :class:`ProviderRegistry`.

    Maps each capability name to a fixed ``data`` payload. ``fetch`` accepts the
    capability either positionally (``fetch("ohlcv", ...)``) or by keyword
    (``fetch(capability="analyst_ratings", ...)``), matching how the various
    servers call it, and records every call for assertion.
    """

    def __init__(self, data_by_capability: dict[str, Any], *, provider: str = "fake") -> None:
        self._data = dict(data_by_capability)
        self.provider = provider
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(
        self,
        capability: str | None = None,
        *,
        ttl: float | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        if capability is None:  # tolerate fetch(capability=...) keyword form
            capability = params.pop("capability")
        self.calls.append((capability, dict(params)))
        if capability not in self._data:
            from makecrazypenny.core.errors import AllProvidersFailed

            raise AllProvidersFailed(capability)
        return {
            "provider": self.provider,
            "data": self._data[capability],
            "cached": False,
        }


def _patch_registry(monkeypatch: pytest.MonkeyPatch, module: Any, registry: Any) -> None:
    """Monkeypatch ``module.get_registry`` to return ``registry``."""
    monkeypatch.setattr(module, "get_registry", lambda: registry)


def _parse_content(result: dict[str, Any]) -> Any:
    """Assert the MCP content-dict shape and return the parsed JSON payload.

    Verifies the exact mandated envelope (CONTRACT.md §2.4):
    ``{"content": [{"type": "text", "text": "<json string>"}]}`` and that the
    text field is valid JSON.
    """
    assert isinstance(result, dict)
    assert set(result.keys()) == {"content"}
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1
    block = content[0]
    assert block["type"] == "text"
    assert isinstance(block["text"], str)
    return json.loads(block["text"])


# ---------------------------------------------------------------------------
# Synthetic OHLCV series (deterministic; used by the technical tests)
# ---------------------------------------------------------------------------


def _iso_day(i: int) -> str:
    """Return a strictly-increasing, unique ISO-8601 UTC timestamp for bar ``i``.

    Using ``timedelta`` from a fixed base date guarantees the timestamps are
    unique and monotonically increasing, so the technical layer's ``sort_index``
    preserves the intended bar order (a naive ``i % 28`` scheme collides and
    reshuffles the series, corrupting the indicators).
    """
    from datetime import datetime, timedelta, timezone

    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(days=i)).isoformat()


def _synthetic_ohlcv(n: int = 260) -> dict[str, Any]:
    """Build a deterministic ``OHLCV.to_dict()``-shaped payload.

    A long, smooth uptrend so SMA50 sits above SMA200 (uptrend / golden-cross
    territory) and indicators are well-defined over the >200-bar window.
    """
    bars: list[dict[str, Any]] = []
    for i in range(n):
        close = 50.0 + i * 0.5  # steady uptrend
        high = close + 1.0
        low = close - 1.0
        open_ = close - 0.25
        bars.append(
            {
                "ts": _iso_day(i),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000 + i * 10,
            }
        )
    return {
        "symbol": "TEST",
        "interval": "1d",
        "bars": bars,
        "provenance": {"provider": "fake", "fetched_at": "2023-01-01T00:00:00+00:00", "cached": False},
    }


def _oversold_ohlcv(n: int = 260) -> dict[str, Any]:
    """A steadily *declining* series so RSI is depressed and SMA50<SMA200.

    Used to exercise the bearish branch of :func:`technical.detect_signals`.
    """
    bars: list[dict[str, Any]] = []
    for i in range(n):
        close = 200.0 - i * 0.5
        bars.append(
            {
                "ts": _iso_day(i),
                "open": close + 0.25,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + i,
            }
        )
    return {"symbol": "TEST", "interval": "1d", "bars": bars, "provenance": None}


# ===========================================================================
# technical
# ===========================================================================


async def test_technical_get_ohlcv(monkeypatch: pytest.MonkeyPatch) -> None:
    series = _synthetic_ohlcv()
    reg = FakeRegistry({"ohlcv": series})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.get_ohlcv(" $test ", interval="1d", period="6mo")

    assert result["symbol"] == "TEST"  # normalize_symbol applied
    assert result["interval"] == "1d"
    assert result["period"] == "6mo"
    assert result["provider"] == "fake"
    assert result["cached"] is False
    assert result["n_bars"] == len(series["bars"])
    assert result["bars"] == series["bars"]
    # Registry was asked for ohlcv with the normalized symbol.
    assert reg.calls[0][0] == "ohlcv"
    assert reg.calls[0][1]["symbol"] == "TEST"
    # And the result is JSON-serializable.
    json.dumps(result)


async def test_technical_get_ohlcv_tool_content_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _synthetic_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    envelope = await technical.get_ohlcv_tool({"symbol": "TEST"})
    payload = _parse_content(envelope)
    assert payload["symbol"] == "TEST"
    assert isinstance(payload["bars"], list)


async def test_technical_compute_indicators(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _synthetic_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.compute_indicators("TEST")
    ind = result["indicators"]

    # The full default battery is requested and computed.
    assert set(result["requested"]) == set(technical.DEFAULT_INDICATORS)
    # RSI is a finite float in the valid 0..100 band.
    assert isinstance(ind["rsi"], float)
    assert 0.0 <= ind["rsi"] <= 100.0
    # MACD/Bollinger/SMA blocks have the expected nested keys with float values.
    assert set(ind["macd"]) == {"macd", "signal", "hist"}
    assert set(ind["bbands"]) == {"upper", "middle", "lower"}
    assert ind["sma"]["sma50"] is not None
    assert ind["sma"]["sma200"] is not None
    # On a clean uptrend SMA50 sits above SMA200.
    assert ind["sma"]["sma50"] > ind["sma"]["sma200"]
    # OBV present and numeric.
    assert isinstance(ind["obv"], float)
    json.dumps(result)


async def test_technical_compute_indicators_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _synthetic_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.compute_indicators("TEST", indicators=["rsi"])
    assert result["requested"] == ["rsi"]
    assert "rsi" in result["indicators"]
    assert "macd" not in result["indicators"]


async def test_technical_compute_indicators_empty_series(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": {"symbol": "TEST", "interval": "1d", "bars": []}})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.compute_indicators("TEST")
    assert result["n_bars"] == 0
    assert result["indicators"] == {}


async def test_technical_detect_signals_bullish(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _synthetic_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.detect_signals("TEST")
    assert isinstance(result["signals"], list)
    values = result["values"]
    # The derived values are populated and self-consistent with an uptrend.
    assert values["close"] is not None
    assert values["sma50"] is not None and values["sma200"] is not None
    assert values["sma50"] > values["sma200"]
    # Every emitted signal carries the documented shape.
    for sig in result["signals"]:
        assert set(sig) == {"name", "direction", "detail"}
        assert sig["direction"] in {"bullish", "bearish"}
    json.dumps(result)


async def test_technical_detect_signals_oversold(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _oversold_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.detect_signals("TEST")
    names = {s["name"] for s in result["signals"]}
    # A long, monotonic downtrend yields a depressed RSI -> oversold signal.
    assert "rsi_oversold" in names
    # And SMA50 sits below SMA200 in a downtrend.
    assert result["values"]["sma50"] < result["values"]["sma200"]


async def test_technical_support_resistance(monkeypatch: pytest.MonkeyPatch) -> None:
    series = _synthetic_ohlcv()
    reg = FakeRegistry({"ohlcv": series})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.support_resistance("TEST")
    assert result["support"] is not None
    assert result["resistance"] is not None
    assert result["resistance"] >= result["support"]
    assert result["window"] > 0
    # Classic pivot derivatives are computed.
    assert result["pivot"] is not None
    assert result["r1"] is not None and result["s1"] is not None
    json.dumps(result)


async def test_technical_multi_timeframe_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"ohlcv": _synthetic_ohlcv()})
    _patch_registry(monkeypatch, technical, reg)

    result = await technical.multi_timeframe_summary("TEST")
    assert result["symbol"] == "TEST"
    # All three timeframes are summarized.
    assert set(result["timeframes"]) == {"daily", "weekly", "monthly"}
    daily = result["timeframes"]["daily"]
    assert daily["trend"] in {"bullish", "bearish", "neutral", "unknown"}
    json.dumps(result)


# ===========================================================================
# sentiment
# ===========================================================================


def _sentiment_payload(score: float, label: str, n: int, drivers: list[str]) -> dict[str, Any]:
    return {
        "symbol": "TEST",
        "score": score,
        "label": label,
        "n_articles": n,
        "drivers": drivers,
        "provenance": {"provider": "fake", "fetched_at": "2023-01-01T00:00:00+00:00", "cached": False},
    }


async def test_sentiment_get_news(monkeypatch: pytest.MonkeyPatch) -> None:
    articles = [
        {"symbol": "TEST", "headline": "Up day", "source": "X", "url": None,
         "published_at": "2023-01-02", "summary": None},
        {"symbol": "TEST", "headline": "Down day", "source": "Y", "url": None,
         "published_at": "2023-01-01", "summary": None},
    ]
    reg = FakeRegistry({"company_news": articles})
    _patch_registry(monkeypatch, sentiment, reg)

    result = await sentiment.get_news(" $test ", days=10)
    assert result["symbol"] == "TEST"
    assert result["days"] == 10
    assert result["count"] == 2
    assert result["articles"] == articles
    assert result["provider"] == "fake"
    json.dumps(result)


async def test_sentiment_news_sentiment(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry(
        {"news_sentiment": _sentiment_payload(0.6, "bullish", 12, ["earnings beat"])}
    )
    _patch_registry(monkeypatch, sentiment, reg)

    result = await sentiment.news_sentiment("TEST")
    assert result["symbol"] == "TEST"
    assert result["score"] == pytest.approx(0.6)
    assert result["label"] == "bullish"
    assert result["n_articles"] == 12
    assert result["drivers"] == ["earnings beat"]
    json.dumps(result)


async def test_sentiment_social_sentiment(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry(
        {"social_sentiment": _sentiment_payload(-0.4, "bearish", 30, ["short squeeze fears"])}
    )
    _patch_registry(monkeypatch, sentiment, reg)

    result = await sentiment.social_sentiment("TEST")
    assert result["score"] == pytest.approx(-0.4)
    assert result["label"] == "bearish"
    assert result["n_articles"] == 30
    json.dumps(result)


async def test_sentiment_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry(
        {
            "news_sentiment": _sentiment_payload(0.8, "bullish", 10, ["earnings beat"]),
            "social_sentiment": _sentiment_payload(0.8, "bullish", 10, ["hype"]),
        }
    )
    _patch_registry(monkeypatch, sentiment, reg)

    result = await sentiment.aggregate_sentiment("TEST")
    assert result["symbol"] == "TEST"
    assert result["available"] is True
    # Equal +0.8 scores blend to ~+0.8 -> bullish.
    assert result["score"] == pytest.approx(0.8, abs=1e-6)
    assert result["label"] == "bullish"
    assert "earnings beat" in result["drivers"] and "hype" in result["drivers"]
    assert set(result["components"]) == {"news", "social"}
    json.dumps(result)


async def test_sentiment_news_sentiment_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty registry -> AllProvidersFailed -> graceful error payload, never raises.
    reg = FakeRegistry({})
    _patch_registry(monkeypatch, sentiment, reg)

    result = await sentiment.news_sentiment("TEST")
    assert result["available"] is False
    assert "error" in result
    assert result["symbol"] == "TEST"
    json.dumps(result)


async def test_sentiment_tool_content_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry(
        {"news_sentiment": _sentiment_payload(0.2, "bullish", 5, [])}
    )
    _patch_registry(monkeypatch, sentiment, reg)

    # Call the @tool MCP wrapper (distinct ``*_tool`` name; see CONTRACT.md §9.1).
    envelope = await sentiment.news_sentiment_tool({"symbol": "TEST"})
    payload = _parse_content(envelope)
    assert payload["symbol"] == "TEST"
    assert payload["label"] == "bullish"


# ===========================================================================
# congress
# ===========================================================================


def _trade(member: str, disclosure: str, txn_date: str, symbol: str = "NVDA") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "member": member,
        "chamber": "House",
        "transaction": "buy",
        "amount_range": "$1,001 - $15,000",
        "transaction_date": txn_date,
        "disclosure_date": disclosure,
        "provenance": {"provider": "fake", "fetched_at": "2023-01-01T00:00:00+00:00", "cached": False},
    }


async def test_congress_trades_by_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    trades = [
        _trade("Pelosi", "2023-03-01", "2023-02-01"),
        _trade("Cruz", "2023-04-01", "2023-03-15"),
    ]
    reg = FakeRegistry({"congress_trades": trades})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.congress_trades("NVDA")
    assert result["query_type"] == "symbol"
    assert result["query"] == "NVDA"
    assert result["count"] == 2
    # Sorted most-recent disclosure first.
    assert result["trades"][0]["disclosure_date"] == "2023-04-01"
    # Disclosure-lag caveat is always present.
    assert "caveat" in result and result["caveat"]
    # Registry queried with the normalized symbol.
    assert reg.calls[0] == ("congress_trades", {"symbol": "NVDA"})
    json.dumps(result)


async def test_congress_trades_by_member(monkeypatch: pytest.MonkeyPatch) -> None:
    trades = [
        _trade("Nancy Pelosi", "2023-03-01", "2023-02-01"),
        _trade("Ted Cruz", "2023-04-01", "2023-03-15"),
    ]
    reg = FakeRegistry({"congress_trades": trades})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.congress_trades("Pelosi")
    assert result["query_type"] == "member"
    # Only Pelosi's trade survives the member filter.
    assert result["count"] == 1
    assert "Pelosi" in result["trades"][0]["member"]
    # Member queries fetch without a symbol filter.
    assert reg.calls[0] == ("congress_trades", {})


async def test_congress_trades_since_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    trades = [
        _trade("Pelosi", "2023-03-01", "2023-02-01"),
        _trade("Cruz", "2023-04-01", "2023-03-15"),
    ]
    reg = FakeRegistry({"congress_trades": trades})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.congress_trades("NVDA", since="2023-03-15")
    assert result["count"] == 1
    assert result["trades"][0]["disclosure_date"] == "2023-04-01"


async def test_congress_recent_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    trades = [_trade("Pelosi", today, today), _trade("Cruz", "2000-01-01", "2000-01-01")]
    reg = FakeRegistry({"congress_trades": trades})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.recent_congress_activity(days=7)
    # Only the recently-disclosed trade is within the trailing window.
    assert result["count"] == 1
    assert result["trades"][0]["disclosure_date"] == today
    assert result["window_days"] == 7
    json.dumps(result)


async def test_congress_insider_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    txns = [
        {"symbol": "TEST", "insider": "Jane Doe", "role": "CEO", "transaction": "sell",
         "shares": 100.0, "value": 5000.0, "date": "2023-02-01", "provenance": None},
        {"symbol": "TEST", "insider": "John Roe", "role": "CFO", "transaction": "buy",
         "shares": 50.0, "value": 2500.0, "date": "2023-03-01", "provenance": None},
    ]
    reg = FakeRegistry({"insider_transactions": txns})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.insider_transactions("test")
    assert result["symbol"] == "TEST"
    assert result["count"] == 2
    # Most-recent first.
    assert result["transactions"][0]["date"] == "2023-03-01"
    assert "caveat" in result
    json.dumps(result)


async def test_congress_new_disclosures(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"congress_trades": [_trade("Pelosi", "2023-03-01", "2023-02-01")]})
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.new_disclosures(["nvda", "AAPL"], since="2023-01-01")
    assert result["watchlist"] == ["NVDA", "AAPL"]
    assert set(result["per_symbol"]) == {"NVDA", "AAPL"}
    # The fake returns the same trade dict for both symbols; per-symbol counts it
    # once each, but the merged feed de-duplicates identical disclosures.
    assert result["per_symbol"]["NVDA"] == 1
    assert result["per_symbol"]["AAPL"] == 1
    assert result["count"] == 1
    assert len(result["disclosures"]) == 1
    json.dumps(result)


async def test_congress_trades_all_providers_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({})  # any fetch raises AllProvidersFailed
    _patch_registry(monkeypatch, congress, reg)

    result = await congress.congress_trades("NVDA")
    # Failure surfaces as data (an "error" field), never a raised exception.
    assert "error" in result
    assert result["count"] == 0
    json.dumps(result)


async def test_congress_tool_content_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"congress_trades": [_trade("Pelosi", "2023-03-01", "2023-02-01")]})
    _patch_registry(monkeypatch, congress, reg)

    envelope = await congress.congress_trades_tool({"symbol_or_member": "NVDA"})
    payload = _parse_content(envelope)
    assert payload["query"] == "NVDA"
    assert payload["count"] == 1


# ===========================================================================
# reports
# ===========================================================================


async def test_reports_analyst_ratings(monkeypatch: pytest.MonkeyPatch) -> None:
    ratings = [
        {"symbol": "TEST", "period": "2023-03", "strong_buy": 5, "buy": 10, "hold": 3,
         "sell": 1, "strong_sell": 0, "provenance": None},
    ]
    reg = FakeRegistry({"analyst_ratings": ratings})
    _patch_registry(monkeypatch, reports, reg)

    result = await reports.analyst_ratings(" $test ")
    assert result["symbol"] == "TEST"
    assert result["provider"] == "fake"
    assert result["cached"] is False
    assert result["ratings"] == ratings
    # Capability passed by keyword in this module.
    assert reg.calls[0] == ("analyst_ratings", {"symbol": "TEST"})
    json.dumps(result)


async def test_reports_price_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    pt = {"symbol": "TEST", "mean": 120.0, "high": 150.0, "low": 90.0, "current": 100.0,
          "provenance": None}
    reg = FakeRegistry({"price_targets": pt})
    _patch_registry(monkeypatch, reports, reg)

    result = await reports.price_targets("TEST")
    assert result["targets"] == pt
    json.dumps(result)


async def test_reports_upgrades_downgrades_since(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"symbol": "TEST", "firm": "BankA", "from_grade": "Hold", "to_grade": "Buy",
         "action": "upgrade", "date": "2023-02-01", "provenance": None},
        {"symbol": "TEST", "firm": "BankB", "from_grade": "Buy", "to_grade": "Hold",
         "action": "downgrade", "date": "2023-04-01", "provenance": None},
    ]
    reg = FakeRegistry({"upgrades_downgrades": events})
    _patch_registry(monkeypatch, reports, reg)

    result = await reports.upgrades_downgrades("TEST", since="2023-03-01")
    assert result["count"] == 1
    assert result["events"][0]["date"] == "2023-04-01"
    assert result["since"] == "2023-03-01"
    json.dumps(result)


async def test_reports_sec_filings_form_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    filings = [
        {"symbol": "TEST", "form": "10-K", "title": "Annual", "filed_at": "2023-02-01",
         "url": None, "provenance": None},
        {"symbol": "TEST", "form": "S-1", "title": "Reg", "filed_at": "2023-03-01",
         "url": None, "provenance": None},
    ]
    reg = FakeRegistry({"sec_filings": filings})
    _patch_registry(monkeypatch, reports, reg)

    result = await reports.sec_filings("TEST", forms=["10-K"])
    # Only the 10-K survives the form filter.
    assert result["count"] == 1
    assert result["filings"][0]["form"] == "10-K"
    assert result["forms"] == ["10-K"]
    json.dumps(result)


async def test_reports_sec_filings_default_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry({"sec_filings": []})
    _patch_registry(monkeypatch, reports, reg)

    result = await reports.sec_filings("TEST")
    assert result["forms"] == reports.DEFAULT_FORMS


async def test_reports_tool_content_shape_and_disclaimer(monkeypatch: pytest.MonkeyPatch) -> None:
    ratings = [
        {"symbol": "TEST", "period": "2023-03", "strong_buy": 1, "buy": 0, "hold": 0,
         "sell": 0, "strong_sell": 0, "provenance": None},
    ]
    reg = FakeRegistry({"analyst_ratings": ratings})
    _patch_registry(monkeypatch, reports, reg)

    envelope = await reports.analyst_ratings_tool({"symbol": "TEST"})
    payload = _parse_content(envelope)
    assert payload["symbol"] == "TEST"
    # report_result attaches the not-investment-advice disclaimer.
    assert "disclaimer" in payload and payload["disclaimer"]


# ===========================================================================
# synthesis
# ===========================================================================


async def test_synthesis_cross_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # Consensus Buy, but price below all MAs (downtrend) and margins compressing
    # -> the canonical divergence the contract calls out.
    # A long, monotonic decline: each SMA window averages older (higher) prices,
    # so the latest price sits below all moving averages (the bearish read).
    closes = [{"close": 300.0 - i} for i in range(260)]  # last close == 41.0
    ratings = [
        {"symbol": "TEST", "period": "2023-03", "strong_buy": 20, "buy": 10, "hold": 0,
         "sell": 0, "strong_sell": 0, "provenance": None},
    ]
    reg = FakeRegistry(
        {
            "analyst_ratings": ratings,
            "price_targets": {"symbol": "TEST", "mean": 250.0, "high": 300.0, "low": 200.0,
                              "current": 50.0, "provenance": None},
            "quote": {"symbol": "TEST", "price": 41.0, "change": None, "change_pct": None,
                      "provenance": None},
            "ohlcv": {"symbol": "TEST", "interval": "1d", "bars": closes},
            "fundamentals": {"grossMargins": 0.20},
        }
    )
    _patch_registry(monkeypatch, synthesis, reg)
    # Prevent the optional reports sibling from intercepting the registry fetch:
    # force the direct-registry path by stubbing _sibling_call to return None.

    async def _no_sibling(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(synthesis, "_sibling_call", _no_sibling)

    result = await synthesis.cross_check(" $test ")
    assert result["symbol"] == "TEST"
    # The three views are present.
    assert set(result["views"]) == {"consensus", "technical", "fundamentals"}
    assert result["views"]["consensus"]["available"] is True
    assert result["views"]["consensus"]["label"] == "Buy"
    # Price sits below all moving averages -> bearish stance.
    assert result["views"]["technical"]["below_all_mas"] is True
    assert result["views"]["technical"]["stance"] == "bearish"
    # Divergence assessment present and flags the consensus-vs-price conflict.
    div = result["divergence"]
    assert div["signals"]["consensus_vs_price"] == "conflict"
    assert div["label"] in {"some_divergence", "high_divergence"}
    assert isinstance(div["flags"], list) and div["flags"]
    # Summary + sources present.
    assert isinstance(result["summary"], str) and result["summary"]
    assert "analyst_ratings" in result["sources"]
    json.dumps(result)


async def test_synthesis_cross_check_tool_content_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = FakeRegistry(
        {
            "analyst_ratings": [],
            "price_targets": {},
            "quote": {"symbol": "TEST", "price": 100.0},
            "ohlcv": {"symbol": "TEST", "interval": "1d", "bars": []},
            "fundamentals": {},
        }
    )
    _patch_registry(monkeypatch, synthesis, reg)

    async def _no_sibling(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(synthesis, "_sibling_call", _no_sibling)

    envelope = await synthesis.cross_check_tool({"symbol": "TEST"})
    payload = _parse_content(envelope)
    assert payload["symbol"] == "TEST"
    assert "divergence" in payload


async def test_synthesis_cross_check_degrades_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty registry: every capability raises AllProvidersFailed, but cross_check
    # must degrade to a partial assessment rather than raising.
    reg = FakeRegistry({})
    _patch_registry(monkeypatch, synthesis, reg)

    async def _no_sibling(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(synthesis, "_sibling_call", _no_sibling)

    result = await synthesis.cross_check("TEST")
    assert result["symbol"] == "TEST"
    assert result["views"]["consensus"]["available"] is False
    # All views failed -> errors recorded for each.
    assert result["errors"]
    json.dumps(result)


# ===========================================================================
# orchestration
# ===========================================================================


async def test_orchestration_spawn_analyst_missing_sdk_stub() -> None:
    # The SDK is genuinely absent in the test environment; spawn_analyst must
    # return a stub error dict, not crash.
    settings = orchestration.Settings.from_env()
    result = await orchestration.spawn_analyst("technical-analyst", "Analyze TEST", settings=settings)
    if not SDK_AVAILABLE:
        assert result["sdk"] is False
        assert "error" in result
        assert result["role"] == "technical-analyst"
    json.dumps(result)


async def test_orchestration_spawn_analyst_depth_guard() -> None:
    settings = orchestration.Settings.from_env()
    settings.max_depth = 3
    settings.max_budget_usd = 100.0  # ensure depth is the binding guard
    result = await orchestration.spawn_analyst(
        "technical-analyst", "Analyze", depth=3, settings=settings
    )
    assert result["refused"] is True
    assert result["reason"] == "max_depth_exceeded"
    assert result["depth"] == 3
    assert result["max_depth"] == 3
    json.dumps(result)


async def test_orchestration_spawn_analyst_negative_depth() -> None:
    settings = orchestration.Settings.from_env()
    result = await orchestration.spawn_analyst("role", "task", depth=-1, settings=settings)
    assert result["refused"] is True
    assert result["reason"] == "invalid_depth"


async def test_orchestration_spawn_analyst_budget_guard() -> None:
    settings = orchestration.Settings.from_env()
    settings.max_depth = 10  # depth is not the binding guard
    settings.max_budget_usd = 0.10  # below ESTIMATED_COST_PER_SPAWN_USD (0.25)
    result = await orchestration.spawn_analyst(
        "role", "task", depth=0, settings=settings, budget_spent_usd=0.0
    )
    assert result["refused"] is True
    assert result["reason"] == "max_budget_exceeded"
    assert result["max_budget_usd"] == pytest.approx(0.10)
    assert result["projected_usd"] == pytest.approx(orchestration.ESTIMATED_COST_PER_SPAWN_USD)
    json.dumps(result)


async def test_orchestration_spawn_analyst_within_guards_missing_sdk() -> None:
    # Within both guards but no SDK -> stub error (sdk False), still never raises.
    settings = orchestration.Settings.from_env()
    settings.max_depth = 5
    settings.max_budget_usd = 100.0
    result = await orchestration.spawn_analyst("role", "task", depth=0, settings=settings)
    if not SDK_AVAILABLE:
        assert result.get("sdk") is False
        assert "error" in result


async def test_orchestration_register_alert(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    settings = orchestration.Settings.from_env()
    settings.cache_dir = tmp_path  # isolate persistence to a temp dir

    result = await orchestration.register_alert(
        watchlist=[" $nvda ", "aapl"],
        kinds=["congress", "bogus_kind"],
        settings=settings,
    )
    assert result["registered"] is True
    assert result["watchlist"] == ["NVDA", "AAPL"]
    assert result["kinds"] == ["congress"]
    assert result["ignored_kinds"] == ["bogus_kind"]
    assert result["persisted"] is True
    # The config file was written under the temp cache dir.
    assert (tmp_path / orchestration._ALERTS_CONFIG_FILE).exists()
    json.dumps(result)


async def test_orchestration_check_alerts_no_registration(tmp_path: Any) -> None:
    settings = orchestration.Settings.from_env()
    settings.cache_dir = tmp_path  # empty -> no config registered

    result = await orchestration.check_alerts(settings=settings)
    assert result["checked"] is False
    assert result["reason"] == "no_alert_registered"
    json.dumps(result)


async def test_orchestration_check_alerts_deltas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    settings = orchestration.Settings.from_env()
    settings.cache_dir = tmp_path

    reg = FakeRegistry(
        {"congress_trades": [_trade("Pelosi", "2023-03-01", "2023-02-01")]}
    )
    _patch_registry(monkeypatch, orchestration, reg)

    config = {
        "watchlist": ["NVDA"],
        "kinds": ["congress"],
        "sinks": {"console": False},  # silent sink for a quiet test
    }

    # First run: the single trade is brand new.
    first = await orchestration.check_alerts(settings=settings, config=config)
    assert first["checked"] is True
    assert first["n_new"] == 1
    assert first["new_events"][0]["kind"] == "congress"
    assert first["state_persisted"] is True

    # Second run with identical data: the trade is already seen -> no new events.
    second = await orchestration.check_alerts(settings=settings, config=config)
    assert second["checked"] is True
    assert second["n_new"] == 0
    json.dumps(first)
    json.dumps(second)


async def test_orchestration_spawn_analyst_tool_content_shape() -> None:
    envelope = await orchestration.spawn_analyst_tool(
        {"role": "role", "task": "task", "depth": 99}
    )
    payload = _parse_content(envelope)
    # depth 99 >= default max_depth -> refused, wrapped in the content envelope.
    assert payload["refused"] is True
    assert payload["reason"] == "max_depth_exceeded"
