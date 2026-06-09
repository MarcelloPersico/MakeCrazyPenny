"""yfinance provider adapter (see CONTRACT.md §8.7).

Wraps the synchronous `yfinance` library to serve the ``ohlcv``, ``quote``, and
``fundamentals`` capabilities. yfinance needs no API key (``requires_key`` is
``None``) and we stay polite rather than enforcing a hard rate (``rate_per_min``
is ``0`` -> effectively unlimited token bucket).

Engineering mandates honored here:
  * **Import safety.** ``yfinance``/``pandas`` are imported lazily *inside* the
    fetch helpers, never at module top, so importing this module never requires
    the heavy libs and never hits the network.
  * **Sync lib off the event loop.** Every blocking yfinance call runs inside
    ``asyncio.to_thread(...)``.
  * **Normalization.** Each capability maps the raw payload into the matching
    core dataclass and returns its ``to_dict()`` output (JSON-serializable).
  * ``fetch`` raises ``NotImplementedError`` for unsupported capabilities (via
    :meth:`Provider.ensure_supported`). ``MissingApiKey`` is never raised because
    no key is required, but the base ``api_key()`` contract still applies.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from ..core.types import OHLCV, OHLCVBar, Provenance, Quote, utcnow_iso
from .base import Provider, register_provider

# Fundamentals fields copied through from ``Ticker.info`` (the useful subset).
_FUNDAMENTALS_FIELDS: tuple[str, ...] = (
    "longName",
    "shortName",
    "sector",
    "industry",
    "country",
    "currency",
    "exchange",
    "marketCap",
    "enterpriseValue",
    "trailingPE",
    "forwardPE",
    "pegRatio",
    "priceToBook",
    "priceToSalesTrailing12Months",
    "enterpriseToRevenue",
    "enterpriseToEbitda",
    "beta",
    "dividendYield",
    "dividendRate",
    "payoutRatio",
    "trailingEps",
    "forwardEps",
    "bookValue",
    "revenuePerShare",
    "totalRevenue",
    "revenueGrowth",
    "earningsGrowth",
    "grossMargins",
    "operatingMargins",
    "profitMargins",
    "ebitdaMargins",
    "returnOnAssets",
    "returnOnEquity",
    "totalCash",
    "totalDebt",
    "debtToEquity",
    "currentRatio",
    "quickRatio",
    "freeCashflow",
    "operatingCashflow",
    "sharesOutstanding",
    "floatShares",
    "fiftyTwoWeekHigh",
    "fiftyTwoWeekLow",
    "fiftyDayAverage",
    "twoHundredDayAverage",
)


def _clean(value: Any) -> Any:
    """Coerce a raw value into something JSON-serializable, dropping junk.

    yfinance frequently yields ``NaN``/``Infinity`` floats and numpy scalars.
    ``NaN``/``inf`` are mapped to ``None`` (not valid JSON), numpy scalars are
    converted to native Python types, and everything else is passed through.
    """
    if value is None:
        return None
    # Unwrap numpy / pandas scalar wrappers to native Python types.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def _to_float(value: Any) -> float | None:
    """Best-effort conversion to a finite ``float``, else ``None``."""
    value = _clean(value)
    if value is None:
        return None
    try:
        out = float(value)
    except (ValueError, TypeError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


@register_provider
class YFinanceProvider(Provider):
    """Provider adapter backed by the ``yfinance`` library.

    Serves ``ohlcv`` (via ``yf.Ticker.history``), ``quote`` (via
    ``fast_info``/last close), and ``fundamentals`` (a subset of ``Ticker.info``).
    """

    name = "yfinance"
    supported = {"ohlcv", "quote", "fundamentals"}
    rate_per_min = 0  # no published limit; the registry bucket stays unlimited
    cost = 1
    requires_key = None

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Dispatch ``capability`` to the matching normalized fetcher.

        Args:
            capability: One of ``"ohlcv"``, ``"quote"``, ``"fundamentals"``.
            **params: Capability-specific params (notably ``symbol``).

        Returns:
            The matching core dataclass's ``to_dict()`` output.

        Raises:
            NotImplementedError: If ``capability`` is not supported.
        """
        self.ensure_supported(capability)
        # No key is required; calling api_key() keeps the base contract uniform
        # (it returns "" when requires_key is None and would raise MissingApiKey
        # if a key were ever required).
        self.api_key()

        if capability == "ohlcv":
            return await self._fetch_ohlcv(**params)
        if capability == "quote":
            return await self._fetch_quote(**params)
        if capability == "fundamentals":
            return await self._fetch_fundamentals(**params)
        # Defensive: ensure_supported already guards this.
        raise NotImplementedError(
            f"Provider {self.name!r} does not support capability {capability!r}."
        )

    # -- ohlcv --------------------------------------------------------------

    async def _fetch_ohlcv(
        self,
        symbol: str,
        interval: str = "1d",
        period: str = "6mo",
        **_: Any,
    ) -> dict[str, Any]:
        """Fetch OHLCV history and normalize to :class:`OHLCV`."""
        sym = _norm_symbol(symbol)
        bars = await asyncio.to_thread(self._ohlcv_blocking, sym, interval, period)
        series = OHLCV(
            symbol=sym,
            interval=interval,
            bars=bars,
            provenance=Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False),
        )
        return series.to_dict()

    @staticmethod
    def _ohlcv_blocking(symbol: str, interval: str, period: str) -> list[OHLCVBar]:
        """Blocking yfinance history call -> list of normalized bars.

        Runs inside ``asyncio.to_thread``. ``yfinance`` and ``pandas`` are
        imported here (lazily) so module import stays light.
        """
        import yfinance as yf  # noqa: PLC0415 (lazy import is intentional)

        ticker = yf.Ticker(symbol)
        # auto_adjust=True: split/dividend-adjusted prices. The factor lookbacks
        # (12-1 momentum, 200-DMA, 52w high) and the backtest consume these bars;
        # unadjusted closes would turn every split into a fake crash.
        frame = ticker.history(period=period, interval=interval, auto_adjust=True)

        bars: list[OHLCVBar] = []
        if frame is None or getattr(frame, "empty", True):
            return bars

        cols = {str(c).lower(): c for c in frame.columns}
        open_c = cols.get("open")
        high_c = cols.get("high")
        low_c = cols.get("low")
        close_c = cols.get("close")
        vol_c = cols.get("volume")

        for idx, row in frame.iterrows():
            ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            close = _to_float(row.get(close_c)) if close_c is not None else None
            if close is None:
                # A bar without a close is unusable; skip it.
                continue
            open_v = _to_float(row.get(open_c)) if open_c is not None else None
            high_v = _to_float(row.get(high_c)) if high_c is not None else None
            low_v = _to_float(row.get(low_c)) if low_c is not None else None
            vol_v = _to_float(row.get(vol_c)) if vol_c is not None else None
            bars.append(
                OHLCVBar(
                    ts=ts,
                    open=open_v if open_v is not None else close,
                    high=high_v if high_v is not None else close,
                    low=low_v if low_v is not None else close,
                    close=close,
                    volume=vol_v if vol_v is not None else 0.0,
                )
            )
        return bars

    # -- quote --------------------------------------------------------------

    async def _fetch_quote(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Fetch the latest price and normalize to :class:`Quote`."""
        sym = _norm_symbol(symbol)
        price, prev_close = await asyncio.to_thread(self._quote_blocking, sym)
        if price is None:
            raise ValueError(f"yfinance returned no price for {sym!r}")

        change: float | None = None
        change_pct: float | None = None
        if prev_close is not None and prev_close != 0:
            change = price - prev_close
            change_pct = (change / prev_close) * 100.0

        quote = Quote(
            symbol=sym,
            price=price,
            change=change,
            change_pct=change_pct,
            provenance=Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False),
        )
        return quote.to_dict()

    @staticmethod
    def _quote_blocking(symbol: str) -> tuple[float | None, float | None]:
        """Blocking last-price lookup -> ``(price, previous_close)``.

        Prefers ``fast_info`` (cheap, no full info pull); falls back to the most
        recent 1d history close if ``fast_info`` is unavailable.
        """
        import yfinance as yf  # noqa: PLC0415 (lazy import is intentional)

        ticker = yf.Ticker(symbol)

        price: float | None = None
        prev_close: float | None = None

        fast = getattr(ticker, "fast_info", None)
        if fast is not None:
            price = (
                _fast_get(fast, "last_price")
                or _fast_get(fast, "lastPrice")
                or _fast_get(fast, "last_close")
            )
            prev_close = _fast_get(fast, "previous_close") or _fast_get(fast, "previousClose")

        if price is None:
            # Fallback: pull a tiny recent history and use the last two closes.
            frame = ticker.history(period="5d", interval="1d", auto_adjust=False)
            if frame is not None and not getattr(frame, "empty", True):
                cols = {str(c).lower(): c for c in frame.columns}
                close_c = cols.get("close")
                if close_c is not None:
                    closes = [_to_float(v) for v in frame[close_c].tolist()]
                    closes = [c for c in closes if c is not None]
                    if closes:
                        price = closes[-1]
                        if prev_close is None and len(closes) >= 2:
                            prev_close = closes[-2]

        return price, prev_close

    # -- fundamentals -------------------------------------------------------

    async def _fetch_fundamentals(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Fetch a subset of ``Ticker.info`` plus provenance.

        Returns a plain JSON-serializable dict (there is no dedicated core type
        for fundamentals); provenance is attached so callers can attribute it.
        """
        sym = _norm_symbol(symbol)
        info = await asyncio.to_thread(self._fundamentals_blocking, sym)
        provenance = Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)
        return {
            "symbol": sym,
            "fundamentals": info,
            "provenance": provenance.to_dict(),
        }

    @staticmethod
    def _fundamentals_blocking(symbol: str) -> dict[str, Any]:
        """Blocking ``Ticker.info`` pull -> cleaned subset dict."""
        import yfinance as yf  # noqa: PLC0415 (lazy import is intentional)

        ticker = yf.Ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception:
            # Some yfinance/network states raise on .info; degrade to get_info.
            getter = getattr(ticker, "get_info", None)
            info = getter() if callable(getter) else {}
            info = info or {}

        out: dict[str, Any] = {}
        for key in _FUNDAMENTALS_FIELDS:
            if key in info:
                out[key] = _clean(info[key])
        return out


def _norm_symbol(symbol: str) -> str:
    """Uppercase, strip whitespace, and drop a leading ``$`` (e.g. ``$aapl``)."""
    s = str(symbol).strip()
    if s.startswith("$"):
        s = s[1:]
    return s.strip().upper()


def _fast_get(fast: Any, key: str) -> float | None:
    """Read ``key`` from a yfinance ``fast_info`` (mapping- or attr-style)."""
    value: Any = None
    getter = getattr(fast, "get", None)
    if callable(getter):
        try:
            value = fast.get(key)
        except Exception:
            value = None
    if value is None:
        value = getattr(fast, key, None)
    return _to_float(value)


__all__ = ["YFinanceProvider"]
