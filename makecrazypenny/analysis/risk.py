"""Risk & position sizing (CONTRACT.md §10.7.2).

Turns a *verdict* (direction + conviction) into a *sized trade*: an ATR-based
stop and target, a volatility-targeted weight, and a **fractional (½) Kelly**
fraction — then takes the conservative combination, capped and scaled by the
market regime. Pure and deterministic; operates on plain numbers/bars.

Why these choices (see RESEARCH.md): we use volatility targeting for **risk control**
— it robustly reduces tail risk and drawdowns (low exposure in high-vol regimes). We
deliberately do *not* rely on it as a Sharpe booster: that benefit is contested
(Moreira-Muir find gains, but Cederburg-O'Doherty-Wang-Yan find vol-management beats
the unmanaged version in only ~half of 103 equity strategies). **Half-Kelly** is used
rather than full Kelly because full Kelly is acutely sensitive to estimation error and
produces brutal drawdowns — fractional Kelly trades a little growth for much lower
variance. Nothing here is a dollar amount or advice; it is a % of risk budget with an
explicit invalidation level.
"""

from __future__ import annotations

from typing import Any

#: Defaults (retail, unlevered).
DEFAULT_TARGET_VOL = 0.15
DEFAULT_ATR_MULT = 2.0
DEFAULT_REWARD_MULT = 2.0
DEFAULT_KELLY_FRACTION = 0.5
DEFAULT_MAX_POSITION = 0.20


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    """Average True Range over the last ``period`` bars (Wilder's TR, simple mean)."""
    rows = [b for b in bars if isinstance(b, dict)]
    if len(rows) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(rows)):
        try:
            high = float(rows[i]["high"])
            low = float(rows[i]["low"])
            prev_close = float(rows[i - 1]["close"])
        except (TypeError, ValueError, KeyError):
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def kelly_fraction_from_conviction(
    conviction: float,
    reward_to_risk: float = DEFAULT_REWARD_MULT,
    fraction: float = DEFAULT_KELLY_FRACTION,
) -> dict[str, float]:
    """Map a 0..1 conviction to a fractional-Kelly position fraction.

    Conviction is treated as the edge over a coin flip: ``p = 0.5 + 0.25*conviction``
    (so conviction 1.0 → 75% hit-rate). Full Kelly for a payoff ``b`` (reward:risk)
    is ``f* = p - (1-p)/b``; we return ``fraction * f*`` (default half), floored at 0.

    Returns ``{"p", "kelly_full", "kelly_used"}``.
    """
    p = _clamp(0.5 + 0.25 * _clamp(conviction, 0.0, 1.0), 0.0, 1.0)
    b = max(reward_to_risk, 1e-6)
    kelly_full = p - (1.0 - p) / b
    kelly_used = max(0.0, fraction * kelly_full)
    return {"p": round(p, 4), "kelly_full": round(kelly_full, 4), "kelly_used": round(kelly_used, 4)}


def position_sizing(
    *,
    price: float | None,
    atr_value: float | None,
    annual_vol: float | None,
    conviction: float,
    direction: str,
    target_vol: float = DEFAULT_TARGET_VOL,
    atr_mult: float = DEFAULT_ATR_MULT,
    reward_mult: float = DEFAULT_REWARD_MULT,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    max_position: float = DEFAULT_MAX_POSITION,
    regime_scale: float = 1.0,
) -> dict[str, Any]:
    """Compute stop/target levels and a position size for a sized trade.

    The position fraction is the **conservative minimum** of the volatility-target
    weight (``target_vol/realized_vol``) and the fractional-Kelly fraction, capped
    at ``max_position`` and scaled by ``regime_scale`` (the market-regime gross
    exposure, 0..1). ``FLAT`` directions size to zero.

    Returns a dict with ``stop_price``, ``target_price``, ``risk_per_share``,
    ``vol_target_weight``, ``kelly_*``, ``position_pct``, ``r_multiple``, and notes.
    """
    direction = (direction or "").upper()
    kelly = kelly_fraction_from_conviction(conviction, reward_mult, kelly_fraction)

    vol_target_weight: float | None = None
    if annual_vol and annual_vol > 0:
        vol_target_weight = round(target_vol / annual_vol, 4)

    if direction not in ("LONG", "SHORT"):
        return {
            "direction": direction or "FLAT",
            "position_pct": 0.0,
            "stop_price": None,
            "target_price": None,
            "risk_per_share": None,
            "vol_target_weight": vol_target_weight,
            "kelly_full": kelly["kelly_full"],
            "kelly_used": kelly["kelly_used"],
            "r_multiple": reward_mult,
            "regime_scale": round(regime_scale, 4),
            "notes": "no position (FLAT)",
        }

    # Conservative size: min(vol-target, fractional-Kelly), capped + regime-scaled.
    candidates = [kelly["kelly_used"]]
    if vol_target_weight is not None:
        candidates.append(vol_target_weight)
    raw = min(candidates) if candidates else kelly["kelly_used"]
    position_pct = round(_clamp(raw, 0.0, max_position) * _clamp(regime_scale, 0.0, 1.0), 4)

    stop_price = target_price = risk_per_share = None
    if price and price > 0 and atr_value and atr_value > 0:
        risk_per_share = round(atr_mult * atr_value, 4)
        if direction == "LONG":
            stop_price = round(price - risk_per_share, 4)
            target_price = round(price + reward_mult * risk_per_share, 4)
        else:  # SHORT
            stop_price = round(price + risk_per_share, 4)
            target_price = round(price - reward_mult * risk_per_share, 4)

    return {
        "direction": direction,
        "position_pct": position_pct,
        "stop_price": stop_price,
        "target_price": target_price,
        "risk_per_share": risk_per_share,
        "vol_target_weight": vol_target_weight,
        "kelly_full": kelly["kelly_full"],
        "kelly_used": kelly["kelly_used"],
        "r_multiple": reward_mult,
        "regime_scale": round(regime_scale, 4),
        "notes": (
            f"min(vol-target {vol_target_weight}, ½-Kelly {kelly['kelly_used']}) "
            f"capped {max_position}, regime x{round(regime_scale, 2)}"
        ),
    }


__all__ = [
    "atr",
    "kelly_fraction_from_conviction",
    "position_sizing",
    "DEFAULT_TARGET_VOL",
    "DEFAULT_MAX_POSITION",
]
