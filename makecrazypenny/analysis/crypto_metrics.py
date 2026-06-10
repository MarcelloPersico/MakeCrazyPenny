"""Crypto derivatives signal cores (CONTRACT.md §16).

Pure functions that turn perpetual-futures metrics into a normalized directional
**strength** in ``[-1, 1]`` (positive = bullish) plus a short human-readable
detail string. The crypto decision engine multiplies each strength by a category
weight and folds it into the same weighted-factor scoring used for equities.

Research basis (see RESEARCH.md §crypto):

* **Funding rate** — *contrarian at extremes*. Persistently positive funding means
  longs are crowded and paying to hold; that overhang fuels long squeezes
  (bearish). Persistently negative funding means crowded shorts (short-squeeze
  fuel, bullish). Scored on the annualized rate.
* **Open interest x price** — the classic matrix. Rising OI confirms the price
  move (new money); falling OI means the move is an unwind/short-cover and is
  faded.
* **Long/short account ratio** — *contrarian*. When the crowd is overwhelmingly
  long, fade it; when overwhelmingly short, lean long.
* **Fear & Greed** — *contrarian*. Extreme greed precedes pullbacks; extreme fear
  precedes bounces.
* **Taker flow / CVD** — *pro-trend*. Aggressor order-flow imbalance is the
  best-evidenced short-horizon price driver (Cont-Kukanov-Stoikov OFI); CVD-vs-price
  divergence is the standard perp absorption/exhaustion read.
* **Top-trader spread** — *follow smart money*. The top-20%-by-margin position ratio
  vs the global account ratio isolates informed-vs-retail divergence (COT-style).
* **Funding z / predicted funding** — *contrarian at regime-relative extremes*.
  Per-symbol z-scores (BIS WP1087: high carry precedes deleveraging) plus HL
  predicted fundings (1h updates front-run 8h CEX settlements).
* **Social velocity** — deterministic buzz counting only (post velocity x
  platform-native bullish/bearish tallies); no LLM interpretation enters scoring.

The perpetual **basis** (mark vs index) is surfaced for transparency but not
scored separately, because funding is mechanically derived from the same premium
(scoring both would double-count). ``depth_imbalance`` and ``venue_divergence``
are likewise *not scored*: they are order-time execution gates (book imbalance
decays in seconds-to-minutes; cross-venue divergence is a slippage sanity check).
"""

from __future__ import annotations

import math
from typing import Any

