"""Crypto news RSS provider (DESIGN-SWARM.md).

Keyless headline feed for the host-side news reader: CoinTelegraph and CoinDesk
publisher RSS plus a per-coin Google News RSS sweep, merged, deduplicated by
normalized title (case-insensitive), and sorted newest-first. Parsing uses
stdlib ``xml.etree`` only — no new dependencies — and handles both RSS 2.0
(``<item>``) and Atom (``<entry>``) documents. A feed that fails to fetch or
parse is skipped; the provider raises only when EVERY feed fails, so the
registry records the failure and the dossier carries an ``_error`` marker.

News items are NOT scored by the deterministic engine (host-side
interpretation only — DESIGN-SWARM hard constraint 1); they ride in the
dossier as ASCII-sanitized titles with UTC timestamps and ``age_minutes``.

Verified from this host (2026-06-10): ``cointelegraph.com/rss`` 200; CoinDesk
308-redirects (the client follows redirects); ``news.google.com/rss/search``
200 (Google item links are encoded redirects — titles + ``<source>`` carry the
attribution).

Capability: ``news_feed``. No API key required. ``httpx`` is imported lazily
so importing this module never hits the network.
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from ..core.symbols import base_asset
from ..core.types import utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "news_rss"

#: Publisher feeds polled on every scan (label, url). CoinDesk 308-redirects.
_FEEDS: tuple[tuple[str, str], ...] = (
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
)
_GOOGLE_NEWS_URL = "https://news.google.com/rss/search"

#: Descriptive UA (publisher feeds answer plain clients; identify politely).
_USER_AGENT = "MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"

#: Items kept per feed before the merged sort (Google returns up to 100).
_PER_FEED_CAP = 50

#: Human coin names for the Google News query (mirrors social._COIN_NAMES).
_COIN_NAMES: dict[str, str] = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB",
    "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano", "AVAX": "Avalanche",
    "LINK": "Chainlink", "DOT": "Polkadot", "MATIC": "Polygon", "LTC": "Litecoin",
    "TRX": "Tron", "BCH": "Bitcoin Cash", "NEAR": "Near Protocol", "APT": "Aptos",
    "ARB": "Arbitrum", "OP": "Optimism", "SUI": "Sui", "INJ": "Injective",
    "ATOM": "Cosmos", "FIL": "Filecoin", "ETC": "Ethereum Classic",
    "UNI": "Uniswap", "AAVE": "Aave", "TIA": "Celestia", "SEI": "Sei",
    "RUNE": "THORChain", "PEPE": "Pepe", "WIF": "dogwifhat", "SHIB": "Shiba Inu",
    "TON": "Toncoin", "ICP": "Internet Computer", "RNDR": "Render",
    "FTM": "Fantom", "ALGO": "Algorand", "HBAR": "Hedera", "STX": "Stacks",
    "IMX": "Immutable", "GALA": "Gala", "HYPE": "Hyperliquid",
}

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def _ascii(text: Any) -> str:
    """Sanitize arbitrary text to single-spaced printable ASCII.

    Unicode whitespace (incl. NBSP) is normalized to spaces first so dropping
    non-ASCII codepoints never glues words together; then emoji/CJK/smart
    punctuation are stripped and whitespace runs collapse to one space.
    ``$TICKER`` cashtags are plain ASCII and pass through untouched.
    """
    s = str(text) if text is not None else ""
    s = _WS_RE.sub(" ", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    return _WS_RE.sub(" ", s).strip()


def _strip_html(text: Any) -> str:
    """Drop HTML tags and unescape entities (feed titles may embed markup)."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", str(text)))


