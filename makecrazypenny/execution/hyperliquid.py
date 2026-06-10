"""Hyperliquid **testnet** paper-trading client (CONTRACT.md §17).

A thin, synchronous wrapper over the official ``hyperliquid-python-sdk`` (the
``trade`` extra). The SDK handles the hard part — msgpack action-hashing and the
EIP-712 signing that the ``/exchange`` endpoint requires — while this module
provides a small, stable surface the rest of the toolkit calls:

  * read:  :meth:`HyperliquidPaperClient.account_state`,
           :meth:`HyperliquidPaperClient.open_orders`,
           :meth:`HyperliquidPaperClient.recent_fills`;
  * write: :meth:`HyperliquidPaperClient.set_leverage`,
           :meth:`HyperliquidPaperClient.open_position`,
           :meth:`HyperliquidPaperClient.set_position_tpsl`,
           :meth:`HyperliquidPaperClient.close_position`,
           :meth:`HyperliquidPaperClient.cancel_order`.

Safety rails:

* **Testnet-locked.** The ``Exchange``/``Info`` clients are always built against
  :attr:`Settings.hyperliquid_testnet_url`; there is no mainnet URL to misconfigure.
* **Import-safe.** The SDK and ``eth_account`` are imported lazily inside
  :func:`_build_clients`, which is also the seam tests monkeypatch with a fake —
  so importing this module never needs the ``trade`` extra and never hits the
  network (mirrors the lazy-``httpx`` pattern in ``providers/binance.py``).
* **Secret-safe.** The signing key comes from ``MCP_HL_PRIVATE_KEY``; every error
  message is scrubbed through :func:`~makecrazypenny.core.redact.redact_secrets`.

The client is synchronous (the SDK uses blocking ``requests``); the async MCP /
CLI layers call it via ``asyncio.to_thread`` (see
:mod:`makecrazypenny.orchestration.paper_trade`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.redact import redact_secrets
from ..core.symbols import to_hyperliquid_coin

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: Hyperliquid rejects orders whose notional is below this many USD.
_MIN_ORDER_USD: float = 10.0

#: Perp price wire format: at most this many significant figures.
_PRICE_SIG_FIGS: int = 5
#: Perp price wire format: max decimals is ``_MAX_PRICE_DECIMALS - szDecimals``.
_MAX_PRICE_DECIMALS: int = 6

#: Actionable hint when the optional ``trade`` extra is not installed.
_SDK_MISSING_HINT = (
    "Hyperliquid paper trading needs the optional 'trade' extra. Install it with:\n"
    "    pip install 'makecrazypenny[trade]'\n"
    "  (or: pip install hyperliquid-python-sdk eth-account)"
)


class ExecutionError(Exception):
    """A paper-trading execution failure (config, validation, or upstream).

    The message is always pre-scrubbed of any secret via
    :func:`~makecrazypenny.core.redact.redact_secrets`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(redact_secrets(message))


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _side_to_is_buy(side: str) -> bool:
    """Map a human/engine side onto Hyperliquid's ``is_buy`` boolean.

    Accepts ``LONG``/``BUY``/``B``/``BID`` (-> ``True``) and
    ``SHORT``/``SELL``/``A``/``ASK`` (-> ``False``).
    """
    s = str(side or "").strip().upper()
    if s in ("LONG", "BUY", "B", "BID"):
        return True
    if s in ("SHORT", "SELL", "A", "ASK"):
        return False
    raise ExecutionError(f"unrecognized order side: {side!r} (use LONG/SHORT or BUY/SELL)")


def _normalize_order_result(res: Any) -> dict[str, Any]:
    """Flatten the SDK's nested order/cancel response into a compact dict.

    Surfaces ``status`` and, when present, the per-order ``statuses`` list
    (``resting``/``filled``/``error``). A non-``ok`` status or an embedded error
    string is captured under ``error`` (redacted).
    """
    if not isinstance(res, dict):
        return {"raw": res}
    out: dict[str, Any] = {"status": res.get("status")}
    resp = res.get("response")
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and isinstance(data.get("statuses"), list):
            out["statuses"] = data["statuses"]
            for st in data["statuses"]:
                if isinstance(st, dict) and st.get("error"):
                    out["error"] = redact_secrets(str(st["error"]))
    if res.get("status") not in (None, "ok"):
        out.setdefault("error", redact_secrets(str(res)))
    return out


