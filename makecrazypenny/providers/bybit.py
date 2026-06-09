"""Bybit v5 linear-perpetual provider (CONTRACT.md §16).

The keyless fallback for crypto derivatives data, reachable from regions where
Binance's global API is geo-blocked. Talks to ``api.bybit.com`` (v5 market
endpoints) over ``httpx`` and normalizes into the same crypto core types as
:mod:`~makecrazypenny.providers.binance`, so the registry can fall through
transparently. Bybit's ``/v5/market/tickers`` conveniently returns price,
funding rate, and open interest in a single call.

Capabilities: ``crypto_ohlcv``, ``crypto_quote``, ``funding_rate``,
``open_interest``, ``long_short_ratio``. No API key required. ``httpx`` is
imported lazily so importing this module never hits the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.symbols import to_bybit_perp
from ..core.types import (
    FundingRate,
    LongShortRatio,
    OHLCV,
    OHLCVBar,
    OpenInterest,
    Provenance,
    Quote,
    utcnow_iso,
)
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "bybit"

#: Map interval aliases to Bybit kline intervals (minutes as strings, or D/W/M).
_KLINE_INTERVALS: dict[str, str] = {
    "1m": "1", "1min": "1", "3m": "3", "5m": "5", "5min": "5", "15m": "15",
    "15min": "15", "30m": "30", "60m": "60", "1h": "60", "2h": "120", "4h": "240",
    "6h": "360", "12h": "720", "1d": "D", "1day": "D", "daily": "D", "1w": "W", "1mo": "M",
}

#: Map interval aliases to Bybit stats periods (open-interest / account-ratio).
_STAT_PERIODS: dict[str, str] = {
    "1m": "5min", "5m": "5min", "5min": "5min", "15m": "15min", "15min": "15min",
    "30m": "30min", "1h": "1h", "60m": "1h", "2h": "4h", "4h": "4h", "1d": "1d", "daily": "1d",
}


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


def _kline_interval(interval: str) -> str:
    return _KLINE_INTERVALS.get(str(interval).strip().lower(), "5")


def _stat_period(interval: str) -> str:
    return _STAT_PERIODS.get(str(interval).strip().lower(), "5min")


@register_provider
class BybitProvider(Provider):
    """Bybit v5 linear-perpetual REST adapter (keyless)."""

    name = _PROVIDER_NAME
    supported = {
        "crypto_ohlcv",
        "crypto_quote",
        "funding_rate",
        "open_interest",
        "long_short_ratio",
    }
    rate_per_min = 600
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME
        self._base = settings.bybit_base_url.rstrip("/")
        #: Memoized per-symbol funding-interval hours (instruments-info carries
        #: ``fundingInterval`` in minutes; Bybit runs 1h/4h/8h depending on symbol).
        self._funding_intervals: dict[str, float] = {}

    # -- HTTP -----------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a Bybit v5 endpoint; return ``result`` or raise on a non-zero retCode."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        async with httpx.AsyncClient(base_url=self._base, timeout=20.0) as client:
            response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("retCode") not in (0, "0"):
            msg = body.get("retMsg") if isinstance(body, dict) else "unknown error"
            raise ValueError(f"Bybit error: {msg}")
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    def _provenance(self) -> Provenance:
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    async def _ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch the linear ticker row (price + funding + OI in one call)."""
        result = await self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        rows = result.get("list")
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from Bybit."""
        self.ensure_supported(capability)
        handlers = {
            "crypto_ohlcv": self._fetch_ohlcv,
            "crypto_quote": self._fetch_quote,
            "funding_rate": self._fetch_funding_rate,
            "open_interest": self._fetch_open_interest,
            "long_short_ratio": self._fetch_long_short_ratio,
        }
        return await handlers[capability](**params)

    # -- Capability handlers --------------------------------------------------

    async def _fetch_ohlcv(
        self, symbol: str, interval: str = "5m", limit: int = 500, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/v5/market/kline`` into :class:`OHLCV` (Bybit returns newest-first)."""
        sym = to_bybit_perp(symbol)
        iv = _kline_interval(interval)
        result = await self._get(
            "/v5/market/kline",
            {"category": "linear", "symbol": sym, "interval": iv, "limit": max(1, min(int(limit), 1000))},
        )
        rows = result.get("list") or []
        bars: list[OHLCVBar] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            ts = _ms_to_iso(row[0])
            close = _to_float(row[4])
            if ts is None or close is None:
                continue
            bars.append(
                OHLCVBar(
                    ts=ts,
                    open=_to_float(row[1]) or close,
                    high=_to_float(row[2]) or close,
                    low=_to_float(row[3]) or close,
                    close=close,
                    volume=_to_float(row[5]) or 0.0,
                )
            )
        bars.sort(key=lambda b: b.ts)  # Bybit returns newest-first; we want ascending
        return OHLCV(symbol=sym, interval=iv, bars=bars, provenance=self._provenance()).to_dict()

    async def _fetch_quote(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize the ticker into :class:`Quote`."""
        sym = to_bybit_perp(symbol)
        row = await self._ticker(sym)
        last = _to_float(row.get("lastPrice")) or 0.0
        prev = _to_float(row.get("prevPrice24h"))
        pcnt = _to_float(row.get("price24hPcnt"))
        return Quote(
            symbol=sym,
            price=last,
            change=(last - prev) if prev is not None else None,
            change_pct=(pcnt * 100.0) if pcnt is not None else None,
            provenance=self._provenance(),
        ).to_dict()

    async def _funding_interval_hours(self, symbol: str) -> float:
        """Funding interval (hours) for ``symbol`` from instruments-info.

        Memoized per symbol; any failure falls back to the 8h default — a
        wrong-but-close interval must never sink the funding fetch itself.
        """
        cached = self._funding_intervals.get(symbol)
        if cached is not None:
            return cached
        hours = 8.0
        try:
            result = await self._get(
                "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}
            )
            rows = result.get("list") or []
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            minutes = _to_float(row.get("fundingInterval"))
            if minutes and minutes > 0:
                hours = minutes / 60.0
        except Exception:
            pass
        self._funding_intervals[symbol] = hours
        return hours

    async def _fetch_funding_rate(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize the ticker's funding fields into :class:`FundingRate`."""
        sym = to_bybit_perp(symbol)
        row = await self._ticker(sym)
        rate = _to_float(row.get("fundingRate"))
        if rate is None:
            # Missing rate = provider failure, not a balanced market; raise so
            # the registry falls through the chain.
            raise ValueError(f"Bybit returned no funding rate for {sym!r}")
        interval_hours = await self._funding_interval_hours(sym)
        return FundingRate(
            symbol=sym,
            rate=rate,
            mark_price=_to_float(row.get("markPrice")),
            index_price=_to_float(row.get("indexPrice")),
            next_funding_time=_ms_to_iso(row.get("nextFundingTime")),
            interval_hours=interval_hours,
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_open_interest(
        self, symbol: str, interval: str = "5m", limit: int = 48, **_: Any
    ) -> dict[str, Any]:
        """Normalize open interest (current ticker + history) for the OI/price matrix."""
        sym = to_bybit_perp(symbol)
        row = await self._ticker(sym)
        history: list[dict[str, Any]] = []
        try:
            result = await self._get(
                "/v5/market/open-interest",
                {
                    "category": "linear",
                    "symbol": sym,
                    "intervalTime": _stat_period(interval),
                    "limit": max(1, min(int(limit), 200)),
                },
            )
            rows = result.get("list") or []
            for r in rows:
                if isinstance(r, dict):
                    history.append({"ts": _ms_to_iso(r.get("timestamp")), "open_interest": _to_float(r.get("openInterest"))})
            history.sort(key=lambda h: h.get("ts") or "")  # ascending
        except Exception:
            history = []
        oi = OpenInterest(
            symbol=sym,
            open_interest=_to_float(row.get("openInterest")) or 0.0,
            value=_to_float(row.get("openInterestValue")),
            ts=utcnow_iso(),
            provenance=self._provenance(),
        )
        return {**oi.to_dict(), "history": history}

    async def _fetch_long_short_ratio(
        self, symbol: str, interval: str = "5m", limit: int = 30, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/v5/market/account-ratio`` into :class:`LongShortRatio`."""
        sym = to_bybit_perp(symbol)
        result = await self._get(
            "/v5/market/account-ratio",
            {"category": "linear", "symbol": sym, "period": _stat_period(interval), "limit": max(1, min(int(limit), 200))},
        )
        rows = result.get("list") or []
        latest = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        buy = _to_float(latest.get("buyRatio"))
        sell = _to_float(latest.get("sellRatio"))
        ratio = (buy / sell) if (buy is not None and sell not in (None, 0)) else None
        return LongShortRatio(
            symbol=sym,
            ratio=ratio,
            long_pct=buy,
            short_pct=sell,
            ts=_ms_to_iso(latest.get("timestamp")),
            provenance=self._provenance(),
        ).to_dict()


__all__ = ["BybitProvider"]
