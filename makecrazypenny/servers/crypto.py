"""Crypto capability server (CONTRACT.md §16).

The crypto analogue of :mod:`makecrazypenny.servers.technical` +
:mod:`~makecrazypenny.servers.sentiment`, specialized for perpetual futures and
short-window leveraged trading. Exposes:

  * ``crypto_ohlcv`` / ``crypto_indicators`` / ``crypto_signals`` — price action on
    any interval (perp klines), reusing the shared :mod:`analysis.indicators`
    helpers (so this server does not import the equity ``technical`` server);
  * ``multi_timeframe`` — a 5m/15m/1h trend snapshot (the engine's default blend);
  * ``derivatives`` — funding rate, open interest (+ change), long/short ratio, and
    basis, the metrics that drive leveraged decisions;
  * ``crypto_sentiment`` — the Fear & Greed Index plus global market context.

Follows the per-server pattern: pure async logic functions (testable with a
monkeypatched module-level :func:`get_registry`), thin ``@tool`` wrappers, a
``create_sdk_mcp_server`` instance, and a guarded ``__main__`` runner. Heavy libs
(``pandas``/``ta``) are lazy-imported via the shared helpers, so importing this
module never hits the network.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.crypto_metrics import (
    basis_value,
    cvd_signal,
    depth_imbalance,
    oi_change_from_history,
    taker_flow_signal,
    top_trader_spread_signal,
    venue_divergence,
)
from ..analysis.indicators import (
    DEFAULT_INDICATORS,
    compute_indicator_frame,
    ohlcv_to_dataframe,
    signals_from_frame,
    summarize_timeframe,
)
from ..core.disclaimer import DISCLAIMER
from ..core.symbols import canonical_crypto
from ..providers import get_registry as _provider_get_registry
from ._common import text_result
from ._sdk import create_sdk_mcp_server, tool

#: Default timeframes for the multi-timeframe snapshot (label, interval, limit).
_MTF_TIMEFRAMES: tuple[tuple[str, str, int], ...] = (
    ("5m", "5m", 300),
    ("15m", "15m", 300),
    ("1h", "1h", 300),
)


def get_registry() -> Any:
    """Return the shared provider registry (monkeypatchable for tests)."""
    return _provider_get_registry()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_crypto_frame(symbol: str, interval: str, limit: int) -> tuple[Any, dict[str, Any]]:
    """Fetch crypto OHLCV for ``symbol`` and return ``(DataFrame, envelope)``."""
    registry = get_registry()
    envelope = await registry.fetch("crypto_ohlcv", symbol=symbol, interval=interval, limit=limit)
    data = envelope.get("data") or {}
    df = ohlcv_to_dataframe(data)
    return df, envelope


def _meta(symbol: str, interval: str, envelope: dict[str, Any], df: Any) -> dict[str, Any]:
    """Build the common metadata block shared by the crypto tools."""
    return {
        "symbol": symbol,
        "interval": interval,
        "provider": envelope.get("provider"),
        "cached": envelope.get("cached"),
        "n_bars": int(len(df)),
        "disclaimer": DISCLAIMER,
    }


async def _fetch_data(registry: Any, capability: str, **params: Any) -> Any:
    """Fetch a capability and return its ``data``, or an ``{"_error": ...}`` marker."""
    try:
        env = await registry.fetch(capability, **params)
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    return env.get("data") if isinstance(env, dict) else env


# ---------------------------------------------------------------------------
# Pure async logic functions (testable)
# ---------------------------------------------------------------------------


async def crypto_ohlcv(symbol: str, interval: str = "5m", limit: int = 500) -> dict[str, Any]:
    """Fetch raw perpetual OHLCV bars for ``symbol`` at ``interval``."""
    sym = canonical_crypto(symbol)
    df, envelope = await _fetch_crypto_frame(sym, interval, limit)
    data = envelope.get("data") or {}
    result = _meta(sym, interval, envelope, df)
    result["limit"] = limit
    result["bars"] = data.get("bars") or []
    return result


async def crypto_indicators(
    symbol: str, interval: str = "15m", indicators: list[str] | None = None
) -> dict[str, Any]:
    """Compute technical indicators for ``symbol`` on ``interval`` perp bars."""
    sym = canonical_crypto(symbol)
    names = list(indicators) if indicators else list(DEFAULT_INDICATORS)
    df, envelope = await _fetch_crypto_frame(sym, interval, 500)
    result = _meta(sym, interval, envelope, df)
    result["requested"] = names
    result["indicators"] = {} if len(df) == 0 else compute_indicator_frame(df, names)
    return result


async def crypto_signals(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """Detect technical signals for ``symbol`` on ``interval`` perp bars."""
    sym = canonical_crypto(symbol)
    df, envelope = await _fetch_crypto_frame(sym, interval, 500)
    result = _meta(sym, interval, envelope, df)
    signals, values = signals_from_frame(df)
    result["values"] = values
    result["signals"] = signals
    return result


async def multi_timeframe(symbol: str) -> dict[str, Any]:
    """Summarize trend/momentum across the 5m / 15m / 1h timeframes."""
    sym = canonical_crypto(symbol)
    timeframes: dict[str, Any] = {}
    last_provider: str | None = None
    any_cached = False
    for label, interval, limit in _MTF_TIMEFRAMES:
        try:
            df, envelope = await _fetch_crypto_frame(sym, interval, limit)
        except Exception as exc:
            timeframes[label] = {"interval": interval, "error": str(exc)}
            continue
        last_provider = envelope.get("provider") or last_provider
        any_cached = any_cached or bool(envelope.get("cached"))
        timeframes[label] = summarize_timeframe(df, interval)
    return {
        "symbol": sym,
        "provider": last_provider,
        "cached": any_cached,
        "timeframes": timeframes,
        "disclaimer": DISCLAIMER,
    }


async def derivatives(symbol: str, interval: str = "5m") -> dict[str, Any]:
    """Gather funding rate, open interest (+change), long/short ratio, and basis.

    Tolerant: each metric is fetched independently and a failure becomes an
    ``{"_error": ...}`` marker rather than aborting the rest. Also derives the
    short-window ``oi_change_pct`` (from the OI history) and ``price_change_pct``
    (from a short kline window) so the OI/price matrix can be scored.
    """
    sym = canonical_crypto(symbol)
    registry = get_registry()
    funding, oi, ls, ohlcv = await asyncio.gather(
        _fetch_data(registry, "funding_rate", symbol=sym),
        _fetch_data(registry, "open_interest", symbol=sym, interval=interval),
        _fetch_data(registry, "long_short_ratio", symbol=sym, interval=interval),
        _fetch_data(registry, "crypto_ohlcv", symbol=sym, interval=interval, limit=48),
        return_exceptions=False,
    )

    result: dict[str, Any] = {"symbol": sym, "interval": interval, "disclaimer": DISCLAIMER}
    result["funding"] = funding
    result["open_interest"] = oi
    result["long_short"] = ls

    # Basis (mark vs index) — prefer the funding payload's own basis if present.
    basis: float | None = None
    if isinstance(funding, dict) and "_error" not in funding:
        basis = funding.get("basis")
        if basis is None:
            basis = basis_value(funding.get("mark_price"), funding.get("index_price"))
    result["basis"] = basis

    # Open-interest change over the window (for the OI/price matrix).
    oi_change: float | None = None
    if isinstance(oi, dict) and "_error" not in oi:
        oi_change = oi_change_from_history(oi.get("history"))
    result["oi_change_pct"] = oi_change

    # Short-window price change over the same window.
    price_change: float | None = None
    if isinstance(ohlcv, dict) and "_error" not in ohlcv:
        bars = ohlcv.get("bars") or []
        closes = [b.get("close") for b in bars if isinstance(b, dict) and isinstance(b.get("close"), (int, float))]
        if len(closes) >= 2 and closes[0]:
            price_change = closes[-1] / closes[0] - 1.0
    result["price_change_pct"] = price_change

    return result


async def crypto_sentiment() -> dict[str, Any]:
    """Fetch the Fear & Greed Index plus global market context (tolerant)."""
    registry = get_registry()
    fng, glob = await asyncio.gather(
        _fetch_data(registry, "crypto_sentiment"),
        _fetch_data(registry, "crypto_global"),
        return_exceptions=False,
    )
    return {"fear_greed": fng, "global": glob, "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# Swarm extension (CONTRACT.md §18) — flow, HL-native context, social, news
# ---------------------------------------------------------------------------


def _signal_block(sig: tuple[float, str] | None) -> dict[str, Any] | None:
    """Render an analysis ``(strength, rationale)`` tuple as a JSON block."""
    if sig is None:
        return None
    return {"strength": round(float(sig[0]), 4), "detail": sig[1]}


async def flow_metrics(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """Gather the taker-flow evidence block (Binance futures, tolerant).

    Returns the raw ``taker_flow`` / ``top_trader_ratio`` / ``funding_history``
    payloads for the scorer; each sub-fetch degrades independently to an
    ``{"_error": ...}`` marker.
    """
    sym = canonical_crypto(symbol)
    registry = get_registry()
    taker, top, hist, ohlcv = await asyncio.gather(
        _fetch_data(registry, "taker_flow", symbol=sym, interval=interval, limit=48),
        _fetch_data(registry, "top_trader_ratio", symbol=sym, interval=interval, limit=48),
        _fetch_data(registry, "funding_history", symbol=sym, limit=66),
        # Bars carry taker_buy_volume (Binance kline field 9) for the CVD read.
        _fetch_data(registry, "crypto_ohlcv", symbol=sym, interval=interval, limit=100),
        return_exceptions=False,
    )
    return {
        "symbol": sym,
        "interval": interval,
        "taker_flow": taker,
        "top_trader": top,
        "funding_history": hist,
        "ohlcv": ohlcv,
        "disclaimer": DISCLAIMER,
    }


async def hl_context(symbol: str) -> dict[str, Any]:
    """Gather the Hyperliquid-native context (asset ctx + predicted funding).

    HL funding is hourly and is the venue-correct carry number for positions
    held on Hyperliquid; predicted funding adds the cross-venue forward view.
    """
    sym = canonical_crypto(symbol)
    registry = get_registry()
    ctx, predicted = await asyncio.gather(
        _fetch_data(registry, "hl_asset_ctx", symbol=sym),
        _fetch_data(registry, "hl_predicted_funding", symbol=sym),
        return_exceptions=False,
    )
    return {"symbol": sym, "asset_ctx": ctx, "predicted_funding": predicted, "disclaimer": DISCLAIMER}


async def social_scan(symbol: str = "CRYPTO", limit: int = 25) -> dict[str, Any]:
    """Deterministic social-chatter scan (Reddit velocity, StockTwits labels,
    /biz/ mentions, CoinGecko trending). Pure counting — no model anywhere."""
    registry = get_registry()
    data = await _fetch_data(registry, "social_scan", symbol=symbol or "CRYPTO", limit=limit)
    return {"symbol": symbol or "CRYPTO", "scan": data, "disclaimer": DISCLAIMER}


async def news_feed(symbol: str = "CRYPTO", limit: int = 30) -> dict[str, Any]:
    """Merged crypto news headlines (CoinTelegraph + CoinDesk + Google News),
    newest first, ASCII titles. NOT scored by the engine — host-side reading."""
    registry = get_registry()
    data = await _fetch_data(registry, "news_feed", symbol=symbol or "CRYPTO", limit=limit)
    return {"symbol": symbol or "CRYPTO", "feed": data, "disclaimer": DISCLAIMER}


async def market_pulse() -> dict[str, Any]:
    """One-call Hyperliquid universe snapshot: per-coin funding/OI/volume plus
    newly listed perps (universe diff vs the persisted snapshot)."""
    registry = get_registry()
    data = await _fetch_data(registry, "hl_market_pulse")
    return {"pulse": data, "disclaimer": DISCLAIMER}


async def orderflow(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """Order-flow snapshot for a symbol: taker flow, CVD, top-trader spread,
    book-depth imbalance, and HL-vs-CEX price divergence.

    The depth/divergence values are ORDER-TIME GATES (they decay in minutes),
    not scored factors; the flow/positioning signals mirror what the engine
    scores so a host can drill into the why.
    """
    sym = canonical_crypto(symbol)
    registry = get_registry()
    taker, top, crowd, book, ctx, funding, ohlcv = await asyncio.gather(
        _fetch_data(registry, "taker_flow", symbol=sym, interval=interval, limit=48),
        _fetch_data(registry, "top_trader_ratio", symbol=sym, interval=interval, limit=48),
        _fetch_data(registry, "long_short_ratio", symbol=sym, interval=interval),
        _fetch_data(registry, "hl_l2book", symbol=sym),
        _fetch_data(registry, "hl_asset_ctx", symbol=sym),
        _fetch_data(registry, "funding_rate", symbol=sym),
        _fetch_data(registry, "crypto_ohlcv", symbol=sym, interval=interval, limit=100),
        return_exceptions=False,
    )

    result: dict[str, Any] = {"symbol": sym, "interval": interval, "disclaimer": DISCLAIMER}

    taker_series = taker.get("series") if isinstance(taker, dict) and "_error" not in taker else None
    result["taker_flow"] = _signal_block(taker_flow_signal(taker_series) if taker_series else None)

    bars = ohlcv.get("bars") if isinstance(ohlcv, dict) and "_error" not in ohlcv else None
    result["cvd"] = _signal_block(cvd_signal(bars) if bars else None)

    top_latest: float | None = None
    if isinstance(top, dict) and "_error" not in top:
        series = top.get("series") or []
        if series and isinstance(series[-1], dict):
            top_latest = series[-1].get("ratio")
    crowd_ratio = crowd.get("ratio") if isinstance(crowd, dict) and "_error" not in crowd else None
    result["top_trader"] = _signal_block(top_trader_spread_signal(top_latest, crowd_ratio))

    mid = ctx.get("mid_price") if isinstance(ctx, dict) and "_error" not in ctx else None
    imbalance: float | None = None
    if isinstance(book, dict) and "_error" not in book and mid:
        imbalance = depth_imbalance(book.get("bids") or [], book.get("asks") or [], mid)
    result["depth"] = {"imbalance": imbalance, "band_bps": 20.0, "mid": mid}

    hl_mark = ctx.get("mark_price") if isinstance(ctx, dict) and "_error" not in ctx else None
    cex_mark = funding.get("mark_price") if isinstance(funding, dict) and "_error" not in funding else None
    result["venue"] = {
        "hl_mark": hl_mark,
        "cex_mark": cex_mark,
        "divergence_bps": venue_divergence(hl_mark, cex_mark),
    }
    return result


# ---------------------------------------------------------------------------
# MCP tool wrappers (thin)
# ---------------------------------------------------------------------------


@tool(
    "crypto_ohlcv",
    "Fetch raw perpetual OHLCV bars for a crypto symbol at an interval (1m..1d).",
    {"symbol": str, "interval": str, "limit": int},
)
async def crypto_ohlcv_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`crypto_ohlcv`."""
    result = await crypto_ohlcv(
        args["symbol"], interval=args.get("interval", "5m"), limit=int(args.get("limit", 500))
    )
    return text_result(result)


