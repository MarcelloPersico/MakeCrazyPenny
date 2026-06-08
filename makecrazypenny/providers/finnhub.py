"""Finnhub provider adapter (see CONTRACT.md §8.7).

Talks to the Finnhub REST API (https://finnhub.io/api/v1) over ``httpx`` and
normalizes each capability's raw payload into the matching ``core.types``
value object, returning its ``to_dict()`` output (JSON-serializable).

Capabilities served (FROZEN names):
    ohlcv, quote, company_news, news_sentiment, social_sentiment,
    congress_trades, insider_transactions, analyst_ratings, price_targets,
    upgrades_downgrades

Behavioral contract:
  * A missing ``FINNHUB_API_KEY`` raises :class:`MissingApiKey` so the registry
    falls through the fallback chain.
  * An unsupported capability raises ``NotImplementedError`` so the registry
    skips this provider.
  * Every response is normalized to a core type and returned as ``to_dict()``
    output, with ``provenance.cached=False`` (the registry reports true cache
    status separately).

Import safety: importing this module never hits the network and never requires
a key. ``httpx`` is imported lazily inside :meth:`fetch` so the module imports
even when ``httpx`` is absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.types import (
    AnalystRating,
    CongressTrade,
    InsiderTransaction,
    NewsItem,
    OHLCV,
    OHLCVBar,
    PriceTarget,
    Provenance,
    Quote,
    SentimentScore,
    UpgradeDowngrade,
    utcnow_iso,
)
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_BASE_URL = "https://finnhub.io/api/v1"
_PROVIDER_NAME = "finnhub"

# Map common interval aliases to Finnhub candle resolutions.
_RESOLUTION_MAP: dict[str, str] = {
    "1m": "1",
    "1min": "1",
    "5m": "5",
    "5min": "5",
    "15m": "15",
    "15min": "15",
    "30m": "30",
    "30min": "30",
    "60m": "60",
    "1h": "60",
    "1d": "D",
    "1day": "D",
    "d": "D",
    "day": "D",
    "daily": "D",
    "1w": "W",
    "1wk": "W",
    "w": "W",
    "weekly": "W",
    "1mo": "M",
    "1month": "M",
    "m": "M",
    "monthly": "M",
}


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; returns ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int:
    """Best-effort int coercion; returns ``0`` on missing/invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _epoch_to_iso(epoch: Any) -> str | None:
    """Convert a Unix epoch (seconds) to an ISO-8601 UTC string, or ``None``."""
    ts = _to_float(epoch)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@register_provider
