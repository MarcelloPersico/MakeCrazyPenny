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
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..core.config import Settings
from ..core.crypto_universe import fetch_hyperliquid_perps
from ..core.disclaimer import DISCLAIMER
from ..core.redact import redact_secrets
from ..core.symbols import canonical_crypto
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
    """
    c = _client(client, settings)
    return await _run(
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

    leverage = plan.get("suggested_leverage")

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
    placed = "error" not in order
    result: dict[str, Any] = {
        **base,
        "placed": placed,
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
    if placed:
        result["account_after"] = await _run(c.account_state)
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