@tool(
    "crypto_indicators",
    "Compute technical indicators (rsi, macd, bbands, sma, ema, atr, stoch, adx, obv) on perp bars.",
    {"symbol": str, "interval": str, "indicators": list},
)
async def crypto_indicators_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`crypto_indicators`."""
    result = await crypto_indicators(
        args["symbol"], interval=args.get("interval", "15m"), indicators=args.get("indicators")
    )
    return text_result(result)


@tool(
    "crypto_signals",
    "Detect technical signals (cross, RSI extremes, MACD cross, Bollinger breaks) on perp bars.",
    {"symbol": str, "interval": str},
)
async def crypto_signals_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`crypto_signals`."""
    result = await crypto_signals(args["symbol"], interval=args.get("interval", "15m"))
    return text_result(result)


@tool(
    "multi_timeframe",
    "Summarize trend and momentum across the 5m, 15m, and 1h timeframes for a crypto symbol.",
    {"symbol": str},
)
async def multi_timeframe_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`multi_timeframe`."""
    result = await multi_timeframe(args["symbol"])
    return text_result(result)


@tool(
    "derivatives",
    "Funding rate, open interest (+ change), long/short ratio, and basis for a crypto perpetual.",
    {"symbol": str, "interval": str},
)
async def derivatives_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`derivatives`."""
    result = await derivatives(args["symbol"], interval=args.get("interval", "5m"))
    return text_result(result)


