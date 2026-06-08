"""Layer 2: sub-agent definitions + mother-orchestrator options (CONTRACT.md §10.1).

This module wires the Claude Agent SDK into MakeCrazyPenny:

* Four specialist :class:`AgentDefinition`s the mother orchestrator may delegate to
  (technical-analyst, sentiment-analyst, congress-tracker, report-checker).
* :func:`build_options` returning a :class:`ClaudeAgentOptions` for the mother
  orchestrator: model ``claude-opus-4-8``, all six capability MCP servers wired in,
  the allowed-tool surface from the contract, and the four agents.

**Import safety (CONTRACT.md §2.2, §10.1).** Every SDK symbol is imported through
``servers._sdk``, which falls back to no-op shims when ``claude_agent_sdk`` is
absent. Importing this module therefore never fails and never touches the network,
even with no SDK installed and no API key present. The agent definitions and the
options descriptor are built at *call time* (``define_agents`` / ``build_options``),
so module import does no SDK work at all.
"""

from __future__ import annotations

from typing import Any

from ..servers import _sdk
from ..servers._sdk import (
    SDK_AVAILABLE,
    AgentDefinition,
    ClaudeAgentOptions,
)
from ..servers.congress import server as congress_server
from ..servers.orchestration import server as orchestration_server
from ..servers.reports import server as reports_server
from ..servers.sentiment import server as sentiment_server
from ..servers.synthesis import server as synthesis_server
from ..servers.technical import server as technical_server

# ---------------------------------------------------------------------------
# Model assignments (CONTRACT.md §10.1). Bare model-id strings, no date suffix.
# ---------------------------------------------------------------------------

MOTHER_MODEL: str = "claude-opus-4-8"
TECHNICAL_MODEL: str = "claude-sonnet-4-6"
SENTIMENT_MODEL: str = "claude-haiku-4-5"
CONGRESS_MODEL: str = "claude-haiku-4-5"
REPORT_CHECKER_MODEL: str = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# MCP server registry (CONTRACT.md §10.1). The dict KEY is the MCP namespace:
# a server registered as ``technical`` exposes its tools as ``mcp__technical__*``.
# ---------------------------------------------------------------------------

MCP_SERVERS: dict[str, Any] = {
    "technical": technical_server,
    "sentiment": sentiment_server,
    "congress": congress_server,
    "reports": reports_server,
    "synthesis": synthesis_server,
    "orchestration": orchestration_server,
}

# ---------------------------------------------------------------------------
# Allowed-tool surface for the mother orchestrator (CONTRACT.md §10.1).
# ---------------------------------------------------------------------------

ALLOWED_TOOLS: list[str] = [
    "WebSearch",
    "WebFetch",
    "Agent",
    "mcp__technical__*",
    "mcp__sentiment__*",
    "mcp__congress__*",
    "mcp__reports__*",
    "mcp__synthesis__cross_check",
    "mcp__orchestration__spawn_analyst",
]

# ---------------------------------------------------------------------------
# Per-agent prompts. Kept terse; the disclaimer policy lives in the report layer.
# ---------------------------------------------------------------------------

_TECHNICAL_PROMPT = (
    "You are the technical analyst. Use the technical MCP tools "
    "(mcp__technical__*) to fetch OHLCV, compute indicators, detect signals, "
    "find support/resistance, and summarize multiple timeframes for the symbol. "
    "Report indicator readings and signals concisely. Do not give buy/sell "
    "recommendations; this is informational only and NOT investment advice."
)

_SENTIMENT_PROMPT = (
    "You are the sentiment analyst. Use the sentiment MCP tools "
    "(mcp__sentiment__*) plus WebSearch and WebFetch to gather recent news and "
    "social sentiment for the symbol and produce a blended sentiment read with "
    "the key drivers. Cite sources. Note recency and data gaps. This is "
    "informational only and NOT investment advice."
)

_CONGRESS_PROMPT = (
    "You are the congressional-trade tracker. Use the congress MCP tools "
    "(mcp__congress__*) to surface congressional trades and insider "
    "transactions for the symbol. ALWAYS flag the disclosure lag (often 30-45 "
    "days) so the reader does not treat disclosures as real-time. This is "
    "informational only and NOT investment advice."
)

_REPORT_CHECKER_PROMPT = (
    "You are the report checker. Use the reports MCP tools (mcp__reports__*) "
    "for analyst ratings, price targets, upgrades/downgrades, and SEC filings. "
    "Use mcp__synthesis__cross_check to reconcile analyst consensus against "
    "price/technicals and fundamentals and to flag divergences. When a sub-task "
    "needs deeper, isolated reasoning you may delegate via "
    "mcp__orchestration__spawn_analyst (it is depth- and budget-guarded). "
    "This is informational only and NOT investment advice."
)


def define_agents() -> dict[str, Any]:
    """Build the four specialist :class:`AgentDefinition`s (CONTRACT.md §10.1).

    Returns:
        A mapping of agent name -> :class:`AgentDefinition` (or shim instance
        when the SDK is absent). Built at call time so module import does no
        SDK work.
    """
    return {
        "technical-analyst": AgentDefinition(
            description="Technical analysis: indicators, signals, S/R, multi-timeframe.",
            prompt=_TECHNICAL_PROMPT,
            tools=["mcp__technical__*"],
            model=TECHNICAL_MODEL,
        ),
        "sentiment-analyst": AgentDefinition(
            description="News and social sentiment, blended with web research.",
            prompt=_SENTIMENT_PROMPT,
            tools=["mcp__sentiment__*", "WebSearch", "WebFetch"],
            model=SENTIMENT_MODEL,
        ),
        "congress-tracker": AgentDefinition(
            description="Congressional trades and insider transactions (note disclosure lag).",
            prompt=_CONGRESS_PROMPT,
            tools=["mcp__congress__*"],
            model=CONGRESS_MODEL,
        ),
        "report-checker": AgentDefinition(
            description="Expert reports, ratings, filings; cross-checks and may delegate.",
            prompt=_REPORT_CHECKER_PROMPT,
            tools=[
                "mcp__reports__*",
                "mcp__synthesis__cross_check",
                "mcp__orchestration__spawn_analyst",
            ],
            model=REPORT_CHECKER_MODEL,
        ),
    }


def build_options() -> Any:
    """Build the mother-orchestrator :class:`ClaudeAgentOptions` (CONTRACT.md §10.1).

    Wires model ``claude-opus-4-8``, all six capability MCP servers, the
    contract's allowed-tool surface, and the four specialist agents.

    When the Claude Agent SDK is not installed this returns the ``_sdk`` shim's
    :class:`ClaudeAgentOptions` (a kwargs-storing descriptor) so the call still
    succeeds for inspection/testing; the SDK is only genuinely required when the
    options are actually handed to a real client (see ``main.py``).

    Returns:
        A :class:`ClaudeAgentOptions` (real or shim) configured for the
        orchestrator.
    """
    return ClaudeAgentOptions(
        model=MOTHER_MODEL,
        mcp_servers=dict(MCP_SERVERS),
        allowed_tools=list(ALLOWED_TOOLS),
        agents=define_agents(),
    )


__all__ = [
    "SDK_AVAILABLE",
    "MOTHER_MODEL",
    "TECHNICAL_MODEL",
    "SENTIMENT_MODEL",
    "CONGRESS_MODEL",
    "REPORT_CHECKER_MODEL",
    "MCP_SERVERS",
    "ALLOWED_TOOLS",
    "define_agents",
    "build_options",
]

# Re-export the SDK availability flag for callers that import from this module.
_ = _sdk  # keep the module reference (used for the SDK-availability indirection)
