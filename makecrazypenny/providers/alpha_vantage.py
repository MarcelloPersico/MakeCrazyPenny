"""Alpha Vantage provider adapter (see CONTRACT.md §8.7).

Talks to ``https://www.alphavantage.co/query`` over ``httpx`` and normalizes the
raw JSON into the project's core value types. Supports four capabilities:

  * ``ohlcv``           -> ``TIME_SERIES_*`` (intraday / daily / weekly / monthly)
  * ``quote``           -> ``GLOBAL_QUOTE``
  * ``news_sentiment``  -> ``NEWS_SENTIMENT`` (aggregated to a :class:`SentimentScore`)
  * ``fundamentals``    -> ``OVERVIEW``

Behavioral contract (enforced here):
  * Missing ``ALPHA_VANTAGE_API_KEY`` -> raise :class:`MissingApiKey` so the
    registry silently falls through to the next provider in the chain.
  * Unsupported capability -> raise ``NotImplementedError`` so the registry skips.
  * Every successful response is normalized to the matching core dataclass and
    returned as ``to_dict()`` output (a JSON-serializable dict / list).

Import safety: ``httpx`` is lazy-imported inside :meth:`fetch`; importing this
module never hits the network and never requires a key. The free Alpha Vantage
tier is capped at 5 requests/minute (modeled via ``rate_per_min``) and 500/day
(documented only — see CONTRACT.md §13.8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.errors import ProviderError
from ..core.types import (
    OHLCV,
    OHLCVBar,
    Provenance,
    Quote,
    SentimentScore,
)
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

# Base endpoint for every Alpha Vantage REST call.
_BASE_URL = "https://www.alphavantage.co/query"

# Network timeout (seconds) for a single upstream call.
_TIMEOUT_S = 30.0

# Map a requested OHLCV interval to the Alpha Vantage ``function`` + the prefix
# of the JSON key that carries the bar dictionary, plus any extra query params.
# Intraday intervals carry their cadence in the ``interval`` query param.
_INTRADAY_INTERVALS = {"1min", "5min", "15min", "30min", "60min"}


@register_provider
class AlphaVantageProvider(Provider):
    """Alpha Vantage adapter for OHLCV, quotes, news sentiment and fundamentals."""

    name = "alpha_vantage"
    supported = {"ohlcv", "quote", "news_sentiment", "fundamentals"}
    rate_per_min = 5  # free tier: 5 requests/minute (500/day documented only)
    cost = 1
    requires_key = "ALPHA_VANTAGE_API_KEY"

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        # Distinct providers may share a bucket by sharing a rate_key; Alpha
        # Vantage uses its own key, so the bucket key matches the name.
        self.rate_key = "alpha_vantage"

    # -- public API ---------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize data for ``capability``.

        Raises:
            MissingApiKey: If ``ALPHA_VANTAGE_API_KEY`` is absent.
            NotImplementedError: If ``capability`` is not supported.
            ProviderError: On an upstream error / throttle / malformed payload.
        """
        self.ensure_supported(capability)
        # Raises MissingApiKey if the required key is absent -> registry skips.
        api_key = self.api_key()

        if capability == "ohlcv":
            return await self._fetch_ohlcv(api_key, **params)
        if capability == "quote":
            return await self._fetch_quote(api_key, **params)
        if capability == "news_sentiment":
            return await self._fetch_news_sentiment(api_key, **params)
        if capability == "fundamentals":
            return await self._fetch_fundamentals(api_key, **params)

        # Defensive: supported set and dispatch above are kept in sync, but a
        # mismatch should still surface as an unsupported-capability skip.
        raise NotImplementedError(
            f"Provider {self.name!r} does not support capability {capability!r}."
        )

    # -- HTTP plumbing ------------------------------------------------------

    async def _request(self, api_key: str, query: dict[str, Any]) -> dict[str, Any]:
        """Issue a single GET to the Alpha Vantage endpoint and return its JSON.

        ``httpx`` is imported here (lazy) so importing the module stays cheap and
        offline-safe. Upstream error / throttle envelopes are converted into a
        :class:`ProviderError` so the registry's circuit breaker can react.
        """
        import httpx  # lazy import — keeps module import network/dep free

        full_query = {**query, "apikey": api_key}
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(_BASE_URL, params=full_query)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            raise ProviderError(f"Alpha Vantage returned a non-object payload: {type(data)!r}")

        # Alpha Vantage signals problems with 200-OK bodies carrying one of these
        # keys rather than an HTTP error status. Treat them as runtime failures.
        for key in ("Error Message", "Note", "Information"):
            if key in data:
                raise ProviderError(f"Alpha Vantage {key}: {data[key]}")

        return data

    # -- capability handlers ------------------------------------------------

    async def _fetch_ohlcv(self, api_key: str, **params: Any) -> dict[str, Any]:
        """Normalize a ``TIME_SERIES_*`` response into :class:`OHLCV`."""
        symbol = _require_symbol(params)
        interval = str(params.get("interval", "1d"))
        period = str(params.get("period", ""))

        query, series_key_prefix = _ohlcv_query(symbol, interval, period)
        data = await self._request(api_key, query)

        series = _extract_time_series(data, series_key_prefix)
        bars: list[OHLCVBar] = []
        # Alpha Vantage returns newest-first; sort ascending by timestamp.
        for ts in sorted(series.keys()):
            row = series[ts]
            bars.append(
                OHLCVBar(
                    ts=ts,
                    open=_to_float(row.get("1. open")),
                    high=_to_float(row.get("2. high")),
                    low=_to_float(row.get("3. low")),
                    close=_to_float(row.get("4. close")),
                    # Adjusted endpoints place volume under "6. volume"; plain
                    # endpoints under "5. volume". Accept whichever is present.
                    volume=_to_float(row.get("5. volume", row.get("6. volume"))),
                )
            )

        ohlcv = OHLCV(
            symbol=symbol,
            interval=interval,
            bars=bars,
            provenance=Provenance(provider=self.name, cached=False),
        )
        return ohlcv.to_dict()

    async def _fetch_quote(self, api_key: str, **params: Any) -> dict[str, Any]:
        """Normalize a ``GLOBAL_QUOTE`` response into :class:`Quote`."""
        symbol = _require_symbol(params)
        data = await self._request(api_key, {"function": "GLOBAL_QUOTE", "symbol": symbol})

        quote_block = data.get("Global Quote") or data.get("globalQuote") or {}
        if not quote_block:
            raise ProviderError(f"Alpha Vantage returned no quote for {symbol!r}.")

        change_pct_raw = quote_block.get("10. change percent")
        quote = Quote(
            symbol=quote_block.get("01. symbol") or symbol,
            price=_to_float(quote_block.get("05. price")),
            change=_to_float_or_none(quote_block.get("09. change")),
            change_pct=_parse_percent(change_pct_raw),
            provenance=Provenance(provider=self.name, cached=False),
        )
        return quote.to_dict()

    async def _fetch_news_sentiment(self, api_key: str, **params: Any) -> dict[str, Any]:
        """Aggregate ``NEWS_SENTIMENT`` into a single :class:`SentimentScore`.

        Alpha Vantage returns a ``feed`` of articles, each with a list of
        ``ticker_sentiment`` entries. We average the ticker-specific scores for
        the requested symbol, map the mean onto a label, and surface the top
        article headlines as drivers.
        """
        symbol = _require_symbol(params)
        query: dict[str, Any] = {"function": "NEWS_SENTIMENT", "tickers": symbol}
        # Pass through optional Alpha Vantage filters when callers supply them.
        for opt in ("topics", "time_from", "time_to", "sort", "limit"):
            if params.get(opt) is not None:
                query[opt] = params[opt]

        data = await self._request(api_key, query)
        feed = data.get("feed") or []

        scores: list[float] = []
        drivers: list[str] = []
        for article in feed:
            for ts in article.get("ticker_sentiment", []):
                if (ts.get("ticker") or "").upper() == symbol.upper():
                    score = _to_float_or_none(ts.get("ticker_sentiment_score"))
                    if score is not None:
                        scores.append(score)
                        headline = article.get("title")
                        if headline and len(drivers) < 5:
                            drivers.append(headline)
                    break

        mean = sum(scores) / len(scores) if scores else 0.0
        sentiment = SentimentScore.normalize(
            symbol=symbol,
            score=mean,
            label=_sentiment_label(mean),
            n_articles=len(scores),
            drivers=drivers,
            provenance=Provenance(provider=self.name, cached=False),
        )
        return sentiment.to_dict()

    async def _fetch_fundamentals(self, api_key: str, **params: Any) -> dict[str, Any]:
        """Return the ``OVERVIEW`` payload with a provenance block attached.

        There is no dedicated fundamentals core type, so the (already
        JSON-serializable) overview dict is returned verbatim with a
        ``provenance`` entry so callers can trace its origin.
        """
        symbol = _require_symbol(params)
        data = await self._request(api_key, {"function": "OVERVIEW", "symbol": symbol})

        if not data.get("Symbol"):
            raise ProviderError(f"Alpha Vantage returned no fundamentals for {symbol!r}.")

        result = dict(data)
        result["symbol"] = data.get("Symbol") or symbol
        result["provenance"] = Provenance(provider=self.name, cached=False).to_dict()
        return result


