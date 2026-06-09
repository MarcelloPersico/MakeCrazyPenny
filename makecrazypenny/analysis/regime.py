"""Market-regime filter (CONTRACT.md §10.7.3).

A trend + volatility regime read on a benchmark (default SPY) that scales how much
gross exposure the system should take — the single most evidence-backed "timing"
signal that survives out-of-sample (Faber 2007 trend timing; Moskowitz-Ooi-Pedersen
time-series momentum; Moreira-Muir volatility management; see plan.md §10).

Rule: **risk-on** when price is above its 200-day SMA *and* 12-1 month index
momentum is positive (full gross); **caution** when only one holds (reduced);
**risk-off** when both fail (low gross). A volatility overlay further dampens gross
when index volatility is elevated. Pure core operates on bars; the async fetcher
pulls the benchmark history through the cached registry.
"""

from __future__ import annotations

from typing import Any

from ..analysis.factors import _floats, momentum_12_1, realized_vol, trend_vs_sma
from ..core.config import Settings

#: Base gross-exposure scalars per regime.
_GROSS = {"risk_on": 1.0, "caution": 0.6, "risk_off": 0.3}
#: Index volatility the overlay targets; higher realized vol shrinks gross.
_TARGET_INDEX_VOL = 0.16
_VOL_FLOOR = 0.5  # vol overlay never cuts gross below this multiple


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def regime_from_bars(bars: list[dict[str, Any]], *, benchmark: str = "SPY") -> dict[str, Any]:
    """Compute the market regime + gross-exposure scalar from benchmark bars.

    Pure function. Returns ``{regime, gross_exposure, above_200dma, ts_momentum,
    realized_vol, vol_scale, benchmark, n_bars}``. With insufficient history it
    returns a neutral ``caution`` regime rather than failing.
    """
    closes = _floats(bars, "close")
    trend = trend_vs_sma(closes, window=200)
    tsmom = momentum_12_1(closes)
    vol = realized_vol(closes)

    if trend is None or tsmom is None:
        return {
            "benchmark": benchmark,
            "regime": "caution",
            "gross_exposure": _GROSS["caution"],
            "above_200dma": None,
            "ts_momentum": tsmom,
            "realized_vol": vol,
            "vol_scale": 1.0,
            "n_bars": len(closes),
            "note": "insufficient history; defaulting to caution",
        }

    above = trend > 0
    tsmom_pos = tsmom > 0
    if above and tsmom_pos:
        regime = "risk_on"
    elif above or tsmom_pos:
        regime = "caution"
    else:
        regime = "risk_off"

    vol_scale = 1.0
    if vol and vol > 0:
        vol_scale = _clamp(_TARGET_INDEX_VOL / vol, _VOL_FLOOR, 1.0)

    gross = round(_GROSS[regime] * vol_scale, 3)
    return {
        "benchmark": benchmark,
        "regime": regime,
        "gross_exposure": gross,
        "above_200dma": above,
        "ts_momentum": round(tsmom, 4),
        "realized_vol": round(vol, 4) if vol else None,
        "vol_scale": round(vol_scale, 3),
        "n_bars": len(closes),
    }


async def market_regime(*, benchmark: str = "SPY", settings: Settings | None = None) -> dict[str, Any]:
    """Fetch benchmark history and compute the market regime (never raises).

    On a data failure returns a neutral ``caution`` regime with an ``_error`` note
    so callers can still size conservatively.
    """
    from ..servers import technical as tech

    try:
        ohlcv = await tech.get_ohlcv(benchmark, interval="1d", period="2y")
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {
            "benchmark": benchmark,
            "regime": "caution",
            "gross_exposure": _GROSS["caution"],
            "_error": f"{type(exc).__name__}: {exc}",
        }
    return regime_from_bars(bars, benchmark=benchmark)


__all__ = ["regime_from_bars", "market_regime", "_GROSS"]
