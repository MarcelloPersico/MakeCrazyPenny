"""Expert-report capability MCP server (see CONTRACT.md §9.2 ``reports``).

Exposes analyst-coverage and regulatory-filing capabilities over MCP:

  * :func:`analyst_ratings` — analyst recommendation distribution
    (strong-buy / buy / hold / sell / strong-sell).
  * :func:`price_targets` — analyst price-target summary (mean / high / low /
    current).
  * :func:`upgrades_downgrades` — analyst rating-change events, optionally
    filtered to those on or after a ``since`` date.
  * :func:`sec_filings` — SEC filings for a symbol, optionally restricted to a
    set of form types (defaults to ``10-K`` / ``10-Q`` / ``8-K``).

Each capability is implemented as a *pure async logic function* that calls
``get_registry().fetch(capability=..., **params)`` and shapes a compact result
``dict``. These logic functions are importable and unit-testable with a
monkeypatched module-level :func:`get_registry` — no SDK and no network required.
A thin ``@tool``-decorated wrapper sits in front of each logic function and wraps
its result with :func:`report_result` (which attaches the not-investment-advice
disclaimer and produces the canonical MCP text-content envelope). The decorated
wrappers are collected into a :data:`server` built with
:func:`create_sdk_mcp_server`.

The cross-capability ``cross_check`` reconciliation tool lives in the
``synthesis`` server, NOT here (CONTRACT.md §9.2): this module never imports
another server and is safe for ``synthesis`` to import for read-only composition.

Importing this module pulls in only the standard library plus ``core`` and the
provider package indirection; it is safe to import without the Claude Agent SDK,
without any optional heavy library, and without touching the network.
"""

from __future__ import annotations

from typing import Any

from ..providers import get_registry
from ._common import normalize_symbol, report_result
from ._sdk import create_sdk_mcp_server, tool

# Default SEC form types surfaced by :func:`sec_filings` when the caller does not
# restrict the set. Annual report, quarterly report, current report.
DEFAULT_FORMS: list[str] = ["10-K", "10-Q", "8-K"]


# --------------------------------------------------------------------------- #
# Pure async logic functions (testable with a monkeypatched get_registry()).   #
# --------------------------------------------------------------------------- #


async def analyst_ratings(symbol: str) -> dict[str, Any]:
    """Fetch the analyst recommendation distribution for ``symbol``.

    Args:
        symbol: Ticker symbol (normalized internally; e.g. ``" $aapl "`` →
            ``"AAPL"``).

    Returns:
        A compact result ``dict`` with the normalized ``symbol``, the serving
        ``provider``, whether the value came from ``cached`` storage, and the
        provider's normalized analyst-rating payload under ``ratings`` (an
        :class:`~makecrazypenny.core.types.AnalystRating` ``to_dict()`` result,
        or a list thereof).
    """
    sym = normalize_symbol(symbol)
    result = await get_registry().fetch(capability="analyst_ratings", symbol=sym)
    return {
        "symbol": sym,
        "provider": result["provider"],
        "cached": result["cached"],
        "ratings": result["data"],
    }


async def price_targets(symbol: str) -> dict[str, Any]:
    """Fetch the analyst price-target summary for ``symbol``.

    Args:
        symbol: Ticker symbol (normalized internally).

    Returns:
        A compact result ``dict`` with the normalized ``symbol``, the serving
        ``provider``, the ``cached`` flag, and the provider's normalized
        price-target payload under ``targets`` (a
        :class:`~makecrazypenny.core.types.PriceTarget` ``to_dict()`` result).
    """
    sym = normalize_symbol(symbol)
    result = await get_registry().fetch(capability="price_targets", symbol=sym)
    return {
        "symbol": sym,
        "provider": result["provider"],
        "cached": result["cached"],
        "targets": result["data"],
    }


async def upgrades_downgrades(symbol: str, since: str | None = None) -> dict[str, Any]:
    """Fetch analyst rating-change events for ``symbol``.

    When ``since`` is provided, events are filtered to those whose ``date`` is on
    or after ``since`` (lexicographic comparison on ISO-8601 ``YYYY-MM-DD``
    strings, which orders correctly by date). Events with no ``date`` are kept
    only when no ``since`` filter is given.

    Args:
        symbol: Ticker symbol (normalized internally).
        since: Optional ISO-8601 date (``YYYY-MM-DD``) lower bound, inclusive.

    Returns:
        A compact result ``dict`` with the normalized ``symbol``, the
        ``since`` echo, the serving ``provider``, the ``cached`` flag, a
        ``count`` of returned events, and the (optionally filtered) list of
        :class:`~makecrazypenny.core.types.UpgradeDowngrade` ``to_dict()``
        results under ``events``.
    """
    sym = normalize_symbol(symbol)
    result = await get_registry().fetch(capability="upgrades_downgrades", symbol=sym)

    events = result["data"]
    if not isinstance(events, list):
        events = [] if events is None else [events]

    if since is not None:
        events = [e for e in events if _event_on_or_after(e, since)]

    return {
        "symbol": sym,
        "since": since,
        "provider": result["provider"],
        "cached": result["cached"],
        "count": len(events),
        "events": events,
    }


