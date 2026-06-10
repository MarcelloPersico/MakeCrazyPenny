"""Hyperliquid info-API provider tests (DESIGN-SWARM.md). Offline via respx.

Mock payloads are copied from the LIVE-VERIFIED response shapes captured in
``research-out/hlSrc.json`` — notably ``metaAndAssetCtxs`` being a two-element
``[meta, assetCtxs]`` list whose arrays are index-aligned, and
``predictedFundings`` being nested ``[[coin, [[venue, {...}], ...]], ...]``
tuples. All POSTs go to one URL, so a single side-effect router dispatches on
the request body's ``type`` field.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from makecrazypenny.core.config import Settings
from makecrazypenny.providers.hyperliquid_info import HyperliquidInfoProvider

_INFO_URL = "https://api.hyperliquid.xyz/info"


def _settings(cache_dir: Path | None = None) -> Settings:
    return Settings(cache_dir=cache_dir or Path(tempfile.mkdtemp()), l2_cache_enabled=False)


def _meta_and_ctxs(extra_coin: str | None = None) -> list[Any]:
    """Real ``[meta, assetCtxs]`` shape from hlSrc.json (BTC ctx verbatim)."""
    universe = [
        {"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "marginTableId": 56},
        {"szDecimals": 1, "name": "MATIC", "maxLeverage": 20, "marginTableId": 20, "isDelisted": True},
        {"szDecimals": 2, "name": "ETH", "maxLeverage": 25, "marginTableId": 10},
    ]
    ctxs = [
        {
            "funding": "0.000000888",
            "openInterest": "32097.10012",
            "prevDayPx": "62675.0",
            "dayNtlVlm": "2968978885.799",
            "premium": "-0.0006162629",
            "oraclePx": "61662.0",
            "markPx": "61628.0",
            "midPx": "61623.5",
            "impactPxs": ["61621.8", "61624.0"],
            "dayBaseVlm": "47860.40511",
        },
        {
            "funding": "0.0000125",
            "openInterest": "1000.0",
            "prevDayPx": "0.5",
            "dayNtlVlm": "1000.0",
            "premium": "0.0",
            "oraclePx": "0.5",
            "markPx": "0.5",
            "midPx": "0.5",
            "impactPxs": ["0.49", "0.51"],
            "dayBaseVlm": "2000.0",
        },
        {
            "funding": "0.0000125",
            "openInterest": "500000.5",
            "prevDayPx": "3000.0",
            "dayNtlVlm": "150000000.0",
            "premium": "0.0001",
            "oraclePx": "3100.0",
            "markPx": "3105.0",
            "midPx": "3104.5",
            "impactPxs": ["3104.0", "3105.0"],
            "dayBaseVlm": "48000.0",
        },
    ]
    if extra_coin:
        universe.append({"szDecimals": 0, "name": extra_coin, "maxLeverage": 10, "marginTableId": 1})
        ctxs.append(
            {
                "funding": "0.0000125",
                "openInterest": "10.0",
                "prevDayPx": "1.0",
                "dayNtlVlm": "5.0",
                "premium": "0.0",
                "oraclePx": "1.0",
                "markPx": "1.0",
                "midPx": "1.0",
                "impactPxs": ["0.99", "1.01"],
                "dayBaseVlm": "5.0",
            }
        )
    return [{"universe": universe}, ctxs]


# Verbatim nested-tuple shape from hlSrc.json (predictedFundings).
_PREDICTED = [
    [
        "0G",
        [
            ["BinPerp", {"fundingRate": "0.0000109", "nextFundingTime": 1781064000000, "fundingIntervalHours": 4}],
            ["HlPerp", {"fundingRate": "-0.0000916585", "nextFundingTime": 1781053200000, "fundingIntervalHours": 1}],
            ["BybitPerp", {"fundingRate": "0.00005", "nextFundingTime": 1781064000000, "fundingIntervalHours": 4}],
        ],
    ],
]

# Verbatim hourly rows from hlSrc.json (fundingHistory).
_FUNDING_HISTORY = [
    {"coin": "BTC", "fundingRate": "0.0000115313", "premium": "-0.0004077499", "time": 1781002800067},
    {"coin": "BTC", "fundingRate": "0.0000118977", "premium": "-0.0004048187", "time": 1781006400036},
    {"coin": "BTC", "fundingRate": "0.0000125", "premium": "-0.0003952308", "time": 1781010000031},
]


def _l2book(n_bids: int = 25, n_asks: int = 3) -> dict[str, Any]:
    """l2Book shape from hlSrc.json: levels = [bids[], asks[]], {px, sz, n} rows."""
    bids = [{"px": f"{61635.0 - i:.1f}", "sz": f"{1.0 + i / 10.0:.4f}", "n": 4} for i in range(n_bids)]
    asks = [{"px": f"{61636.0 + i:.1f}", "sz": f"{2.0 + i / 10.0:.4f}", "n": 7} for i in range(n_asks)]
    return {"coin": "BTC", "time": 1781055922933, "levels": [bids, asks]}


def _mock_info(payloads: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> None:
    """Route every POST /info by the JSON body's ``type`` (one URL, many shapes)."""

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if seen is not None:
            seen.append(body)
        rtype = body.get("type")
        if rtype in payloads:
            return httpx.Response(200, json=payloads[rtype])
        return httpx.Response(422, json={"error": f"unmocked type {rtype}"})

    respx.post(_INFO_URL).mock(side_effect=_respond)


