"""House & Senate Stock Watcher provider adapter (see CONTRACT.md §8.7).

Serves the ``congress_trades`` capability from the open-source *Stock Watcher*
project's bulk aggregate transaction feeds (large JSON files mirrored on GitHub
/ S3). No API key is required.

Both chambers publish a single aggregate JSON array of every disclosed trade.
Each record carries a ticker, the disclosing member, a transaction type, an
amount *range*, and the transaction / disclosure dates. This adapter downloads
both feeds, normalizes every record into :class:`CongressTrade`, and applies the
``symbol`` / ``member`` / ``since`` filters in memory.

Engineering notes:
  * **Import safety** — importing this module never hits the network. ``httpx``
    is lazy-imported inside :meth:`fetch`.
  * **Caching** — these feeds are large, so the registry caches the normalized
    result aggressively via the ``congress_trades`` TTL (1 hour). Set
    ``provenance.cached=False`` here; the registry reports the true cache status.
  * **No key** — :attr:`requires_key` is ``None``; ``fetch`` never raises
    ``MissingApiKey`` for this provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.types import CongressTrade, Provenance, utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # for typing only; never imported at runtime
    from ..core.config import Settings

# Bulk aggregate transaction feeds from the Stock Watcher project. These are the
# large, GitHub-hosted aggregate JSON arrays (one record per disclosed trade).
HOUSE_FEED_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
SENATE_FEED_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)

# Be polite to the public mirror — these are large files.
_HTTP_TIMEOUT_S = 60.0
_USER_AGENT = "MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"


def _norm(value: Any) -> str:
    """Return a stripped string for any value (``None`` -> empty string)."""
    if value is None:
        return ""
    return str(value).strip()


@register_provider
class StockWatcherProvider(Provider):
    """House & Senate Stock Watcher bulk-JSON congressional-trade adapter.

    Supports only ``congress_trades``. No API key. Downloads both chambers'
    aggregate feeds and filters in memory by ``symbol`` / ``member`` / ``since``.
    """

    name = "stockwatcher"
    supported = {"congress_trades"}
    rate_per_min = 0  # be polite; the registry's bucket treats 0 as unlimited
    cost = 1
    requires_key = None  # no key — fetch never raises MissingApiKey

    def __init__(self, settings: "Settings") -> None:
        """Store settings and resolve the (overridable) feed URLs.

        Args:
            settings: Process configuration. Optional attributes
                ``stockwatcher_house_url`` / ``stockwatcher_senate_url`` override
                the default feed URLs if present.
        """
        super().__init__(settings)
        self._house_url: str = getattr(settings, "stockwatcher_house_url", None) or HOUSE_FEED_URL
        self._senate_url: str = (
            getattr(settings, "stockwatcher_senate_url", None) or SENATE_FEED_URL
        )

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch congressional trades, normalized to ``CongressTrade`` dicts.

        Args:
            capability: Must be ``"congress_trades"``.
            **params: Optional filters:
                * ``symbol``: ticker to match (case-insensitive, ``$`` stripped).
                * ``member``: substring match against the disclosing member's
                  name (case-insensitive).
                * ``since``: ISO date/datetime string; keep trades whose
                  transaction date (falling back to disclosure date) is on or
                  after it (lexicographic ``YYYY-MM-DD`` compare).

        Returns:
            A list of :meth:`CongressTrade.to_dict` dicts, newest disclosure
            first.

        Raises:
            NotImplementedError: If ``capability`` is not ``congress_trades``.
        """
        self.ensure_supported(capability)

        # Lazy-import the heavy HTTP lib so module import stays network/dep free.
        import httpx

        symbol = params.get("symbol")
        member = params.get("member")
        since = params.get("since")

        symbol_key = _norm(symbol).lstrip("$").upper() or None
        member_key = _norm(member).lower() or None
        since_key = self._date_prefix(since) if since else None

        fetched_at = utcnow_iso()

        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S, headers=headers, follow_redirects=True
        ) as client:
            house_rows = await self._get_feed(client, self._house_url)
            senate_rows = await self._get_feed(client, self._senate_url)

        trades: list[CongressTrade] = []
        for row in house_rows:
            trades.append(self._normalize_row(row, chamber="House", fetched_at=fetched_at))
        for row in senate_rows:
            trades.append(self._normalize_row(row, chamber="Senate", fetched_at=fetched_at))

        filtered = [
            t
            for t in trades
            if self._matches(t, symbol_key=symbol_key, member_key=member_key, since_key=since_key)
        ]

        # Newest disclosure first (empty dates sort last).
        filtered.sort(
            key=lambda t: (t.disclosure_date or "", t.transaction_date or ""),
            reverse=True,
        )

        return [t.to_dict() for t in filtered]

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    async def _get_feed(client: Any, url: str) -> list[dict[str, Any]]:
        """GET one aggregate feed; return its list of records (``[]`` on failure).

        A single chamber being unavailable (404, transient error, non-list body)
        degrades to an empty list rather than failing the whole fetch, so the
        other chamber's data still flows through.
        """
        import httpx

        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        # Some mirrors wrap the array under a "transactions"/"data" key.
        if isinstance(data, dict):
            for key in ("transactions", "data", "results"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return [row for row in inner if isinstance(row, dict)]
        return []

    @staticmethod
    def _normalize_row(row: dict[str, Any], *, chamber: str, fetched_at: str) -> CongressTrade:
        """Map one raw Stock Watcher record into a :class:`CongressTrade`.

        The House feed names the member ``representative``; the Senate feed names
        it ``senator``. Other field names are shared across both feeds.
        """
        member = (
            _norm(row.get("representative"))
            or _norm(row.get("senator"))
            or _norm(row.get("member"))
            or "Unknown"
        )
        symbol = (_norm(row.get("ticker")) or _norm(row.get("symbol"))).lstrip("$").upper()
        transaction = _norm(row.get("type")) or _norm(row.get("transaction")) or "unknown"
        amount_range = _norm(row.get("amount")) or _norm(row.get("amount_range")) or None
        transaction_date = (
            _norm(row.get("transaction_date")) or _norm(row.get("transactionDate")) or None
        )
        disclosure_date = (
            _norm(row.get("disclosure_date")) or _norm(row.get("disclosureDate")) or None
        )

        return CongressTrade(
            symbol=symbol,
            member=member,
            chamber=chamber,
            transaction=transaction,
            amount_range=amount_range,
            transaction_date=transaction_date,
            disclosure_date=disclosure_date,
            provenance=Provenance(provider="stockwatcher", fetched_at=fetched_at, cached=False),
        )

    @staticmethod
    def _date_prefix(value: Any) -> str | None:
        """Extract a ``YYYY-MM-DD`` prefix from an ISO date/datetime string.

        Stock Watcher dates are stored as ``YYYY-MM-DD`` (sometimes the literal
        string ``"--"`` for unknown). Comparing 10-char ISO-date prefixes
        lexicographically gives a correct chronological compare. Returns ``None``
        if no usable prefix is found.
        """
        text = _norm(value)
        if not text or text == "--":
            return None
        # Normalize separators and take the date portion.
        text = text.replace("/", "-")
        head = text.split("T", 1)[0].split(" ", 1)[0]
        return head[:10] if head else None

    @classmethod
    def _matches(
        cls,
        trade: CongressTrade,
        *,
        symbol_key: str | None,
        member_key: str | None,
        since_key: str | None,
    ) -> bool:
        """Return whether ``trade`` passes all active filters."""
        if symbol_key is not None and trade.symbol != symbol_key:
            return False
        if member_key is not None and member_key not in trade.member.lower():
            return False
        if since_key is not None:
            ref = cls._date_prefix(trade.transaction_date) or cls._date_prefix(
                trade.disclosure_date
            )
            if ref is None or ref < since_key:
                return False
        return True


__all__ = ["StockWatcherProvider", "HOUSE_FEED_URL", "SENATE_FEED_URL"]
