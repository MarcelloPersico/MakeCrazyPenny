"""Offline tests for the decision-driven paper-trading orchestration (CONTRACT.md §17).

Monkeypatches ``decide_crypto`` and passes a fake client, so the suite exercises
the sizing/translation logic without the SDK, network, or a real decision run.
"""

from __future__ import annotations

from typing import Any

import pytest

import makecrazypenny.orchestration.paper_trade as pt
from makecrazypenny.execution.hyperliquid import ExecutionError


class FakeDecision:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, equity: float = 1000.0, tradable: float | None = None) -> None:
        self.equity = equity
        self.tradable = tradable
        self.opened: list[dict[str, Any]] = []
        self.raise_on_open: Exception | None = None

    def account_state(self) -> dict[str, Any]:
        state = {"network": "testnet", "account_value": self.equity, "positions": []}
        if self.tradable is not None:
            state["tradable_usdc"] = self.tradable
        return state

    def open_position(self, symbol: str, side: str, **kwargs: Any) -> dict[str, Any]:
        if self.raise_on_open is not None:
            raise self.raise_on_open
        call = {"symbol": symbol, "side": side, **kwargs}
        self.opened.append(call)
        return {"coin": "BTC", "side": "BUY", "size": 0.01, "result": {"status": "ok"}}

    def close_position(self, symbol: str, size: Any = None) -> dict[str, Any]:
        return {"coin": "BTC", "action": "close", "result": {"status": "ok"}}


def _patch_decision(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def _fake_decide(symbol: str, **kwargs: Any) -> FakeDecision:
        return FakeDecision(payload)

    monkeypatch.setattr(pt, "decide_crypto", _fake_decide)


async def test_open_from_decision_sizes_from_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {
            "action": "BUY",
            "direction": "LONG",
            "leverage": {"direction": "LONG", "notional_pct": 0.5, "suggested_leverage": 5},
        },
    )
    client = FakeClient(equity=1000.0)
    res = await pt.open_from_decision("BTC", client=client)

    assert res["placed"] is True
    assert len(client.opened) == 1
    call = client.opened[0]
    # notional = equity (1000) * notional_pct (0.5) = 500; leverage from the plan
    assert call["notional_usd"] == 500.0
    assert call["leverage"] == 5 and call["side"] == "LONG"
    assert res["sizing"]["notional_usd"] == 500.0
    assert "account_after" in res


async def test_notional_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {"action": "BUY", "direction": "LONG", "leverage": {"direction": "LONG", "notional_pct": 0.5, "suggested_leverage": 3}},
    )
    client = FakeClient(equity=1000.0)
    res = await pt.open_from_decision("BTC", notional_usd=250, client=client)
    assert client.opened[0]["notional_usd"] == 250.0
    assert res["sizing"]["notional_usd"] == 250.0


async def test_sizes_against_spot_when_perp_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    # Perp equity is $0 (funds sit in spot); tradable_usdc reflects spot collateral.
    _patch_decision(
        monkeypatch,
        {"action": "BUY", "direction": "LONG", "leverage": {"direction": "LONG", "notional_pct": 0.5, "suggested_leverage": 5}},
    )
    client = FakeClient(equity=0.0, tradable=500.0)
    res = await pt.open_from_decision("BTC", client=client)
    assert res["placed"] is True
    assert client.opened[0]["notional_usd"] == 250.0  # 500 * 0.5, not 0 * 0.5


async def test_avoid_places_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {"action": "AVOID", "direction": "FLAT", "leverage": {"direction": "FLAT", "notional_pct": 0.0}},
    )
    client = FakeClient()
    res = await pt.open_from_decision("BTC", client=client)
    assert res["placed"] is False
    assert "AVOID" in res["reason"]
    assert client.opened == []
    assert "decision" in res  # analysis still returned


