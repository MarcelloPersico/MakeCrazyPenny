"""Provider-layer tests (CONTRACT.md §8.7, §11).

Deterministic and fully offline:

  * httpx-backed providers (alpha_vantage, finnhub, fmp, edgar, stockwatcher,
    marketaux) are exercised through ``respx`` route stubs — no real network.
  * ``yfinance`` is exercised by monkeypatching the lazily-imported ``yfinance``
    module with a fake ``Ticker`` so no network or heavy lib is required.

Each provider is asserted to:
  * normalize the upstream payload into the matching ``core.types`` dataclass'
    ``to_dict()`` shape (or a list thereof / a plain fundamentals dict), and
  * raise ``MissingApiKey`` when a required key is absent (keyed providers only).

Built against the ACTUAL provider signatures — every assertion mirrors the
implementation modules, not an invented API.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest
import respx

from makecrazypenny.core.config import Settings
from makecrazypenny.core.errors import MissingApiKey
from makecrazypenny.providers.alpha_vantage import AlphaVantageProvider
from makecrazypenny.providers.edgar import EdgarProvider
from makecrazypenny.providers.finnhub import FinnhubProvider
from makecrazypenny.providers.fmp import FMPProvider
from makecrazypenny.providers.marketaux import MarketauxProvider
from makecrazypenny.providers.stockwatcher import (
    HOUSE_FEED_URL,
    SENATE_FEED_URL,
    StockWatcherProvider,
)
from makecrazypenny.providers.yfinance_provider import YFinanceProvider


# ---------------------------------------------------------------------------
# Settings helpers — build offline Settings with / without specific keys.
# ---------------------------------------------------------------------------


def _settings(**keys: str) -> Settings:
    """Build a default ``Settings`` (no env read) with optional API keys set.

    Using the dataclass constructor directly (NOT ``from_env``) keeps the test
    independent of the host environment and any ``.env`` file.
    """
    return Settings(
        alpha_vantage_api_key=keys.get("alpha_vantage"),
        finnhub_api_key=keys.get("finnhub"),
        fmp_api_key=keys.get("fmp"),
        marketaux_api_key=keys.get("marketaux"),
    )


# ===========================================================================
# Alpha Vantage
# ===========================================================================


@respx.mock
async def test_alpha_vantage_ohlcv_normalizes_to_ohlcv():
    provider = AlphaVantageProvider(_settings(alpha_vantage="AVKEY"))
    route = respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "Meta Data": {"2. Symbol": "AAPL"},
                "Time Series (Daily)": {
                    # newest-first in the raw payload; provider sorts ascending.
                    "2024-01-02": {
                        "1. open": "100.0",
                        "2. high": "110.0",
                        "3. low": "99.0",
                        "4. close": "105.0",
                        "5. volume": "1000",
                    },
                    "2024-01-01": {
                        "1. open": "90.0",
                        "2. high": "95.0",
                        "3. low": "88.0",
                        "4. close": "92.0",
                        "5. volume": "500",
                    },
                },
            },
        )
    )

    result = await provider.fetch("ohlcv", symbol="aapl", interval="1d")

    assert route.called
    assert result["symbol"] == "AAPL"
    assert result["interval"] == "1d"
    assert result["provenance"]["provider"] == "alpha_vantage"
    assert result["provenance"]["cached"] is False
    # Sorted ascending by timestamp.
    assert [b["ts"] for b in result["bars"]] == ["2024-01-01", "2024-01-02"]
    first = result["bars"][0]
    assert first["open"] == 90.0
    assert first["close"] == 92.0
    assert first["volume"] == 500.0


@respx.mock
async def test_alpha_vantage_quote_normalizes_to_quote():
    provider = AlphaVantageProvider(_settings(alpha_vantage="AVKEY"))
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "Global Quote": {
                    "01. symbol": "AAPL",
                    "05. price": "150.25",
                    "09. change": "1.50",
                    "10. change percent": "1.0084%",
                }
            },
        )
    )

    result = await provider.fetch("quote", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["price"] == 150.25
    assert result["change"] == 1.50
    assert result["change_pct"] == pytest.approx(1.0084)
    assert result["provenance"]["provider"] == "alpha_vantage"


@respx.mock
async def test_alpha_vantage_news_sentiment_aggregates_to_sentiment_score():
    provider = AlphaVantageProvider(_settings(alpha_vantage="AVKEY"))
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "feed": [
                    {
                        "title": "Big bullish news",
                        "ticker_sentiment": [
                            {"ticker": "AAPL", "ticker_sentiment_score": "0.5"},
                        ],
                    },
                    {
                        "title": "More upside",
                        "ticker_sentiment": [
                            {"ticker": "AAPL", "ticker_sentiment_score": "0.7"},
                        ],
                    },
                ]
            },
        )
    )

    result = await provider.fetch("news_sentiment", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    # Mean of 0.5 and 0.7 = 0.6 -> "Bullish" per the AV label scale.
    assert result["score"] == pytest.approx(0.6)
    assert result["label"] == "Bullish"
    assert result["n_articles"] == 2
    assert "Big bullish news" in result["drivers"]


@respx.mock
async def test_alpha_vantage_fundamentals_returns_overview_with_provenance():
    provider = AlphaVantageProvider(_settings(alpha_vantage="AVKEY"))
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={"Symbol": "AAPL", "Name": "Apple Inc", "PERatio": "30.0"},
        )
    )

    result = await provider.fetch("fundamentals", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["Name"] == "Apple Inc"
    assert result["provenance"]["provider"] == "alpha_vantage"


async def test_alpha_vantage_missing_key_raises():
    provider = AlphaVantageProvider(_settings())  # no AV key
    with pytest.raises(MissingApiKey) as exc:
        await provider.fetch("quote", symbol="AAPL")
    assert exc.value.provider == "alpha_vantage"
    assert exc.value.env_var == "ALPHA_VANTAGE_API_KEY"


async def test_alpha_vantage_unsupported_capability_raises_not_implemented():
    provider = AlphaVantageProvider(_settings(alpha_vantage="AVKEY"))
    with pytest.raises(NotImplementedError):
        await provider.fetch("congress_trades", symbol="AAPL")


# ===========================================================================
# Finnhub
# ===========================================================================


@respx.mock
async def test_finnhub_quote_normalizes_to_quote():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/quote").mock(
        return_value=httpx.Response(
            200, json={"c": 150.0, "d": 2.5, "dp": 1.69}
        )
    )

    result = await provider.fetch("quote", symbol="aapl")

    assert result["symbol"] == "AAPL"
    assert result["price"] == 150.0
    assert result["change"] == 2.5
    assert result["change_pct"] == 1.69
    assert result["provenance"]["provider"] == "finnhub"
    assert result["provenance"]["cached"] is False


@respx.mock
async def test_finnhub_ohlcv_normalizes_candles():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/candle").mock(
        return_value=httpx.Response(
            200,
            json={
                "s": "ok",
                "o": [10.0, 11.0],
                "h": [12.0, 13.0],
                "l": [9.0, 10.0],
                "c": [11.0, 12.0],
                "v": [1000, 2000],
                "t": [1704067200, 1704153600],
            },
        )
    )

    result = await provider.fetch("ohlcv", symbol="AAPL", interval="1d")

    assert result["symbol"] == "AAPL"
    assert len(result["bars"]) == 2
    assert result["bars"][0]["open"] == 10.0
    assert result["bars"][1]["close"] == 12.0
    # epoch -> ISO string conversion.
    assert result["bars"][0]["ts"].startswith("2024-01-01")


@respx.mock
async def test_finnhub_company_news_returns_newsitem_list():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/company-news").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "headline": "Apple soars",
                    "source": "Reuters",
                    "url": "https://example.com/a",
                    "datetime": 1704067200,
                    "summary": "Up big.",
                }
            ],
        )
    )

    result = await provider.fetch("company_news", symbol="AAPL", days=7)

    assert isinstance(result, list)
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["headline"] == "Apple soars"
    assert result[0]["source"] == "Reuters"
    assert result[0]["published_at"].startswith("2024-01-01")


@respx.mock
async def test_finnhub_news_sentiment_maps_to_score():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/news-sentiment").mock(
        return_value=httpx.Response(
            200,
            json={
                "sentiment": {"bullishPercent": 0.8, "bearishPercent": 0.2},
                "buzz": {"articlesInLastWeek": 42},
                "companyNewsScore": 0.75,
                "sectorAverageBullishPercent": 0.6,
            },
        )
    )

    result = await provider.fetch("news_sentiment", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    # bullish - bearish = 0.6.
    assert result["score"] == pytest.approx(0.6)
    assert result["label"] == "bullish"
    assert result["n_articles"] == 42


@respx.mock
async def test_finnhub_social_sentiment_averages_sources():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/social-sentiment").mock(
        return_value=httpx.Response(
            200,
            json={
                "reddit": [{"score": 0.4, "mention": 10}],
                "twitter": [{"score": 0.6, "mention": 5}],
            },
        )
    )

    result = await provider.fetch("social_sentiment", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["score"] == pytest.approx(0.5)  # mean(0.4, 0.6)
    assert result["label"] == "bullish"
    assert result["n_articles"] == 15  # 10 + 5 mentions
    assert set(result["drivers"]) == {"reddit", "twitter"}


@respx.mock
async def test_finnhub_congress_trades_returns_list():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/congressional-trading").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "Jane Doe",
                        "chamber": "Senate",
                        "transactionType": "Purchase",
                        "amountFrom": 1000,
                        "amountTo": 5000,
                        "transactionDate": "2024-01-01",
                        "filingDate": "2024-02-01",
                    }
                ]
            },
        )
    )

    result = await provider.fetch("congress_trades", symbol="AAPL")

    assert isinstance(result, list)
    trade = result[0]
    assert trade["symbol"] == "AAPL"
    assert trade["member"] == "Jane Doe"
    assert trade["chamber"] == "Senate"
    assert trade["transaction"] == "Purchase"
    assert trade["amount_range"] == "1000-5000"
    assert trade["transaction_date"] == "2024-01-01"
    assert trade["disclosure_date"] == "2024-02-01"


@respx.mock
async def test_finnhub_insider_transactions_computes_value():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/insider-transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "CEO Person",
                        "transactionCode": "S",
                        "share": 100,
                        "transactionPrice": 10.0,
                        "transactionDate": "2024-03-01",
                    }
                ]
            },
        )
    )

    result = await provider.fetch("insider_transactions", symbol="AAPL")

    txn = result[0]
    assert txn["symbol"] == "AAPL"
    assert txn["insider"] == "CEO Person"
    assert txn["shares"] == 100.0
    assert txn["value"] == 1000.0  # 100 * 10
    assert txn["date"] == "2024-03-01"


@respx.mock
async def test_finnhub_analyst_ratings_returns_list():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/recommendation").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "period": "2024-01-01",
                    "strongBuy": 5,
                    "buy": 10,
                    "hold": 3,
                    "sell": 1,
                    "strongSell": 0,
                }
            ],
        )
    )

    result = await provider.fetch("analyst_ratings", symbol="AAPL")

    rating = result[0]
    assert rating["symbol"] == "AAPL"
    assert rating["period"] == "2024-01-01"
    assert rating["strong_buy"] == 5
    assert rating["buy"] == 10
    assert rating["hold"] == 3
    assert rating["sell"] == 1
    assert rating["strong_sell"] == 0


@respx.mock
async def test_finnhub_price_targets_normalizes():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/price-target").mock(
        return_value=httpx.Response(
            200,
            json={
                "targetMean": 200.0,
                "targetHigh": 250.0,
                "targetLow": 150.0,
                "lastPrice": 180.0,
            },
        )
    )

    result = await provider.fetch("price_targets", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["mean"] == 200.0
    assert result["high"] == 250.0
    assert result["low"] == 150.0
    assert result["current"] == 180.0


@respx.mock
async def test_finnhub_upgrades_downgrades_returns_list():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    respx.get("https://finnhub.io/api/v1/stock/upgrade-downgrade").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "company": "Big Bank",
                    "fromGrade": "Hold",
                    "toGrade": "Buy",
                    "action": "up",
                    "gradeTime": 1704067200,
                }
            ],
        )
    )

    result = await provider.fetch("upgrades_downgrades", symbol="AAPL")

    event = result[0]
    assert event["symbol"] == "AAPL"
    assert event["firm"] == "Big Bank"
    assert event["from_grade"] == "Hold"
    assert event["to_grade"] == "Buy"
    assert event["action"] == "up"
    assert event["date"].startswith("2024-01-01")


async def test_finnhub_missing_key_raises():
    provider = FinnhubProvider(_settings())  # no finnhub key
    with pytest.raises(MissingApiKey) as exc:
        await provider.fetch("quote", symbol="AAPL")
    assert exc.value.provider == "finnhub"
    assert exc.value.env_var == "FINNHUB_API_KEY"


async def test_finnhub_unsupported_capability_raises():
    provider = FinnhubProvider(_settings(finnhub="FHKEY"))
    with pytest.raises(NotImplementedError):
        await provider.fetch("sec_filings", symbol="AAPL")


# ===========================================================================
# FMP
# ===========================================================================


@respx.mock
async def test_fmp_analyst_ratings_normalizes():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    respx.get(
        "https://financialmodelingprep.com/api/v3/analyst-stock-recommendations/AAPL"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "date": "2024-01-01",
                    "analystRatingsStrongBuy": 7,
                    "analystRatingsbuy": 12,
                    "analystRatingsHold": 4,
                    "analystRatingsSell": 2,
                    "analystRatingsStrongSell": 1,
                }
            ],
        )
    )

    result = await provider.fetch("analyst_ratings", symbol="aapl")

    rating = result[0]
    assert rating["symbol"] == "AAPL"
    assert rating["period"] == "2024-01-01"
    assert rating["strong_buy"] == 7
    assert rating["buy"] == 12
    assert rating["hold"] == 4
    assert rating["sell"] == 2
    assert rating["strong_sell"] == 1
    assert rating["provenance"]["provider"] == "fmp"


@respx.mock
async def test_fmp_price_targets_normalizes_consensus():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    respx.get("https://financialmodelingprep.com/api/v4/price-target-consensus").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "targetConsensus": 210.0,
                    "targetHigh": 260.0,
                    "targetLow": 160.0,
                    "lastPrice": 185.0,
                }
            ],
        )
    )

    result = await provider.fetch("price_targets", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["mean"] == 210.0
    assert result["high"] == 260.0
    assert result["low"] == 160.0
    assert result["current"] == 185.0


@respx.mock
async def test_fmp_upgrades_downgrades_normalizes():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    respx.get("https://financialmodelingprep.com/api/v4/upgrades-downgrades").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "previousGrade": "Hold",
                    "newGrade": "Buy",
                    "gradingCompany": "Morgan Stanley",
                    "action": "upgrade",
                    "publishedDate": "2024-05-01",
                }
            ],
        )
    )

    result = await provider.fetch("upgrades_downgrades", symbol="AAPL")

    event = result[0]
    assert event["symbol"] == "AAPL"
    assert event["from_grade"] == "Hold"
    assert event["to_grade"] == "Buy"
    assert event["firm"] == "Morgan Stanley"
    assert event["action"] == "upgrade"
    assert event["date"] == "2024-05-01"


@respx.mock
async def test_fmp_congress_trades_merges_chambers():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    respx.get("https://financialmodelingprep.com/api/v4/senate-trading").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "firstName": "Jane",
                    "lastName": "Senator",
                    "type": "purchase",
                    "amount": "$1,001 - $15,000",
                    "transactionDate": "2024-01-10",
                    "disclosureDate": "2024-02-10",
                }
            ],
        )
    )
    respx.get("https://financialmodelingprep.com/api/v4/house-trading").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "representative": "John Rep",
                    "type": "sale",
                    "amount": "$1,001 - $15,000",
                    "transactionDate": "2024-01-11",
                    "disclosureDate": "2024-02-11",
                }
            ],
        )
    )

    result = await provider.fetch("congress_trades", symbol="AAPL")

    assert isinstance(result, list)
    assert len(result) == 2
    chambers = {t["chamber"] for t in result}
    assert chambers == {"Senate", "House"}
    senate = next(t for t in result if t["chamber"] == "Senate")
    assert senate["symbol"] == "AAPL"
    assert senate["member"] == "Jane Senator"
    assert senate["transaction"] == "purchase"
    house = next(t for t in result if t["chamber"] == "House")
    assert house["member"] == "John Rep"


@respx.mock
async def test_fmp_fundamentals_merges_metrics_and_ratios():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    respx.get("https://financialmodelingprep.com/api/v3/key-metrics/AAPL").mock(
        return_value=httpx.Response(
            200,
            json=[{"date": "2023-12-31", "period": "FY", "marketCap": 3.0e12}],
        )
    )
    respx.get("https://financialmodelingprep.com/api/v3/ratios/AAPL").mock(
        return_value=httpx.Response(
            200,
            json=[{"date": "2023-12-31", "currentRatio": 1.1}],
        )
    )

    result = await provider.fetch("fundamentals", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["key_metrics"]["marketCap"] == 3.0e12
    assert result["ratios"]["currentRatio"] == 1.1
    assert result["provenance"]["provider"] == "fmp"


async def test_fmp_missing_key_raises():
    provider = FMPProvider(_settings())  # no fmp key
    with pytest.raises(MissingApiKey) as exc:
        await provider.fetch("analyst_ratings", symbol="AAPL")
    assert exc.value.provider == "fmp"
    assert exc.value.env_var == "FMP_API_KEY"


async def test_fmp_unsupported_capability_raises():
    provider = FMPProvider(_settings(fmp="FMPKEY"))
    with pytest.raises(NotImplementedError):
        await provider.fetch("quote", symbol="AAPL")


# ===========================================================================
# Marketaux
# ===========================================================================


@respx.mock
async def test_marketaux_company_news_normalizes_to_newsitem_list():
    provider = MarketauxProvider(_settings(marketaux="MAKEY"))
    respx.get("https://api.marketaux.com/v1/news/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "Apple announces buyback",
                        "description": "A large buyback program.",
                        "url": "https://example.com/news",
                        "source": "MarketWatch",
                        "published_at": "2024-01-01T00:00:00.000000Z",
                        "entities": [{"symbol": "AAPL"}],
                    },
                    {
                        # An article with no title is skipped.
                        "description": "no headline here",
                    },
                ]
            },
        )
    )

    result = await provider.fetch("company_news", symbol="$aapl")

    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    assert item["symbol"] == "AAPL"
    assert item["headline"] == "Apple announces buyback"
    assert item["source"] == "MarketWatch"
    assert item["url"] == "https://example.com/news"
    assert item["summary"] == "A large buyback program."
    assert item["published_at"].startswith("2024-01-01")


async def test_marketaux_missing_key_raises():
    provider = MarketauxProvider(_settings())  # no marketaux key
    with pytest.raises(MissingApiKey) as exc:
        await provider.fetch("company_news", symbol="AAPL")
    assert exc.value.provider == "marketaux"
    assert exc.value.env_var == "MARKETAUX_API_KEY"


async def test_marketaux_unsupported_capability_raises():
    provider = MarketauxProvider(_settings(marketaux="MAKEY"))
    with pytest.raises(NotImplementedError):
        await provider.fetch("quote", symbol="AAPL")


# ===========================================================================
# EDGAR (keyless — never raises MissingApiKey)
# ===========================================================================

_COMPANY_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}


def _edgar_submissions(forms: list[str], accessions: list[str]) -> dict:
    n = len(forms)
    return {
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions,
                "filingDate": [f"2024-01-{i + 1:02d}" for i in range(n)],
                "reportDate": ["" for _ in range(n)],
                "primaryDocument": [f"doc{i}.htm" for i in range(n)],
                "primaryDocDescription": [f"desc{i}" for i in range(n)],
            }
        }
    }


@respx.mock
async def test_edgar_sec_filings_normalizes_to_filing_list():
    provider = EdgarProvider(_settings())
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=_COMPANY_TICKERS_JSON)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(
            200,
            json=_edgar_submissions(
                forms=["10-K", "8-K", "4"],
                accessions=[
                    "0000320193-24-000001",
                    "0000320193-24-000002",
                    "0000320193-24-000003",
                ],
            ),
        )
    )

    result = await provider.fetch("sec_filings", symbol="aapl")

    assert isinstance(result, list)
    # Only 10-K and 8-K match the default form filter (not Form 4).
    forms = {f["form"] for f in result}
    assert forms == {"10-K", "8-K"}
    first = result[0]
    assert first["symbol"] == "AAPL"
    assert first["filed_at"] == "2024-01-01"
    assert first["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert first["provenance"]["provider"] == "edgar"


@respx.mock
async def test_edgar_insider_transactions_surfaces_form4():
    provider = EdgarProvider(_settings())
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=_COMPANY_TICKERS_JSON)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(
            200,
            json=_edgar_submissions(
                forms=["10-K", "4", "4/A"],
                accessions=[
                    "0000320193-24-000001",
                    "0000320193-24-000002",
                    "0000320193-24-000003",
                ],
            ),
        )
    )

    result = await provider.fetch("insider_transactions", symbol="AAPL")

    assert isinstance(result, list)
    # Only the Form 4 / 4/A rows are surfaced.
    assert len(result) == 2
    txn = result[0]
    assert txn["symbol"] == "AAPL"
    assert txn["transaction"].startswith("Form 4")
    assert txn["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert txn["provenance"]["provider"] == "edgar"


@respx.mock
async def test_edgar_unsupported_capability_raises():
    provider = EdgarProvider(_settings())
    with pytest.raises(NotImplementedError):
        await provider.fetch("quote", symbol="AAPL")


# ===========================================================================
# Stock Watcher (keyless — never raises MissingApiKey)
# ===========================================================================


@respx.mock
async def test_stockwatcher_congress_trades_merges_and_filters():
    provider = StockWatcherProvider(_settings())
    respx.get(HOUSE_FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ticker": "AAPL",
                    "representative": "Rep One",
                    "type": "purchase",
                    "amount": "$1,001 - $15,000",
                    "transaction_date": "2024-01-05",
                    "disclosure_date": "2024-02-05",
                },
                {
                    # Different ticker — filtered out by symbol filter.
                    "ticker": "MSFT",
                    "representative": "Rep Two",
                    "type": "sale",
                    "transaction_date": "2024-01-06",
                    "disclosure_date": "2024-02-06",
                },
            ],
        )
    )
    respx.get(SENATE_FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ticker": "AAPL",
                    "senator": "Sen One",
                    "type": "sale",
                    "amount": "$15,001 - $50,000",
                    "transaction_date": "2024-01-07",
                    "disclosure_date": "2024-02-07",
                }
            ],
        )
    )

    result = await provider.fetch("congress_trades", symbol="aapl")

    assert isinstance(result, list)
    # Only the two AAPL trades (one per chamber); MSFT filtered out.
    assert len(result) == 2
    chambers = {t["chamber"] for t in result}
    assert chambers == {"House", "Senate"}
    assert all(t["symbol"] == "AAPL" for t in result)
    # Newest disclosure first (Senate 2024-02-07 before House 2024-02-05).
    assert result[0]["chamber"] == "Senate"
    assert result[0]["provenance"]["provider"] == "stockwatcher"


@respx.mock
async def test_stockwatcher_one_chamber_failure_degrades_gracefully():
    provider = StockWatcherProvider(_settings())
    respx.get(HOUSE_FEED_URL).mock(return_value=httpx.Response(500, text="boom"))
    respx.get(SENATE_FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ticker": "AAPL",
                    "senator": "Sen Only",
                    "type": "purchase",
                    "transaction_date": "2024-01-07",
                    "disclosure_date": "2024-02-07",
                }
            ],
        )
    )

    result = await provider.fetch("congress_trades", symbol="AAPL")

    # House 500 degrades to []; Senate data still flows through.
    assert len(result) == 1
    assert result[0]["member"] == "Sen Only"
    assert result[0]["chamber"] == "Senate"


@respx.mock
async def test_stockwatcher_unsupported_capability_raises():
    provider = StockWatcherProvider(_settings())
    with pytest.raises(NotImplementedError):
        await provider.fetch("quote", symbol="AAPL")


# ===========================================================================
# yfinance (sync lib — monkeypatch the lazily-imported ``yfinance`` module)
# ===========================================================================


class _FakeHistoryRow:
    """Minimal mapping-style row supporting ``.get(column)`` like a pandas row."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeIndex:
    """Index value exposing ``.isoformat()`` like a pandas Timestamp."""

    def __init__(self, iso: str):
        self._iso = iso

    def isoformat(self) -> str:
        return self._iso


