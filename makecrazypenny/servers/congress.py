"""Congressional-trade & insider-transaction capability server (CONTRACT.md §9).

Layer-1 server exposing congressional stock-trade disclosures and corporate
insider transactions over MCP. It follows the per-server pattern from
CONTRACT.md §9.1:

  1. **Pure async logic functions** — ``async def`` functions that call
     :func:`get_registry` and shape a compact result ``dict``. These are
     importable and unit-testable by monkeypatching the module-level
     :func:`get_registry` to inject a fake registry. They never require the SDK
     or the network.
  2. **Module-level :func:`get_registry`** — a thin re-export of
     :func:`makecrazypenny.providers.get_registry` so tests can monkeypatch it.
  3. **MCP wiring** — each logic function is wrapped with ``@tool`` (real or the
     graceful shim from :mod:`makecrazypenny.servers._sdk`) and registered on a
     ``create_sdk_mcp_server`` instance named ``"congress"``. Each tool returns
     :func:`text_result`.
  4. **stdio guard** — ``if __name__ == "__main__":`` runs the stdio server, or
     exits non-zero with a clear message if the real SDK is absent.

Tools (all surface the disclosure-lag caveat in their output):

  * ``congress_trades(symbol_or_member, since=None)`` — trades for a ticker or a
    named member of Congress.
  * ``recent_congress_activity(days=7)`` — recently *disclosed* congressional
    trades across all tracked members/symbols.
  * ``insider_transactions(symbol)`` — corporate insider (officer / director /
    10%-owner) transactions for a symbol.
  * ``new_disclosures(watchlist, since=None)`` — alert feed of newly disclosed
    congressional trades touching any symbol on a watchlist.

**Disclosure-lag caveat.** Under the STOCK Act, members of Congress have up to
~30–45 days to disclose a trade (and enforcement is weak), so a "recent"
disclosure may describe a transaction that happened weeks earlier. Every tool's
output carries a ``caveat`` field stating this so downstream agents and users
never treat the feed as real-time.
"""

from __future__ import annotations

from typing import Any

from ..providers import get_registry as _provider_get_registry
from ._common import normalize_symbol, text_result
from ._sdk import HAS_SDK, create_sdk_mcp_server, tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Human-readable caveat surfaced in every tool result. Congressional trade
#: disclosures lag the actual transaction by up to ~30–45 days under the STOCK
#: Act, so this feed must never be treated as real-time.
DISCLOSURE_LAG_CAVEAT: str = (
    "Congressional trade disclosures are subject to a reporting lag of roughly "
    "30-45 days under the STOCK Act (and enforcement is weak): a trade shown "
    "here may have been executed weeks before it was disclosed. Treat this data "
    "as historical, not real-time."
)

#: Caveat for the insider-transaction tool. SEC Form 4 must be filed within two
#: business days, so the lag is far smaller, but a small lag still exists.
INSIDER_LAG_CAVEAT: str = (
    "Corporate insider transactions are reported on SEC Form 4, generally due "
    "within two business days of the trade; a short reporting lag still applies."
)


# ---------------------------------------------------------------------------
# Registry indirection (monkeypatchable by tests)
# ---------------------------------------------------------------------------


def get_registry() -> Any:
    """Return the process-wide provider registry.

    A thin indirection over :func:`makecrazypenny.providers.get_registry` so
    that tests can monkeypatch *this* module's ``get_registry`` to inject a fake
    registry without touching the real provider singleton.

    Returns:
        The shared provider registry (anything exposing an async ``fetch``).
    """
    return _provider_get_registry()


# ---------------------------------------------------------------------------
# Internal helpers (pure)
# ---------------------------------------------------------------------------


def _looks_like_symbol(value: str) -> bool:
    """Heuristically decide whether ``value`` is a ticker symbol vs a member name.

    The two namespaces overlap (a short surname like ``"Cruz"`` reads like a
    ticker), so this leans on how callers actually write each one:

      * A leading ``$`` cashtag is always a symbol.
      * A value with a space, or longer than six characters once the ``$`` is
        stripped, is a member name.
      * A ticker is conventionally written in all-caps (optionally with digits
        and ``.``/``-`` for class shares / suffixes, e.g. ``BRK.B``); a member
        name is written in mixed/title case (``"Pelosi"``). So a value
        containing any lowercase letter is treated as a member name.

    Args:
        value: The raw ``symbol_or_member`` argument.

    Returns:
        ``True`` if ``value`` looks like a ticker symbol, ``False`` otherwise.
    """
    cleaned = value.strip()
    if not cleaned or " " in cleaned:
        return False
    had_cashtag = cleaned.startswith("$")
    core = cleaned.lstrip("$")
    if not core or len(core) > 6:
        return False
    if not all(ch.isalnum() or ch in ".-" for ch in core):
        return False
    # An explicit cashtag is unambiguous; otherwise require all-caps (no
    # lowercase) so mixed-case surnames are routed to the member path.
    if had_cashtag:
        return True
    return not any(ch.islower() for ch in core)


