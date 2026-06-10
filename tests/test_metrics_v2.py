"""Tests for the swarm-upgrade pure metrics (DESIGN-SWARM.md "New pure metrics in analysis/").

Covers the eight new crypto_metrics signals/gates and the four new risk.py
additions. All pure/deterministic and OFFLINE — synthetic inputs, no network,
no keys. Every function under test must be TOTAL: None/neutral on missing or
malformed inputs, never raising.
"""

from __future__ import annotations

import math
import statistics

import pytest

from makecrazypenny.analysis import crypto_metrics as cm
from makecrazypenny.analysis import risk

NAN = float("nan")


# ---------------------------------------------------------------------------
# taker_flow_signal (pro-trend aggressor flow)
# ---------------------------------------------------------------------------


def test_taker_flow_hand_computed() -> None:
    series = [{"time": i, "buy_sell_ratio": 1.05} for i in range(12)]
    strength, detail = cm.taker_flow_signal(series)
    assert strength == pytest.approx(math.log(1.05) / 0.10)  # ~0.4879, pro-trend bullish
    assert "buyers" in detail and detail.isascii()


def test_taker_flow_clamps_at_saturation() -> None:
    assert cm.taker_flow_signal([{"buy_sell_ratio": 1.2}] * 12)[0] == 1.0   # ln 1.2 > 0.10
    assert cm.taker_flow_signal([{"buy_sell_ratio": 0.8}] * 12)[0] == -1.0  # sellers saturate


def test_taker_flow_uses_last_window_only() -> None:
    # 36 old bearish windows then 12 bullish ones: only the tail counts.
    series = [{"buy_sell_ratio": 0.5}] * 36 + [{"buy_sell_ratio": 1.05}] * 12
    assert cm.taker_flow_signal(series)[0] == pytest.approx(math.log(1.05) / 0.10)


def test_taker_flow_malformed_inputs() -> None:
    assert cm.taker_flow_signal(None) is None
    assert cm.taker_flow_signal([]) is None
    assert cm.taker_flow_signal("junk") is None
    assert cm.taker_flow_signal([{"buy_sell_ratio": NAN}, {"buy_sell_ratio": -1}]) is None
    # Mixed garbage + one good observation still computes.
    strength, _ = cm.taker_flow_signal([{"buy_sell_ratio": "x"}, {"buy_sell_ratio": 1.0}])
    assert strength == 0.0


# ---------------------------------------------------------------------------
# cvd_signal (CVD slope vs price slope)
# ---------------------------------------------------------------------------


def _cvd_bars(n: int, taker_buy: float, rising: bool = True) -> list[dict]:
    closes = [100.0 + (i if rising else -i) for i in range(n)]
    return [
        {"close": c, "volume": 100.0, "taker_buy_volume": taker_buy}
        for c in closes
    ]


def test_cvd_confirms_uptrend() -> None:
    # delta = 2*60 - 100 = +20/bar over 20 bars; imbalance = 400/2000 = 0.2 -> mag 1.0.
    strength, detail = cm.cvd_signal(_cvd_bars(25, taker_buy=60.0))
    assert strength == 1.0
    assert "confirms" in detail and detail.isascii()


def test_cvd_divergence_fades_uptrend() -> None:
    # Price up but delta = 2*40 - 100 = -20/bar: absorption -> bearish.
    strength, detail = cm.cvd_signal(_cvd_bars(25, taker_buy=40.0))
    assert strength == -1.0
    assert "diverges" in detail


def test_cvd_partial_magnitude_hand_computed() -> None:
    # delta = 2*52 - 100 = +4/bar; imbalance = 80/2000 = 0.04 -> mag = 0.04/0.2 = 0.2.
    strength, _ = cm.cvd_signal(_cvd_bars(25, taker_buy=52.0))
    assert strength == pytest.approx(0.2)


def test_cvd_accumulation_in_downtrend_is_bullish() -> None:
    strength, detail = cm.cvd_signal(_cvd_bars(25, taker_buy=60.0, rising=False))
    assert strength == 1.0
    assert "accumulation" in detail