# ===========================================================================
# hl_asset_ctx
# ===========================================================================


@respx.mock
async def test_asset_ctx_normalizes_meta_and_ctxs_pair() -> None:
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs()})
    provider = HyperliquidInfoProvider(_settings())

    result = await provider.fetch("hl_asset_ctx", symbol="BTCUSDT")

    assert result["coin"] == "BTC"
    assert result["mark_price"] == 61628.0
    assert result["oracle_price"] == 61662.0
    assert result["mid_price"] == 61623.5
    assert result["funding_hourly"] == pytest.approx(0.000000888)
    assert result["funding_annualized"] == pytest.approx(0.000000888 * 24 * 365)
    assert result["open_interest"] == pytest.approx(32097.10012)
    assert result["premium"] == pytest.approx(-0.0006162629)
    assert result["day_volume_usd"] == pytest.approx(2968978885.799)
    assert result["max_leverage"] == 40.0
    assert result["impact_bid"] == 61621.8
    assert result["impact_ask"] == 61624.0
    assert result["prev_day_price"] == 62675.0
    assert result["as_of"]
    assert result["provenance"]["provider"] == "hyperliquid_info"
    assert result["provenance"]["cached"] is False


@respx.mock
@pytest.mark.parametrize("spelling", ["btc", "BTC", "BTC/USDT", "BTC-USD", "BTCUSDC", "BTC-PERP", "$btc"])
async def test_asset_ctx_symbol_spellings_resolve_to_coin(spelling: str) -> None:
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs()})
    provider = HyperliquidInfoProvider(_settings())

    result = await provider.fetch("hl_asset_ctx", symbol=spelling)

    assert result["coin"] == "BTC"


@respx.mock
async def test_asset_ctx_unknown_symbol_raises() -> None:
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs()})
    provider = HyperliquidInfoProvider(_settings())

    with pytest.raises(ValueError):
        await provider.fetch("hl_asset_ctx", symbol="DOGEUSDT")


async def test_unsupported_capability_raises_not_implemented() -> None:
    provider = HyperliquidInfoProvider(_settings())

    with pytest.raises(NotImplementedError):
        await provider.fetch("crypto_quote", symbol="BTC")


# ===========================================================================
# hl_predicted_funding
# ===========================================================================


@respx.mock
async def test_predicted_funding_unpacks_nested_venue_tuples() -> None:
    _mock_info({"predictedFundings": _PREDICTED})
    provider = HyperliquidInfoProvider(_settings())

    result = await provider.fetch("hl_predicted_funding", symbol="0gusdt")

    assert result["coin"] == "0G"
    assert [v["venue"] for v in result["venues"]] == ["BinPerp", "HlPerp", "BybitPerp"]
    hl = result["venues"][1]
    assert hl["rate"] == pytest.approx(-0.0000916585)
    assert hl["interval_hours"] == 1.0
    assert result["venues"][0]["interval_hours"] == 4.0
    assert result["as_of"]
    assert result["provenance"]["provider"] == "hyperliquid_info"


@respx.mock
async def test_predicted_funding_unknown_coin_raises() -> None:
    _mock_info({"predictedFundings": _PREDICTED})
    provider = HyperliquidInfoProvider(_settings())

    with pytest.raises(ValueError):
        await provider.fetch("hl_predicted_funding", symbol="BTC")


# ===========================================================================
# hl_l2book
# ===========================================================================


@respx.mock
async def test_l2book_truncates_to_top20_and_forwards_nsigfigs() -> None:
    seen: list[dict[str, Any]] = []
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs(), "l2Book": _l2book(n_bids=25, n_asks=3)}, seen)
    provider = HyperliquidInfoProvider(_settings())

    result = await provider.fetch("hl_l2book", symbol="BTC-PERP", n_sig_figs=5)

    assert result["coin"] == "BTC"
    assert len(result["bids"]) == 20  # top 20 of the 25 supplied
    assert len(result["asks"]) == 3
    assert result["bids"][0] == [61635.0, 1.0]
    assert result["asks"][0] == [61636.0, 2.0]
    # as_of comes from the book's own ms timestamp.
    assert result["as_of"].startswith("2026-")
    book_bodies = [b for b in seen if b.get("type") == "l2Book"]
    assert book_bodies == [{"type": "l2Book", "coin": "BTC", "nSigFigs": 5}]