# ---------------------------------------------------------------------------
# Module-level helpers (pure, stateless — no network, easy to unit-test).
# ---------------------------------------------------------------------------


def _require_symbol(params: dict[str, Any]) -> str:
    """Extract and normalize a required ``symbol`` param, or raise."""
    symbol = params.get("symbol")
    if not symbol:
        raise ProviderError("Alpha Vantage: a 'symbol' parameter is required.")
    return str(symbol).strip().upper()


def _needs_full_output(period: str) -> bool:
    """Whether the requested look-back exceeds AV's 100-point ``compact`` window.

    ``compact`` returns only the latest 100 daily bars, silently starving the
    12-1 momentum (253 bars) and 200-DMA factors when AV serves the chain. Any
    period of a year or longer (``1y``, ``2y``, ``10y``, ``max``) needs ``full``.
    """
    p = period.strip().lower()
    if not p:
        return False
    if p == "max" or p.endswith("y"):
        return True
    # Month-denominated periods: >4 months of trading days exceeds 100 bars.
    if p.endswith("mo"):
        try:
            return int(p[:-2]) > 4
        except ValueError:
            return False
    return False


def _ohlcv_query(symbol: str, interval: str, period: str = "") -> tuple[dict[str, Any], str]:
    """Map an interval to the Alpha Vantage function + the time-series key prefix.

    Returns a ``(query, key_prefix)`` tuple. ``key_prefix`` is matched against the
    response keys to locate the bar dictionary (Alpha Vantage names that key
    differently per function, e.g. ``"Time Series (Daily)"``). ``period`` selects
    ``outputsize`` (``full`` for >100-bar look-backs).

    Note: the daily series is split/dividend-UNadjusted (the adjusted endpoint is
    premium-only). yfinance, which serves adjusted bars, is ahead of AV in the
    default ``ohlcv`` chain; AV is a degraded fallback.
    """
    iv = interval.strip().lower()
    outputsize = "full" if _needs_full_output(period) else "compact"

    if iv in _INTRADAY_INTERVALS:
        return (
            {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": iv,
                "outputsize": outputsize,
            },
            "Time Series (",
        )
    if iv in ("1wk", "1w", "week", "weekly"):
        return ({"function": "TIME_SERIES_WEEKLY", "symbol": symbol}, "Weekly Time Series")
    if iv in ("1mo", "1m", "month", "monthly"):
        return ({"function": "TIME_SERIES_MONTHLY", "symbol": symbol}, "Monthly Time Series")

    # Default: daily bars (covers "1d", "1day", "daily", and anything unknown).
    return (
        {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": outputsize},
        "Time Series (Daily)",
    )


def _extract_time_series(data: dict[str, Any], key_prefix: str) -> dict[str, Any]:
    """Find the time-series sub-dict in a TIME_SERIES_* response.

    Alpha Vantage names the series key per function (e.g. ``"Time Series (Daily)"``,
    ``"Time Series (5min)"``). We match the first non-"Meta Data" key whose name
    starts with ``key_prefix``.
    """
    for key, value in data.items():
        if key == "Meta Data":
            continue
        if key.startswith(key_prefix) and isinstance(value, dict):
            return value
    raise ProviderError("Alpha Vantage response did not contain a recognizable time series.")


def _sentiment_label(score: float) -> str:
    """Map a mean sentiment score onto Alpha Vantage's qualitative label scale."""
    if score <= -0.35:
        return "Bearish"
    if score <= -0.15:
        return "Somewhat-Bearish"
    if score < 0.15:
        return "Neutral"
    if score < 0.35:
        return "Somewhat-Bullish"
    return "Bullish"


def _to_float(value: Any) -> float:
    """Coerce a value to ``float``, defaulting to ``0.0`` on failure/None."""
    result = _to_float_or_none(value)
    return result if result is not None else 0.0


def _to_float_or_none(value: Any) -> float | None:
    """Coerce a value to ``float``, returning ``None`` if it cannot be parsed."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _parse_percent(value: Any) -> float | None:
    """Parse a percent string like ``"1.2345%"`` into a float (``1.2345``)."""
    if value is None:
        return None
    return _to_float_or_none(str(value).strip().rstrip("%"))


__all__ = ["AlphaVantageProvider"]
