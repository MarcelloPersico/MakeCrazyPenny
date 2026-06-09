"""Tests for crypto derivatives signal cores + symbols (CONTRACT.md §16). Pure."""

from __future__ import annotations

import pytest

from makecrazypenny.analysis.crypto_metrics import (
    basis_value,
    fear_greed_signal,
    funding_signal,
    long_short_signal,
    oi_change_from_history,
    oi_price_signal,
)
from makecrazypenny.core import symbols as S


# --- funding (contrarian) ---------------------------------------------------


def test_funding_signal_is_contrarian() -> None:
    # Crowded longs (positive funding) -> bearish; crowded shorts -> bullish.
    bearish, _ = funding_signal(0.0005, 0.60)
    bullish, _ = funding_signal(-0.0004, -0.40)
    assert bearish < 0 < bullish
    # Saturates at the configured extreme.
    assert funding_signal(0.01, 1.0)[0] == -1.0
    assert funding_signal(None) is None


# --- OI x price matrix ------------------------------------------------------


def test_oi_price_matrix_quadrants() -> None:
    assert oi_price_signal(0.05, 0.03)[0] > 0     # OI up + price up -> bullish
    assert oi_price_signal(0.05, -0.03)[0] < 0    # OI up + price down -> bearish
    assert oi_price_signal(-0.05, 0.03)[0] < 0    # OI down + price up -> fade (mild bearish)
    assert oi_price_signal(-0.05, -0.03)[0] > 0   # OI down + price down -> fade flush (mild bullish)
    assert oi_price_signal(None, 0.03) is None


def test_oi_change_from_history() -> None:
    assert oi_change_from_history([{"open_interest": 100}, {"open_interest": 110}]) == pytest.approx(0.1)
    assert oi_change_from_history([{"open_interest": 100}]) is None
    assert oi_change_from_history("nope") is None


# --- long/short (contrarian) ------------------------------------------------


def test_long_short_signal_is_contrarian() -> None:
    assert long_short_signal(3.0)[0] < 0    # crowd long -> bearish
    assert long_short_signal(0.333)[0] > 0  # crowd short -> bullish
    assert abs(long_short_signal(1.0)[0]) < 1e-9
    assert long_short_signal(0) is None


# --- fear & greed (contrarian) ----------------------------------------------


def test_fear_greed_signal_is_contrarian() -> None:
    assert fear_greed_signal(90)[0] < 0   # extreme greed -> bearish
    assert fear_greed_signal(10)[0] > 0   # extreme fear -> bullish
    assert fear_greed_signal(None) is None


def test_basis_value() -> None:
    assert basis_value(101.0, 100.0) == pytest.approx(0.01)
    assert basis_value(None, 100.0) is None


# --- symbols ----------------------------------------------------------------


def test_canonical_and_split() -> None:
    assert S.canonical_crypto("btc") == "BTCUSDT"
    assert S.canonical_crypto("$BTC") == "BTCUSDT"
    assert S.canonical_crypto("ETH/USD") == "ETHUSDT"
    assert S.canonical_crypto("BTC-USDT") == "BTCUSDT"
    assert S.canonical_crypto("solusdt") == "SOLUSDT"
    # USD-like quotes (incl. USDC) map onto the liquid USDT perp by design.
    assert S.split_crypto("BTCUSDC") == ("BTC", "USDT")
    assert S.split_crypto("BTCEUR") == ("BTC", "EUR")  # a non-USD quote is preserved
    assert S.base_asset("DOGE/USDT") == "DOGE"


def test_is_crypto_symbol_detection() -> None:
    assert S.is_crypto_symbol("BTC") is True
    assert S.is_crypto_symbol("BTC/USDT") is True
    assert S.is_crypto_symbol("ETHUSDT") is True
    # Equity tickers are not misread as crypto.
    assert S.is_crypto_symbol("AAPL") is False
    assert S.is_crypto_symbol("BRK-B") is False
