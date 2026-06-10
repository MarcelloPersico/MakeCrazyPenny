"""Social-pulse provider: deterministic crowd-chatter metrics (DESIGN-SWARM.md).

Keyless aggregation of four independent social feeds into one ``social_scan``
payload of **deterministic, countable** metrics — Reddit post velocity,
platform-native bullish/bearish tallies, /biz/ mention counts, and trending
membership. No model and no interpretation happens here (the engine stays
AI-free per DESIGN-SWARM hard constraint 1); the numbers are safe to score as
factors and the sanitized titles ride along for host-side reading.

Sources (all verified keyless from this host, 2026-06-10):

  * Reddit via the **Arctic Shift** mirror (``arctic-shift.photon-reddit.com``)
    — ``reddit.com`` itself hard-403s this machine's datacenter egress IP, so
    the mirror is the canonical path. Subreddits: CryptoCurrency,
    CryptoMarkets, SatoshiStreetBets, Hyperliquid, plus a coin-specific
    subreddit and a ``query=<base>`` filter when a symbol is given.
  * StockTwits symbol streams (``{BASE}.X.json``) — messages carry user-tagged
    ``entities.sentiment.basic`` Bullish/Bearish labels, so polarity is a plain
    count, never a model. ``BTC.X`` proxies a market-wide (``CRYPTO``) scan.
  * 4chan ``/biz/`` catalog — mention counting only across thread subjects and
    comments (HTML stripped first); polarity is deliberately NOT inferred.
  * CoinGecko ``/search/trending`` — retail-attention proxy. The shared
    keyless pool is ~5 req/min measured, which caps this provider's bucket.

Every sub-source is independently tolerant: a failure surfaces as an
``{"_error": ...}`` marker under that key and never sinks the whole scan. ALL
text crossing this boundary is ASCII-sanitized (strip non-ASCII, collapse
whitespace; ``$TICKER`` cashtags survive) per the strict-ASCII output rule.

Capability: ``social_scan``. No API key required (an optional CoinGecko demo
key is attached when configured). ``httpx`` is imported lazily so importing
this module never hits the network.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.symbols import base_asset
from ..core.types import utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "social_pulse"

_ARCTIC_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
_STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol"
_FOURCHAN_URL = "https://a.4cdn.org/biz/catalog.json"
_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

#: Descriptive UA — the Arctic Shift mirror is volunteer-run; identify politely.
_USER_AGENT = "MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"

#: Subreddits scanned on every call (DESIGN-SWARM.md, providers/social.py).
_BASE_SUBREDDITS: tuple[str, ...] = (
    "CryptoCurrency",
    "CryptoMarkets",
    "SatoshiStreetBets",
    "Hyperliquid",
)

#: Coin-specific subreddit appended when a symbol is given (best-effort map).
_COIN_SUBREDDITS: dict[str, str] = {
    "AAVE": "Aave", "ADA": "cardano", "ATOM": "cosmosnetwork", "AVAX": "Avax",
    "BTC": "Bitcoin", "DOGE": "dogecoin", "DOT": "Polkadot", "ETH": "ethtrader",
    "HYPE": "Hyperliquid", "LINK": "Chainlink", "LTC": "litecoin",
    "NEAR": "nearprotocol", "PEPE": "pepecoin", "SHIB": "SHIBArmy",
    "SOL": "solana", "TON": "TONcoin", "UNI": "UniSwap", "XRP": "XRP",
}

#: Human coin names for /biz/ mention matching (mirrors news_rss._COIN_NAMES).
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

#: Posts pulled per subreddit (Arctic Shift max page; covers the 2h windows).
_POSTS_PER_SUB = 100
#: Velocity window width in seconds (posts/hr over the last vs previous hour).
_HOUR_S = 3600.0


def _now_s() -> float:
    """Current UTC epoch seconds (single clock seam; monkeypatched in tests)."""
    return datetime.now(timezone.utc).timestamp()

#: Chrome-ordered TLS cipher list. StockTwits' CDN fingerprints TLS handshakes
#: and 403-blocks Python's default OpenSSL ordering (verified live 2026-06-10:
#: default context -> 403 HTML block page regardless of User-Agent; this
#: reordered context -> 200). Harmless to the other hosts in this scan.
_TLS_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384"
)

_tls_context_cache: Any = None


def _tls_context() -> Any:
    """Build (once) the cipher-reordered SSL context used by the scan client.

    Created lazily so importing this module never touches the CA store; any
    failure to apply the cipher list degrades to the default context (the rest
    of the sub-sources work either way).
    """
    global _tls_context_cache
    if _tls_context_cache is None:
        import ssl  # lazy: create_default_context reads the CA bundle from disk

        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers(_TLS_CIPHERS)
        except ssl.SSLError:
            pass
        ctx.options |= ssl.OP_NO_TICKET
        _tls_context_cache = ctx
    return _tls_context_cache

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
    """Drop HTML tags and unescape entities (4chan ``sub``/``com`` carry markup)."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", str(text)))


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int:
    """Best-effort int coercion; ``0`` on missing/invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _epoch_to_iso(epoch: float | None) -> str | None:
    """Convert an epoch-seconds value to an ISO-8601 UTC string."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_utc(raw: Any) -> str | None:
    """Normalize an ISO-ish timestamp string to UTC ISO-8601, or ``None``."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _resolve_base(symbol: Any) -> str | None:
    """Map the request symbol to a base coin, or ``None`` for a market-wide scan."""
    s = str(symbol or "").strip().upper()
    if not s or s == "CRYPTO":
        return None
    return base_asset(s)


def _mention_terms(base: str | None) -> list[str]:
    """Terms counted on /biz/: the base symbol + coin name, or generics."""
    if not base:
        return ["crypto", "bitcoin", "BTC"]
    terms = [base]
    name = _COIN_NAMES.get(base)
    if name and name.upper() != base:
        terms.append(name)
    return terms


@register_provider
class SocialPulseProvider(Provider):
    """Deterministic social-chatter scanner (keyless)."""

    name = _PROVIDER_NAME
    supported = {"social_scan"}
    # The CoinGecko shared keyless pool (~5 req/min measured live) is the
    # tightest sub-source, so the whole scan inherits that budget.
    rate_per_min = 5
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME

    # -- Dispatch -------------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` (``social_scan``)."""
        self.ensure_supported(capability)
        return await self._fetch_scan(**params)

    # -- Capability handler -----------------------------------------------------

    async def _fetch_scan(
        self, symbol: str = "CRYPTO", limit: int = 25, **_: Any
    ) -> dict[str, Any]:
        """Run all four sub-source scans concurrently, each independently tolerant."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        base = _resolve_base(symbol)
        n = max(1, min(_to_int(limit) or 25, 100))
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
            verify=_tls_context(),
        ) as client:
            reddit, stocktwits, fourchan, trending = await asyncio.gather(
                self._tolerant(self._scan_reddit(client, base, n)),
                self._tolerant(self._scan_stocktwits(client, base)),
                self._tolerant(self._scan_fourchan(client, base)),
                self._tolerant(self._scan_trending(client, base)),
            )
        return {
            "symbol": base or "CRYPTO",
            "reddit": reddit,
            "stocktwits": stocktwits,
            "fourchan_biz": fourchan,
            "trending": trending,
            "as_of": utcnow_iso(),
        }

    @staticmethod
    async def _tolerant(task: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Degrade a failed sub-source to an ``{"_error": ...}`` marker, never raise."""
        try:
            return await task
        except Exception as exc:
            msg = _ascii(f"{type(exc).__name__}: {exc}")[:300]
            return {"_error": msg or type(exc).__name__}

    # -- Sub-sources ------------------------------------------------------------

    async def _fetch_subreddit(
        self, client: Any, sub: str, query: str | None
    ) -> list[dict[str, Any]]:
        """Fetch one subreddit's newest posts from the Arctic Shift mirror."""
        params: dict[str, Any] = {"subreddit": sub, "limit": _POSTS_PER_SUB, "sort": "desc"}
        if query:
            params["query"] = query
        response = await client.get(_ARCTIC_URL, params=params)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def _scan_reddit(self, client: Any, base: str | None, limit: int) -> dict[str, Any]:
        """Aggregate posts + per-hour velocity across the watched subreddits.

        A symbol-scoped scan adds the coin's own subreddit (when known) and
        filters the generic subs with ``query=<base>`` so velocity counts
        symbol chatter, not all of Reddit. Per-subreddit failures are tolerated;
        only an all-subreddits failure degrades the ``reddit`` key.
        """
        subs = list(_BASE_SUBREDDITS)
        if base:
            coin_sub = _COIN_SUBREDDITS.get(base)
            if coin_sub and coin_sub not in subs:
                subs.append(coin_sub)
        results = await asyncio.gather(
            *(
                self._fetch_subreddit(client, sub, base if sub in _BASE_SUBREDDITS else None)
                for sub in subs
            ),
            return_exceptions=True,
        )

        rows: list[tuple[str, dict[str, Any]]] = []
        errors: list[str] = []
        for sub, res in zip(subs, results):
            if isinstance(res, BaseException):
                errors.append(_ascii(f"{sub}: {type(res).__name__}: {res}")[:160])
                continue
            rows.extend((sub, row) for row in res)
        if errors and len(errors) == len(subs):
            return {"_error": "; ".join(errors)[:300]}

        now_s = _now_s()
        velocity = prev_velocity = 0
        posts: list[dict[str, Any]] = []
        for sub, row in rows:
            created = _to_float(row.get("created_utc"))
            if created is not None:
                age_s = now_s - created
                if age_s < _HOUR_S:
                    velocity += 1
                elif age_s < 2 * _HOUR_S:
                    prev_velocity += 1
            posts.append(
                {
                    "title_ascii": _ascii(row.get("title")),
                    "created_utc": _epoch_to_iso(created),
                    "age_minutes": (
                        int(max(0.0, now_s - created) // 60) if created is not None else None
                    ),
                    "score": _to_int(row.get("score")),
                    "num_comments": _to_int(row.get("num_comments")),
                    "subreddit": _ascii(row.get("subreddit") or sub),
                }
            )
        posts.sort(key=lambda p: p["created_utc"] or "", reverse=True)
        return {
            "posts": posts[:limit],
            "post_velocity_per_hr": float(velocity),
            "prev_velocity_per_hr": float(prev_velocity),
        }

    async def _scan_stocktwits(self, client: Any, base: str | None) -> dict[str, Any]:
        """Count platform-native Bullish/Bearish labels on the ``{BASE}.X`` stream.

        Sentiment is the user's own tag (``entities.sentiment.basic``) — a
        deterministic count, no model. Untagged messages count as neutral.
        ``BTC.X`` serves as the market proxy for a ``CRYPTO`` (no-symbol) scan.
        """
        sym = f"{base or 'BTC'}.X"
        response = await client.get(f"{_STOCKTWITS_URL}/{sym}.json")
        response.raise_for_status()
        body = response.json()
        messages = body.get("messages") if isinstance(body, dict) else None
        messages = messages if isinstance(messages, list) else []
        bullish = bearish = neutral = 0
        newest: str | None = None
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            entities = msg.get("entities") if isinstance(msg.get("entities"), dict) else {}
            sentiment = (
                entities.get("sentiment") if isinstance(entities.get("sentiment"), dict) else {}
            )
            label = sentiment.get("basic")
            if label == "Bullish":
                bullish += 1
            elif label == "Bearish":
                bearish += 1
            else:
                neutral += 1
            ts = _iso_utc(msg.get("created_at"))
            if ts and (newest is None or ts > newest):
                newest = ts
        return {
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "n_messages": bullish + bearish + neutral,
            "newest_ts": newest,
        }

    async def _scan_fourchan(self, client: Any, base: str | None) -> dict[str, Any]:
        """Count /biz/ catalog threads mentioning the coin (subjects + comments).

        HTML is stripped before matching so markup/attributes never count as
        mentions; terms match on word boundaries case-insensitively (``$BTC``
        and ``btc`` count, ``BTCUSD`` does not). Mention counting ONLY — /biz/
        polarity is unusable by design.
        """
        response = await client.get(_FOURCHAN_URL)
        response.raise_for_status()
        pages = response.json()
        patterns = [
            re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
            for term in _mention_terms(base)
        ]
        total = mentions = 0
        for page in pages if isinstance(pages, list) else []:
            threads = page.get("threads") if isinstance(page, dict) else None
            for thread in threads if isinstance(threads, list) else []:
                if not isinstance(thread, dict):
                    continue
                total += 1
                text = f"{_strip_html(thread.get('sub'))} {_strip_html(thread.get('com'))}"
                if any(p.search(text) for p in patterns):
                    mentions += 1
        return {"thread_mentions": mentions, "total_threads": total}

    async def _scan_trending(self, client: Any, base: str | None) -> dict[str, Any]:
        """Normalize CoinGecko ``/search/trending`` (attaching the demo key if set)."""
        headers: dict[str, str] = {}
        key = self.settings.coingecko_api_key
        if key:
            headers["x-cg-demo-api-key"] = key
        response = await client.get(_TRENDING_URL, headers=headers)
        response.raise_for_status()
        body = response.json()
        raw = body.get("coins") if isinstance(body, dict) else None
        coins: list[dict[str, Any]] = []
        for rank, entry in enumerate(raw if isinstance(raw, list) else [], start=1):
            item = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                continue
            coins.append(
                {
                    "id": _ascii(item.get("id")),
                    "symbol": _ascii(item.get("symbol")).upper(),
                    "rank": rank,
                }
            )
        trending = bool(base) and any(c["symbol"] == base for c in coins)
        return {"coins": coins, "symbol_trending": trending}


__all__ = ["SocialPulseProvider"]