def test_cvd_malformed_inputs() -> None:
    assert cm.cvd_signal(None) is None
    assert cm.cvd_signal([]) is None
    # Bars without taker_buy_volume (e.g. Bybit) degrade to None, never raise.
    bars = [{"close": 100.0, "volume": 10.0} for _ in range(25)]
    assert cm.cvd_signal(bars) is None
    # NaN volume rows are skipped; too few valid rows -> None.
    bars = [{"close": 100.0, "volume": NAN, "taker_buy_volume": 5.0} for _ in range(25)]
    assert cm.cvd_signal(bars) is None
    # Zero total volume -> None (no flow information).
    bars = [{"close": 100.0 + i, "volume": 0.0, "taker_buy_volume": 0.0} for i in range(25)]
    assert cm.cvd_signal(bars) is None


# ---------------------------------------------------------------------------
# top_trader_spread_signal (follow smart money)
# ---------------------------------------------------------------------------


def test_top_trader_spread_hand_computed() -> None:
    # Spread saturates exactly at a 1.5x top-vs-crowd ratio.
    assert cm.top_trader_spread_signal(1.5, 1.0)[0] == pytest.approx(1.0)
    assert cm.top_trader_spread_signal(1.0, 1.5)[0] == pytest.approx(-1.0)
    strength, detail = cm.top_trader_spread_signal(1.2, 1.0)
    assert strength == pytest.approx(math.log(1.2) / math.log(1.5))  # ~0.4497
    assert "follow long" in detail and detail.isascii()


def test_top_trader_spread_clamps_beyond_saturation() -> None:
    assert cm.top_trader_spread_signal(3.0, 1.0)[0] == 1.0
    assert cm.top_trader_spread_signal(0.4, 2.0)[0] == -1.0


def test_top_trader_spread_malformed_inputs() -> None:
    assert cm.top_trader_spread_signal(None, 1.0) is None
    assert cm.top_trader_spread_signal(1.0, None) is None
    assert cm.top_trader_spread_signal(0.0, 1.0) is None
    assert cm.top_trader_spread_signal(1.0, -2.0) is None
    assert cm.top_trader_spread_signal(NAN, 1.0) is None


# ---------------------------------------------------------------------------
# funding_z_signal (contrarian, dead zone |z| < 1.5)
# ---------------------------------------------------------------------------

_RATES = [0.0001, 0.0002] * 10  # 20 obs, mean 0.00015


def _z(current: float) -> float:
    return (current - statistics.mean(_RATES)) / statistics.stdev(_RATES)


def test_funding_z_dead_zone_inside_one_point_five() -> None:
    current = 0.00019  # z ~ +0.78, well inside the dead zone
    assert abs(_z(current)) < 1.5
    strength, detail = cm.funding_z_signal([{"rate": r} for r in _RATES], current)
    assert strength == 0.0
    assert "dead zone" in detail and detail.isascii()


def test_funding_z_contrarian_at_extremes() -> None:
    high = 0.0005   # z ~ +6.8 -> crowded longs -> fully bearish
    low = -0.0002   # z ~ -6.8 -> crowded shorts -> fully bullish
    assert _z(high) > 3.0 and _z(low) < -3.0
    assert cm.funding_z_signal([{"rate": r} for r in _RATES], high)[0] == -1.0
    assert cm.funding_z_signal([{"rate": r} for r in _RATES], low)[0] == 1.0


def test_funding_z_ramp_between_deadzone_and_saturation() -> None:
    # Pick the current rate that lands exactly at z = 2.25 -> ramp (2.25-1.5)/1.5 = 0.5.
    current = statistics.mean(_RATES) + 2.25 * statistics.stdev(_RATES)
    strength, _ = cm.funding_z_signal([{"rate": r} for r in _RATES], current)
    assert strength == pytest.approx(-0.5)


def test_funding_z_malformed_inputs() -> None:
    assert cm.funding_z_signal([{"rate": r} for r in _RATES], None) is None
    assert cm.funding_z_signal(None, 0.0001) is None
    assert cm.funding_z_signal([{"rate": 0.0001}] * 5, 0.0005) is None       # too short
    assert cm.funding_z_signal([{"rate": 0.0001}] * 20, 0.0005) is None      # zero std
    assert cm.funding_z_signal([{"rate": NAN}] * 20, 0.0001) is None
    # Bare floats accepted alongside dict rows.
    assert cm.funding_z_signal(list(_RATES), 0.0005)[0] == -1.0


