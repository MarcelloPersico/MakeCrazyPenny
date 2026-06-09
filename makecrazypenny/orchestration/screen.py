"""Layer 2: whole-market screen — funnel a universe down to the best trades.

Extends the engine from a single ticker / one sector to a **whole investable
universe** (the S&P 500 by default). Running the full evidence engine on 500
names in one call is neither fast nor free, so this is a two-stage **funnel**,
exactly how a real quant screen works:

  1. **Prefilter (cheap, free, whole universe).** For every constituent, compute
     price-only factors (12-1 momentum, trend vs 200-DMA, 52-week-high proximity,
     realized vol) from a single free OHLCV pull and combine them into one
     composite score. This needs no API key and reuses the cached registry, so
     the full universe can be ranked within the rate budget.
  2. **Deep dive (full engine, shortlist only).** Take the strongest long and
     short candidates and run the complete deterministic decision engine
     (:func:`makecrazypenny.orchestration.debate.decide`) — evidence across every
     capability server, market regime, and ATR-based position sizing — on just
     those names. Surface the best longs and shorts as full
     :class:`~makecrazypenny.core.types.TradeDecision`\\ s so the user sees not
     only *what* to trade but *how* (entry/stop/target, size, invalidation).

Like the rest of the engine this is pure orchestration + cached I/O; the optional
bull-vs-bear debate over the finalists is run by the MCP host via the
``decide_market`` prompt. Both stages are bounded by semaphores so a wide sweep
never stampedes the providers, every name is independent (one failure becomes an
``errors`` entry), and the whole thing is offline-testable by monkeypatching the
two fetch points.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.factors import factor_values
from ..analysis.regime import market_regime
from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.types import MarketScreen
from ..core.universe import fetch_sp500
from ..servers._common import normalize_symbol
from .debate import decide

#: Names prefiltered concurrently (stage 1 is one cheap OHLCV pull each).
MAX_PREFILTER_CONCURRENCY = 8
#: Shortlisted names deep-dived concurrently (stage 2 fans out per name).
MAX_DEEP_CONCURRENCY = 5
#: Default number of candidates shortlisted per side for the deep dive.
DEFAULT_SHORTLIST = 15
#: Default number of long/short ideas surfaced.
DEFAULT_TOP_N = 3

# Prefilter composite weights — mirror the factor weights in ``debate`` so the
# cheap ranking is directionally consistent with the full quant score.
_MOMENTUM_WEIGHT = 2.0
_TREND_WEIGHT = 1.5
_52WHIGH_WEIGHT = 1.0


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lo, hi]``."""
    return max(lo, min(hi, value))


def _num(value: Any) -> float | None:
    """Return ``value`` as a float if it is a finite number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def prefilter_score(
    factors: dict[str, Any],
    *,
    mom_saturation: float = 0.30,
    trend_saturation: float = 0.10,
    p52_band: float = 0.15,
) -> float | None:
    """Combine price-only factors into one cheap directional score (pure).

    Positive = bullish (long candidate), negative = bearish (short candidate).
    Returns ``None`` when none of the inputs are computable (e.g. too little
    history), so the name is excluded from ranking rather than scored as neutral.

    The default saturations calibrate for equity daily bars; the crypto screen
    passes interval-appropriate values (see
    :func:`makecrazypenny.orchestration.crypto.factor_saturations`).
    """
    if not isinstance(factors, dict):
        return None
    parts: list[float] = []
    mom = _num(factors.get("momentum_12_1"))
    if mom is not None:
        parts.append(_clamp(mom / mom_saturation, -1.0, 1.0) * _MOMENTUM_WEIGHT)
    trend = _num(factors.get("trend_200"))
    if trend is not None:
        parts.append(_clamp(trend / trend_saturation, -1.0, 1.0) * _TREND_WEIGHT)
    p52 = _num(factors.get("pct_52w_high"))
    if p52 is not None:
        parts.append(_clamp((p52 - (1.0 - p52_band)) / p52_band, -1.0, 1.0) * _52WHIGH_WEIGHT)
    if not parts:
        return None
    return round(sum(parts), 4)


async def _prefilter_factors(symbol: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch ~2y of free daily OHLCV and compute price-only factors for ``symbol``.

    Deliberately skips fundamentals (the slow ``.info`` pull) — the prefilter only
    needs price factors, so a universe-wide sweep stays cheap. Reads through the
    cached registry, so the deep dive's later OHLCV pull hits the cache. Never
    raises; a data failure surfaces as ``{"_error": ...}``.
    """
    from ..servers import technical as tech  # lazy: keep module import light

    try:
        ohlcv = await tech.get_ohlcv(symbol, interval="1d", period="2y")
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    return factor_values(bars)


