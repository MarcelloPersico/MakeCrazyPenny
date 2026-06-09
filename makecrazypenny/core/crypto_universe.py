"""Tradable crypto perpetual universe — live-fetched, cached, with a fallback.

The crypto screen (:mod:`makecrazypenny.orchestration.crypto_screen`) needs a list
of liquid perpetuals to rank. Liquidity churns, so this fetches the most-active
USDT perpetuals live (by 24h quote volume) and keeps them fresh, mirroring
:mod:`makecrazypenny.core.universe`:

  1. **Live** — Binance ``/fapi/v1/ticker/24hr`` (Bybit tickers as fallback), both
     keyless.
  2. **Cache** — persist the ranked list under the cache dir with a ~daily TTL.
  3. **Fallback** — the curated :data:`~makecrazypenny.core.symbols.MAJOR_CRYPTO_BASES`
     as ``BASEUSDT`` so a screen can always run offline.

Every path is non-fatal and tagged with its ``source``. Import-safe: only stdlib
at module top; ``httpx`` is imported lazily inside the fetchers.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .symbols import MAJOR_CRYPTO_BASES

#: Cache filename (under the resolved cache dir) for the ranked perp list.
_CACHE_FILE = "crypto_universe.json"
#: Refresh at most ~daily; top-volume membership shifts slowly day to day.
_CACHE_TTL_SECONDS = 24 * 3600
#: HTTP timeout for the (single) universe fetch.
_FETCH_TIMEOUT_S = 20.0
#: How many ranked names to cache (callers slice to their own ``limit``).
_CACHE_DEPTH = 200
#: Plain USDT perpetual symbol (excludes dated/quarterly futures with a suffix).
_PERP_RE = re.compile(r"^[A-Z0-9]+USDT$")


def _fallback() -> dict[str, Any]:
    """Curated offline universe from the major bases (``BTC`` -> ``BTCUSDT``)."""
    symbols = [f"{b}USDT" for b in MAJOR_CRYPTO_BASES]
    return {"symbols": symbols, "count": len(symbols), "source": "fallback", "as_of": None}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_live_binance(base_url: str) -> list[str] | None:
    """Rank Binance USDⓈ-M perps by 24h quote volume; ``None`` on any failure."""
    try:
        import httpx  # lazy import
    except ImportError:
        return None
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", timeout=_FETCH_TIMEOUT_S)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return None
    ranked: list[tuple[float, str]] = []
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol") or "")
            if not _PERP_RE.match(sym):
                continue
            qv = _to_float(r.get("quoteVolume"))
            if qv is not None:
                ranked.append((qv, sym))
    if not ranked:
        return None
    ranked.sort(key=lambda t: -t[0])
    return [sym for _, sym in ranked[:_CACHE_DEPTH]]


def _fetch_live_bybit(base_url: str) -> list[str] | None:
    """Rank Bybit linear perps by 24h turnover; ``None`` on any failure."""
    try:
        import httpx  # lazy import
    except ImportError:
        return None
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/v5/market/tickers",
            params={"category": "linear"},
            timeout=_FETCH_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("retCode") not in (0, "0"):
        return None
    rows = (body.get("result") or {}).get("list") if isinstance(body.get("result"), dict) else None
    ranked: list[tuple[float, str]] = []
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol") or "")
            if not _PERP_RE.match(sym):
                continue
            tv = _to_float(r.get("turnover24h"))
            if tv is not None:
                ranked.append((tv, sym))
    if not ranked:
        return None
    ranked.sort(key=lambda t: -t[0])
    return [sym for _, sym in ranked[:_CACHE_DEPTH]]


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("symbols"):
        return None
    return data


def _write_cache(path: Path, doc: dict[str, Any]) -> bool:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError:
        return False


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


async def fetch_top_perps(
    *, settings: Settings | None = None, limit: int = 40, force_refresh: bool = False
) -> dict[str, Any]:
    """Return the most-liquid USDT perpetuals (live -> cache -> fallback), tagged.

    Resolution order mirrors :func:`makecrazypenny.core.universe.fetch_sp500`: a
    fresh cache wins unless ``force_refresh``; otherwise a live fetch (Binance,
    then Bybit) is cached and returned; a stale cache beats a failed live fetch;
    the curated majors are the last resort. Blocking work runs in a worker thread;
    never raises. The cached list is sliced to ``limit``.
    """
    import asyncio

    return await asyncio.to_thread(_fetch_blocking, settings, int(limit), force_refresh)


def _fetch_blocking(settings: Settings | None, limit: int, force_refresh: bool) -> dict[str, Any]:
    """Synchronous core of :func:`fetch_top_perps` (run via ``asyncio.to_thread``)."""
    settings = settings or Settings.from_env()
    cache_path = settings.resolve_cache_dir() / _CACHE_FILE
    now = time.time()

    cached = _read_cache(cache_path)
    if cached and not force_refresh:
        fetched_at = float(cached.get("fetched_at", 0.0) or 0.0)
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return _slice(cached, "cache", limit, stale=False)

    symbols = _fetch_live_binance(settings.binance_base_url) or _fetch_live_bybit(settings.bybit_base_url)
    if symbols:
        doc = {"symbols": symbols, "count": len(symbols), "fetched_at": now, "as_of": _iso(now)}
        _write_cache(cache_path, doc)
        return _slice({**doc, "source": "live"}, "live", limit, stale=False)

    if cached:
        return _slice(cached, "cache", limit, stale=True)

    return _slice(_fallback(), "fallback", limit, stale=False)


def _slice(doc: dict[str, Any], source: str, limit: int, *, stale: bool) -> dict[str, Any]:
    """Return a result dict sliced to ``limit`` and tagged with its source."""
    symbols = list(doc.get("symbols", []))[: max(1, limit)]
    return {
        "symbols": symbols,
        "count": len(symbols),
        "total_available": len(doc.get("symbols", [])),
        "source": source,
        "as_of": doc.get("as_of"),
        "stale": stale,
    }


__all__ = ["fetch_top_perps"]
