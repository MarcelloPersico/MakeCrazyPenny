"""Append-only trade journal + PnL reconciliation for the paper-trading swarm.

Persists three JSONL streams under ``Settings.resolve_cache_dir()/journal/``:

* ``decisions.jsonl`` — one entry per placed (or host-recorded) trade decision:
  id, timestamps, symbol/coin, action/direction, conviction, the plan's
  entry/stop/target/leverage, the client order id (``cloid``), the exchange
  order ids, and the swarm context (scout/news/chart verdict summaries).
* ``cycles.jsonl`` — one entry per swarm cycle (goal, verdicts, chosen action).
* ``equity.jsonl`` — point-in-time account snapshots; the daily-loss
  kill-switch in :mod:`.paper_trade` baselines against these, and
  :func:`performance` returns the tail as an equity curve.

Writers are append-only and never raise (a broken / read-only cache dir
degrades to ``journaled: False``); readers are tolerant (corrupt lines are
skipped), so a half-written line from a crashed process can never poison the
journal. :func:`reconcile` joins exchange fills back to journaled decisions —
client order id first, then the recorded exchange order ids, then a
symbol+time-window fallback — and computes realized PnL, R-multiples, and
win/loss per closed round trip. :func:`performance` pulls the live account and
fills through :mod:`.paper_trade` (tolerant: no key configured returns
``{"_error": ...}``) and aggregates the journal into the swarm's scoreboard.

Import-safe: stdlib only at module import; :mod:`.paper_trade` is imported
lazily inside :func:`performance` (and ``paper_trade`` lazily imports this
module back), so neither module needs the other at import time.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.symbols import to_hyperliquid_coin

#: JSONL stream filenames under ``<cache_dir>/journal/``.
DECISIONS_FILE = "decisions.jsonl"
CYCLES_FILE = "cycles.jsonl"
EQUITY_FILE = "equity.jsonl"

#: Fallback fill-matching window after a decision (seconds) when no id matches.
DEFAULT_MATCH_WINDOW_S: float = 6 * 3600.0


def _now() -> datetime:
    """Current UTC time (single seam for timestamps)."""
    return datetime.now(timezone.utc)


def _journal_dir(settings: Settings | None) -> Path:
    """Resolve (and best-effort create) the journal directory."""
    s = settings or Settings.from_env()
    path = s.resolve_cache_dir() / "journal"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Appends will fail soft (journaled: False); readers return [].
        pass
    return path


def _append_jsonl(path: Path, entry: dict[str, Any]) -> bool:
    """Append one JSON line to ``path``. Returns success; never raises.

    Mirrors the alerts subsystem's file sink: a read-only or broken cache dir
    degrades to ``False`` instead of crashing a tool (or blocking an order).
    """
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Read a JSONL file tolerantly: corrupt / non-object lines are skipped."""
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # half-written / corrupt line: skip, never poison reads
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    if limit is not None and limit > 0:
        rows = rows[-int(limit) :]
    return rows


def goal_get(*, settings: Settings | None = None) -> dict[str, Any]:
    """Read the swarm's standing goal (CONTRACT.md §18).

    The goal persists server-side (``journal/goal.json``) so stateless headless
    swarm cycles share one objective. Returns ``{"goal": None}`` when unset.
    """
    path = _journal_dir(settings) / "goal.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"goal": None, "updated_at": None, "disclaimer": DISCLAIMER}
    if not isinstance(data, dict):
        return {"goal": None, "updated_at": None, "disclaimer": DISCLAIMER}
    return {
        "goal": data.get("goal"),
        "updated_at": data.get("updated_at"),
        "disclaimer": DISCLAIMER,
    }


