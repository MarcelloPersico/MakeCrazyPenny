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

The perpetual **basis** (mark vs index) is surfaced for transparency but not
scored separately, because funding is mechanically derived from the same premium
(scoring both would double-count).
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


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


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


__all__ = [
    "funding_signal",
    "oi_price_signal",
    "long_short_signal",
    "fear_greed_signal",
    "basis_value",
    "oi_change_from_history",
]
