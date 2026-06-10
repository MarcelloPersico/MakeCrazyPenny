"""Offline tests for the Hyperliquid testnet execution client (CONTRACT.md §17).

The SDK is never required: every test monkeypatches the ``_build_clients`` seam
with in-memory fakes, so nothing imports ``hyperliquid``/``eth_account`` or hits
the network. Covers symbol->coin mapping, size/price rounding, the $10 min
notional, side handling, leverage capping, response parsing, the testnet lock,
and the missing-key / missing-SDK errors.
"""

from __future__ import annotations

import importlib.util

import pytest

import makecrazypenny.execution.hyperliquid as hx
from makecrazypenny.core.config import Settings
from makecrazypenny.execution.hyperliquid import ExecutionError, HyperliquidPaperClient

_ADDRESS = "0x" + "11" * 20


class FakeInfo:
    """Minimal stand-in for the SDK ``Info`` client."""

    def __init__(self) -> None:
        self.universe = [
            {"name": "BTC", "szDecimals": 3, "maxLeverage": 50},
            {"name": "ETH", "szDecimals": 2, "maxLeverage": 50},
            {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10},
        ]
        self.mids = {"BTC": "60000.0", "ETH": "3000.0", "DOGE": "0.1"}

    def meta(self) -> dict:
        return {"universe": self.universe}

    def all_mids(self) -> dict:
        return dict(self.mids)

    def user_state(self, address: str) -> dict:
        return {
            "marginSummary": {"accountValue": "1000.0", "totalMarginUsed": "120.0", "totalNtlPos": "600.0"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.01",
                        "entryPx": "60000",
                        "positionValue": "600",
                        "unrealizedPnl": "5",
                        "leverage": {"type": "cross", "value": 5},
                        "liquidationPx": "50000",
                        "marginUsed": "120",
                    }
                }
            ],
            "withdrawable": "880.0",
        }

    def spot_user_state(self, address: str) -> dict:
        return {"balances": [{"coin": "USDC", "total": "500.0", "hold": "0.0"}]}

    def open_orders(self, address: str) -> list:
        return [{"coin": "BTC", "oid": 123, "side": "B", "sz": "0.01", "limitPx": "59000", "timestamp": 1}]

    def user_fills(self, address: str) -> list:
        return [
            {"coin": "BTC", "side": "B", "px": "60000", "sz": "0.01", "closedPnl": "0", "fee": "0.1", "oid": 123, "time": 1},
            {"coin": "ETH", "side": "A", "px": "3000", "sz": "0.1", "closedPnl": "2", "fee": "0.05", "oid": 124, "time": 2},
        ]


