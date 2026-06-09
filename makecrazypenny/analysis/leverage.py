"""Leverage-aware risk sizing for perpetual futures (CONTRACT.md §16).

Equities are sized unlevered (``analysis/risk.py``). Leveraged crypto needs more:
the position can be **liquidated** before your stop, funding **accrues** while you
hold, and the safe leverage depends on how far your stop sits from entry. This
module turns a verdict (direction + conviction) into a *leveraged plan*:

  * an isolated-margin **liquidation price** estimate,
  * the **maximum safe leverage** that keeps the ATR stop comfortably inside the
    liquidation distance (so you get stopped out, not liquidated),
  * notional / margin sizing to a fixed **risk-per-trade** budget, and
  * an **estimated funding cost** over the expected hold.

Everything is pure, deterministic, and expressed as a *percentage of a risk
budget* with explicit stop / liquidation / invalidation levels — informational
only, never a dollar amount or advice. The "aggressive" defaults (≤20x, ~2.5%
risk) are overridable via :class:`~makecrazypenny.core.config.Settings`.
"""

from __future__ import annotations

from typing import Any

#: Conservative isolated maintenance-margin-rate default (varies by tier/symbol).
DEFAULT_MMR = 0.005
#: ATR multiple for the protective stop, and the reward:risk target multiple.
DEFAULT_ATR_MULT = 2.0
DEFAULT_REWARD_MULT = 2.0
#: Default per-trade risk budget (fraction of equity lost if the stop triggers).
DEFAULT_RISK_PER_TRADE = 0.025
#: The stop must sit at least this fraction inside the liquidation distance.
DEFAULT_LIQ_BUFFER = 0.5
#: Hard leverage ceiling regardless of how tight the stop is.
DEFAULT_MAX_LEVERAGE = 20.0
#: Suggested leverage as a fraction of the max-safe value. The max-safe number
#: comes from a simplified liquidation model (no fee/tier effects) and assumes
#: the stop actually fills — thin books wick through stops. Running at half the
#: ceiling keeps the same risk-per-trade (sizing is notional-based) while
#: doubling the cushion between the stop and the liquidation price.
DEFAULT_LEVERAGE_FRACTION = 0.5
#: Perp funding interval (hours) — most USDT perps settle every 8h.
DEFAULT_FUNDING_INTERVAL_H = 8.0
#: Fallback stop distance (fraction of price) when ATR is unavailable.
_FALLBACK_STOP_FRAC = 0.02


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def liquidation_price(
    entry: float, leverage: float, direction: str, mmr: float = DEFAULT_MMR
) -> float | None:
    """Estimate the isolated-margin liquidation price for a single position.

    Simplified single-position isolated formula (ignores fees and the
    maintenance-amount tiers an exchange applies, so treat it as an estimate):

        long  liq ~= entry * (1 - 1/L + mmr)
        short liq ~= entry * (1 + 1/L - mmr)

    Returns ``None`` for a flat/invalid direction or non-positive inputs.
    """
    d = (direction or "").upper()
    if entry is None or entry <= 0 or leverage is None or leverage <= 0:
        return None
    inv = 1.0 / leverage
    if d == "LONG":
        return round(entry * (1.0 - inv + mmr), 8)
    if d == "SHORT":
        return round(entry * (1.0 + inv - mmr), 8)
    return None


def max_safe_leverage(
    stop_distance_frac: float,
    *,
    mmr: float = DEFAULT_MMR,
    buffer: float = DEFAULT_LIQ_BUFFER,
    hard_cap: float = DEFAULT_MAX_LEVERAGE,
) -> float:
    """Largest leverage that keeps the stop inside the liquidation distance.

    The liquidation distance (fraction of entry) for an isolated position is
    approximately ``1/L - mmr``. Requiring it to exceed the stop distance by
    ``buffer`` gives ``1/L - mmr >= d*(1+buffer)``, i.e.
    ``L <= 1 / (d*(1+buffer) + mmr)``. Capped at ``hard_cap`` and floored at 1.

    Args:
        stop_distance_frac: Stop distance as a fraction of entry (e.g. 0.015).
        mmr: Maintenance-margin rate.
        buffer: Extra fraction the liquidation must sit beyond the stop.
        hard_cap: Absolute leverage ceiling.
    """
    d = max(float(stop_distance_frac), 1e-6)
    denom = d * (1.0 + max(buffer, 0.0)) + max(mmr, 0.0)
    raw = 1.0 / denom if denom > 0 else hard_cap
    return round(_clamp(raw, 1.0, hard_cap), 2)


def funding_cost(
    funding_rate: float | None,
    hours_held: float,
    *,
    interval_hours: float = DEFAULT_FUNDING_INTERVAL_H,
    direction: str = "LONG",
) -> float:
    """Estimate funding paid (+) / received (-) over a hold, as a fraction of notional.

    Funding settles every ``interval_hours``; a long with positive funding *pays*
    (a drag), a short with positive funding *receives*. Returns the signed cost as
    a fraction of notional (positive => drag on the position).
    """
    if funding_rate is None or interval_hours <= 0:
        return 0.0
    intervals = max(0.0, hours_held) / interval_hours
    side = 1.0 if (direction or "").upper() == "LONG" else -1.0
    return round(funding_rate * intervals * side, 8)


