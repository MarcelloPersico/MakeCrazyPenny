"""Crypto market-regime filter (CONTRACT.md §16).

The crypto analogue of :mod:`makecrazypenny.analysis.regime`: a trend +
volatility read on **BTC** (the market beta for crypto) that scales how much
gross exposure to take, with a crypto-tuned volatility target and a Fear & Greed
overlay. "Don't fight the daily BTC trend" is the single most robust backdrop for
a short-window leveraged book.

Rule: **risk-on** when BTC is above its long SMA *and* 12-1 momentum is positive;
**caution** when only one holds; **risk-off** when both fail. A volatility overlay
shrinks gross when BTC realized vol runs hot, and an *extreme* Fear & Greed value
trims gross further (froth and panic both precede sharp reversals). Pure core
operates on bars; the async fetcher pulls BTC daily history + the index through
the cached registry and never raises.
"""

from __future__ import annotations

from typing import Any

from ..analysis.factors import _floats, momentum_12_1, realized_vol, trend_vs_sma
from ..core.config import Settings

#: Base gross-exposure scalars per regime.
_GROSS = {"risk_on": 1.0, "caution": 0.6, "risk_off": 0.3}
#: Volatility overlay floor (never cut gross below this multiple on vol alone).
_VOL_FLOOR = 0.4
#: Fear & Greed values at/beyond which gross is trimmed for froth/panic.
_FNG_GREED = 80.0
_FNG_FEAR = 20.0
_FNG_TRIM = 0.85


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def crypto_regime_from_bars(
    bars: list[dict[str, Any]],
    *,
    benchmark: str = "BTCUSDT",
    target_vol: float = 0.80,
    fng_value: float | None = None,
) -> dict[str, Any]:
    """Compute the crypto regime + gross-exposure scalar from BTC daily bars.

    Pure function. Falls back to a shorter trend SMA when history is thin, and to
    a neutral ``caution`` regime when even that is unavailable.
    """
    closes = _floats(bars, "close")
    trend = trend_vs_sma(closes, window=200)
    if trend is None and len(closes) >= 100:
        trend = trend_vs_sma(closes, window=100)
    tsmom = momentum_12_1(closes)
    if tsmom is None and len(closes) >= 60:
        # Short-history fallback: the 60d->21d return, skipping the most recent
        # ~3 weeks to keep the same reversal control as the full 12-1 measure.
        start = closes[-60]
        tsmom = (closes[-21] / start - 1.0) if start > 0 else None
    # Crypto trades every calendar day: annualize daily vol with sqrt(365).
    vol = realized_vol(closes, periods_per_year=365.0)

    if trend is None or tsmom is None:
        return {
            "benchmark": benchmark,
            "regime": "caution",
            "gross_exposure": _GROSS["caution"],
            "above_trend": None,
            "ts_momentum": tsmom,
            "realized_vol": vol,
            "vol_scale": 1.0,
            "fng_value": fng_value,
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
        vol_scale = _clamp(target_vol / vol, _VOL_FLOOR, 1.0)

    fng_scale = 1.0
    if fng_value is not None and (fng_value >= _FNG_GREED or fng_value <= _FNG_FEAR):
        fng_scale = _FNG_TRIM

    gross = round(_GROSS[regime] * vol_scale * fng_scale, 3)
    return {
        "benchmark": benchmark,
        "regime": regime,
        "gross_exposure": gross,
        "above_trend": above,
        "ts_momentum": round(tsmom, 4),
        "realized_vol": round(vol, 4) if vol else None,
        "vol_scale": round(vol_scale, 3),
        "fng_value": fng_value,
        "fng_scale": fng_scale,
        "n_bars": len(closes),
    }


async def crypto_regime(
    *, benchmark: str = "BTCUSDT", settings: Settings | None = None
) -> dict[str, Any]:
    """Fetch BTC daily history + Fear & Greed and compute the crypto regime.

    Never raises — on a data failure returns a neutral ``caution`` regime with an
    ``_error`` note so callers can still size conservatively.
    """
    settings = settings or Settings.from_env()
    from ..providers import get_registry

    registry = get_registry()
    try:
        env = await registry.fetch("crypto_ohlcv", symbol=benchmark, interval="1d", limit=400)
        data = env.get("data") if isinstance(env, dict) else {}
        bars = data.get("bars", []) if isinstance(data, dict) else []
    except Exception as exc:
        return {
            "benchmark": benchmark,
            "regime": "caution",
            "gross_exposure": _GROSS["caution"],
            "_error": f"{type(exc).__name__}: {exc}",
        }

    fng_value: float | None = None
    try:
        fenv = await registry.fetch("crypto_sentiment")
        fdata = fenv.get("data") if isinstance(fenv, dict) else {}
        raw = fdata.get("value") if isinstance(fdata, dict) else None
        fng_value = float(raw) if raw is not None else None
    except Exception:
        fng_value = None  # the overlay simply won't apply

    return crypto_regime_from_bars(
        bars, benchmark=benchmark, target_vol=settings.crypto_target_vol, fng_value=fng_value
    )


__all__ = ["crypto_regime_from_bars", "crypto_regime", "_GROSS"]