# ---------------------------------------------------------------------------
# predicted_funding_signal (flip/extreme detector)
# ---------------------------------------------------------------------------


def test_predicted_funding_crowding_building_is_contrarian() -> None:
    # current 2e-5/h (ann 0.1752, excess +0.0657); predicted HL 5e-5/h (ann 0.438,
    # excess +0.3285 > current, > 0.10 min) -> building -> -0.3285/0.50 = -0.657.
    venues = [{"venue": "HlPerp", "rate": 5e-5, "interval_hours": 1}]
    strength, detail = cm.predicted_funding_signal(2e-5, venues)
    assert strength == pytest.approx(-(5e-5 * 8760 - cm._FUNDING_BASELINE_ANN) / 0.50)
    assert "building" in detail and detail.isascii()


def test_predicted_funding_compression_scores_zero() -> None:
    # Extreme current funding predicted to compress toward baseline: squeeze resolving.
    venues = [{"venue": "HlPerp", "rate": 1e-5, "interval_hours": 1}]
    strength, detail = cm.predicted_funding_signal(5e-5, venues)
    assert strength == 0.0
    assert "compressing/aligned" in detail


def test_predicted_funding_small_excess_scores_zero() -> None:
    # Both near baseline (1.25e-5/h ~ 0.1095 ann): too small to call crowding.
    venues = [{"venue": "HlPerp", "rate": 1.3e-5, "interval_hours": 1}]
    assert cm.predicted_funding_signal(1.26e-5, venues)[0] == 0.0


def test_predicted_funding_interval_normalization_and_venue_mean() -> None:
    # No HL venue: 8h-interval CEX rate 4e-4 normalizes to 5e-5/h -> same as HL case.
    venues = [{"venue": "BinPerp", "rate": 4e-4, "interval_hours": 8}]
    strength, _ = cm.predicted_funding_signal(2e-5, venues)
    assert strength == pytest.approx(-(5e-5 * 8760 - cm._FUNDING_BASELINE_ANN) / 0.50)


def test_predicted_funding_malformed_inputs() -> None:
    assert cm.predicted_funding_signal(None, [{"venue": "HlPerp", "rate": 1e-5}]) is None
    assert cm.predicted_funding_signal(1e-5, None) is None
    assert cm.predicted_funding_signal(1e-5, []) is None
    assert cm.predicted_funding_signal(1e-5, [{"venue": "HlPerp", "rate": NAN}, "junk"]) is None


# ---------------------------------------------------------------------------
# social_velocity_signal (deterministic buzz counting)
# ---------------------------------------------------------------------------


def test_social_velocity_hand_computed() -> None:
    # ratio = 10/2 = 5 (at cap), log1p(5) ~ 1.79 -> mult 1; bull share 0.75 -> +0.5.
    strength, detail = cm.social_velocity_signal(10.0, 2.0, 30, 10)
    assert strength == pytest.approx(0.5)
    assert "bullish buzz" in detail and detail.isascii()
    # Mirror image is bearish.
    assert cm.social_velocity_signal(10.0, 2.0, 10, 30)[0] == pytest.approx(-0.5)


def test_social_velocity_ratio_capped_at_5x() -> None:
    # A 100x spike scores the same as a 5x spike (cap prevents one viral burst dominating).
    assert cm.social_velocity_signal(100.0, 1.0, 30, 10)[0] == pytest.approx(
        cm.social_velocity_signal(5.0, 1.0, 30, 10)[0]
    )


def test_social_velocity_flat_buzz_damps_direction() -> None:
    # Unchanged velocity: mult = log1p(1) ~ 0.693 < 1 damps the directional share.
    strength, _ = cm.social_velocity_signal(4.0, 4.0, 30, 10)
    assert strength == pytest.approx(0.5 * math.log1p(1.0))


def test_social_velocity_zero_prev_velocity() -> None:
    # First run (prev 0): a live velocity is treated as a capped spike; dead = 0.
    assert cm.social_velocity_signal(3.0, 0.0, 30, 10)[0] == pytest.approx(0.5)
    assert cm.social_velocity_signal(0.0, 0.0, 30, 10)[0] == 0.0