def leverage_plan(
    *,
    price: float | None,
    atr_value: float | None,
    direction: str,
    conviction: float,
    funding_rate: float | None = None,
    funding_interval_hours: float = DEFAULT_FUNDING_INTERVAL_H,
    expected_hold_hours: float = 8.0,
    max_leverage: float = DEFAULT_MAX_LEVERAGE,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    mmr: float = DEFAULT_MMR,
    atr_mult: float = DEFAULT_ATR_MULT,
    reward_mult: float = DEFAULT_REWARD_MULT,
    liq_buffer: float = DEFAULT_LIQ_BUFFER,
    regime_scale: float = 1.0,
) -> dict[str, Any]:
    """Build a leverage-aware trade plan from a verdict and price/volatility data.

    The ATR stop sets the risk distance; leverage is capped so liquidation stays
    beyond the stop by ``liq_buffer``; the position is sized so a stop-out costs
    ``risk_per_trade`` of equity (scaled by conviction and the market regime).

    Returns a dict with ``suggested_leverage``, ``max_safe_leverage``,
    ``liquidation_price``, ``stop_price``, ``target_price``, ``stop_distance_pct``,
    ``notional_pct``, ``margin_pct``, ``risk_per_trade_pct``, ``est_funding_cost``,
    ``r_multiple``, and human-readable ``notes``. A flat/invalid direction sizes to
    zero.
    """
    d = (direction or "").upper()
    flat = {
        "direction": d or "FLAT",
        "suggested_leverage": 0.0,
        "max_safe_leverage": 0.0,
        "liquidation_price": None,
        "stop_price": None,
        "target_price": None,
        "stop_distance_pct": None,
        "notional_pct": 0.0,
        "margin_pct": 0.0,
        "risk_per_trade_pct": 0.0,
        "est_funding_cost_pct": 0.0,
        "r_multiple": reward_mult,
        "regime_scale": round(regime_scale, 4),
        "notes": "no position (FLAT)",
    }
    if d not in ("LONG", "SHORT") or not price or price <= 0:
        return flat

    # Stop distance as a fraction of entry (ATR-based; fallback if ATR missing).
    if atr_value and atr_value > 0:
        stop_frac = atr_mult * atr_value / price
        stop_note = f"ATR stop {atr_mult}x"
    else:
        stop_frac = _FALLBACK_STOP_FRAC
        stop_note = f"fallback stop {_FALLBACK_STOP_FRAC:.1%} (no ATR)"
    stop_frac = _clamp(stop_frac, 1e-4, 0.95)

    l_safe = max_safe_leverage(stop_frac, mmr=mmr, buffer=liq_buffer, hard_cap=max_leverage)
    # Suggest a fraction of the ceiling, not the ceiling itself: same notional
    # (risk is set by the stop), less margin efficiency, much wider liquidation
    # cushion against wicks and the model's own approximations.
    suggested_leverage = round(_clamp(l_safe * DEFAULT_LEVERAGE_FRACTION, 1.0, max_leverage), 2)

    # Risk budget: scale the per-trade risk by conviction (floor 0.4) and regime.
    conv = _clamp(conviction, 0.0, 1.0)
    risk_used = risk_per_trade * (0.4 + 0.6 * conv) * _clamp(regime_scale, 0.0, 1.0)
    # Size so a stop-out costs `risk_used`: loss = notional% * stop_frac = risk_used.
    notional_pct = risk_used / stop_frac if stop_frac > 0 else 0.0
    margin_pct = notional_pct / suggested_leverage if suggested_leverage > 0 else 0.0

    risk_per_share = stop_frac * price
    if d == "LONG":
        stop_price = round(price - risk_per_share, 8)
        target_price = round(price + reward_mult * risk_per_share, 8)
    else:
        stop_price = round(price + risk_per_share, 8)
        target_price = round(price - reward_mult * risk_per_share, 8)

    liq = liquidation_price(price, suggested_leverage, d, mmr)
    fund = funding_cost(
        funding_rate, expected_hold_hours, interval_hours=funding_interval_hours, direction=d
    )
    est_funding_cost_pct = round(fund * notional_pct, 6)

    return {
        "direction": d,
        "entry_price": round(price, 8),
        "suggested_leverage": round(suggested_leverage, 2),
        "max_safe_leverage": round(l_safe, 2),
        "liquidation_price": liq,
        "stop_price": stop_price,
        "target_price": target_price,
        "stop_distance_pct": round(stop_frac, 6),
        "notional_pct": round(notional_pct, 6),
        "margin_pct": round(margin_pct, 6),
        "risk_per_trade_pct": round(risk_used, 6),
        "est_funding_cost_pct": est_funding_cost_pct,
        "r_multiple": reward_mult,
        "regime_scale": round(regime_scale, 4),
        "notes": (
            f"{stop_note}; suggested {suggested_leverage:.1f}x = "
            f"{DEFAULT_LEVERAGE_FRACTION:.0%} of max-safe {l_safe:.1f}x (liquidation "
            f"buffer >={1 + liq_buffer:.1f}x stop distance at the max); risk "
            f"{risk_used:.2%} of equity, margin {margin_pct:.2%}"
        ),
    }


__all__ = [
    "liquidation_price",
    "max_safe_leverage",
    "funding_cost",
    "leverage_plan",
    "DEFAULT_MAX_LEVERAGE",
    "DEFAULT_RISK_PER_TRADE",
    "DEFAULT_MMR",
    "DEFAULT_LIQ_BUFFER",
    "DEFAULT_LEVERAGE_FRACTION",
]
