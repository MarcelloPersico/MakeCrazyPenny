"""Crypto provider tests (CONTRACT.md §16). Deterministic + offline via respx."""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
import respx

from makecrazypenny.core.config import Settings
from makecrazypenny.providers.binance import BinanceProvider
from makecrazypenny.providers.bybit import BybitProvider
from makecrazypenny.providers.coingecko import CoinGeckoProvider
from makecrazypenny.providers.fear_greed import FearGreedProvider
from makecrazypenny.providers.registry import ProviderRegistry


def _settings() -> Settings:
    return Settings(cache_dir=Path(tempfile.mkdtemp()), l2_cache_enabled=False)


# ===========================================================================
# Binance
# ===========================================================================


@respx.mock
async def test_binance_ohlcv_normalizes_array_klines() -> None:
    respx.get("https://fapi.binance.com/fapi/v1/klines").mock(
        return_value=httpx.Response(
            200,
            json=[
                [1700000000000, "42000", "42100", "41900", "42050", "1234", 0, "x", 10, "600", "y", "0"],
                [1700000300000, "42050", "42200", "42000", "42180", "999", 0, "x", 9, "500", "y", "0"],
            ],
        )
    )
    provider = BinanceProvider(_settings())
    result = await provider.fetch("crypto_ohlcv", symbol="btc", interval="5m", limit=2)
    assert result["symbol"] == "BTCUSDT"
    assert result["interval"] == "5m"
    assert [b["close"] for b in result["bars"]] == [42050.0, 42180.0]
    assert result["provenance"]["provider"] == "binance"


@respx.mock
async def test_binance_funding_rate_with_basis() -> None:
    respx.get("https://fapi.binance.com/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "markPrice": "42180.0",
                "indexPrice": "42150.0",
                "lastFundingRate": "0.00012",
                "nextFundingTime": 1700028800000,
            },
        )
    )
    provider = BinanceProvider(_settings())
    fr = await provider.fetch("funding_rate", symbol="BTC/USDT")
    assert fr["rate"] == 0.00012
    assert fr["annualized"] == pytest.approx(0.00012 * 3 * 365, rel=1e-6)
    assert fr["basis"] == pytest.approx(42180.0 / 42150.0 - 1.0, rel=1e-6)


@respx.mock
async def test_binance_open_interest_history_and_change() -> None:
    respx.get("https://fapi.binance.com/futures/data/openInterestHist").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"sumOpenInterest": "1000", "sumOpenInterestValue": "4.0e7", "timestamp": 1700000000000},
                {"sumOpenInterest": "1100", "sumOpenInterestValue": "4.5e7", "timestamp": 1700000300000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    oi = await provider.fetch("open_interest", symbol="BTCUSDT", interval="5m")
    assert oi["open_interest"] == 1100.0  # latest point is current
    assert len(oi["history"]) == 2


@respx.mock
async def test_binance_long_short_ratio() -> None:
    respx.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"longShortRatio": "2.1", "longAccount": "0.68", "shortAccount": "0.32", "timestamp": 1700000300000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    ls = await provider.fetch("long_short_ratio", symbol="BTCUSDT", interval="5m")
    assert ls["ratio"] == 2.1
    assert ls["long_pct"] == 0.68


@respx.mock
async def test_binance_funding_interval_discovered() -> None:
    # fundingInfo lists symbols that deviate from the 8h default (here: 4h).
    respx.get("https://fapi.binance.com/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(
            200,
            json={"symbol": "ALTUSDT", "markPrice": "1.0", "indexPrice": "1.0", "lastFundingRate": "0.0001"},
        )
    )
    respx.get("https://fapi.binance.com/fapi/v1/fundingInfo").mock(
        return_value=httpx.Response(200, json=[{"symbol": "ALTUSDT", "fundingIntervalHours": "4"}])
    )
    provider = BinanceProvider(_settings())
    fr = await provider.fetch("funding_rate", symbol="ALT")
    assert fr["interval_hours"] == 4.0
    # Annualization follows the real interval: 6 settlements/day, not 3.
    assert fr["annualized"] == pytest.approx(0.0001 * 6 * 365, rel=1e-6)


@respx.mock
async def test_binance_missing_funding_rate_raises() -> None:
    # A missing rate is a provider failure, not a balanced market at 0.
    respx.get("https://fapi.binance.com/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "42000"})
    )
    provider = BinanceProvider(_settings())
    with pytest.raises(ValueError):
        await provider.fetch("funding_rate", symbol="BTC")


# ===========================================================================
# Bybit
# ===========================================================================


