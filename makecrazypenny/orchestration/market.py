"""Layer 2: sector-wide market scan (CONTRACT.md §10.5).

Extends the single-ticker decision engine to a **broad slice of the market**: it
runs the deterministic decision engine (:mod:`makecrazypenny.orchestration.debate`)
on every constituent of a sector and aggregates the results into a
:class:`~makecrazypenny.core.types.SectorScan` — a sector ``stance``
(overweight/underweight/neutral), breadth statistics, and ranked long/short ideas.

Like the rest of the engine this is **pure orchestration + I/O through the cached
Layer-0 registry**: no model is called (the AI debate over the scan is run by the
MCP host via the ``decide_sector`` prompt). Per-ticker analysis runs concurrently
under a bounded semaphore so a wide scan does not stampede the providers, and each
ticker is independent — one failure becomes an ``errors`` entry, never aborting the
sweep. Fully offline-testable by monkeypatching the evidence gathering.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.sectors import resolve_sector, sector_constituents
from ..core.types import SectorScan, TradeDecision
from ..servers._common import normalize_symbol
from .debate import decide_from_scores, gather_evidence, score_evidence

#: Max constituents analysed concurrently (keeps a wide scan within rate budgets).
MAX_CONCURRENCY = 5
#: Default number of top long/short ideas surfaced.
DEFAULT_TOP_N = 5


def _entry(decision: TradeDecision) -> dict[str, Any]:
    """Build a compact per-ticker ranking entry from a full decision."""
    return {
        "symbol": decision.symbol,
        "action": decision.action,
        "direction": decision.direction,
        "conviction": decision.conviction,
        "net_score": decision.net_score,
        "summary": decision.summary,
        "n_factors": decision.data_quality.get("n_factors", 0),
    }


def _stance(net_tilt: float, bullish_pct: float, bearish_pct: float) -> str:
    """Derive the sector stance from net momentum + breadth."""
    if net_tilt >= 1.0 and bullish_pct >= 0.4:
        return "overweight"
    if net_tilt <= -1.0 and bearish_pct >= 0.4:
        return "underweight"
    return "neutral"


def _summary_line(sector: str, stance: str, n: int, bullish_pct: float) -> str:
    """Build a one-line human-readable sector verdict."""
    verb = {"overweight": "Overweight", "underweight": "Underweight", "neutral": "Neutral on"}.get(
        stance, stance
    )
    return f"{verb} {sector} - {bullish_pct:.0%} of {n} names bullish."


def aggregate_scan(
    sector: str,
    decisions: list[TradeDecision],
    errors: list[dict[str, Any]],
    *,
    n_requested: int,
    top_n: int = DEFAULT_TOP_N,
) -> SectorScan:
    """Aggregate per-ticker decisions into a :class:`SectorScan` (pure function).

    Args:
        sector: Canonical sector name.
        decisions: The per-constituent :class:`TradeDecision` results.
        errors: ``{"symbol", "error"}`` entries for constituents that failed.
        n_requested: How many constituents were attempted.
        top_n: How many long/short ideas to surface.

    Returns:
        The aggregated :class:`SectorScan` (carries the disclaimer).
    """
    entries = [_entry(d) for d in decisions]
    n = len(entries)
    rankings = sorted(entries, key=lambda e: -e["net_score"])
    buys = [e for e in rankings if e["action"] == "BUY"]
    shorts = [e for e in rankings if e["action"] == "SHORT"]
    avoids = [e for e in rankings if e["action"] == "AVOID"]

    net_tilt = round(sum(e["net_score"] for e in entries) / n, 4) if n else 0.0
    avg_conviction = round(sum(e["conviction"] for e in entries) / n, 4) if n else 0.0
    bullish_pct = round(len(buys) / n, 4) if n else 0.0
    bearish_pct = round(len(shorts) / n, 4) if n else 0.0
    stance = _stance(net_tilt, bullish_pct, bearish_pct)

    top_longs = buys[:top_n]
    top_shorts = sorted(shorts, key=lambda e: e["net_score"])[:top_n]

    return SectorScan(
        sector=sector,
        stance=stance,
        n_requested=n_requested,
        n_analyzed=n,
        net_tilt=net_tilt,
        avg_conviction=avg_conviction,
        breadth={
            "buy": len(buys),
            "short": len(shorts),
            "avoid": len(avoids),
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
        },
        rankings=rankings,
        top_longs=top_longs,
        top_shorts=top_shorts,
        errors=errors,
        method="quant",
        summary=_summary_line(sector, stance, n, bullish_pct),
        disclaimer=DISCLAIMER,
    )


async def scan_sector(
    sector: str,
    *,
    limit: int | None = None,
    top_n: int = DEFAULT_TOP_N,
    settings: Settings | None = None,
) -> SectorScan:
    """Run the deterministic decision engine across a sector's constituents.

    Resolves ``sector`` (tolerant: aliases, case, substrings) to its curated
    constituents, analyses each concurrently (bounded by :data:`MAX_CONCURRENCY`),
    and aggregates into a :class:`SectorScan`. AI-free; the host runs the debate
    over the result via the ``decide_sector`` MCP prompt.

    Args:
        sector: Sector name / alias (e.g. ``"tech"``, ``"Health Care"``).
        limit: Optional cap on how many constituents to scan (first N).
        top_n: How many long/short ideas to surface.
        settings: Optional settings (defaults to ``Settings.from_env()``).

    Returns:
        A :class:`SectorScan`. For an unknown sector, an empty scan whose
        ``errors`` explains the failure (never raises).
    """
    settings = settings or Settings.from_env()
    canonical = resolve_sector(sector)
    if not canonical:
        return SectorScan(
            sector=str(sector),
            summary=f"Unknown sector '{sector}'.",
            errors=[{"symbol": None, "error": f"unknown sector '{sector}'"}],
            disclaimer=DISCLAIMER,
        )

    tickers = sector_constituents(canonical)
    if limit is not None and limit > 0:
        tickers = tickers[:limit]

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, Any]:
        sym = normalize_symbol(symbol)
        async with sem:
            try:
                dossier = await gather_evidence(sym, settings=settings)
                scored = score_evidence(dossier)
                return ("ok", decide_from_scores(sym, scored, method="quant"))
            except Exception as exc:  # one bad name never aborts the sweep
                return ("err", {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"})

    results = await asyncio.gather(*(_one(t) for t in tickers))
    decisions = [payload for tag, payload in results if tag == "ok"]
    errors = [payload for tag, payload in results if tag == "err"]

    return aggregate_scan(canonical, decisions, errors, n_requested=len(tickers), top_n=top_n)


__all__ = [
    "MAX_CONCURRENCY",
    "DEFAULT_TOP_N",
    "scan_sector",
    "aggregate_scan",
]