class _FakeFrame:
    """Tiny stand-in for a pandas DataFrame used by the yfinance provider.

    Supports the exact surface the provider touches: ``.empty``, ``.columns``,
    ``.iterrows()``, item access by column (``frame[col]``) and ``.tolist()``.
    """

    def __init__(self, rows: list[tuple[str, dict]], columns: list[str]):
        self._rows = rows
        self.columns = columns
        self.empty = len(rows) == 0

    def iterrows(self):
        for iso, data in self._rows:
            yield _FakeIndex(iso), _FakeHistoryRow(data)

    def __getitem__(self, col):
        return _FakeColumn([data.get(col) for _, data in self._rows])


class _FakeColumn:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


class _FakeTicker:
    """Fake ``yfinance.Ticker`` with deterministic, offline data."""

    fast_info = {"last_price": 150.0, "previous_close": 148.0}

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, period=None, interval=None, auto_adjust=False):
        return _FakeFrame(
            rows=[
                (
                    "2024-01-01T00:00:00+00:00",
                    {
                        "Open": 100.0,
                        "High": 110.0,
                        "Low": 95.0,
                        "Close": 105.0,
                        "Volume": 1000.0,
                    },
                ),
                (
                    "2024-01-02T00:00:00+00:00",
                    {
                        "Open": 105.0,
                        "High": 115.0,
                        "Low": 102.0,
                        "Close": 112.0,
                        "Volume": 2000.0,
                    },
                ),
            ],
            columns=["Open", "High", "Low", "Close", "Volume"],
        )

    @property
    def info(self):
        return {
            "longName": "Apple Inc.",
            "sector": "Technology",
            "marketCap": 3.0e12,
            "trailingPE": 30.0,
            "unwanted_field": "ignored",
        }


