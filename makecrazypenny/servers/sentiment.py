"""Sentiment capability MCP server (see CONTRACT.md §9.1 and §9.2).

Layer-1, agent-agnostic capability server exposing market-sentiment tools over
MCP. It is a thin wiring layer over the Layer-0 :class:`ProviderRegistry`:

  * ``get_news`` -> registry ``company_news``
  * ``news_sentiment`` -> registry ``news_sentiment``
  * ``social_sentiment`` -> registry ``social_sentiment``
  * ``aggregate_sentiment`` -> blends news + social into one score + top drivers

Design (per CONTRACT.md §9.1):

  * **Pure async logic functions** (``get_news``, ``news_sentiment``,
    ``social_sentiment``, ``aggregate_sentiment``) call the module-level
    :func:`get_registry` and shape a compact JSON-ready ``dict``. They are
    importable and unit-testable by monkeypatching :func:`get_registry`.
  * **Module-level :func:`get_registry`** — a thin indirection re-exported from
    :mod:`makecrazypenny.providers` so tests can swap in a fake registry.
  * **MCP wiring** — each logic function is wrapped by a thin ``@tool`` adapter
    that returns ``text_result(...)``; the adapters are collected into
    ``server = create_sdk_mcp_server(name="sentiment", ...)``.
  * **stdio guard** — ``if __name__ == "__main__":`` runs the stdio server, or
    prints a clear message and exits non-zero when the SDK is absent.

Deep web search is performed by the agent layer (Layer 2), not here.

Importing this module pulls in only the standard library plus ``core`` and the
SDK shims; it never requires the Claude Agent SDK, any API key, or the network.
"""

from __future__ import annotations

from typing import Any

from ..providers import get_registry
from ._common import normalize_symbol, text_result
from ._sdk import HAS_SDK, create_sdk_mcp_server, tool

# Re-export so the symbol is importable as ``sentiment.get_registry`` and is the
# single monkeypatch target for tests (see CONTRACT.md §9.1.2).
__all__ = [
    "get_registry",
    "get_news",
    "news_sentiment",
    "social_sentiment",
    "aggregate_sentiment",
    "get_news_tool",
    "news_sentiment_tool",
    "social_sentiment_tool",
    "aggregate_sentiment_tool",
    "server",
]


# ---------------------------------------------------------------------------
# Small shaping helpers
# ---------------------------------------------------------------------------

# Coarse score thresholds for turning a blended numeric score into a label.
_BULLISH_THRESHOLD = 0.15
_BEARISH_THRESHOLD = -0.15


def _label_for_score(score: float) -> str:
    """Map a numeric sentiment score in ``[-1, 1]`` to a coarse label."""
    if score >= _BULLISH_THRESHOLD:
        return "bullish"
    if score <= _BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _as_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float coercion that never raises (returns ``default``)."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion that never raises (returns ``default``)."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_payload(symbol: str, capability: str, exc: Exception) -> dict[str, Any]:
    """Build a uniform, JSON-ready error dict for an unservable capability.

    Tools always return a ``text_result``; when the whole provider chain fails
    (``AllProvidersFailed``) or any unexpected error occurs, this records the
    failure so the calling agent can reason about it rather than crashing.
    """
    return {
        "symbol": symbol,
        "capability": capability,
        "error": str(exc) or exc.__class__.__name__,
        "available": False,
    }


# ---------------------------------------------------------------------------
# Pure async logic functions (testable via a monkeypatched get_registry)
# ---------------------------------------------------------------------------


async def get_news(symbol: str, days: int = 7) -> dict[str, Any]:
    """Fetch recent company news for ``symbol`` over the last ``days`` days.

    Calls the registry ``company_news`` capability and shapes a compact result.
    The registry returns a list of ``NewsItem.to_dict()`` dicts under ``data``.

    Args:
        symbol: Ticker symbol (normalized internally).
        days: Look-back window in days (clamped to ``>= 1``).

    Returns:
        A JSON-ready dict with the symbol, window, provider/cached provenance,
        article count, and the list of news items (empty on failure).
    """
    sym = normalize_symbol(symbol)
    window = max(1, _as_int(days, 7))
    try:
        env = await get_registry().fetch("company_news", symbol=sym, days=window)
    except Exception as exc:  # AllProvidersFailed or anything unexpected
        payload = _error_payload(sym, "company_news", exc)
        payload.update({"days": window, "count": 0, "articles": []})
        return payload

    data = env.get("data")
    articles = data if isinstance(data, list) else ([] if data is None else [data])
    return {
        "symbol": sym,
        "days": window,
        "provider": env.get("provider"),
        "cached": env.get("cached"),
        "count": len(articles),
        "articles": articles,
    }


