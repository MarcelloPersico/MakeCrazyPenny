"""Social + news provider tests (DESIGN-SWARM.md, agent W3). Offline via respx.

Covers the two new keyless providers:

  * ``social_pulse`` / ``social_scan`` — Arctic Shift Reddit mirror, StockTwits
    platform-native label counting, 4chan /biz/ mention counting (HTML stripped
    first), CoinGecko trending. Every sub-source independently tolerant
    (``{"_error": ...}`` per key, the scan itself never raises) and ALL text
    ASCII-sanitized at the provider boundary (Unicode-heavy fixtures asserted).
  * ``news_rss`` / ``news_feed`` — stdlib ``xml.etree`` parsing of RSS 2.0 AND
    Atom, redirect following (CoinDesk 308), Google News query construction per
    the design, case-insensitive title dedupe after sanitization, UTC ISO
    ``published_utc`` + ``age_minutes``, per-feed bad-XML tolerance.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from makecrazypenny.core.config import Settings
from makecrazypenny.providers import news_rss as news_mod
from makecrazypenny.providers import social as social_mod
from makecrazypenny.providers.news_rss import NewsRSSProvider
from makecrazypenny.providers.social import SocialPulseProvider

ARCTIC_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
FOURCHAN_URL = "https://a.4cdn.org/biz/catalog.json"
TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
STOCKTWITS_BTC = "https://api.stocktwits.com/api/2/streams/symbol/BTC.X.json"

COINTELEGRAPH_URL = "https://cointelegraph.com/rss"
COINDESK_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"


def _settings() -> Settings:
    return Settings(cache_dir=Path(tempfile.mkdtemp()), l2_cache_enabled=False)


def _assert_ascii(obj: Any) -> None:
    """Every string anywhere in the payload is pure ASCII (CONTRACT.md §2)."""
    if isinstance(obj, str):
        assert all(ord(ch) < 128 for ch in obj), f"non-ASCII leaked: {obj!r}"
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _assert_ascii(key)
            _assert_ascii(value)
    elif isinstance(obj, list):
        for value in obj:
            _assert_ascii(value)


# ===========================================================================
# social_pulse / social_scan
# ===========================================================================


def _trending_json(*symbols: str) -> dict[str, Any]:
    return {"coins": [{"item": {"id": s.lower(), "symbol": s.lower()}} for s in symbols]}


@respx.mock
async def test_social_scan_shape_counting_and_ascii_sanitization() -> None:
    unicode_title = (
        "\U0001f680\U0001f680 $BTC to the møøn — 強気 pump now"
    )
    respx.get(ARCTIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": unicode_title,
                        "created_utc": time.time() - 300,
                        "score": 42,
                        "num_comments": 7,
                        "subreddit": "CryptoCurrency",
                    }
                ]
            },
        )
    )
    respx.get(STOCKTWITS_BTC).mock(
        return_value=httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "created_at": "2026-06-10T12:00:00Z",
                        "entities": {"sentiment": {"basic": "Bullish"}},
                    },
                    {
                        "created_at": "2026-06-10T12:05:00Z",
                        "entities": {"sentiment": {"basic": "Bearish"}},
                    },
                    {"created_at": "2026-06-10T11:55:00Z", "entities": {"sentiment": None}},
                    {"created_at": "2026-06-10T11:50:00Z", "entities": {}},
                ]
            },
        )
    )
    respx.get(FOURCHAN_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "page": 1,
                    "threads": [
                        # HTML stripped before matching; entity unescaped.
                        {"no": 1, "sub": "Why <b>BTC</b> wins", "com": "buy &amp; hold"},
                        {"no": 2, "sub": "stocks general", "com": "SPY only, no coins"},
                        # cashtag + lowercase still counts (word boundary, case-insensitive)
                        {"no": 3, "sub": None, "com": "shorting $btc here"},
                        # joined token must NOT count; href attr is stripped with the tag
                        {"no": 4, "sub": "BTCUSD pair talk", "com": '<a href="btc.html">x</a>'},
                    ],
                }
            ],
        )
    )
    respx.get(TRENDING_URL).mock(
        return_value=httpx.Response(200, json=_trending_json("btc", "sol"))
    )

    provider = SocialPulseProvider(_settings())
    result = await provider.fetch("social_scan", symbol="BTC/USDT", limit=10)

    assert result["symbol"] == "BTC"
    # One post per subreddit (catch-all route): base subs + the coin-specific sub.
    n_subs = len(social_mod._BASE_SUBREDDITS) + 1  # + r/Bitcoin for BTC
    reddit = result["reddit"]
    assert len(reddit["posts"]) == n_subs
    assert reddit["post_velocity_per_hr"] == float(n_subs)
    assert reddit["prev_velocity_per_hr"] == 0.0
    post = reddit["posts"][0]
    # ASCII-sanitized: emoji/CJK/em-dash dropped, NBSP normalized, cashtag kept.
    assert post["title_ascii"] == "$BTC to the mn pump now"
    assert "  " not in post["title_ascii"]
    assert post["score"] == 42 and post["num_comments"] == 7
    assert post["created_utc"].endswith("+00:00")
    assert isinstance(post["age_minutes"], int) and 4 <= post["age_minutes"] <= 6

    st = result["stocktwits"]
    assert st == {
        "bullish": 1,
        "bearish": 1,
        "neutral": 2,
        "n_messages": 4,
        "newest_ts": "2026-06-10T12:05:00+00:00",
    }

    assert result["fourchan_biz"] == {"thread_mentions": 2, "total_threads": 4}

    trending = result["trending"]
    assert trending["coins"][0] == {"id": "btc", "symbol": "BTC", "rank": 1}
    assert trending["symbol_trending"] is True

    assert result["as_of"]
    _assert_ascii(result)


@respx.mock
async def test_social_scan_velocity_windows_and_market_wide_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Freeze the provider clock: ages/velocity windows must not drift with the
    # wall clock between fixture construction and the provider's own "now".
    now = 1_780_000_000.0
    monkeypatch.setattr("makecrazypenny.providers.social._now_s", lambda: now)
    posts = [
        {"title": "fresh 1", "created_utc": now - 120, "score": 1, "num_comments": 0},
        {"title": "fresh 2", "created_utc": now - 1800, "score": 2, "num_comments": 0},
        {"title": "prev hour", "created_utc": now - 4500, "score": 3, "num_comments": 0},
        {"title": "ancient", "created_utc": now - 90000, "score": 4, "num_comments": 0},
    ]
    # Only r/CryptoCurrency has posts; the other subs return empty pages.
    crypto_route = respx.get(ARCTIC_URL, params={"subreddit": "CryptoCurrency"}).mock(
        return_value=httpx.Response(200, json={"data": posts})
    )
    respx.get(ARCTIC_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    st_route = respx.get(STOCKTWITS_BTC).mock(  # CRYPTO scan proxies BTC.X
        return_value=httpx.Response(200, json={"messages": []})
    )
    respx.get(FOURCHAN_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "page": 1,
                    "threads": [
                        {"no": 1, "sub": "crypto winter over?", "com": ""},
                        {"no": 2, "sub": "boomer rocks (gold)", "com": "silver too"},
                    ],
                }
            ],
        )
    )
    respx.get(TRENDING_URL).mock(return_value=httpx.Response(200, json=_trending_json("sol")))

    provider = SocialPulseProvider(_settings())
    result = await provider.fetch("social_scan")  # defaults: symbol="CRYPTO"

    assert result["symbol"] == "CRYPTO"
    reddit = result["reddit"]
    assert reddit["post_velocity_per_hr"] == 2.0
    assert reddit["prev_velocity_per_hr"] == 1.0
    # Sorted newest-first; created_utc converted to UTC ISO.
    assert [p["title_ascii"] for p in reddit["posts"][:2]] == ["fresh 1", "fresh 2"]
    assert reddit["posts"][0]["age_minutes"] == 2
    # A market-wide scan sends NO query filter to the mirror.
    sent = crypto_route.calls[0].request.url.params
    assert "query" not in sent
    assert sent["sort"] == "desc"
    assert st_route.called
    # Generic mention terms for CRYPTO ("crypto"/"bitcoin"/"BTC"): thread 1 only.
    assert result["fourchan_biz"] == {"thread_mentions": 1, "total_threads": 2}
    assert result["trending"]["symbol_trending"] is False


@respx.mock
async def test_social_scan_symbol_routes_coin_stream_subreddit_and_query() -> None:
    arctic_route = respx.get(ARCTIC_URL).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    sol_route = respx.get(
        "https://api.stocktwits.com/api/2/streams/symbol/SOL.X.json"
    ).mock(return_value=httpx.Response(200, json={"messages": []}))
    respx.get(FOURCHAN_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(TRENDING_URL).mock(return_value=httpx.Response(200, json=_trending_json()))

    provider = SocialPulseProvider(_settings())
    result = await provider.fetch("social_scan", symbol="SOLUSDT")

    assert result["symbol"] == "SOL"
    assert sol_route.called
    queried = {c.request.url.params["subreddit"] for c in arctic_route.calls}
    assert "solana" in queried  # coin-specific subreddit appended
    for call in arctic_route.calls:
        params = call.request.url.params
        if params["subreddit"] in social_mod._BASE_SUBREDDITS:
            assert params["query"] == "SOL"  # generic subs are symbol-filtered
        else:
            assert "query" not in params  # the coin sub is already on-topic


@respx.mock
async def test_social_scan_subsource_failures_are_isolated_per_key() -> None:
    respx.get(ARCTIC_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(STOCKTWITS_BTC).mock(return_value=httpx.Response(500))
    respx.get(FOURCHAN_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(TRENDING_URL).mock(
        return_value=httpx.Response(429, json={"status": "throttled"})
    )

    result = await SocialPulseProvider(_settings()).fetch("social_scan", symbol="CRYPTO")

    assert "_error" in result["stocktwits"]
    assert "_error" in result["trending"]
    # Healthy keys are unaffected by the failing ones.
    assert result["reddit"]["post_velocity_per_hr"] == 0.0
    assert result["fourchan_biz"] == {"thread_mentions": 0, "total_threads": 0}
    _assert_ascii(result)


@respx.mock
async def test_social_scan_reddit_total_failure_degrades_only_reddit_key() -> None:
    respx.get(ARCTIC_URL).mock(return_value=httpx.Response(503))
    respx.get(STOCKTWITS_BTC).mock(return_value=httpx.Response(200, json={"messages": []}))
    respx.get(FOURCHAN_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(TRENDING_URL).mock(return_value=httpx.Response(200, json=_trending_json()))

    result = await SocialPulseProvider(_settings()).fetch("social_scan")

    assert "_error" in result["reddit"]
    assert result["stocktwits"]["n_messages"] == 0
    assert result["fourchan_biz"]["total_threads"] == 0
    _assert_ascii(result)


# ===========================================================================
# news_rss / news_feed
# ===========================================================================


def _rss(items_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>'
        f"{items_xml}</channel></rss>"
    )


def _rss_item(title: str, url: str, when: datetime | str, source: str | None = None) -> str:
    pub = format_datetime(when) if isinstance(when, datetime) else when
    src = f'<source url="https://pub.example">{source}</source>' if source else ""
    return (
        f"<item><title><![CDATA[{title}]]></title><link>{url}</link>"
        f"<pubDate>{pub}</pubDate>{src}</item>"
    )


def _atom(entries_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>CD</title>'
        f"{entries_xml}</feed>"
    )


def _atom_entry(title: str, url: str, when: datetime) -> str:
    return (
        f'<entry><title>{title}</title><link rel="alternate" href="{url}"/>'
        f"<published>{when.isoformat()}</published></entry>"
    )


@respx.mock
async def test_news_feed_merges_rss_atom_google_follows_redirect_and_dedupes() -> None:
    now = datetime.now(timezone.utc)
    ct = _rss(
        _rss_item(
            "ETH crash to $1K looms — déjà vu?",
            "https://cointelegraph.com/a/1",
            now - timedelta(minutes=30),
        )
        + _rss_item("Shared Headline", "https://cointelegraph.com/a/2", now - timedelta(minutes=90))
    )
    respx.get(COINTELEGRAPH_URL).mock(return_value=httpx.Response(200, text=ct))
    # CoinDesk 308-redirects its feed; the provider client must follow it.
    respx.get(COINDESK_URL).mock(
        return_value=httpx.Response(
            308, headers={"location": "https://www.coindesk.com/feed-real"}
        )
    )
    respx.get("https://www.coindesk.com/feed-real").mock(
        return_value=httpx.Response(
            200,
            text=_atom(
                _atom_entry(
                    "BTC ETF inflows surge", "https://coindesk.com/a/1", now - timedelta(minutes=5)
                )
            ),
        )
    )
    google = _rss(
        _rss_item(
            "SHARED HEADLINE",  # dedupes case-insensitively against cointelegraph's
            "https://news.google.com/rss/articles/x",
            now - timedelta(minutes=10),
            source="MarketWatch",
        )
    )
    google_route = respx.get(GOOGLE_NEWS_URL).mock(
        return_value=httpx.Response(200, text=google)
    )

    result = await NewsRSSProvider(_settings()).fetch("news_feed", symbol="BTC", limit=30)

    items = result["items"]
    titles = [i["title_ascii"] for i in items]
    # Newest first; the duplicate title survives only once (its newest copy).
    assert titles == [
        "BTC ETF inflows surge",
        "SHARED HEADLINE",
        "ETH crash to $1K looms dj vu?",
    ]
    assert items[0]["source"] == "coindesk"
    assert items[0]["url"] == "https://coindesk.com/a/1"  # Atom href link
    assert items[1]["source"] == "MarketWatch"  # Google item credits its <source>
    assert items[2]["source"] == "cointelegraph"
    assert 4 <= items[0]["age_minutes"] <= 6
    assert 29 <= items[2]["age_minutes"] <= 31
    published = datetime.fromisoformat(items[0]["published_utc"])
    assert published.utcoffset() == timedelta(0)
    # The Google query follows the design: "<coin name> OR <symbol> crypto".
    assert google_route.calls[0].request.url.params["q"] == "Bitcoin OR BTC crypto"
    assert result["as_of"]
    _assert_ascii(result)


@respx.mock
async def test_news_feed_tolerates_bad_xml_and_bad_dates_per_feed() -> None:
    now = datetime.now(timezone.utc)
    respx.get(COINTELEGRAPH_URL).mock(
        return_value=httpx.Response(200, text="<rss><channel><item>broken")
    )
    cd = _rss(
        _rss_item("Only survivor", "https://coindesk.com/a/1", now - timedelta(minutes=3))
        + _rss_item("Undated story", "https://coindesk.com/a/2", "not a date")
    )
    respx.get(COINDESK_URL).mock(return_value=httpx.Response(200, text=cd))
    respx.get(GOOGLE_NEWS_URL).mock(return_value=httpx.Response(404))

    result = await NewsRSSProvider(_settings()).fetch("news_feed")

    titles = [i["title_ascii"] for i in result["items"]]
    assert titles == ["Only survivor", "Undated story"]  # undated sorts last
    assert result["items"][1]["published_utc"] is None
    assert result["items"][1]["age_minutes"] is None


@respx.mock
async def test_news_feed_market_wide_query_and_limit() -> None:
    now = datetime.now(timezone.utc)
    respx.get(COINTELEGRAPH_URL).mock(return_value=httpx.Response(200, text=_rss("")))
    respx.get(COINDESK_URL).mock(return_value=httpx.Response(200, text=_rss("")))
    items_xml = "".join(
        _rss_item(f"Story {i}", f"https://g/{i}", now - timedelta(minutes=i)) for i in range(5)
    )
    google_route = respx.get(GOOGLE_NEWS_URL).mock(
        return_value=httpx.Response(200, text=_rss(items_xml))
    )

    result = await NewsRSSProvider(_settings()).fetch("news_feed", limit=3)

    assert google_route.calls[0].request.url.params["q"] == "crypto OR bitcoin"
    assert len(result["items"]) == 3
    assert [i["title_ascii"] for i in result["items"]] == ["Story 0", "Story 1", "Story 2"]


@respx.mock
async def test_news_feed_raises_only_when_every_feed_fails() -> None:
    respx.get(COINTELEGRAPH_URL).mock(return_value=httpx.Response(500))
    respx.get(COINDESK_URL).mock(return_value=httpx.Response(502))
    respx.get(GOOGLE_NEWS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ValueError, match="all RSS feeds failed"):
        await NewsRSSProvider(_settings()).fetch("news_feed", symbol="BTC")


def test_google_query_falls_back_to_symbol_for_unknown_coins() -> None:
    assert news_mod._google_query("ZZZCOIN") == "ZZZCOIN crypto"
    assert news_mod._google_query(None) == "crypto OR bitcoin"
    assert news_mod._google_query("ETH") == "Ethereum OR ETH crypto"
