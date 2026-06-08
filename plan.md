\# MakeCrazyPenny — Design \& Architecture



&#x20;Agentic financial-analysis platform. A mother orchestrator agent spawns specialized

&#x20;sub-agents (technical analysis, deep-search sentiment, congressional-trade alerts, and

&#x20;expert-report cross-checks). Every capability is exposed over MCP so any MCP-capable

&#x20;agent (Claude or otherwise) can drive it. Built in Python on the Claude Agent SDK.



Status design only (no application code yet) — for review before implementation.

Not investment advice. Every report this system produces is informational; a disclaimer is baked into the output.



\---



\## 1. Goals \& requirements



From the brief



1\. Technical analysis — indicators, signals, multi-timeframe reads.

2\. Deep-search sentiment analysis — news + social + LLM web research.

3\. Congress trading alerts — disclosed HouseSenate trades + insider (Form 4) activity.

4\. Broad-scale expert-report alerts \& cross-checks — analyst ratings  price targets 

&#x20;  upgrades-downgrades, reconciled against price action and fundamentals to flag divergences.

5\. Free APIs wherever possible; deep search where it adds value.

6\. Mother agent orchestrates a specialized sub-agent per analysis type, and sub-agents can

&#x20;  spin up their own sub-agents when a task needs to fan out further.



\### Decisions locked in this session

\- Languageruntime Python (best fit for finance + pandas + the SDK).

\- API posture free-tier API keys are acceptable (kept in `.env`, never committed).

\- Deep-search backend Claude's built-in `WebSearch`  `WebFetch` server tools.

\- This session designplan only.



\---



\## 2. The core architectural constraint (and how we solve it)



The brief requires recursive delegation — sub-agents that can spawn their own sub-agents.



The Claude Agent SDK's native subagents (the `Agent`Task tool) are depth-1 only a

subagent launched via the Task tool is not given the Task tool itself, so it cannot spawn

further subagents. This is a known, intentional limitation