class FinnhubProvider(Provider):
    """Finnhub REST adapter.

    See the module docstring and CONTRACT.md §8.7 for the capability matrix.
    """

    name = _PROVIDER_NAME
    supported = {
        "ohlcv",
        "quote",
        "company_news",
        "news_sentiment",
        "social_sentiment",
        "congress_trades",
        "insider_transactions",
        "analyst_ratings",
        "price_targets",
        "upgrades_downgrades",
    }
    rate_per_min = 60
    requires_key = "FINNHUB_API_KEY"

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME

    # -- HTTP -----------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """Issue an authenticated GET to a Finnhub endpoint and return JSON.

        ``httpx`` is imported lazily here to keep module import network-free and
        dependency-light.

        Args:
            path: Endpoint path beginning with ``/`` (e.g. ``"/quote"``).
            params: Query parameters (the API token is sent as a header).

        Returns:
            The decoded JSON body (dict or list).

        Raises:
            httpx.HTTPStatusError: On a non-2xx response.
        """
        import httpx  # lazy import (see CONTRACT.md §2.2)

        # Authenticate via header, NOT a `?token=` query param, so the key never
        # appears in a URL (and thus never in an httpx error message / log).
        headers = {"X-Finnhub-Token": self.api_key()}
        async with httpx.AsyncClient(
            base_url=_BASE_URL, timeout=20.0, headers=headers
        ) as client:
            response = await client.get(path, params=dict(params))
            response.raise_for_status()
            return response.json()

    def _provenance(self) -> Provenance:
        """Build provenance for a freshly fetched payload (``cached=False``)."""
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from Finnhub.

        Args:
            capability: One of the FROZEN capability names.
            **params: Capability-specific parameters (commonly ``symbol``).

        Returns:
            A JSON-serializable normalized result (a core type's ``to_dict()``
            output, or a list of them).

        Raises:
            MissingApiKey: If ``FINNHUB_API_KEY`` is not configured.
            NotImplementedError: If ``capability`` is not supported.
        """
        self.ensure_supported(capability)
        # Force the key check up front so a missing key short-circuits before any
        # network work (registry falls through the chain on MissingApiKey).
        self.api_key()

        handlers = {
            "ohlcv": self._fetch_ohlcv,
            "quote": self._fetch_quote,
            "company_news": self._fetch_company_news,
            "news_sentiment": self._fetch_news_sentiment,
            "social_sentiment": self._fetch_social_sentiment,
            "congress_trades": self._fetch_congress_trades,
            "insider_transactions": self._fetch_insider_transactions,
            "analyst_ratings": self._fetch_analyst_ratings,
            "price_targets": self._fetch_price_targets,
            "upgrades_downgrades": self._fetch_upgrades_downgrades,
        }
        return await handlers[capability](**params)

    # -- Capability handlers --------------------------------------------------

    async def _fetch_ohlcv(
        self,
        symbol: str,
        interval: str = "1d",
        *,
        resolution: str | None = None,
        from_: int | None = None,
        to: int | None = None,
        count: int | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Normalize ``/stock/candle`` into :class:`OHLCV`."""
        sym = symbol.upper()
        res = resolution or _RESOLUTION_MAP.get(interval.lower(), "D")

        query: dict[str, Any] = {"symbol": sym, "resolution": res}
        if count is not None:
            query["count"] = count
        else:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            query["from"] = from_ if from_ is not None else now - 60 * 60 * 24 * 365
            query["to"] = to if to is not None else now

        payload = await self._get("/stock/candle", query)

        bars: list[OHLCVBar] = []
        if isinstance(payload, dict) and payload.get("s") == "ok":
            opens = payload.get("o") or []
            highs = payload.get("h") or []
            lows = payload.get("l") or []
            closes = payload.get("c") or []
            volumes = payload.get("v") or []
            times = payload.get("t") or []
            for i in range(len(times)):
                ts_iso = _epoch_to_iso(times[i])
                if ts_iso is None:
                    continue
                bars.append(
                    OHLCVBar(
                        ts=ts_iso,
                        open=_to_float(opens[i]) or 0.0,
                        high=_to_float(highs[i]) or 0.0,
                        low=_to_float(lows[i]) or 0.0,
                        close=_to_float(closes[i]) or 0.0,
                        volume=_to_float(volumes[i]) or 0.0,
                    )
                )

        return OHLCV(
            symbol=sym,
            interval=interval,
            bars=bars,
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_quote(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/quote`` into :class:`Quote`."""
        sym = symbol.upper()
        payload = await self._get("/quote", {"symbol": sym})
        data = payload if isinstance(payload, dict) else {}
        return Quote(
            symbol=sym,
            price=_to_float(data.get("c")) or 0.0,
            change=_to_float(data.get("d")),
            change_pct=_to_float(data.get("dp")),
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_company_news(
        self,
        symbol: str,
        from_: str | None = None,
        to: str | None = None,
        days: int | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Normalize ``/company-news`` into a list of :class:`NewsItem`."""
        sym = symbol.upper()
        today = datetime.now(tz=timezone.utc).date()
        if to is None:
            to = today.isoformat()
        if from_ is None:
            span = days if days is not None else 7
            from_ = (today.fromordinal(today.toordinal() - span)).isoformat()

        payload = await self._get(
            "/company-news", {"symbol": sym, "from": from_, "to": to}
        )

        items: list[NewsItem] = []
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                items.append(
                    NewsItem(
                        symbol=sym,
                        headline=str(entry.get("headline") or ""),
                        source=entry.get("source") or None,
                        url=entry.get("url") or None,
                        published_at=_epoch_to_iso(entry.get("datetime")),
                        summary=entry.get("summary") or None,
                    )
                )
        return [item.to_dict() for item in items]

    async def _fetch_news_sentiment(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/news-sentiment`` into :class:`SentimentScore`.

        Finnhub returns a company news score in ``[0, 1]`` and a bullish/bearish
        percent split. We map the bullish-vs-bearish balance to a ``[-1, 1]``
        score and derive a coarse label.
        """
        sym = symbol.upper()
        payload = await self._get("/news-sentiment", {"symbol": sym})
        data = payload if isinstance(payload, dict) else {}

        sentiment = data.get("sentiment") or {}
        bullish = _to_float(sentiment.get("bullishPercent")) or 0.0
        bearish = _to_float(sentiment.get("bearishPercent")) or 0.0
        # bullish/bearish are fractions in [0, 1]; balance maps to [-1, 1].
        score = bullish - bearish

        buzz = data.get("buzz") or {}
        n_articles = _to_int(buzz.get("articlesInLastWeek"))

        if score > 0.15:
            label = "bullish"
        elif score < -0.15:
            label = "bearish"
        else:
            label = "neutral"

        drivers: list[str] = []
        company_score = _to_float(data.get("companyNewsScore"))
        if company_score is not None:
            drivers.append(f"companyNewsScore={company_score:.3f}")
        sector_avg = _to_float(data.get("sectorAverageBullishPercent"))
        if sector_avg is not None:
            drivers.append(f"sectorAvgBullish={sector_avg:.3f}")

        return SentimentScore.normalize(
            symbol=sym,
            score=score,
            label=label,
            n_articles=n_articles,
            drivers=drivers,
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_social_sentiment(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/stock/social-sentiment`` into :class:`SentimentScore`.

        Aggregates Reddit and Twitter entries: averages their per-entry ``score``
        and sums mentions for ``n_articles``.
        """
        sym = symbol.upper()
        payload = await self._get("/stock/social-sentiment", {"symbol": sym})
        data = payload if isinstance(payload, dict) else {}

        scores: list[float] = []
        mentions = 0
        sources_present: list[str] = []
        for source_key in ("reddit", "twitter"):
            entries = data.get(source_key) or []
            if not isinstance(entries, list):
                continue
            had_entry = False
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                had_entry = True
                s = _to_float(entry.get("score"))
                if s is not None:
                    scores.append(s)
                mentions += _to_int(entry.get("mention"))
            if had_entry:
                sources_present.append(source_key)

        score = sum(scores) / len(scores) if scores else 0.0
        if score > 0.15:
            label = "bullish"
        elif score < -0.15:
            label = "bearish"
        else:
            label = "neutral"

        return SentimentScore.normalize(
            symbol=sym,
            score=score,
            label=label,
            n_articles=mentions,
            drivers=sources_present,
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_congress_trades(
        self,
        symbol: str,
        from_: str | None = None,
        to: str | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Normalize ``/stock/congressional-trading`` into :class:`CongressTrade`."""
        sym = symbol.upper()
        query: dict[str, Any] = {"symbol": sym}
        if from_ is not None:
            query["from"] = from_
        if to is not None:
            query["to"] = to

        payload = await self._get("/stock/congressional-trading", query)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        trades: list[CongressTrade] = []
        if isinstance(rows, list):
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                amount_low = entry.get("amountFrom")
                amount_high = entry.get("amountTo")
                amount_range = entry.get("amount")
                if amount_range is None and (amount_low is not None or amount_high is not None):
                    amount_range = f"{amount_low}-{amount_high}"
                trades.append(
                    CongressTrade(
                        symbol=sym,
                        member=str(entry.get("name") or ""),
                        chamber=str(entry.get("chamber") or "") or "",
                        transaction=str(
                            entry.get("transactionType")
                            or entry.get("transaction")
                            or ""
                        ),
                        amount_range=str(amount_range) if amount_range is not None else None,
                        transaction_date=entry.get("transactionDate") or None,
                        disclosure_date=entry.get("filingDate") or None,
                        provenance=self._provenance(),
                    )
                )
        return [trade.to_dict() for trade in trades]

    async def _fetch_insider_transactions(
        self,
        symbol: str,
        from_: str | None = None,
        to: str | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Normalize ``/stock/insider-transactions`` into :class:`InsiderTransaction`."""
        sym = symbol.upper()
        query: dict[str, Any] = {"symbol": sym}
        if from_ is not None:
            query["from"] = from_
        if to is not None:
            query["to"] = to

        payload = await self._get("/stock/insider-transactions", query)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        txns: list[InsiderTransaction] = []
        if isinstance(rows, list):
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                shares = _to_float(entry.get("share") or entry.get("change"))
                price = _to_float(entry.get("transactionPrice"))
                value = None
                if shares is not None and price is not None:
                    value = shares * price
                txns.append(
                    InsiderTransaction(
                        symbol=sym,
                        insider=str(entry.get("name") or ""),
                        role=None,
                        transaction=str(entry.get("transactionCode") or ""),
                        shares=shares,
                        value=value,
                        date=entry.get("transactionDate") or entry.get("filingDate") or None,
                        provenance=self._provenance(),
                    )
                )
        return [txn.to_dict() for txn in txns]

    async def _fetch_analyst_ratings(self, symbol: str, **_: Any) -> list[dict[str, Any]]:
        """Normalize ``/stock/recommendation`` into a list of :class:`AnalystRating`."""
        sym = symbol.upper()
        payload = await self._get("/stock/recommendation", {"symbol": sym})
        ratings: list[AnalystRating] = []
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                ratings.append(
                    AnalystRating(
                        symbol=sym,
                        period=str(entry.get("period") or ""),
                        strong_buy=_to_int(entry.get("strongBuy")),
                        buy=_to_int(entry.get("buy")),
                        hold=_to_int(entry.get("hold")),
                        sell=_to_int(entry.get("sell")),
                        strong_sell=_to_int(entry.get("strongSell")),
                        provenance=self._provenance(),
                    )
                )
        return [rating.to_dict() for rating in ratings]

    async def _fetch_price_targets(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/stock/price-target`` into :class:`PriceTarget`."""
        sym = symbol.upper()
        payload = await self._get("/stock/price-target", {"symbol": sym})
        data = payload if isinstance(payload, dict) else {}
        return PriceTarget(
            symbol=sym,
            mean=_to_float(data.get("targetMean")),
            high=_to_float(data.get("targetHigh")),
            low=_to_float(data.get("targetLow")),
            current=_to_float(data.get("lastPrice")),
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_upgrades_downgrades(
        self,
        symbol: str,
        from_: str | None = None,
        to: str | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Normalize ``/stock/upgrade-downgrade`` into :class:`UpgradeDowngrade`."""
        sym = symbol.upper()
        query: dict[str, Any] = {"symbol": sym}
        if from_ is not None:
            query["from"] = from_
        if to is not None:
            query["to"] = to

        payload = await self._get("/stock/upgrade-downgrade", query)
        events: list[UpgradeDowngrade] = []
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                events.append(
                    UpgradeDowngrade(
                        symbol=sym,
                        firm=str(entry.get("company") or ""),
                        from_grade=entry.get("fromGrade") or None,
                        to_grade=entry.get("toGrade") or None,
                        action=str(entry.get("action") or ""),
                        date=_epoch_to_iso(entry.get("gradeTime")),
                        provenance=self._provenance(),
                    )
                )
        return [event.to_dict() for event in events]


__all__ = ["FinnhubProvider"]
