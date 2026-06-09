"""Factor signals computed from free OHLCV + fundamentals (CONTRACT.md §10.7.1).

The replicated, free-data factors from the research shortlist (plan.md §10):
cross-sectional **momentum (12-1)**, **52-week-high proximity**, **trend**
(price vs 200-day SMA), **realized volatility** (low-vol / sizing input), and —
when free fundamentals are present — **value** (earnings/book/FCF yield) and
**quality** (gross profitability, ROE, margins).

Pure core: :func:`factor_values` operates on plain ``bars``/``fundamentals`` and
never does I/O. :func:`compute_factors` is the thin async fetcher that pulls daily
history (and best-effort fundamentals) through the Layer-0 registry. Missing data
degrades gracefully — a factor that cannot be computed is simply omitted.
"""

from __future__ import annotations

import math
from typing import Any

from ..analysis.risk import atr
from ..core.config import Settings

#: Trading days used for the lookbacks.
_YEAR = 252
_SKIP = 21  # skip the most recent ~1 month for 12-1 momentum (reversal control)
_VOL_WINDOW = 126
_SMA_WINDOW = 200


def _floats(bars: list[dict[str, Any]], key: str) -> list[float]:
    """Extract a clean float series for ``key`` from OHLCV bars (NaNs dropped)."""
    out: list[float] = []
    for b in bars:
        try:
            v = float(b.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def momentum_12_1(closes: list[float]) -> float | None:
    """12-1 month cumulative return: ``close[-21]/close[-252] - 1`` (skips last ~1mo)."""
    if len(closes) < _YEAR + 1:
        return None
    start = closes[-_YEAR]
    end = closes[-_SKIP] if _SKIP > 0 else closes[-1]
    if start <= 0:
        return None
    return end / start - 1.0


def pct_of_52w_high(closes: list[float], highs: list[float]) -> float | None:
    """Current close as a fraction of the trailing 52-week high (≈1.0 at the high)."""
    if not closes or not highs:
        return None
    window_high = max(highs[-_YEAR:]) if len(highs) >= _YEAR else max(highs)
    if window_high <= 0:
        return None
    return closes[-1] / window_high


def trend_vs_sma(closes: list[float], window: int = _SMA_WINDOW) -> float | None:
    """Price relative to its ``window``-day SMA: ``close/SMA - 1`` (>0 = uptrend)."""
    if len(closes) < window:
        return None
    sma = sum(closes[-window:]) / window
    if sma <= 0:
        return None
    return closes[-1] / sma - 1.0


def realized_vol(closes: list[float], window: int = _VOL_WINDOW) -> float | None:
    """Annualized realized volatility from daily log returns over ``window`` days."""
    series = closes[-(window + 1):]
    if len(series) < 20:
        return None
    rets = [math.log(series[i] / series[i - 1]) for i in range(1, len(series)) if series[i - 1] > 0]
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(_YEAR)


def _unwrap_fundamentals(fundamentals: Any) -> dict[str, Any]:
    """Return the inner fundamentals dict (yfinance nests it under 'fundamentals')."""
    if not isinstance(fundamentals, dict):
        return {}
    inner = fundamentals.get("fundamentals")
    if isinstance(inner, dict):
        return inner
    data = fundamentals.get("data")
    if isinstance(data, dict):
        return data
    return fundamentals


def _num(d: dict[str, Any], *keys: str) -> float | None:
    """First finite numeric value among ``keys`` (case-insensitive)."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = d.get(key)
        if v is None:
            v = lower.get(key.lower())
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def value_quality(fundamentals: Any) -> dict[str, float]:
    """Extract value + quality factor values from a free fundamentals payload.

    Defensive: reads common yfinance ``.info`` / provider keys and returns only
    the factors it can compute. Yields (when available):
    ``earnings_yield``, ``book_to_price``, ``fcf_yield`` (value) and
    ``gross_profitability``/``roe``/``profit_margin`` (quality).
    """
    info = _unwrap_fundamentals(fundamentals)
    if not info:
        return {}
    out: dict[str, float] = {}

    pe = _num(info, "trailingPE", "pe_ratio", "peRatio", "forwardPE")
    if pe and pe > 0:
        out["earnings_yield"] = 1.0 / pe
    pb = _num(info, "priceToBook", "price_to_book", "pb_ratio")
    if pb and pb > 0:
        out["book_to_price"] = 1.0 / pb
    fcf = _num(info, "freeCashflow", "free_cash_flow")
    mcap = _num(info, "marketCap", "market_cap")
    if fcf is not None and mcap and mcap > 0:
        out["fcf_yield"] = fcf / mcap

    gm = _num(info, "grossMargins", "gross_margin", "gross_profitability")
    if gm is not None:
        out["gross_profitability"] = gm
    roe = _num(info, "returnOnEquity", "roe")
    if roe is not None:
        out["roe"] = roe
    pm = _num(info, "profitMargins", "profit_margin", "net_margin")
    if pm is not None:
        out["profit_margin"] = pm
    return out


def factor_values(
    bars: list[dict[str, Any]], fundamentals: Any = None
) -> dict[str, Any]:
    """Compute all available factor values from ``bars`` (+ optional fundamentals).

    Pure function. Returns a dict with whichever of ``momentum_12_1``,
    ``pct_52w_high``, ``trend_200``, ``realized_vol`` (from price) and the
    value/quality factors (from fundamentals) could be computed, plus ``n_bars``.
    """
    closes = _floats(bars, "close")
    highs = _floats(bars, "high") or closes
    values: dict[str, Any] = {"n_bars": len(closes)}
    if closes:
        values["last_close"] = round(closes[-1], 6)
    atr14 = atr(bars, period=14)
    if atr14 is not None:
        values["atr14"] = round(atr14, 6)

    mom = momentum_12_1(closes)
    if mom is not None:
        values["momentum_12_1"] = round(mom, 6)
    p52 = pct_of_52w_high(closes, highs)
    if p52 is not None:
        values["pct_52w_high"] = round(p52, 6)
    trend = trend_vs_sma(closes)
    if trend is not None:
        values["trend_200"] = round(trend, 6)
    vol = realized_vol(closes)
    if vol is not None:
        values["realized_vol"] = round(vol, 6)

    values.update({k: round(v, 6) for k, v in value_quality(fundamentals).items()})
    return values


async def compute_factors(symbol: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch ~2y of daily history (+ best-effort fundamentals) and compute factors.

    Reads through the Layer-0 cached registry. Never raises — on a data failure it
    returns ``{"_error": ...}`` so the caller can fold in whatever is available.
    """
    from ..servers import technical as tech

    try:
        ohlcv = await tech.get_ohlcv(symbol, interval="1d", period="2y")
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}

    fundamentals: Any = None
    try:
        from ..providers import get_registry

        env = await get_registry().fetch("fundamentals", symbol=symbol)
        fundamentals = env.get("data") if isinstance(env, dict) else None
    except Exception:
        fundamentals = None  # value/quality simply won't contribute

    return factor_values(bars, fundamentals)


__all__ = [
    "momentum_12_1",
    "pct_of_52w_high",
    "trend_vs_sma",
    "realized_vol",
    "value_quality",
    "factor_values",
    "compute_factors",
]
