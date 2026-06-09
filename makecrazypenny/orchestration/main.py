"""Layer 2: CLI entrypoint for the mother orchestrator (CONTRACT.md §10.2).

Usage::

    python -m makecrazypenny.orchestration.main SYMBOL [--depth N]

Runs the mother-orchestrator Claude agent over ``SYMBOL``: it plans the
analysis, delegates to the specialist sub-agents (technical, sentiment,
congress, report-checker), and synthesizes a single cross-checked report. The
report is printed with the not-investment-advice disclaimer appended via
:func:`core.disclaimer.with_disclaimer`.

**Graceful degradation (CONTRACT.md §10.2, §2.2).** If the Claude Agent SDK is
not installed, the CLI prints clear install instructions and exits non-zero —
it never crashes with a traceback. Importing this module is always safe (the SDK
is imported through ``servers._sdk`` shims).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from ..core.disclaimer import with_disclaimer
from ..servers._sdk import SDK_AVAILABLE, ClaudeSDKClient
from .agents import build_options
from .debate import decide as run_decision

# Exit codes.
EXIT_OK = 0
EXIT_SDK_MISSING = 2
EXIT_RUNTIME_ERROR = 3

_INSTALL_HINT = (
    "The Claude Agent SDK is not installed, so the orchestrator cannot run.\n"
    "\n"
    "Install it with either of:\n"
    "    pip install claude-agent-sdk\n"
    "    pip install 'makecrazypenny'   # pulls claude-agent-sdk as a dependency\n"
    "\n"
    "You will also need a Claude API key configured in your environment "
    "(see .env.example)."
)


def _build_prompt(symbol: str, depth: int) -> str:
    """Construct the mother-orchestrator prompt for a symbol.

    Args:
        symbol: The ticker symbol to analyze (already normalized upstream).
        depth: Requested analysis depth (higher = more delegation / detail).

    Returns:
        The orchestrator instruction string.
    """
    return (
        f"Produce a cross-checked analysis report for the stock symbol {symbol}.\n"
        "\n"
        "Plan the work, then delegate to your specialist sub-agents:\n"
        "  - technical-analyst: indicators, signals, support/resistance, timeframes.\n"
        "  - sentiment-analyst: news and social sentiment (use web research).\n"
        "  - congress-tracker: congressional trades and insider transactions "
        "(flag disclosure lag).\n"
        "  - report-checker: analyst ratings, price targets, filings; then call "
        "mcp__synthesis__cross_check to reconcile consensus vs. price/technicals "
        "vs. fundamentals and flag divergences.\n"
        "\n"
        f"Analysis depth: {depth} (deeper = more thorough cross-checking and "
        "delegation).\n"
        "\n"
        "Synthesize a single, clearly structured report. Be specific about data "
        "provenance and recency, and surface any divergences or data gaps. This "
        "report is informational only and is NOT investment advice."
    )


def _extract_text(message: Any) -> str:
    """Best-effort text extraction from an SDK message/response object.

    Mirrors the tolerant shape-handling used by ``servers.orchestration`` so the
    CLI works across plausible SDK message surfaces without importing SDK types.
    """
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        if isinstance(message.get("text"), str):
            return message["text"]
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and isinstance(blk.get("text"), str)
            )
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = []
        for blk in content:
            text = getattr(blk, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else ""


async def run_orchestrator(symbol: str, depth: int) -> str:
    """Drive the mother orchestrator over ``symbol`` and return the report text.

    Builds the orchestrator options via :func:`agents.build_options`, opens a
    :class:`ClaudeSDKClient`, queries it with the orchestration prompt, and
    collects the streamed response text.

    Args:
        symbol: Normalized ticker symbol.
        depth: Requested analysis depth.

    Returns:
        The synthesized report text (without the disclaimer; the caller appends it).

    Raises:
        RuntimeError: If the SDK client cannot be driven (propagated to the CLI,
            which reports it cleanly).
    """
    options = build_options()
    prompt = _build_prompt(symbol, depth)

    collected: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        receive = getattr(client, "receive_response", None)
        if callable(receive):
            async for message in client.receive_response():
                text = _extract_text(message)
                if text:
                    collected.append(text)
        else:
            # Alternate SDK surface: query() may return the result directly.
            direct = await client.query(prompt)
            text = _extract_text(direct)
            if text:
                collected.append(text)
    return "\n".join(collected).strip()


def _bullets(items: list, *, empty: str = "(none)") -> str:
    """Render a list as Markdown bullets, or a placeholder when empty.

    Output is intentionally ASCII-only so it prints on a legacy Windows console
    (cp1252) without an encoding error.
    """
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return f"  {empty}"
    return "\n".join(f"  - {i}" for i in items)


def _format_decision(d: dict[str, Any]) -> str:
    """Render a :class:`TradeDecision` dict as a readable CLI report.

    The disclaimer is appended by the caller via :func:`with_disclaimer`, so it
    is intentionally omitted here.
    """
    action = d.get("action", "?")
    direction = d.get("direction", "?")
    conviction = d.get("conviction", 0.0)
    sym = d.get("symbol", "?")

    lines: list[str] = [
        f"DECISION for {sym}:  {action}  ({direction})",
        f"Conviction: {float(conviction):.0%}   Horizon: {d.get('horizon', '?')}   "
        f"Sizing: {d.get('suggested_sizing', '?')}",
        "",
        d.get("summary", ""),
        "",
        "Why:",
        _bullets(d.get("rationale")),
        "",
        "Bull case (for going long):",
        _bullets(d.get("bull_case")),
        "",
        "Bear case (against / for shorting):",
        _bullets(d.get("bear_case")),
        "",
        "Risks:",
        _bullets(d.get("risks")),
    ]
    if d.get("invalidation"):
        lines += ["", f"Invalidation (what would flip this): {d['invalidation']}"]

    lines += [
        "",
        (
            f"Quant backbone: net {d.get('net_score', 0):+.2f} "
            f"(bull {d.get('bull_score', 0):.2f} / bear {d.get('bear_score', 0):.2f}) "
            f"across {d.get('data_quality', {}).get('n_factors', 0)} factors, "
            f"coverage {d.get('data_quality', {}).get('coverage', 0):.0%}"
        ),
    ]
    factors = d.get("factors") or []
    if factors:
        top = sorted(factors, key=lambda f: -abs(f.get("contribution", 0)))[:6]
        for f in top:
            lines.append(
                f"  [{f.get('side', '?'):>4}] {f.get('detail', f.get('name', '?'))} "
                f"({f.get('contribution', 0):+.2f})"
            )

    transcript = d.get("transcript")
    if isinstance(transcript, dict) and transcript.get("arguments"):
        lines += ["", f"Debate ({transcript.get('rounds', 0)} round(s)):"]
        for a in transcript["arguments"]:
            conv = a.get("conviction")
            conv_s = f" [{float(conv):.0%}]" if isinstance(conv, (int, float)) else ""
            lines.append(f"  {a.get('side', '?').upper()} r{a.get('round', '?')}{conv_s}: {a.get('thesis', '')}")

    lines += ["", f"Method: {d.get('method', '?')}"]
    if d.get("note"):
        lines.append(f"Note: {d['note']}")
    if d.get("method") == "quant":
        lines.append(
            "Tip: this is the deterministic quant baseline. For the full bull-vs-bear "
            "AI debate (run on your own subscription), mount the MCP server "
            "(makecrazypenny-mcp) in Claude Desktop/Code and run its `decide` prompt."
        )

    return "\n".join(lines)


def _normalize_symbol(symbol: str) -> str:
    """Uppercase, strip whitespace, and strip a leading ``$`` from a symbol."""
    cleaned = symbol.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return cleaned.strip().upper()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse CLI arguments for the orchestrator entrypoint."""
    parser = argparse.ArgumentParser(
        prog="makecrazypenny",
        description=(
            "Run MakeCrazyPenny over a stock symbol. In 'decide' mode (default) a "
            "bull and a bear advocate debate and an orchestrator decides BUY / SHORT "
            "/ AVOID. In 'report' mode it prints a cross-checked research report. "
            "Informational only; NOT investment advice."
        ),
    )
    parser.add_argument(
        "symbol",
        metavar="SYMBOL",
        help="Ticker symbol to analyze (e.g. AAPL or $aapl).",
    )
    parser.add_argument(
        "--mode",
        choices=("decide", "report"),
        default="decide",
        help=(
            "decide: deterministic BUY/SHORT/AVOID quant decision (default; no API "
            "key). For the full bull/bear AI debate, mount the MCP server and run "
            "its `decide` prompt. report: SDK cross-checked research report."
        ),
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Report-mode analysis depth: more delegation/cross-checking (default: 1).",
    )
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> int:
    """Console-script entrypoint. Returns a process exit code (never raises).

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``2`` if the SDK is missing; ``3`` on a runtime error.
    """
    args = _parse_args(argv)
    symbol = _normalize_symbol(args.symbol)

    if not symbol:
        print("Error: a non-empty ticker symbol is required.", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    # 'decide' mode is the deterministic quant decision — AI-free, no API key. The
    # full bull/bear debate runs in an MCP host (see `makecrazypenny.mcp_server`).
    # 'report' mode still drives the legacy SDK orchestrator and needs the SDK.
    if args.mode == "report" and not SDK_AVAILABLE:
        print(_INSTALL_HINT, file=sys.stderr)
        return EXIT_SDK_MISSING

    try:
        if args.mode == "decide":
            decision = asyncio.run(run_decision(symbol))
            output = _format_decision(decision.to_dict())
        else:
            output = asyncio.run(run_orchestrator(symbol, args.depth))
            if not output:
                output = f"No report was produced for {symbol}."
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # surface cleanly, no traceback
        print(
            f"Error: the {args.mode} run failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR

    print(with_disclaimer(output))
    return EXIT_OK


def main(argv: list[str] | None = None) -> None:
    """``python -m`` entrypoint: run :func:`cli` and exit with its code."""
    raise SystemExit(cli(argv))


if __name__ == "__main__":
    main()
