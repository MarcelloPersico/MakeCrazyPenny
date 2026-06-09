"""MakeCrazyPenny MCP server — host-driven autonomous trade decisions.

This is the **primary surface** of MakeCrazyPenny: a standalone stdio MCP server
that an MCP host (Claude Desktop, Claude Code, or any MCP client) mounts. The
host's own model — the user's subscription — runs the bull-vs-bear debate and
renders the decision; **no Anthropic API key is needed and nothing is billed per
token** (see CONTRACT.md §10.4, plan.md §8).

It exposes two surfaces:

* **Tools (deterministic, AI-free):** ``decide`` (the quant baseline decision),
  ``gather_evidence`` (the full dossier), the per-domain analysis tools, and
  ``finalize_decision`` (merge the host's debated verdict with the quant backbone
  into the canonical :class:`~makecrazypenny.core.types.TradeDecision`). These call
  the Layer-1 server **logic functions** and never invoke a model.
* **Prompts (run by the host's model):** ``decide`` orchestrates the whole
  bull → bear → rebuttals → judge flow; ``bull_case`` / ``bear_case`` / ``judge``
  are the individual personas for stepwise use. The host plays the agents (using
  its native sub-agents if it has them) and calls the tools for evidence.

Run it::

    makecrazypenny-mcp                       # console script (stdio)
    python -m makecrazypenny.mcp_server      # module form

Then mount it in your host, e.g. Claude Code::

    claude mcp add makecrazypenny -- makecrazypenny-mcp

Importing this module is safe and never hits the network; tools fetch lazily only
when called. Informational only; NOT investment advice.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .analysis.backtest import backtest as run_backtest
from .analysis.crypto_regime import crypto_regime as run_crypto_regime
from .analysis.regime import market_regime
from .core.config import Settings
from .core.disclaimer import DISCLAIMER
from .core.sectors import list_sectors, resolve_sector
from .core.sectors import sector_constituents as _sector_constituents
from .core.symbols import canonical_crypto
from .orchestration.crypto import (
    decide_crypto as engine_decide_crypto,
)
from .orchestration.crypto import (
    enrich_crypto_decision,
    gather_crypto_evidence,
    score_crypto_evidence,
)
from .orchestration.crypto_screen import screen_crypto
from .orchestration.debate import (
    decide as engine_decide,
)
from .orchestration.debate import (
    decide_from_scores,
    enrich_decision,
    gather_evidence,
    score_evidence,
)
from .orchestration.market import scan_sector
from .orchestration.portfolio import build_portfolio, build_sector_portfolio
from .orchestration.screen import screen_market
from .servers._common import json_default, normalize_symbol

mcp = FastMCP(
    "makecrazypenny",
    instructions=(
        "Autonomous stock trade-decision toolkit. To decide whether to BUY (go "
        "long), SHORT, or AVOID a symbol, use the `decide` prompt — it runs a "
        "bull-vs-bear debate and an orchestrator judgment using these tools for "
        "evidence. Tools are deterministic and require no API key. Informational "
        "only; NOT investment advice."
    ),
)


def _dumps(obj: Any) -> str:
    """Encode a result as a compact JSON string (handles core dataclasses)."""
    return json.dumps(obj, default=json_default)


# ---------------------------------------------------------------------------
# Tools — deterministic, AI-free (call the Layer-1 logic functions / engine)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="decide",
    title="Quant decision (BUY/SHORT/AVOID)",
    description=(
        "Deterministic baseline decision for a symbol: gathers evidence across all "
        "capability servers, scores it, and returns a TradeDecision "
        "(action BUY/SHORT/AVOID, conviction, factors, bull/bear cases). No AI, no "
        "API key. Use this for the quant baseline before debating, then call "
        "finalize_decision with the debated verdict."
    ),
)
async def decide_tool(symbol: str) -> str:
    """Return the deterministic quant :class:`TradeDecision` (with sizing + regime) as JSON."""
    decision = await engine_decide(normalize_symbol(symbol))
    return _dumps(decision.to_dict())


@mcp.tool(
    name="gather_evidence",
    title="Gather full evidence dossier",
    description=(
        "Fan out across every capability server (technical signals, sentiment, "
        "congressional + insider trades, analyst reports, cross-check) and return "
        "the combined evidence dossier plus the deterministic quant scoring. This "
        "is the raw material for the debate."
    ),
)
async def gather_evidence_tool(symbol: str) -> str:
    """Return the evidence dossier + quant scoring as JSON."""
    sym = normalize_symbol(symbol)
    dossier = await gather_evidence(sym)
    scored = score_evidence(dossier)
    return _dumps({"symbol": sym, "dossier": dossier, "quant": scored})


@mcp.tool(
    name="technical_analysis",
    title="Technical analysis",
    description="Technical signals, latest indicators, and a multi-timeframe summary for a symbol.",
)
async def technical_analysis_tool(symbol: str) -> str:
    """Return technical signals + indicators + multi-timeframe summary as JSON."""
    from .servers import technical as tech

    sym = normalize_symbol(symbol)
    signals, indicators, mtf = await _safe_gather(
        tech.detect_signals(sym), tech.compute_indicators(sym), tech.multi_timeframe_summary(sym)
    )
    return _dumps({"symbol": sym, "signals": signals, "indicators": indicators, "multi_timeframe": mtf})


@mcp.tool(
    name="sentiment_analysis",
    title="News & social sentiment",
    description="Blended news + social sentiment (score, label, drivers) and recent headlines for a symbol.",
)
async def sentiment_analysis_tool(symbol: str) -> str:
    """Return blended sentiment + recent news as JSON."""
    from .servers import sentiment as sent

    sym = normalize_symbol(symbol)
    agg, news = await _safe_gather(sent.aggregate_sentiment(sym), sent.get_news(sym))
    return _dumps({"symbol": sym, "sentiment": agg, "news": news})


@mcp.tool(
    name="congress_activity",
    title="Congressional & insider trades",
    description="Disclosed congressional trades and corporate insider transactions for a symbol (note: disclosures lag).",
)
async def congress_activity_tool(symbol: str) -> str:
    """Return congressional + insider activity as JSON."""
    from .servers import congress as cong

    sym = normalize_symbol(symbol)
    trades, insider = await _safe_gather(cong.congress_trades(sym), cong.insider_transactions(sym))
    return _dumps({"symbol": sym, "congress": trades, "insider": insider})


@mcp.tool(
    name="analyst_reports",
    title="Analyst ratings, targets & filings",
    description="Analyst rating distribution, price targets, recent upgrades/downgrades, and SEC filings for a symbol.",
)
async def analyst_reports_tool(symbol: str) -> str:
    """Return analyst ratings, price targets, upgrades/downgrades, filings as JSON."""
    from .servers import reports as rep

    sym = normalize_symbol(symbol)
    ratings, targets, upgrades, filings = await _safe_gather(
        rep.analyst_ratings(sym), rep.price_targets(sym), rep.upgrades_downgrades(sym), rep.sec_filings(sym)
    )
    return _dumps(
        {"symbol": sym, "ratings": ratings, "price_targets": targets, "upgrades": upgrades, "filings": filings}
    )


@mcp.tool(
    name="cross_check",
    title="Cross-check (consensus vs price vs fundamentals)",
    description="Reconcile analyst consensus against price/technicals and fundamentals; flag divergences.",
)
async def cross_check_tool(symbol: str) -> str:
    """Return the synthesis cross-check as JSON."""
    from .servers import synthesis as syn

    sym = normalize_symbol(symbol)
    (cc,) = await _safe_gather(syn.cross_check(sym))
    return _dumps({"symbol": sym, "cross_check": cc})


@mcp.tool(
    name="finalize_decision",
    title="Finalize the debated decision",
    description=(
        "After the bull/bear debate, record the judge's verdict here to produce the "
        "canonical TradeDecision. The verdict's action/conviction/rationale override "
        "the quant baseline while the deterministic scores and factors are preserved "
        "for transparency. Always returns the decision with the disclaimer attached."
    ),
)
async def finalize_decision_tool(
    symbol: str,
    action: str,
    conviction: float | None = None,
    horizon: str | None = None,
    summary: str | None = None,
    rationale: list[str] | None = None,
    bull_case: list[str] | None = None,
    bear_case: list[str] | None = None,
    risks: list[str] | None = None,
    invalidation: str | None = None,
    suggested_sizing: str | None = None,
) -> str:
    """Merge the host's debated verdict with the quant backbone; return JSON."""
    sym = normalize_symbol(symbol)
    dossier = await gather_evidence(sym)
    scored = score_evidence(dossier)
    verdict: dict[str, Any] = {"action": action}
    if conviction is not None:
        verdict["conviction"] = conviction
    if horizon:
        verdict["horizon"] = horizon
    if summary:
        verdict["summary"] = summary
    if rationale:
        verdict["rationale"] = rationale
    if bull_case:
        verdict["bull_case"] = bull_case
    if bear_case:
        verdict["bear_case"] = bear_case
    if risks:
        verdict["risks"] = risks
    if invalidation:
        verdict["invalidation"] = invalidation
    if suggested_sizing:
        verdict["suggested_sizing"] = suggested_sizing
    decision = decide_from_scores(sym, scored, verdict=verdict, method="debate")
    decision = await enrich_decision(decision, dossier)
    return _dumps(decision.to_dict())


