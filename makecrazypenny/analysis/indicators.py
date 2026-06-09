"""Shared technical-indicator helpers (CONTRACT.md §16).

Pure, asset-agnostic computation over an ``OHLCV.to_dict()`` payload: building a
pandas frame, computing the ``ta``-library indicators, detecting common signals,
and summarizing a timeframe. Extracted from ``servers/technical.py`` so the
equity ``technical`` server and the crypto ``crypto`` server can both reuse it
without one server importing another (the dependency rule allows servers to
share *analysis* logic, not each other's MCP wiring).

``pandas``/``ta`` are imported lazily inside the functions, so importing this
module stays light and never hits the network. Every numeric output is a plain
JSON-safe float (NaN/inf -> ``None``).
"""

from __future__ import annotations

from typing import Any

#: Default indicator set (CONTRACT.md §9.2).
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


def clean_float(value: Any) -> float | None:
    """Coerce ``value`` to a JSON-safe float, mapping NaN/inf/None to ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def last_clean(series: Any) -> float | None:
    """Return the last non-NaN value of a pandas Series as a JSON-safe float."""
    if series is None:
        return None
    try:
        dropped = series.dropna()
    except AttributeError:
        return clean_float(series)
    if len(dropped) == 0:
        return None
    return clean_float(dropped.iloc[-1])


def ohlcv_to_dataframe(data: dict[str, Any]) -> Any:
    """Build a ``pandas.DataFrame`` from an ``OHLCV.to_dict()`` payload.

    Lazy-imports ``pandas``. The frame is indexed by timestamp (parsed when
    possible) with lowercase ``open/high/low/close/volume`` columns in ascending
    time order. Empty (no rows) when there are no bars.
    """
    import pandas as pd

    bars = data.get("bars") or []
    columns = ["open", "high", "low", "close", "volume"]
    if not bars:
        empty = pd.DataFrame(columns=columns)
        empty.index = pd.to_datetime([])
        return empty

    df = pd.DataFrame(bars)
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


def compute_indicator_frame(df: Any, names: list[str]) -> dict[str, Any]:
    """Compute the requested indicators on ``df``; return a latest-value summary.

    Lazy-imports ``ta``. Each indicator contributes one or more latest scalar
    values; unknown names are collected under ``"unknown"``.
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
            out["rsi"] = last_clean(momentum.RSIIndicator(close=close).rsi())
        elif key == "macd" and close is not None:
            macd = trend.MACD(close=close)
            out["macd"] = {
                "macd": last_clean(macd.macd()),
                "signal": last_clean(macd.macd_signal()),
                "hist": last_clean(macd.macd_diff()),
            }
        elif key == "bbands" and close is not None:
            bb = volatility.BollingerBands(close=close)
            out["bbands"] = {
                "upper": last_clean(bb.bollinger_hband()),
                "middle": last_clean(bb.bollinger_mavg()),
                "lower": last_clean(bb.bollinger_lband()),
            }
        elif key == "sma" and close is not None:
            out["sma"] = {
                "sma20": last_clean(trend.SMAIndicator(close=close, window=20).sma_indicator()),
                "sma50": last_clean(trend.SMAIndicator(close=close, window=50).sma_indicator()),
                "sma200": last_clean(trend.SMAIndicator(close=close, window=200).sma_indicator()),
            }
        elif key == "ema" and close is not None:
            out["ema"] = {
                "ema12": last_clean(trend.EMAIndicator(close=close, window=12).ema_indicator()),
                "ema26": last_clean(trend.EMAIndicator(close=close, window=26).ema_indicator()),
                "ema50": last_clean(trend.EMAIndicator(close=close, window=50).ema_indicator()),
            }
        elif key == "atr" and close is not None and high is not None and low is not None:
            out["atr"] = last_clean(
                volatility.AverageTrueRange(high=high, low=low, close=close).average_true_range()
            )
        elif key == "stoch" and close is not None and high is not None and low is not None:
            stoch = momentum.StochasticOscillator(high=high, low=low, close=close)
            out["stoch"] = {
                "k": last_clean(stoch.stoch()),
                "d": last_clean(stoch.stoch_signal()),
            }
        elif key == "adx" and close is not None and high is not None and low is not None:
            out["adx"] = last_clean(trend.ADXIndicator(high=high, low=low, close=close).adx())
        elif key == "obv" and close is not None and vol is not None:
            out["obv"] = last_clean(
                ta_volume.OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
            )
        else:
            unknown.append(key)

    if unknown:
        out["unknown"] = unknown
    return out