def _to_int(value: Any) -> int:
    """Best-effort int coercion; ``0`` on missing/invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _resolve_base(symbol: Any) -> str | None:
    """Map the request symbol to a base coin, or ``None`` for a market-wide feed."""
    s = str(symbol or "").strip().upper()
    if not s or s == "CRYPTO":
        return None
    return base_asset(s)


def _google_query(base: str | None) -> str:
    """Build the Google News query (DESIGN-SWARM: coin name OR symbol crypto)."""
    if not base:
        return "crypto OR bitcoin"
    name = _COIN_NAMES.get(base)
    if name and name.upper() != base:
        return f"{name} OR {base} crypto"
    return f"{base} crypto"


def _local_tag(tag: Any) -> str:
    """Lower-cased local name of a (possibly namespaced) element tag."""
    return str(tag).rsplit("}", 1)[-1].lower()


def _child_text(item: "ET.Element", *names: str) -> str | None:
    """First non-empty text of a direct child matching ``names`` (in priority order)."""
    for name in names:
        for child in item:
            if _local_tag(child.tag) == name and child.text and child.text.strip():
                return child.text.strip()
    return None


def _item_url(item: "ET.Element") -> str:
    """Best link for an item: RSS text link first, then an Atom ``href``."""
    fallback = ""
    for child in item:
        if _local_tag(child.tag) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        href = (child.get("href") or "").strip()
        if href:
            if (child.get("rel") or "alternate") == "alternate":
                return href
            fallback = fallback or href
    return fallback


def _parse_when(raw: str | None) -> datetime | None:
    """Parse an RFC-822 (RSS) or ISO-8601 (Atom) timestamp to aware UTC, or ``None``."""
    if not raw:
        return None
    text = raw.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_items(label: str, content: bytes | str) -> list[dict[str, Any]]:
    """Parse one RSS 2.0 / Atom document into raw item dicts (may raise on bad XML)."""
    root = ET.fromstring(content)
    items: list[dict[str, Any]] = []
    for elem in root.iter():
        if _local_tag(elem.tag) not in ("item", "entry"):
            continue
        title = _ascii(_strip_html(_child_text(elem, "title")))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "url": _item_url(elem),
                "source": _ascii(_child_text(elem, "source")) or label,
                "published": _parse_when(
                    _child_text(elem, "pubdate", "published", "updated", "date")
                ),
            }
        )
    return items


@register_provider
class NewsRSSProvider(Provider):
    """Keyless crypto news aggregator over publisher + Google News RSS."""

    name = _PROVIDER_NAME
    supported = {"news_feed"}
    rate_per_min = 20  # three feed GETs per scan; ~1/min/feed politeness budget
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` (``news_feed``)."""
        self.ensure_supported(capability)
        return await self._fetch_news(**params)

    # -- Capability handler -----------------------------------------------------

    async def _fetch_feed(
        self, client: Any, url: str, params: dict[str, Any] | None
    ) -> bytes:
        """GET one feed and return its raw body (redirects followed — CoinDesk 308s)."""
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.content

    async def _fetch_news(
        self, symbol: str = "CRYPTO", limit: int = 30, **_: Any
    ) -> dict[str, Any]:
        """Merge the publisher feeds + a per-coin Google News sweep into one list."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        base = _resolve_base(symbol)
        n = max(1, min(_to_int(limit) or 30, 100))
        feeds: list[tuple[str, str, dict[str, Any] | None]] = [
            (label, url, None) for label, url in _FEEDS
        ]
        feeds.append(
            (
                "google_news",
                _GOOGLE_NEWS_URL,
                {"q": _google_query(base), "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
        )

        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        ) as client:
            bodies = await asyncio.gather(
                *(self._fetch_feed(client, url, params) for _, url, params in feeds),
                return_exceptions=True,
            )

        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        parsed_feeds = 0
        for (label, _url, _params), body in zip(feeds, bodies):
            if isinstance(body, BaseException):
                errors.append(_ascii(f"{label}: {type(body).__name__}: {body}")[:160])
                continue
            try:
                items = _parse_items(label, body)
            except Exception as exc:
                # Bad XML in one feed must never sink the scan.
                errors.append(_ascii(f"{label}: {type(exc).__name__}: {exc}")[:160])
                continue
            parsed_feeds += 1
            collected.extend(items[:_PER_FEED_CAP])
        if not parsed_feeds:
            raise ValueError(f"all RSS feeds failed: {'; '.join(errors) or 'no feeds'}")

        now = datetime.now(timezone.utc)
        epoch_zero = datetime.fromtimestamp(0, tz=timezone.utc)
        collected.sort(key=lambda item: item["published"] or epoch_zero, reverse=True)

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in collected:
            key = item["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            published = item["published"]
            out.append(
                {
                    "title_ascii": item["title"],
                    "source": item["source"],
                    "published_utc": published.isoformat() if published else None,
                    "url": item["url"],
                    "age_minutes": (
                        max(0, int((now - published).total_seconds() // 60))
                        if published
                        else None
                    ),
                }
            )
            if len(out) >= n:
                break
        return {"items": out, "as_of": utcnow_iso()}


__all__ = ["NewsRSSProvider"]
