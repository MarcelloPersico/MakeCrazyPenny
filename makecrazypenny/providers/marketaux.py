"""Marketaux provider adapter (see CONTRACT.md §8.7).

Marketaux serves financial news headlines for one or more symbols. This adapter
exposes a single capability, ``company_news``, normalizing the upstream JSON into
a list of :class:`~makecrazypenny.core.types.NewsItem` ``to_dict()`` payloads.

Engineering mandates honored here (CONTRACT.md §2):
  * **Import safety** — ``httpx`` is lazy-imported *inside* :meth:`fetch`; importing
    this module never hits the network and never requires a key.
  * **Missing key** — :meth:`fetch` raises
    :class:`~makecrazypenny.core.errors.MissingApiKey` when ``MARKETAUX_API_KEY``
    is absent, so the registry falls through to the next provider in the chain.
  * **Unsupported capability** — raises ``NotImplementedError``.
  * **Normalization** — returns JSON-serializable ``NewsItem.to_dict()`` output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.types import NewsItem
from .base import Provider, register_provider

if TYPE_CHECKING:  # only for typing; no runtime import cycle / heavy import
    from ..core.config import Settings

# Marketaux news endpoint (CONTRACT.md §8.7).
_NEWS_URL = "https://api.marketaux.com/v1/news/all"

# Be conservative with payload size; Marketaux free tier returns 3 articles/page,
# but we ask explicitly so behavior is stable across plan tiers.
_DEFAULT_LIMIT = 50
_HTTP_TIMEOUT = 20.0


@register_provider
class MarketauxProvider(Provider):
    """Marketaux REST adapter for the ``company_news`` capability."""

    name = "marketaux"
    supported = {"company_news"}
    rate_per_min = 0  # free-tier; be polite (registry handles bucketing)
    cost = 1
    requires_key = "MARKETAUX_API_KEY"

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize data for ``capability``.

        Args:
            capability: Must be ``"company_news"``.
            **params: Capability params. Recognized keys:
                ``symbol`` (str, required): ticker to fetch news for.
                ``limit`` (int, optional): max articles to request.
                ``days`` (int, optional): only return articles published in the
                    last ``days`` days (passed to Marketaux as ``published_after``).

        Returns:
            For ``company_news``: a ``list[dict]`` of ``NewsItem.to_dict()`` output.

        Raises:
            MissingApiKey: If ``MARKETAUX_API_KEY`` is not configured.
            NotImplementedError: If ``capability`` is not supported.
        """
        self.ensure_supported(capability)

        if capability == "company_news":
            return await self._company_news(**params)

        # Defensive: ensure_supported should have already rejected this.
        raise NotImplementedError(
            f"Provider {self.name!r} does not support capability {capability!r}."
        )

    async def _company_news(
        self,
        symbol: str = "",
        *,
        limit: int = _DEFAULT_LIMIT,
        days: int | None = None,
        **_ignored: Any,
    ) -> list[dict[str, Any]]:
        """Fetch company news for ``symbol`` and normalize to ``NewsItem`` dicts."""
        # Raises MissingApiKey if the key is absent → registry falls through.
        key = self.api_key()

        norm_symbol = _normalize_symbol(symbol)

        params: dict[str, Any] = {
            "api_token": key,
            "symbols": norm_symbol,
            "language": "en",
            "limit": int(limit),
        }
        if days is not None:
            published_after = _published_after(days)
            if published_after:
                params["published_after"] = published_after

        # Lazy import: keep module import network/dep free (CONTRACT.md §2).
        import httpx

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(_NEWS_URL, params=params)
            response.raise_for_status()
            payload = response.json()

        return _normalize_news(norm_symbol, payload)


def _normalize_symbol(symbol: str) -> str:
    """Uppercase, strip whitespace, and strip a leading ``$`` (e.g. ' $aapl ' -> 'AAPL')."""
    return (symbol or "").strip().lstrip("$").strip().upper()


def _published_after(days: int) -> str | None:
    """Return an ISO-8601 UTC timestamp ``days`` days in the past, or ``None``.

    Marketaux accepts ``published_after`` as ``YYYY-MM-DDTHH:MM`` (UTC).
    """
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=n)
    # Marketaux expects minute precision in UTC.
    return cutoff.strftime("%Y-%m-%dT%H:%M")


def _normalize_news(symbol: str, payload: Any) -> list[dict[str, Any]]:
    """Map a Marketaux ``/news/all`` payload to a list of ``NewsItem`` dicts.

    Marketaux response shape (relevant fields)::

        {
          "data": [
            {
              "title": "...",
              "description": "...",
              "snippet": "...",
              "url": "...",
              "source": "...",
              "published_at": "2024-01-01T00:00:00.000000Z",
              "entities": [{"symbol": "AAPL", ...}, ...]
            },
            ...
          ]
        }

    Missing/None fields map to ``None`` on the dataclass.
    """
    items: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return items

    data = payload.get("data")
    if not isinstance(data, list):
        return items

    for article in data:
        if not isinstance(article, dict):
            continue

        headline = article.get("title")
        if not headline:
            # An article without a headline is not useful; skip it.
            continue

        # Prefer the entity-matched symbol when present; fall back to the query.
        item_symbol = _entity_symbol(article, fallback=symbol)

        summary = article.get("description") or article.get("snippet")

        news_item = NewsItem(
            symbol=item_symbol,
            headline=str(headline),
            source=article.get("source"),
            url=article.get("url"),
            published_at=article.get("published_at"),
            summary=summary,
        )
        items.append(news_item.to_dict())

    return items


def _entity_symbol(article: dict[str, Any], *, fallback: str) -> str:
    """Return the article's matched ticker from ``entities``, or ``fallback``."""
    entities = article.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict):
                sym = entity.get("symbol")
                if sym:
                    return str(sym).upper()
    return fallback


__all__ = ["MarketauxProvider"]
