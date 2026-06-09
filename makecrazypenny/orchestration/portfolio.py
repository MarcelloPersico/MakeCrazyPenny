"""Layer 2: portfolio construction (CONTRACT.md §10.6).

Turns a set of candidate names (or a sector) into a **sized multi-name portfolio**:
run the deterministic decision engine on each, keep the actionable BUY/SHORT names,
weight them by **conviction x inverse-volatility**, cap per-name and renormalize,
and scale gross exposure by the **market regime** (plan.md §10). Conviction-weighting
concentrates in the best ideas; inverse-vol equalises risk contribution; the regime
scalar dials total risk up/down. Deterministic and AI-free; bounded concurrency keeps
a wide build within rate budgets.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.sectors import resolve_sector, sector_constituents
from ..servers._common import normalize_symbol
from .debate import decide_from_scores, gather_evidence, score_evidence

MAX_CONCURRENCY = 5
DEFAULT_MAX_POSITIONS = 10
DEFAULT_MAX_WEIGHT = 0.25


def _weight_side(rows: list[dict[str, Any]], max_weight: float) -> list[dict[str, Any]]:
    """Conviction x inverse-vol weights for one side, capped and normalized to 1."""
    if not rows:
        return []
    raw: list[float] = []
    for r in rows:
        vol = r.get("realized_vol")
        inv_vol = 1.0 / vol if isinstance(vol, (int, float)) and vol and vol > 0 else 1.0
        raw.append(max(0.0, float(r.get("conviction", 0.0))) * inv_vol)
    total = sum(raw)
    if total <= 0:
        # Equal-weight fallback when convictions are all zero.
        raw = [1.0] * len(rows)
        total = float(len(rows))
    weights = [w / total for w in raw]
    # The cap is a *concentration* limit, so auto-relax it to at least equal weight
    # when there are too few names to fill the side under the nominal cap (keeps the
    # side fully allocated rather than under-deployed).
    cap = max(max_weight, 1.0 / len(rows))
    # Enforce the cap properly: repeatedly clamp over-cap names and redistribute the
    # freed weight across the still-uncapped names, until stable (a single
    # clamp+renormalize can push a clamped name back over the cap).
    for _ in range(len(weights) + 1):
        over = [i for i, w in enumerate(weights) if w > cap + 1e-12]
        if not over:
            break
        uncapped = [i for i in range(len(weights)) if i not in over]
        unc_sum = sum(weights[i] for i in uncapped)
        remaining = 1.0 - cap * len(over)
        for i in over:
            weights[i] = cap
        if uncapped and unc_sum > 0 and remaining > 0:
            for i in uncapped:
                weights[i] = weights[i] / unc_sum * remaining
        else:
            break
    out = []
    for r, w in zip(rows, weights):
        out.append({"symbol": r["symbol"], "weight": round(w, 4), "conviction": r.get("conviction"),
                    "net_score": r.get("net_score"), "realized_vol": r.get("realized_vol")})
    return sorted(out, key=lambda x: -x["weight"])


async def build_portfolio(
    symbols: list[str],
    *,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    max_weight: float = DEFAULT_MAX_WEIGHT,
    regime: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a regime-scaled, conviction x inverse-vol portfolio from ``symbols``.

    Returns a dict with ``longs``/``shorts`` (each ``{symbol, weight, conviction,
    ...}``), ``gross_exposure``/``net_exposure``, the ``regime``, ``errors``, and the
    disclaimer. Weights within each side sum to 1 *before* the gross-exposure scale;
    ``weight x gross_exposure`` is the suggested capital fraction.
    """
    settings = settings or Settings.from_env()
    syms = [normalize_symbol(s) for s in symbols if str(s).strip()]
    if regime is None:
        from .debate import market_regime

        try:
            regime = await market_regime(settings=settings)
        except Exception:
            regime = {}
    gross = float(regime.get("gross_exposure", 1.0)) if isinstance(regime, dict) else 1.0

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, Any]:
        async with sem:
            try:
                dossier = await gather_evidence(symbol, settings=settings)
                scored = score_evidence(dossier)
                dec = decide_from_scores(symbol, scored, method="quant")
                fac = dossier.get("factors") if isinstance(dossier.get("factors"), dict) else {}
                return ("ok", {
                    "symbol": symbol, "action": dec.action, "conviction": dec.conviction,
                    "net_score": dec.net_score, "realized_vol": fac.get("realized_vol"),
                })
            except Exception as exc:
                return ("err", {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    results = await asyncio.gather(*(_one(s) for s in syms))
    rows = [p for tag, p in results if tag == "ok"]
    errors = [p for tag, p in results if tag == "err"]

    longs_in = sorted([r for r in rows if r["action"] == "BUY"], key=lambda r: -r["conviction"])[:max_positions]
    shorts_in = sorted([r for r in rows if r["action"] == "SHORT"], key=lambda r: r["net_score"])[:max_positions]
    longs = _weight_side(longs_in, max_weight)
    shorts = _weight_side(shorts_in, max_weight)

    long_gross = round(gross * sum(x["weight"] for x in longs), 4)
    short_gross = round(gross * sum(x["weight"] for x in shorts), 4)
    return {
        "n_candidates": len(syms),
        "n_analyzed": len(rows),
        "regime": regime,
        "gross_exposure": round(gross, 4),
        "longs": longs,
        "shorts": shorts,
        "long_exposure": long_gross,
        "short_exposure": short_gross,
        "net_exposure": round(long_gross - short_gross, 4),
        "errors": errors,
        "method": "quant",
        "disclaimer": DISCLAIMER,
    }


async def build_sector_portfolio(
    sector: str,
    *,
    limit: int | None = None,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    max_weight: float = DEFAULT_MAX_WEIGHT,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a portfolio from a sector's constituents (resolves aliases)."""
    canonical = resolve_sector(sector)
    if not canonical:
        return {
            "sector": str(sector), "longs": [], "shorts": [], "errors": [{"error": f"unknown sector '{sector}'"}],
            "disclaimer": DISCLAIMER,
        }
    tickers = sector_constituents(canonical)
    if limit is not None and limit > 0:
        tickers = tickers[:limit]
    result = await build_portfolio(
        tickers, max_positions=max_positions, max_weight=max_weight, settings=settings
    )
    result["sector"] = canonical
    return result


__all__ = ["build_portfolio", "build_sector_portfolio", "DEFAULT_MAX_POSITIONS", "DEFAULT_MAX_WEIGHT"]