@respx.mock
async def test_l2book_resolution_reuses_memoized_universe() -> None:
    seen: list[dict[str, Any]] = []
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs(), "l2Book": _l2book()}, seen)
    provider = HyperliquidInfoProvider(_settings())

    await provider.fetch("hl_l2book", symbol="BTC")
    await provider.fetch("hl_l2book", symbol="BTC")

    meta_calls = [b for b in seen if b.get("type") == "metaAndAssetCtxs"]
    assert len(meta_calls) == 1  # second resolve hits the memoized universe


# ===========================================================================
# hl_funding_history
# ===========================================================================


@respx.mock
async def test_funding_history_normalizes_hourly_rates() -> None:
    seen: list[dict[str, Any]] = []
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs(), "fundingHistory": _FUNDING_HISTORY}, seen)
    provider = HyperliquidInfoProvider(_settings())

    result = await provider.fetch("hl_funding_history", symbol="BTCUSDT", hours=48)

    assert result["coin"] == "BTC"
    assert [r["rate"] for r in result["rates"]] == [
        pytest.approx(0.0000115313),
        pytest.approx(0.0000118977),
        pytest.approx(0.0000125),
    ]
    assert all(r["time"].endswith("+00:00") for r in result["rates"])
    body = next(b for b in seen if b.get("type") == "fundingHistory")
    assert body["coin"] == "BTC"
    assert isinstance(body["startTime"], int)


@respx.mock
async def test_funding_history_empty_payload_raises() -> None:
    _mock_info({"metaAndAssetCtxs": _meta_and_ctxs(), "fundingHistory": []})
    provider = HyperliquidInfoProvider(_settings())

    with pytest.raises(ValueError):
        await provider.fetch("hl_funding_history", symbol="ETH")


# ===========================================================================
# hl_market_pulse + new-listing snapshot
# ===========================================================================


@respx.mock
async def test_market_pulse_first_run_writes_snapshot_and_skips_delisted(
    tmp_path: Path,
) -> None:
    payloads = {"metaAndAssetCtxs": _meta_and_ctxs()}
    _mock_info(payloads)
    provider = HyperliquidInfoProvider(_settings(cache_dir=tmp_path))

    result = await provider.fetch("hl_market_pulse")

    # First run: no snapshot existed, so nothing can be called "new".
    assert result["new_listings"] == []
    coins = [a["coin"] for a in result["assets"]]
    assert coins == ["BTC", "ETH"]  # MATIC isDelisted -> excluded from assets
    btc = result["assets"][0]
    assert btc["mark_price"] == 61628.0
    assert btc["day_change_pct"] == pytest.approx((61628.0 / 62675.0 - 1.0) * 100.0)
    assert btc["open_interest_usd"] == pytest.approx(32097.10012 * 61628.0)
    assert btc["funding_annualized"] == pytest.approx(0.000000888 * 24 * 365)
    assert btc["max_leverage"] == 40.0
    snapshot = json.loads((tmp_path / "hl_universe_snapshot.json").read_text(encoding="utf-8"))
    # Snapshot keeps ALL names (incl. delisted) so a delisting never re-flags.
    assert snapshot["names"] == ["BTC", "MATIC", "ETH"]


@respx.mock
async def test_market_pulse_second_run_detects_new_listing(tmp_path: Path) -> None:
    payloads: dict[str, Any] = {"metaAndAssetCtxs": _meta_and_ctxs()}
    _mock_info(payloads)
    provider = HyperliquidInfoProvider(_settings(cache_dir=tmp_path))

    first = await provider.fetch("hl_market_pulse")
    assert first["new_listings"] == []

    payloads["metaAndAssetCtxs"] = _meta_and_ctxs(extra_coin="WIF")
    second = await provider.fetch("hl_market_pulse")

    assert second["new_listings"] == ["WIF"]
    assert "WIF" in [a["coin"] for a in second["assets"]]
    # Snapshot advanced: a third call sees nothing new.
    third = await provider.fetch("hl_market_pulse")
    assert third["new_listings"] == []


# ===========================================================================
# Settings / declaration sanity
# ===========================================================================


def test_provider_is_keyless_and_declares_swarm_capabilities() -> None:
    assert HyperliquidInfoProvider.requires_key is None
    assert HyperliquidInfoProvider.supported == {
        "hl_asset_ctx",
        "hl_predicted_funding",
        "hl_l2book",
        "hl_funding_history",
        "hl_market_pulse",
    }
    assert HyperliquidInfoProvider.rate_per_min == 60


@respx.mock
async def test_base_url_honors_settings_attribute_when_present() -> None:
    settings = _settings()
    # Simulate the integrator-wired Settings field without requiring it to exist.
    settings.hyperliquid_info_url = "https://example.test/info"  # type: ignore[attr-defined]
    route = respx.post("https://example.test/info").mock(
        return_value=httpx.Response(200, json=_meta_and_ctxs())
    )
    provider = HyperliquidInfoProvider(settings)

    result = await provider.fetch("hl_asset_ctx", symbol="ETH")

    assert route.called
    assert result["coin"] == "ETH"
