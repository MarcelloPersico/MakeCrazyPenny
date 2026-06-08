# MakeCrazyPenny

> **Not investment advice.** Every report this system produces is informational and
> educational only. It is NOT investment advice, a recommendation, or a solicitation
> to buy or sell any security. Data may be delayed, incomplete, or inaccurate, and
> congressional/insider disclosures are subject to reporting lag. Always do your own
> research and consult a licensed financial professional before making any investment
> decision. The disclaimer is baked into `makecrazypenny/core/disclaimer.py` and is
> appended to every user-facing report.

## What it is

MakeCrazyPenny is an agentic financial-analysis platform. A **mother orchestrator**
agent (Claude, via the Claude Agent SDK) plans an analysis of a stock symbol,
delegates to specialized sub-agents (technical, sentiment, congressional-trade,
expert-report), and synthesizes a single **cross-checked** report. Every capability
is exposed over the Model Context Protocol (MCP), so any MCP-capable host can drive
it. See [`plan.md`](./plan.md) for the original design and
[`CONTRACT.md`](./CONTRACT.md) for the authoritative build specification.

## Architecture

The system is built in **three layers** with a strict, acyclic dependency rule
(**Layer 2 → Layer 1 → Layer 0**, never upward; no sideways calls within Layer 1
except `synthesis`). This mirrors the layering described in
[`plan.md`](./plan.md) and formalized in [`CONTRACT.md`](./CONTRACT.md) §1.

| Layer | Package | Role |
|-------|---------|------|
| **0** | `makecrazypenny/providers/` | Shared data-access. One client per external API behind a single `ProviderRegistry` that owns rate limiting, caching, retries, circuit breaking, and per-capability fallback chains. Knows nothing about agents or MCP. |
| **1** | `makecrazypenny/servers/` | Agent-agnostic capability MCP servers: `technical`, `sentiment`, `congress`, `reports`, `synthesis`, `orchestration`. Pure async logic + MCP wiring. Depend only on Layer 0 + core. |
| **2** | `makecrazypenny/orchestration/` | Claude Agent SDK reasoning. Specialist sub-agent definitions + mother-orchestrator options + the CLI entrypoint. |
| — | `makecrazypenny/core/` | Cross-cutting primitives: types, config, disclaimer, errors. Imported by all layers. |

The Layer-2 orchestrator (`orchestration/agents.py`) defines four specialists:

| Agent | Model | Tools |
|-------|-------|-------|
| `technical-analyst` | `claude-sonnet-4-6` | `mcp__technical__*` |
| `sentiment-analyst` | `claude-haiku-4-5` | `mcp__sentiment__*`, `WebSearch`, `WebFetch` |
| `congress-tracker` | `claude-haiku-4-5` | `mcp__congress__*` |
| `report-checker` | `claude-sonnet-4-6` | `mcp__reports__*`, `mcp__synthesis__cross_check`, `mcp__orchestration__spawn_analyst` |

The mother orchestrator runs on `claude-opus-4-8` with all six MCP servers wired in.

**Import safety.** Providers and servers import cleanly *without* the optional heavy
libraries (`yfinance`, `pandas`, `ta`) or the Claude Agent SDK present, and without
any API key — heavy libs are lazy-imported inside function bodies, and SDK symbols
go through graceful shims (`servers/_sdk.py`). Importing a module never hits the
network and never raises on a missing key.

## Install

Requires **Python 3.11+**.

```bash
# from the repository root
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate

pip install -e ".[dev]"
```

This installs the runtime dependencies (`claude-agent-sdk`, `yfinance`, `pandas`,
`ta`, `httpx`, `python-dotenv`) plus the dev extras (`pytest`, `pytest-asyncio`,
`respx`, `ruff`).

## Configure

Copy the example environment file and fill in whatever keys you have:

```bash
cp .env.example .env   # Windows: copy .env.example .env
```

Then edit `.env`. **Every API key is optional** — providers whose key is missing
raise `MissingApiKey` at fetch time and the registry falls through to the next
provider in that capability's chain.

| Variable | Used for |
|----------|----------|
| `ALPHA_VANTAGE_API_KEY` | OHLCV, quotes, news sentiment, fundamentals |
| `FINNHUB_API_KEY` | OHLCV, quotes, news, sentiment, congress, insider, ratings, targets, upgrades |
| `FMP_API_KEY` | Congress trades, ratings, price targets, upgrades/downgrades, fundamentals |
| `MARKETAUX_API_KEY` | Company news |
| `MCP_CACHE_DIR` | On-disk L2 cache + persisted alert state (defaults to a temp dir) |

Running the **orchestrator CLI** additionally requires the Claude Agent SDK to be
installed (it is a declared dependency) and a Claude API key configured in your
environment, per the SDK's own conventions.

## Run the CLI

```bash
# Module form
python -m makecrazypenny.orchestration.main AAPL

# Console script (installed by `pip install -e .`)
makecrazypenny AAPL --depth 2
```

```
usage: makecrazypenny [-h] [--depth DEPTH] SYMBOL
```

The CLI normalizes the symbol (`$aapl` → `AAPL`), runs the mother orchestrator over
it, and prints the cross-checked report with the not-investment-advice disclaimer
appended.

If the Claude Agent SDK is **not** installed, the CLI prints clear install
instructions and exits with a non-zero status — it does not crash with a traceback.

## Run tests

Tests are deterministic and offline (no network); `respx`/monkeypatching stub all
external I/O, and the SDK shims keep orchestration importable without the SDK.

```bash
pytest
```

Lint with ruff (line length 100):

```bash
ruff check .
```

## Disclaimer

MakeCrazyPenny is provided for **informational and educational purposes only**. It is
**NOT investment advice**, a recommendation, or a solicitation to buy or sell any
security. Market data may be delayed, incomplete, or inaccurate; congressional and
insider disclosures carry reporting lag. Always do your own research and consult a
licensed financial professional before making any investment decision.