# ---------------------------------------------------------------------------
# Sector / broad-market / risk tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="market_regime",
    title="Market regime (risk-on/off)",
    description=(
        "Trend + volatility regime on a benchmark (default SPY): risk-on / caution / "
        "risk-off and a 0..1 gross-exposure scalar that scales how much risk to take. "
        "Based on price vs 200-day SMA, 12-1 time-series momentum, and a volatility "
        "overlay. AI-free, no API key."
    ),
)
async def market_regime_tool(benchmark: str = "SPY") -> str:
    """Return the market regime + gross-exposure scalar as JSON."""
    return _dumps(await market_regime(benchmark=benchmark))


@mcp.tool(
    name="backtest",
    title="Backtest the price signals (walk-forward)",
    description=(
        "Walk-forward backtest of the trend+momentum long/flat rule on free daily "
        "history, net of transaction costs: CAGR, Sharpe, max-drawdown, hit-rate, and "
        "exposure vs buy-and-hold, PLUS the Probabilistic & Deflated Sharpe Ratio so a "
        "good Sharpe is discounted for sample length and the number of variants tried. "
        "Only price/factor signals are backtested (others lack free history)."
    ),
)
async def backtest_tool(symbol: str, period: str = "10y", cost_bps: float = 10.0) -> str:
    """Return the walk-forward backtest + overfit metrics for ``symbol`` as JSON."""
    return _dumps(await run_backtest(symbol, period=period, cost_bps=cost_bps))


