"""Layer 2: paper-trading orchestration over the Hyperliquid testnet (CONTRACT.md §17).

Bridges the read-only decision engine and the authenticated execution client:

* :func:`open_from_decision` is the **decision-driven** path — it runs
  :func:`makecrazypenny.orchestration.crypto.decide_crypto`, reads the leverage
  plan it already produces (suggested leverage + ``notional_pct`` of equity),
  sizes that against the **live testnet account equity**, and places the order.
* the remaining thin wrappers (:func:`account`, :func:`open_manual`,
  :func:`set_tpsl`, :func:`close`, :func:`cancel`, :func:`set_leverage`,
  :func:`open_orders`, :func:`recent_fills`) are the **manual** primitives the
  MCP tools and CLI share.

The :class:`~makecrazypenny.execution.hyperliquid.HyperliquidPaperClient` is
synchronous, so every call hops onto a worker thread via ``asyncio.to_thread`` to
avoid blocking the event loop. Each wrapper is tolerant: an
:class:`ExecutionError` (missing key, missing SDK, validation, upstream failure)
becomes an ``{"error": ...}`` dict (already secret-scrubbed) rather than raising —
matching the rest of the toolkit's "never crash a tool over I/O" style.

Swarm upgrade (research-out/DESIGN-SWARM.md "Journal + risk gate"): every
risk-increasing placement first passes a **risk gate** (same-direction position
cap, daily-loss kill-switch vs the equity journal, correlated-exposure check
when available) — a refusal is a ``{"placed": False, "reason": ...}`` envelope,
never an exception. Placed orders get a generated client order id (``cloid``)
and are appended to the trade journal (:mod:`.journal`, lazily imported); a
journaling failure never blocks or voids an order.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..core.config import Settings
from ..core.crypto_universe import fetch_hyperliquid_perps
from ..core.disclaimer import DISCLAIMER
from ..core.redact import redact_secrets
from ..core.symbols import canonical_crypto, to_hyperliquid_coin
from ..execution.hyperliquid import ExecutionError, HyperliquidPaperClient
from .crypto import decide_crypto


def _client(client: HyperliquidPaperClient | None, settings: Settings | None) -> HyperliquidPaperClient:
    """Return the provided client or build one from ``settings``/env."""
    if client is not None:
        return client
    return HyperliquidPaperClient(settings or Settings.from_env())


async def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a synchronous client call off-thread; convert failures to an error dict."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ExecutionError as exc:
        return {"error": str(exc), "disclaimer": DISCLAIMER}
    except Exception as exc:  # never raise out of a paper-trade wrapper
        return {"error": redact_secrets(f"{type(exc).__name__}: {exc}"), "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# Risk gate + journal hooks (swarm upgrade; research-out/DESIGN-SWARM.md)
# ---------------------------------------------------------------------------

#: Set to ``off`` / ``0`` / ``false`` to disable the pre-placement risk gate.
#: DANGEROUS: removes the position cap, the daily-loss kill-switch, and the
#: correlated-exposure check that make unattended trading loops survivable.
_RISK_GATE_ENV = "MCP_SWARM_RISK_GATE"
#: Max simultaneously open same-direction positions (same-coin adds exempt).
_MAX_POSITIONS_ENV = "MCP_SWARM_MAX_POSITIONS"
_DEFAULT_MAX_POSITIONS = 3
#: Daily loss (percent of today's baseline equity) that halts new risk.
_MAX_DAILY_LOSS_ENV = "MCP_SWARM_MAX_DAILY_LOSS_PCT"
_DEFAULT_MAX_DAILY_LOSS_PCT = 5.0


def _gate_enabled() -> bool:
    """Whether the risk gate is active (default on; ``MCP_SWARM_RISK_GATE=off``)."""
    return os.environ.get(_RISK_GATE_ENV, "").strip().lower() not in ("off", "0", "false")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _new_cloid() -> str:
    """A fresh 128-bit client order id (``0x`` + 32 hex chars).

    The format matches Hyperliquid's ``Cloid`` wire form so it can be passed
    through to the exchange once the execution layer accepts one; today the
    execution client does not forward a client order id, so the cloid lives in
    the journal and fills are joined back via the recorded exchange order ids.
    """
    return "0x" + uuid.uuid4().hex


def _side_is_long(side: str) -> bool | None:
    """``LONG``/``BUY``-ish -> ``True``, ``SHORT``/``SELL``-ish -> ``False``."""
    s = str(side or "").strip().upper()
    if s in ("LONG", "BUY", "B", "BID"):
        return True
    if s in ("SHORT", "SELL", "A", "ASK"):
        return False
    return None


def _order_ids(order: dict[str, Any] | None) -> list[Any]:
    """Extract exchange order ids (oids) from a normalized order result."""
    ids: list[Any] = []
    result = (order or {}).get("result")
    if not isinstance(result, dict):
        return ids
    for st in result.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        for key in ("filled", "resting"):
            sub = st.get(key)
            if isinstance(sub, dict) and sub.get("oid") is not None:
                ids.append(sub["oid"])
    return ids


def _exchange_error(order: dict[str, Any] | None) -> str | None:
    """Extract an exchange-level per-order rejection from a normalized result.

    ``_normalize_order_result`` surfaces ``statuses: [{"error": ...}]`` payloads
    under ``result["error"]`` with the transport-level status still ``"ok"`` -
    so a top-level ``"error" in order`` check misses them entirely.
    """
    result = (order or {}).get("result")
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return None


def _journal_record(
    decision: dict[str, Any],
    *,
    context: dict[str, Any],
    cloid: str,
    sizing: dict[str, Any] | None,
    order: dict[str, Any] | None,
    address: str | None,
    settings: Settings | None,
) -> dict[str, Any]:
    """Append the placed decision to the trade journal. NEVER blocks the order.

    Entries are tagged with the placing account's address so the journal stays
    attributable per wallet; with no address known (e.g. the account read was
    skipped) nothing is written rather than journaling unattributable rows.
    """
    if not address:
        return {"recorded": False, "skipped": "account address unknown - not journaled"}
    try:
        from . import journal  # lazy: a broken journal must never block trading

        entry = journal.record_decision(
            decision,
            context,
            cloid=cloid,
            sizing=sizing,
            order_ids=_order_ids(order),
            entry_price=(order or {}).get("reference_price"),
            interval=context.get("interval"),
            address=address,
            settings=settings,
        )
        return {"recorded": bool(entry.get("journaled")), "id": entry.get("id")}
    except Exception:  # journaling failure must never block or void a placed order
        return {"recorded": False}


def _risk_gate(
    account: dict[str, Any],
    *,
    symbol: str,
    side: str,
    notional_usd: float | None,
    settings: Settings,
) -> dict[str, Any]:
    """Pre-placement portfolio risk gate for unattended-loop safety.

    Three checks, all tolerant, evaluated against the live account state:

    1. **Daily-loss kill-switch** — refuses new risk once equity has dropped
       ``MCP_SWARM_MAX_DAILY_LOSS_PCT`` (default 5.0) percent below today's
       baseline (the equity-journal snapshot at/just before UTC midnight, or
       the first snapshot of the day). No snapshot yet -> the gate passes but
       records one now, arming the switch for the rest of the day.
    2. **Same-direction position cap** — at most ``MCP_SWARM_MAX_POSITIONS``
       (default 3) open positions in the candidate's direction; adding to an
       existing position on the same coin stays allowed.
    3. **Correlated-exposure cap** — delegates to
       ``analysis.risk.correlated_exposure_check`` when it exists (it lands in
       a parallel track); absent or failing, the gate degrades gracefully to
       the two checks above. An ``allowed`` verdict may carry a
       ``scaled_notional`` that shrinks the order.

    Never raises: a refusal is an ``{"ok": False, "reason": ...}`` verdict and
    any internal failure downgrades the affected check to ``skipped``. Setting
    ``MCP_SWARM_RISK_GATE=off`` disables the gate entirely (dangerous — meant
    only for debugging, never for unattended loops).
    """
    if not _gate_enabled():
        return {
            "ok": True,
            "enabled": False,
            "note": "risk gate disabled via MCP_SWARM_RISK_GATE=off (dangerous)",
        }
    checks: dict[str, Any] = {}
    gate: dict[str, Any] = {"ok": True, "enabled": True, "reason": None, "checks": checks}
    try:
        positions = [
            p for p in (account.get("positions") or []) if isinstance(p, dict) and p.get("size")
        ]
        address = account.get("address")
        try:
            coin = to_hyperliquid_coin(symbol)
        except Exception:
            coin = None

        # 1. Daily-loss kill-switch vs the UTC-midnight equity baseline.
        max_loss_pct = _env_float(_MAX_DAILY_LOSS_ENV, _DEFAULT_MAX_DAILY_LOSS_PCT)
        equity = account.get("tradable_usdc")
        if equity is None:
            equity = account.get("account_value")
        if not address or not equity or float(equity) <= 0:
            checks["kill_switch"] = {"skipped": "no account address/equity to baseline against"}
        else:
            try:
                from . import journal  # lazy: a broken journal never blocks trading

                entries = [
                    e
                    for e in journal.read_equity(settings=settings)
                    if e.get("address") in (None, address)
                ]
                baseline = journal.daily_loss_baseline(entries)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                latest = max(entries, key=lambda e: e.get("ts_ms") or 0) if entries else None
                if latest is None or str(latest.get("date") or "") != today:
                    # First sighting today: record the day's baseline snapshot now.
                    journal.snapshot_equity(account, settings=settings)
                if baseline is None or float(baseline.get("value") or 0.0) <= 0:
                    checks["kill_switch"] = {
                        "passed": True,
                        "note": "no usable equity baseline yet - snapshot recorded, gate passes",
                    }
                else:
                    base_val = float(baseline["value"])
                    loss_pct = (base_val - float(equity)) / base_val * 100.0
                    checks["kill_switch"] = {
                        "passed": loss_pct < max_loss_pct,
                        "baseline_equity": round(base_val, 6),
                        "equity": round(float(equity), 6),
                        "daily_loss_pct": round(loss_pct, 4),
                        "limit_pct": max_loss_pct,
                    }
                    if loss_pct >= max_loss_pct:
                        gate["ok"] = False
                        gate["reason"] = (
                            f"daily-loss kill-switch: equity {float(equity):.2f} is "
                            f"{loss_pct:.2f}% below today's baseline {base_val:.2f} "
                            f"(limit {max_loss_pct:.1f}%) - refusing new risk-increasing "
                            "orders until UTC midnight"
                        )
                        return gate
            except Exception:
                checks["kill_switch"] = {"skipped": "equity journal unavailable"}

        # 2. Max same-direction positions (same-coin adds exempt).
        max_positions = _env_int(_MAX_POSITIONS_ENV, _DEFAULT_MAX_POSITIONS)
        is_long = _side_is_long(side)
        if is_long is None:
            checks["max_positions"] = {"skipped": f"unrecognized side {side!r}"}
        else:
            same = [p for p in positions if (float(p.get("size") or 0.0) > 0.0) == is_long]
            same_coins = {p.get("coin") for p in same}
            adding = coin is not None and coin in same_coins
            checks["max_positions"] = {
                "passed": len(same) < max_positions or adding,
                "open_same_direction": len(same),
                "limit": max_positions,
                "adding_to_existing": adding,
            }
            if len(same) >= max_positions and not adding:
                gate["ok"] = False
                gate["reason"] = (
                    f"max same-direction positions reached ({len(same)}/{max_positions}) - "
                    "close one first or raise MCP_SWARM_MAX_POSITIONS"
                )
                return gate

        # 3. Correlated-exposure cap (analysis.risk lands in a parallel track).
        check_fn = None
        try:
            from ..analysis import risk as _risk_mod

            check_fn = getattr(_risk_mod, "correlated_exposure_check", None)
        except Exception:
            check_fn = None
        if check_fn is None:
            checks["correlated_exposure"] = {
                "skipped": "analysis.risk.correlated_exposure_check not available"
            }
        elif not positions:
            checks["correlated_exposure"] = {"skipped": "no open positions to correlate against"}
        else:
            try:
                equity_val: float | None = float(equity) if equity else None
            except (TypeError, ValueError):
                equity_val = None
            # Translate to correlated_exposure_check's contract: it reads
            # candidate "notional"/"equity" and position rows "symbol"/"notional"
            # (account_state rows carry coin/position_value instead).
            candidate = {
                "symbol": coin or symbol,
                "coin": coin,
                "side": "LONG" if is_long else ("SHORT" if is_long is not None else side),
                "notional": float(notional_usd) if notional_usd else None,
                "notional_usd": float(notional_usd) if notional_usd else None,
                "equity": equity_val,
            }
            mapped = [
                {
                    "symbol": p.get("coin"),
                    "notional": abs(float(p.get("position_value") or 0.0)),
                }
                for p in positions
            ]
            verdict: Any = None
            try:
                verdict = check_fn(mapped, candidate, {}, cap_mult=2.0, equity=equity_val)
            except TypeError:
                # Older/looser signatures: equity still rides in the candidate.
                try:
                    verdict = check_fn(mapped, candidate, {})
                except Exception:
                    verdict = None
            except Exception:
                verdict = None
            if isinstance(verdict, dict):
                checks["correlated_exposure"] = {
                    "passed": verdict.get("allowed") is not False,
                    "allowed": verdict.get("allowed"),
                    "scaled_notional": verdict.get("scaled_notional"),
                    "reason": verdict.get("reason"),
                }
                if verdict.get("allowed") is False:
                    gate["ok"] = False
                    gate["reason"] = str(verdict.get("reason") or "correlated exposure cap exceeded")
                    return gate
                scaled = verdict.get("scaled_notional")
                if (
                    isinstance(scaled, (int, float))
                    and scaled > 0
                    and notional_usd
                    and float(scaled) < float(notional_usd)
                ):
                    gate["scaled_notional"] = float(scaled)
            else:
                checks["correlated_exposure"] = {"skipped": "check returned no verdict"}
    except Exception as exc:  # the gate itself must never break order placement
        gate["degraded"] = redact_secrets(f"{type(exc).__name__}: {exc}")
    return gate


# ---------------------------------------------------------------------------
# Manual primitives
# ---------------------------------------------------------------------------


async def available_pairs(
    *, force_refresh: bool = False, settings: Settings | None = None
) -> dict[str, Any]:
    """List the tradable Hyperliquid testnet perps (coins + per-coin max leverage).

    Keyless — this reads the exchange's public listing, so it needs **no**
    ``MCP_HL_PRIVATE_KEY`` and no SDK. Use it to confirm a coin is actually
    tradable before deciding/placing, so you never chase an impossible trade.
    """
    return await fetch_hyperliquid_perps(settings=settings or Settings.from_env(), force_refresh=force_refresh)


async def account(
    *, client: HyperliquidPaperClient | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Testnet account equity, margin usage, and open positions."""
    return await _run(_client(client, settings).account_state)


