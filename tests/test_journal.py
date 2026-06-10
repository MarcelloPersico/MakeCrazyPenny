"""Offline tests for the trade journal + the pre-placement risk gate (swarm upgrade).

Everything runs against a tmp cache dir (``MCP_CACHE_DIR`` monkeypatched) with
fake clients injected through the ``client=`` seam — no SDK, no network, no
real wallet. Covers: append/read tolerance (corrupt lines skipped), reconcile
matching (cloid -> oid -> symbol+time-window fallback) and round-trip PnL /
R-multiple / win-loss scoring, the performance aggregation (incl. the
no-key ``{"_error": ...}`` path), every risk-gate branch (same-direction
position cap + same-coin add exemption, kill-switch breach + baseline
bootstrap, correlated-exposure delegation/scaling/absence, env overrides, the
``off`` switch), and the journaling-failure-never-blocks guarantee.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import makecrazypenny.analysis.risk as analysis_risk
import makecrazypenny.orchestration.journal as journal
import makecrazypenny.orchestration.paper_trade as pt
from makecrazypenny.core.config import Settings

_ADDRESS = "0x" + "22" * 20


@pytest.fixture(autouse=True)
def _journal_env(hermetic_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every test: tmp cache dir, no swarm env knobs leaking in.

    ``correlated_exposure_check`` is also removed by default (it is built in a
    parallel track): gate tests that are not about correlation must not depend
    on whether (or how) it happens to be implemented. The correlation tests
    install their own fakes on top of this.
    """
    monkeypatch.setenv("MCP_CACHE_DIR", str(tmp_path))
    for var in ("MCP_SWARM_RISK_GATE", "MCP_SWARM_MAX_POSITIONS", "MCP_SWARM_MAX_DAILY_LOSS_PCT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delattr(analysis_risk, "correlated_exposure_check", raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()  # cache_dir = the tmp MCP_CACHE_DIR


def _decision_payload(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "direction": "LONG",
        "conviction": 0.7,
        "summary": "test verdict",
        "leverage": {
            "direction": "LONG",
            "notional_pct": 0.5,
            "suggested_leverage": 5,
            "stop_price": 95.0,
            "target_price": 120.0,
        },
    }
    payload.update(over)
    return payload


class FakeDecision:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


def _patch_decision(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def _fake_decide(symbol: str, **kwargs: Any) -> FakeDecision:
        return FakeDecision(payload)

    monkeypatch.setattr(pt, "decide_crypto", _fake_decide)


def _pos(coin: str, size: float) -> dict[str, Any]:
    return {"coin": coin, "size": size, "entry_price": 1.0, "unrealized_pnl": 0.0}


class FakeClient:
    """Paper-client fake with an address, positions, and canned fills."""

    def __init__(
        self,
        *,
        equity: float = 1000.0,
        positions: list[dict[str, Any]] | None = None,
        fills: list[dict[str, Any]] | None = None,
        address: str | None = _ADDRESS,
    ) -> None:
        self.equity = equity
        self.positions = list(positions or [])
        self.fills = list(fills or [])
        self.address = address
        self.opened: list[dict[str, Any]] = []

    def account_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "network": "testnet",
            "account_value": self.equity,
            "tradable_usdc": self.equity,
            "positions": list(self.positions),
        }
        if self.address is not None:
            state["address"] = self.address
        return state

    def open_position(self, symbol: str, side: str, **kwargs: Any) -> dict[str, Any]:
        self.opened.append({"symbol": symbol, "side": side, **kwargs})
        return {
            "coin": "BTC",
            "side": "BUY" if str(side).upper() in ("LONG", "BUY") else "SELL",
            "size": 0.01,
            "reference_price": 100.0,
            "result": {"status": "ok", "statuses": [{"filled": {"oid": 11, "totalSz": "0.01"}}]},
        }

    def recent_fills(self, limit: int = 20) -> dict[str, Any]:
        return {"fills": list(self.fills)[: max(1, int(limit))]}


def _fill(
    coin: str,
    side: str,
    price: float,
    size: float,
    *,
    closed_pnl: float = 0.0,
    fee: float = 0.0,
    oid: Any = None,
    time: int = 0,
    cloid: str | None = None,
) -> dict[str, Any]:
    f: dict[str, Any] = {
        "coin": coin,
        "side": side,
        "price": price,
        "size": size,
        "closed_pnl": closed_pnl,
        "fee": fee,
        "oid": oid,
        "time": time,
    }
    if cloid is not None:
        f["cloid"] = cloid
    return f


def _midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _write_equity_line(
    settings: Settings, *, ts_ms: int, value: float, address: str | None = _ADDRESS
) -> None:
    path = settings.resolve_cache_dir() / "journal"
    path.mkdir(parents=True, exist_ok=True)
    when = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    entry = {
        "ts_ms": ts_ms,
        "date": when.strftime("%Y-%m-%d"),
        "address": address,
        "tradable_usdc": value,
        "account_value": value,
    }
    with (path / "equity.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Journal basics: append-only writers + tolerant readers
# ---------------------------------------------------------------------------


def test_record_decision_shape_and_corrupt_lines_skipped(settings: Settings) -> None:
    e1 = journal.record_decision(
        _decision_payload(),
        {"scout": "hype up", "news": "neutral"},
        cloid="0x" + "ab" * 16,
        order_ids=[5],
        entry_price=100.0,
        interval="15m",
        address=_ADDRESS,
        settings=settings,
    )
    assert e1["journaled"] is True
    assert len(e1["id"]) == 32 and e1["symbol"] == "BTCUSDT" and e1["coin"] == "BTC"
    assert e1["stop"] == 95.0 and e1["target"] == 120.0 and e1["leverage"] == 5
    assert e1["entry"] == 100.0 and e1["cloid"].startswith("0x")
    assert e1["interval"] == "15m" and e1["conviction"] == 0.7
    assert e1["context"]["scout"] == "hype up"
    # Corrupt + non-object lines must be skipped, never poison the reader.
    path = settings.resolve_cache_dir() / "journal" / "decisions.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write('"just a string"\n')
    journal.record_decision(_decision_payload(symbol="ETHUSDT"), settings=settings)
    rows = journal.read_decisions(settings=settings)
    assert len(rows) == 2
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]


def test_record_cycle_snapshot_and_recent(settings: Settings) -> None:
    journal.record_cycle({"goal": "find momentum", "symbols": ["BTCUSDT"]}, settings=settings)
    snap = journal.snapshot_equity(
        {
            "address": _ADDRESS,
            "account_value": 900.0,
            "tradable_usdc": 1000.0,
            "positions": [_pos("BTC", 0.01)],
        },
        settings=settings,
    )
    assert snap["journaled"] is True and snap["tradable_usdc"] == 1000.0
    assert snap["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert snap["n_positions"] == 1
    out = journal.recent(5, settings=settings)
    assert out["cycles"][0]["goal"] == "find momentum"
    assert out["equity"][0]["address"] == _ADDRESS
    assert out["decisions"] == []  # missing file reads as empty, not an error
    assert "disclaimer" in out


def test_digest_empty_journal(settings: Settings) -> None:
    d = journal.digest(settings=settings)
    assert d["goal"] is None and d["goal_updated_at"] is None
    assert d["cycles_since_review"] == 0
    assert d["last_review"] is None
    assert d["cycles"] == [] and d["decisions"] == [] and d["equity"] == []
    assert d["counts"] == {"cycles": 0, "decisions": 0}
    assert "disclaimer" in d


def test_digest_counts_cycles_since_review_and_drops_leg_verbosity(settings: Settings) -> None:
    journal.goal_set("core || STRATEGY @ t0: bias=long", settings=settings)
    journal.record_cycle({"summary": "memo r0", "kind": journal.REVIEW_KIND}, settings=settings)
    for i in range(3):
        journal.record_cycle(
            {"summary": f"c{i}", "action": "none", "scout": "x" * 500, "news": "y" * 500},
            settings=settings,
        )
    d = journal.digest(settings=settings)
    assert d["goal"] == "core || STRATEGY @ t0: bias=long"
    assert d["cycles_since_review"] == 3
    assert d["last_review"]["summary"] == "memo r0"
    # The verbose per-leg fields are dropped by design (context budget).
    assert all("scout" not in row and "news" not in row for row in d["cycles"])
    # A fresh review resets the counter to 0.
    journal.record_cycle({"summary": "memo r1", "kind": journal.REVIEW_KIND}, settings=settings)
    d2 = journal.digest(settings=settings)
    assert d2["cycles_since_review"] == 0
    assert d2["last_review"]["summary"] == "memo r1"


def test_digest_without_review_counts_all_cycles(settings: Settings) -> None:
    for i in range(5):
        journal.record_cycle({"summary": f"c{i}"}, settings=settings)
    assert journal.digest(settings=settings)["cycles_since_review"] == 5


def test_digest_clips_fields_and_bounds_rows(settings: Settings) -> None:
    journal.record_cycle(
        {"summary": "s" * 500, "thesis": "t" * 500, "kind": journal.REVIEW_KIND},
        settings=settings,
    )
    for i in range(9):
        journal.record_cycle({"summary": f"c{i}", "symbol": "BTCUSDT"}, settings=settings)
    journal.record_decision(
        _decision_payload(summary="d" * 500), cloid="0xabc", entry_price=100.0, settings=settings
    )
    journal.snapshot_equity(
        {"address": _ADDRESS, "account_value": 900.0, "tradable_usdc": 1000.0, "positions": []},
        settings=settings,
    )
    d = journal.digest(n_cycles=2, n_decisions=8, settings=settings)
    assert len(d["cycles"]) == 2 and d["counts"]["cycles"] == 10
    assert [c["summary"] for c in d["cycles"]] == ["c7", "c8"]
    # The review entry is older than the n_cycles window but still surfaces.
    assert len(d["last_review"]["summary"]) == 400 and d["last_review"]["summary"].endswith("...")
    dec = d["decisions"][0]
    assert dec["symbol"] == "BTCUSDT" and dec["cloid"] == "0xabc"
    assert dec["entry"] == 100.0 and dec["stop"] == 95.0 and dec["target"] == 120.0
    assert len(dec["summary"]) == 120 and dec["summary"].endswith("...")
    eq = d["equity"][-1]
    assert eq["tradable_usdc"] == 1000.0 and eq["n_positions"] == 0
    # Equity rows never carry the address (digest is single-account context).
    assert set(eq) == {"ts", "account_value", "tradable_usdc", "n_positions", "unrealized_pnl"}


def test_daily_loss_baseline_prefers_pre_midnight_snapshot() -> None:
    midnight = _midnight_ms()
    entries = [
        {"ts_ms": midnight - 7_200_000, "tradable_usdc": 980.0},
        {"ts_ms": midnight - 3_600_000, "tradable_usdc": 1000.0},
        {"ts_ms": midnight + 3_600_000, "tradable_usdc": 950.0},
    ]
    base = journal.daily_loss_baseline(entries)
    assert base["value"] == 1000.0 and base["kind"] == "pre_midnight"
    base2 = journal.daily_loss_baseline(entries[2:])
    assert base2["value"] == 950.0 and base2["kind"] == "first_of_today"
    assert journal.daily_loss_baseline([]) is None


# ---------------------------------------------------------------------------
# Reconcile: matching tiers + round-trip scoring
# ---------------------------------------------------------------------------


def test_reconcile_cloid_match_round_trip_win() -> None:
    dec = {
        "id": "d1",
        "symbol": "BTCUSDT",
        "coin": "BTC",
        "direction": "LONG",
        "cloid": "0xabc",
        "order_ids": [],
        "ts_ms": 1_000,
        "stop": 95.0,
    }
    fills = [
        _fill("BTC", "BUY", 100.0, 1.0, fee=0.1, oid=5, time=2_000, cloid="0xabc"),
        _fill("BTC", "SELL", 110.0, 1.0, closed_pnl=10.0, fee=0.1, oid=6, time=3_000),
    ]
    rec = journal.reconcile(fills, [dec])
    row = rec["decisions"][0]
    assert row["matched_by"] == "cloid" and row["filled"] is True
    assert row["outcome"] == "win" and row["realized_pnl"] == 10.0
    assert row["entry_price"] == 100.0 and row["entry_size"] == 1.0
    assert row["r_multiple"] == 2.0  # risk = |100 - 95| * 1.0 = 5; 10 / 5
    assert row["fees"] == 0.2
    assert rec["n_closed"] == 1 and rec["wins"] == 1 and rec["total_realized_pnl"] == 10.0


def test_reconcile_oid_match_short_loss_negative_r() -> None:
    dec = {
        "id": "d2",
        "symbol": "ETHUSDT",
        "coin": "ETH",
        "direction": "SHORT",
        "order_ids": [7],
        "ts_ms": 1_000,
        "stop": 102.0,
    }
    fills = [
        _fill("ETH", "SELL", 100.0, 2.0, oid=7, time=2_000),
        _fill("ETH", "BUY", 105.0, 2.0, closed_pnl=-10.0, fee=0.2, oid=8, time=3_000),
    ]
    rec = journal.reconcile(fills, [dec])
    row = rec["decisions"][0]
    assert row["matched_by"] == "oid" and row["outcome"] == "loss"
    assert row["realized_pnl"] == -10.0
    assert row["r_multiple"] == -2.5  # risk = |100 - 102| * 2 = 4; -10 / 4
    assert rec["losses"] == 1


def test_reconcile_window_fallback_and_unfilled() -> None:
    in_window = {"id": "d3", "symbol": "BTCUSDT", "direction": "LONG", "ts_ms": 1_000}
    too_late = {"id": "d4", "symbol": "DOGEUSDT", "direction": "LONG", "ts_ms": 1_000}
    fills = [
        # A pre-entry SELL must not be window-matched as a LONG entry (or exit).
        _fill("BTC", "SELL", 99.0, 1.0, oid=8, time=31_000),
        _fill("BTC", "BUY", 100.0, 1.0, oid=9, time=61_000),
        # DOGE fill lands 7h after the decision: outside the 6h window.
        _fill("DOGE", "BUY", 0.1, 100.0, oid=10, time=1_000 + int(7 * 3600 * 1000)),
    ]
    rec = journal.reconcile(fills, [in_window, too_late])
    by_id = {r["id"]: r for r in rec["decisions"]}
    assert by_id["d3"]["matched_by"] == "window" and by_id["d3"]["outcome"] == "open"
    assert by_id["d3"]["exit_size"] == 0.0  # the earlier SELL predates the entry
    assert by_id["d4"]["matched_by"] is None and by_id["d4"]["outcome"] == "unfilled"
    assert rec["n_unfilled"] == 1 and rec["n_open"] == 1


def test_reconcile_partial_exit_stays_open() -> None:
    dec = {
        "id": "d5",
        "symbol": "BTCUSDT",
        "coin": "BTC",
        "direction": "LONG",
        "cloid": "0xff",
        "ts_ms": 1_000,
        "stop": 95.0,
    }
    fills = [
        _fill("BTC", "BUY", 100.0, 1.0, oid=5, time=2_000, cloid="0xff"),
        _fill("BTC", "SELL", 104.0, 0.4, closed_pnl=1.6, oid=6, time=3_000),
    ]
    rec = journal.reconcile(fills, [dec])
    row = rec["decisions"][0]
    assert row["outcome"] == "open" and row["filled"] is True
    assert row["exit_size"] == 0.4 and row["realized_pnl"] == 1.6
    assert row["r_multiple"] is None
    assert rec["n_closed"] == 0 and rec["n_open"] == 1


# ---------------------------------------------------------------------------
# Performance aggregation
# ---------------------------------------------------------------------------


async def test_performance_aggregates(settings: Settings) -> None:
    journal.record_decision(
        _decision_payload(),
        {"scout": "hype"},
        cloid="0x" + "cd" * 16,
        order_ids=[5],
        entry_price=100.0,
        address=_ADDRESS,
        settings=settings,
    )
    journal.snapshot_equity(
        {"address": _ADDRESS, "account_value": 1000.0, "tradable_usdc": 1000.0, "positions": []},
        settings=settings,
    )
    journal.snapshot_equity(
        {"address": _ADDRESS, "account_value": 1010.0, "tradable_usdc": 1010.0, "positions": []},
        settings=settings,
    )
    fills = [  # newest-first, as recent_fills returns them; reconcile sorts
        _fill("BTC", "SELL", 110.0, 1.0, closed_pnl=10.0, fee=0.1, oid=6, time=3_000),
        _fill("BTC", "BUY", 100.0, 1.0, fee=0.1, oid=5, time=2_000),
    ]
    client = FakeClient(equity=1010.0, positions=[_pos("ETH", 0.5)], fills=fills)
    perf = await journal.performance(settings=settings, client=client)
    assert perf["n_closed"] == 1 and perf["hit_rate"] == 1.0
    assert perf["avg_R"] == 2.0 and perf["total_realized_pnl"] == 10.0
    assert perf["by_symbol"]["BTCUSDT"]["wins"] == 1
    assert perf["by_symbol"]["BTCUSDT"]["realized_pnl"] == 10.0
    assert perf["open_positions_marked"][0]["coin"] == "ETH"
    assert len(perf["equity_curve_tail"]) == 2
    assert "disclaimer" in perf


async def test_performance_without_key_returns_error_marker(settings: Settings) -> None:
    # No client injected and no MCP_HL_PRIVATE_KEY: the account read fails
    # (missing key or missing SDK), and performance degrades to {"_error": ...}.
    perf = await journal.performance(settings=settings)
    assert "_error" in perf and "disclaimer" in perf


# ---------------------------------------------------------------------------
# Risk gate: position cap
# ---------------------------------------------------------------------------


async def test_gate_refuses_fourth_same_direction_position(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient(positions=[_pos("ETH", 1.0), _pos("SOL", 2.0), _pos("DOGE", 3.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is False
    assert "positions" in res["reason"]
    assert res["risk_gate"]["ok"] is False
    assert res["risk_gate"]["checks"]["max_positions"]["passed"] is False
    assert client.opened == []


async def test_gate_allows_adding_to_existing_same_coin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient(positions=[_pos("BTC", 1.0), _pos("SOL", 2.0), _pos("DOGE", 3.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True and len(client.opened) == 1
    assert res["risk_gate"]["checks"]["max_positions"]["adding_to_existing"] is True


async def test_gate_ignores_opposite_direction_positions(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient(positions=[_pos("ETH", -1.0), _pos("SOL", -2.0), _pos("DOGE", -3.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True
    assert res["risk_gate"]["checks"]["max_positions"]["open_same_direction"] == 0


async def test_gate_max_positions_env_override(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("MCP_SWARM_MAX_POSITIONS", "1")
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient(positions=[_pos("ETH", 1.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is False and "1" in res["reason"]
    assert client.opened == []


async def test_gate_off_env_disables_everything(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("MCP_SWARM_RISK_GATE", "off")
    _patch_decision(monkeypatch, _decision_payload())
    # Both a kill-switch breach (50% daily loss) and a full position book...
    _write_equity_line(settings, ts_ms=_midnight_ms() - 3_600_000, value=1000.0)
    client = FakeClient(
        equity=500.0, positions=[_pos("ETH", 1.0), _pos("SOL", 2.0), _pos("DOGE", 3.0)]
    )
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    # ...are waved through when the gate is explicitly (dangerously) off.
    assert res["placed"] is True
    assert res["risk_gate"]["enabled"] is False


# ---------------------------------------------------------------------------
# Risk gate: daily-loss kill-switch
# ---------------------------------------------------------------------------


async def test_kill_switch_breach_refuses(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    _write_equity_line(settings, ts_ms=_midnight_ms() - 3_600_000, value=1000.0)
    client = FakeClient(equity=900.0)  # down 10% on the day; default limit 5%
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is False and "kill-switch" in res["reason"]
    assert res["risk_gate"]["checks"]["kill_switch"]["passed"] is False
    assert res["risk_gate"]["checks"]["kill_switch"]["daily_loss_pct"] == 10.0
    assert client.opened == []


async def test_kill_switch_env_threshold_override(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("MCP_SWARM_MAX_DAILY_LOSS_PCT", "15")
    _patch_decision(monkeypatch, _decision_payload())
    _write_equity_line(settings, ts_ms=_midnight_ms() - 3_600_000, value=1000.0)
    client = FakeClient(equity=900.0)  # 10% loss < 15% limit
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True
    assert res["risk_gate"]["checks"]["kill_switch"]["passed"] is True


async def test_kill_switch_no_snapshot_passes_and_snapshots_now(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient(equity=1000.0)
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True
    rows = journal.read_equity(settings=settings)
    assert len(rows) == 1 and rows[0]["address"] == _ADDRESS
    assert rows[0]["tradable_usdc"] == 1000.0


async def test_gate_degrades_when_journal_broken(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("journal exploded")

    monkeypatch.setattr(journal, "read_equity", _boom)
    client = FakeClient()
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True  # a broken journal degrades, never blocks
    assert res["risk_gate"]["checks"]["kill_switch"] == {"skipped": "equity journal unavailable"}


# ---------------------------------------------------------------------------
# Risk gate: correlated-exposure delegation (analysis.risk lands in parallel)
# ---------------------------------------------------------------------------


async def test_correlated_exposure_refusal_when_check_exists(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    monkeypatch.setattr(
        analysis_risk,
        "correlated_exposure_check",
        lambda positions, candidate, betas, cap_mult=2.0: {
            "allowed": False,
            "reason": "BTC-beta cluster already at cap",
        },
        raising=False,
    )
    client = FakeClient(positions=[_pos("ETH", 1.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is False and "cluster" in res["reason"]
    assert client.opened == []


async def test_correlated_exposure_scales_notional(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    monkeypatch.setattr(
        analysis_risk,
        "correlated_exposure_check",
        lambda positions, candidate, betas, cap_mult=2.0: {
            "allowed": True,
            "scaled_notional": 100.0,
        },
        raising=False,
    )
    client = FakeClient(equity=1000.0, positions=[_pos("ETH", 1.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    # Plan notional = 1000 * 0.5 = 500 -> shrunk to the cluster headroom.
    assert res["placed"] is True
    assert client.opened[0]["notional_usd"] == 100.0
    assert res["sizing"]["notional_usd"] == 100.0


async def test_correlated_check_absent_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    monkeypatch.delattr(analysis_risk, "correlated_exposure_check", raising=False)
    client = FakeClient(positions=[_pos("ETH", 1.0)])
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True
    assert "skipped" in res["risk_gate"]["checks"]["correlated_exposure"]


# ---------------------------------------------------------------------------
# Cloid + journal append on placement
# ---------------------------------------------------------------------------


async def test_placed_order_is_journaled_with_cloid_and_oids(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())
    client = FakeClient()
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True
    cloid = res["cloid"]
    assert cloid.startswith("0x") and len(cloid) == 34 and int(cloid, 16) >= 0
    assert res["journal"]["recorded"] is True
    rows = journal.read_decisions(settings=settings)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["cloid"] == cloid and entry["order_ids"] == [11]
    assert entry["address"] == _ADDRESS and entry["entry"] == 100.0
    assert entry["stop"] == 95.0 and entry["target"] == 120.0
    assert entry["context"]["source"] == "open_from_decision"


async def test_journaling_failure_never_blocks_order(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _patch_decision(monkeypatch, _decision_payload())

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(journal, "record_decision", _boom)
    client = FakeClient()
    res = await pt.open_from_decision("BTC", client=client, settings=settings)
    assert res["placed"] is True and len(client.opened) == 1
    assert res["journal"]["recorded"] is False


# ---------------------------------------------------------------------------
# Risk gate on the manual path
# ---------------------------------------------------------------------------


async def test_open_manual_gate_refusal_envelope(settings: Settings) -> None:
    client = FakeClient(positions=[_pos("ETH", 1.0), _pos("SOL", 2.0), _pos("DOGE", 3.0)])
    res = await pt.open_manual("BTC", "LONG", notional_usd=100, client=client, settings=settings)
    assert res["placed"] is False and "positions" in res["reason"]
    assert "disclaimer" in res
    assert client.opened == []


async def test_open_manual_reduce_only_bypasses_gate(settings: Settings) -> None:
    client = FakeClient(positions=[_pos("ETH", 1.0), _pos("SOL", 2.0), _pos("DOGE", 3.0)])
    res = await pt.open_manual(
        "BTC", "SELL", size=1.0, reduce_only=True, client=client, settings=settings
    )
    assert res.get("placed") is True and len(client.opened) == 1
    assert res["cloid"].startswith("0x")


async def test_open_manual_success_is_journaled(settings: Settings) -> None:
    client = FakeClient()
    res = await pt.open_manual("BTC", "LONG", notional_usd=100.0, client=client, settings=settings)
    assert res["placed"] is True and res["journal"]["recorded"] is True
    rows = journal.read_decisions(settings=settings)
    assert rows[-1]["context"]["source"] == "open_manual"
    assert rows[-1]["direction"] == "LONG" and rows[-1]["cloid"] == res["cloid"]