def test_social_velocity_malformed_inputs() -> None:
    assert cm.social_velocity_signal(None, 1.0, 30, 10) is None
    assert cm.social_velocity_signal(1.0, None, 30, 10) is None
    assert cm.social_velocity_signal(1.0, 1.0, 0, 0) is None      # no labeled messages
    assert cm.social_velocity_signal(1.0, 1.0, NAN, 10) is None
    assert cm.social_velocity_signal(-1.0, 1.0, 30, 10) is None


# ---------------------------------------------------------------------------
# depth_imbalance + venue_divergence (execution gates, NOT scored)
# ---------------------------------------------------------------------------


def test_depth_imbalance_hand_computed() -> None:
    # 20bps band around mid 100 -> [99.8, 100.2]; 99.5 bid and 100.6 ask fall outside.
    bids = [[99.9, 30.0], [99.5, 100.0]]
    asks = [[100.1, 10.0], [100.6, 50.0]]
    assert cm.depth_imbalance(bids, asks, 100.0) == pytest.approx((30 - 10) / (30 + 10))


def test_depth_imbalance_dict_levels_and_band_widening() -> None:
    bids = [{"px": 99.5, "sz": 100.0}]
    asks = [{"px": 100.6, "sz": 50.0}]
    # Inside 20bps nothing qualifies; a 100bps band picks both sides up.
    assert cm.depth_imbalance(bids, asks, 100.0) is None
    assert cm.depth_imbalance(bids, asks, 100.0, band_bps=100.0) == pytest.approx(
        (100 - 50) / 150
    )


def test_depth_imbalance_malformed_inputs() -> None:
    assert cm.depth_imbalance(None, None, 100.0) is None
    assert cm.depth_imbalance([], [], 100.0) is None
    assert cm.depth_imbalance([[99.9, 1.0]], [[100.1, 1.0]], None) is None
    assert cm.depth_imbalance([[99.9, 1.0]], [[100.1, 1.0]], 0.0) is None
    assert cm.depth_imbalance([[NAN, 1.0], "junk", [99.9]], [[100.1, 2.0]], 100.0) == -1.0


def test_venue_divergence_abs_bps() -> None:
    assert cm.venue_divergence(100.1, 100.0) == pytest.approx(10.0)
    assert cm.venue_divergence(99.9, 100.0) == pytest.approx(10.0)  # absolute, side-agnostic
    assert cm.venue_divergence(100.0, 100.0) == 0.0
    assert cm.venue_divergence(None, 100.0) is None
    assert cm.venue_divergence(100.0, 0.0) is None
    assert cm.venue_divergence(NAN, 100.0) is None


# ---------------------------------------------------------------------------
# risk: parkinson_vol / yang_zhang_vol
# ---------------------------------------------------------------------------


def _gapless_bars(n: int = 40, start: float = 100.0) -> list[dict]:
    """Alternating +/-1% closes; open = previous close (24/7 crypto, no gaps)."""
    bars: list[dict] = []
    prev_close = start
    for i in range(n):
        o = prev_close
        c = o * (1.01 if i % 2 == 0 else 0.99)
        bars.append(
            {"open": o, "high": max(o, c) * 1.005, "low": min(o, c) * 0.995, "close": c}
        )
        prev_close = c
    return bars


def test_parkinson_hand_computed() -> None:
    # Constant H/L ratio 1.01: var/bar = ln(1.01)^2 / (4 ln 2); annualized over 365.
    bars = [{"high": 101.0, "low": 100.0} for _ in range(30)]
    expected = math.sqrt(math.log(1.01) ** 2 / (4.0 * math.log(2.0)) * 365.0)
    assert risk.parkinson_vol(bars, 365.0) == pytest.approx(expected)


def test_parkinson_vs_yang_zhang_same_order_of_magnitude_gapless() -> None:
    bars = _gapless_bars()
    pk = risk.parkinson_vol(bars, 365.0)
    yz = risk.yang_zhang_vol(bars, 365.0)
    assert pk is not None and pk > 0
    assert yz is not None and yz > 0
    # On a gapless series the estimators agree to within ~3x (same order of magnitude).
    assert 1 / 3 < pk / yz < 3