@mcp.tool(
    name="build_portfolio",
    title="Build a portfolio from symbols",
    description=(
        "Build a regime-scaled, conviction x inverse-volatility portfolio from a list "
        "of symbols: runs the decision engine on each, keeps BUY/SHORT names, weights "
        "and caps them, and scales gross exposure by the market regime. Returns longs/"
        "shorts with weights + net/gross exposure. AI-free."
    ),
)
async def build_portfolio_tool(symbols: list[str], max_positions: int = 10, max_weight: float = 0.25) -> str:
    """Return a constructed portfolio for ``symbols`` as JSON."""
    result = await build_portfolio(list(symbols), max_positions=max_positions, max_weight=max_weight)
    return _dumps(result)


@mcp.tool(
    name="build_sector_portfolio",
    title="Build a portfolio from a sector",
    description="Build a regime-scaled conviction x inverse-vol portfolio from a sector's constituents.",
)
async def build_sector_portfolio_tool(
    sector: str, limit: int = 12, max_positions: int = 10, max_weight: float = 0.25
) -> str:
    """Return a constructed portfolio for a sector as JSON."""
    result = await build_sector_portfolio(
        sector, limit=limit or None, max_positions=max_positions, max_weight=max_weight
    )
    return _dumps(result)


@mcp.tool(
    name="list_sectors",
    title="List market sectors",
    description="List the available market sectors (the 11 GICS sectors) and how many constituents each has.",
)
def list_sectors_tool() -> str:
    """Return the available sectors and their constituent counts as JSON."""
    sectors = {name: len(_sector_constituents(name)) for name in list_sectors()}
    return _dumps({"sectors": sectors, "count": len(sectors)})


@mcp.tool(
    name="sector_constituents",
    title="Sector constituents",
    description="Return the constituent ticker symbols for a sector (accepts aliases like 'tech', 'healthcare').",
)
def sector_constituents_tool(sector: str) -> str:
    """Return the resolved sector name and its constituent symbols as JSON."""
    canonical = resolve_sector(sector)
    return _dumps(
        {
            "query": sector,
            "sector": canonical,
            "constituents": _sector_constituents(sector),
            "available": list_sectors() if canonical is None else None,
        }
    )


@mcp.tool(
    name="scan_sector",
    title="Scan a whole sector (quant ranking)",
    description=(
        "Run the deterministic decision engine across a sector's constituents and "
        "return an aggregated SectorScan: sector stance (overweight/underweight/"
        "neutral), breadth, and ranked BUY/SHORT ideas. AI-free, no API key. Use "
        "this as the quant baseline before debating the sector. `limit` caps how "
        "many names are scanned; `top_n` sets how many long/short ideas to surface."
    ),
)
async def scan_sector_tool(sector: str, limit: int = 12, top_n: int = 5) -> str:
    """Return the aggregated :class:`SectorScan` for ``sector`` as JSON."""
    scan = await scan_sector(sector, limit=limit or None, top_n=top_n)
    return _dumps(scan.to_dict())


@mcp.tool(
    name="screen_market",
    title="Screen the whole S&P 500 -> best longs & shorts",
    description=(
        "Screen an entire universe (the S&P 500 by default) in one call and return "
        "the best long and short trade ideas WITH how to trade each. Uses a two-stage "
        "funnel: a cheap price-factor prefilter (momentum/trend/52w-high) ranks every "
        "constituent, then the full decision engine (evidence + regime + ATR sizing) "
        "runs only on the strongest candidates. Returns top_longs/top_shorts as full "
        "TradeDecisions (action, conviction, stop/target, position size, invalidation), "
        "the regime, and the prefilter shortlist. AI-free, no API key. The S&P 500 list "
        "is fetched live and cached. `shortlist` = candidates deep-dived per side; "
        "`top_n` = ideas surfaced per side. Use this as the quant baseline before "
        "debating the finalists."
    ),
)
async def screen_market_tool(shortlist: int = 15, top_n: int = 3, force_refresh: bool = False) -> str:
    """Return the whole-market :class:`MarketScreen` as JSON."""
    screen = await screen_market(shortlist=shortlist, top_n=top_n, force_refresh=force_refresh)
    return _dumps(screen.to_dict())