@respx.mock
async def test_bybit_kline_sorted_ascending() -> None:
    respx.get("https://api.bybit.com/v5/market/kline").mock(
        return_value=httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        ["1700000300000", "42050", "42200", "42000", "42180", "999", "x"],
                        ["1700000000000", "42000", "42100", "41900", "42050", "1234", "x"],
                    ]
                },
            },
        )
    )
    provider = BybitProvider(_settings())
    result = await provider.fetch("crypto_ohlcv", symbol="BTC", interval="5m", limit=2)
    # Bybit returns newest-first; provider sorts ascending.
    assert [b["close"] for b in result["bars"]] == [42050.0, 42180.0]


@respx.mock
async def test_bybit_funding_from_ticker() -> None:
    respx.get("https://api.bybit.com/v5/market/tickers").mock(
        return_value=httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT", "lastPrice": "42180", "prevPrice24h": "41000",
                            "price24hPcnt": "0.0288", "markPrice": "42185", "indexPrice": "42150",
                            "fundingRate": "-0.0003", "nextFundingTime": "1700028800000",
                            "openInterest": "5000", "openInterestValue": "210900000",
                        }
                    ]
                },
            },
        )
    )
    provider = BybitProvider(_settings())
    fr = await provider.fetch("funding_rate", symbol="BTC")
    assert fr["rate"] == -0.0003
    q = await provider.fetch("crypto_quote", symbol="BTC")
    assert q["price"] == 42180.0
    assert q["change_pct"] == pytest.approx(2.88, rel=1e-6)


@respx.mock
async def test_bybit_funding_interval_from_instruments_info() -> None:
    respx.get("https://api.bybit.com/v5/market/tickers").mock(
        return_value=httpx.Response(
            200,
            json={"retCode": 0, "result": {"list": [{"symbol": "ALTUSDT", "lastPrice": "1.0", "fundingRate": "0.0001"}]}},
        )
    )
    respx.get("https://api.bybit.com/v5/market/instruments-info").mock(
        return_value=httpx.Response(
            200,
            json={"retCode": 0, "result": {"list": [{"symbol": "ALTUSDT", "fundingInterval": "60"}]}},
        )
    )
    provider = BybitProvider(_settings())
    fr = await provider.fetch("funding_rate", symbol="ALT")
    assert fr["interval_hours"] == 1.0  # 60 minutes
    assert fr["annualized"] == pytest.approx(0.0001 * 24 * 365, rel=1e-6)


@respx.mock
async def test_bybit_non_zero_retcode_raises() -> None:
    respx.get("https://api.bybit.com/v5/market/tickers").mock(
        return_value=httpx.Response(200, json={"retCode": 10001, "retMsg": "bad symbol", "result": {}})
    )
    provider = BybitProvider(_settings())
    with pytest.raises(ValueError):
        await provider.fetch("funding_rate", symbol="NOPE")


# ===========================================================================
# CoinGecko + Fear & Greed
# ===========================================================================


@respx.mock
async def test_coingecko_global() -> None:
    respx.get("https://api.coingecko.com/api/v3/global").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "total_market_cap": {"usd": 2.3e12},
                    "total_volume": {"usd": 9.0e10},
                    "market_cap_percentage": {"btc": 52.1, "eth": 17.0},
                    "market_cap_change_percentage_24h_usd": 1.5,
                }
            },
        )
    )
    provider = CoinGeckoProvider(_settings())
    g = await provider.fetch("crypto_global")
    assert g["btc_dominance"] == 52.1
    assert g["total_market_cap"] == 2.3e12


@respx.mock
async def test_fear_greed_maps_to_score() -> None:
    respx.get("https://api.alternative.me/fng/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"value": "25", "value_classification": "Fear", "timestamp": "1700000000"},
                    {"value": "40", "value_classification": "Fear", "timestamp": "1699913600"},
                ]
            },
        )
    )
    provider = FearGreedProvider(_settings())
    fg = await provider.fetch("crypto_sentiment")
    assert fg["score"] == -0.5  # (25 - 50) / 50
    assert fg["value"] == 25.0
    assert fg["label"] == "fear"


# ===========================================================================
# Registry fallthrough: Binance geo-blocked -> Bybit
# ===========================================================================


@respx.mock
async def test_registry_falls_through_binance_to_bybit() -> None:
    # Binance returns HTTP 451 (geo-block); the chain must fall through to Bybit.
    respx.get("https://fapi.binance.com/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(451, text="Unavailable For Legal Reasons")
    )
    respx.get("https://api.bybit.com/v5/market/tickers").mock(
        return_value=httpx.Response(
            200,
            json={"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "42000", "markPrice": "42010", "indexPrice": "42000", "fundingRate": "0.0001"}]}},
        )
    )
    registry = ProviderRegistry(_settings())
    registry.register(BinanceProvider(registry.settings))
    registry.register(BybitProvider(registry.settings))
    env = await registry.fetch("funding_rate", symbol="BTCUSDT")
    assert env["provider"] == "bybit"
    assert env["data"]["rate"] == 0.0001