async def news_sentiment(symbol: str) -> dict[str, Any]:
    """Fetch news-derived sentiment for ``symbol``.

    Calls the registry ``news_sentiment`` capability. The registry returns a
    single ``SentimentScore.to_dict()`` dict under ``data``.

    Args:
        symbol: Ticker symbol (normalized internally).

    Returns:
        A JSON-ready dict with the symbol, provider/cached provenance, and the
        sentiment score/label/drivers (an error marker on failure).
    """
    sym = normalize_symbol(symbol)
    try:
        env = await get_registry().fetch("news_sentiment", symbol=sym)
    except Exception as exc:
        return _error_payload(sym, "news_sentiment", exc)

    data = env.get("data")
    score = data if isinstance(data, dict) else {}
    return {
        "symbol": sym,
        "provider": env.get("provider"),
        "cached": env.get("cached"),
        "score": _as_float(score.get("score")),
        "label": score.get("label") or _label_for_score(_as_float(score.get("score"))),
        "n_articles": _as_int(score.get("n_articles")),
        "drivers": list(score.get("drivers") or []),
        "sentiment": score,
    }


async def social_sentiment(symbol: str) -> dict[str, Any]:
    """Fetch social-media-derived sentiment for ``symbol``.

    Calls the registry ``social_sentiment`` capability. The registry returns a
    single ``SentimentScore.to_dict()`` dict under ``data``.

    Args:
        symbol: Ticker symbol (normalized internally).

    Returns:
        A JSON-ready dict with the symbol, provider/cached provenance, and the
        sentiment score/label/drivers (an error marker on failure).
    """
    sym = normalize_symbol(symbol)
    try:
        env = await get_registry().fetch("social_sentiment", symbol=sym)
    except Exception as exc:
        return _error_payload(sym, "social_sentiment", exc)

    data = env.get("data")
    score = data if isinstance(data, dict) else {}
    return {
        "symbol": sym,
        "provider": env.get("provider"),
        "cached": env.get("cached"),
        "score": _as_float(score.get("score")),
        "label": score.get("label") or _label_for_score(_as_float(score.get("score"))),
        "n_articles": _as_int(score.get("n_articles")),
        "drivers": list(score.get("drivers") or []),
        "sentiment": score,
    }


def _blend(
    news: dict[str, Any],
    social: dict[str, Any],
) -> tuple[float, list[str], dict[str, Any]]:
    """Blend the news and social sentiment results into one score + drivers.

    The blend is an article-count-weighted mean of the two component scores,
    falling back to an equal-weight mean when neither component reports a sample
    size. Top drivers are de-duplicated, news first then social.

    Args:
        news: The :func:`news_sentiment` result dict.
        social: The :func:`social_sentiment` result dict.

    Returns:
        A ``(blended_score, drivers, components)`` tuple where ``blended_score``
        is clamped to ``[-1, 1]``, ``drivers`` is the merged driver list, and
        ``components`` summarizes each side's contribution.
    """
    news_ok = bool(news) and news.get("available", True) is not False
    social_ok = bool(social) and social.get("available", True) is not False

    news_score = _as_float(news.get("score")) if news_ok else 0.0
    social_score = _as_float(social.get("score")) if social_ok else 0.0
    news_n = _as_int(news.get("n_articles")) if news_ok else 0
    social_n = _as_int(social.get("n_articles")) if social_ok else 0

    # Presence weights: a component with no data contributes nothing.
    news_present = 1.0 if news_ok else 0.0
    social_present = 1.0 if social_ok else 0.0

    if news_n + social_n > 0:
        # Article-count weighting, but only over components that are present.
        w_news = float(news_n) * news_present
        w_social = float(social_n) * social_present
    else:
        # No counts available: equal weight over present components.
        w_news = news_present
        w_social = social_present

    total_w = w_news + w_social
    if total_w > 0:
        blended = (news_score * w_news + social_score * w_social) / total_w
    else:
        blended = 0.0
    blended = max(-1.0, min(1.0, blended))

    drivers: list[str] = []
    seen: set[str] = set()
    for src in (news, social):
        if not src or src.get("available", True) is False:
            continue
        for d in src.get("drivers") or []:
            key = str(d).strip()
            if key and key.lower() not in seen:
                seen.add(key.lower())
                drivers.append(key)

    components = {
        "news": {
            "available": news_ok,
            "score": news_score,
            "label": news.get("label") if news_ok else None,
            "n_articles": news_n,
            "weight": round(w_news / total_w, 4) if total_w > 0 else 0.0,
        },
        "social": {
            "available": social_ok,
            "score": social_score,
            "label": social.get("label") if social_ok else None,
            "n_articles": social_n,
            "weight": round(w_social / total_w, 4) if total_w > 0 else 0.0,
        },
    }
    return blended, drivers, components