# ---------------------------------------------------------------------------
# Crypto tools — perpetual futures, leverage-aware (CONTRACT.md §16)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="crypto_decide",
    title="Leverage-aware crypto decision (BUY/SHORT/AVOID)",
    description=(
        "Deterministic baseline decision for a crypto perpetual: gathers multi-timeframe "
        "price action plus the derivatives metrics that matter for leverage (funding rate, "
        "open interest, long/short ratio, basis) and Fear & Greed, scores them, and returns a "
        "TradeDecision WITH a leverage plan — suggested leverage, liquidation price, stop/target, "
        "margin %, and funding cost — sized so the stop sits inside liquidation. No AI, no API key. "
        "`interval` is the entry timeframe (1m..1d; default 15m); `leverage_cap` caps suggested "
        "leverage. Use this as the quant baseline before debating, then call crypto_finalize_decision."
    ),
)
async def crypto_decide_tool(symbol: str, interval: str = "15m", leverage_cap: float = 20.0) -> str:
    """Return the deterministic leverage-aware crypto :class:`TradeDecision` as JSON."""
    decision = await engine_decide_crypto(
        canonical_crypto(symbol), interval=interval, leverage_cap=leverage_cap
    )
    return _dumps(decision.to_dict())


@mcp.tool(
    name="crypto_evidence",
    title="Gather full crypto evidence dossier",
    description=(
        "Fan out across the crypto capability server (multi-timeframe technicals, derivatives "
        "funding/OI/long-short/basis, Fear & Greed, price factors) and return the combined dossier "
        "plus the deterministic quant scoring. This is the raw material for the crypto debate."
    ),
)
async def crypto_evidence_tool(symbol: str, interval: str = "15m") -> str:
    """Return the crypto evidence dossier + quant scoring as JSON."""
    sym = canonical_crypto(symbol)
    dossier = await gather_crypto_evidence(sym, interval=interval)
    scored = score_crypto_evidence(dossier)
    return _dumps({"symbol": sym, "interval": interval, "dossier": dossier, "quant": scored})


@mcp.tool(
    name="derivatives",
    title="Crypto derivatives (funding, OI, long/short, basis)",
    description=(
        "Funding rate (+ annualized), open interest and its short-window change, the long/short "
        "account ratio, and the perpetual basis (mark vs index) for a crypto symbol. The core "
        "microstructure read for a leveraged setup. `interval` sets the OI/long-short window."
    ),
)
async def derivatives_tool(symbol: str, interval: str = "5m") -> str:
    """Return the crypto derivatives snapshot as JSON."""
    from .servers import crypto as cx

    sym = canonical_crypto(symbol)
    (deriv,) = await _safe_gather(cx.derivatives(sym, interval=interval))
    return _dumps({"symbol": sym, "derivatives": deriv})


@mcp.tool(
    name="funding_rate",
    title="Crypto funding rate",
    description=(
        "Current perpetual funding rate for a symbol, annualized, with mark/index price and the "
        "basis. Positive funding = crowded longs paying shorts (contrarian-bearish at extremes)."
    ),
)
async def funding_rate_tool(symbol: str) -> str:
    """Return the current funding rate as JSON."""
    from .providers import get_registry

    sym = canonical_crypto(symbol)
    try:
        env = await get_registry().fetch("funding_rate", symbol=sym)
        data = env.get("data") if isinstance(env, dict) else env
    except Exception as exc:
        data = {"_error": f"{type(exc).__name__}: {exc}"}
    return _dumps({"symbol": sym, "funding": data})


@mcp.tool(
    name="crypto_technicals",
    title="Crypto technical analysis (multi-timeframe)",
    description=(
        "Technical signals + latest indicators at the entry interval plus a 5m/15m/1h "
        "multi-timeframe trend snapshot for a crypto symbol."
    ),
)
async def crypto_technicals_tool(symbol: str, interval: str = "15m") -> str:
    """Return crypto signals + indicators + multi-timeframe summary as JSON."""
    from .servers import crypto as cx

    sym = canonical_crypto(symbol)
    signals, indicators, mtf = await _safe_gather(
        cx.crypto_signals(sym, interval=interval),
        cx.crypto_indicators(sym, interval=interval),
        cx.multi_timeframe(sym),
    )
    return _dumps(
        {"symbol": sym, "interval": interval, "signals": signals, "indicators": indicators, "multi_timeframe": mtf}
    )


@mcp.tool(
    name="crypto_regime",
    title="Crypto market regime (risk-on/off)",
    description=(
        "Trend + volatility regime on BTC (the crypto market beta): risk-on / caution / risk-off "
        "and a 0..1 gross-exposure scalar, with a crypto-tuned volatility target and a Fear & Greed "
        "extreme overlay. Scales how much leverage/exposure to take. AI-free, no API key."
    ),
)
async def crypto_regime_tool() -> str:
    """Return the crypto market regime + gross-exposure scalar as JSON."""
    return _dumps(await run_crypto_regime())


@mcp.tool(
    name="crypto_screen",
    title="Screen the crypto perp universe -> best longs & shorts",
    description=(
        "Screen the most-liquid USDT perpetuals in one call and return the best long and short "
        "setups WITH how to trade each. Two-stage funnel: a cheap price-factor prefilter ranks the "
        "universe, then the full crypto engine (derivatives + regime + leverage sizing) runs on the "
        "strongest candidates. Returns top_longs/top_shorts as full leverage-aware TradeDecisions. "
        "AI-free, no API key. `interval` = entry timeframe; `shortlist` = candidates deep-dived per "
        "side; `top_n` = ideas surfaced per side."
    ),
)
async def crypto_screen_tool(interval: str = "15m", shortlist: int = 10, top_n: int = 3) -> str:
    """Return the crypto :class:`MarketScreen` as JSON."""
    screen = await screen_crypto(interval=interval, shortlist=shortlist, top_n=top_n)
    return _dumps(screen.to_dict())