@tool(
    "crypto_sentiment",
    "Crypto Fear & Greed Index plus global market context (total cap, BTC dominance).",
    {},
)
async def crypto_sentiment_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`crypto_sentiment`."""
    result = await crypto_sentiment()
    return text_result(result)


@tool(
    "social_scan",
    "Deterministic social-chatter scan: Reddit velocity, StockTwits bull/bear labels, /biz/ mentions, trending.",
    {"symbol": str, "limit": int},
)
async def social_scan_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`social_scan`."""
    result = await social_scan(args.get("symbol") or "CRYPTO", limit=int(args.get("limit", 25)))
    return text_result(result)


@tool(
    "news_feed",
    "Merged crypto news headlines (CoinTelegraph, CoinDesk, Google News), newest first.",
    {"symbol": str, "limit": int},
)
async def news_feed_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`news_feed`."""
    result = await news_feed(args.get("symbol") or "CRYPTO", limit=int(args.get("limit", 30)))
    return text_result(result)


@tool(
    "market_pulse",
    "Hyperliquid universe snapshot: per-coin funding/OI/volume/movers plus newly listed perps.",
    {},
)
async def market_pulse_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`market_pulse`."""
    result = await market_pulse()
    return text_result(result)


@tool(
    "orderflow",
    "Order-flow snapshot: taker flow, CVD, top-trader spread, book-depth imbalance, venue divergence.",
    {"symbol": str, "interval": str},
)
async def orderflow_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`orderflow`."""
    result = await orderflow(args["symbol"], interval=args.get("interval", "15m"))
    return text_result(result)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

server = create_sdk_mcp_server(
    name="crypto",
    version="0.1.0",
    tools=[
        crypto_ohlcv_tool,
        crypto_indicators_tool,
        crypto_signals_tool,
        multi_timeframe_tool,
        derivatives_tool,
        crypto_sentiment_tool,
        social_scan_tool,
        news_feed_tool,
        market_pulse_tool,
        orderflow_tool,
    ],
)


__all__ = [
    "get_registry",
    "crypto_ohlcv",
    "crypto_indicators",
    "crypto_signals",
    "multi_timeframe",
    "derivatives",
    "crypto_sentiment",
    "flow_metrics",
    "hl_context",
    "social_scan",
    "news_feed",
    "market_pulse",
    "orderflow",
    "crypto_ohlcv_tool",
    "crypto_indicators_tool",
    "crypto_signals_tool",
    "multi_timeframe_tool",
    "derivatives_tool",
    "crypto_sentiment_tool",
    "social_scan_tool",
    "news_feed_tool",
    "market_pulse_tool",
    "orderflow_tool",
    "server",
]


# ---------------------------------------------------------------------------
# stdio runner (guarded)
# ---------------------------------------------------------------------------


def _main() -> int:
    """Run the crypto server over stdio; require the real SDK."""
    from ._sdk import SDK_AVAILABLE

    if not SDK_AVAILABLE:
        print(
            "The Claude Agent SDK is not installed. Install it with "
            "'pip install claude-agent-sdk' to run the crypto MCP server.",
        )
        return 1

    import claude_agent_sdk  # type: ignore

    runner = getattr(claude_agent_sdk, "run_mcp_server_stdio", None) or getattr(
        claude_agent_sdk, "run_stdio_server", None
    )
    if runner is None:
        print(
            "The installed Claude Agent SDK does not expose a known stdio "
            "server runner; cannot start the crypto MCP server.",
        )
        return 1

    result = runner(server)
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