def detect_cross(fast: Any, slow: Any) -> str | None:
    """Return ``"up"``/``"down"``/``None`` for the latest cross of two series.

    ``"up"`` when ``fast`` was at/below ``slow`` on the prior valid point and is
    now above it; ``"down"`` for the opposite. Requires two aligned valid points.
    """
    try:
        import pandas as pd

        aligned = pd.concat([fast, slow], axis=1).dropna()
    except Exception:
        return None
    if len(aligned) < 2:
        return None
    prev_fast = clean_float(aligned.iloc[-2, 0])
    prev_slow = clean_float(aligned.iloc[-2, 1])
    cur_fast = clean_float(aligned.iloc[-1, 0])
    cur_slow = clean_float(aligned.iloc[-1, 1])
    if None in (prev_fast, prev_slow, cur_fast, cur_slow):
        return None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return "up"
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return "down"
    return None


def signals_from_frame(df: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Detect common technical signals on ``df``; return ``(signals, values)``.

    Signals: golden/death cross (SMA50 vs SMA200), RSI extremes (<30 / >70),
    MACD line/signal cross, and Bollinger-band breaks. ``values`` holds the
    latest scalars used to derive them. Works on any OHLCV frame (equity or
    crypto); the caller decides the interval/period it was built from.
    """
    signals: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    if len(df) < 2 or "close" not in df.columns:
        return signals, values

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
        "close": last_clean(close),
        "sma50": last_clean(sma50),
        "sma200": last_clean(sma200),
        "rsi": last_clean(rsi),
        "macd": last_clean(macd_line),
        "macd_signal": last_clean(macd_signal),
        "bb_upper": last_clean(bb_high),
        "bb_lower": last_clean(bb_low),
    }

    cross = detect_cross(sma50, sma200)
    if cross == "up":
        signals.append({"name": "golden_cross", "direction": "bullish", "detail": "SMA50 crossed above SMA200"})
    elif cross == "down":
        signals.append({"name": "death_cross", "direction": "bearish", "detail": "SMA50 crossed below SMA200"})

    rsi_val = values["rsi"]
    if rsi_val is not None and rsi_val < 30:
        signals.append({"name": "rsi_oversold", "direction": "bullish", "detail": f"RSI {rsi_val:.1f} < 30"})
    elif rsi_val is not None and rsi_val > 70:
        signals.append({"name": "rsi_overbought", "direction": "bearish", "detail": f"RSI {rsi_val:.1f} > 70"})

    macd_cross = detect_cross(macd_line, macd_signal)
    if macd_cross == "up":
        signals.append({"name": "macd_bullish_cross", "direction": "bullish", "detail": "MACD crossed above signal"})
    elif macd_cross == "down":
        signals.append({"name": "macd_bearish_cross", "direction": "bearish", "detail": "MACD crossed below signal"})

    last_close = values["close"]
    if last_close is not None and values["bb_upper"] is not None and last_close > values["bb_upper"]:
        signals.append({"name": "bollinger_break_up", "direction": "bullish", "detail": "Close above upper Bollinger band"})
    elif last_close is not None and values["bb_lower"] is not None and last_close < values["bb_lower"]:
        signals.append({"name": "bollinger_break_down", "direction": "bearish", "detail": "Close below lower Bollinger band"})

    return signals, values


def summarize_timeframe(df: Any, interval: str) -> dict[str, Any]:
    """Build a compact trend/momentum snapshot for one timeframe DataFrame."""
    snap: dict[str, Any] = {"interval": interval, "n_bars": int(len(df))}
    if len(df) == 0 or "close" not in df.columns:
        snap.update({"close": None, "sma20": None, "sma50": None, "rsi": None, "trend": "unknown"})
        return snap

    from ta import momentum, trend

    close = df["close"]
    sma20 = last_clean(trend.SMAIndicator(close=close, window=20).sma_indicator())
    sma50 = last_clean(trend.SMAIndicator(close=close, window=50).sma_indicator())
    rsi = last_clean(momentum.RSIIndicator(close=close).rsi())
    last_close = last_clean(close)

    direction = "neutral"
    if last_close is not None and sma50 is not None and sma20 is not None:
        if last_close > sma50 and sma20 > sma50:
            direction = "bullish"
        elif last_close < sma50 and sma20 < sma50:
            direction = "bearish"
    elif last_close is not None and sma20 is not None:
        direction = "bullish" if last_close > sma20 else "bearish"

    snap.update({"close": last_close, "sma20": sma20, "sma50": sma50, "rsi": rsi, "trend": direction})
    return snap


__all__ = [
    "DEFAULT_INDICATORS",
    "clean_float",
    "last_clean",
    "ohlcv_to_dataframe",
    "compute_indicator_frame",
    "detect_cross",
    "signals_from_frame",
    "summarize_timeframe",
]
