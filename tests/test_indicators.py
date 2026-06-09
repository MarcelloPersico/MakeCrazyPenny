"""Tests for the shared indicator helpers (CONTRACT.md §16). Offline (uses ta/pandas)."""

from __future__ import annotations

from typing import Any

from makecrazypenny.analysis.indicators import (
    DEFAULT_INDICATORS,
    compute_indicator_frame,
    detect_cross,
    ohlcv_to_dataframe,
    signals_from_frame,
    summarize_timeframe,
)


def _bars(closes: list[float]) -> dict[str, Any]:
    """Build an OHLCV.to_dict()-style payload from a close series."""
    return {
        "bars": [
            {
                "ts": f"2024-01-01T00:{i:02d}:00+00:00",
                "open": c,
                "high": c + 1,
                "low": c - 1,
                "close": c,
                "volume": 1000,
            }
            for i, c in enumerate(closes)
        ]
    }


def test_ohlcv_to_dataframe_columns_and_empty() -> None:
    df = ohlcv_to_dataframe(_bars([10, 11, 12]))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 3
    empty = ohlcv_to_dataframe({"bars": []})
    assert len(empty) == 0


def test_compute_indicator_frame_keys_and_unknown() -> None:
    df = ohlcv_to_dataframe(_bars([100 + (i % 5) for i in range(60)]))
    out = compute_indicator_frame(df, ["rsi", "macd", "bogus"])
    assert "rsi" in out and "macd" in out
    assert "macd" in out["macd"] and "signal" in out["macd"]
    assert out["unknown"] == ["bogus"]
    # Full default set computes without error.
    full = compute_indicator_frame(df, list(DEFAULT_INDICATORS))
    assert {"rsi", "macd", "bbands", "sma", "ema", "atr"} <= set(full)


def test_signals_from_frame_structure_and_short_data() -> None:
    signals, values = signals_from_frame(ohlcv_to_dataframe(_bars(list(range(100, 360)))))
    assert isinstance(signals, list)
    assert {"close", "rsi", "sma50", "sma200"} <= set(values)
    # Too little data -> no signals, no values.
    s2, v2 = signals_from_frame(ohlcv_to_dataframe(_bars([100])))
    assert s2 == [] and v2 == {}


def test_summarize_timeframe_trend() -> None:
    up = summarize_timeframe(ohlcv_to_dataframe(_bars([100 + i for i in range(80)])), "15m")
    assert up["trend"] == "bullish"
    down = summarize_timeframe(ohlcv_to_dataframe(_bars([200 - i for i in range(80)])), "15m")
    assert down["trend"] == "bearish"
    unknown = summarize_timeframe(ohlcv_to_dataframe({"bars": []}), "15m")
    assert unknown["trend"] == "unknown"


def test_detect_cross() -> None:
    import pandas as pd

    fast = pd.Series([1.0, 2.0, 3.0])
    slow = pd.Series([2.0, 2.0, 2.0])
    assert detect_cross(fast, slow) == "up"
    assert detect_cross(slow, fast) == "down"