def test_yang_zhang_captures_overnight_gaps() -> None:
    gapless = _gapless_bars()
    gapped = [dict(b) for b in gapless]
    # Inject alternating +/-2% opening gaps (open != previous close).
    for i in range(1, len(gapped)):
        gap = 1.02 if i % 2 == 0 else 0.98
        o = gapped[i - 1]["close"] * gap
        c = o * (1.01 if i % 2 == 0 else 0.99)
        gapped[i] = {"open": o, "high": max(o, c) * 1.005, "low": min(o, c) * 0.995, "close": c}
    yz_gapless = risk.yang_zhang_vol(gapless, 365.0)
    yz_gapped = risk.yang_zhang_vol(gapped, 365.0)
    # The overnight-variance term makes YZ strictly larger on the gapped series.
    assert yz_gapped > yz_gapless * 1.5


def test_vol_estimators_malformed_inputs() -> None:
    assert risk.parkinson_vol(None, 365.0) is None
    assert risk.parkinson_vol([], 365.0) is None
    assert risk.parkinson_vol([{"high": 101.0, "low": 100.0}] * 3, 365.0) is None  # < 5 bars
    assert risk.parkinson_vol([{"high": 100.0, "low": 101.0}] * 30, 365.0) is None  # high < low
    assert risk.parkinson_vol([{"high": NAN, "low": 100.0}] * 30, 365.0) is None
    assert risk.parkinson_vol([{"high": 101.0, "low": 100.0}] * 30, 0.0) is None
    assert risk.yang_zhang_vol(None, 365.0) is None
    assert risk.yang_zhang_vol(_gapless_bars(4), 365.0) is None  # too few observations
    assert risk.yang_zhang_vol([{"open": 0.0, "high": 1.0, "low": 1.0, "close": 1.0}] * 30) is None


# ---------------------------------------------------------------------------
# risk: kelly_calibrated (Laplace-shrunk hit-rate + cold-start quarter-Kelly)
# ---------------------------------------------------------------------------


def test_kelly_calibrated_n0_cold_start() -> None:
    # n=0: p_hat = 2/4 = 0.5 caps any conviction; quarter-Kelly of (0.5 - 0.5/2) = 0.0625.
    out = risk.kelly_calibrated(1.0, None)
    assert out["p_hat"] == pytest.approx(0.5)
    assert out["p_eff"] == pytest.approx(0.5)
    assert out["fraction"] == 0.25
    assert out["kelly_full"] == pytest.approx(0.25)
    assert out["kelly_used"] == pytest.approx(0.0625)


def test_kelly_calibrated_n10_still_quarter_kelly() -> None:
    out = risk.kelly_calibrated(1.0, {"n_closed": 10, "wins": 7})
    assert out["p_hat"] == pytest.approx(9 / 14, abs=1e-4)  # values rounded to 4dp
    assert out["p_eff"] == pytest.approx(9 / 14, abs=1e-4)  # min(0.75 conviction-p, 0.6429)
    assert out["fraction"] == 0.25
    assert out["kelly_used"] == pytest.approx(0.25 * (9 / 14 - (5 / 14) / 2), abs=1e-4)


def test_kelly_calibrated_n100_graduates_to_half_kelly() -> None:
    out = risk.kelly_calibrated(1.0, {"n_closed": 100, "wins": 60})
    assert out["p_hat"] == pytest.approx(62 / 104, abs=1e-4)
    assert out["fraction"] == 0.5
    assert out["kelly_used"] == pytest.approx(0.5 * (62 / 104 - (42 / 104) / 2), abs=1e-4)


def test_kelly_calibrated_bad_journal_zeroes_sizing() -> None:
    # 5/20 realized: p_hat = 7/24 < breakeven for 2:1 -> kelly_full < 0 -> used floored at 0.
    out = risk.kelly_calibrated(1.0, {"n_closed": 20, "wins": 5})
    assert out["kelly_full"] < 0
    assert out["kelly_used"] == 0.0


