"""Tests for the quantitative analysis primitives (CONTRACT.md §11).

All pure/deterministic and OFFLINE — synthetic bars, no network, no keys.
"""

from __future__ import annotations


from makecrazypenny.analysis import backtest, factors, regime, risk


def _bars(closes: list[float]) -> list[dict]:
    """Build synthetic OHLCV bars from a close series (high/low ±1%)."""
    return [
        {"ts": f"d{i}", "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
        for i, c in enumerate(closes)
    ]


def _uptrend(n: int = 320, rate: float = 0.0007, start: float = 100.0) -> list[float]:
    return [start * (1.0 + rate) ** i for i in range(n)]


def _downtrend(n: int = 320, rate: float = 0.0007, start: float = 100.0) -> list[float]:
    return [start * (1.0 - rate) ** i for i in range(n)]


# ---------------------------------------------------------------------------
# factors
# ---------------------------------------------------------------------------


def test_momentum_and_trend_positive_in_uptrend() -> None:
    closes = _uptrend()
    assert factors.momentum_12_1(closes) > 0
    assert factors.trend_vs_sma(closes, 200) > 0
    assert factors.pct_of_52w_high(closes, closes) > 0.95  # near the high in an uptrend


def test_momentum_negative_in_downtrend() -> None:
    closes = _downtrend()
    assert factors.momentum_12_1(closes) < 0
    assert factors.trend_vs_sma(closes, 200) < 0


def test_factor_helpers_handle_short_series() -> None:
    assert factors.momentum_12_1([1.0, 2.0]) is None
    assert factors.trend_vs_sma([1.0, 2.0], 200) is None
    assert factors.realized_vol([1.0]) is None


def test_factor_values_bundle() -> None:
    vals = factors.factor_values(_bars(_uptrend()))
    for key in ("momentum_12_1", "trend_200", "pct_52w_high", "realized_vol", "last_close", "atr14", "n_bars"):
        assert key in vals
    assert vals["last_close"] > 0


def test_value_quality_extraction_defensive() -> None:
    info = {"fundamentals": {"trailingPE": 20.0, "priceToBook": 4.0, "profitMargins": 0.22, "returnOnEquity": 0.30}}
    vq = factors.value_quality(info)
    assert abs(vq["earnings_yield"] - 0.05) < 1e-6
    assert "book_to_price" in vq and "profit_margin" in vq and "roe" in vq
    assert factors.value_quality({}) == {}
    assert factors.value_quality(None) == {}


# ---------------------------------------------------------------------------
# risk / sizing
# ---------------------------------------------------------------------------


def test_atr_positive() -> None:
    a = risk.atr(_bars(_uptrend(60)))
    assert a is not None and a > 0


def test_kelly_monotonic_and_fractional() -> None:
    low = risk.kelly_fraction_from_conviction(0.2)
    high = risk.kelly_fraction_from_conviction(0.9)
    assert high["kelly_used"] > low["kelly_used"]
    # Half-Kelly is half of full (within 4-dp rounding).
    assert abs(high["kelly_used"] - 0.5 * high["kelly_full"]) < 1e-3


def test_position_sizing_long_sets_stop_below_price() -> None:
    s = risk.position_sizing(
        price=100.0, atr_value=2.0, annual_vol=0.25, conviction=0.7, direction="LONG"
    )
    assert s["position_pct"] > 0
    assert s["stop_price"] < 100.0 < s["target_price"]
    assert s["risk_per_share"] == 4.0  # 2 x ATR


def test_position_sizing_flat_is_zero() -> None:
    s = risk.position_sizing(price=100.0, atr_value=2.0, annual_vol=0.25, conviction=0.5, direction="FLAT")
    assert s["position_pct"] == 0.0
    assert s["stop_price"] is None


def test_position_sizing_scales_with_regime() -> None:
    base = risk.position_sizing(price=100.0, atr_value=2.0, annual_vol=0.20, conviction=0.8, direction="LONG", regime_scale=1.0)
    half = risk.position_sizing(price=100.0, atr_value=2.0, annual_vol=0.20, conviction=0.8, direction="LONG", regime_scale=0.5)
    assert half["position_pct"] <= base["position_pct"]


# ---------------------------------------------------------------------------
# regime
# ---------------------------------------------------------------------------


def test_regime_risk_on_in_uptrend() -> None:
    r = regime.regime_from_bars(_bars(_uptrend()))
    assert r["regime"] == "risk_on"
    assert 0 < r["gross_exposure"] <= 1.0
    assert r["above_200dma"] is True


def test_regime_risk_off_in_downtrend() -> None:
    r = regime.regime_from_bars(_bars(_downtrend()))
    assert r["regime"] == "risk_off"
    assert r["gross_exposure"] < 1.0


def test_regime_insufficient_history_defaults_caution() -> None:
    r = regime.regime_from_bars(_bars(_uptrend(50)))
    assert r["regime"] == "caution"


# ---------------------------------------------------------------------------
# backtest + overfit metrics
# ---------------------------------------------------------------------------


def test_norm_cdf_ppf_sanity() -> None:
    assert abs(backtest._norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(backtest._norm_ppf(0.975) - 1.959964) < 1e-3


def test_psr_in_unit_interval_and_monotone() -> None:
    low = backtest.probabilistic_sharpe_ratio(0.02, 1000, 0.0, 3.0)
    high = backtest.probabilistic_sharpe_ratio(0.08, 1000, 0.0, 3.0)
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0
    assert high > low


def test_deflated_sharpe_penalizes_more_trials() -> None:
    few = backtest.deflated_sharpe_ratio(0.06, 1000, 0.0, 3.0, n_trials=1)
    many = backtest.deflated_sharpe_ratio(0.06, 1000, 0.0, 3.0, n_trials=200)
    assert few >= many  # more trials -> harder to beat the null


def test_backtest_long_flat_runs_in_uptrend() -> None:
    res = backtest.backtest_long_flat(_bars(_uptrend(400)), cost_bps=10.0)
    assert "_error" not in res
    assert res["exposure"] > 0
    assert "sharpe" in res["strategy"]
    assert "deflated_sharpe" in res["overfit_checks"]
    assert res["buy_hold"]["total_return"] > 0


def test_backtest_insufficient_history() -> None:
    res = backtest.backtest_long_flat(_bars(_uptrend(100)))
    assert res["_error"] == "insufficient history"