def _shortlist_entry(symbol: str, factors: dict[str, Any], score: float) -> dict[str, Any]:
    """Build a compact prefilter ranking entry for transparency."""
    return {
        "symbol": symbol,
        "score": score,
        "momentum_12_1": factors.get("momentum_12_1"),
        "trend_200": factors.get("trend_200"),
        "pct_52w_high": factors.get("pct_52w_high"),
        "realized_vol": factors.get("realized_vol"),
        "last_close": factors.get("last_close"),
    }


async def prefilter_universe(
    symbols: list[str], *, settings: Settings | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Score every symbol with the cheap price-factor prefilter (bounded).

    Returns ``(ranked, errors)`` where ``ranked`` is the list of scored entries
    sorted most→least bullish, and ``errors`` holds ``{"symbol", "error"}`` for
    names whose data could not be fetched.
    """
    sem = asyncio.Semaphore(MAX_PREFILTER_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, Any]:
        async with sem:
            try:
                factors = await _prefilter_factors(symbol, settings=settings)
            except Exception as exc:  # defensive: _prefilter_factors already guards
                return ("err", {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            if not isinstance(factors, dict) or "_error" in factors:
                err = factors.get("_error") if isinstance(factors, dict) else "no factors"
                return ("err", {"symbol": symbol, "error": str(err)})
            score = prefilter_score(factors)
            if score is None:
                return ("err", {"symbol": symbol, "error": "insufficient history for factors"})
            return ("ok", _shortlist_entry(symbol, factors, score))

    results = await asyncio.gather(*(_one(s) for s in symbols))
    ranked = sorted(
        (p for tag, p in results if tag == "ok"), key=lambda e: -e["score"]
    )
    errors = [p for tag, p in results if tag == "err"]
    return ranked, errors


async def _deep_dive(
    symbols: list[str], *, settings: Settings | None = None
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Run the full decision engine on each shortlisted symbol (bounded)."""
    sem = asyncio.Semaphore(MAX_DEEP_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, Any]:
        async with sem:
            try:
                return ("ok", await decide(symbol, settings=settings))
            except Exception as exc:
                return ("err", {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    results = await asyncio.gather(*(_one(s) for s in symbols))
    decisions = [p for tag, p in results if tag == "ok"]
    errors = [p for tag, p in results if tag == "err"]
    return decisions, errors


def _summary_line(source: str, count: int, n_long: int, n_short: int) -> str:
    """Build a one-line human-readable screen verdict."""
    src = {"live": "live", "cache": "cached", "fallback": "fallback", "explicit": "given"}.get(
        source, source
    )
    return (
        f"Screened {count} names ({src} universe): "
        f"{n_long} long idea(s), {n_short} short idea(s)."
    )


async def screen_market(
    *,
    symbols: list[str] | None = None,
    shortlist: int = DEFAULT_SHORTLIST,
    top_n: int = DEFAULT_TOP_N,
    universe_label: str = "S&P 500",
    force_refresh: bool = False,
    settings: Settings | None = None,
) -> MarketScreen:
    """Screen a whole universe and return the best long/short trade ideas.

    Two stages (see the module docstring): a cheap price-factor **prefilter** over
    the entire universe, then the full decision engine on the shortlisted
    candidates. The strongest BUY and SHORT decisions are surfaced as complete
    :class:`~makecrazypenny.core.types.TradeDecision`\\ s — with regime, sizing,
    stop/target and invalidation — so the result says both what and how to trade.

    Args:
        symbols: Optional explicit universe; when ``None`` the live-fetched S&P 500
            constituents are used.
        shortlist: How many candidates to deep-dive *per side* (long / short).
        top_n: How many long and how many short ideas to surface.
        universe_label: Human label for the universe (used in the summary).
        force_refresh: Bypass the cached constituent list and refetch it live.
        settings: Optional settings (defaults to ``Settings.from_env()``).

    Returns:
        A :class:`MarketScreen` carrying the disclaimer. Never raises — data and
        fetch failures are captured under ``errors``.
    """
    settings = settings or Settings.from_env()

    # --- Resolve the universe -------------------------------------------------
    if symbols:
        universe_syms = [normalize_symbol(s) for s in symbols if str(s).strip()]
        source, count, as_of = "explicit", len(universe_syms), None
    else:
        uni = await fetch_sp500(settings=settings, force_refresh=force_refresh)
        universe_syms = list(uni.get("symbols", []))
        source = str(uni.get("source", "unknown"))
        count = int(uni.get("count", len(universe_syms)))
        as_of = uni.get("as_of")

    if not universe_syms:
        return MarketScreen(
            universe=universe_label,
            universe_source=source,
            universe_count=count,
            as_of=as_of,
            summary="No universe constituents available to screen.",
            errors=[{"symbol": None, "error": "empty universe"}],
            disclaimer=DISCLAIMER,
        )

    # --- Market regime (once; warms the SPY cache for the deep dive) ----------
    try:
        regime = await market_regime(settings=settings)
    except Exception as exc:
        regime = {"regime": "unknown", "_error": f"{type(exc).__name__}: {exc}"}

    # --- Stage 1: prefilter the whole universe --------------------------------
    ranked, pre_errors = await prefilter_universe(universe_syms, settings=settings)

    n_side = max(1, int(shortlist))
    long_candidates = [e for e in ranked if e["score"] > 0][:n_side]
    short_candidates = sorted(
        (e for e in ranked if e["score"] < 0), key=lambda e: e["score"]
    )[:n_side]

    # --- Stage 2: deep-dive the shortlist (dedup across sides) ----------------
    shortlist_syms: list[str] = []
    seen: set[str] = set()
    for entry in (*long_candidates, *short_candidates):
        sym = entry["symbol"]
        if sym not in seen:
            seen.add(sym)
            shortlist_syms.append(sym)

    decisions, deep_errors = await _deep_dive(shortlist_syms, settings=settings)

    # --- Rank the finalists by side (from whatever the full engine concluded) -
    n_top = max(1, int(top_n))
    buys = sorted(
        (d for d in decisions if d.action == "BUY"),
        key=lambda d: (-d.conviction, -d.net_score),
    )[:n_top]
    shorts = sorted(
        (d for d in decisions if d.action == "SHORT"),
        key=lambda d: (-d.conviction, d.net_score),
    )[:n_top]

    summary = _summary_line(source, count, len(buys), len(shorts))

    return MarketScreen(
        universe=universe_label,
        universe_source=source,
        universe_count=count,
        as_of=as_of,
        n_prefiltered=len(ranked),
        n_evaluated=len(decisions),
        regime=regime if isinstance(regime, dict) else {},
        top_longs=[d.to_dict() for d in buys],
        top_shorts=[d.to_dict() for d in shorts],
        long_shortlist=long_candidates,
        short_shortlist=short_candidates,
        errors=(pre_errors + deep_errors)[:25],
        method="quant",
        summary=summary,
        disclaimer=DISCLAIMER,
    )


__all__ = [
    "MAX_PREFILTER_CONCURRENCY",
    "MAX_DEEP_CONCURRENCY",
    "DEFAULT_SHORTLIST",
    "DEFAULT_TOP_N",
    "prefilter_score",
    "prefilter_universe",
    "screen_market",
]