async def sec_filings(
    symbol: str,
    forms: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch SEC filings for ``symbol``, optionally restricted to ``forms``.

    Args:
        symbol: Ticker symbol (normalized internally).
        forms: Form types to include (e.g. ``["10-K", "10-Q", "8-K"]``). When
            omitted (``None``), defaults to :data:`DEFAULT_FORMS`. Comparison is
            case-insensitive.

    Returns:
        A compact result ``dict`` with the normalized ``symbol``, the requested
        ``forms``, the serving ``provider``, the ``cached`` flag, a ``count`` of
        returned filings, and the filtered list of
        :class:`~makecrazypenny.core.types.Filing` ``to_dict()`` results under
        ``filings``.
    """
    sym = normalize_symbol(symbol)
    wanted = list(forms) if forms is not None else list(DEFAULT_FORMS)
    result = await get_registry().fetch(capability="sec_filings", symbol=sym, forms=wanted)

    filings = result["data"]
    if not isinstance(filings, list):
        filings = [] if filings is None else [filings]

    wanted_norm = {f.strip().upper() for f in wanted}
    if wanted_norm:
        filings = [f for f in filings if _filing_form(f) in wanted_norm]

    return {
        "symbol": sym,
        "forms": wanted,
        "provider": result["provider"],
        "cached": result["cached"],
        "count": len(filings),
        "filings": filings,
    }


# --------------------------------------------------------------------------- #
# Small helpers.                                                               #
# --------------------------------------------------------------------------- #


def _event_on_or_after(event: Any, since: str) -> bool:
    """Return ``True`` if a rating-change ``event``'s date is >= ``since``.

    Events are provider ``to_dict()`` payloads (``dict``); a missing/empty date
    excludes the event when a ``since`` filter is active.
    """
    date = event.get("date") if isinstance(event, dict) else None
    if not date:
        return False
    return str(date) >= since


def _filing_form(filing: Any) -> str:
    """Return a filing's normalized (upper-cased) form type, or ``""``."""
    form = filing.get("form") if isinstance(filing, dict) else None
    return str(form).strip().upper() if form else ""


# --------------------------------------------------------------------------- #
# MCP tool wiring — thin @tool-wrapped adapters over the logic functions.      #
# The raw logic functions above stay directly importable/callable for tests.   #
# --------------------------------------------------------------------------- #


@tool(
    "analyst_ratings",
    "Analyst recommendation distribution (strong-buy/buy/hold/sell/strong-sell) "
    "for a stock symbol.",
    {"symbol": str},
)
async def analyst_ratings_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`analyst_ratings`."""
    return report_result(await analyst_ratings(args["symbol"]))


@tool(
    "price_targets",
    "Analyst price-target summary (mean/high/low/current) for a stock symbol.",
    {"symbol": str},
)
async def price_targets_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`price_targets`."""
    return report_result(await price_targets(args["symbol"]))


@tool(
    "upgrades_downgrades",
    "Analyst rating-change events (upgrades/downgrades/initiations) for a stock "
    "symbol, optionally filtered to events on or after a 'since' date "
    "(YYYY-MM-DD).",
    {"symbol": str, "since": str},
)
async def upgrades_downgrades_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`upgrades_downgrades`."""
    return report_result(
        await upgrades_downgrades(args["symbol"], since=args.get("since"))
    )


@tool(
    "sec_filings",
    "SEC filings for a stock symbol, optionally restricted to a list of form "
    "types (defaults to 10-K, 10-Q, 8-K).",
    {"symbol": str, "forms": list},
)
async def sec_filings_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`sec_filings`."""
    return report_result(await sec_filings(args["symbol"], forms=args.get("forms")))


# The MCP server descriptor. With the real SDK present this is a genuine
# in-process MCP server; without it, ``_sdk.create_sdk_mcp_server`` returns a
# lightweight stub descriptor so module import still succeeds.
server = create_sdk_mcp_server(
    name="reports",
    version="0.1.0",
    tools=[
        analyst_ratings_tool,
        price_targets_tool,
        upgrades_downgrades_tool,
        sec_filings_tool,
    ],
)


__all__ = [
    "analyst_ratings",
    "price_targets",
    "upgrades_downgrades",
    "sec_filings",
    "analyst_ratings_tool",
    "price_targets_tool",
    "upgrades_downgrades_tool",
    "sec_filings_tool",
    "server",
    "get_registry",
    "DEFAULT_FORMS",
]


# --------------------------------------------------------------------------- #
# Guarded stdio runner (CONTRACT.md §9.1 step 4).                              #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import sys

    from ._sdk import SDK_AVAILABLE

    if not SDK_AVAILABLE:
        print(
            "claude_agent_sdk is not installed; the 'reports' MCP server cannot "
            "run over stdio. Install it with: pip install claude-agent-sdk",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # The real SDK exposes a stdio runner for an in-process MCP server. Import
    # lazily so this module stays importable without the SDK.
    try:
        from claude_agent_sdk import run_mcp_server_stdio  # type: ignore[import-not-found]
    except ImportError:  # older/newer SDK surface; try the generic entrypoint.
        run_mcp_server_stdio = None  # type: ignore[assignment]

    if run_mcp_server_stdio is None:
        print(
            "The installed claude_agent_sdk does not expose a stdio runner "
            "entrypoint for in-process servers.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    run_mcp_server_stdio(server)
