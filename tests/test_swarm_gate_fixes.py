"""Regression tests for the adversarial-review fixes on the swarm trade path.

Covers the cross-agent seams the original suite could not see:

* the REAL ``analysis.risk.correlated_exposure_check`` driven through
  ``paper_trade._risk_gate`` with ``account_state()``-shaped positions
  (refusal and downsize branches — the gate was silently inert before);
* exchange-level per-order rejections (``statuses: [{"error": ...}]``)
  reported as ``placed: False`` with no phantom journal row;
* the manual path honoring the gate's ``scaled_notional``;
* ``performance()`` surfacing a fills outage instead of fabricating an
  all-unfilled scoreboard;
* the daily-loss baseline ignoring stale (pre-yesterday) snapshots;
* ``reconcile()`` window fallback never stealing a fill another decision
  owns by order id.

Offline: tmp cache dir, fake clients via the ``client=`` seam.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import makecrazypenny.analysis.risk as analysis_risk
import makecrazypenny.orchestration.journal as journal
import makecrazypenny.orchestration.paper_trade as pt
from makecrazypenny.core.config import Settings

_ADDRESS = "0x" + "33" * 20


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCP_CACHE_DIR", str(tmp_path))
    for var in ("MCP_SWARM_RISK_GATE", "MCP_SWARM_MAX_POSITIONS", "MCP_SWARM_MAX_DAILY_LOSS_PCT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()


def _account(positions: list[dict[str, Any]], equity: float) -> dict[str, Any]:
    return {
        "address": _ADDRESS,
        "tradable_usdc": equity,
        "account_value": equity,
        "positions": positions,
    }


def _hl_pos(coin: str, size: float, position_value: float) -> dict[str, Any]:
    """A position row shaped exactly like HyperliquidPaperClient.account_state()."""
    return {
        "coin": coin,
        "size": size,
        "entry_price": 1.0,
        "position_value": position_value,
        "unrealized_pnl": 0.0,
        "leverage": 5,
    }


# ---------------------------------------------------------------------------
# Real correlated_exposure_check through the real gate (was silently inert)
# ---------------------------------------------------------------------------


def test_risk_gate_real_correlated_check_refuses(settings: Settings) -> None:
    """$50k same-bucket candidate on a $1k account must be refused, not waved by."""
    assert hasattr(analysis_risk, "correlated_exposure_check")
    account = _account(
        [_hl_pos("ETH", 10.0, 25_000.0), _hl_pos("SOL", 100.0, 25_000.0)], equity=1_000.0
    )
    gate = pt._risk_gate(
        account, symbol="BTCUSDT", side="LONG", notional_usd=50_000.0, settings=settings
    )
    corr = gate["checks"]["correlated_exposure"]
    assert "skipped" not in corr, f"correlated check did not run: {corr}"
    assert gate["ok"] is False
    assert corr["allowed"] is False


def test_risk_gate_real_correlated_check_downsizes(settings: Settings) -> None:
    """Partial headroom must surface as a scaled_notional on the gate."""
    account = _account([_hl_pos("ETH", 10.0, 50_000.0)], equity=100_000.0)
    gate = pt._risk_gate(
        account, symbol="BTCUSDT", side="LONG", notional_usd=250_000.0, settings=settings
    )
    corr = gate["checks"]["correlated_exposure"]
    assert "skipped" not in corr, f"correlated check did not run: {corr}"
    assert gate["ok"] is True
    scaled = gate.get("scaled_notional")
    assert scaled is not None and 0 < scaled < 250_000.0


# ---------------------------------------------------------------------------
# Exchange-level per-order rejection (statuses: [{"error": ...}])
# ---------------------------------------------------------------------------


class RejectingClient:
    """Healthy account; the exchange rejects the order itself."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []

    def account_state(self) -> dict[str, Any]:
        return _account([], equity=1_000.0)

    def open_position(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.placed.append(k)
        return {
            "status": "ok",
            "reference_price": 100.0,
            "result": {"error": "Insufficient margin to place order."},
        }


def _no_journal_rows(tmp: Path) -> bool:
    f = tmp / "journal" / "decisions.jsonl"
    return not f.exists() or not f.read_text(encoding="utf-8").strip()


async def test_manual_exchange_rejection_not_placed_not_journaled(
    settings: Settings, tmp_path: Path
) -> None:
    client = RejectingClient()
    res = await pt.open_manual("BTC", "LONG", notional_usd=500, client=client, settings=settings)
    assert res["placed"] is False
    assert "exchange rejected" in res["reason"]
    assert _no_journal_rows(tmp_path)


async def test_decision_exchange_rejection_not_placed_not_journaled(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    payload = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "direction": "LONG",
        "leverage": {
            "direction": "LONG",
            "notional_pct": 0.5,
            "suggested_leverage": 5,
            "stop_price": 95.0,
            "target_price": 120.0,
        },
    }

    class FakeDecision:
        def to_dict(self) -> dict[str, Any]:
            return payload

    async def _fake_decide(symbol: str, **kwargs: Any) -> FakeDecision:
        return FakeDecision()

    monkeypatch.setattr(pt, "decide_crypto", _fake_decide)
    client = RejectingClient()
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is False
    assert "exchange rejected" in res["reason"]
    assert "journal" not in res
    assert _no_journal_rows(tmp_path)


# ---------------------------------------------------------------------------
# Manual path honors the gate's downsize
# ---------------------------------------------------------------------------


class RecordingClient:
    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self._positions = positions

    def account_state(self) -> dict[str, Any]:
        return _account(self._positions, equity=1_000.0)

    def open_position(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.kwargs.append(k)
        return {
            "status": "ok",
            "reference_price": 100.0,
            "result": {"statuses": [{"filled": {"oid": 41}}]},
        }


async def test_manual_applies_scaled_notional(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(
        analysis_risk,
        "correlated_exposure_check",
        lambda *a, **k: {"allowed": True, "scaled_notional": 100.0, "reason": "headroom"},
    )
    client = RecordingClient([_hl_pos("ETH", 1.0, 500.0)])
    res = await pt.open_manual("BTC", "LONG", notional_usd=500, client=client, settings=settings)
    assert res["placed"] is True
    assert client.kwargs[0]["notional_usd"] == 100.0


# ---------------------------------------------------------------------------
# performance(): a fills outage must not fabricate an all-unfilled scoreboard
# ---------------------------------------------------------------------------


class FillsOutageClient:
    def account_state(self) -> dict[str, Any]:
        return _account([], equity=1_000.0)

    def recent_fills(self, limit: int = 200) -> Any:
        raise RuntimeError("HTTP 429 rate limited")


async def test_performance_surfaces_fills_outage(settings: Settings) -> None:
    journal.record_decision(
        {"symbol": "BTCUSDT", "action": "BUY", "direction": "LONG"},
        {"source": "test"},
        cloid="0x" + "ab" * 16,
        sizing=None,
        order_ids=[7],
        entry_price=100.0,
        interval="15m",
        address=_ADDRESS,
        settings=settings,
    )
    perf = await journal.performance(client=FillsOutageClient(), settings=settings)
    assert "_error" in perf and "fills unavailable" in perf["_error"]
    assert "n_unfilled" not in perf  # no fabricated reconcile stats


# ---------------------------------------------------------------------------
# Daily-loss baseline: stale snapshots must not anchor today
# ---------------------------------------------------------------------------


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_daily_loss_baseline_ignores_stale_snapshot() -> None:
    now_ms = _ms("2026-06-10T12:00:00")
    stale = [{"ts_ms": _ms("2026-06-05T22:00:00"), "tradable_usdc": 1000.0}]
    assert journal.daily_loss_baseline(stale, now_ms=now_ms) is None

    fresh = stale + [{"ts_ms": _ms("2026-06-09T23:00:00"), "tradable_usdc": 800.0}]
    base = journal.daily_loss_baseline(fresh, now_ms=now_ms)
    assert base is not None and base["value"] == 800.0 and base["kind"] == "pre_midnight"


# ---------------------------------------------------------------------------
# reconcile(): window fallback may not steal an id-owned fill
# ---------------------------------------------------------------------------


def test_reconcile_window_fallback_skips_id_owned_fills() -> None:
    t0 = _ms("2026-06-10T10:00:00")
    fills = [
        {"coin": "BTC", "side": "BUY", "oid": 777, "time": t0 + 60_000, "price": 100.0, "size": 1.0}
    ]
    older_no_ids = {
        "id": "older",
        "symbol": "BTCUSDT",
        "coin": "BTC",
        "direction": "LONG",
        "ts_ms": t0,
        "order_ids": [],
    }
    newer_owns_fill = {
        "id": "newer",
        "symbol": "BTCUSDT",
        "coin": "BTC",
        "direction": "LONG",
        "ts_ms": t0 + 30_000,
        "order_ids": [777],
    }
    rec = journal.reconcile(fills, [older_no_ids, newer_owns_fill])
    by_id = {r["id"]: r for r in rec["decisions"]}
    assert by_id["newer"]["matched_by"] == "oid" and by_id["newer"]["filled"] is True
    assert by_id["older"]["outcome"] == "unfilled"
