"""Technical-analysis capability server (see CONTRACT.md §9.2).

Exposes OHLCV retrieval and a battery of technical-analysis tools built on the
pure-Python ``ta`` library + ``pandas``. Both heavy libraries are *lazy
imported* inside the function bodies so this module imports cleanly without
them (and without the Claude Agent SDK or any network access), as required by
the global engineering mandates (CONTRACT.md §2).

Layout (the per-server pattern, CONTRACT.md §9.1):
  * Pure ``async def`` logic functions that call :func:`get_registry`'s
    ``fetch("ohlcv", ...)`` and shape a compact result ``dict``. These are
    importable and unit-testable with a monkeypatched module-level
    :func:`get_registry`.
  * Thin ``@tool``-decorated wrappers that call the logic functions and wrap the
    result with :func:`text_result`.
  * A ``create_sdk_mcp_server(name="technical", ...)`` instance.
  * A guarded ``__main__`` stdio runner.

All numeric outputs are plain Python floats (NaN -> ``None``) so the result is
directly JSON-serializable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.indicators import (
    DEFAULT_INDICATORS,
    clean_float as _clean_float,
    compute_indicator_frame as _compute_indicator_frame,
    ohlcv_to_dataframe as _ohlcv_to_dataframe,
    signals_from_frame as _signals_from_frame,
    summarize_timeframe as _summarize_timeframe,
)
from ..core.disclaimer import DISCLAIMER
from ..providers import get_registry as _provider_get_registry
from ._common import normalize_symbol, text_result
from ._sdk import create_sdk_mcp_server, tool

# Timeframes summarized by :func:`multi_timeframe_summary`.
_MTF_TIMEFRAMES: tuple[tuple[str, str, str], ...] = (
    # (label, interval, period)
    ("daily", "1d", "6mo"),
    ("weekly", "1wk", "2y"),
    ("monthly", "1mo", "5y"),
)


# ---------------------------------------------------------------------------
# Monkeypatchable registry indirection (CONTRACT.md §9.1.2)
# ---------------------------------------------------------------------------


def get_registry() -> Any:
    """Return the shared :class:`ProviderRegistry`.

    Thin indirection over :func:`makecrazypenny.providers.get_registry` so tests
    can monkeypatch *this* module's ``get_registry`` to inject a fake registry
    without touching the providers package.

    Returns:
        The process-wide provider registry (or a test fake).
    """
    return _provider_get_registry()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_ohlcv_frame(
    symbol: str, interval: str, period: str
) -> tuple[Any, dict[str, Any]]:
    """Fetch OHLCV for ``symbol`` and return ``(DataFrame, envelope)``.

    Calls ``get_registry().fetch("ohlcv", ...)`` (awaitable) and converts the
    payload to a pandas DataFrame. ``pandas`` is lazy-imported via
    :func:`_ohlcv_to_dataframe`.

    Returns:
        A tuple of the constructed DataFrame and the raw registry envelope
        (``{"provider", "data", "cached"}``).
    """
    registry = get_registry()
    envelope = await registry.fetch("ohlcv", symbol=symbol, interval=interval, period=period)
    data = envelope.get("data") or {}
    df = _ohlcv_to_dataframe(data)
    return df, envelope


def _meta(symbol: str, interval: str, envelope: dict[str, Any], df: Any) -> dict[str, Any]:
    """Build the common metadata block shared by every tool result."""
    return {
        "symbol": symbol,
        "interval": interval,
        "provider": envelope.get("provider"),
        "cached": envelope.get("cached"),
        "n_bars": int(len(df)),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Pure async logic functions (testable; CONTRACT.md §9.1.1)
# ---------------------------------------------------------------------------


async def get_ohlcv(symbol: str, interval: str = "1d", period: str = "6mo") -> dict[str, Any]:
    """Fetch raw OHLCV bars for ``symbol``.

    Args:
        symbol: Ticker (normalized: uppercased, ``$`` stripped).
        interval: Bar interval (e.g. ``"1d"``, ``"1wk"``).
        period: Look-back window (e.g. ``"6mo"``, ``"1y"``).

    Returns:
        A dict with metadata plus a ``bars`` list of OHLCV dicts.
    """
    sym = normalize_symbol(symbol)
    df, envelope = await _fetch_ohlcv_frame(sym, interval, period)
    data = envelope.get("data") or {}
    result = _meta(sym, interval, envelope, df)
    result["period"] = period
    result["bars"] = data.get("bars") or []
    return result


async def compute_indicators(
    symbol: str, indicators: list[str] | None = None
) -> dict[str, Any]:
    """Compute technical indicators for ``symbol`` on daily bars.

    Supported indicators: ``rsi, macd, bbands, sma, ema, atr, stoch, adx, obv``
    (computed via the pure-Python ``ta`` library on a pandas DataFrame).

    Args:
        symbol: Ticker.
        indicators: Subset of supported indicator names; ``None`` => all.

    Returns:
        A dict with metadata plus an ``indicators`` block of latest values.
    """
    sym = normalize_symbol(symbol)
    names = list(indicators) if indicators else list(DEFAULT_INDICATORS)
    df, envelope = await _fetch_ohlcv_frame(sym, "1d", "1y")
    result = _meta(sym, "1d", envelope, df)
    result["requested"] = names
    if len(df) == 0:
        result["indicators"] = {}
        return result
    result["indicators"] = _compute_indicator_frame(df, names)
    return result


async def detect_signals(symbol: str) -> dict[str, Any]:
    """Detect common technical signals for ``symbol``.

    Signals: golden/death cross (SMA50 vs SMA200), RSI extremes (<30 / >70),
    MACD line/signal cross, and Bollinger-band breaks (close beyond a band).

    Args:
        symbol: Ticker.

    Returns:
        A dict with metadata plus a ``signals`` list (each ``{name, direction,
        detail}``) and the latest ``values`` used to derive them.
    """
    sym = normalize_symbol(symbol)
    df, envelope = await _fetch_ohlcv_frame(sym, "1d", "1y")
    result = _meta(sym, "1d", envelope, df)
    signals, values = _signals_from_frame(df)
    result["values"] = values
    result["signals"] = signals
    return result


async def support_resistance(symbol: str) -> dict[str, Any]:
    """Estimate recent support and resistance levels for ``symbol``.

    Uses the recent (up to 60-bar) window: support = recent low, resistance =
    recent high, plus the classic pivot point and its R1/S1 derivatives from the
    most recent bar.

    Args:
        symbol: Ticker.

    Returns:
        A dict with metadata plus ``support``, ``resistance``, ``pivot``,
        ``r1``, ``s1`` and the ``window`` size used.
    """
    sym = normalize_symbol(symbol)
    df, envelope = await _fetch_ohlcv_frame(sym, "1d", "6mo")
    result = _meta(sym, "1d", envelope, df)

    if len(df) == 0 or not {"high", "low", "close"}.issubset(df.columns):
        result.update(
            {
                "support": None,
                "resistance": None,
                "pivot": None,
                "r1": None,
                "s1": None,
                "window": 0,
            }
        )
        return result

    window = min(60, len(df))
    recent = df.iloc[-window:]
    support = _clean_float(recent["low"].min())
    resistance = _clean_float(recent["high"].max())

    last = df.iloc[-1]
    h = _clean_float(last["high"])
    low_v = _clean_float(last["low"])
    c = _clean_float(last["close"])
    pivot = r1 = s1 = None
    if None not in (h, low_v, c):
        pivot = (h + low_v + c) / 3.0
        r1 = (2.0 * pivot) - low_v
        s1 = (2.0 * pivot) - h

    result.update(
        {
            "support": support,
            "resistance": resistance,
            "pivot": _clean_float(pivot),
            "r1": _clean_float(r1),
            "s1": _clean_float(s1),
            "window": int(window),
        }
    )
    return result


async def multi_timeframe_summary(symbol: str) -> dict[str, Any]:
    """Summarize trend/momentum across daily, weekly, and monthly timeframes.

    For each timeframe, fetches OHLCV and computes a compact snapshot (latest
    close, SMA20/SMA50, RSI, and a simple bullish/bearish/neutral trend read
    from close vs. SMA50 and SMA20 vs. SMA50).

    Args:
        symbol: Ticker.

    Returns:
        A dict with metadata plus a ``timeframes`` map keyed by label.
    """
    sym = normalize_symbol(symbol)
    timeframes: dict[str, Any] = {}
    last_provider: str | None = None
    any_cached: bool = False

    for label, interval, period in _MTF_TIMEFRAMES:
        try:
            df, envelope = await _fetch_ohlcv_frame(sym, interval, period)
        except Exception as exc:  # one timeframe failing must not kill the rest
            timeframes[label] = {"interval": interval, "error": str(exc)}
            continue
        last_provider = envelope.get("provider") or last_provider
        any_cached = any_cached or bool(envelope.get("cached"))
        timeframes[label] = _summarize_timeframe(df, interval)

    result = {
        "symbol": sym,
        "provider": last_provider,
        "cached": any_cached,
        "timeframes": timeframes,
        "disclaimer": DISCLAIMER,
    }
    return result


# ---------------------------------------------------------------------------
# MCP tool wrappers (thin; CONTRACT.md §9.1.3)
# ---------------------------------------------------------------------------


@tool(
    "get_ohlcv",
    "Fetch raw OHLCV (open/high/low/close/volume) price bars for a symbol.",
    {"symbol": str, "interval": str, "period": str},
)
async def get_ohlcv_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`get_ohlcv`."""
    result = await get_ohlcv(
        args["symbol"],
        interval=args.get("interval", "1d"),
        period=args.get("period", "6mo"),
    )
    return text_result(result)


