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
            "Run the MakeCrazyPenny mother orchestrator over a stock symbol and "
            "print a cross-checked report. Informational only; NOT investment advice."
        ),
    )
    parser.add_argument(
        "symbol",
        metavar="SYMBOL",
        help="Ticker symbol to analyze (e.g. AAPL or $aapl).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Analysis depth: deeper means more delegation and cross-checking (default: 1).",
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

    if not SDK_AVAILABLE:
        print(_INSTALL_HINT, file=sys.stderr)
        return EXIT_SDK_MISSING

    try:
        report = asyncio.run(run_orchestrator(symbol, args.depth))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # surface cleanly, no traceback
        print(
            f"Error: the orchestrator failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR

    if not report:
        report = f"No report was produced for {symbol}."

    print(with_disclaimer(report))
    return EXIT_OK


def main(argv: list[str] | None = None) -> None:
    """``python -m`` entrypoint: run :func:`cli` and exit with its code."""
    raise SystemExit(cli(argv))


if __name__ == "__main__":
    main()