@mcp.tool(
    name="crypto_finalize_decision",
    title="Finalize the debated crypto decision",
    description=(
        "After the crypto bull/bear debate, record the judge's verdict here to produce the canonical "
        "leverage-aware TradeDecision. The verdict's action/conviction/rationale override the quant "
        "baseline while the deterministic scores, factors, regime, and the recomputed leverage plan "
        "(liquidation price, suggested leverage, stop/target) are preserved. Always returns the "
        "decision with the disclaimer."
    ),
)
async def crypto_finalize_decision_tool(
    symbol: str,
    action: str,
    interval: str = "15m",
    leverage_cap: float = 20.0,
    conviction: float | None = None,
    horizon: str | None = None,
    summary: str | None = None,
    rationale: list[str] | None = None,
    bull_case: list[str] | None = None,
    bear_case: list[str] | None = None,
    risks: list[str] | None = None,
    invalidation: str | None = None,
) -> str:
    """Merge the host's debated crypto verdict with the quant backbone; return JSON."""
    sym = canonical_crypto(symbol)
    dossier = await gather_crypto_evidence(sym, interval=interval)
    scored = score_crypto_evidence(dossier)
    verdict: dict[str, Any] = {"action": action}
    if conviction is not None:
        verdict["conviction"] = conviction
    if horizon:
        verdict["horizon"] = horizon
    if summary:
        verdict["summary"] = summary
    if rationale:
        verdict["rationale"] = rationale
    if bull_case:
        verdict["bull_case"] = bull_case
    if bear_case:
        verdict["bear_case"] = bear_case
    if risks:
        verdict["risks"] = risks
    if invalidation:
        verdict["invalidation"] = invalidation
    decision = decide_from_scores(sym, scored, verdict=verdict, method="debate")
    decision = await enrich_crypto_decision(
        decision, dossier, interval=interval, leverage_cap=leverage_cap
    )
    return _dumps(decision.to_dict())


async def _safe_gather(*coros: Any) -> list[Any]:
    """Await coroutines concurrently; a failure becomes an ``{"_error": ...}`` dict."""
    import asyncio

    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[Any] = []
    for res in results:
        if isinstance(res, BaseException):
            out.append({"_error": f"{type(res).__name__}: {res}"})
        else:
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# Prompts — run by the HOST's model (the user's subscription)
# ---------------------------------------------------------------------------


def build_decide_prompt(symbol: str, rounds: int = 2) -> str:
    """Build the master orchestration prompt for the host's model."""
    sym = normalize_symbol(symbol)
    return (
        f"You are running MakeCrazyPenny's autonomous trade-decision debate for {sym}. "
        "Your goal is a single decision: BUY (open a long), SHORT (open a short), or "
        "AVOID (no position).\n\n"
        "Follow these steps:\n"
        f"1. EVIDENCE — Call the `gather_evidence` tool for {sym} (it returns the full "
        "dossier plus a deterministic quant score). Optionally call `technical_analysis`, "
        "`sentiment_analysis`, `congress_activity`, `analyst_reports`, or `cross_check` to "
        "dig into anything contested. You may also use your own web search for fresh news.\n"
        "2. BULL — Build the strongest HONEST case to go long, citing concrete numbers from "
        "the evidence.\n"
        "3. BEAR — Build the strongest HONEST case to short or avoid, citing concrete numbers.\n"
        f"4. REBUTTALS — Run {rounds} round(s) where each side directly answers the other's "
        "best points.\n"
        "5. JUDGE — As the orchestrator, weigh the QUALITY of each side's argument and the "
        "strength of the evidence (do NOT just average). Penalize unsupported claims; account "
        "for data gaps and any cross-check divergence. Lean AVOID when the edge is thin or the "
        "sides are evenly matched.\n"
        "6. FINALIZE — Call the `finalize_decision` tool with your verdict (action, conviction "
        "0..1, horizon, suggested_sizing, summary, rationale, bull_case, bear_case, risks, "
        "invalidation). It returns the canonical decision with the disclaimer.\n\n"
        "If you have a sub-agent / Task capability, spawn a dedicated 'bull-advocate' and a "
        "'bear-advocate' for steps 2-4 so the debate is genuinely adversarial; otherwise "
        "role-play each side rigorously in turn.\n\n"
        "Present the final decision clearly, then the bull case, the bear case, the risks, and "
        "the invalidation condition. This is informational only and is NOT investment advice."
    )


def build_bull_prompt(symbol: str) -> str:
    """Build the bull-advocate persona prompt."""
    sym = normalize_symbol(symbol)
    return (
        f"You are the BULL advocate for {sym}. Build the strongest HONEST case to GO LONG "
        f"(BUY). First call `gather_evidence` for {sym} (and any of `technical_analysis`, "
        "`sentiment_analysis`, `congress_activity`, `analyst_reports`, `cross_check` you need); "
        "you may also search the web for fresh catalysts. Cite specific numbers. State a clear "
        "thesis, 3-6 key points each tied to evidence, and your honest conviction (0..1). Be "
        "persuasive but never fabricate — a judge will check your claims. Informational only; "
        "NOT investment advice."
    )