@tool(
    "compute_indicators",
    "Compute technical indicators (rsi, macd, bbands, sma, ema, atr, stoch, adx, obv).",
    {"symbol": str, "indicators": list},
)
async def compute_indicators_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`compute_indicators`."""
    result = await compute_indicators(args["symbol"], indicators=args.get("indicators"))
    return text_result(result)


@tool(
    "detect_signals",
    "Detect technical signals: golden/death cross, RSI extremes, MACD cross, Bollinger breaks.",
    {"symbol": str},
)
async def detect_signals_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`detect_signals`."""
    result = await detect_signals(args["symbol"])
    return text_result(result)


@tool(
    "support_resistance",
    "Estimate recent support/resistance levels and classic pivot points for a symbol.",
    {"symbol": str},
)
async def support_resistance_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`support_resistance`."""
    result = await support_resistance(args["symbol"])
    return text_result(result)


@tool(
    "multi_timeframe_summary",
    "Summarize trend and momentum across daily, weekly, and monthly timeframes.",
    {"symbol": str},
)
async def multi_timeframe_summary_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`multi_timeframe_summary`."""
    result = await multi_timeframe_summary(args["symbol"])
    return text_result(result)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

server = create_sdk_mcp_server(
    name="technical",
    version="0.1.0",
    tools=[
        get_ohlcv_tool,
        compute_indicators_tool,
        detect_signals_tool,
        support_resistance_tool,
        multi_timeframe_summary_tool,
    ],
)