def test_kelly_calibrated_accepts_hit_rate_and_malformed_stats() -> None:
    # hit_rate * n_closed derives wins when "wins" is absent.
    via_rate = risk.kelly_calibrated(1.0, {"n_closed": 10, "hit_rate": 0.7})
    via_wins = risk.kelly_calibrated(1.0, {"n_closed": 10, "wins": 7})
    assert via_rate["p_hat"] == via_wins["p_hat"]
    # Malformed stats / conviction degrade to the conservative cold start, never raise.
    assert risk.kelly_calibrated(NAN, {"n_closed": "junk", "wins": NAN})["fraction"] == 0.25
    assert risk.kelly_calibrated(0.5, "junk")["p_hat"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# risk: correlated_exposure_check (allow / scale / refuse)
# ---------------------------------------------------------------------------

_BETAS = {
    "BTCUSDT": {"beta": 1.0, "corr": 1.0},
    "ETHUSDT": {"beta": 1.1, "corr": 0.85},
    "DOGEUSDT": {"beta": 1.5, "corr": 0.8},
    "XYZUSDT": {"beta": 0.2, "corr": 0.3},
}


def test_correlated_exposure_allow_branch() -> None:
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 500.0}],
        {"symbol": "ETHUSDT", "notional": 500.0},
        _BETAS,
        equity=1000.0,
    )
    # Bucket BTC: 500*1.0 held + 500*1.1 candidate = 1050 <= cap 2000.
    assert out["allowed"] is True
    assert out["scaled_notional"] == 500.0
    assert out["bucket"] == "BTC"
    assert "allowed" in out["reason"] and out["reason"].isascii()


def test_correlated_exposure_scale_branch() -> None:
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 1500.0}],
        {"symbol": "ETHUSDT", "notional": 1000.0},
        _BETAS,
        equity=1000.0,
    )
    # Headroom = 2000 - 1500 = 500 beta-notional -> 500/1.1 notional.
    assert out["allowed"] is True
    assert out["scaled_notional"] == pytest.approx(500.0 / 1.1, abs=0.01)
    assert "downsized" in out["reason"]


def test_correlated_exposure_refuse_branch() -> None:
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 1000.0}, {"symbol": "DOGEUSDT", "notional": 1000.0}],
        {"symbol": "ETHUSDT", "notional": 100.0},
        _BETAS,
        equity=1000.0,
    )
    # Bucket exposure 1000 + 1500 = 2500 >= cap 2000: no headroom at all.
    assert out["allowed"] is False
    assert out["scaled_notional"] == 0.0
    assert "refused" in out["reason"]


def test_correlated_exposure_uncorrelated_symbol_own_bucket() -> None:
    # corr 0.3 < 0.7: XYZ does not share the (full) BTC bucket and sails through.
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 5000.0}],
        {"symbol": "XYZUSDT", "notional": 500.0},
        _BETAS,
        equity=1000.0,
    )
    assert out["allowed"] is True
    assert out["bucket"] == "XYZUSDT"
    assert out["scaled_notional"] == 500.0


def test_correlated_exposure_unknown_symbol_defaults_to_btc_bucket() -> None:
    # No beta info: conservative beta 1.0 / corr 1.0 -> shares the saturated BTC bucket.
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 2500.0}],
        {"symbol": "NEWCOIN", "notional": 100.0},
        _BETAS,
        equity=1000.0,
    )
    assert out["allowed"] is False


def test_correlated_exposure_tolerant_inputs() -> None:
    # No candidate notional: nothing to gate.
    out = risk.correlated_exposure_check([], {}, _BETAS, equity=1000.0)
    assert out["allowed"] is True and out["scaled_notional"] == 0.0
    # Missing equity: gate passes through with an explicit skip reason.
    out = risk.correlated_exposure_check([], {"symbol": "ETHUSDT", "notional": 100.0}, _BETAS)
    assert out["allowed"] is True and "skipped" in out["reason"]
    # Equity readable from the candidate payload when the keyword is absent.
    out = risk.correlated_exposure_check(
        [{"symbol": "BTCUSDT", "notional": 2500.0}],
        {"symbol": "ETHUSDT", "notional": 100.0, "equity": 1000.0},
        _BETAS,
    )
    assert out["allowed"] is False
    # Garbage everywhere never raises.
    out = risk.correlated_exposure_check(
        ["junk", {"symbol": "BTCUSDT", "notional": NAN}],
        {"symbol": "ETHUSDT", "notional": 100.0},
        "junk",
        cap_mult=NAN,
        equity=1000.0,
    )
    assert out["allowed"] is True
