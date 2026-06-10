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

import math
from typing import Any

#: Defaults (retail, unlevered).
DEFAULT_TARGET_VOL = 0.15
DEFAULT_ATR_MULT = 2.0
DEFAULT_REWARD_MULT = 2.0
DEFAULT_KELLY_FRACTION = 0.5
DEFAULT_MAX_POSITION = 0.20

#: Parkinson normalization constant (4 ln 2).
_PARKINSON_FACTOR = 4.0 * math.log(2.0)
#: Minimum usable observations for a range-based vol estimate.
_MIN_VOL_BARS = 5
#: Closed paper trades required before the Kelly multiplier rises 0.25 -> 0.5.
KELLY_CALIBRATION_MIN_TRADES = 50
KELLY_COLD_FRACTION = 0.25
KELLY_SEASONED_FRACTION = 0.5
#: 30d correlation to BTC above which a symbol joins the BTC-beta bucket.
BTC_CLUSTER_CORR = 0.7


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _finite(v: Any) -> float | None:
    """Coerce to a finite float; ``None`` for anything malformed (NaN/inf/non-numeric)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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


def parkinson_vol(bars: list[dict[str, Any]], periods_per_year: float = 365.0) -> float | None:
    """Parkinson range volatility: sqrt(sum(ln(H/L)^2) / (4 ln2 * N)) * sqrt(periods_per_year).

    ~5x more statistically efficient than close-to-close (it uses the full bar range),
    so a 7-10 bar window already yields a stable sizing input — vol targeting on a fast
    estimator is the documented Sharpe lever for leveraged crypto momentum.
    """
    ppy = _finite(periods_per_year)
    if ppy is None or ppy <= 0 or not isinstance(bars, list):
        return None
    sq_ranges: list[float] = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        high = _finite(b.get("high"))
        low = _finite(b.get("low"))
        if high is not None and low is not None and low > 0 and high >= low:
            sq_ranges.append(math.log(high / low) ** 2)
    if len(sq_ranges) < _MIN_VOL_BARS:
        return None
    var = sum(sq_ranges) / (_PARKINSON_FACTOR * len(sq_ranges))
    return math.sqrt(var * ppy)


def yang_zhang_vol(bars: list[dict[str, Any]], periods_per_year: float = 365.0) -> float | None:
    """Yang-Zhang OHLC volatility: sigma^2 = sigma_open^2 + k*sigma_close^2 + (1-k)*sigma_RS^2.

    Drift-independent and overnight-gap-aware (k = 0.34 / (1.34 + (n+1)/(n-1)); RS is
    the Rogers-Satchell term), ~8-14x more efficient than close-to-close (Yang-Zhang
    2000) — on 24/7 gapless crypto bars it converges toward the Parkinson estimate.
    """
    ppy = _finite(periods_per_year)
    if ppy is None or ppy <= 0 or not isinstance(bars, list):
        return None
    rows: list[tuple[float, float, float, float]] = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        o = _finite(b.get("open"))
        h = _finite(b.get("high"))
        low = _finite(b.get("low"))
        c = _finite(b.get("close"))
        if o is None or h is None or low is None or c is None:
            continue
        if o > 0 and low > 0 and c > 0 and h >= low:
            rows.append((o, h, low, c))
    if len(rows) < _MIN_VOL_BARS + 1:
        return None
    o_terms: list[float] = []
    c_terms: list[float] = []
    rs_terms: list[float] = []
    for i in range(1, len(rows)):
        o, h, low, c = rows[i]
        prev_close = rows[i - 1][3]
        o_i = math.log(o / prev_close)
        c_i = math.log(c / o)
        u = math.log(h / o)
        d = math.log(low / o)
        o_terms.append(o_i)
        c_terms.append(c_i)
        rs_terms.append(u * (u - c_i) + d * (d - c_i))
    n = len(o_terms)
    mean_o = sum(o_terms) / n
    mean_c = sum(c_terms) / n
    var_open = sum((x - mean_o) ** 2 for x in o_terms) / (n - 1)
    var_close = sum((x - mean_c) ** 2 for x in c_terms) / (n - 1)
    var_rs = sum(rs_terms) / n
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = var_open + k * var_close + (1.0 - k) * var_rs
    return math.sqrt(max(var, 0.0) * ppy)


def kelly_calibrated(
    conviction: float,
    journal_stats: dict[str, Any] | None,
    reward_to_risk: float = DEFAULT_REWARD_MULT,
) -> dict[str, float]:
    """Journal-calibrated Kelly: p_eff = min(p_conviction, (wins+2)/(n+4)), quarter -> half.

    Caps the conviction-implied hit-rate with the Laplace-shrunk realized one (Benter:
    good models overestimate edge ~2x, turning intended full-Kelly into ruinous
    2x-Kelly) and holds the multiplier at quarter-Kelly until ``n >= 50`` closed trades
    prove the edge, then half-Kelly (Thorp/MacLean-Ziemba: ~75% of growth, far smaller
    drawdowns). ``journal_stats`` accepts ``wins`` directly or ``hit_rate * n_closed``.

    Returns ``{"p_conviction", "p_hat", "p_eff", "n_closed", "fraction",
    "kelly_full", "kelly_used"}``.
    """
    stats = journal_stats if isinstance(journal_stats, dict) else {}
    n_raw = _finite(stats.get("n_closed", stats.get("n")))
    n = max(0, int(n_raw)) if n_raw is not None else 0
    wins_raw = _finite(stats.get("wins"))
    if wins_raw is None:
        hit_rate = _finite(stats.get("hit_rate"))
        wins_raw = _clamp(hit_rate, 0.0, 1.0) * n if hit_rate is not None else 0.0
    wins = _clamp(wins_raw, 0.0, float(n))
    conv = _finite(conviction)
    p_conviction = _clamp(0.5 + 0.25 * _clamp(conv if conv is not None else 0.0, 0.0, 1.0), 0.0, 1.0)
    p_hat = (wins + 2.0) / (n + 4.0)
    p_eff = min(p_conviction, p_hat)
    fraction = KELLY_SEASONED_FRACTION if n >= KELLY_CALIBRATION_MIN_TRADES else KELLY_COLD_FRACTION
    b_raw = _finite(reward_to_risk)
    b = max(b_raw if b_raw is not None else DEFAULT_REWARD_MULT, 1e-6)
    kelly_full = p_eff - (1.0 - p_eff) / b
    kelly_used = max(0.0, fraction * kelly_full)
    return {
        "p_conviction": round(p_conviction, 4),
        "p_hat": round(p_hat, 4),
        "p_eff": round(p_eff, 4),
        "n_closed": float(n),
        "fraction": fraction,
        "kelly_full": round(kelly_full, 4),
        "kelly_used": round(kelly_used, 4),
    }


def correlated_exposure_check(
    positions: list[dict[str, Any]] | None,
    candidate: dict[str, Any] | None,
    betas: dict[str, Any] | None,
    cap_mult: float = 2.0,
    *,
    equity: float | None = None,
) -> dict[str, Any]:
    """Portfolio beta-cluster exposure gate: allow / downsize / refuse at order time.

    Symbols with BTC correlation > 0.7 share ONE bucket (in cascades nearly every alt
    converges to BTC beta — three "independent" 5x alt longs are one 15x BTC long when
    it matters); the candidate is allowed while ``sum(|notional_i * beta_i|)`` in its
    bucket stays under ``cap_mult * equity``, auto-downsized into remaining headroom,
    and refused when the bucket is already at/over the cap.

    Args:
        positions: Open positions, each ``{"symbol", "notional", ...}`` (notional USD).
        candidate: Proposed order ``{"symbol", "notional", ...}``; ``"equity"`` is read
            from here when the ``equity`` keyword is absent.
        betas: ``{symbol: {"beta": float, "corr": float}}`` (bare floats treated as
            beta). Unknown symbols default conservatively to beta 1.0 in the BTC bucket.
        cap_mult: Bucket cap as a multiple of equity (default 2.0).
        equity: Account equity in USD; without it the cap cannot be computed and the
            gate passes through with an explicit "skipped" reason.

    Returns ``{"allowed", "scaled_notional", "reason", "bucket", "bucket_exposure",
    "cap"}`` — total, never raises.
    """

    def _beta_info(sym: str) -> tuple[float, float]:
        raw = betas.get(sym) if isinstance(betas, dict) else None
        if isinstance(raw, dict):
            beta = _finite(raw.get("beta"))
            corr = _finite(raw.get("corr"))
        else:
            beta = _finite(raw)
            corr = None
        # Conservative defaults: an unknown crypto behaves like BTC under stress.
        return beta if beta is not None else 1.0, corr if corr is not None else 1.0

    def _bucket(sym: str, corr: float) -> str:
        return "BTC" if corr > BTC_CLUSTER_CORR else sym

    cand = candidate if isinstance(candidate, dict) else {}
    sym = str(cand.get("symbol") or "").upper()
    notional = _finite(cand.get("notional"))
    if not sym or notional is None or notional <= 0:
        return {
            "allowed": True,
            "scaled_notional": 0.0,
            "reason": "no candidate notional -> nothing to gate",
            "bucket": None,
            "bucket_exposure": 0.0,
            "cap": None,
        }
    cand_beta, cand_corr = _beta_info(sym)
    bucket = _bucket(sym, cand_corr)
    eq = _finite(equity)
    if eq is None:
        eq = _finite(cand.get("equity"))
    cm = _finite(cap_mult)
    if cm is None or cm <= 0:
        cm = 2.0
    if eq is None or eq <= 0:
        return {
            "allowed": True,
            "scaled_notional": round(notional, 2),
            "reason": "no equity reference -> correlation cap skipped",
            "bucket": bucket,
            "bucket_exposure": 0.0,
            "cap": None,
        }
    cap = cm * eq
    bucket_exposure = 0.0
    for pos in positions if isinstance(positions, list) else []:
        if not isinstance(pos, dict):
            continue
        psym = str(pos.get("symbol") or "").upper()
        pnotional = _finite(pos.get("notional"))
        if not psym or pnotional is None or pnotional == 0:
            continue
        pbeta, pcorr = _beta_info(psym)
        if _bucket(psym, pcorr) == bucket:
            bucket_exposure += abs(pnotional) * abs(pbeta)
    cand_exposure = notional * abs(cand_beta)
    headroom = cap - bucket_exposure
    base = {"bucket": bucket, "bucket_exposure": round(bucket_exposure, 2), "cap": round(cap, 2)}
    if cand_exposure <= max(headroom, 0.0):
        return {
            "allowed": True,
            "scaled_notional": round(notional, 2),
            "reason": (
                f"bucket {bucket} beta-notional {bucket_exposure:.0f}+{cand_exposure:.0f}"
                f" <= cap {cap:.0f} -> allowed"
            ),
            **base,
        }
    if headroom <= 0:
        return {
            "allowed": False,
            "scaled_notional": 0.0,
            "reason": (
                f"bucket {bucket} beta-notional {bucket_exposure:.0f} >= cap {cap:.0f}"
                " -> refused (no headroom)"
            ),
            **base,
        }
    scaled = headroom / abs(cand_beta)
    return {
        "allowed": True,
        "scaled_notional": round(scaled, 2),
        "reason": (
            f"bucket {bucket} beta-notional {bucket_exposure:.0f}+{cand_exposure:.0f}"
            f" > cap {cap:.0f} -> downsized to {scaled:.0f}"
        ),
        **base,
    }


__all__ = [
    "atr",
    "kelly_fraction_from_conviction",
    "position_sizing",
    "parkinson_vol",
    "yang_zhang_vol",
    "kelly_calibrated",
    "correlated_exposure_check",
    "DEFAULT_TARGET_VOL",
    "DEFAULT_MAX_POSITION",
    "KELLY_CALIBRATION_MIN_TRADES",
    "BTC_CLUSTER_CORR",
]