(\[claude-code issue #4182](httpsgithub.comanthropicsclaude-codeissues4182),

\[Subagents in the SDK](httpscode.claude.comdocsenagent-sdksubagents)).



Solution — a recursive `spawn\_analyst` MCP tool. We expose an in-process MCP tool that, when

called, constructs a fresh nested `ClaudeSDKClient` with a role-specific prompt + toolset and

returns its result. Because it's just another tool, any agent (mother or sub) can call it,

giving true (bounded) recursion with full observability — unlike the `claude -p`-via-Bash hack.

Hard `max\_depth` and `max\_budget\_usd` guards prevent runaway recursioncost.



So we use a hybrid delegation model

\- Native `AgentDefinition` subagents for the common shallow fan-out (fast, simple).

\- `spawn\_analyst` recursion for the deep case (a report-checker spawning per-source

&#x20; verifiers; a sentiment agent spawning per-source readers; etc.).



\---



\## 3. Two-layer architecture



```

┌──────────────────────────────────────────────────────────────────────────┐

│ LAYER 2 — Orchestration (Claude Agent SDK)                                 │

│                                                                            │

│   Mother  orchestrator  (claude-opus-4-8, high effort)                    │

│   plans → delegates → synthesizes a cross-checked report                   │

│        │                                                                   │

│        ├── technical-analyst   (sonnet-4-6)                                │

│        ├── sentiment-analyst    (haiku-4-5 fan-out + WebSearchWebFetch)   │

│        ├── congress-tracker     (haiku-4-5)                                │

│        └── report-checker       (opussonnet) ──┐ may recurse via          │

│                                                  │ spawn\_analyst           │

│              (any agent can call spawn\_analyst → nested ClaudeSDKClient)   │

└───────────────────────────────┬────────────────────────────────────────--┘

&#x20;                                │ MCP (in-process SDK servers + stdio)

┌───────────────────────────────┴────────────────────────────────────────--┐

│ LAYER 1 — Capability MCP servers (agent-agnostic)                          │

│                                                                            │

│   technical · sentiment · congress · reports · orchestration(spawn+alerts) │

│        │            │          │          │                                │

│   providers  (thin adapters over free APIs, cached + rate-limited)        │

│   yahoo · alpha\_vantage · finnhub · fmp · edgar · stockwatcher             │

└──────────────────────────────────────────────────────────────────────────┘

```



Why two layers. Layer 1 is pure capability (data + computation) exposed as MCP tools — it has

no idea who's calling it, so Claude, Cursor, or any MCP host can mount it. Layer 2 is the

reasoningorchestration built specifically on the Claude Agent SDK. Core logic lives in plain

Python functions; each is exposed both as an in-process SDK MCP server (fast, shares state,

used by the orchestrator) and as a standalone stdio MCP server (portable, for any other client).



\---



\## 4. Layer 1 — Capability MCP servers



Each tool wraps a provider adapter, returns compact structured JSON (text content blocks), and is

namespaced `mcp\_\_server\_\_tool`.



\### 4.1 `technical` — market data \& technical analysis

\- Data `yfinance` (primary, no key) for OHLCV; Alpha Vantage  Twelve Data  Finnhub as keyed fallbacks.

\- Indicators `pandas-ta` (or the pure-Python `ta` package) — avoids the TA-Lib C dependency.

\- Tools `get\_ohlcv(symbol, interval, period)` · `compute\_indicators(symbol, indicators=\[rsi,macd,bbands,sma,ema,atr,stoch,adx,obv])` · `detect\_signals(symbol)` (goldendeath cross, RSI extremes, MACD cross, BB breaks) · `support\_resistance(symbol)` · `multi\_timeframe\_summary(symbol)`.



\### 4.2 `sentiment` — news + social + deep search

\- Data Finnhub `company-news` + `news-sentiment`; Alpha Vantage `NEWS\_SENTIMENT` (returns pre-computed scores); Marketaux (free); StockTwitsReddit optional.

\- Deep search done by the agent via Claude `WebSearch``WebFetch` — the MCP server supplies the quantified scores; the agent reasons over both structured scores and fresh web context.

\- Tools `get\_news(symbol, days)` · `news\_sentiment(symbol)` (aggregated) · `social\_sentiment(symbol)` · `aggregate\_sentiment(symbol)` (blended score + drivers).



\### 4.3 `congress` — congressional \& insider trades

\- Data Finnhub `stockcongressional-trading` (free, 60min); FMP `senate-trading`  `house-trading` (free tier); HouseSenate Stock Watcher bulk JSON (no key); SEC EDGAR Form 4 (insider).

\- Tools `congress\_trades(symbolmember, since)` · `recent\_congress\_activity(days)` · `insider\_transactions(symbol)` · `new\_disclosures(watchlist, since)` (alert feed).

\- Caveat disclosures lag (often 30–45 days) — surfaced in output.



\### 4.4 `reports` — analystexpert reports \& cross-checks

\- Data Finnhub `recommendation-trends` + `price-target`; FMP `upgrades-downgrades` + `price-target`; SEC EDGAR full-text search + company facts (10-K10-Q).

\- Tools `analyst\_ratings(symbol)` · `price\_targets(symbol)` · `upgrades\_downgrades(symbol, since)` · `sec\_filings(symbol, forms)` · `cross\_check(symbol)` — reconciles analyst consensus vs. current pricetechnicals vs. fundamentals and flags divergences (e.g. consensus Buy, but price below all MAs and margins compressing).



\### 4.5 `orchestration` — recursion + alerts

\- `spawn\_analyst(role, task, context, model, depth)` — the recursion enabler (see §2§5).

\- Alerts `register\_alert(watchlist, kinds)` · `check\_alerts()` — congress + expert-report deltas since last run, emitted to consolefilewebhook sinks.



\---



\## 5. Layer 2 — Orchestration (verified SDK patterns)



Package `claude-agent-sdk` · `from claude\_agent\_sdk import query, ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition, tool, create\_sdk\_mcp\_server`.



\### 5.1 Custom in-process MCP tools (`@tool` + `create\_sdk\_mcp\_server`)

```python

@tool(compute\_indicators, Compute TA indicators for a symbol,

&#x20;     {symbol str, indicators list})

async def compute\_indicators(args dict) - dict

&#x20;   result = ta\_engine.compute(args\[symbol], args\[indicators])  # plain Python

&#x20;   return {content \[{type text, text result.to\_json()}]}



technical = create\_sdk\_mcp\_server(name=technical, version=0.1.0,

&#x20;                                 tools=\[get\_ohlcv, compute\_indicators, detect\_signals])

```

In-process SDK servers run in the same Python process (no subprocess) and can hold state

(cached clients, sessions).



\### 5.2 Native specialist subagents (depth-1, the common case)

```python

options = ClaudeAgentOptions(

&#x20;   model=claude-opus-4-8,

&#x20;   mcp\_servers={technical technical, sentiment sentiment,

&#x20;                congress congress, reports reports, orchestration orchestration},

&#x20;   allowed\_tools=\[WebSearch, WebFetch, Agent,

&#x20;                  mcp\_\_technical\_\_, mcp\_\_sentiment\_\_,

&#x20;                  mcp\_\_congress\_\_, mcp\_\_reports\_\_,

&#x20;                  mcp\_\_orchestration\_\_spawn\_analyst],

&#x20;   agents={