async def open_orders(
    *, client: HyperliquidPaperClient | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Currently resting testnet orders."""
    return await _run(_client(client, settings).open_orders)


async def recent_fills(
    limit: int = 20, *, client: HyperliquidPaperClient | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Most recent testnet fills (newest first)."""
    return await _run(_client(client, settings).recent_fills, limit)


async def set_leverage(
    symbol: str,
    leverage: float,
    cross: bool = True,
    *,
    client: HyperliquidPaperClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Set leverage for ``symbol`` on testnet."""
    return await _run(_client(client, settings).set_leverage, symbol, leverage, cross)


async def open_manual(
    symbol: str,
    side: str,
    *,
    size: float | None = None,
    notional_usd: float | None = None,
    leverage: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    reduce_only: bool = False,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    client: HyperliquidPaperClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Place a manual paper order (market or limit) with explicit parameters.

    ``stop_loss`` / ``take_profit`` attach exchange-side OCO trigger orders once
    the entry fills (see :meth:`HyperliquidPaperClient.open_position`).

    Risk-increasing orders (``reduce_only=False``) pass the same risk gate as
    the decision-driven path — a refusal returns ``{"placed": False,
    "reason": ...}`` without placing anything (``MCP_SWARM_RISK_GATE=off``
    disables the gate; dangerous). Placed orders gain a generated client order
    id (``cloid``) and a best-effort journal entry; a journaling failure never
    blocks the order.
    """
    settings = settings or Settings.from_env()
    c = _client(client, settings)
    gate: dict[str, Any] | None = None
    account_before: dict[str, Any] | None = None
    if not reduce_only and _gate_enabled():
        # The lambda defers the attribute lookup into _run's try/except, so an
        # injected client without account_state still degrades to a dict.
        account_before = await _run(lambda: c.account_state())
        if "error" in account_before:
            # Fail CLOSED like open_from_decision: a risk-increasing order must
            # not go out ungated just because the account read hiccuped.
            return {
                "placed": False,
                "reason": "could not reach testnet account - refusing ungated order",
                "account": account_before,
                "disclaimer": DISCLAIMER,
            }
        gate = _risk_gate(
            account_before,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            settings=settings,
        )
        if not gate.get("ok", True):
            return {
                "placed": False,
                "reason": gate.get("reason") or "refused by risk gate",
                "risk_gate": gate,
                "disclaimer": DISCLAIMER,
            }
        scaled = gate.get("scaled_notional")
        if scaled and notional_usd:
            # Honor the correlation cap's downsize on the manual path too
            # (scaling only ever applies to notional-sized orders).
            notional_usd = float(scaled)
    cloid = _new_cloid()
    res = await _run(
        lambda: c.open_position(
            symbol,
            side,
            size=size,
            notional_usd=notional_usd,
            leverage=leverage,
            order_type=order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    )
    if "error" in res:
        return res
    nested_error = _exchange_error(res)
    if nested_error is not None:
        # The exchange accepted the request but rejected the order itself
        # (insufficient margin, price band, reduce-only violation, ...).
        return {
            **res,
            "placed": False,
            "reason": f"exchange rejected the order: {nested_error}",
            "disclaimer": DISCLAIMER,
        }
    out: dict[str, Any] = {**res, "placed": True, "cloid": cloid}
    if account_before is None:
        # Gate-off / reduce-only path: still try (tolerantly) to learn the
        # account address so the placed order can be journaled.
        account_before = await _run(lambda: c.account_state())
    if gate is not None:
        out["risk_gate"] = gate
    is_long = _side_is_long(side)
    direction = "LONG" if is_long else ("SHORT" if is_long is not None else "FLAT")
    try:
        sym = canonical_crypto(symbol)
    except Exception:
        sym = str(symbol).upper()
    out["journal"] = _journal_record(
        {
            "symbol": sym,
            "action": str(side).upper(),
            "direction": direction,
            "leverage": {
                "stop_price": stop_loss,
                "target_price": take_profit,
                "suggested_leverage": leverage,
            },
        },
        context={"source": "open_manual", "order_type": order_type, "reduce_only": reduce_only},
        cloid=cloid,
        sizing={"size": size, "notional_usd": notional_usd, "leverage": leverage},
        order=out,
        address=(account_before or {}).get("address"),
        settings=settings,
    )
    return out


async def set_tpsl(
    symbol: str,
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    size: float | None = None,
    client: HyperliquidPaperClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Attach exchange-side stop-loss / take-profit triggers to an open position.

    The legs are reduce-only trigger-market orders OCO-grouped to the position
    (``positionTpsl``): one executing cancels the other, and they keep protecting
    the trade with no client connected. ``size`` defaults to the full position.
    """
    c = _client(client, settings)
    return await _run(
        lambda: c.set_position_tpsl(
            symbol, stop_loss=stop_loss, take_profit=take_profit, size=size
        )
    )


async def close(
    symbol: str,
    size: float | None = None,
    *,
    client: HyperliquidPaperClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Market-close all (or ``size`` units) of a testnet position."""
    return await _run(_client(client, settings).close_position, symbol, size)


async def cancel(
    symbol: str,
    oid: int,
    *,
    client: HyperliquidPaperClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Cancel a resting testnet order by coin + order id."""
    return await _run(_client(client, settings).cancel_order, symbol, oid)


# ---------------------------------------------------------------------------
# Decision-driven path
# ---------------------------------------------------------------------------


async def open_from_decision(
    symbol: str,
    *,
    interval: str = "15m",
    leverage_cap: float = 20.0,
    notional_usd: float | None = None,
    attach_tpsl: bool = True,
    settings: Settings | None = None,
    client: HyperliquidPaperClient | None = None,
) -> dict[str, Any]:
    """Run the crypto engine for ``symbol`` and place the sized order on testnet.

    Sizing reuses the leverage plan ``decide_crypto`` already computes: notional
    defaults to ``account_equity * plan["notional_pct"]`` (override with
    ``notional_usd``), leverage to ``plan["suggested_leverage"]``, side to the
    plan direction. With ``attach_tpsl`` (the default) the plan's ``stop_price``
    and ``target_price`` are also placed as exchange-side OCO trigger orders, so
    the engine's own invalidation/target protect the position even offline. An
    ``AVOID`` / flat verdict places **no** order and returns the analysis with a
    reason. The decision is always included so the caller can see the rationale
    even if execution is unavailable.

    Before placing, the **risk gate** (position cap, daily-loss kill-switch,
    correlated-exposure check; see :func:`_risk_gate`) is evaluated against the
    live account — a refusal returns ``{"placed": False, "reason": ...}`` with
    the gate verdict under ``"risk_gate"``, never an exception. A placed order
    gains a generated client order id (``cloid``) and a best-effort journal
    entry (``"journal"``); journaling failures never block the order.
    """
    settings = settings or Settings.from_env()
    sym = canonical_crypto(symbol)

    decision = await decide_crypto(sym, interval=interval, leverage_cap=leverage_cap, settings=settings)
    d = decision.to_dict()
    plan = d.get("leverage") or {}
    direction = str(plan.get("direction") or d.get("direction") or "").upper()
    action = d.get("action")

    base = {"symbol": sym, "interval": interval, "decision": d, "plan": plan, "disclaimer": DISCLAIMER}

    if direction not in ("LONG", "SHORT"):
        return {**base, "placed": False, "reason": f"decision is {action or direction or 'FLAT'} - no position opened"}

    c = _client(client, settings)
    account_before = await _run(c.account_state)
    if "error" in account_before:
        return {**base, "placed": False, "reason": "could not reach testnet account", "account": account_before}

    # Size against total deployable collateral: perp equity + free spot USDC
    # (Hyperliquid auto-draws spot into perp), falling back to perp equity alone.
    equity = account_before.get("tradable_usdc")
    if equity is None:
        equity = account_before.get("account_value")
    if notional_usd is None:
        if not equity or float(equity) <= 0:
            return {
                **base,
                "placed": False,
                "reason": "no testnet account equity to size against (fund the wallet)",
                "account": account_before,
            }
        notional_usd = float(equity) * float(plan.get("notional_pct") or 0.0)

    if not notional_usd or float(notional_usd) <= 0:
        return {**base, "placed": False, "reason": "computed notional is zero (flat sizing)", "account": account_before}

    gate = _risk_gate(
        account_before,
        symbol=sym,
        side=direction,
        notional_usd=float(notional_usd),
        settings=settings,
    )
    if not gate.get("ok", True):
        return {
            **base,
            "placed": False,
            "reason": gate.get("reason") or "refused by risk gate",
            "risk_gate": gate,
            "account": account_before,
        }
    scaled = gate.get("scaled_notional")
    if scaled:
        notional_usd = float(scaled)

    leverage = plan.get("suggested_leverage")
    cloid = _new_cloid()

    def _price(key: str) -> float | None:
        try:
            px = float(plan.get(key) or 0.0)
        except (TypeError, ValueError):
            return None
        return px if px > 0 else None

    stop_loss = _price("stop_price") if attach_tpsl else None
    take_profit = _price("target_price") if attach_tpsl else None
    order = await _run(
        lambda: c.open_position(
            sym,
            direction,
            notional_usd=float(notional_usd),
            leverage=leverage,
            order_type="market",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    )
    nested_error = _exchange_error(order)
    placed = "error" not in order and nested_error is None
    result: dict[str, Any] = {
        **base,
        "placed": placed,
        "cloid": cloid,
        "risk_gate": gate,
        "sizing": {
            "equity": equity,
            "notional_usd": round(float(notional_usd), 2),
            "leverage": leverage,
            "side": direction,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
        "order": order,
    }
    if nested_error is not None:
        result["reason"] = f"exchange rejected the order: {nested_error}"
    if placed:
        result["account_after"] = await _run(c.account_state)
        result["journal"] = _journal_record(
            d,
            context={"source": "open_from_decision", "interval": interval},
            cloid=cloid,
            sizing=result["sizing"],
            order=order,
            address=account_before.get("address"),
            settings=settings,
        )
    return result


__all__ = [
    "available_pairs",
    "account",
    "open_orders",
    "recent_fills",
    "set_leverage",
    "open_manual",
    "set_tpsl",
    "close",
    "cancel",
    "open_from_decision",
]