@pytest.fixture
def fake_yfinance(monkeypatch):
    """Install a fake ``yfinance`` module so the provider's lazy import resolves.

    The provider does ``import yfinance as yf`` inside its blocking helpers, so
    we register a fake module under ``sys.modules['yfinance']``. No network, no
    real dependency.
    """
    fake_module = types.ModuleType("yfinance")
    fake_module.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)
    return fake_module


async def test_yfinance_ohlcv_normalizes(fake_yfinance):
    provider = YFinanceProvider(_settings())
    result = await provider.fetch("ohlcv", symbol="aapl", interval="1d", period="6mo")

    assert result["symbol"] == "AAPL"
    assert result["interval"] == "1d"
    assert len(result["bars"]) == 2
    assert result["bars"][0]["open"] == 100.0
    assert result["bars"][0]["close"] == 105.0
    assert result["bars"][1]["volume"] == 2000.0
    assert result["bars"][0]["ts"].startswith("2024-01-01")
    assert result["provenance"]["provider"] == "yfinance"
    assert result["provenance"]["cached"] is False


async def test_yfinance_quote_computes_change(fake_yfinance):
    provider = YFinanceProvider(_settings())
    result = await provider.fetch("quote", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    assert result["price"] == 150.0
    # change = 150 - 148 = 2.0; pct = (2 / 148) * 100.
    assert result["change"] == pytest.approx(2.0)
    assert result["change_pct"] == pytest.approx((2.0 / 148.0) * 100.0)
    assert result["provenance"]["provider"] == "yfinance"


async def test_yfinance_fundamentals_subset(fake_yfinance):
    provider = YFinanceProvider(_settings())
    result = await provider.fetch("fundamentals", symbol="AAPL")

    assert result["symbol"] == "AAPL"
    funds = result["fundamentals"]
    assert funds["longName"] == "Apple Inc."
    assert funds["sector"] == "Technology"
    assert funds["marketCap"] == 3.0e12
    # Only whitelisted fields are copied through.
    assert "unwanted_field" not in funds
    assert result["provenance"]["provider"] == "yfinance"


async def test_yfinance_unsupported_capability_raises(fake_yfinance):
    provider = YFinanceProvider(_settings())
    with pytest.raises(NotImplementedError):
        await provider.fetch("company_news", symbol="AAPL")