def _build_clients(settings: Settings) -> tuple[Any, Any, str]:
    """Construct the testnet ``(Info, Exchange, account_address)`` triple.

    This is the **monkeypatch seam**: tests replace it with a fake returning
    stub Info/Exchange objects, so the suite runs without the SDK or network.

    Raises:
        ExecutionError: if the ``trade`` extra is missing, the key is unset, or
            the key is malformed.
    """
    try:
        from eth_account import Account  # type: ignore[import-not-found]
        from hyperliquid.exchange import Exchange  # type: ignore[import-not-found]
        from hyperliquid.info import Info  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via the hint test
        raise ExecutionError(f"{_SDK_MISSING_HINT}\n(import error: {exc})") from exc

    key = settings.hl_private_key
    if not key:
        raise ExecutionError(
            "MCP_HL_PRIVATE_KEY is not set. Export the private key of a funded "
            "Hyperliquid TESTNET wallet (fund it at https://app.hyperliquid-testnet.xyz/drip)."
        )
    try:
        account = Account.from_key(key)
    except Exception as exc:  # malformed key
        raise ExecutionError(f"invalid MCP_HL_PRIVATE_KEY: {exc}") from exc

    address = settings.hl_account_address or account.address
    base_url = settings.hyperliquid_testnet_url  # testnet-locked: the only base URL
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(account, base_url, account_address=address)
    return info, exchange, address