def _disclosure_sort_key(trade: dict[str, Any]) -> str:
    """Sort key for a trade dict: most-recent disclosure first.

    Falls back to the transaction date, then the empty string, so trades with
    missing dates sort last.

    Args:
        trade: A :class:`~makecrazypenny.core.types.CongressTrade` ``to_dict``.

    Returns:
        A string usable as a descending-sort key.
    """
    return str(trade.get("disclosure_date") or trade.get("transaction_date") or "")


def _coerce_trade_list(data: Any) -> list[dict[str, Any]]:
    """Normalize a registry ``data`` payload into a list of trade dicts.

    Providers return ``to_dict()`` output — a single dict or a list of dicts (or
    occasionally ``None``). This collapses all of those into a plain list.

    Args:
        data: The ``data`` field of a registry fetch envelope.

    Returns:
        A list of trade/transaction dicts (possibly empty).
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _filter_since(
    trades: list[dict[str, Any]], since: str | None
) -> list[dict[str, Any]]:
    """Keep only trades disclosed (or transacted) on/after ``since``.

    Comparison is lexicographic on ISO-8601-style date strings, which orders
    correctly for ``YYYY-MM-DD`` (and longer) timestamps. Trades with no usable
    date are dropped when a ``since`` bound is given.

    Args:
        trades: Trade dicts to filter.
        since: Inclusive lower bound as an ISO date string, or ``None`` for no
            filtering.

    Returns:
        The filtered list (a new list; input is not mutated).
    """
    if not since:
        return list(trades)
    bound = since.strip()
    kept: list[dict[str, Any]] = []
    for trade in trades:
        stamp = trade.get("disclosure_date") or trade.get("transaction_date")
        if stamp is not None and str(stamp) >= bound:
            kept.append(trade)
    return kept


def _filter_member(
    trades: list[dict[str, Any]], member: str
) -> list[dict[str, Any]]:
    """Keep only trades whose ``member`` field contains ``member`` (case-insensitive).

    Args:
        trades: Trade dicts to filter.
        member: The member-name substring to match.

    Returns:
        The filtered list (a new list; input is not mutated).
    """
    needle = member.strip().casefold()
    return [
        trade
        for trade in trades
        if needle in str(trade.get("member") or "").casefold()
    ]


# ---------------------------------------------------------------------------
# Pure async logic functions
# ---------------------------------------------------------------------------


async def congress_trades(
    symbol_or_member: str, since: str | None = None
) -> dict[str, Any]:
    """Congressional stock trades for a ticker symbol or a named member.

    If ``symbol_or_member`` looks like a ticker (short, no spaces) the
    ``congress_trades`` capability is fetched for that symbol. Otherwise the
    value is treated as a member name: trades are fetched without a symbol filter
    and then narrowed to disclosures whose ``member`` field matches the name.

    Args:
        symbol_or_member: A ticker symbol (e.g. ``"NVDA"``) or a member of
            Congress (e.g. ``"Pelosi"``).
        since: Optional inclusive ISO-date lower bound; only trades disclosed (or
            transacted) on/after this date are returned.

    Returns:
        A compact result dict with the resolved query, the matching ``trades``
        (most-recent disclosure first), a ``count``, provenance/cache metadata,
        and the :data:`DISCLOSURE_LAG_CAVEAT`.
    """
    raw = symbol_or_member.strip()
    is_symbol = _looks_like_symbol(raw)

    result: dict[str, Any] = {
        "query": raw,
        "query_type": "symbol" if is_symbol else "member",
        "since": since,
        "trades": [],
        "count": 0,
        "caveat": DISCLOSURE_LAG_CAVEAT,
    }

    registry = get_registry()
    try:
        if is_symbol:
            symbol = normalize_symbol(raw)
            result["query"] = symbol
            envelope = await registry.fetch("congress_trades", symbol=symbol)
        else:
            envelope = await registry.fetch("congress_trades")
    except Exception as exc:  # noqa: BLE001 - surface as data, never crash the tool
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    trades = _coerce_trade_list(envelope.get("data"))
    if not is_symbol:
        trades = _filter_member(trades, raw)
    trades = _filter_since(trades, since)
    trades.sort(key=_disclosure_sort_key, reverse=True)

    result["trades"] = trades
    result["count"] = len(trades)
    result["provider"] = envelope.get("provider")
    result["cached"] = envelope.get("cached")
    return result


async def recent_congress_activity(days: int = 7) -> dict[str, Any]:
    """Recently *disclosed* congressional trades across all tracked members.

    Fetches the ``congress_trades`` capability without a symbol filter and keeps
    disclosures whose ``disclosure_date`` falls within the trailing ``days``
    window. Because of the reporting lag (see :data:`DISCLOSURE_LAG_CAVEAT`),
    "recent" refers to when a trade was *disclosed*, not when it occurred.

    Args:
        days: Size of the trailing disclosure window in days (must be positive;
            non-positive values are clamped to 1).

    Returns:
        A result dict with the ``window_days``, the cutoff date used, the
        matching ``trades`` (most-recent disclosure first), a ``count``,
        provenance/cache metadata, and the disclosure-lag caveat.
    """
    from datetime import datetime, timedelta, timezone

    window = max(1, int(days))
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=window)
    cutoff = cutoff_dt.date().isoformat()

    result: dict[str, Any] = {
        "window_days": window,
        "since": cutoff,
        "trades": [],
        "count": 0,
        "caveat": DISCLOSURE_LAG_CAVEAT,
    }

    registry = get_registry()
    try:
        envelope = await registry.fetch("congress_trades")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    trades = _coerce_trade_list(envelope.get("data"))
    trades = _filter_since(trades, cutoff)
    trades.sort(key=_disclosure_sort_key, reverse=True)

    result["trades"] = trades
    result["count"] = len(trades)
    result["provider"] = envelope.get("provider")
    result["cached"] = envelope.get("cached")
    return result


async def insider_transactions(symbol: str) -> dict[str, Any]:
    """Corporate insider (officer / director / 10%-owner) transactions for a symbol.

    Fetches the ``insider_transactions`` capability for the normalized symbol and
    returns the transactions most-recent first.

    Args:
        symbol: The ticker symbol (normalized via
            :func:`makecrazypenny.servers._common.normalize_symbol`).

    Returns:
        A result dict with the resolved ``symbol``, the matching
        ``transactions`` (most-recent first), a ``count``, provenance/cache
        metadata, and the :data:`INSIDER_LAG_CAVEAT`.
    """
    sym = normalize_symbol(symbol)
    result: dict[str, Any] = {
        "symbol": sym,
        "transactions": [],
        "count": 0,
        "caveat": INSIDER_LAG_CAVEAT,
    }

    registry = get_registry()
    try:
        envelope = await registry.fetch("insider_transactions", symbol=sym)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    txns = _coerce_trade_list(envelope.get("data"))
    txns.sort(key=lambda t: str(t.get("date") or ""), reverse=True)

    result["transactions"] = txns
    result["count"] = len(txns)
    result["provider"] = envelope.get("provider")
    result["cached"] = envelope.get("cached")
    return result


async def new_disclosures(
    watchlist: list[str] | str, since: str | None = None
) -> dict[str, Any]:
    """Alert feed: newly disclosed congressional trades touching a watchlist.

    For each symbol on ``watchlist`` the ``congress_trades`` capability is
    fetched and filtered to disclosures on/after ``since``. Results are merged,
    de-duplicated, and sorted most-recent disclosure first — an at-a-glance feed
    of "what's new" for the symbols a user cares about.

    Args:
        watchlist: A list of ticker symbols, or a single comma/space-separated
            string of symbols.
        since: Optional inclusive ISO-date lower bound for the disclosure date.
            When ``None`` the full available history per symbol is returned.

    Returns:
        A result dict with the normalized ``watchlist``, the ``since`` bound, the
        merged ``disclosures`` (most-recent first), a total ``count``, a
        ``per_symbol`` count breakdown, an ``errors`` map for any symbols that
        failed to fetch, and the disclosure-lag caveat.
    """
    symbols = _normalize_watchlist(watchlist)

    result: dict[str, Any] = {
        "watchlist": symbols,
        "since": since,
        "disclosures": [],
        "count": 0,
        "per_symbol": {},
        "errors": {},
        "caveat": DISCLOSURE_LAG_CAVEAT,
    }
    if not symbols:
        return result

    registry = get_registry()
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for sym in symbols:
        try:
            envelope = await registry.fetch("congress_trades", symbol=sym)
        except Exception as exc:  # noqa: BLE001
            result["errors"][sym] = f"{type(exc).__name__}: {exc}"
            result["per_symbol"][sym] = 0
            continue

        trades = _coerce_trade_list(envelope.get("data"))
        trades = _filter_since(trades, since)
        result["per_symbol"][sym] = len(trades)
        for trade in trades:
            dedup_key = (
                trade.get("symbol"),
                trade.get("member"),
                trade.get("transaction"),
                trade.get("transaction_date"),
                trade.get("disclosure_date"),
                trade.get("amount_range"),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            merged.append(trade)

    merged.sort(key=_disclosure_sort_key, reverse=True)
    result["disclosures"] = merged
    result["count"] = len(merged)
    return result


def _normalize_watchlist(watchlist: list[str] | str) -> list[str]:
    """Normalize a watchlist argument into a de-duplicated list of symbols.

    Accepts a list of symbols or a single comma/space-separated string. Each
    entry is normalized via
    :func:`makecrazypenny.servers._common.normalize_symbol`; blanks are dropped
    and order is preserved.

    Args:
        watchlist: A list of symbols or a delimited string.

    Returns:
        A de-duplicated, order-preserving list of normalized symbols.
    """
    if isinstance(watchlist, str):
        raw_items = watchlist.replace(",", " ").split()
    else:
        raw_items = list(watchlist)

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            continue
        sym = normalize_symbol(item)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


# ---------------------------------------------------------------------------
# MCP tool wrappers (thin; pass logic output through text_result)
# ---------------------------------------------------------------------------


@tool(
    "congress_trades",
    "Congressional stock trades for a ticker symbol or a named member of "
    "Congress. Note: disclosures lag the actual trade by ~30-45 days.",
    {"symbol_or_member": str, "since": str},
)
async def congress_trades_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`congress_trades`."""
    data = await congress_trades(
        args["symbol_or_member"], since=args.get("since")
    )
    return text_result(data)


