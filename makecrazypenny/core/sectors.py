"""Market sector universe (see CONTRACT.md §12).

A curated, deterministic map of the eleven GICS sectors to a basket of liquid
large-cap constituents, used by the sector-scan engine
(:mod:`makecrazypenny.orchestration.market`) to analyse a broad slice of the
market rather than a single ticker.

Why curated (not fetched): it keeps sector resolution **deterministic, offline,
and free** — no API key and no network needed just to know what's in a sector,
which keeps the scan engine unit-testable. The lists are representative, not
exhaustive; a future revision could back them with live ETF-holdings data behind
the same interface.

Importing this module pulls in only the standard library.
"""

from __future__ import annotations

#: Canonical GICS sector -> representative liquid constituents (symbols).
SECTORS: dict[str, list[str]] = {
    "Technology": [
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "ACN", "INTC", "QCOM",
    ],
    "Communication Services": [
        "GOOGL", "META", "NFLX", "DIS", "T", "VZ", "TMUS", "CMCSA",
    ],
    "Consumer Discretionary": [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG",
    ],
    "Consumer Staples": [
        "WMT", "PG", "KO", "PEP", "COST", "MDLZ", "CL", "MO",
    ],
    "Financials": [
        "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "SCHW",
    ],
    "Health Care": [
        "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY",
    ],
    "Energy": [
        "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY",
    ],
    "Industrials": [
        "CAT", "BA", "HON", "UPS", "GE", "RTX", "UNP", "DE", "LMT",
    ],
    "Materials": [
        "LIN", "SHW", "APD", "FCX", "NEM", "DOW", "NUE",
    ],
    "Utilities": [
        "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE",
    ],
    "Real Estate": [
        "PLD", "AMT", "EQIX", "CCI", "PSA", "O", "SPG",
    ],
}

#: Common aliases / shorthands -> canonical sector name (lower-cased keys).
SECTOR_ALIASES: dict[str, str] = {
    "tech": "Technology",
    "information technology": "Technology",
    "it": "Technology",
    "comm": "Communication Services",
    "comms": "Communication Services",
    "communication": "Communication Services",
    "communications": "Communication Services",
    "comm services": "Communication Services",
    "telecom": "Communication Services",
    "discretionary": "Consumer Discretionary",
    "consumer disc": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "financial": "Financials",
    "finance": "Financials",
    "banks": "Financials",
    "healthcare": "Health Care",
    "health": "Health Care",
    "health-care": "Health Care",
    "energy": "Energy",
    "oil": "Energy",
    "oil & gas": "Energy",
    "industrial": "Industrials",
    "materials": "Materials",
    "basic materials": "Materials",
    "utility": "Utilities",
    "utilities": "Utilities",
    "real estate": "Real Estate",
    "reit": "Real Estate",
    "reits": "Real Estate",
}


def list_sectors() -> list[str]:
    """Return the canonical sector names, sorted."""
    return sorted(SECTORS)


def resolve_sector(name: str) -> str | None:
    """Resolve a user-supplied sector name to its canonical key, or ``None``.

    Matching is tolerant: case-insensitive, alias-aware, and falls back to a
    unique substring match (e.g. ``"real"`` -> ``"Real Estate"``). Ambiguous or
    unknown names return ``None``.

    Args:
        name: A sector name, alias, or shorthand (e.g. ``"tech"``, ``"health"``).

    Returns:
        The canonical sector name, or ``None`` if it cannot be resolved uniquely.
    """
    if not name:
        return None
    key = " ".join(str(name).strip().lower().split())
    # Exact canonical (case-insensitive).
    for canonical in SECTORS:
        if canonical.lower() == key:
            return canonical
    # Alias.
    if key in SECTOR_ALIASES:
        return SECTOR_ALIASES[key]
    # Unique substring match against canonical names.
    hits = [c for c in SECTORS if key in c.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


def sector_constituents(name: str) -> list[str]:
    """Return the constituent symbols for a sector (resolved), or ``[]``."""
    canonical = resolve_sector(name)
    return list(SECTORS[canonical]) if canonical else []


__all__ = [
    "SECTORS",
    "SECTOR_ALIASES",
    "list_sectors",
    "resolve_sector",
    "sector_constituents",
]
