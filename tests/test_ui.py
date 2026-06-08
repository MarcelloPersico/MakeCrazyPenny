"""Tests for the Streamlit dashboard's pure helpers and import-safety.

These do not require Streamlit's runtime: we exercise the framework-free helper
functions and confirm the module imports cleanly (the app body must not run on a
plain import).
"""

from __future__ import annotations

import pandas as pd

from makecrazypenny.core.errors import AllProvidersFailed
from makecrazypenny.ui import dashboard as dash
from makecrazypenny.ui import launch as launch_mod


def test_module_imports_without_running_app() -> None:
    # under_streamlit() must be False outside the streamlit runtime, which is the
    # guard that prevents render() from executing on import.
    assert dash.under_streamlit() is False
    assert callable(dash.render)
    assert callable(launch_mod.launch)


def test_fmt_num_and_pct() -> None:
    assert dash.fmt_num(None) == "—"
    assert dash.fmt_num(1234.5) == "1,234.50"
    assert dash.fmt_num("n/a") == "n/a"
    assert dash.fmt_pct(0.1234) == "12.3%"
    assert dash.fmt_pct(None) == "—"


def test_as_records() -> None:
    assert dash.as_records([{"a": 1}, "skip", {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert dash.as_records({"a": 1}) == [{"a": 1}]
    assert dash.as_records(None) == []
    assert dash.as_records(42) == []


def test_df_from_bars_builds_time_index() -> None:
    bars = [
        {"ts": "2024-01-02T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"ts": "2024-01-01T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.0, "volume": 20},
    ]
    df = dash.df_from_bars(bars)
    assert not df.empty
    assert list(df.columns) >= ["close"] or "close" in df.columns
    # Sorted ascending by timestamp.
    assert df.index.is_monotonic_increasing
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["close"].iloc[0] == 1.0


def test_df_from_bars_empty() -> None:
    assert dash.df_from_bars(None).empty
    assert dash.df_from_bars([]).empty


def test_has_error() -> None:
    assert dash.has_error({"_error": "boom"}) == "boom"
    assert dash.has_error({"error": "nope"}) == "nope"
    assert dash.has_error({"ok": True}) is None
    assert dash.has_error("not a dict") is None


def test_explain_failure_missing_keys_is_actionable() -> None:
    exc = AllProvidersFailed(
        "analyst_ratings",
        {"finnhub": "missing API key: FINNHUB_API_KEY", "fmp": "missing API key: FMP_API_KEY"},
    )
    msg = dash.explain_failure(exc)
    assert "needs an API key" in msg
    assert "FINNHUB_API_KEY" in msg and "FMP_API_KEY" in msg
    # No alarming "all providers failed" wording when it's just missing keys.
    assert "failed" not in msg.lower()


def test_explain_failure_real_error_is_verbatim() -> None:
    exc = AllProvidersFailed("ohlcv", {"yfinance": "RuntimeError: network down"})
    msg = dash.explain_failure(exc)
    assert "ohlcv" in msg  # falls back to the full message when not a key issue
    assert dash.explain_failure(ValueError("boom")) == "ValueError: boom"
