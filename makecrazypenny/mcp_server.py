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

from .core.config import Settings
from .core.disclaimer import DISCLAIMER
from .orchestration.debate import (
    decide_from_scores,
    gather_evidence,
    score_evidence,
)
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
    """Return the deterministic quant :class:`TradeDecision` as JSON."""
    sym = normalize_symbol(symbol)
    dossier = await gather_evidence(sym)
    scored = score_evidence(dossier)
    decision = decide_from_scores(sym, scored, method="quant")
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


# Keep a reference so linters see the config import is intentional (settings are
# read lazily by the engine; surfaced here for host operators tuning the server).
_ = (Settings, DISCLAIMER)


def main() -> None:
    """Console-script entrypoint: run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
