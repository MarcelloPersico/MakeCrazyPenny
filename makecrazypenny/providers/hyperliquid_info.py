"""Hyperliquid public info-API provider (CONTRACT.md §16; DESIGN-SWARM.md).

Keyless, **read-only** market data from Hyperliquid's ``POST /info`` endpoint —
the venue paper trades actually execute on. One ``metaAndAssetCtxs`` call
returns the whole perp market (mark/oracle/mid prices, *hourly* funding, open
interest, premium, impact prices, day volume, max leverage), ``l2Book`` gives
HL-native depth, ``fundingHistory`` the realized hourly funding trail, and
``predictedFundings`` cross-venue predicted funding (Binance/Bybit/Hyperliquid)
in a single call. HL funding settles every hour — materially different from the
4h/8h CEX intervals the other crypto providers report.

This provider never signs anything and never places orders: the authenticated
write path stays locked to the testnet execution layer (CONTRACT.md §17). The
base URL comes from the optional ``Settings.hyperliquid_info_url`` field (read
via ``getattr`` with a safe default so the provider works before that field is
wired).

Capabilities: ``hl_asset_ctx``, ``hl_predicted_funding``, ``hl_l2book``,
``hl_funding_history``, ``hl_market_pulse``. No API key required. ``httpx`` is
imported lazily so importing this module never hits the network.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.symbols import to_hyperliquid_coin
from ..core.types import Provenance, utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "hyperliquid_info"

#: Default public info endpoint for market-data reads. Overridable through the
#: optional ``Settings.hyperliquid_info_url`` field / ``MCP_HL_INFO_URL``.
_DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"

#: Lifetime (seconds) of the memoized perp universe used for symbol resolution.
_UNIVERSE_TTL_S = 600.0

#: Snapshot file (under the cache dir) used for new-listing detection.
_SNAPSHOT_FILE = "hl_universe_snapshot.json"

#: Depth levels kept per side of the L2 book (HL serves up to 20).
_BOOK_DEPTH = 20

#: Default funding-history window (72h = 72 hourly settlements) and a hard cap.
_DEFAULT_FUNDING_HOURS = 72.0
_MAX_FUNDING_HOURS = 24.0 * 30.0


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ms: Any) -> str | None:
    """Convert an epoch-milliseconds value to an ISO-8601 UTC string."""
    f = _to_float(ms)
    if f is None:
        return None
    try:
        return datetime.fromtimestamp(f / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _strip_perp_suffix(symbol: str) -> str:
    """Drop a trailing ``PERP`` marker (``BTC-PERP``/``BTCPERP``) before mapping."""
    s = str(symbol).strip().upper()
    if s.endswith("PERP") and len(s) > 4:
        s = s[:-4].rstrip("-_/: ")
    return s


def _hl_base(symbol: str) -> str:
    """Resolve any user spelling to a candidate HL base (``BTCUSDT`` -> ``BTC``).

    Strips ``PERP`` markers, then quote suffixes (``USDT``/``USD``/``USDC``/...)
    via :func:`~makecrazypenny.core.symbols.to_hyperliquid_coin`, upper-casing
    along the way. The result is a *candidate* — callers verify it against the
    fetched universe (which also recovers HL's mixed-case names like ``kPEPE``).
    """
    return to_hyperliquid_coin(_strip_perp_suffix(symbol))


@register_provider
class HyperliquidInfoProvider(Provider):
    """Hyperliquid public ``/info`` REST adapter (keyless, read-only)."""

    name = _PROVIDER_NAME
    supported = {
        "hl_asset_ctx",
        "hl_predicted_funding",
        "hl_l2book",
        "hl_funding_history",
        "hl_market_pulse",
    }
    rate_per_min = 60  # one shared bucket; well under HL's 1200 weight/min/IP
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME
        # ``getattr``: the Settings field is optional wiring — the provider must
        # construct (and default to the public info URL) even before it exists.
        self._info_url = str(getattr(settings, "hyperliquid_info_url", "") or _DEFAULT_INFO_URL)
        #: Memoized universe map: upper-cased coin name -> canonical coin name.
        self._universe: dict[str, str] | None = None
        self._universe_at: float = 0.0

    # -- HTTP -----------------------------------------------------------------

    async def _post(self, body: dict[str, Any]) -> Any:
        """POST a typed JSON query to the info endpoint and return the JSON reply."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(self._info_url, json=body)
            response.raise_for_status()
            return response.json()

    def _provenance(self) -> Provenance:
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from the Hyperliquid info API."""
        self.ensure_supported(capability)
        handlers = {
            "hl_asset_ctx": self._fetch_asset_ctx,
            "hl_predicted_funding": self._fetch_predicted_funding,
            "hl_l2book": self._fetch_l2book,
            "hl_funding_history": self._fetch_funding_history,
            "hl_market_pulse": self._fetch_market_pulse,
        }
        return await handlers[capability](**params)

    # -- Universe / symbol resolution ------------------------------------------

    async def _meta_and_ctxs(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch ``metaAndAssetCtxs`` and split the ``[meta, assetCtxs]`` pair.

        The response is a two-element list: ``[0]`` is the meta dict carrying
        ``universe`` (name/szDecimals/maxLeverage/isDelisted rows) and ``[1]`` is
        the ctx array, **index-aligned** with the universe. Also refreshes the
        memoized universe map used for symbol resolution.

        Raises:
            ValueError: If the payload is not the documented two-element shape
                or carries no universe rows.
        """
        payload = await self._post({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            raise ValueError("Hyperliquid metaAndAssetCtxs returned an unexpected shape")
        meta = payload[0] if isinstance(payload[0], dict) else {}
        universe = meta.get("universe")
        rows = [r for r in universe if isinstance(r, dict)] if isinstance(universe, list) else []
        if not rows:
            raise ValueError("Hyperliquid metaAndAssetCtxs returned no universe")
        raw_ctxs = payload[1] if isinstance(payload[1], list) else []
        ctxs = [c if isinstance(c, dict) else {} for c in raw_ctxs]
        self._universe = {
            str(r["name"]).upper(): str(r["name"]) for r in rows if r.get("name")
        }
        self._universe_at = time.monotonic()
        return rows, ctxs

    async def _resolve_coin(self, symbol: str) -> str:
        """Map any symbol spelling to the canonical HL coin name (or raise).

        Args:
            symbol: User/agent spelling — ``BTCUSDT``, ``BTC``, ``btc``,
                ``BTC-PERP``, ``$BTC`` all resolve to ``BTC``.

        Returns:
            The exchange-canonical coin name (case-corrected, e.g. ``kPEPE``).

        Raises:
            ValueError: When no base can be derived or the coin is not listed.
        """
        candidate = _hl_base(symbol)
        if not candidate:
            raise ValueError(f"Cannot derive a Hyperliquid coin from {symbol!r}")
        if self._universe is None or time.monotonic() - self._universe_at > _UNIVERSE_TTL_S:
            await self._meta_and_ctxs()
        coin = (self._universe or {}).get(candidate)
        if coin is None:
            raise ValueError(f"{candidate!r} is not in the Hyperliquid perp universe")
        return coin

    # -- Capability handlers --------------------------------------------------

    async def _fetch_asset_ctx(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize one coin's slice of ``metaAndAssetCtxs`` into a flat ctx dict."""
        rows, ctxs = await self._meta_and_ctxs()
        candidate = _hl_base(symbol)
        index = next(
            (i for i, r in enumerate(rows) if str(r.get("name") or "").upper() == candidate),
            None,
        )
        if index is None:
            raise ValueError(f"{candidate!r} is not in the Hyperliquid perp universe")
        row = rows[index]
        ctx = ctxs[index] if index < len(ctxs) else {}
        funding = _to_float(ctx.get("funding"))
        impact = ctx.get("impactPxs")
        impact = list(impact) if isinstance(impact, (list, tuple)) else []
        return {
            "coin": str(row.get("name")),
            "mark_price": _to_float(ctx.get("markPx")),
            "oracle_price": _to_float(ctx.get("oraclePx")),
            "mid_price": _to_float(ctx.get("midPx")),
            "funding_hourly": funding,
            "funding_annualized": funding * 24.0 * 365.0 if funding is not None else None,
            "open_interest": _to_float(ctx.get("openInterest")),
            "premium": _to_float(ctx.get("premium")),
            "day_volume_usd": _to_float(ctx.get("dayNtlVlm")),
            "max_leverage": _to_float(row.get("maxLeverage")),
            "impact_bid": _to_float(impact[0]) if len(impact) > 0 else None,
            "impact_ask": _to_float(impact[1]) if len(impact) > 1 else None,
            "prev_day_price": _to_float(ctx.get("prevDayPx")),
            "as_of": utcnow_iso(),
            "provenance": self._provenance().to_dict(),
        }

    async def _fetch_predicted_funding(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``predictedFundings`` for one coin into a per-venue list.

        Upstream shape is nested tuples: ``[[coin, [[venue, {fundingRate,
        nextFundingTime, fundingIntervalHours}], ...]], ...]`` — venues are
        ``BinPerp``/``BybitPerp``/``HlPerp`` (HL settles hourly).
        """
        payload = await self._post({"type": "predictedFundings"})
        if not isinstance(payload, list):
            raise ValueError("Hyperliquid predictedFundings returned an unexpected shape")
        candidate = _hl_base(symbol)
        if not candidate:
            raise ValueError(f"Cannot derive a Hyperliquid coin from {symbol!r}")
        entry = next(
            (
                e
                for e in payload
                if isinstance(e, (list, tuple)) and len(e) >= 2 and str(e[0]).upper() == candidate
            ),
            None,
        )
        if entry is None:
            raise ValueError(f"No predicted funding for {candidate!r} on Hyperliquid")
        venues: list[dict[str, Any]] = []
        rows = entry[1] if isinstance(entry[1], (list, tuple)) else []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], dict):
                continue
            venues.append(
                {
                    "venue": str(row[0]),
                    "rate": _to_float(row[1].get("fundingRate")),
                    "interval_hours": _to_float(row[1].get("fundingIntervalHours")),
                }
            )
        return {
            "coin": str(entry[0]),
            "venues": venues,
            "as_of": utcnow_iso(),
            "provenance": self._provenance().to_dict(),
        }

    async def _fetch_l2book(
        self, symbol: str, n_sig_figs: int | None = None, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``l2Book`` into top-20 ``[price, size]`` rows per side.

        Args:
            symbol: Any coin spelling; resolved against the live universe.
            n_sig_figs: Optional price aggregation (HL accepts 2-5; clamped).
        """
        coin = await self._resolve_coin(symbol)
        body: dict[str, Any] = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            body["nSigFigs"] = max(2, min(int(n_sig_figs), 5))
        payload = await self._post(body)
        levels = payload.get("levels") if isinstance(payload, dict) else None
        if not isinstance(levels, list) or len(levels) < 2:
            raise ValueError(f"Hyperliquid l2Book returned no levels for {coin!r}")

        def _side(raw: Any) -> list[list[float]]:
            out: list[list[float]] = []
            for level in raw if isinstance(raw, list) else []:
                if not isinstance(level, dict):
                    continue
                px = _to_float(level.get("px"))
                sz = _to_float(level.get("sz"))
                if px is not None and sz is not None:
                    out.append([px, sz])
                if len(out) >= _BOOK_DEPTH:
                    break
            return out

        return {
            "coin": coin,
            "bids": _side(levels[0]),
            "asks": _side(levels[1]),
            "as_of": _ms_to_iso(payload.get("time")) or utcnow_iso(),
            "provenance": self._provenance().to_dict(),
        }

    async def _fetch_funding_history(
        self, symbol: str, hours: float = _DEFAULT_FUNDING_HOURS, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``fundingHistory`` (one record per HOUR on HL) into rates.

        Args:
            symbol: Any coin spelling; resolved against the live universe.
            hours: Lookback window in hours (default 72; capped at 30 days).
        """
        coin = await self._resolve_coin(symbol)
        window_h = _to_float(hours)
        if window_h is None or window_h <= 0:
            window_h = _DEFAULT_FUNDING_HOURS
        window_h = min(window_h, _MAX_FUNDING_HOURS)
        start_ms = int((time.time() - window_h * 3600.0) * 1000.0)
        payload = await self._post(
            {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        )
        rates: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict):
                continue
            rate = _to_float(row.get("fundingRate"))
            ts = _ms_to_iso(row.get("time"))
            if rate is None or ts is None:
                continue
            rates.append({"time": ts, "rate": rate})
        if not rates:
            raise ValueError(f"Hyperliquid returned no funding history for {coin!r}")
        return {
            "coin": coin,
            "rates": rates,
            "as_of": utcnow_iso(),
            "provenance": self._provenance().to_dict(),
        }

    async def _fetch_market_pulse(self, **_: Any) -> dict[str, Any]:
        """Snapshot every live HL perp + new-listing diff from ONE upstream call.

        Delisted coins are excluded from ``assets`` (they are not tradable) but
        kept in the persisted name snapshot so a delisting never re-reports the
        rest of the universe as new. ``open_interest_usd`` converts HL's
        coin-unit open interest via the mark price.
        """
        rows, ctxs = await self._meta_and_ctxs()
        assets: list[dict[str, Any]] = []
        names: list[str] = []
        for i, row in enumerate(rows):
            name = str(row.get("name") or "")
            if not name:
                continue
            names.append(name)
            if row.get("isDelisted"):
                continue
            ctx = ctxs[i] if i < len(ctxs) else {}
            mark = _to_float(ctx.get("markPx"))
            prev = _to_float(ctx.get("prevDayPx"))
            funding = _to_float(ctx.get("funding"))
            oi = _to_float(ctx.get("openInterest"))
            day_change = ((mark / prev) - 1.0) * 100.0 if mark is not None and prev else None
            assets.append(
                {
                    "coin": name,
                    "mark_price": mark,
                    "day_change_pct": day_change,
                    "funding_hourly": funding,
                    "funding_annualized": (
                        funding * 24.0 * 365.0 if funding is not None else None
                    ),
                    "open_interest_usd": (
                        oi * mark if oi is not None and mark is not None else None
                    ),
                    "day_volume_usd": _to_float(ctx.get("dayNtlVlm")),
                    "max_leverage": _to_float(row.get("maxLeverage")),
                    "premium": _to_float(ctx.get("premium")),
                }
            )
        return {
            "assets": assets,
            "new_listings": self._diff_universe_snapshot(names),
            "as_of": utcnow_iso(),
            "provenance": self._provenance().to_dict(),
        }

    # -- New-listing snapshot ---------------------------------------------------

    def _diff_universe_snapshot(self, names: list[str]) -> list[str]:
        """Diff the live universe names against the persisted snapshot, then update it.

        First run (missing/corrupt snapshot) returns ``[]`` — every coin would
        otherwise look "new" — and writes the snapshot. Read and write are both
        best-effort: disk trouble must never sink a market-pulse fetch.
        """
        path = self.settings.resolve_cache_dir() / _SNAPSHOT_FILE
        previous: list[str] | None = None
        try:
            with path.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and isinstance(doc.get("names"), list):
                previous = [str(n) for n in doc["names"]]
        except (OSError, ValueError):
            previous = None

        if previous is None:
            new_listings: list[str] = []
        else:
            seen = set(previous)
            new_listings = [n for n in names if n not in seen]

        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"names": names, "updated_at": utcnow_iso()}, fh, sort_keys=True)
            tmp.replace(path)
        except OSError:
            pass
        return new_listings


__all__ = ["HyperliquidInfoProvider"]
