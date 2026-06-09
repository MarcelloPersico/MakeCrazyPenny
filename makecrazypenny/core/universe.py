"""S&P 500 universe — live-fetched, disk-cached, with an offline fallback.

The whole-market screen (:mod:`makecrazypenny.orchestration.screen`) needs the
full list of S&P 500 constituents. Unlike the deliberately-curated sector baskets
in :mod:`makecrazypenny.core.sectors`, the index membership *churns*, so this
module fetches the current constituents live and keeps them fresh:

  1. **Live** — pull a maintained CSV of constituents over HTTPS (no API key).
  2. **Cache** — persist the parsed list under the cache dir with a weekly TTL so
     repeated screens are fast and survive transient outages (even a *stale* cache
     beats nothing when the live fetch fails).
  3. **Fallback** — if both the live fetch and the cache are unavailable, degrade
     to the union of the curated sector baskets so a screen can always run offline.

Every path is non-fatal and the result is tagged with its ``source`` so callers
(and users) know how fresh the universe is. Symbols are normalized to the
yfinance convention (class shares use ``-`` not ``.``: ``BRK.B`` -> ``BRK-B``).

Import safety: only the standard library is imported at module top; ``httpx`` is
imported lazily inside the fetcher, so importing this module never hits the
network and never requires the HTTP client.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .sectors import SECTORS

#: Maintained, key-free CSV of current S&P 500 constituents (Symbol,Security,GICS Sector,...).
SP500_SOURCE_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)

#: Cache filename (under the resolved cache dir) for the parsed universe.
_CACHE_FILE = "sp500_universe.json"

#: Refresh the live list at most ~weekly; index membership changes slowly.
_CACHE_TTL_SECONDS = 7 * 24 * 3600

#: HTTP timeout for the (single) constituents fetch.
_FETCH_TIMEOUT_S = 20.0


def normalize_universe_symbol(symbol: str) -> str:
    """Canonicalize a constituent symbol to the yfinance convention.

    Upper-cases, strips a leading ``$``, and maps class-share dots to dashes
    (``BRK.B`` -> ``BRK-B``) so the symbol resolves through the OHLCV providers.
    """
    s = str(symbol).strip().upper()
    if s.startswith("$"):
        s = s[1:]
    return s.strip().replace(".", "-")


def _fallback() -> dict[str, Any]:
    """Build the offline fallback universe from the curated sector baskets.

    A partial but liquid stand-in (the ~100 large caps in
    :data:`makecrazypenny.core.sectors.SECTORS`) used only when both the live
    fetch and the on-disk cache are unavailable, so a screen can always run.
    """
    symbols: list[str] = []
    sector_of: dict[str, str] = {}
    for sector, names in SECTORS.items():
        for raw in names:
            sym = normalize_universe_symbol(raw)
            if sym not in sector_of:
                symbols.append(sym)
                sector_of[sym] = sector
    return {
        "symbols": symbols,
        "sector_of": sector_of,
        "count": len(symbols),
        "source": "fallback",
        "as_of": None,
        "url": None,
    }


def _parse_csv(text: str) -> dict[str, Any]:
    """Parse the constituents CSV into ``{symbols, sector_of, ...}``.

    Tolerant of column-name drift: matches the symbol/sector columns
    case-insensitively. Returns an empty payload if no symbols are found.
    """
    reader = csv.DictReader(io.StringIO(text))
    symbols: list[str] = []
    sector_of: dict[str, str] = {}
    for row in reader:
        lower = {str(k).strip().lower(): v for k, v in row.items() if k}
        raw_symbol = lower.get("symbol") or lower.get("ticker")
        if not raw_symbol or not str(raw_symbol).strip():
            continue
        sym = normalize_universe_symbol(str(raw_symbol))
        if sym in sector_of:
            continue
        symbols.append(sym)
        sector = lower.get("gics sector") or lower.get("sector") or ""
        sector_of[sym] = str(sector).strip()
    return {"symbols": symbols, "sector_of": sector_of, "count": len(symbols)}


def _read_cache(path: Path) -> dict[str, Any] | None:
    """Read the cached universe document, or ``None`` if absent/unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("symbols"):
        return None
    return data


def _write_cache(path: Path, doc: dict[str, Any]) -> bool:
    """Best-effort atomic-ish write of the universe cache. Never raises."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError:
        return False


def _fetch_live(url: str = SP500_SOURCE_URL) -> dict[str, Any] | None:
    """Fetch + parse the live constituents CSV, or ``None`` on any failure.

    ``httpx`` is imported lazily so the module stays importable without it.
    """
    try:
        import httpx  # lazy import (CONTRACT.md §2.2)
    except ImportError:
        return None
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT_S, follow_redirects=True)
        resp.raise_for_status()
        parsed = _parse_csv(resp.text)
    except Exception:  # network/transport/parse errors are all non-fatal
        return None
    if not parsed.get("symbols"):
        return None
    return parsed


async def fetch_sp500(
    *, settings: Settings | None = None, force_refresh: bool = False
) -> dict[str, Any]:
    """Return the S&P 500 universe (live -> cache -> fallback), tagged by source.

    Resolution order:

      1. A **fresh** on-disk cache (younger than the weekly TTL) is returned as-is
         unless ``force_refresh`` is set.
      2. Otherwise a **live** fetch is attempted; on success it is cached and
         returned (``source="live"``).
      3. If the live fetch fails, a **stale** cache (if any) is returned
         (``source="cache"``, ``stale=True``).
      4. If there is no cache either, the curated-sector **fallback** is returned
         (``source="fallback"``).

    The blocking HTTP + disk work runs in a worker thread so the event loop is not
    stalled. Never raises.

    Returns:
        ``{"symbols": [...], "sector_of": {sym: sector}, "count": int,
        "source": "live"|"cache"|"fallback", "as_of": <iso|None>,
        "stale": <bool>, "url": <str|None>}``.
    """
    import asyncio

    return await asyncio.to_thread(_fetch_sp500_blocking, settings, force_refresh)


def _fetch_sp500_blocking(settings: Settings | None, force_refresh: bool) -> dict[str, Any]:
    """Synchronous core of :func:`fetch_sp500` (run via ``asyncio.to_thread``)."""
    settings = settings or Settings.from_env()
    cache_path = settings.resolve_cache_dir() / _CACHE_FILE
    now = time.time()

    cached = _read_cache(cache_path)
    if cached and not force_refresh:
        fetched_at = float(cached.get("fetched_at", 0.0) or 0.0)
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return {**cached, "source": "cache", "stale": False}

    live = _fetch_live()
    if live:
        doc = {
            **live,
            "source": "live",
            "fetched_at": now,
            "as_of": _iso(now),
            "url": SP500_SOURCE_URL,
        }
        _write_cache(cache_path, doc)
        return {**doc, "stale": False}

    # Live failed — a stale cache still beats the partial fallback.
    if cached:
        return {**cached, "source": "cache", "stale": True}

    return {**_fallback(), "stale": False}


def _iso(epoch: float) -> str:
    """Render an epoch timestamp as a UTC ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


__all__ = [
    "SP500_SOURCE_URL",
    "normalize_universe_symbol",
    "fetch_sp500",
]