class FakeExchange:
    """Records calls and returns a canned ``ok`` response."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok"}

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_open", name, is_buy, sz, px, slippage))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1, "totalSz": str(sz)}}]}}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type, reduce_only))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 2}}]}}}

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_close", coin, sz, px, slippage))
        return {"status": "ok"}

    def bulk_orders(self, order_requests, builder=None, grouping="na"):
        self.calls.append(("bulk_orders", order_requests, grouping))
        statuses = [{"resting": {"oid": 100 + i}} for i in range(len(order_requests))]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        return {"status": "ok"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> HyperliquidPaperClient:
    info, exchange = FakeInfo(), FakeExchange()
    monkeypatch.setattr(hx, "_build_clients", lambda settings: (info, exchange, _ADDRESS))
    c = HyperliquidPaperClient(Settings())
    c._fake_info, c._fake_exchange = info, exchange  # type: ignore[attr-defined]
    return c


def _exchange(c: HyperliquidPaperClient) -> FakeExchange:
    return c._fake_exchange  # type: ignore[attr-defined]


def test_open_market_maps_coin_sizes_and_sets_leverage(client: HyperliquidPaperClient) -> None:
    res = client.open_position("BTCUSDT", "LONG", notional_usd=600, leverage=5)
    calls = _exchange(client).calls
    assert ("update_leverage", 5, "BTC", True) in calls
    mo = [c for c in calls if c[0] == "market_open"][0]
    # name=BTC, is_buy=True, sz=600/60000=0.01 rounded to 3 dp
    assert mo[1] == "BTC" and mo[2] is True and mo[3] == 0.01
    assert res["coin"] == "BTC" and res["side"] == "BUY" and res["size"] == 0.01
    assert res["result"]["status"] == "ok"


def test_size_rounds_to_sz_decimals(client: HyperliquidPaperClient) -> None:
    res = client.open_position("ETH", "LONG", notional_usd=100)  # 100/3000 = 0.0333 -> 0.03
    assert res["size"] == 0.03


def test_short_is_sell(client: HyperliquidPaperClient) -> None:
    res = client.open_position("BTC", "SHORT", size=0.01)
    mo = [c for c in _exchange(client).calls if c[0] == "market_open"][0]
    assert mo[2] is False and res["side"] == "SELL"


def test_invalid_side_raises(client: HyperliquidPaperClient) -> None:
    with pytest.raises(ExecutionError):
        client.open_position("BTC", "sideways", size=0.01)


def test_min_notional_rejected(client: HyperliquidPaperClient) -> None:
    # DOGE @ 0.1, size 50 -> notional $5 < $10 minimum.
    with pytest.raises(ExecutionError, match="minimum"):
        client.open_position("DOGE", "LONG", size=50)


def test_size_rounds_to_zero_rejected(client: HyperliquidPaperClient) -> None:
    # BTC szDecimals=3, $5/60000 rounds to 0.000 -> rejected before min-notional.
    with pytest.raises(ExecutionError, match="zero"):
        client.open_position("BTC", "LONG", notional_usd=5)


def test_limit_order_uses_gtc_and_rounds_price(client: HyperliquidPaperClient) -> None:
    res = client.open_position("BTC", "LONG", size=0.01, order_type="limit", limit_price=59000, reduce_only=True)
    o = [c for c in _exchange(client).calls if c[0] == "order"][0]
    assert o[1] == "BTC" and o[3] == 0.01 and o[4] == 59000.0
    assert o[5] == {"limit": {"tif": "Gtc"}} and o[6] is True
    assert res["order_type"] == "limit"


def test_limit_order_requires_price(client: HyperliquidPaperClient) -> None:
    with pytest.raises(ExecutionError, match="limit_price"):
        client.open_position("BTC", "LONG", size=0.01, order_type="limit")


def test_unknown_coin_rejected(client: HyperliquidPaperClient) -> None:
    with pytest.raises(ExecutionError, match="unknown"):
        client.open_position("NOTACOINUSDT", "LONG", size=1)


def test_set_leverage_capped_to_max(client: HyperliquidPaperClient) -> None:
    res = client.set_leverage("BTC", 100)  # max 50
    assert res["leverage"] == 50
    assert ("update_leverage", 50, "BTC", True) in _exchange(client).calls


def test_account_state_parsed(client: HyperliquidPaperClient) -> None:
    state = client.account_state()
    assert state["network"] == "testnet" and state["account_value"] == 1000.0
    assert state["address"] == _ADDRESS
    # Spot USDC is surfaced and folded into a deployable total (perp + spot).
    assert state["spot_usdc"] == 500.0 and state["tradable_usdc"] == 1500.0
    pos = state["positions"][0]
    assert pos["coin"] == "BTC" and pos["leverage"] == 5.0 and pos["liquidation_price"] == 50000.0


def test_spot_hold_not_double_counted(client: HyperliquidPaperClient) -> None:
    """Spot USDC on ``hold`` backs open perp margin and already lives inside the
    perp ``accountValue``; only free spot (total - hold) may fold into
    ``tradable_usdc``, else sizing and the kill-switch baseline inflate by the
    margin in use (observed live 2026-06-10: ~$191 counted twice)."""
    info = client._fake_info  # type: ignore[attr-defined]
    info.spot_user_state = lambda address: {
        "balances": [{"coin": "USDC", "total": "992.11", "hold": "191.06"}]
    }
    state = client.account_state()
    assert state["spot_usdc"] == pytest.approx(801.05)
    assert state["tradable_usdc"] == pytest.approx(1000.0 + 801.05)


def test_open_orders_and_fills_side_mapping(client: HyperliquidPaperClient) -> None:
    assert client.open_orders()["open_orders"][0]["side"] == "BUY"
    fills = client.recent_fills(limit=1)["fills"]
    assert len(fills) == 1 and fills[0]["side"] == "BUY"


def test_set_position_tpsl_places_oco_triggers(client: HyperliquidPaperClient) -> None:
    # FakeInfo holds a 0.01 BTC LONG @ 60000 (mid 60000).
    res = client.set_position_tpsl("BTCUSDT", stop_loss=58000, take_profit=66000)
    bo = [c for c in _exchange(client).calls if c[0] == "bulk_orders"][0]
    reqs, grouping = bo[1], bo[2]
    assert grouping == "positionTpsl" and len(reqs) == 2
    sl, tp = reqs
    # Closing a long sells; both legs are reduce-only triggers for the full position.
    for req in (sl, tp):
        assert req["coin"] == "BTC" and req["is_buy"] is False
        assert req["reduce_only"] is True and req["sz"] == 0.01
    assert sl["order_type"]["trigger"] == {"triggerPx": 58000.0, "isMarket": True, "tpsl": "sl"}
    assert tp["order_type"]["trigger"]["tpsl"] == "tp"
    assert tp["order_type"]["trigger"]["triggerPx"] == 66000.0
    # The fired market leg's limit bounds slippage: a closing SELL floors at trigger*(1-0.05).
    assert sl["limit_px"] == 55100.0
    assert res["grouping"] == "positionTpsl" and set(res["legs"]) == {"stop_loss", "take_profit"}
    assert res["result"]["status"] == "ok"


def test_tpsl_wrong_side_rejected(client: HyperliquidPaperClient) -> None:
    # Long @ mid 60000: a "stop" above the market (or target below) would fire instantly.
    with pytest.raises(ExecutionError, match="immediately"):
        client.set_position_tpsl("BTC", stop_loss=61000)
    with pytest.raises(ExecutionError, match="immediately"):
        client.set_position_tpsl("BTC", take_profit=59000)
    assert not any(c[0] == "bulk_orders" for c in _exchange(client).calls)


def test_tpsl_needs_open_position(client: HyperliquidPaperClient) -> None:
    with pytest.raises(ExecutionError, match="no open"):
        client.set_position_tpsl("ETH", stop_loss=2900)


def test_tpsl_needs_at_least_one_leg(client: HyperliquidPaperClient) -> None:
    with pytest.raises(ExecutionError, match="stop_loss and/or take_profit"):
        client.set_position_tpsl("BTC")


def test_open_with_tpsl_attaches_after_fill(client: HyperliquidPaperClient) -> None:
    res = client.open_position("BTC", "SHORT", size=0.01, stop_loss=63000, take_profit=57000)
    bo = [c for c in _exchange(client).calls if c[0] == "bulk_orders"][0]
    # Closing a short buys back; legs are sized to the entry fill (totalSz).
    assert all(r["is_buy"] is True and r["sz"] == 0.01 for r in bo[1])
    assert res["tpsl"]["side"] == "BUY" and res["tpsl"]["result"]["status"] == "ok"


def test_open_limit_resting_skips_tpsl(client: HyperliquidPaperClient) -> None:
    res = client.open_position(
        "BTC", "LONG", size=0.01, order_type="limit", limit_price=59000, stop_loss=55000
    )
    assert "skipped" in res["tpsl"]
    assert not any(c[0] == "bulk_orders" for c in _exchange(client).calls)


def test_open_with_bad_tpsl_keeps_entry_and_reports_error(client: HyperliquidPaperClient) -> None:
    # Entry fills, but the stop sits on the wrong side: surface the error, never void the fill.
    res = client.open_position("BTC", "LONG", size=0.01, stop_loss=66000)
    assert res["result"]["status"] == "ok"
    assert "immediately" in res["tpsl"]["error"]


def test_close_and_cancel(client: HyperliquidPaperClient) -> None:
    client.close_position("BTC")
    client.cancel_order("BTC", 123)
    calls = _exchange(client).calls
    assert any(c[0] == "market_close" and c[1] == "BTC" for c in calls)
    assert ("cancel", "BTC", 123) in calls


# -- testnet lock + config errors (no fake; real _build_clients) ----------------


def test_testnet_url_is_the_only_base() -> None:
    s = Settings()
    assert s.hyperliquid_testnet_url == "https://api.hyperliquid-testnet.xyz"
    # There is intentionally no mainnet URL field anywhere on Settings.
    assert not any("mainnet" in name.lower() for name in vars(s))


def test_missing_key_raises_actionable_error() -> None:
    # No SDK import path reached only if SDK absent raises first; guard on that.
    if importlib.util.find_spec("hyperliquid") is None:
        pytest.skip("SDK absent: missing-SDK hint covers this path")
    with pytest.raises(ExecutionError, match="MCP_HL_PRIVATE_KEY"):
        hx._build_clients(Settings(hl_private_key=None))


def test_missing_sdk_gives_install_hint() -> None:
    if importlib.util.find_spec("hyperliquid") is not None:
        pytest.skip("SDK installed: missing-SDK path not reachable")
    with pytest.raises(ExecutionError, match="trade"):
        hx._build_clients(Settings(hl_private_key="0x" + "ab" * 32))
