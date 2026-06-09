# MakeCrazyPenny

> **Not investment advice.** Every report this system produces is informational and
> educational only. It is NOT investment advice, a recommendation, or a solicitation
> to buy or sell any security. Data may be delayed, incomplete, or inaccurate, and
> congressional/insider disclosures are subject to reporting lag. Always do your own
> research and consult a licensed financial professional before making any investment
> decision. The disclaimer is baked into `makecrazypenny/core/disclaimer.py` and is
> appended to every user-facing report.

## What it is

MakeCrazyPenny is an agentic financial-analysis platform that makes an **autonomous
trade decision** — **BUY (go long)**, **SHORT**, or **AVOID** — for a stock symbol,
through an adversarial **bull-vs-bear debate** that an orchestrator/judge adjudicates.

It ships as a **stdio MCP server**: you mount it in an MCP host (Claude Desktop or
Claude Code) and the **host's own model — your subscription — runs the debate**. No
Anthropic API key, nothing billed per token. The server provides the host with
deterministic, AI-free **tools** (evidence from *all* the APIs + a quant baseline +
`finalize_decision`) and the bull/bear/judge **prompts** to run. The `decide` prompt
orchestrates the whole flow: gather evidence → bull argues long → bear argues
short/avoid → rebuttals → judge weighs argument quality and decides, with a
conviction, sizing, rationale, risks, and an invalidation condition.

Under the debate sits a **pure, deterministic quant backbone**, so the `decide` tool
(and the CLI) always produce a real decision with *no model call at all*. A classic
**cross-checked research report** mode also remains. See [`plan.md`](./plan.md) §8 for
the design and [`CONTRACT.md`](./CONTRACT.md) §10.3–§10.4 for the build spec.

It also analyses a **whole sector** at once: `scan_sector` runs the engine across a
sector's constituents and returns a stance (overweight / underweight / neutral),
breadth, and ranked long/short ideas; the `decide_sector` prompt then debates the top
candidates into a sector playbook. See [`plan.md`](./plan.md) §9.

And it screens the **entire S&P 500 in one call**: `screen_market` runs a two-stage
**funnel** — a cheap price-factor prefilter (momentum / trend / 52-week-high) ranks
every constituent, then the full decision engine (evidence + regime + ATR sizing)
runs only on the strongest candidates — and returns the best **long and short ideas
with how to trade each** (action, conviction, stop/target, position size,
invalidation). The constituent list is fetched live and cached. The `decide_market`
prompt then debates the finalists. (Informational only; not investment advice.)

The decision engine is enriched with **research-backed, free-data edges** (see
[`plan.md`](./plan.md) §10): factor signals (12-1 **momentum**, 52-week-high, **trend**,
**value/quality**) folded into the score; a **market-regime** filter (SPY trend + vol)
that scales exposure; **risk sizing** (ATR stops + volatility-target / half-Kelly
position size) on every decision; a **portfolio builder** (conviction × inverse-vol,
regime-scaled); and a **walk-forward backtest** with transaction costs and the
**Deflated Sharpe Ratio** to guard against overfitting.

> **Why an MCP server instead of the API?** So the AI runs on your existing Claude
> subscription via the host, not a metered API key. MCP "sampling" would be the ideal
> mechanism but no Claude host implements it yet, so the debate is exposed as MCP
> **prompts** the host's model executes (with the server's tools for evidence).

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

**Decision layer** (`mcp_server.py` + `orchestration/debate.py`). The autonomous
buy/short/avoid decision is produced by an adversarial debate **run by the host's
model** through the MCP server:

| MCP prompt | Role (host's model plays it) |
|------------|------------------------------|
| `decide` | Orchestrates the whole flow: evidence → bull → bear → rebuttals → judge → `finalize_decision`. |
| `bull_case` | Argues the strongest case to go long (BUY). |
| `bear_case` | Argues the strongest case to short or avoid. |
| `judge` | Weighs argument quality + evidence, then decides. |

| MCP tool (deterministic, AI-free) | Returns |
|-----------------------------------|---------|
| `decide` | Quant baseline `TradeDecision` (BUY/SHORT/AVOID + conviction + factors). |
| `gather_evidence` | Full evidence dossier + quant score. |
| `technical_analysis` / `sentiment_analysis` / `congress_activity` / `analyst_reports` / `cross_check` | Per-domain evidence. |
| `finalize_decision` | Merges the host's debated verdict with the quant backbone into the canonical decision. |

The engine `orchestration/debate.py` (`gather_evidence → score_evidence →
decide_from_scores`) is pure and AI-free — it's the deterministic baseline the host's
judgment refines. See [`plan.md`](./plan.md) §8 and [`CONTRACT.md`](./CONTRACT.md) §10.4.

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

The decision **tools and prompts need no API key** — the host's model does the
reasoning. The legacy `report` mode (and `spawn_analyst`) additionally require the
Claude Agent SDK and a Claude API key, per the SDK's conventions.

## Run as an MCP server (recommended)

This is the primary way to use MakeCrazyPenny: mount it in an MCP host and run the
debate on **your own subscription**.

```bash
# Mount in Claude Code (after `pip install -e .`)
claude mcp add makecrazypenny -- makecrazypenny-mcp

# …or run the stdio server directly
makecrazypenny-mcp
python -m makecrazypenny.mcp_server
```

> **The `makecrazypenny-mcp` command must be resolvable by the MCP host.** It is
> installed into your virtualenv's `Scripts/` (Windows) or `bin/` (macOS/Linux), so
> it is only on `PATH` while that venv is **activated**. Since the host usually
> launches the server in its own environment, the most reliable form is the **full
> path** to the console script:
>
> ```bash
> # Windows (PowerShell) — full path to the venv script
> claude mcp add makecrazypenny -- "C:\path\to\repo\.venv\Scripts\makecrazypenny-mcp.exe"
>
> # macOS / Linux
> claude mcp add makecrazypenny -- "/path/to/repo/.venv/bin/makecrazypenny-mcp"
> ```
>
> Also note `claude` itself must be on `PATH`. With the Claude **desktop app** on
> Windows the CLI is bundled inside the app package and is *not* on `PATH` by
> default — invoke it by full path or add a launcher to `PATH`. Verify the server is
> wired up with `claude mcp list` (look for `✓ Connected`).

For Claude Desktop, add to its MCP config (use the full path to the script, as above,
if the bare command is not on `PATH`):

```json
{
  "mcpServers": {
    "makecrazypenny": { "command": "makecrazypenny-mcp" }
  }
}
```

Then, in the host, run the **`decide`** prompt for a symbol (e.g. `AAPL`). The host's
model gathers evidence via the tools, argues bull vs bear, judges, and calls
`finalize_decision` to produce the canonical verdict. You can also run the
`bull_case` / `bear_case` / `judge` prompts individually, or call any tool directly
(e.g. `decide AAPL` for the instant quant baseline).

For a **whole sector**, run the **`decide_sector`** prompt (e.g. `tech`, `healthcare`,
`energy`): the host calls `scan_sector` for the quant ranking, debates the top
long/short candidates, and synthesizes a sector playbook. Or call the tools directly —
`list_sectors`, `sector_constituents`, `scan_sector`.

For the **whole S&P 500**, run the **`decide_market`** prompt: the host calls
`screen_market` (the prefilter → deep-dive funnel), then debates the best long and
short finalists and lays out the plan for each. Or call `screen_market` directly for
the quant baseline (`shortlist` = candidates deep-dived per side, `top_n` = ideas per
side).

## Run the CLI (deterministic quant decision)

The CLI gives the AI-free quant decision instantly — handy for scripting/CI:

```bash
# Autonomous quant decision (default mode): BUY / SHORT / AVOID + conviction
python -m makecrazypenny.orchestration.main AAPL
makecrazypenny NVDA              # console script (installed by `pip install -e .`)

# Scan a whole sector: stance + ranked long/short ideas
makecrazypenny --sector tech --limit 12 --top 5
makecrazypenny --sector healthcare

# Market regime (risk-on/off + gross-exposure scalar) and a walk-forward backtest
makecrazypenny --regime
makecrazypenny --backtest AAPL

# Classic cross-checked research report (needs the Claude Agent SDK)
python -m makecrazypenny.orchestration.main AAPL --mode report --depth 2
```

```
usage: makecrazypenny [-h] [--sector NAME] [--limit N] [--top N] [--market] [--regime] [--backtest]
                      [--crypto SYMBOL] [--crypto-market] [--crypto-regime] [--interval TF] [--leverage N]
                      [--mode {decide,report}] [--depth DEPTH] [SYMBOL]
```

The `decide` output now also shows the **sized trade** (stop / target / position %) and
the **market regime**. New MCP tools: `market_regime`, `backtest`, `build_portfolio`,
`build_sector_portfolio`.

`decide` mode (default) normalizes the symbol (`$aapl` → `AAPL`) and prints the
verdict, conviction, bull/bear cases, risks, the quant factor breakdown, and a tip to
run the full debate via the MCP server — always with the not-investment-advice
disclaimer. It needs **no SDK or API key**. `report` mode still drives the SDK
orchestrator and exits non-zero (no traceback) if the SDK is absent.

## Crypto — very-short-window leveraged perpetuals

A parallel **crypto track** for leveraged perpetual-futures trading, built on the same
engine but tuned for short timeframes and the derivatives metrics that matter under
leverage. All data is **keyless**: Binance + Bybit (perp klines, funding rate, open
interest, long/short ratio), CoinGecko (market cap / BTC dominance), and the
Alternative.me Fear & Greed Index. On a US IP, Binance's geo-block trips the registry's
fallback chain straight to Bybit automatically.

```bash
# Leverage-aware decision: action + suggested leverage, liquidation price, stop/target
makecrazypenny --crypto BTCUSDT --interval 15m --leverage 20
makecrazypenny --crypto ETH                 # BTC, ETH/USDT, BTC-USD all normalize

# Screen the most-liquid perps -> best leveraged long/short setups
makecrazypenny --crypto-market --interval 15m --top 3

# Crypto market regime (BTC trend + vol + Fear & Greed)
makecrazypenny --crypto-regime
```

Beyond the BUY/SHORT/AVOID verdict, every crypto decision carries a **leverage plan**:
suggested leverage (capped so the ATR stop sits *inside* the estimated liquidation
price), liquidation price, stop/target, margin %, and the estimated funding cost over
the hold — all scaled by the crypto regime. The derivatives signals are scored as
**contrarian at extremes** (crowded longs / high funding / extreme greed warn of a
squeeze). Tunable via `MCP_CRYPTO_*` env vars (default preset: ≤20x, ~2.5% risk/trade).

New MCP **tools** — `crypto_decide`, `crypto_evidence`, `derivatives`, `funding_rate`,
`crypto_technicals`, `crypto_regime`, `crypto_screen`, `crypto_finalize_decision` — and
**prompts** `decide_crypto`, `bull_case_crypto` / `bear_case_crypto`, `decide_crypto_market`
(the host runs the leverage-aware bull/bear debate on your own subscription).

> Leverage amplifies losses and liquidation prices are estimates. Informational only;
> NOT investment advice.

## Run the dashboard (Streamlit GUI)

A single-page web dashboard renders everything for one ticker — the `cross_check`
verdict, the price chart + indicators + signals, blended sentiment + headlines,
congressional/insider trades, and analyst ratings/targets/filings — in five tabs.
It is a thin Layer-2 presentation surface: it calls the existing server **logic
functions** and renders their results, adding no business logic.

```bash
# install the optional UI extra (adds streamlit)
pip install -e ".[ui]"        # or ".[dev]" which already includes it

# launch (console script)
makecrazypenny-dashboard       # add streamlit flags after, e.g. --server.port 8502

# or directly
streamlit run makecrazypenny/ui/dashboard.py
```

Then open the printed URL (default http://localhost:8501), enter a ticker in the
sidebar, and click **Analyze**. No data is fetched until you do; results are cached
for ~5 minutes. The dashboard needs **no** Claude Agent SDK (it talks to Layer 1/0
directly) and no API keys for the free providers (yfinance / EDGAR / StockWatcher);
add keys to `.env` to populate the keyed panels. `makecrazypenny/ui/dashboard.py`
stays import-safe without Streamlit installed, so it never affects the test suite.

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
