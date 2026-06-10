"""Binance USDⓈ-M futures provider (CONTRACT.md §16).

The richest **keyless** source of perpetual-futures market data: sub-minute
klines plus the derivatives metrics that matter for leveraged short-window
trading — funding rate, open interest, the long/short account ratio, taker
buy/sell flow, top-trader positioning, and settled-funding history. Talks to
``fapi.binance.com`` over ``httpx`` and normalizes into the crypto core types.

Geo note: the global Binance API is geo-blocked (HTTP 451) from some regions
(e.g. US IPs). Any such failure is a normal exception that the
:class:`ProviderRegistry` records and falls through — the chain continues to the
:mod:`~makecrazypenny.providers.bybit` fallback automatically. No special-casing
needed here.

Capabilities: ``crypto_ohlcv``, ``crypto_quote``, ``funding_rate``,
``open_interest``, ``long_short_ratio``, ``taker_flow``, ``top_trader_ratio``,
``funding_history``. No API key required. ``httpx`` is imported lazily so
importing this module never hits the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.symbols import to_binance_perp
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

_PROVIDER_NAME = "binance"

#: Map common interval aliases to Binance kline intervals.
_INTERVALS: dict[str, str] = {
    "1m": "1m", "1min": "1m", "3m": "3m", "5m": "5m", "5min": "5m",
    "15m": "15m", "15min": "15m", "30m": "30m", "60m": "1h", "1h": "1h",
    "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "1day": "1d", "daily": "1d", "1w": "1w", "1wk": "1w", "1mo": "1M",
}

#: Valid Binance open-interest-history periods (closest match is chosen).
_OI_PERIODS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
#: Valid long/short-ratio periods.
_LS_PERIODS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")


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


def _binance_interval(interval: str) -> str:
    """Resolve an interval alias to a valid Binance kline interval (default 5m)."""
    return _INTERVALS.get(str(interval).strip().lower(), "5m")


#: Minutes per Binance interval, for choosing the closest valid stats period.
_INTERVAL_MIN: dict[str, float] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
    "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "1w": 10080, "1M": 43200,
}


def _usdm_perp_or_skip(symbol: str, capability: str) -> str:
    """Resolve ``symbol`` to a USDⓈ-M perp symbol, or raise ``NotImplementedError``.

    The ``/futures/data/*`` statistics endpoints and ``/fapi/v1/fundingRate``
    exist only for USD-margined perpetuals. A pair that canonicalizes to a
    non-USDT quote (e.g. ``BTCEUR``, ``ETHBTC``) is a spot-only market on
    Binance, so the futures-only capabilities raise ``NotImplementedError`` —
    the :class:`ProviderRegistry` treats that as a silent skip (no circuit-
    breaker trip) and the chain simply moves on.
    """
    sym = to_binance_perp(symbol)
    if not sym.endswith("USDT"):
        raise NotImplementedError(
            f"Binance capability {capability!r} is futures-only; "
            f"{sym!r} is not a USD-margined perpetual symbol."
        )
    return sym


def _closest_period(interval: str, allowed: tuple[str, ...]) -> str:
    """Pick the *closest* valid stats period for a kline interval.

    The OI/long-short endpoints only accept the ``allowed`` periods; an interval
    outside that set (e.g. ``1m``, ``8h``, ``1w``) maps to the nearest one by
    duration so the stats window roughly matches the requested timeframe.
    """
    iv = _binance_interval(interval)
    if iv in allowed:
        return iv
    minutes = _INTERVAL_MIN.get(iv, 5.0)
    return min(allowed, key=lambda p: abs(_INTERVAL_MIN.get(p, 5.0) - minutes))


@register_provider
class BinanceProvider(Provider):
    """Binance USDⓈ-M futures REST adapter (keyless)."""

    name = _PROVIDER_NAME
    supported = {
        "crypto_ohlcv",
        "crypto_quote",
        "funding_rate",
        "open_interest",
        "long_short_ratio",
        "taker_flow",
        "top_trader_ratio",
        "funding_history",
    }
    rate_per_min = 1200  # fapi allows a high weight budget; stay polite
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME
        self._base = settings.binance_base_url.rstrip("/")
        #: Memoized per-symbol funding-interval hours from ``/fapi/v1/fundingInfo``
        #: (the endpoint lists only symbols that deviate from the 8h default).
        self._funding_intervals: dict[str, float] | None = None
        self._funding_intervals_at: float = 0.0

    # -- HTTP -----------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """Issue an unauthenticated GET to a Binance endpoint and return JSON."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        async with httpx.AsyncClient(base_url=self._base, timeout=20.0) as client:
            response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
            response.raise_for_status()
            return response.json()

    def _provenance(self) -> Provenance:
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from Binance futures."""
        self.ensure_supported(capability)
        handlers = {
            "crypto_ohlcv": self._fetch_ohlcv,
            "crypto_quote": self._fetch_quote,
            "funding_rate": self._fetch_funding_rate,
            "open_interest": self._fetch_open_interest,
            "long_short_ratio": self._fetch_long_short_ratio,
            "taker_flow": self._fetch_taker_flow,
            "top_trader_ratio": self._fetch_top_trader_ratio,
            "funding_history": self._fetch_funding_history,
        }
        return await handlers[capability](**params)

    # -- Capability handlers --------------------------------------------------

    async def _fetch_ohlcv(
        self, symbol: str, interval: str = "5m", limit: int = 500, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/fapi/v1/klines`` into :class:`OHLCV` (+ taker-flow extras).

        Each serialized bar row additionally carries ``quote_volume`` (kline
        field 7) and ``taker_buy_volume`` (field 9, taker buy base-asset volume)
        for the flow metrics (CVD). Additive only — the :class:`OHLCVBar` keys
        (``ts``/``open``/``high``/``low``/``close``/``volume``) are unchanged;
        a missing/invalid extra field is ``None``, never silently ``0.0``.
        """
        sym = to_binance_perp(symbol)
        iv = _binance_interval(interval)
        payload = await self._get(
            "/fapi/v1/klines",
            {"symbol": sym, "interval": iv, "limit": max(1, min(int(limit), 1500))},
        )
        bars: list[OHLCVBar] = []
        extras: list[dict[str, Any]] = []
        if isinstance(payload, list):
            for row in payload:
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
                # Fields 7/9 are not part of the frozen OHLCVBar core type;
                # they ride alongside and are merged into the rows below.
                extras.append(
                    {
                        "quote_volume": _to_float(row[7]) if len(row) > 7 else None,
                        "taker_buy_volume": _to_float(row[9]) if len(row) > 9 else None,
                    }
                )
        result = OHLCV(symbol=sym, interval=iv, bars=bars, provenance=self._provenance()).to_dict()
        for bar_row, extra in zip(result["bars"], extras):
            bar_row.update(extra)
        return result

    async def _fetch_quote(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/fapi/v1/ticker/24hr`` into :class:`Quote`."""
        sym = to_binance_perp(symbol)
        data = await self._get("/fapi/v1/ticker/24hr", {"symbol": sym})
        data = data if isinstance(data, dict) else {}
        return Quote(
            symbol=sym,
            price=_to_float(data.get("lastPrice")) or 0.0,
            change=_to_float(data.get("priceChange")),
            change_pct=_to_float(data.get("priceChangePercent")),
            provenance=self._provenance(),
        ).to_dict()

    async def _funding_interval_hours(self, symbol: str) -> float:
        """Funding interval (hours) for ``symbol``; 8h unless the exchange says otherwise.

        ``/fapi/v1/fundingInfo`` lists only the symbols whose interval deviates
        from the 8h default (many run 4h). The list is memoized for an hour and
        any failure silently falls back to 8h — a wrong-but-close interval must
        never sink the funding fetch itself.
        """
        import time

        now = time.monotonic()
        if self._funding_intervals is None or now - self._funding_intervals_at > 3600.0:
            intervals: dict[str, float] = {}
            try:
                rows = await self._get("/fapi/v1/fundingInfo", {})
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict):
                            s = str(r.get("symbol") or "")
                            h = _to_float(r.get("fundingIntervalHours"))
                            if s and h and h > 0:
                                intervals[s] = h
            except Exception:
                pass  # keep whatever we had; default applies below
            self._funding_intervals = intervals
            self._funding_intervals_at = now
        return self._funding_intervals.get(symbol, 8.0)

    async def _fetch_funding_rate(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/fapi/v1/premiumIndex`` into :class:`FundingRate`."""
        sym = to_binance_perp(symbol)
        data = await self._get("/fapi/v1/premiumIndex", {"symbol": sym})
        data = data if isinstance(data, dict) else {}
        rate = _to_float(data.get("lastFundingRate"))
        if rate is None:
            # A missing rate is a provider failure, not a "perfectly balanced
            # market" — raise so the registry falls through the chain.
            raise ValueError(f"Binance returned no funding rate for {sym!r}")
        interval_hours = await self._funding_interval_hours(sym)
        return FundingRate(
            symbol=sym,
            rate=rate,
            mark_price=_to_float(data.get("markPrice")),
            index_price=_to_float(data.get("indexPrice")),
            next_funding_time=_ms_to_iso(data.get("nextFundingTime")),
            interval_hours=interval_hours,
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_open_interest(
        self, symbol: str, interval: str = "5m", limit: int = 48, **_: Any
    ) -> dict[str, Any]:
        """Normalize open interest (current + recent history) for the OI/price matrix.

        Uses ``/futures/data/openInterestHist`` whose latest point is the current
        OI; if that data endpoint is unavailable, falls back to the current-only
        ``/fapi/v1/openInterest``.
        """
        sym = to_binance_perp(symbol)
        period = _closest_period(interval, _OI_PERIODS)
        history: list[dict[str, Any]] = []
        try:
            rows = await self._get(
                "/futures/data/openInterestHist",
                {"symbol": sym, "period": period, "limit": max(1, min(int(limit), 500))},
            )
        except Exception:
            rows = None
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                history.append(
                    {
                        "ts": _ms_to_iso(r.get("timestamp")),
                        "open_interest": _to_float(r.get("sumOpenInterest")),
                        "value": _to_float(r.get("sumOpenInterestValue")),
                    }
                )

        if history:
            latest = history[-1]
            oi = OpenInterest(
                symbol=sym,
                open_interest=latest.get("open_interest") or 0.0,
                value=latest.get("value"),
                ts=latest.get("ts"),
                provenance=self._provenance(),
            )
        else:
            cur = await self._get("/fapi/v1/openInterest", {"symbol": sym})
            cur = cur if isinstance(cur, dict) else {}
            oi = OpenInterest(
                symbol=sym,
                open_interest=_to_float(cur.get("openInterest")) or 0.0,
                value=None,
                ts=_ms_to_iso(cur.get("time")),
                provenance=self._provenance(),
            )
        return {**oi.to_dict(), "history": history}

    async def _fetch_long_short_ratio(
        self, symbol: str, interval: str = "5m", limit: int = 30, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/futures/data/globalLongShortAccountRatio`` into :class:`LongShortRatio`."""
        sym = to_binance_perp(symbol)
        period = _closest_period(interval, _LS_PERIODS)
        rows = await self._get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": sym, "period": period, "limit": max(1, min(int(limit), 500))},
        )
        latest = rows[-1] if isinstance(rows, list) and rows and isinstance(rows[-1], dict) else {}
        return LongShortRatio(
            symbol=sym,
            ratio=_to_float(latest.get("longShortRatio")),
            long_pct=_to_float(latest.get("longAccount")),
            short_pct=_to_float(latest.get("shortAccount")),
            ts=_ms_to_iso(latest.get("timestamp")),
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_taker_flow(
        self, symbol: str, interval: str = "5m", limit: int = 48, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/futures/data/takerlongshortRatio`` into a taker-flow series.

        Returns ``{"symbol", "series": [{"time", "buy_sell_ratio"}], "as_of"}``
        oldest-first; ``buy_sell_ratio`` > 1 means taker buys exceeded taker
        sells in that window (aggressive buying pressure). Futures-only:
        spot-only pairs raise ``NotImplementedError`` (registry silent skip).
        """
        sym = _usdm_perp_or_skip(symbol, "taker_flow")
        period = _closest_period(interval, _LS_PERIODS)
        rows = await self._get(
            "/futures/data/takerlongshortRatio",
            {"symbol": sym, "period": period, "limit": max(1, min(int(limit), 500))},
        )
        series: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ts = _ms_to_iso(r.get("timestamp"))
                ratio = _to_float(r.get("buySellRatio"))
                if ts is None or ratio is None:
                    continue
                series.append({"time": ts, "buy_sell_ratio": ratio})
        return {"symbol": sym, "series": series, "as_of": utcnow_iso()}

    async def _fetch_top_trader_ratio(
        self, symbol: str, interval: str = "5m", limit: int = 48, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/futures/data/topLongShortPositionRatio`` into a ratio series.

        Top 20% of accounts by margin balance, **position**-weighted (not
        account-counted) — the "smart money" side of the top-vs-crowd spread.
        Returns ``{"symbol", "series": [{"time", "ratio"}], "as_of"}``
        oldest-first. Futures-only: spot-only pairs raise
        ``NotImplementedError`` (registry silent skip).
        """
        sym = _usdm_perp_or_skip(symbol, "top_trader_ratio")
        period = _closest_period(interval, _LS_PERIODS)
        rows = await self._get(
            "/futures/data/topLongShortPositionRatio",
            {"symbol": sym, "period": period, "limit": max(1, min(int(limit), 500))},
        )
        series: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ts = _ms_to_iso(r.get("timestamp"))
                ratio = _to_float(r.get("longShortRatio"))
                if ts is None or ratio is None:
                    continue
                series.append({"time": ts, "ratio": ratio})
        return {"symbol": sym, "series": series, "as_of": utcnow_iso()}

    async def _fetch_funding_history(
        self, symbol: str, limit: int = 66, **_: Any
    ) -> dict[str, Any]:
        """Normalize ``/fapi/v1/fundingRate`` (settled fundings) into a rate series.

        Returns ``{"symbol", "rates": [{"time", "rate"}], "as_of"}`` oldest-
        first. The default ``limit=66`` covers ~22 days of 8h settlements —
        enough history for a trailing-21d funding z-score without over-
        fetching. Futures-only: spot-only pairs raise ``NotImplementedError``
        (registry silent skip).
        """
        sym = _usdm_perp_or_skip(symbol, "funding_history")
        rows = await self._get(
            "/fapi/v1/fundingRate",
            {"symbol": sym, "limit": max(1, min(int(limit), 1000))},
        )
        rates: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ts = _ms_to_iso(r.get("fundingTime"))
                rate = _to_float(r.get("fundingRate"))
                if ts is None or rate is None:
                    continue
                rates.append({"time": ts, "rate": rate})
        return {"symbol": sym, "rates": rates, "as_of": utcnow_iso()}


__all__ = ["BinanceProvider"]
