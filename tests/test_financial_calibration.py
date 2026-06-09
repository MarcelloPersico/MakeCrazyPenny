"""Regression tests for the financial-calibration fixes.

Pins the behavior introduced by the audit follow-up:

* funding signal centered on the equilibrium baseline (not zero);
* Fear & Greed contrarian only at the extremes (mid-range scores 0);
* suggested leverage is a fraction of max-safe, not the ceiling;
* analyst consensus / price-target upside de-meaned against their optimism skew;
* congress/insider flow weighted by disclosed dollar size;
* interval-aware crypto factor saturations + vol annualization;
* value_quality reports ``gross_margin`` (honest naming);
* Alpha Vantage requests ``outputsize=full`` for >100-bar look-backs;
* TTL cache promotes L2 hits back into L1.

All pure/offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from makecrazypenny.analysis import factors as F
from makecrazypenny.analysis.crypto_metrics import fear_greed_signal, funding_signal
from makecrazypenny.analysis.leverage import leverage_plan, max_safe_leverage
from makecrazypenny.orchestration import crypto as crypto_engine
from makecrazypenny.orchestration import debate


# --- funding baseline ---------------------------------------------------------


def test_funding_at_equilibrium_baseline_scores_zero() -> None:
    # +0.01%/8h is the resting interest-rate component, not crowding.
    strength, detail = funding_signal(0.0001)
    assert strength == pytest.approx(0.0, abs=1e-9)
    assert "baseline" in detail


def test_funding_scores_excess_over_baseline() -> None:
    above, _ = funding_signal(0.0002)   # ~+22% ann, ~+11% over baseline
    below, _ = funding_signal(-0.0001)  # ~-11% ann, ~-22% under baseline
    assert above < 0 < below
    assert abs(below) > abs(above)  # symmetric in *excess*, not in raw rate


# --- Fear & Greed dead zone -----------------------------------------------------


def test_fear_greed_mid_range_scores_zero() -> None:
    for value in (30, 45, 50, 60, 74):
        assert fear_greed_signal(value)[0] == 0.0


def test_fear_greed_extremes_ramp() -> None:
    assert fear_greed_signal(90)[0] == pytest.approx(-0.6)
    assert fear_greed_signal(100)[0] == pytest.approx(-1.0)
    assert fear_greed_signal(10)[0] == pytest.approx(0.6)
    assert fear_greed_signal(0)[0] == pytest.approx(1.0)


# --- leverage suggestion --------------------------------------------------------


def test_suggested_leverage_is_half_of_max_safe() -> None:
    plan = leverage_plan(price=100.0, atr_value=1.0, direction="LONG", conviction=0.6, max_leverage=20.0)
    # stop = 2*ATR = 2% -> max safe hits the 20x cap; suggested runs at half.
    assert plan["max_safe_leverage"] == 20.0
    assert plan["suggested_leverage"] == pytest.approx(10.0)
    # Liquidation is computed at the *suggested* leverage -> wider cushion.
    assert plan["liquidation_price"] < plan["stop_price"]


def test_suggested_leverage_floors_at_one() -> None:
    # A very wide stop -> max safe near 1x; suggestion must not drop below 1x.
    wide = max_safe_leverage(0.50, hard_cap=20.0)
    plan = leverage_plan(price=100.0, atr_value=25.0, direction="LONG", conviction=0.5, max_leverage=20.0)
    assert wide >= 1.0
    assert plan["suggested_leverage"] >= 1.0


# --- de-meaned analyst signals ---------------------------------------------------


def test_baseline_consensus_tilt_is_not_a_buy_signal() -> None:
    # tilt = (2*1 + 4) / (2*10) = +0.30 == the structural baseline -> no factor.
    dossier = {"ratings": {"ratings": [{"strong_buy": 1, "buy": 4, "hold": 5, "sell": 0, "strong_sell": 0}]}}
    scored = debate.score_evidence(dossier)
    assert all(f["name"] != "consensus" for f in scored["factors"])


def test_strong_consensus_still_scores_bullish() -> None:
    dossier = {"ratings": {"ratings": [{"strong_buy": 8, "buy": 6, "hold": 2, "sell": 0, "strong_sell": 0}]}}
    scored = debate.score_evidence(dossier)
    consensus = [f for f in scored["factors"] if f["name"] == "consensus"]
    assert consensus and consensus[0]["contribution"] > 0


def test_routine_target_optimism_is_not_a_buy_signal() -> None:
    # +10% mean-target upside is the routine optimism premium -> no factor.
    dossier = {"price_targets": {"targets": {"mean": 110.0, "current": 100.0}}}
    scored = debate.score_evidence(dossier)
    assert all(f["name"] != "target_upside" for f in scored["factors"])
    # A target *below* spot is now genuinely bearish (excess -30%).
    bearish = debate.score_evidence({"price_targets": {"targets": {"mean": 80.0, "current": 100.0}}})
    tu = [f for f in bearish["factors"] if f["name"] == "target_upside"]
    assert tu and tu[0]["contribution"] < 0


# --- dollar-weighted flow --------------------------------------------------------


def test_flow_is_dollar_weighted() -> None:
    big_insider = {"insider": {"transactions": [{"transaction": "Buy", "value": 2_000_000}]}}
    small_congress = {"congress": {"trades": [{"transaction": "Purchase", "amount_range": "$1,001 - $15,000"}]}}
    big = debate.score_evidence(big_insider)["factors"][0]
    small = debate.score_evidence(small_congress)["factors"][0]
    assert big["contribution"] > small["contribution"] > 0


def test_flow_without_size_info_defaults_to_unit_weight() -> None:
    dossier = {"congress": {"trades": [{"transaction": "Purchase"}, {"transaction": "Purchase"}]}}
    scored = debate.score_evidence(dossier)
    flow = [f for f in scored["factors"] if f["name"] == "congress_flow"]
    assert flow and flow[0]["contribution"] == pytest.approx(2.0 / 3.0, abs=1e-3)


# --- interval-aware crypto calibration --------------------------------------------


def test_crypto_periods_per_year_by_interval() -> None:
    assert crypto_engine.periods_per_year("1d") == pytest.approx(365.0)
    assert crypto_engine.periods_per_year("15m") == pytest.approx(365.0 * 96.0)


def test_crypto_saturations_shrink_with_interval() -> None:
    daily = crypto_engine.factor_saturations("1d")
    m15 = crypto_engine.factor_saturations("15m")
    m1 = crypto_engine.factor_saturations("1m")
    assert daily["mom_saturation"] == pytest.approx(0.60)
    assert m15["mom_saturation"] < daily["mom_saturation"]
    assert m1["mom_saturation"] < m15["mom_saturation"]
    # Floors keep 1m from scoring pure noise as saturated signal.
    assert m1["mom_saturation"] >= 0.01


def test_crypto_intraday_momentum_actually_contributes() -> None:
    # A +5% move over the 252-bar window at 15m should register near-saturated,
    # not the ~0.17 the old daily threshold (0.30) produced.
    dossier = {"interval": "15m", "factors": {"momentum_12_1": 0.05}}
    scored = crypto_engine.score_crypto_evidence(dossier)
    mom = [f for f in scored["factors"] if f["name"] == "momentum_12_1"]
    assert mom and mom[0]["contribution"] > 1.0  # weight 2.0 x strength > 0.5


def test_realized_vol_annualization_factor() -> None:
    closes = [100.0 * (1.0 + 0.01 * ((-1) ** i)) for i in range(60)]
    v252 = F.realized_vol(closes)
    v365 = F.realized_vol(closes, periods_per_year=365.0)
    assert v252 is not None and v365 is not None
    assert v365 == pytest.approx(v252 * (365.0 / 252.0) ** 0.5, rel=1e-9)


# --- honest factor naming ----------------------------------------------------------


def test_value_quality_reports_gross_margin() -> None:
    out = F.value_quality({"grossMargins": 0.45, "returnOnEquity": 0.2})
    assert out["gross_margin"] == 0.45
    assert "gross_profitability" not in out


# --- Alpha Vantage output size -------------------------------------------------------


def test_av_outputsize_full_for_long_lookbacks() -> None:
    from makecrazypenny.providers.alpha_vantage import _needs_full_output, _ohlcv_query

    assert _needs_full_output("2y") and _needs_full_output("10y") and _needs_full_output("max")
    assert _needs_full_output("6mo")
    assert not _needs_full_output("3mo") and not _needs_full_output("")
    query, _ = _ohlcv_query("AAPL", "1d", "2y")
    assert query["outputsize"] == "full"
    query, _ = _ohlcv_query("AAPL", "1d", "3mo")
    assert query["outputsize"] == "compact"


# --- cache L2 -> L1 promotion ----------------------------------------------------------


async def test_l2_hit_promotes_into_l1(tmp_path: Any) -> None:
    from makecrazypenny.providers.cache import TTLCache

    cache = TTLCache(tmp_path, l2_enabled=True)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "value"

    first = await cache.get_or_fetch("k", ttl=60.0, factory=factory)
    assert first.cached is False and calls == 1

    cache.clear()  # drop L1; the value survives only in L2
    second = await cache.get_or_fetch("k", ttl=60.0, factory=factory)
    assert second.cached is True and calls == 1
    # The L2 hit must repopulate L1 so the next lookup skips the disk.
    assert "k" in cache._l1