@tool(
    "recent_congress_activity",
    "Recently disclosed congressional trades across all tracked members within "
    "a trailing window. Note: 'recent' means recently disclosed, not recently "
    "traded (disclosures lag ~30-45 days).",
    {"days": int},
)
async def recent_congress_activity_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`recent_congress_activity`."""
    data = await recent_congress_activity(days=int(args.get("days", 7)))
    return text_result(data)


@tool(
    "insider_transactions",
    "Corporate insider (officer/director/10%-owner) transactions for a symbol, "
    "reported on SEC Form 4 (generally within two business days).",
    {"symbol": str},
)
async def insider_transactions_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`insider_transactions`."""
    data = await insider_transactions(args["symbol"])
    return text_result(data)


@tool(
    "new_disclosures",
    "Alert feed of newly disclosed congressional trades touching any symbol on a "
    "watchlist. Note: disclosures lag the actual trade by ~30-45 days.",
    {"watchlist": list, "since": str},
)
async def new_disclosures_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP wrapper for :func:`new_disclosures`."""
    data = await new_disclosures(args["watchlist"], since=args.get("since"))
    return text_result(data)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

server = create_sdk_mcp_server(
    name="congress",
    version="0.1.0",
    tools=[
        congress_trades_tool,
        recent_congress_activity_tool,
        insider_transactions_tool,
        new_disclosures_tool,
    ],
)


__all__ = [
    "DISCLOSURE_LAG_CAVEAT",
    "INSIDER_LAG_CAVEAT",
    "get_registry",
    "congress_trades",
    "recent_congress_activity",
    "insider_transactions",
    "new_disclosures",
    "congress_trades_tool",
    "recent_congress_activity_tool",
    "insider_transactions_tool",
    "new_disclosures_tool",
    "server",
]


# ---------------------------------------------------------------------------
# stdio runner (guarded)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if not HAS_SDK:
        print(
            "claude-agent-sdk is not installed; the congress MCP server cannot "
            "run over stdio. Install it with: pip install claude-agent-sdk",
            file=sys.stderr,
        )
        sys.exit(1)

    import anyio  # type: ignore[import-not-found]
    from claude_agent_sdk import run_mcp_server_stdio  # type: ignore[import-not-found]

    anyio.run(run_mcp_server_stdio, server)
