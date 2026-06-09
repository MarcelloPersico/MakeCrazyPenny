"""Tests for the leverage-aware sizing core (CONTRACT.md §16). Pure/offline."""

from __future__ import annotations

from makecrazypenny.analysis.leverage import (
    funding_cost,
    leverage_plan,
    liquidation_price,
    max_safe_leverage,
)


def test_liquidation_price_long_below_short_above() -> None:
    entry = 100.0
    liq_long = liquidation_price(entry, 10, "LONG", mmr=0.005)
    liq_short = liquidation_price(entry, 10, "SHORT", mmr=0.005)
    assert liq_long is not None and liq_short is not None
    assert liq_long < entry < liq_short
    # Higher leverage => liquidation closer to entry.
    assert liquidation_price(entry, 20, "LONG") > liquidation_price(entry, 5, "LONG")
    assert liquidation_price(entry, 10, "FLAT") is None


def test_max_safe_leverage_caps_and_responds_to_stop_width() -> None:
    # A tight stop allows high leverage but never above the hard cap.
    assert max_safe_leverage(0.005, hard_cap=20.0) == 20.0
    # A wider stop forces lower leverage.
    wide = max_safe_leverage(0.07, hard_cap=20.0)
    assert 1.0 < wide < 20.0
    # Tighter stop => more leverage allowed.
    assert max_safe_leverage(0.02) > max_safe_leverage(0.05)


def test_funding_cost_sign_by_direction() -> None:
    # Positive funding is a drag on a long, a credit to a short.
    assert funding_cost(0.0001, 24, interval_hours=8, direction="LONG") > 0
    assert funding_cost(0.0001, 24, interval_hours=8, direction="SHORT") < 0
    # Three 8h intervals over 24h.
    assert funding_cost(0.0001, 24, interval_hours=8) == 0.0003
    assert funding_cost(None, 24) == 0.0


def test_leverage_plan_stop_inside_liquidation_long_and_short() -> None:
    for direction in ("LONG", "SHORT"):
        plan = leverage_plan(
            price=42000.0, atr_value=300.0, direction=direction, conviction=0.6,
            funding_rate=0.0001, max_leverage=20.0,
        )
        liq = plan["liquidation_price"]
        stop = plan["stop_price"]
        assert plan["suggested_leverage"] <= 20.0
        if direction == "LONG":
            assert liq < stop < 42000.0  # stop hit before liquidation
        else:
            assert 42000.0 < stop < liq
        assert plan["margin_pct"] > 0
        assert plan["risk_per_trade_pct"] > 0


def test_leverage_plan_flat_sizes_to_zero() -> None:
    plan = leverage_plan(price=42000.0, atr_value=300.0, direction="FLAT", conviction=0.5)
    assert plan["suggested_leverage"] == 0.0
    assert plan["notional_pct"] == 0.0
    assert plan["liquidation_price"] is None


def test_leverage_plan_conviction_scales_risk_and_cap_respected() -> None:
    low = leverage_plan(price=100.0, atr_value=1.0, direction="LONG", conviction=0.1, max_leverage=10.0)
    high = leverage_plan(price=100.0, atr_value=1.0, direction="LONG", conviction=0.9, max_leverage=10.0)
    assert high["risk_per_trade_pct"] > low["risk_per_trade_pct"]
    assert low["suggested_leverage"] <= 10.0 and high["suggested_leverage"] <= 10.0


def test_leverage_plan_regime_scale_reduces_risk() -> None:
    full = leverage_plan(price=100.0, atr_value=1.0, direction="LONG", conviction=0.6, regime_scale=1.0)
    halved = leverage_plan(price=100.0, atr_value=1.0, direction="LONG", conviction=0.6, regime_scale=0.5)
    assert halved["risk_per_trade_pct"] < full["risk_per_trade_pct"]