__all__ = [
    "get_registry",
    "get_ohlcv",
    "compute_indicators",
    "detect_signals",
    "support_resistance",
    "multi_timeframe_summary",
    "get_ohlcv_tool",
    "compute_indicators_tool",
    "detect_signals_tool",
    "support_resistance_tool",
    "multi_timeframe_summary_tool",
    "server",
    "DEFAULT_INDICATORS",
]


# ---------------------------------------------------------------------------
# stdio runner (guarded; CONTRACT.md §9.1.4)
# ---------------------------------------------------------------------------


def _main() -> int:
    """Run the technical server over stdio; require the real SDK."""
    from ._sdk import SDK_AVAILABLE

    if not SDK_AVAILABLE:
        print(
            "The Claude Agent SDK is not installed. Install it with "
            "'pip install claude-agent-sdk' to run the technical MCP server.",
        )
        return 1

    import claude_agent_sdk  # type: ignore

    # The SDK exposes a stdio runner for an in-process MCP server; resolve it
    # defensively so a future rename surfaces a clear message rather than an
    # AttributeError.
    runner = getattr(claude_agent_sdk, "run_mcp_server_stdio", None) or getattr(
        claude_agent_sdk, "run_stdio_server", None
    )
    if runner is None:
        print(
            "The installed Claude Agent SDK does not expose a known stdio "
            "server runner; cannot start the technical MCP server.",
        )
        return 1

    result = runner(server)
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
