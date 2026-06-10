"""Binance flow/positioning/funding-history capability tests (swarm design, agent W2).

Deterministic and fully offline via ``respx``. Covers:

  * the kline-parsing regression — bars rows STILL carry the frozen
    ``OHLCVBar`` keys and now additionally ``quote_volume`` (field 7) and
    ``taker_buy_volume`` (field 9);
  * the three new futures-only capabilities (``taker_flow``,
    ``top_trader_ratio``, ``funding_history``) and their exact payload shapes;
  * the spot-pair nuance: futures-only capabilities raise
    ``NotImplementedError`` (the registry's silent-skip signal) BEFORE any
    HTTP request is issued.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
import respx

from makecrazypenny.core.config import Settings
from makecrazypenny.providers.binance import BinanceProvider


def _settings() -> Settings:
    return Settings(cache_dir=Path(tempfile.mkdtemp()), l2_cache_enabled=False)


#: Two full 12-field fapi klines (openTime, o, h, l, c, volume, closeTime,
#: quoteVolume, nTrades, takerBuyBase, takerBuyQuote, ignore).
_KLINES_FULL = [
    [1700000000000, "42000", "42100", "41900", "42050", "1234", 1700000299999,
     "51900000", 2456, "700", "29500000", "0"],
    [1700000300000, "42050", "42200", "42000", "42180", "999", 1700000599999,
     "42100000", 2100, "400", "16900000", "0"],
]


# ===========================================================================
# Kline regression: existing keys unchanged, flow extras added
# ===========================================================================


@respx.mock
async def test_klines_keep_ohlc_and_gain_flow_fields() -> None:
    respx.get("https://fapi.binance.com/fapi/v1/klines").mock(
        return_value=httpx.Response(200, json=_KLINES_FULL)
    )
    provider = BinanceProvider(_settings())
    result = await provider.fetch("crypto_ohlcv", symbol="BTC", interval="5m", limit=2)
    assert result["symbol"] == "BTCUSDT"
    assert len(result["bars"]) == 2
    first = result["bars"][0]
    # Regression: the frozen OHLCVBar keys are all still present and correct.
    assert first["open"] == 42000.0
    assert first["high"] == 42100.0
    assert first["low"] == 41900.0
    assert first["close"] == 42050.0
    assert first["volume"] == 1234.0
    assert first["ts"].startswith("2023-11-14")
    # New: flow extras ride on every row (fields 7 and 9).
    assert first["quote_volume"] == 51900000.0
    assert first["taker_buy_volume"] == 700.0
    assert result["bars"][1]["taker_buy_volume"] == 400.0
    assert result["bars"][1]["quote_volume"] == 42100000.0
    assert result["provenance"]["provider"] == "binance"


@respx.mock
async def test_klines_short_rows_yield_none_extras_not_zero() -> None:
    # A 6-field row (no fields 7/9) still parses; the extras are None — a
    # missing taker volume must never masquerade as "all-sell" 0.0 for CVD.
    respx.get("https://fapi.binance.com/fapi/v1/klines").mock(
        return_value=httpx.Response(
            200, json=[[1700000000000, "42000", "42100", "41900", "42050", "1234"]]
        )
    )
    provider = BinanceProvider(_settings())
    result = await provider.fetch("crypto_ohlcv", symbol="BTCUSDT", interval="5m", limit=1)
    bar = result["bars"][0]
    assert bar["close"] == 42050.0
    assert bar["volume"] == 1234.0
    assert bar["quote_volume"] is None
    assert bar["taker_buy_volume"] is None


# ===========================================================================
# taker_flow -> /futures/data/takerlongshortRatio
# ===========================================================================


@respx.mock
async def test_taker_flow_series_shape_and_params() -> None:
    route = respx.get("https://fapi.binance.com/futures/data/takerlongshortRatio").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"buySellRatio": "1.2", "buyVol": "120", "sellVol": "100",
                 "timestamp": 1700000000000},
                {"buySellRatio": "0.8", "buyVol": "80", "sellVol": "100",
                 "timestamp": 1700000300000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    flow = await provider.fetch("taker_flow", symbol="BTC/USDT", interval="5m", limit=2)
    assert flow["symbol"] == "BTCUSDT"
    assert [p["buy_sell_ratio"] for p in flow["series"]] == [1.2, 0.8]
    assert all(set(p) == {"time", "buy_sell_ratio"} for p in flow["series"])
    assert flow["series"][0]["time"].startswith("2023-11-14")
    assert flow["as_of"]
    params = route.calls.last.request.url.params
    assert params["symbol"] == "BTCUSDT"
    assert params["period"] == "5m"
    assert params["limit"] == "2"


@respx.mock
async def test_taker_flow_tolerates_garbage_rows_and_clamps_limit() -> None:
    route = respx.get("https://fapi.binance.com/futures/data/takerlongshortRatio").mock(
        return_value=httpx.Response(
            200,
            json=[
                "not-a-dict",
                {"buySellRatio": "junk", "timestamp": 1700000000000},
                {"buySellRatio": "1.1"},  # no timestamp
                {"buySellRatio": "1.05", "timestamp": 1700000300000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    # interval 1m is not a valid stats period -> closest valid (5m); limit clamped to 500.
    flow = await provider.fetch("taker_flow", symbol="BTC", interval="1m", limit=9999)
    assert [p["buy_sell_ratio"] for p in flow["series"]] == [1.05]
    params = route.calls.last.request.url.params
    assert params["period"] == "5m"
    assert params["limit"] == "500"


# ===========================================================================
# top_trader_ratio -> /futures/data/topLongShortPositionRatio
# ===========================================================================


@respx.mock
async def test_top_trader_ratio_series_shape() -> None:
    route = respx.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"longShortRatio": "1.8", "longAccount": "0.64", "shortAccount": "0.36",
                 "timestamp": 1700000000000},
                {"longShortRatio": "2.0", "longAccount": "0.67", "shortAccount": "0.33",
                 "timestamp": 1700000300000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    top = await provider.fetch("top_trader_ratio", symbol="eth", interval="15m", limit=2)
    assert top["symbol"] == "ETHUSDT"
    assert [p["ratio"] for p in top["series"]] == [1.8, 2.0]
    assert all(set(p) == {"time", "ratio"} for p in top["series"])
    assert top["as_of"]
    params = route.calls.last.request.url.params
    assert params["symbol"] == "ETHUSDT"
    assert params["period"] == "15m"


# ===========================================================================
# funding_history -> /fapi/v1/fundingRate
# ===========================================================================


@respx.mock
async def test_funding_history_rates_shape_and_clamp() -> None:
    route = respx.get("https://fapi.binance.com/fapi/v1/fundingRate").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "BTCUSDT", "fundingRate": "0.00010", "fundingTime": 1700000000000},
                {"symbol": "BTCUSDT", "fundingRate": "-0.00005", "fundingTime": 1700028800000},
                {"symbol": "BTCUSDT", "fundingRate": "bad", "fundingTime": 1700057600000},
            ],
        )
    )
    provider = BinanceProvider(_settings())
    hist = await provider.fetch("funding_history", symbol="BTC-USD", limit=5000)
    assert hist["symbol"] == "BTCUSDT"
    assert [p["rate"] for p in hist["rates"]] == [0.0001, -5e-05]
    assert all(set(p) == {"time", "rate"} for p in hist["rates"])
    assert hist["rates"][0]["time"].startswith("2023-11-14")
    assert hist["as_of"]
    params = route.calls.last.request.url.params
    assert params["symbol"] == "BTCUSDT"
    assert params["limit"] == "1000"  # clamped from 5000


@respx.mock
async def test_funding_history_default_limit_is_66() -> None:
    route = respx.get("https://fapi.binance.com/fapi/v1/fundingRate").mock(
        return_value=httpx.Response(200, json=[])
    )
    provider = BinanceProvider(_settings())
    hist = await provider.fetch("funding_history", symbol="SOL")
    assert hist["rates"] == []
    assert route.calls.last.request.url.params["limit"] == "66"


# ===========================================================================
# Futures-only nuance: spot-only pairs are a registry silent skip
# ===========================================================================


@respx.mock
async def test_futures_only_capabilities_skip_spot_pairs_before_http() -> None:
    # No respx routes registered: any HTTP attempt would error loudly, so a
    # clean NotImplementedError proves the guard fires before the request.
    provider = BinanceProvider(_settings())
    for capability in ("taker_flow", "top_trader_ratio", "funding_history"):
        with pytest.raises(NotImplementedError):
            await provider.fetch(capability, symbol="BTC/EUR")
        with pytest.raises(NotImplementedError):
            await provider.fetch(capability, symbol="ETH/BTC")


def test_new_capabilities_declared_supported() -> None:
    assert {"taker_flow", "top_trader_ratio", "funding_history"} <= BinanceProvider.supported
    # And the original capability set is intact.
    assert {
        "crypto_ohlcv", "crypto_quote", "funding_rate", "open_interest", "long_short_ratio",
    } <= BinanceProvider.supported