def build_bear_prompt(symbol: str) -> str:
    """Build the bear-advocate persona prompt."""
    sym = normalize_symbol(symbol)
    return (
        f"You are the BEAR advocate for {sym}. Build the strongest HONEST case AGAINST a long — "
        f"to SHORT or AVOID. First call `gather_evidence` for {sym} (and any of "
        "`technical_analysis`, `sentiment_analysis`, `congress_activity`, `analyst_reports`, "
        "`cross_check` you need); you may also search the web for risks/catalysts. Cite specific "
        "numbers. State a clear thesis, 3-6 key points each tied to evidence, and your honest "
        "conviction (0..1). Be persuasive but never fabricate — a judge will check your claims. "
        "Informational only; NOT investment advice."
    )


def build_judge_prompt(symbol: str) -> str:
    """Build the orchestrator/judge persona prompt."""
    sym = normalize_symbol(symbol)
    return (
        f"You are the ORCHESTRATOR and final judge deciding what to do with {sym}. A bull and a "
        "bear have argued. Weigh the QUALITY of each argument and the strength of the underlying "
        "evidence — do NOT merely average. Penalize claims the evidence does not support; account "
        "for data gaps and the cross-check divergence. Call `gather_evidence` (or the per-domain "
        "tools) to verify any contested claim. Then decide BUY (long), SHORT, or AVOID with an "
        "honest conviction (AVOID when the edge is thin), and call `finalize_decision` with your "
        "verdict to produce the canonical decision. Informational only; NOT investment advice."
    )


def build_decide_sector_prompt(sector: str, top_n: int = 3) -> str:
    """Build the sector-debate orchestration prompt for the host's model."""
    canonical = resolve_sector(sector)
    label = canonical or sector
    return (
        f"You are running MakeCrazyPenny's sector analysis for the {label} sector. "
        "Your goal is a sector playbook: an overall stance plus the best long and "
        "short ideas within it.\n\n"
        "Follow these steps:\n"
        f"1. SCAN — Call the `scan_sector` tool for '{label}' (it analyses every "
        "constituent with the deterministic quant engine and returns a stance, "
        "breadth, and ranked BUY/SHORT ideas). Note the sector stance and breadth.\n"
        f"2. SHORTLIST — Take the top {top_n} long candidates and top {top_n} short "
        "candidates from the scan.\n"
        "3. DEBATE — For each shortlisted name, run a quick bull-vs-bear check: call "
        "`gather_evidence` (or the per-domain tools) for it, argue the strongest "
        "case for and against, and decide whether the quant ranking holds. Drop "
        "names whose case is weak on inspection.\n"
        "4. SYNTHESIZE — Produce the sector playbook: the overall stance "
        "(overweight / underweight / neutral) with its rationale, the surviving "
        "long ideas (each with a one-line thesis and conviction), the surviving "
        "short ideas (same), the key sector-wide risks, and what would change the "
        "stance.\n"
        "5. (Optional) For any single name you want the canonical decision object, "
        "call `finalize_decision` with your verdict.\n\n"
        "If you have a sub-agent / Task capability, fan the per-name debates out to "
        "parallel sub-agents. Present the stance first, then longs, then shorts, "
        "then risks. This is informational only and is NOT investment advice."
    )


def build_decide_market_prompt(top_n: int = 3) -> str:
    """Build the whole-market screen + debate orchestration prompt for the host."""
    return (
        "You are running MakeCrazyPenny's whole-market screen of the S&P 500. "
        f"Your goal is a shortlist: the {top_n} best long ideas and the {top_n} best "
        "short ideas, each with a clear plan for HOW to trade it.\n\n"
        "Follow these steps:\n"
        f"1. SCREEN — Call the `screen_market` tool (top_n={top_n}). It ranks the whole "
        "universe with a cheap price-factor prefilter, then runs the full quant engine "
        "on the strongest candidates and returns `top_longs` and `top_shorts` as full "
        "TradeDecisions (each already carries conviction, regime, position sizing, "
        "stop/target and an invalidation level). Note the market regime it reports.\n"
        f"2. DEBATE — For each of the {top_n} longs and {top_n} shorts, run a quick "
        "bull-vs-bear check: call `gather_evidence` (or the per-domain tools) for the "
        "name, argue the strongest honest case for and against, and decide whether the "
        "quant ranking holds. Drop any idea whose case is weak on inspection; you may "
        "also search the web for fresh catalysts.\n"
        "3. SYNTHESIZE — Present the surviving longs and shorts. For EACH, give: the "
        "ticker and direction, a one-line thesis, conviction, and the concrete plan "
        "from its TradeDecision sizing (entry zone, stop, target, suggested size) plus "
        "the invalidation that would flip the thesis. State the market regime and how "
        "much gross exposure it argues for.\n"
        "4. (Optional) For any single name you want the canonical decision object, call "
        "`finalize_decision` with your debated verdict.\n\n"
        "If you have a sub-agent / Task capability, fan the per-name debates out to "
        "parallel sub-agents. Present the regime first, then the longs, then the shorts. "
        "This is informational only and is NOT investment advice."
    )


