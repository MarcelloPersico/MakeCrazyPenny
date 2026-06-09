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

from ..analysis.crypto_metrics import basis_value, oi_change_from_history
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
    "crypto_ohlcv_tool",
    "crypto_indicators_tool",
    "crypto_signals_tool",
    "multi_timeframe_tool",
    "derivatives_tool",
    "crypto_sentiment_tool",
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