def goal_set(goal: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Persist the swarm's standing goal. Returns the stored record."""
    record = {
        "goal": str(goal).strip(),
        "updated_at": _now().isoformat(),
    }
    path = _journal_dir(settings) / "goal.json"
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        stored = True
    except OSError:
        stored = False
    # The disclaimer rides on the RETURN only (never persisted to goal.json).
    return {**record, "stored": stored, "disclaimer": DISCLAIMER}


def _coin_for(symbol: Any) -> str | None:
    """Best-effort Hyperliquid coin name for ``symbol`` (``None`` on failure)."""
    try:
        return to_hyperliquid_coin(str(symbol)) if symbol else None
    except Exception:
        return None


def _num(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Writers (append-only; failure degrades to ``journaled: False``)
# ---------------------------------------------------------------------------


def record_decision(
    decision: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    cloid: str | None = None,
    sizing: dict[str, Any] | None = None,
    order_ids: list[Any] | None = None,
    entry_price: float | None = None,
    interval: str | None = None,
    address: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Append one decision entry to ``decisions.jsonl`` and return it.

    Args:
        decision: A ``TradeDecision.to_dict()``-shaped dict (or a minimal
            manual stand-in carrying ``symbol`` / ``action`` / ``direction`` /
            ``conviction`` and a ``leverage`` plan).
        context: Swarm context — scout/news/chart verdict summaries as plain
            strings, plus any cycle metadata (e.g. ``interval``, ``source``).
        cloid: The client order id generated at placement time.
        sizing: The sizing block actually sent (equity, notional, leverage...).
        order_ids: Exchange order ids (oids) returned for the entry order;
            :func:`reconcile` uses them to join fills back to this decision.
        entry_price: The entry reference price, when known.
        interval: Bar interval the decision was made on (falls back to
            ``context["interval"]``).
        address: The account address that placed the order (per-wallet journal).
        settings: Optional settings (cache dir); defaults to the environment.

    Returns:
        The journal entry plus ``journaled`` (whether the append succeeded).
    """
    plan = decision.get("leverage") if isinstance(decision.get("leverage"), dict) else {}
    now = _now()
    entry: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": now.isoformat(),
        "ts_ms": int(now.timestamp() * 1000),
        "symbol": decision.get("symbol"),
        "coin": _coin_for(decision.get("symbol")),
        "action": decision.get("action"),
        "direction": decision.get("direction"),
        "interval": interval or (context or {}).get("interval"),
        "conviction": decision.get("conviction"),
        "entry": entry_price if entry_price is not None else _num(plan.get("entry_price")),
        "stop": _num(plan.get("stop_price")),
        "target": _num(plan.get("target_price")),
        "leverage": plan.get("suggested_leverage"),
        "cloid": cloid,
        "order_ids": list(order_ids or []),
        "address": address,
        "summary": decision.get("summary") or "",
        "context": dict(context or {}),
    }
    if sizing:
        entry["sizing"] = dict(sizing)
    ok = _append_jsonl(_journal_dir(settings) / DECISIONS_FILE, entry)
    return {**entry, "journaled": ok}


def record_cycle(cycle: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Append one swarm-cycle entry to ``cycles.jsonl`` and return it.

    ``cycle`` is the host's free-form cycle record (goal, per-agent verdict
    summaries, the chosen action, refusal reasons, ...). An ``id`` and UTC
    timestamps are stamped on; cycle keys never override them.
    """
    now = _now()
    entry: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": now.isoformat(),
        "ts_ms": int(now.timestamp() * 1000),
    }
    for key, value in (cycle or {}).items():
        if key not in entry:
            entry[key] = value
    ok = _append_jsonl(_journal_dir(settings) / CYCLES_FILE, entry)
    # The disclaimer rides on the RETURN only (never persisted to the journal).
    return {**entry, "journaled": ok, "disclaimer": DISCLAIMER}


def snapshot_equity(account: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Append a point-in-time equity snapshot derived from an account-state dict.

    The daily-loss kill-switch baselines against these snapshots and
    :func:`performance` returns the tail as the equity curve.
    """
    now = _now()
    positions = [p for p in (account.get("positions") or []) if isinstance(p, dict)]
    upnl = sum(_num(p.get("unrealized_pnl")) or 0.0 for p in positions)
    entry: dict[str, Any] = {
        "ts": now.isoformat(),
        "ts_ms": int(now.timestamp() * 1000),
        "date": now.strftime("%Y-%m-%d"),
        "address": account.get("address"),
        "account_value": _num(account.get("account_value")),
        "tradable_usdc": _num(account.get("tradable_usdc")),
        "total_margin_used": _num(account.get("total_margin_used")),
        "n_positions": len([p for p in positions if _num(p.get("size"))]),
        "unrealized_pnl": round(upnl, 6),
    }
    ok = _append_jsonl(_journal_dir(settings) / EQUITY_FILE, entry)
    return {**entry, "journaled": ok}


# ---------------------------------------------------------------------------
# Readers (tolerant; missing file -> [])
# ---------------------------------------------------------------------------


def read_decisions(
    limit: int | None = None, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Journaled decisions in file order (oldest first); last ``limit`` kept."""
    return _read_jsonl(_journal_dir(settings) / DECISIONS_FILE, limit)


def read_cycles(
    limit: int | None = None, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Journaled swarm cycles in file order (oldest first); last ``limit`` kept."""
    return _read_jsonl(_journal_dir(settings) / CYCLES_FILE, limit)


def read_equity(
    limit: int | None = None, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Equity snapshots in file order (oldest first); last ``limit`` kept."""
    return _read_jsonl(_journal_dir(settings) / EQUITY_FILE, limit)


def recent(n: int = 10, *, settings: Settings | None = None) -> dict[str, Any]:
    """The most recent journal entries across all three streams."""
    n = max(1, int(n))
    return {
        "decisions": read_decisions(n, settings=settings),
        "cycles": read_cycles(n, settings=settings),
        "equity": read_equity(n, settings=settings),
        "disclaimer": DISCLAIMER,
    }


def daily_loss_baseline(
    entries: list[dict[str, Any]] | None = None,
    *,
    address: str | None = None,
    settings: Settings | None = None,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Today's equity baseline for the daily-loss kill-switch.

    The baseline is the last snapshot at or before today's UTC midnight; when
    no pre-midnight snapshot exists (first day of journaling) the earliest
    snapshot taken today stands in, so the kill-switch measures "since we
    started today". Snapshot equity prefers ``tradable_usdc`` and falls back
    to ``account_value``.

    Args:
        entries: Pre-read equity entries (pure-function mode); read from the
            journal when omitted.
        address: When given, only entries for this account (or with no
            address) are considered.
        settings: Optional settings (cache dir) for the journal read.
        now_ms: Override "now" in epoch ms (deterministic tests).

    Returns:
        ``{"value", "ts_ms", "kind"}`` with ``kind`` one of ``pre_midnight`` /
        ``first_of_today``, or ``None`` when no usable snapshot exists.
    """
    if entries is None:
        entries = read_equity(settings=settings)
    rows = [
        e
        for e in entries
        if isinstance(e, dict) and (address is None or e.get("address") in (None, address))
    ]
    if now_ms is None:
        now_ms = int(_now().timestamp() * 1000)
    midnight = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    midnight_ms = int(midnight.timestamp() * 1000)

    def _value(e: dict[str, Any]) -> float | None:
        v = _num(e.get("tradable_usdc"))
        return v if v is not None else _num(e.get("account_value"))

    usable = [(int(_num(e.get("ts_ms")) or 0), _value(e)) for e in rows]
    usable = [(t, v) for t, v in usable if v is not None]
    # A pre-midnight snapshot anchors today only when it is from YESTERDAY:
    # after days of inactivity a stale snapshot would let yesterday's loss
    # block all of today (or yesterday's gain inflate today's allowance).
    day_ms = 24 * 3600 * 1000
    pre = [(t, v) for t, v in usable if midnight_ms - day_ms <= t <= midnight_ms]
    if pre:
        t, v = max(pre, key=lambda tv: tv[0])
        return {"value": v, "ts_ms": t, "kind": "pre_midnight"}
    today = [(t, v) for t, v in usable if t > midnight_ms]
    if today:
        t, v = min(today, key=lambda tv: tv[0])
        return {"value": v, "ts_ms": t, "kind": "first_of_today"}
    return None


# ---------------------------------------------------------------------------
# Reconciliation + performance
# ---------------------------------------------------------------------------


def reconcile(
    fills: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    window_s: float = DEFAULT_MATCH_WINDOW_S,
) -> dict[str, Any]:
    """Join exchange fills back to journaled decisions and score round trips.

    Matching, per decision (oldest first), against still-unclaimed fills:

    1. client order id (``cloid``) carried by a fill, when present;
    2. the recorded exchange ``order_ids`` (the entry order's oid);
    3. fallback: same coin, entry-side fill within ``window_s`` seconds after
       the decision timestamp.

    The matched fill's oid groups partial entry fills; opposite-side fills at
    or after the entry then close the round trip greedily up to the entry
    size. Realized PnL sums Hyperliquid's per-fill ``closed_pnl``; the
    R-multiple is realized PnL over the planned risk
    (``|entry_vwap - stop| * entry_size``).

    Pure and offline — operates on plain dicts shaped like
    ``recent_fills()["fills"]`` rows and ``read_decisions()`` entries.

    Returns:
        ``{"decisions": [per-decision outcome rows], "n_decisions",
        "n_filled", "n_closed", "n_open", "n_unfilled", "wins", "losses",
        "total_realized_pnl"}``.
    """
    rows = [f for f in fills if isinstance(f, dict)]
    rows.sort(key=lambda f: _num(f.get("time")) or 0.0)
    claimed: set[int] = set()
    results: list[dict[str, Any]] = []
    decs = sorted(
        (d for d in decisions if isinstance(d, dict)),
        key=lambda d: _num(d.get("ts_ms")) or 0.0,
    )
    window_ms = float(window_s) * 1000.0

    # Fills that some decision owns BY ID may never be window-claimed by another
    # decision (an older same-coin decision processed first would otherwise
    # steal a newer decision's entry fill and report the real one "unfilled").
    id_owned: set[int] = set()
    all_cloids = {d.get("cloid") for d in decs if d.get("cloid")}
    all_oids = {o for d in decs for o in (d.get("order_ids") or []) if o is not None}
    for i, f in enumerate(rows):
        if (f.get("cloid") and f.get("cloid") in all_cloids) or f.get("oid") in all_oids:
            id_owned.add(i)

    for d in decs:
        coin = d.get("coin") or _coin_for(d.get("symbol"))
        direction = str(d.get("direction") or "").upper()
        entry_side = {"LONG": "BUY", "SHORT": "SELL"}.get(direction)
        row: dict[str, Any] = {
            "id": d.get("id"),
            "symbol": d.get("symbol"),
            "coin": coin,
            "cloid": d.get("cloid"),
            "direction": direction,
            "matched_by": None,
            "filled": False,
            "outcome": "unfilled",
            "entry_price": None,
            "entry_size": None,
            "exit_size": 0.0,
            "realized_pnl": None,
            "fees": None,
            "r_multiple": None,
        }
        if not coin or entry_side is None:
            row["outcome"] = "flat"
            results.append(row)
            continue

        # -- entry match: cloid -> oid -> symbol+time-window fallback ----------
        idx: int | None = None
        matched_by: str | None = None
        cloid = d.get("cloid")
        if cloid:
            for i, f in enumerate(rows):
                if i not in claimed and f.get("coin") == coin and f.get("cloid") == cloid:
                    idx, matched_by = i, "cloid"
                    break
        if idx is None:
            oids = {o for o in (d.get("order_ids") or []) if o is not None}
            if oids:
                for i, f in enumerate(rows):
                    if i not in claimed and f.get("coin") == coin and f.get("oid") in oids:
                        idx, matched_by = i, "oid"
                        break
        if idx is None:
            ts_ms = _num(d.get("ts_ms"))
            if ts_ms is not None:
                for i, f in enumerate(rows):
                    if (
                        i in claimed
                        or i in id_owned
                        or f.get("coin") != coin
                        or f.get("side") != entry_side
                    ):
                        continue
                    t = _num(f.get("time")) or 0.0
                    if ts_ms <= t <= ts_ms + window_ms:
                        idx, matched_by = i, "window"
                        break
        if idx is None:
            results.append(row)
            continue

        # -- entry: group partial fills sharing the matched fill's oid ---------
        entry_oid = rows[idx].get("oid")
        entry_idxs = [
            i
            for i, f in enumerate(rows)
            if i not in claimed
            and f.get("coin") == coin
            and (i == idx or (entry_oid is not None and f.get("oid") == entry_oid))
        ]
        claimed.update(entry_idxs)
        entry_fills = [rows[i] for i in entry_idxs]
        entry_size = sum(_num(f.get("size")) or 0.0 for f in entry_fills)
        fees = sum(_num(f.get("fee")) or 0.0 for f in entry_fills)
        entry_time = max(_num(f.get("time")) or 0.0 for f in entry_fills)
        notional = sum(
            (_num(f.get("price")) or 0.0) * (_num(f.get("size")) or 0.0) for f in entry_fills
        )
        entry_vwap = notional / entry_size if entry_size > 0 else None

        # -- exits: opposite-side fills after the entry, up to the entry size --
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        realized = 0.0
        exit_size = 0.0
        tol = max(entry_size * 1e-6, 1e-9)
        for i, f in enumerate(rows):
            if exit_size >= entry_size - tol:
                break
            if i in claimed or f.get("coin") != coin or f.get("side") != exit_side:
                continue
            if (_num(f.get("time")) or 0.0) < entry_time:
                continue
            claimed.add(i)
            exit_size += _num(f.get("size")) or 0.0
            realized += _num(f.get("closed_pnl")) or 0.0
            fees += _num(f.get("fee")) or 0.0

        closed = entry_size > 0 and exit_size >= entry_size - tol
        row.update(
            {
                "matched_by": matched_by,
                "filled": True,
                "entry_price": round(entry_vwap, 8) if entry_vwap else None,
                "entry_size": round(entry_size, 8),
                "exit_size": round(exit_size, 8),
                "realized_pnl": round(realized, 6),
                "fees": round(fees, 6),
            }
        )
        if closed:
            row["outcome"] = "win" if realized > 0 else ("loss" if realized < 0 else "breakeven")
            stop = _num(d.get("stop"))
            if stop is not None and entry_vwap:
                risk = abs(entry_vwap - stop) * entry_size
                if risk > 0:
                    row["r_multiple"] = round(realized / risk, 4)
        else:
            row["outcome"] = "open"
        results.append(row)

    closed_rows = [r for r in results if r["outcome"] in ("win", "loss", "breakeven")]
    return {
        "decisions": results,
        "n_decisions": len(results),
        "n_filled": sum(1 for r in results if r["filled"]),
        "n_closed": len(closed_rows),
        "n_open": sum(1 for r in results if r["outcome"] == "open"),
        "n_unfilled": sum(1 for r in results if r["outcome"] == "unfilled"),
        "wins": sum(1 for r in closed_rows if r["outcome"] == "win"),
        "losses": sum(1 for r in closed_rows if r["outcome"] == "loss"),
        "total_realized_pnl": round(sum(r["realized_pnl"] or 0.0 for r in closed_rows), 6),
    }


async def performance(
    *,
    fills_limit: int = 200,
    equity_tail: int = 30,
    settings: Settings | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """The swarm scoreboard: journaled decisions vs live testnet account/fills.

    Pulls the account and recent fills through :mod:`.paper_trade` (the
    ``client=`` seam makes this fully testable offline), reconciles them
    against the journaled decisions for that account, and aggregates hit rate,
    average R-multiple, realized PnL per symbol, marked open positions, and
    the equity-curve tail. Tolerant: with no key configured (or any account
    read failure) it returns ``{"_error": ...}`` instead of raising.
    """
    settings = settings or Settings.from_env()
    from .paper_trade import account as _account  # lazy: avoids an import cycle
    from .paper_trade import recent_fills as _recent_fills

    acct = await _account(client=client, settings=settings)
    if "error" in acct:
        return {
            "_error": acct["error"],
            "n_decisions_journaled": len(read_decisions(settings=settings)),
            "disclaimer": DISCLAIMER,
        }
    fills_resp = await _recent_fills(fills_limit, client=client, settings=settings)
    if not isinstance(fills_resp.get("fills"), list):
        # A fills outage must not masquerade as "no order ever filled": surface
        # it instead of fabricating an all-unfilled, zero-PnL scoreboard.
        return {
            "_error": f"fills unavailable: {fills_resp.get('error') or 'no fills payload'}",
            "address": acct.get("address"),
            "n_decisions_journaled": len(read_decisions(settings=settings)),
            "disclaimer": DISCLAIMER,
        }
    fills = fills_resp["fills"]
    address = acct.get("address")
    decisions = [
        d for d in read_decisions(settings=settings) if d.get("address") in (None, address)
    ]
    rec = reconcile(fills, decisions)

    closed = [r for r in rec["decisions"] if r["outcome"] in ("win", "loss", "breakeven")]
    rs = [r["r_multiple"] for r in closed if r["r_multiple"] is not None]
    by_symbol: dict[str, dict[str, Any]] = {}
    for r in closed:
        sym = str(r.get("symbol") or r.get("coin") or "?")
        agg = by_symbol.setdefault(
            sym, {"n_closed": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0}
        )
        agg["n_closed"] += 1
        agg["wins"] += 1 if r["outcome"] == "win" else 0
        agg["losses"] += 1 if r["outcome"] == "loss" else 0
        agg["realized_pnl"] = round(agg["realized_pnl"] + (r["realized_pnl"] or 0.0), 6)
    open_marked = [
        {
            "coin": p.get("coin"),
            "size": p.get("size"),
            "entry_price": p.get("entry_price"),
            "unrealized_pnl": p.get("unrealized_pnl"),
            "leverage": p.get("leverage"),
        }
        for p in (acct.get("positions") or [])
        if isinstance(p, dict) and _num(p.get("size"))
    ]
    equity_rows = [
        e for e in read_equity(settings=settings) if e.get("address") in (None, address)
    ]
    return {
        "address": address,
        "n_decisions": rec["n_decisions"],
        "n_closed": rec["n_closed"],
        "n_open": rec["n_open"],
        "n_unfilled": rec["n_unfilled"],
        "hit_rate": round(rec["wins"] / rec["n_closed"], 4) if rec["n_closed"] else None,
        "avg_R": round(sum(rs) / len(rs), 4) if rs else None,
        "total_realized_pnl": rec["total_realized_pnl"],
        "by_symbol": by_symbol,
        "open_positions_marked": open_marked,
        "equity_curve_tail": equity_rows[-max(1, int(equity_tail)) :],
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "DECISIONS_FILE",
    "CYCLES_FILE",
    "EQUITY_FILE",
    "DEFAULT_MATCH_WINDOW_S",
    "goal_get",
    "goal_set",
    "record_decision",
    "record_cycle",
    "snapshot_equity",
    "read_decisions",
    "read_cycles",
    "read_equity",
    "recent",
    "daily_loss_baseline",
    "reconcile",
    "performance",
]