def build_decide_crypto_prompt(symbol: str, interval: str = "15m", rounds: int = 2) -> str:
    """Build the crypto bull-vs-bear orchestration prompt for the host's model."""
    sym = canonical_crypto(symbol)
    return (
        f"You are running MakeCrazyPenny's autonomous LEVERAGED crypto trade-decision debate for "
        f"{sym} on the {interval} timeframe. Your goal is a single decision: BUY (open a leveraged "
        "long), SHORT (open a leveraged short), or AVOID (no position).\n\n"
        "This is a very-short-window leveraged perpetual-futures trade, so weigh the derivatives "
        "microstructure heavily and respect liquidation risk.\n\n"
        "Follow these steps:\n"
        f"1. EVIDENCE - Call `crypto_evidence` for {sym} (interval {interval}); it returns the full "
        "dossier (multi-timeframe price action, funding, open interest, long/short ratio, basis, "
        "Fear & Greed) plus a deterministic quant score. Drill in with `derivatives`, "
        "`crypto_technicals`, or `funding_rate` as needed. Note the `crypto_regime`.\n"
        "2. BULL - Build the strongest HONEST case to go long, citing concrete numbers.\n"
        "3. BEAR - Build the strongest HONEST case to short or avoid, citing concrete numbers.\n"
        "   Remember the contrarian reads: persistently positive funding and a crowded long/short "
        "ratio and extreme greed all warn of a long squeeze (and vice-versa).\n"
        f"4. REBUTTALS - Run {rounds} round(s) where each side answers the other's best points.\n"
        "5. JUDGE - Weigh argument QUALITY and evidence strength (do NOT just average). Account for "
        "the regime's gross-exposure scalar and for funding cost over the expected hold. Lean AVOID "
        "when the edge is thin, the timeframes disagree, or funding makes the carry expensive.\n"
        "6. FINALIZE - Call `crypto_finalize_decision` with your verdict (symbol, action, interval, "
        "leverage_cap, conviction 0..1, horizon, summary, rationale, bull_case, bear_case, risks, "
        "invalidation). It returns the canonical decision with the LEVERAGE PLAN: suggested "
        "leverage, liquidation price, stop/target, margin %, and funding cost.\n\n"
        "If you have a sub-agent / Task capability, spawn a 'bull-advocate' and a 'bear-advocate' "
        "for steps 2-4 so the debate is genuinely adversarial.\n\n"
        "Present the final decision, then the LEVERAGE PLAN (entry, suggested leverage, liquidation "
        "price, stop, target, margin %, est. funding cost), then the bull case, the bear case, the "
        "risks, and the invalidation. Stress that liquidation is an estimate and leverage amplifies "
        "losses. This is informational only and is NOT investment advice."
    )


def build_bull_crypto_prompt(symbol: str, interval: str = "15m") -> str:
    """Build the crypto bull-advocate persona prompt."""
    sym = canonical_crypto(symbol)
    return (
        f"You are the BULL advocate for a leveraged LONG in {sym} ({interval}). First call "
        f"`crypto_evidence` for {sym} (and `derivatives`/`crypto_technicals`/`funding_rate` as "
        "needed). Cite specific numbers - momentum and trend alignment across timeframes, rising "
        "open interest confirming the move, negative or neutral funding (cheap to hold longs), and "
        "any oversold/fear extreme to fade. State a clear thesis, 3-6 evidence-tied key points, and "
        "your honest conviction (0..1). Acknowledge liquidation/funding risk. Never fabricate - a "
        "judge will check your claims. Informational only; NOT investment advice."
    )


def build_bear_crypto_prompt(symbol: str, interval: str = "15m") -> str:
    """Build the crypto bear-advocate persona prompt."""
    sym = canonical_crypto(symbol)
    return (
        f"You are the BEAR advocate for {sym} ({interval}) - argue to SHORT or AVOID. First call "
        f"`crypto_evidence` for {sym} (and `derivatives`/`crypto_technicals`/`funding_rate` as "
        "needed). Cite specific numbers - crowded positioning (high long/short ratio), persistently "
        "positive funding (longs paying, squeeze risk), open interest rising into resistance, "
        "extreme greed to fade, and bearish timeframe disagreement. State a clear thesis, 3-6 "
        "evidence-tied key points, and your honest conviction (0..1). Never fabricate - a judge will "
        "check your claims. Informational only; NOT investment advice."
    )