#: Annualized funding magnitude *beyond the baseline* treated as "fully crowded".
_FUNDING_SATURATION = 0.50
#: Equilibrium funding baseline: the standard +0.01%/8h interest-rate component
#: (~+11% annualized). Funding at this level is the market's resting state, not
#: positioning crowding — the contrarian signal is centered here so a normal
#: market scores ~0 instead of a permanent bearish drag on every long.
_FUNDING_BASELINE_ANN = 0.0001 * 3.0 * 365.0
#: Price move (fraction) that gives the OI/price signal full magnitude.
_OI_PRICE_SATURATION = 0.02
#: How much a *falling* OI fades (mildly reverses) the price move.
_OI_FADE = 0.3
#: Long/short ratio extreme (3:1) that gives the contrarian signal full strength.
_LS_EXTREME = 3.0
#: Fear & Greed thresholds: contrarian *only at the extremes* (the research
#: basis); mid-range readings carry no reliable contrarian information.
_FNG_GREED_EXTREME = 75.0
_FNG_FEAR_EXTREME = 25.0
#: Mean ln(taker buy/sell ratio) treated as fully directional (~10% persistent tilt).
_TAKER_FLOW_SATURATION = 0.10
#: Taker-flow ratio observations averaged (newest windows of the fetched series).
_TAKER_FLOW_WINDOW = 12
#: Bars in the CVD-vs-price slope comparison window.
_CVD_WINDOW = 20
#: |CVD delta| as a fraction of window volume that gives full magnitude.
_CVD_SATURATION = 0.20
#: ln(top/crowd) long-short ratio spread treated as a fully informed divergence.
_TOP_SPREAD_SATURATION = math.log(1.5)
#: Funding z-score dead zone (no contrarian edge inside) and full-strength extreme.
_FUNDING_Z_DEADZONE = 1.5
_FUNDING_Z_SATURATION = 3.0
#: Minimum funding-history observations for a meaningful z-score (~3.3d of 8h rates).
_FUNDING_Z_MIN_HISTORY = 10
#: Minimum |annualized predicted-funding excess| beyond baseline to call crowding "building".
_PREDICTED_MIN_EXCESS_ANN = 0.10
#: Social post-velocity spike cap (5x previous window) so one viral burst cannot dominate.
_SOCIAL_VELOCITY_CAP = 5.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _finite(v: Any) -> float | None:
    """Coerce to a finite float; ``None`` for anything malformed (NaN/inf/non-numeric)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def funding_signal(rate: float | None, annualized: float | None = None) -> tuple[float, str] | None:
    """Contrarian funding signal, centered on the equilibrium baseline.

    Funding *above* the resting +0.01%/8h baseline means longs are paying a
    crowding premium (bearish); below it (especially negative) means crowded
    shorts (bullish). Funding at the baseline scores ~0.

    Args:
        rate: The per-interval funding rate (e.g. ``0.0001``).
        annualized: Optional pre-annualized rate; computed from ``rate`` (assuming
            8h intervals) when absent.
    """
    if rate is None:
        return None
    a = annualized if annualized is not None else rate * 3.0 * 365.0
    excess = a - _FUNDING_BASELINE_ANN
    strength = -_clamp(excess / _FUNDING_SATURATION, -1.0, 1.0)
    crowd = "crowded longs" if excess > 0 else "crowded shorts" if excess < 0 else "balanced"
    return strength, f"funding {rate:+.4%}/interval (ann {a:+.1%}, baseline {_FUNDING_BASELINE_ANN:+.1%}) -> {crowd}"


def oi_price_signal(
    oi_change_pct: float | None, price_change_pct: float | None
) -> tuple[float, str] | None:
    """Open-interest x price matrix. Rising OI confirms; falling OI fades the move.

    Args:
        oi_change_pct: Fractional change in open interest over the window.
        price_change_pct: Fractional change in price over the same window.
    """
    if oi_change_pct is None or price_change_pct is None:
        return None
    price_dir = 1.0 if price_change_pct > 0 else -1.0 if price_change_pct < 0 else 0.0
    mag = _clamp(abs(price_change_pct) / _OI_PRICE_SATURATION, 0.0, 1.0)
    if oi_change_pct > 0:
        strength = price_dir * mag
        tag = "OI rising + price " + ("up: new longs (confirm)" if price_dir > 0 else "down: new shorts (confirm)")
    else:
        strength = price_dir * mag * -_OI_FADE
        tag = "OI falling: " + ("short-cover rally (fade)" if price_dir > 0 else "long unwind/flush (fade)")
    return _clamp(strength, -1.0, 1.0), f"{tag} [OI {oi_change_pct:+.1%}, px {price_change_pct:+.1%}]"


def long_short_signal(ratio: float | None) -> tuple[float, str] | None:
    """Contrarian long/short positioning signal (ratio = longs/shorts)."""
    if ratio is None or ratio <= 0:
        return None
    lr = math.log(ratio)
    strength = -_clamp(lr / math.log(_LS_EXTREME), -1.0, 1.0)
    crowd = "crowd long" if ratio > 1 else "crowd short" if ratio < 1 else "balanced"
    return strength, f"long/short {ratio:.2f} -> {crowd} (contrarian)"


def fear_greed_signal(value: float | None) -> tuple[float, str] | None:
    """Contrarian Fear & Greed signal — extremes only.

    Extreme greed (>= 75) is bearish, extreme fear (<= 25) is bullish, ramping
    to full strength at 100/0. The 25..75 mid-range scores 0: contrarian value
    in sentiment indices is documented at the extremes; mid-range readings
    behave more like momentum and are not a fade signal.
    """
    if value is None:
        return None
    if value >= _FNG_GREED_EXTREME:
        strength = -_clamp((value - _FNG_GREED_EXTREME) / (100.0 - _FNG_GREED_EXTREME), 0.0, 1.0)
        mood = "extreme greed (fade)"
    elif value <= _FNG_FEAR_EXTREME:
        strength = _clamp((_FNG_FEAR_EXTREME - value) / _FNG_FEAR_EXTREME, 0.0, 1.0)
        mood = "extreme fear (buy dip)"
    else:
        strength = 0.0
        mood = "mid-range (no contrarian edge)"
    return strength, f"Fear&Greed {int(value)}/100 -> {mood}"


def basis_value(mark_price: float | None, index_price: float | None) -> float | None:
    """Perpetual basis (mark/index - 1) for transparency (not scored separately)."""
    if mark_price and index_price and index_price > 0:
        return mark_price / index_price - 1.0
    return None


def oi_change_from_history(history: Any) -> float | None:
    """Fractional change in open interest across an ascending history list.

    ``history`` is a list of ``{"open_interest": float, ...}`` (oldest -> newest),
    as returned by the crypto providers. Returns ``None`` when it cannot compute.
    """
    if not isinstance(history, list) or len(history) < 2:
        return None
    first = None
    last = None
    for row in history:
        if isinstance(row, dict):
            oi = row.get("open_interest")
            if isinstance(oi, (int, float)) and oi > 0:
                if first is None:
                    first = float(oi)
                last = float(oi)
    if first is None or last is None or first <= 0:
        return None
    return last / first - 1.0


def taker_flow_signal(
    series: Any, window: int = _TAKER_FLOW_WINDOW
) -> tuple[float, str] | None:
    """Pro-trend taker aggressor-flow signal: mean ln(buy/sell ratio) saturated at 0.10.

    ``strength = clamp(mean(ln r) / 0.10, -1, 1)`` over the last ``window``
    observations — order-flow imbalance is a linear, dominant short-horizon price
    driver (Cont-Kukanov-Stoikov, arxiv 1011.6402), so persistent net taker buying
    confirms the move. ``series`` rows are ``{"buy_sell_ratio": ...}`` dicts (or
    bare numbers), oldest -> newest.
    """
    if not isinstance(series, list) or not series:
        return None
    logs: list[float] = []
    for row in series:
        raw = row.get("buy_sell_ratio") if isinstance(row, dict) else row
        r = _finite(raw)
        if r is not None and r > 0:
            logs.append(math.log(r))
    if not logs:
        return None
    tail = logs[-max(1, int(window)):]
    mean_log = sum(tail) / len(tail)
    strength = _clamp(mean_log / _TAKER_FLOW_SATURATION, -1.0, 1.0)
    side = "buyers" if mean_log > 0 else "sellers" if mean_log < 0 else "neither side"
    return strength, f"taker flow mean ln(ratio) {mean_log:+.4f} over {len(tail)}w -> {side} in control"


def cvd_signal(bars: Any, window: int = _CVD_WINDOW) -> tuple[float, str] | None:
    """CVD slope vs price slope over the last ``window`` bars: confirm (+) / diverge (-).

    Per bar ``delta = 2*taker_buy_volume - volume``; the CVD slope over the window is
    the delta sum, normalized by window volume and saturated at 0.20; the strength
    carries the CVD direction, so agreement with the price slope confirms the move
    while disagreement is the standard absorption/exhaustion fade (CVD divergence on
    perps, on top of the OFI price-impact literature).
    """
    if not isinstance(bars, list):
        return None
    rows: list[tuple[float, float, float]] = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        close = _finite(b.get("close"))
        vol = _finite(b.get("volume"))
        buy = _finite(b.get("taker_buy_volume"))
        if close is None or vol is None or buy is None or vol < 0:
            continue
        rows.append((close, vol, buy))
    w = max(2, int(window))
    if len(rows) < w:
        return None
    tail = rows[-w:]
    total_vol = sum(v for _, v, _ in tail)
    if total_vol <= 0:
        return None
    cvd_change = sum(2.0 * buy - vol for _, vol, buy in tail)
    imbalance = cvd_change / total_vol
    price_change = tail[-1][0] - tail[0][0]
    mag = _clamp(abs(imbalance) / _CVD_SATURATION, 0.0, 1.0)
    cvd_dir = 1.0 if cvd_change > 0 else -1.0 if cvd_change < 0 else 0.0
    price_dir = 1.0 if price_change > 0 else -1.0 if price_change < 0 else 0.0
    if cvd_dir == 0.0 or price_dir == 0.0:
        tag = "flat CVD or price (no read)"
    elif cvd_dir == price_dir:
        tag = "CVD confirms price " + ("up" if price_dir > 0 else "down")
    else:
        tag = "CVD diverges from price " + ("up (absorption)" if price_dir > 0 else "down (accumulation)")
    return cvd_dir * mag, f"CVD delta {imbalance:+.3f} of {w}-bar volume -> {tag}"


def top_trader_spread_signal(
    top_ratio: float | None, crowd_ratio: float | None
) -> tuple[float, str] | None:
    """Follow-the-smart-money positioning spread: clamp(ln(top/crowd) / ln(1.5), -1, 1).

    The top-20%-by-margin position ratio vs the global account ratio isolates the
    informed-vs-retail divergence (COT commercial-vs-speculator logic): positive when
    top traders are longer than the crowd (follow them), saturating at a 1.5x spread.
    """
    top = _finite(top_ratio)
    crowd = _finite(crowd_ratio)
    if top is None or crowd is None or top <= 0 or crowd <= 0:
        return None
    spread = math.log(top / crowd)
    strength = _clamp(spread / _TOP_SPREAD_SATURATION, -1.0, 1.0)
    if spread > 0:
        lean = "tops longer than crowd (follow long)"
    elif spread < 0:
        lean = "tops shorter than crowd (follow short)"
    else:
        lean = "tops aligned with crowd"
    return strength, f"top/crowd L-S {top:.2f}/{crowd:.2f}, ln spread {spread:+.3f} -> {lean}"


def funding_z_signal(rates: Any, current: float | None) -> tuple[float, str] | None:
    """Contrarian funding z-score vs the symbol's OWN trailing history; dead zone |z| < 1.5.

    ``z = (current - mean) / std`` over ~21d of settlements; strength ramps
    ``-sign(z) * (|z| - 1.5) / 1.5`` to full at |z| >= 3 — per-symbol regime-relative
    crowding (alts have structurally different funding regimes; BIS WP1087: high carry
    predicts deleveraging crashes), zero inside the dead zone. ``rates`` rows are
    ``{"rate": ...}`` dicts (or bare numbers), oldest -> newest.
    """
    cur = _finite(current)
    if cur is None or not isinstance(rates, list):
        return None
    vals: list[float] = []
    for row in rates:
        raw = row.get("rate") if isinstance(row, dict) else row
        r = _finite(raw)
        if r is not None:
            vals.append(r)
    n = len(vals)
    if n < _FUNDING_Z_MIN_HISTORY:
        return None
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    if std <= 1e-12:
        return None
    z = (cur - mean) / std
    if abs(z) < _FUNDING_Z_DEADZONE:
        return 0.0, f"funding z {z:+.2f} vs {n}-obs history -> inside dead zone (no edge)"
    ramp = _clamp(
        (abs(z) - _FUNDING_Z_DEADZONE) / (_FUNDING_Z_SATURATION - _FUNDING_Z_DEADZONE), 0.0, 1.0
    )
    crowd = "crowded longs (fade)" if z > 0 else "crowded shorts (fade)"
    return -math.copysign(ramp, z), f"funding z {z:+.2f} vs {n}-obs history -> {crowd}"


def predicted_funding_signal(
    current_hourly: float | None, predicted_venues: Any
) -> tuple[float, str] | None:
    """Predicted-funding flip/extreme detector: contrarian only when crowding is BUILDING.

    Annualizes the HL predicted rate (mean of venues when HL absent; per-venue rates
    normalized by ``interval_hours``) and the current hourly rate; when the predicted
    excess over the equilibrium baseline shares the current sign, exceeds it, and is
    at least 10% annualized, crowding is intensifying -> contrarian strength
    ``-clamp(pred_excess / 0.50)``; aligned/compressing/small -> 0 (HL's 1h fundings
    front-run 8h CEX settlements, so compression means the squeeze is resolving).
    """
    cur = _finite(current_hourly)
    if cur is None or not isinstance(predicted_venues, list):
        return None
    hourly: list[float] = []
    hl_hourly: float | None = None
    for row in predicted_venues:
        if not isinstance(row, dict):
            continue
        rate = _finite(row.get("rate"))
        if rate is None:
            continue
        iv = _finite(row.get("interval_hours"))
        per_hr = rate / iv if iv is not None and iv > 0 else rate
        hourly.append(per_hr)
        if str(row.get("venue") or "").lower().startswith("hl"):
            hl_hourly = per_hr
    if not hourly:
        return None
    pred = hl_hourly if hl_hourly is not None else sum(hourly) / len(hourly)
    curr_ex = cur * 24.0 * 365.0 - _FUNDING_BASELINE_ANN
    pred_ex = pred * 24.0 * 365.0 - _FUNDING_BASELINE_ANN
    building = (
        pred_ex * curr_ex > 0
        and abs(pred_ex) > abs(curr_ex)
        and abs(pred_ex) >= _PREDICTED_MIN_EXCESS_ANN
    )
    if not building:
        return 0.0, (
            f"predicted funding ann excess {pred_ex:+.1%} vs current {curr_ex:+.1%}"
            " -> compressing/aligned (no kicker)"
        )
    strength = -_clamp(pred_ex / _FUNDING_SATURATION, -1.0, 1.0)
    crowd = "long crowding building (fade)" if pred_ex > 0 else "short crowding building (fade)"
    return strength, f"predicted funding ann excess {pred_ex:+.1%} > current {curr_ex:+.1%} -> {crowd}"


def social_velocity_signal(
    post_velocity: float | None,
    prev_velocity: float | None,
    st_bull: float | None,
    st_bear: float | None,
) -> tuple[float, str] | None:
    """Deterministic social-buzz signal: directional share gated by a velocity spike.

    ``strength = (bull_share - 0.5) * 2 * min(1, log1p(velocity_ratio))`` with
    ``velocity_ratio = velocity / prev`` capped at 5x and ``bull_share`` from
    platform-native StockTwits Bullish/Bearish tallies — pure counting, never LLM
    interpretation, so rising buzz only amplifies a direction the labels already show.
    """
    vel = _finite(post_velocity)
    prev = _finite(prev_velocity)
    bull = _finite(st_bull)
    bear = _finite(st_bear)
    if vel is None or prev is None or bull is None or bear is None:
        return None
    if vel < 0 or prev < 0 or bull < 0 or bear < 0:
        return None
    total = bull + bear
    if total <= 0:
        return None
    bull_share = bull / total
    if prev > 0:
        ratio = min(vel / prev, _SOCIAL_VELOCITY_CAP)
    else:
        ratio = _SOCIAL_VELOCITY_CAP if vel > 0 else 0.0
    mult = min(1.0, math.log1p(ratio))
    strength = _clamp((bull_share - 0.5) * 2.0 * mult, -1.0, 1.0)
    lean = "bullish buzz" if strength > 0 else "bearish buzz" if strength < 0 else "no edge"
    return strength, (
        f"velocity x{ratio:.1f}, bull share {bull_share:.0%} ({int(bull)}/{int(total)}) -> {lean}"
    )


def depth_imbalance(
    bids: Any, asks: Any, mid: float | None, band_bps: float = 20.0
) -> float | None:
    """Signed book-depth imbalance within +/- ``band_bps`` of mid: (B - A) / (B + A).

    Order-time ENTRY GATE only, never a scored factor — book imbalance is the dominant
    next-move predictor at second-to-minute horizons (Cont et al.) and decays before a
    minutes-cadence engine can act on it. ``bids``/``asks`` are ``[[price, size], ...]``
    ladders (``{"px", "sz"}`` dicts also accepted).
    """
    m = _finite(mid)
    bb = _finite(band_bps)
    if m is None or m <= 0 or bb is None or bb <= 0:
        return None
    lo = m * (1.0 - bb / 1e4)
    hi = m * (1.0 + bb / 1e4)

    def _depth(levels: Any, lo_px: float, hi_px: float) -> float:
        total = 0.0
        if isinstance(levels, list):
            for lvl in levels:
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    px, sz = _finite(lvl[0]), _finite(lvl[1])
                elif isinstance(lvl, dict):
                    px, sz = _finite(lvl.get("px")), _finite(lvl.get("sz"))
                else:
                    continue
                if px is not None and sz is not None and sz > 0 and lo_px <= px <= hi_px:
                    total += sz
        return total

    bid_depth = _depth(bids, lo, m)
    ask_depth = _depth(asks, m, hi)
    total = bid_depth + ask_depth
    if total <= 0:
        return None
    return (bid_depth - ask_depth) / total


def venue_divergence(hl_mark: float | None, cex_mark: float | None) -> float | None:
    """Absolute HL-vs-CEX price divergence in basis points: ``|hl/cex - 1| * 1e4``.

    Execution sanity gate, not a scored factor: cross-venue divergences mean-revert in
    minutes via arb, and signals are computed from CEX data while fills happen on HL —
    a large gap means stale data or venue-localized flow (never market-enter the rich side).
    """
    hl = _finite(hl_mark)
    cex = _finite(cex_mark)
    if hl is None or cex is None or hl <= 0 or cex <= 0:
        return None
    return abs(hl / cex - 1.0) * 1e4


__all__ = [
    "funding_signal",
    "oi_price_signal",
    "long_short_signal",
    "fear_greed_signal",
    "basis_value",
    "oi_change_from_history",
    "taker_flow_signal",
    "cvd_signal",
    "top_trader_spread_signal",
    "funding_z_signal",
    "predicted_funding_signal",
    "social_velocity_signal",
    "depth_imbalance",
    "venue_divergence",
]