async def test_zero_equity_blocks_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {"action": "BUY", "direction": "LONG", "leverage": {"direction": "LONG", "notional_pct": 0.5, "suggested_leverage": 5}},
    )
    client = FakeClient(equity=0.0)
    res = await pt.open_from_decision("BTC", client=client)
    assert res["placed"] is False and "equity" in res["reason"]
    assert client.opened == []


async def test_open_error_surfaces_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {"action": "BUY", "direction": "LONG", "leverage": {"direction": "LONG", "notional_pct": 0.5, "suggested_leverage": 5}},
    )
    client = FakeClient(equity=1000.0)
    client.raise_on_open = ExecutionError("upstream rejected")
    res = await pt.open_from_decision("BTC", client=client)
    assert res["placed"] is False
    assert "error" in res["order"]


async def test_open_from_decision_attaches_plan_tpsl(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {
            "action": "BUY",
            "direction": "LONG",
            "leverage": {
                "direction": "LONG",
                "notional_pct": 0.5,
                "suggested_leverage": 5,
                "stop_price": 95.0,
                "target_price": 120.0,
            },
        },
    )
    client = FakeClient(equity=1000.0)
    res = await pt.open_from_decision("BTC", client=client)
    call = client.opened[0]
    # The plan's invalidation/target ride along as exchange-side TP/SL.
    assert call["stop_loss"] == 95.0 and call["take_profit"] == 120.0
    assert res["sizing"]["stop_loss"] == 95.0 and res["sizing"]["take_profit"] == 120.0


async def test_attach_tpsl_false_places_naked_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_decision(
        monkeypatch,
        {
            "action": "BUY",
            "direction": "LONG",
            "leverage": {
                "direction": "LONG",
                "notional_pct": 0.5,
                "suggested_leverage": 5,
                "stop_price": 95.0,
                "target_price": 120.0,
            },
        },
    )
    client = FakeClient(equity=1000.0)
    res = await pt.open_from_decision("BTC", attach_tpsl=False, client=client)
    call = client.opened[0]
    assert call["stop_loss"] is None and call["take_profit"] is None
    assert res["sizing"]["stop_loss"] is None


async def test_set_tpsl_wrapper_converts_error() -> None:
    class Boom:
        def set_position_tpsl(self, *a: Any, **k: Any) -> Any:
            raise ExecutionError("no key configured")

    res = await pt.set_tpsl("BTC", stop_loss=1.0, client=Boom())
    assert "error" in res and "no key" in res["error"]


async def test_available_pairs_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hl(*, settings: Any = None, force_refresh: bool = False) -> dict[str, Any]:
        return {"coins": ["BTC", "ETH"], "count": 2, "source": "live"}

    monkeypatch.setattr(pt, "fetch_hyperliquid_perps", fake_hl)
    res = await pt.available_pairs()
    assert res["coins"] == ["BTC", "ETH"] and res["source"] == "live"


async def test_manual_wrapper_converts_error_to_dict() -> None:
    class Boom:
        def account_state(self) -> Any:
            # Healthy-but-anonymous account: the risk gate evaluates (and
            # passes) without an address/equity to baseline against.
            return {"positions": []}

        def open_position(self, *a: Any, **k: Any) -> Any:
            raise ExecutionError("no key configured")

    res = await pt.open_manual("BTC", "LONG", size=0.01, client=Boom())
    assert "error" in res and "no key" in res["error"]


async def test_manual_refuses_ungated_when_account_read_fails() -> None:
    """A transient account-state failure must fail CLOSED, not place ungated."""

    placed: list[Any] = []

    class FlakyInfo:
        def account_state(self) -> Any:
            raise ExecutionError("info endpoint 503")

        def open_position(self, *a: Any, **k: Any) -> Any:
            placed.append(a)
            return {"status": "ok", "result": {"statuses": [{"filled": {"oid": 1}}]}}

    res = await pt.open_manual("BTC", "LONG", notional_usd=50, client=FlakyInfo())
    assert res["placed"] is False
    assert "could not reach" in res["reason"]
    assert placed == []
