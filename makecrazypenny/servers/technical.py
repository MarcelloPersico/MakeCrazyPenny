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

from ..core.disclaimer import DISCLAIMER
from ..providers import get_registry as _provider_get_registry
from ._common import normalize_symbol, text_result
from ._sdk import create_sdk_mcp_server, tool

# Default indicator set for :func:`compute_indicators` (CONTRACT.md §9.2).
DEFAULT_INDICATORS: tuple[str, ...] = (
    "rsi",
    "macd",
    "bbands",
    "sma",
    "ema",
    "atr",
    "stoch",
    "adx",
    "obv",
)

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


def _clean_float(value: Any) -> float | None:
    """Coerce ``value`` to a JSON-safe float, mapping NaN/inf/None to ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN != NaN; also reject infinities which are not valid JSON.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _last_clean(series: Any) -> float | None:
    """Return the last non-NaN value of a pandas Series as a JSON-safe float."""
    if series is None:
        return None
    try:
        dropped = series.dropna()
    except AttributeError:
        return _clean_float(series)
    if len(dropped) == 0:
        return None
    return _clean_float(dropped.iloc[-1])


def _ohlcv_to_dataframe(data: dict[str, Any]) -> Any:
    """Build a ``pandas.DataFrame`` from an ``OHLCV.to_dict()`` payload.

    Lazy-imports ``pandas``. The frame is indexed by timestamp (parsed when
    possible) and has lowercase ``open/high/low/close/volume`` columns sorted in
    ascending time order.

    Args:
        data: The ``data`` field of a ``registry.fetch("ohlcv", ...)`` envelope
            (an ``OHLCV.to_dict()`` dict with a ``bars`` list).

    Returns:
        A ``pandas.DataFrame``. Empty (no rows) when there are no bars.
    """
    import pandas as pd

    bars = data.get("bars") or []
    columns = ["open", "high", "low", "close", "volume"]
    if not bars:
        empty = pd.DataFrame(columns=columns)
        empty.index = pd.to_datetime([])
        return empty

    df = pd.DataFrame(bars)
    # Index by timestamp; tolerate unparseable timestamps by keeping raw values.
    if "ts" in df.columns:
        idx = pd.to_datetime(df["ts"], errors="coerce", utc=True)
        if idx.isna().all():
            idx = df["ts"]
        df = df.drop(columns=["ts"])
        df.index = idx
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_index()
    return df


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
# Indicator computation (pure, given a DataFrame)
# ---------------------------------------------------------------------------


def _compute_indicator_frame(df: Any, names: list[str]) -> dict[str, Any]:
    """Compute the requested indicators on ``df``; return latest-value summary.

    Lazy-imports ``ta``. Each requested indicator contributes one or more
    latest scalar values to the returned dict. Unknown indicator names are
    recorded under ``"unknown"``.

    Args:
        df: A DataFrame with ``open/high/low/close/volume`` columns.
        names: Lowercased indicator names to compute.

    Returns:
        A dict mapping indicator name -> latest value(s) (JSON-safe floats).
    """
    from ta import momentum, trend, volatility
    from ta import volume as ta_volume

    close = df["close"] if "close" in df.columns else None
    high = df["high"] if "high" in df.columns else None
    low = df["low"] if "low" in df.columns else None
    vol = df["volume"] if "volume" in df.columns else None

    out: dict[str, Any] = {}
    unknown: list[str] = []

    for name in names:
        key = name.strip().lower()
        if key == "rsi" and close is not None:
            out["rsi"] = _last_clean(momentum.RSIIndicator(close=close).rsi())
        elif key == "macd" and close is not None:
            macd = trend.MACD(close=close)
            out["macd"] = {
                "macd": _last_clean(macd.macd()),
                "signal": _last_clean(macd.macd_signal()),
                "hist": _last_clean(macd.macd_diff()),
            }
        elif key == "bbands" and close is not None:
            bb = volatility.BollingerBands(close=close)
            out["bbands"] = {
                "upper": _last_clean(bb.bollinger_hband()),
                "middle": _last_clean(bb.bollinger_mavg()),
                "lower": _last_clean(bb.bollinger_lband()),
            }
        elif key == "sma" and close is not None:
            out["sma"] = {
                "sma20": _last_clean(trend.SMAIndicator(close=close, window=20).sma_indicator()),
                "sma50": _last_clean(trend.SMAIndicator(close=close, window=50).sma_indicator()),
                "sma200": _last_clean(
                    trend.SMAIndicator(close=close, window=200).sma_indicator()
                ),
            }
        elif key == "ema" and close is not None:
            out["ema"] = {
                "ema12": _last_clean(trend.EMAIndicator(close=close, window=12).ema_indicator()),
                "ema26": _last_clean(trend.EMAIndicator(close=close, window=26).ema_indicator()),
                "ema50": _last_clean(trend.EMAIndicator(close=close, window=50).ema_indicator()),
            }
        elif key == "atr" and close is not None and high is not None and low is not None:
            out["atr"] = _last_clean(
                volatility.AverageTrueRange(high=high, low=low, close=close).average_true_range()
            )
        elif key == "stoch" and close is not None and high is not None and low is not None:
            stoch = momentum.StochasticOscillator(high=high, low=low, close=close)
            out["stoch"] = {
                "k": _last_clean(stoch.stoch()),
                "d": _last_clean(stoch.stoch_signal()),
            }
        elif key == "adx" and close is not None and high is not None and low is not None:
            out["adx"] = _last_clean(trend.ADXIndicator(high=high, low=low, close=close).adx())
        elif key == "obv" and close is not None and vol is not None:
            out["obv"] = _last_clean(
                ta_volume.OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
            )
        else:
            unknown.append(key)

    if unknown:
        out["unknown"] = unknown
    return out


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
    signals: list[dict[str, Any]] = []
    values: dict[str, Any] = {}

    if len(df) >= 2 and "close" in df.columns:
        from ta import momentum, trend, volatility

        close = df["close"]

        sma50 = trend.SMAIndicator(close=close, window=50).sma_indicator()
        sma200 = trend.SMAIndicator(close=close, window=200).sma_indicator()
        rsi = momentum.RSIIndicator(close=close).rsi()
        macd_ind = trend.MACD(close=close)
        macd_line = macd_ind.macd()
        macd_signal = macd_ind.macd_signal()
        bb = volatility.BollingerBands(close=close)
        bb_high = bb.bollinger_hband()
        bb_low = bb.bollinger_lband()

        values = {
            "close": _last_clean(close),
            "sma50": _last_clean(sma50),
            "sma200": _last_clean(sma200),
            "rsi": _last_clean(rsi),
            "macd": _last_clean(macd_line),
            "macd_signal": _last_clean(macd_signal),
            "bb_upper": _last_clean(bb_high),
            "bb_lower": _last_clean(bb_low),
        }

        # Golden / death cross (SMA50 vs SMA200): compare last two valid points.
        cross = _detect_cross(sma50, sma200)
        if cross == "up":
            signals.append(
                {
                    "name": "golden_cross",
                    "direction": "bullish",
                    "detail": "SMA50 crossed above SMA200",
                }
            )
        elif cross == "down":
            signals.append(
                {
                    "name": "death_cross",
                    "direction": "bearish",
                    "detail": "SMA50 crossed below SMA200",
                }
            )

        # RSI extremes.
        rsi_val = values["rsi"]
        if rsi_val is not None and rsi_val < 30:
            signals.append(
                {
                    "name": "rsi_oversold",
                    "direction": "bullish",
                    "detail": f"RSI {rsi_val:.1f} < 30",
                }
            )
        elif rsi_val is not None and rsi_val > 70:
            signals.append(
                {
                    "name": "rsi_overbought",
                    "direction": "bearish",
                    "detail": f"RSI {rsi_val:.1f} > 70",
                }
            )

        # MACD cross.
        macd_cross = _detect_cross(macd_line, macd_signal)
        if macd_cross == "up":
            signals.append(
                {
                    "name": "macd_bullish_cross",
                    "direction": "bullish",
                    "detail": "MACD crossed above signal",
                }
            )
        elif macd_cross == "down":
            signals.append(
                {
                    "name": "macd_bearish_cross",
                    "direction": "bearish",
                    "detail": "MACD crossed below signal",
                }
            )

        # Bollinger-band breaks (latest close beyond a band).
        last_close = values["close"]
        if last_close is not None and values["bb_upper"] is not None and (
            last_close > values["bb_upper"]
        ):
            signals.append(
                {
                    "name": "bollinger_break_up",
                    "direction": "bullish",
                    "detail": "Close above upper Bollinger band",
                }
            )
        elif last_close is not None and values["bb_lower"] is not None and (
            last_close < values["bb_lower"]
        ):
            signals.append(
                {
                    "name": "bollinger_break_down",
                    "direction": "bearish",
                    "detail": "Close below lower Bollinger band",
                }
            )

    result["values"] = values
    result["signals"] = signals
    return result


def _detect_cross(fast: Any, slow: Any) -> str | None:
    """Return ``"up"``/``"down"``/``None`` for the latest cross of two series.

    ``"up"`` when ``fast`` was at/below ``slow`` on the prior valid point and is
    now above it; ``"down"`` for the opposite. Requires two aligned valid
    points; otherwise ``None``.
    """
    try:
        import pandas as pd

        aligned = pd.concat([fast, slow], axis=1).dropna()
    except Exception:
        return None
    if len(aligned) < 2:
        return None
    prev_fast = _clean_float(aligned.iloc[-2, 0])
    prev_slow = _clean_float(aligned.iloc[-2, 1])
    cur_fast = _clean_float(aligned.iloc[-1, 0])
    cur_slow = _clean_float(aligned.iloc[-1, 1])
    if None in (prev_fast, prev_slow, cur_fast, cur_slow):
        return None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return "up"
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return "down"
    return None


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


def _summarize_timeframe(df: Any, interval: str) -> dict[str, Any]:
    """Build a compact trend/momentum snapshot for one timeframe DataFrame."""
    snap: dict[str, Any] = {"interval": interval, "n_bars": int(len(df))}
    if len(df) == 0 or "close" not in df.columns:
        snap.update({"close": None, "sma20": None, "sma50": None, "rsi": None, "trend": "unknown"})
        return snap

    from ta import momentum, trend

    close = df["close"]
    sma20 = _last_clean(trend.SMAIndicator(close=close, window=20).sma_indicator())
    sma50 = _last_clean(trend.SMAIndicator(close=close, window=50).sma_indicator())
    rsi = _last_clean(momentum.RSIIndicator(close=close).rsi())
    last_close = _last_clean(close)

    direction = "neutral"
    if last_close is not None and sma50 is not None and sma20 is not None:
        if last_close > sma50 and sma20 > sma50:
            direction = "bullish"
        elif last_close < sma50 and sma20 < sma50:
            direction = "bearish"
    elif last_close is not None and sma20 is not None:
        direction = "bullish" if last_close > sma20 else "bearish"

    snap.update(
        {
            "close": last_close,
            "sma20": sma20,
            "sma50": sma50,
            "rsi": rsi,
            "trend": direction,
        }
    )
    return snap


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