class HyperliquidPaperClient:
    """Synchronous testnet paper-trading client (read + write)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self._info: Any = None
        self._exchange: Any = None
        self._address: str | None = None
        self._meta: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "HyperliquidPaperClient":
        """Build a client from ``settings`` (or the environment)."""
        return cls(settings)

    # -- lazy wiring ----------------------------------------------------------

    def _ensure(self) -> tuple[Any, Any, str]:
        """Build (once) and return the ``(info, exchange, address)`` triple."""
        if self._exchange is None:
            self._info, self._exchange, self._address = _build_clients(self.settings)
        assert self._address is not None
        return self._info, self._exchange, self._address

    def _coin_meta(self, coin: str) -> dict[str, Any]:
        """Return the universe metadata for ``coin`` (validates it exists)."""
        info, _, _ = self._ensure()
        if self._meta is None:
            self._meta = info.meta()
        universe = (self._meta or {}).get("universe", []) if isinstance(self._meta, dict) else []
        for asset in universe:
            if isinstance(asset, dict) and asset.get("name") == coin:
                return asset
        raise ExecutionError(f"unknown Hyperliquid perp coin: {coin!r}")

    def _sz_decimals(self, coin: str) -> int:
        try:
            return int(self._coin_meta(coin).get("szDecimals", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _round_size(self, coin: str, size: float) -> float:
        """Round an order size to the coin's ``szDecimals``."""
        return round(float(size), self._sz_decimals(coin))

    def _round_price(self, coin: str, price: float) -> float:
        """Round a limit price to Hyperliquid's perp wire format.

        Perp prices allow at most ``_PRICE_SIG_FIGS`` significant figures and at
        most ``_MAX_PRICE_DECIMALS - szDecimals`` decimal places.
        """
        px = float(price)
        if px <= 0:
            return 0.0
        sig = float(f"{px:.{_PRICE_SIG_FIGS}g}")
        max_dec = max(0, _MAX_PRICE_DECIMALS - self._sz_decimals(coin))
        return round(sig, max_dec)

    def _mid_price(self, coin: str) -> float:
        """Current mid price for ``coin`` from ``all_mids``."""
        info, _, _ = self._ensure()
        mids = info.all_mids()
        px = _to_float(mids.get(coin)) if isinstance(mids, dict) else None
        if px is None or px <= 0:
            raise ExecutionError(f"no live mid price available for {coin!r}")
        return px

    # -- reads ----------------------------------------------------------------

    def _spot_usdc(self) -> float | None:
        """Free USDC in the **spot** wallet, or ``None`` if it can't be read.

        Hyperliquid holds spot and perp USDC in separate wallets and draws spot
        collateral into perp as needed, so a flat perp account can read ``$0``
        while the funds actually sit in spot. Surfacing this avoids a misleading
        zero and lets the decision sizer see the real account size.
        """
        info, _, address = self._ensure()
        try:
            state = info.spot_user_state(address)
        except Exception:
            return None
        balances = state.get("balances") if isinstance(state, dict) else None
        if not isinstance(balances, list):
            return None
        for bal in balances:
            if isinstance(bal, dict) and bal.get("coin") == "USDC":
                return _to_float(bal.get("total"))
        return 0.0

    def account_state(self) -> dict[str, Any]:
        """Account equity, margin usage, open positions, and spot USDC (testnet)."""
        info, _, address = self._ensure()
        state = info.user_state(address)
        state = state if isinstance(state, dict) else {}
        ms = state.get("marginSummary") if isinstance(state.get("marginSummary"), dict) else {}
        positions: list[dict[str, Any]] = []
        for ap in state.get("assetPositions", []) or []:
            pos = ap.get("position", {}) if isinstance(ap, dict) else {}
            if not isinstance(pos, dict) or not pos:
                continue
            lev = pos.get("leverage") if isinstance(pos.get("leverage"), dict) else {}
            positions.append(
                {
                    "coin": pos.get("coin"),
                    "size": _to_float(pos.get("szi")),
                    "entry_price": _to_float(pos.get("entryPx")),
                    "position_value": _to_float(pos.get("positionValue")),
                    "unrealized_pnl": _to_float(pos.get("unrealizedPnl")),
                    "leverage": _to_float(lev.get("value")),
                    "leverage_type": lev.get("type"),
                    "liquidation_price": _to_float(pos.get("liquidationPx")),
                    "margin_used": _to_float(pos.get("marginUsed")),
                }
            )
        account_value = _to_float(ms.get("accountValue"))
        spot_usdc = self._spot_usdc()
        # Total collateral the account can deploy: perp equity + free spot USDC
        # (Hyperliquid auto-draws spot into perp when opening a position).
        tradable = (account_value or 0.0) + (spot_usdc or 0.0)
        return {
            "network": "testnet",
            "address": address,
            "account_value": account_value,
            "spot_usdc": spot_usdc,
            "tradable_usdc": round(tradable, 6),
            "total_margin_used": _to_float(ms.get("totalMarginUsed")),
            "total_notional_position": _to_float(ms.get("totalNtlPos")),
            "withdrawable": _to_float(state.get("withdrawable")),
            "positions": positions,
            "disclaimer": DISCLAIMER,
        }

    def open_orders(self) -> dict[str, Any]:
        """Currently resting orders (testnet), including TP/SL trigger orders.

        Prefers ``frontend_open_orders`` (which carries trigger metadata —
        order type, trigger price/condition, reduce-only) and falls back to the
        plain ``open_orders`` listing when unavailable.
        """
        info, _, address = self._ensure()
        try:
            rows = info.frontend_open_orders(address)
        except Exception:
            rows = info.open_orders(address)
        orders: list[dict[str, Any]] = []
        for o in rows or []:
            if not isinstance(o, dict):
                continue
            row: dict[str, Any] = {
                "coin": o.get("coin"),
                "oid": o.get("oid"),
                "side": "BUY" if o.get("side") == "B" else "SELL",
                "size": _to_float(o.get("sz")),
                "limit_price": _to_float(o.get("limitPx")),
                "timestamp": o.get("timestamp"),
            }
            if o.get("orderType") is not None:
                row["order_type"] = o.get("orderType")
            if o.get("reduceOnly") is not None:
                row["reduce_only"] = bool(o.get("reduceOnly"))
            if o.get("isTrigger"):
                row["is_trigger"] = True
                row["trigger_price"] = _to_float(o.get("triggerPx"))
                row["trigger_condition"] = o.get("triggerCondition")
            orders.append(row)
        return {"network": "testnet", "address": address, "open_orders": orders, "disclaimer": DISCLAIMER}

    def recent_fills(self, limit: int = 20) -> dict[str, Any]:
        """The most recent fills (testnet), newest first, capped at ``limit``."""
        info, _, address = self._ensure()
        rows = info.user_fills(address)
        rows = rows if isinstance(rows, list) else []
        fills = [
            {
                "coin": f.get("coin"),
                "side": "BUY" if f.get("side") == "B" else "SELL",
                "price": _to_float(f.get("px")),
                "size": _to_float(f.get("sz")),
                "closed_pnl": _to_float(f.get("closedPnl")),
                "fee": _to_float(f.get("fee")),
                "oid": f.get("oid"),
                "time": f.get("time"),
            }
            for f in rows[: max(1, int(limit))]
            if isinstance(f, dict)
        ]
        return {"network": "testnet", "address": address, "fills": fills, "disclaimer": DISCLAIMER}

    # -- writes ---------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: float, cross: bool = True) -> dict[str, Any]:
        """Set the leverage used for ``symbol`` (capped to the coin's maximum)."""
        coin = to_hyperliquid_coin(symbol)
        _, exchange, _ = self._ensure()
        max_lev = _to_float(self._coin_meta(coin).get("maxLeverage")) or float(leverage)
        lev = int(max(1, min(float(leverage), max_lev)))
        res = exchange.update_leverage(lev, coin, cross)
        return {
            "coin": coin,
            "leverage": lev,
            "cross": cross,
            "result": _normalize_order_result(res),
            "disclaimer": DISCLAIMER,
        }

    def _trigger_request(
        self, coin: str, is_buy: bool, sz: float, trigger_px: float, kind: str, slip: float
    ) -> dict[str, Any]:
        """Build one reduce-only trigger-market order request (``kind``: sl/tp).

        ``limit_px`` bounds the slippage of the market order that fires when the
        trigger trips: a closing BUY may pay up to ``trigger * (1 + slip)``, a
        closing SELL accepts down to ``trigger * (1 - slip)``.
        """
        trig = self._round_price(coin, float(trigger_px))
        bound = trig * (1.0 + slip) if is_buy else trig * (1.0 - slip)
        return {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": self._round_price(coin, bound),
            "order_type": {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": kind}},
            "reduce_only": True,
        }

    def _place_tpsl(
        self,
        coin: str,
        close_is_buy: bool,
        size: float,
        stop_loss: float | None,
        take_profit: float | None,
        slippage: float | None = None,
    ) -> dict[str, Any]:
        """Place reduce-only SL/TP trigger orders against an existing position.

        Orders go up under the ``positionTpsl`` grouping, so the exchange ties
        them to the position as an OCO pair: when one leg executes (or the
        position is closed) the other is cancelled. Trigger prices are validated
        against the live mid so a fresh stop can never fire instantly because it
        sits on the wrong side of the market.
        """
        _, exchange, _ = self._ensure()
        if stop_loss is None and take_profit is None:
            raise ExecutionError("provide stop_loss and/or take_profit")
        sz = self._round_size(coin, float(size))
        if sz <= 0:
            raise ExecutionError("TP/SL size rounds to zero - provide a positive size")
        mid = self._mid_price(coin)
        slip = self.settings.hl_default_slippage if slippage is None else float(slippage)

        # Closing BUY => the position is short: SL sits above price, TP below.
        # Closing SELL => the position is long:  SL sits below price, TP above.
        requests: list[dict[str, Any]] = []
        legs: dict[str, Any] = {}
        for kind, px in (("sl", stop_loss), ("tp", take_profit)):
            if px is None:
                continue
            above = close_is_buy if kind == "sl" else not close_is_buy
            if (float(px) > mid) != above:
                want = "above" if above else "below"
                name = "stop_loss" if kind == "sl" else "take_profit"
                raise ExecutionError(
                    f"{name} {px} would trigger immediately - for this position it must "
                    f"be {want} the current price ({mid})"
                )
            req = self._trigger_request(coin, close_is_buy, sz, float(px), kind, slip)
            requests.append(req)
            legs["stop_loss" if kind == "sl" else "take_profit"] = {
                "trigger_price": req["order_type"]["trigger"]["triggerPx"],
                "limit_price": req["limit_px"],
            }

        res = exchange.bulk_orders(requests, grouping="positionTpsl")
        return {
            "coin": coin,
            "side": "BUY" if close_is_buy else "SELL",
            "size": sz,
            "grouping": "positionTpsl",
            "legs": legs,
            "result": _normalize_order_result(res),
            "disclaimer": DISCLAIMER,
        }

    def set_position_tpsl(
        self,
        symbol: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        size: float | None = None,
        slippage: float | None = None,
    ) -> dict[str, Any]:
        """Attach exchange-side stop-loss / take-profit triggers to an open position.

        Reads the live position for ``symbol`` to derive the closing side and,
        when ``size`` is omitted, covers the full position. The legs are
        reduce-only trigger-market orders OCO-grouped to the position
        (``positionTpsl``), so they keep protecting the trade even when no client
        is connected.

        Raises:
            ExecutionError: if there is no open position, neither price is given,
                or a trigger sits on the wrong side of the current price.
        """
        coin = to_hyperliquid_coin(symbol)
        self._coin_meta(coin)  # validate coin
        position: dict[str, Any] | None = None
        for pos in self.account_state().get("positions", []):
            if pos.get("coin") == coin and pos.get("size"):
                position = pos
                break
        if position is None:
            raise ExecutionError(f"no open {coin} position to attach TP/SL to")
        pos_sz = float(position["size"])
        close_is_buy = pos_sz < 0  # closing a short buys back; closing a long sells
        return self._place_tpsl(
            coin,
            close_is_buy,
            abs(pos_sz) if size is None else float(size),
            stop_loss,
            take_profit,
            slippage,
        )

    def open_position(
        self,
        symbol: str,
        side: str,
        *,
        size: float | None = None,
        notional_usd: float | None = None,
        leverage: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
        reduce_only: bool = False,
        slippage: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Open (or add to) a position on testnet.

        Size is taken from ``size`` (coin units) or derived from ``notional_usd``
        at the reference price (the limit price for limit orders, else the live
        mid), then rounded to the coin's ``szDecimals``. ``leverage``, when given,
        is applied first. Market orders use ``market_open`` (IOC at mid +/-
        ``slippage``); limit orders rest a GTC order at ``limit_price``.

        ``stop_loss`` / ``take_profit``, when given, are attached as reduce-only
        OCO trigger orders (see :meth:`set_position_tpsl`) sized to the entry
        fill — but only once the entry actually fills; a resting limit entry
        skips the attach (use :meth:`set_position_tpsl` after it fills). A failed
        attach never voids the entry: the error is reported under ``"tpsl"``.
        """
        coin = to_hyperliquid_coin(symbol)
        info, exchange, _ = self._ensure()
        is_buy = _side_to_is_buy(side)
        otype = str(order_type or "market").strip().lower()
        self._coin_meta(coin)  # validate coin

        if leverage is not None:
            self.set_leverage(symbol, leverage, cross=True)

        if otype == "limit":
            if limit_price is None:
                raise ExecutionError("a limit order requires limit_price")
            ref_px = float(limit_price)
        else:
            ref_px = self._mid_price(coin)
        if ref_px <= 0:
            raise ExecutionError(f"invalid reference price for {coin!r}")

        if size is not None:
            sz = float(size)
        elif notional_usd is not None:
            sz = float(notional_usd) / ref_px
        else:
            raise ExecutionError("provide either size (coin units) or notional_usd")
        sz = self._round_size(coin, sz)
        if sz <= 0:
            raise ExecutionError(
                "order size rounds to zero at this price - increase size/notional"
            )

        notional = sz * ref_px
        if notional < _MIN_ORDER_USD:
            raise ExecutionError(
                f"order notional ${notional:.2f} is below Hyperliquid's ${_MIN_ORDER_USD:.0f} minimum"
            )

        if otype == "limit":
            px = self._round_price(coin, ref_px)
            res = exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}}, reduce_only=reduce_only)
        else:
            slip = self.settings.hl_default_slippage if slippage is None else float(slippage)
            res = exchange.market_open(coin, is_buy, sz, None, slip)

        out: dict[str, Any] = {
            "coin": coin,
            "side": "BUY" if is_buy else "SELL",
            "size": sz,
            "order_type": otype,
            "reduce_only": reduce_only,
            "reference_price": round(ref_px, 8),
            "notional_usd": round(notional, 2),
            "result": _normalize_order_result(res),
            "disclaimer": DISCLAIMER,
        }
        if stop_loss is not None or take_profit is not None:
            filled_sz = None
            for st in out["result"].get("statuses", []) or []:
                if isinstance(st, dict) and isinstance(st.get("filled"), dict):
                    filled_sz = _to_float(st["filled"].get("totalSz"))
            if out["result"].get("status") == "ok" and filled_sz:
                try:
                    # Entry just filled: the TP/SL closes it, so the side flips.
                    out["tpsl"] = self._place_tpsl(
                        coin, not is_buy, filled_sz, stop_loss, take_profit, slippage
                    )
                except ExecutionError as exc:
                    out["tpsl"] = {"error": str(exc)}
            else:
                out["tpsl"] = {
                    "skipped": (
                        "entry did not fill, so no TP/SL was attached - once it fills, "
                        "attach protection with set_position_tpsl (MCP: paper_set_tpsl)"
                    )
                }
        return out

    def close_position(self, symbol: str, size: float | None = None, slippage: float | None = None) -> dict[str, Any]:
        """Market-close all (or ``size`` units) of the ``symbol`` position."""
        coin = to_hyperliquid_coin(symbol)
        _, exchange, _ = self._ensure()
        sz = self._round_size(coin, float(size)) if size is not None else None
        slip = self.settings.hl_default_slippage if slippage is None else float(slippage)
        res = exchange.market_close(coin, sz, None, slip)
        return {
            "coin": coin,
            "size": sz,
            "action": "close",
            "result": _normalize_order_result(res),
            "disclaimer": DISCLAIMER,
        }

    def cancel_order(self, symbol: str, oid: int) -> dict[str, Any]:
        """Cancel a resting order by coin + order id."""
        coin = to_hyperliquid_coin(symbol)
        _, exchange, _ = self._ensure()
        res = exchange.cancel(coin, int(oid))
        return {
            "coin": coin,
            "oid": int(oid),
            "action": "cancel",
            "result": _normalize_order_result(res),
            "disclaimer": DISCLAIMER,
        }


__all__ = ["HyperliquidPaperClient", "ExecutionError", "_build_clients"]
