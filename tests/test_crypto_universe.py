"""Tests for the crypto perp universe (CONTRACT.md §16). Offline (fetch monkeypatched)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from makecrazypenny.core import crypto_universe as U
from makecrazypenny.core.config import Settings


def _settings() -> Settings:
    return Settings(cache_dir=Path(tempfile.mkdtemp()))


def test_fallback_is_majors() -> None:
    fb = U._fallback()
    assert fb["source"] == "fallback"
    assert "BTCUSDT" in fb["symbols"]
    assert fb["count"] > 10


async def test_live_then_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    monkeypatch.setattr(U, "_fetch_live_binance", lambda base: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    monkeypatch.setattr(U, "_fetch_live_bybit", lambda base: None)

    live = await U.fetch_top_perps(settings=settings, limit=2)
    assert live["source"] == "live"
    assert live["symbols"] == ["BTCUSDT", "ETHUSDT"]  # sliced to limit
    assert live["total_available"] == 3

    # Live now fails, but a fresh cache was written -> served from cache.
    monkeypatch.setattr(U, "_fetch_live_binance", lambda base: None)
    cached = await U.fetch_top_perps(settings=settings, limit=3)
    assert cached["source"] == "cache"
    assert cached["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def test_force_refresh_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    # No live, no cache -> curated fallback.
    monkeypatch.setattr(U, "_fetch_live_binance", lambda base: None)
    monkeypatch.setattr(U, "_fetch_live_bybit", lambda base: None)
    fb = await U.fetch_top_perps(settings=settings, limit=5)
    assert fb["source"] == "fallback"
    assert fb["count"] == 5


async def test_bybit_fallback_used_when_binance_none(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    monkeypatch.setattr(U, "_fetch_live_binance", lambda base: None)
    monkeypatch.setattr(U, "_fetch_live_bybit", lambda base: ["XRPUSDT", "DOGEUSDT"])
    result = await U.fetch_top_perps(settings=settings, limit=10)
    assert result["source"] == "live"
    assert result["symbols"] == ["XRPUSDT", "DOGEUSDT"]
