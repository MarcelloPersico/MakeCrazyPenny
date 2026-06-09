"""Tests for the live-fetched S&P 500 universe (CONTRACT.md §12).

Deterministic and OFFLINE: the live HTTP fetch is monkeypatched and the cache dir
is redirected to a tmp path, so nothing touches the network or the real cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from makecrazypenny.core import universe
from makecrazypenny.core.config import Settings

_SAMPLE_CSV = (
    "Symbol,Security,GICS Sector,GICS Sub-Industry\n"
    "MMM,3M,Industrials,Industrial Conglomerates\n"
    "AAPL,Apple Inc.,Information Technology,Technology Hardware\n"
    "BRK.B,Berkshire Hathaway,Financials,Multi-Sector Holdings\n"
    ",Junk row with no symbol,Energy,Oil\n"
    "AAPL,Duplicate Apple,Information Technology,Dup\n"
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(cache_dir=tmp_path / "cache")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_universe_symbol_class_shares() -> None:
    assert universe.normalize_universe_symbol("BRK.B") == "BRK-B"
    assert universe.normalize_universe_symbol(" $aapl ") == "AAPL"
    assert universe.normalize_universe_symbol("bf.b") == "BF-B"


def test_parse_csv_dedups_and_skips_blank_symbols() -> None:
    parsed = universe._parse_csv(_SAMPLE_CSV)
    assert parsed["symbols"] == ["MMM", "AAPL", "BRK-B"]  # dedup + dot->dash, blank skipped
    assert parsed["count"] == 3
    assert parsed["sector_of"]["AAPL"] == "Information Technology"


def test_fallback_is_sector_union() -> None:
    fb = universe._fallback()
    assert fb["source"] == "fallback"
    assert "AAPL" in fb["symbols"] and "JPM" in fb["symbols"]
    assert fb["count"] == len(fb["symbols"]) > 0


# ---------------------------------------------------------------------------
# fetch_sp500 resolution order: live -> cache -> fallback
# ---------------------------------------------------------------------------


async def test_fetch_live_then_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_live(url: str = universe.SP500_SOURCE_URL):
        calls["n"] += 1
        return universe._parse_csv(_SAMPLE_CSV)

    monkeypatch.setattr(universe, "_fetch_live", fake_live)
    settings = _settings(tmp_path)

    first = await universe.fetch_sp500(settings=settings)
    assert first["source"] == "live"
    assert first["symbols"] == ["MMM", "AAPL", "BRK-B"]
    assert first["as_of"]
    assert calls["n"] == 1

    # Second call within TTL is served from the cache (no second live fetch).
    second = await universe.fetch_sp500(settings=settings)
    assert second["source"] == "cache"
    assert second["stale"] is False
    assert second["symbols"] == ["MMM", "AAPL", "BRK-B"]
    assert calls["n"] == 1


async def test_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_live(url: str = universe.SP500_SOURCE_URL):
        calls["n"] += 1
        return universe._parse_csv(_SAMPLE_CSV)

    monkeypatch.setattr(universe, "_fetch_live", fake_live)
    settings = _settings(tmp_path)

    await universe.fetch_sp500(settings=settings)
    await universe.fetch_sp500(settings=settings, force_refresh=True)
    assert calls["n"] == 2  # refresh refetched live


async def test_stale_cache_beats_failed_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    # Seed a live fetch, then make subsequent live fetches fail and TTL expire.
    monkeypatch.setattr(universe, "_fetch_live", lambda url=universe.SP500_SOURCE_URL: universe._parse_csv(_SAMPLE_CSV))
    await universe.fetch_sp500(settings=settings)

    monkeypatch.setattr(universe, "_fetch_live", lambda url=universe.SP500_SOURCE_URL: None)
    monkeypatch.setattr(universe, "_CACHE_TTL_SECONDS", -1)  # force the cache stale

    out = await universe.fetch_sp500(settings=settings)
    assert out["source"] == "cache"
    assert out["stale"] is True
    assert out["symbols"] == ["MMM", "AAPL", "BRK-B"]


async def test_fallback_when_live_and_cache_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(universe, "_fetch_live", lambda url=universe.SP500_SOURCE_URL: None)
    out = await universe.fetch_sp500(settings=_settings(tmp_path))
    assert out["source"] == "fallback"
    assert "AAPL" in out["symbols"]
