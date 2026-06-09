"""Layer 2: crypto screen — funnel the perp universe down to the best setups.

The crypto analogue of :mod:`makecrazypenny.orchestration.screen`: a two-stage
funnel over the most-liquid USDT perpetuals.

  1. **Prefilter (cheap, whole universe).** One free kline pull per name at the
     entry interval -> price-only factors -> a composite momentum/trend/high score
     (reusing :func:`makecrazypenny.orchestration.screen.prefilter_score`).
  2. **Deep dive (full engine, shortlist only).** Run
     :func:`makecrazypenny.orchestration.crypto.decide_crypto` — derivatives,
     regime, and the leverage plan — on the strongest long and short candidates.

The best ``top_n`` long and short verdicts are returned as full
:class:`~makecrazypenny.core.types.TradeDecision`\\ s (each carrying its leverage
plan), so the result says both *what* and *how* to trade. Bounded by semaphores,
tolerant per name, and offline-testable by monkeypatching the two fetch points
plus the universe fetch. The bull/bear debate over the finalists is run by the
MCP host via the ``decide_crypto_market`` prompt.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.crypto_regime import crypto_regime
from ..analysis.factors import factor_values
from ..core.config import Settings
from ..core.crypto_universe import fetch_top_perps
from ..core.disclaimer import DISCLAIMER
from ..core.symbols import canonical_crypto
from ..core.types import MarketScreen
from .crypto import decide_crypto, factor_saturations, periods_per_year
from .screen import prefilter_score

#: Names prefiltered concurrently (stage 1 is one cheap kline pull each).
MAX_PREFILTER_CONCURRENCY = 8
#: Shortlisted names deep-dived concurrently (stage 2 fans out per name).
MAX_DEEP_CONCURRENCY = 5
DEFAULT_UNIVERSE_LIMIT = 40
DEFAULT_SHORTLIST = 10
DEFAULT_TOP_N = 3


async def _prefilter_factors(symbol: str, interval: str) -> dict[str, Any]:
    """Fetch ~300 perp bars at ``interval`` and compute price-only factors."""
    from ..servers import crypto as cx

    try:
        ohlcv = await cx.crypto_ohlcv(symbol, interval=interval, limit=300)
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    return factor_values(bars, periods_per_year=periods_per_year(interval))


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
    symbols: list[str], *, interval: str = "15m", settings: Settings | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Score every symbol with the cheap price-factor prefilter (bounded).

    Returns ``(ranked, errors)`` where ``ranked`` is sorted most->least bullish.
    """
    sem = asyncio.Semaphore(MAX_PREFILTER_CONCURRENCY)

    sat = factor_saturations(interval)

    async def _one(symbol: str) -> tuple[str, Any]:
        async with sem:
            factors = await _prefilter_factors(symbol, interval)
            if not isinstance(factors, dict) or "_error" in factors:
                err = factors.get("_error") if isinstance(factors, dict) else "no factors"
                return ("err", {"symbol": symbol, "error": str(err)})
            score = prefilter_score(factors, **sat)
            if score is None:
                return ("err", {"symbol": symbol, "error": "insufficient history for factors"})
            return ("ok", _shortlist_entry(symbol, factors, score))

    results = await asyncio.gather(*(_one(s) for s in symbols))
    ranked = sorted((p for tag, p in results if tag == "ok"), key=lambda e: -e["score"])
    errors = [p for tag, p in results if tag == "err"]
    return ranked, errors