def build_decide_crypto_market_prompt(top_n: int = 3, interval: str = "15m") -> str:
    """Build the crypto-universe screen + debate orchestration prompt for the host."""
    return (
        "You are running MakeCrazyPenny's screen of the most-liquid crypto perpetuals on the "
        f"{interval} timeframe. Your goal is a shortlist: the {top_n} best long ideas and the "
        f"{top_n} best short ideas, each with a leveraged plan.\n\n"
        "Follow these steps:\n"
        f"1. SCREEN - Call `crypto_screen` (interval={interval}, top_n={top_n}). It prefilters the "
        "universe, runs the full quant engine on the strongest candidates, and returns `top_longs` "
        "and `top_shorts` as full leverage-aware TradeDecisions (each already carries conviction, "
        "regime, suggested leverage, liquidation price, stop/target). Note the crypto regime.\n"
        f"2. DEBATE - For each of the {top_n} longs and {top_n} shorts, run a quick bull-vs-bear "
        "check: call `crypto_evidence` (or `derivatives`) for the name, argue the strongest honest "
        "case for and against, and decide whether the quant ranking holds. Drop weak ideas; respect "
        "funding cost and liquidation risk.\n"
        "3. SYNTHESIZE - Present the surviving longs and shorts. For EACH give: symbol and "
        "direction, a one-line thesis, conviction, and the concrete leverage plan (entry, suggested "
        "leverage, liquidation price, stop, target, margin %) plus the invalidation. State the "
        "regime and how much gross exposure it argues for.\n"
        "4. (Optional) For any single name, call `crypto_finalize_decision` with your verdict.\n\n"
        "If you have a sub-agent / Task capability, fan the per-name debates out to parallel "
        "sub-agents. Present the regime first, then longs, then shorts. Stress that leverage "
        "amplifies losses and liquidation prices are estimates. Informational only; NOT advice."
    )


@mcp.prompt(
    name="decide",
    title="Decide: bull vs bear debate -> BUY/SHORT/AVOID",
    description="Run the full autonomous decision debate for a symbol using the host's model.",
)
def decide_prompt(symbol: str, rounds: str = "2") -> str:
    """MCP prompt: the master debate orchestration for ``symbol``."""
    try:
        n = max(1, int(rounds))
    except (TypeError, ValueError):
        n = 2
    return build_decide_prompt(symbol, n)


@mcp.prompt(name="bull_case", title="Bull advocate", description="Argue the strongest case to go long.")
def bull_prompt(symbol: str) -> str:
    """MCP prompt: the bull-advocate persona for ``symbol``."""
    return build_bull_prompt(symbol)


@mcp.prompt(name="bear_case", title="Bear advocate", description="Argue the strongest case to short or avoid.")
def bear_prompt(symbol: str) -> str:
    """MCP prompt: the bear-advocate persona for ``symbol``."""
    return build_bear_prompt(symbol)


@mcp.prompt(name="judge", title="Orchestrator judge", description="Weigh both sides and decide.")
def judge_prompt(symbol: str) -> str:
    """MCP prompt: the orchestrator/judge persona for ``symbol``."""
    return build_judge_prompt(symbol)


@mcp.prompt(
    name="decide_sector",
    title="Decide a whole sector -> stance + long/short ideas",
    description="Scan a sector and debate the best long/short ideas using the host's model.",
)
def decide_sector_prompt(sector: str, top_n: str = "3") -> str:
    """MCP prompt: orchestrate a sector scan + per-name debate for ``sector``."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 3
    return build_decide_sector_prompt(sector, n)


@mcp.prompt(
    name="decide_market",
    title="Screen the whole S&P 500 -> best longs & shorts",
    description="Screen the whole S&P 500 and debate the best long/short ideas using the host's model.",
)
def decide_market_prompt(top_n: str = "3") -> str:
    """MCP prompt: orchestrate a whole-market screen + per-name debate."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 3
    return build_decide_market_prompt(n)


@mcp.prompt(
    name="decide_crypto",
    title="Decide a crypto perp (leveraged) -> BUY/SHORT/AVOID",
    description="Run the full leverage-aware bull/bear crypto debate for a symbol using the host's model.",
)
def decide_crypto_prompt(symbol: str, interval: str = "15m", rounds: str = "2") -> str:
    """MCP prompt: the master leveraged-crypto debate orchestration for ``symbol``."""
    try:
        n = max(1, int(rounds))
    except (TypeError, ValueError):
        n = 2
    return build_decide_crypto_prompt(symbol, interval or "15m", n)


@mcp.prompt(
    name="bull_case_crypto",
    title="Crypto bull advocate",
    description="Argue the strongest case for a leveraged long.",
)
def bull_crypto_prompt(symbol: str, interval: str = "15m") -> str:
    """MCP prompt: the crypto bull-advocate persona for ``symbol``."""
    return build_bull_crypto_prompt(symbol, interval or "15m")


@mcp.prompt(
    name="bear_case_crypto",
    title="Crypto bear advocate",
    description="Argue the strongest case to short or avoid a leveraged crypto position.",
)
def bear_crypto_prompt(symbol: str, interval: str = "15m") -> str:
    """MCP prompt: the crypto bear-advocate persona for ``symbol``."""
    return build_bear_crypto_prompt(symbol, interval or "15m")


@mcp.prompt(
    name="decide_crypto_market",
    title="Screen crypto perps -> best leveraged longs & shorts",
    description="Screen the crypto perp universe and debate the best long/short setups using the host's model.",
)
def decide_crypto_market_prompt(top_n: str = "3", interval: str = "15m") -> str:
    """MCP prompt: orchestrate a crypto-universe screen + per-name debate."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 3
    return build_decide_crypto_market_prompt(n, interval or "15m")


# Keep a reference so linters see the config import is intentional (settings are
# read lazily by the engine; surfaced here for host operators tuning the server).
_ = (Settings, DISCLAIMER)


def main() -> None:
    """Console-script entrypoint: run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