async def aggregate_sentiment(symbol: str, max_drivers: int = 5) -> dict[str, Any]:
    """Blend news + social sentiment for ``symbol`` into one score and drivers.

    Fetches both component sentiments (independently, so one missing component
    does not sink the other), computes an article-count-weighted blended score,
    derives a coarse label, and surfaces the top drivers.

    Args:
        symbol: Ticker symbol (normalized internally).
        max_drivers: Maximum number of top drivers to surface (clamped to
            ``>= 0``).

    Returns:
        A JSON-ready dict with the blended ``score``/``label``, ``drivers``
        (truncated to ``max_drivers``), per-component ``components`` summary, and
        the full component results under ``news``/``social``.
    """
    sym = normalize_symbol(symbol)
    limit = max(0, _as_int(max_drivers, 5))

    news = await news_sentiment(sym)
    social = await social_sentiment(sym)

    blended, drivers, components = _blend(news, social)
    any_available = components["news"]["available"] or components["social"]["available"]

    return {
        "symbol": sym,
        "score": round(blended, 4),
        "label": _label_for_score(blended) if any_available else "unknown",
        "available": any_available,
        "drivers": drivers[:limit],
        "components": components,
        "news": news,
        "social": social,
    }


# ---------------------------------------------------------------------------
# MCP tool wrappers (thin adapters -> text_result)
# ---------------------------------------------------------------------------


@tool(
    "get_news",
    "Recent company news headlines for a stock symbol over the last N days.",
    {"symbol": str, "days": int},
)
async def get_news_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`get_news`."""
    return text_result(
        await get_news(args["symbol"], days=int(args.get("days", 7)))
    )


@tool(
    "news_sentiment",
    "News-derived sentiment score and drivers for a stock symbol.",
    {"symbol": str},
)
async def news_sentiment_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`news_sentiment`."""
    return text_result(await news_sentiment(args["symbol"]))


@tool(
    "social_sentiment",
    "Social-media-derived sentiment score and drivers for a stock symbol.",
    {"symbol": str},
)
async def social_sentiment_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`social_sentiment`."""
    return text_result(await social_sentiment(args["symbol"]))


@tool(
    "aggregate_sentiment",
    "Blend news and social sentiment into one score with top drivers.",
    {"symbol": str, "max_drivers": int},
)
async def aggregate_sentiment_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`aggregate_sentiment`."""
    return text_result(
        await aggregate_sentiment(
            args["symbol"], max_drivers=int(args.get("max_drivers", 5))
        )
    )


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

server = create_sdk_mcp_server(
    name="sentiment",
    version="0.1.0",
    tools=[
        get_news_tool,
        news_sentiment_tool,
        social_sentiment_tool,
        aggregate_sentiment_tool,
    ],
)


# ---------------------------------------------------------------------------
# stdio runner
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the sentiment server over stdio (requires the real Claude Agent SDK).

    Returns:
        Process exit code (0 on clean shutdown, non-zero when the SDK is absent).
    """
    if not HAS_SDK:
        print(
            "claude_agent_sdk is not installed; the sentiment MCP server cannot "
            "run over stdio. Install it with: pip install claude-agent-sdk",
        )
        return 1

    import anyio  # type: ignore[import-not-found]
    from claude_agent_sdk import run_mcp_server_stdio  # type: ignore[import-not-found]

    anyio.run(run_mcp_server_stdio, server)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