async def _deep_dive(
    symbols: list[str], *, interval: str, leverage_cap: float | None, settings: Settings | None
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Run the full crypto decision engine on each shortlisted symbol (bounded)."""
    sem = asyncio.Semaphore(MAX_DEEP_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, Any]:
        async with sem:
            try:
                return ("ok", await decide_crypto(symbol, interval=interval, leverage_cap=leverage_cap, settings=settings))
            except Exception as exc:
                return ("err", {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    results = await asyncio.gather(*(_one(s) for s in symbols))
    decisions = [p for tag, p in results if tag == "ok"]
    errors = [p for tag, p in results if tag == "err"]
    return decisions, errors


def _summary_line(source: str, count: int, interval: str, n_long: int, n_short: int) -> str:
    src = {"live": "live", "cache": "cached", "fallback": "fallback", "explicit": "given"}.get(source, source)
    return (
        f"Screened {count} perps ({src} universe, {interval}): "
        f"{n_long} long idea(s), {n_short} short idea(s)."
    )


async def screen_crypto(
    *,
    symbols: list[str] | None = None,
    interval: str = "15m",
    shortlist: int = DEFAULT_SHORTLIST,
    top_n: int = DEFAULT_TOP_N,
    universe_limit: int = DEFAULT_UNIVERSE_LIMIT,
    leverage_cap: float | None = None,
    force_refresh: bool = False,
    settings: Settings | None = None,
) -> MarketScreen:
    """Screen the crypto perp universe and return the best long/short setups.

    Args:
        symbols: Optional explicit universe; when ``None`` the live top perps are used.
        interval: Entry timeframe for the prefilter + decision engine.
        shortlist: How many candidates to deep-dive per side (long / short).
        top_n: How many long and how many short ideas to surface.
        universe_limit: How many top perps to pull when fetching the universe.
        leverage_cap: Optional per-call leverage ceiling override.
        force_refresh: Bypass the cached universe and refetch it live.
        settings: Optional settings (defaults to ``Settings.from_env()``).

    Returns:
        A :class:`MarketScreen` (universe ``"Crypto perps"``) whose ``top_longs`` /
        ``top_shorts`` are full leverage-aware ``TradeDecision`` dicts. Never raises.
    """
    settings = settings or Settings.from_env()

    if symbols:
        universe_syms = [canonical_crypto(s) for s in symbols if str(s).strip()]
        source, count, as_of = "explicit", len(universe_syms), None
    else:
        uni = await fetch_top_perps(settings=settings, limit=universe_limit, force_refresh=force_refresh)
        universe_syms = list(uni.get("symbols", []))
        source = str(uni.get("source", "unknown"))
        count = int(uni.get("count", len(universe_syms)))
        as_of = uni.get("as_of")

    if not universe_syms:
        return MarketScreen(
            universe="Crypto perps",
            universe_source=source,
            universe_count=count,
            as_of=as_of,
            summary="No crypto constituents available to screen.",
            errors=[{"symbol": None, "error": "empty universe"}],
            disclaimer=DISCLAIMER,
        )

    try:
        regime = await crypto_regime(settings=settings)
    except Exception as exc:
        regime = {"regime": "unknown", "_error": f"{type(exc).__name__}: {exc}"}

    ranked, pre_errors = await prefilter_universe(universe_syms, interval=interval, settings=settings)

    n_side = max(1, int(shortlist))
    long_candidates = [e for e in ranked if e["score"] > 0][:n_side]
    short_candidates = sorted((e for e in ranked if e["score"] < 0), key=lambda e: e["score"])[:n_side]

    shortlist_syms: list[str] = []
    seen: set[str] = set()
    for entry in (*long_candidates, *short_candidates):
        sym = entry["symbol"]
        if sym not in seen:
            seen.add(sym)
            shortlist_syms.append(sym)

    decisions, deep_errors = await _deep_dive(
        shortlist_syms, interval=interval, leverage_cap=leverage_cap, settings=settings
    )

    n_top = max(1, int(top_n))
    buys = sorted((d for d in decisions if d.action == "BUY"), key=lambda d: (-d.conviction, -d.net_score))[:n_top]
    shorts = sorted((d for d in decisions if d.action == "SHORT"), key=lambda d: (-d.conviction, d.net_score))[:n_top]

    return MarketScreen(
        universe="Crypto perps",
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
        summary=_summary_line(source, count, interval, len(buys), len(shorts)),
        disclaimer=DISCLAIMER,
    )


__all__ = [
    "MAX_PREFILTER_CONCURRENCY",
    "MAX_DEEP_CONCURRENCY",
    "prefilter_universe",
    "screen_crypto",
]
