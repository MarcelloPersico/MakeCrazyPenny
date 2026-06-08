# MakeCrazyPenny — Design & Architecture

Agentic financial-analysis platform. A mother orchestrator agent spawns specialized
sub-agents (technical analysis, deep-search sentiment, congressional-trade alerts, and
expert-report cross-checks). Every capability is exposed over MCP so any MCP-capable
agent (Claude or otherwise) can drive it. Built in Python on the Claude Agent SDK.

Status: design only (no application code yet) — for review before implementation.

Not investment advice. Every report this system produces is informational; a disclaimer is
baked into the output.

> **Revision note (graph-driven).** This revision restructures the architecture from two
> layers to three after a knowledge-graph analysis of the original design surfaced a hidden
> coupling: a single provider (Finnhub) fanned into **four** capability servers and another
> (Alpha Vantage) into two, with no shared layer between them — duplicated keys, caches, and
> rate limits, and a single-point-of-failure that cascades across the system. See
> [§7](#7-graph-driven-revisions) for the full before/after and the metrics that motivated it.

---

## 1. Goals & requirements

From the brief:

1. Technical analysis — indicators, signals, multi-timeframe reads.
2. Deep-search sentiment analysis — news + social + LLM web research.
3. Congress trading alerts — disclosed House/Senate trades + insider (Form 4) activity.
4. Broad-scale expert-report alerts & cross-checks — analyst ratings / price targets /
   upgrades-downgrades, reconciled against price action and fundamentals to flag divergences.
5. Free APIs wherever possible; deep search where it adds value.
6. Mother agent orchestrates a specialized sub-agent per analysis type, and sub-agents can
   spin up their own sub-agents when a task needs to fan out further.

### Decisions locked in this session

- Language/runtime: Python (best fit for finance + pandas + the SDK).
- API posture: free-tier API keys are acceptable (kept in `.env`, never committed).
- Deep-search backend: Claude's built-in `WebSearch` + `WebFetch` server tools.
- This session: design/plan only.

---

## 2. The core architectural constraint (and how we solve it)

The brief requires recursive delegation — sub-agents that can spawn their own sub-agents.

The Claude Agent SDK's native subagents (the `Agent`/Task tool) are depth-1 only: a
subagent launched via the Task tool is not given the Task tool itself, so it cannot spawn
further subagents. This is a known, intentional limitation
([claude-code issue #4182](https://github.com/anthropics/claude-code/issues/4182),
[Subagents in the SDK](https://code.claude.com/docs/en/agent-sdk/subagents)).

**Solution — a recursive `spawn_analyst` MCP tool.** We expose an in-process MCP tool that,
when called, constructs a fresh nested `ClaudeSDKClient` with a role-specific prompt + toolset
and returns its result. Because it's just another tool, any agent (mother or sub) can call it,
giving true (bounded) recursion with full observability — unlike the `claude -p`-via-Bash hack.
Hard `max_depth` and `max_budget_usd` guards prevent runaway recursion/cost.

So we use a hybrid delegation model:

- Native `AgentDefinition` subagents for the common shallow fan-out (fast, simple).
- `spawn_analyst` recursion for the deep case (a report-checker spawning per-source
  verifiers; a sentiment agent spawning per-source readers; etc.).

---

## 3. Three-layer architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — Orchestration (Claude Agent SDK)                                 │
│                                                                            │
│   Mother / orchestrator  (claude-opus-4-8, high effort)                    │
│   plans → delegates → synthesizes a cross-checked report                   │
│        │                                                                   │
│        ├── technical-analyst   (sonnet-4-6)                                │
│        ├── sentiment-analyst   (haiku-4-5 fan-out + WebSearch/WebFetch)    │
│        ├── congress-tracker    (haiku-4-5)                                 │
│        └── report-checker      (opus/sonnet) ──┐ may recurse via           │
│                                                │ spawn_analyst             │
│              (any agent can call spawn_analyst → nested ClaudeSDKClient)    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │ MCP (in-process SDK servers + stdio)
┌───────────────────────────────┴───────────────────────────────────────────┐
│ LAYER 1 — Capability MCP servers (agent-agnostic)                          │
│                                                                            │
│   technical · sentiment · congress · reports · synthesis · orchestration   │
│        │          │          │          │          │            │          │
│        └──────────┴──────────┴────  depend only on  ───┴────────┴──────────┤
│                                 ▼                                           │
├────────────────────────────────────────────────────────────────────────--─┤
│ LAYER 0 — Provider / data-access layer (shared, single-instance)           │
│                                                                            │
│   ProviderRegistry  →  one cached, rate-limited, key-managed client per API │
│   ┌─────────────────────────────────────────────────────────────────────┐ │
│   │ global rate governor · TTL cache · retry/backoff · circuit breaker ·  │ │
│   │ per-capability fallback chains · single-flight dedup                  │ │
│   └─────────────────────────────────────────────────────────────────────┘ │
│   yahoo/yfinance · alpha_vantage · finnhub · fmp · edgar · stockwatcher     │
└──────────────────────────────────────────────────────────────────────────┘
```

**Why three layers (changed from two).** The original design folded data access into each
capability server ("providers: thin adapters … cached + rate-limited"). The graph analysis
showed the consequence: the same provider object is conceptually re-instantiated inside every
server that needs it, so caching, the API key, retry logic, and — critically — the free-tier
rate limit are *per server*, not global. **Layer 0** pulls every external API behind a single
shared `ProviderRegistry`:

- **Layer 0** — pure data access. One client instance per external API, shared process-wide.
  Owns rate limiting, caching, retries, circuit breaking, and fallback. Knows nothing about
  agents or MCP.
- **Layer 1** — pure capability (computation + tool surface) exposed as MCP tools. Depends
  *only* on Layer 0; servers never call each other (the lone cross-cutting case, `cross_check`,
  is isolated in its own `synthesis` server — see §4.5). Agent-agnostic, so Claude, Cursor,
  or any MCP host can mount it.
- **Layer 2** — reasoning/orchestration built specifically on the Claude Agent SDK.

Core logic lives in plain Python functions; each is exposed both as an in-process SDK MCP
server (fast, shares state, used by the orchestrator) and as a standalone stdio MCP server
(portable, for any other client). Both surfaces resolve providers from the *same* Layer-0
registry.

**Dependency rule (keeps the graph acyclic — the original had no import cycles, preserve it):**
Layer 2 → Layer 1 → Layer 0, never upward, and no sideways calls within Layer 1 except the
`synthesis` server, which composes other servers' *read-only* tool outputs.

---

## 4. Layer 1 — Capability MCP servers

Each tool resolves its data through the Layer-0 `ProviderRegistry` (never instantiating a
provider directly), returns compact structured JSON (text content blocks), and is namespaced
`mcp__server__tool`.

### 4.1 `technical` — market data & technical analysis
- Data: `yfinance` (primary, no key) for OHLCV; Alpha Vantage / Twelve Data / Finnhub as keyed
  fallbacks — resolved as a Layer-0 fallback chain, not hard-wired here.
- Indicators: `pandas-ta` (or the pure-Python `ta` package) — avoids the TA-Lib C dependency.
- Tools: `get_ohlcv(symbol, interval, period)` · `compute_indicators(symbol, indicators=[rsi,
  macd,bbands,sma,ema,atr,stoch,adx,obv])` · `detect_signals(symbol)` (golden/death cross, RSI
  extremes, MACD cross, BB breaks) · `support_resistance(symbol)` · `multi_timeframe_summary(symbol)`.

### 4.2 `sentiment` — news + social + deep search
- Data: Finnhub `company-news` + `news-sentiment`; Alpha Vantage `NEWS_SENTIMENT` (returns
  pre-computed scores); Marketaux (free); StockTwits/Reddit optional.
- Deep search done by the agent via Claude `WebSearch`/`WebFetch` — the MCP server supplies the
  quantified scores; the agent reasons over both structured scores and fresh web context.
- Tools: `get_news(symbol, days)` · `news_sentiment(symbol)` (aggregated) · `social_sentiment(symbol)`
  · `aggregate_sentiment(symbol)` (blended score + drivers).

### 4.3 `congress` — congressional & insider trades
- Data: Finnhub `stock/congressional-trading` (free, 60/min); FMP `senate-trading` /
  `house-trading` (free tier); House/Senate Stock Watcher bulk JSON (no key); SEC EDGAR Form 4
  (insider).
- Tools: `congress_trades(symbol|member, since)` · `recent_congress_activity(days)` ·
  `insider_transactions(symbol)` · `new_disclosures(watchlist, since)` (alert feed).
- Caveat: disclosures lag (often 30–45 days) — surfaced in output.

### 4.4 `reports` — analyst/expert reports
- Data: Finnhub `recommendation-trends` + `price-target`; FMP `upgrades-downgrades` +
  `price-target`; SEC EDGAR full-text search + company facts (10-K/10-Q).
- Tools: `analyst_ratings(symbol)` · `price_targets(symbol)` · `upgrades_downgrades(symbol, since)`
  · `sec_filings(symbol, forms)`.
- Note: the cross-domain reconciliation tool (`cross_check`) used to live here; it has moved to
  the new `synthesis` server because it spans technical + reports + fundamentals (see §4.5, §7).

### 4.5 `synthesis` — cross-domain reconciliation *(new — relocated `cross_check`)*
- `cross_check(symbol)` — reconciles analyst consensus vs. current price/technicals vs.
  fundamentals and flags divergences (e.g. consensus *Buy*, but price below all MAs and margins
  compressing).
- **Why its own server.** In the graph, `cross_check` was a high-betweenness bridge linking the
  technical, sentiment, and reports communities, yet it was filed as a leaf inside `reports`.
  That placement either forces `reports` to re-fetch technical + fundamentals data (duplicating
  Layer-0 work) or creates a sideways server-to-server dependency. Isolating it in `synthesis`
  makes the cross-cutting nature explicit: `cross_check` *composes* the read-only outputs of
  `technical`, `reports`, and fundamentals (all via Layer-0 cached calls), and is the only
  Layer-1 tool permitted to consume multiple capabilities.

### 4.6 `orchestration` — recursion + alerts
- `spawn_analyst(role, task, context, model, depth)` — the recursion enabler (see §2, §5).
- Alerts: `register_alert(watchlist, kinds)` · `check_alerts()` — congress + expert-report
  deltas since last run, emitted to console/file/webhook sinks. Delta detection reads through
  Layer-0's cache so an alert sweep across a large watchlist doesn't blow the rate budget.

---

## 5. Layer 0 — Provider / data-access layer *(new section)*

A single `ProviderRegistry` instantiated once at process start and injected into every
capability server. It is the only code that talks to an external API. Each provider adapter
stays "thin" (request → normalize → compact JSON) but the cross-cutting concerns are hoisted
into the registry so they apply uniformly and *globally*.

### 5.1 What the registry owns
- **Global rate governor.** One token bucket per API *key* (e.g. Finnhub 60/min, Alpha Vantage
  5/min·500/day). Every server draws from the same bucket, so concurrent sub-agents can't
  collectively exceed a free-tier limit. Requests that would exceed the budget queue or fail
  fast with a typed `RateLimited` error the agent can reason about.
- **Shared cache.** Keyed by `(provider, endpoint, normalized_params)`. Short TTL for quotes
  (seconds), long TTL for filings/disclosures (hours–days). In-memory L1 + on-disk L2 so it
  survives process restarts. **Single-flight**: identical in-flight requests collapse to one
  upstream call — important when four sub-agents ask for the same symbol at once.
- **Retry/backoff** with jitter on transient errors (timeouts, 5xx, 429).
- **Circuit breaker** per provider: after *N* consecutive failures/429s, open the circuit for a
  cooldown; while open, that provider is skipped and the capability falls through its fallback
  chain. This is the direct fix for the single-point-of-failure the graph exposed — a Finnhub
  outage degrades gracefully instead of breaking technical + sentiment + congress + reports at
  once.
- **Fallback chains.** Each capability declares an *ordered* provider list; the registry tries
  them in order, skipping any whose circuit is open or whose quota is exhausted, and tags the
  response with which provider actually served it. Examples:
  - OHLCV: `yfinance → alpha_vantage → twelve_data → finnhub`
  - news/sentiment: `finnhub → alpha_vantage → marketaux`
  - congress: `finnhub → fmp → stockwatcher`
  - ratings/targets: `finnhub → fmp`
- **Key management.** Loads free-tier keys from `.env`; never logged, never committed.
- **Quota/health surfacing.** Exposes remaining quota and per-provider circuit state so the
  orchestrator can mention degraded data in the final report.

### 5.2 Sketch
```python
registry = ProviderRegistry.from_env()      # one instance, process-wide

# A capability server resolves data through the registry, not a provider directly:
async def get_ohlcv(symbol, interval, period):
    return await registry.fetch(
        capability="ohlcv",                  # registry knows the fallback chain
        symbol=symbol, interval=interval, period=period,
    )                                         # caching, rate-limit, breaker applied for free
```

---

## 6. Layer 2 — Orchestration (verified SDK patterns)

Package `claude-agent-sdk` ·
`from claude_agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition, tool, create_sdk_mcp_server`.

### 6.1 Custom in-process MCP tools (`@tool` + `create_sdk_mcp_server`)
```python
@tool("compute_indicators", "Compute TA indicators for a symbol",
      {"symbol": str, "indicators": list})
async def compute_indicators(args: dict) -> dict:
    result = ta_engine.compute(args["symbol"], args["indicators"])   # plain Python
    return {"content": [{"type": "text", "text": result.to_json()}]}

technical = create_sdk_mcp_server(
    name="technical", version="0.1.0",
    tools=[get_ohlcv, compute_indicators, detect_signals],
)
```
In-process SDK servers run in the same Python process (no subprocess) and can hold state —
notably the shared Layer-0 `ProviderRegistry`, so cache and rate state are truly global.

### 6.2 Native specialist subagents (depth-1, the common case)
```python
options = ClaudeAgentOptions(
    model="claude-opus-4-8",
    mcp_servers={
        "technical": technical, "sentiment": sentiment,
        "congress": congress, "reports": reports,
        "synthesis": synthesis, "orchestration": orchestration,
    },
    allowed_tools=[
        "WebSearch", "WebFetch", "Agent",
        "mcp__technical__*", "mcp__sentiment__*",
        "mcp__congress__*", "mcp__reports__*",
        "mcp__synthesis__cross_check",
        "mcp__orchestration__spawn_analyst",
    ],
    agents={
        "technical-analyst": AgentDefinition(
            description="Indicators, signals, multi-timeframe reads.",
            model="claude-sonnet-4-6",
            tools=["mcp__technical__*"],
        ),
        "sentiment-analyst": AgentDefinition(
            description="News + social + deep web-search sentiment.",
            model="claude-haiku-4-5",
            tools=["mcp__sentiment__*", "WebSearch", "WebFetch"],
        ),
        "congress-tracker": AgentDefinition(
            description="Disclosed congressional + insider trades.",
            model="claude-haiku-4-5",
            tools=["mcp__congress__*"],
        ),
        "report-checker": AgentDefinition(
            description="Analyst reports reconciled against price/fundamentals; may recurse.",
            model="claude-sonnet-4-6",
            tools=["mcp__reports__*", "mcp__synthesis__cross_check",
                   "mcp__orchestration__spawn_analyst"],
        ),
    },
)
```
The mother orchestrator plans, delegates to these specialists (or recurses via
`spawn_analyst` for the deep cases), then synthesizes a single cross-checked report — with the
not-investment-advice disclaimer and any data-degradation caveats from Layer 0 baked in.

---

## 7. Graph-driven revisions

A knowledge graph built from the original `plan.md` (51 nodes, 64 edges, 7 communities) made
two structural problems measurable rather than intuitive:

1. **Provider fan-in / single point of failure.** `Finnhub` had the highest provider
   betweenness centrality (≈0.27) and was referenced by **four** capability servers
   (technical, sentiment, congress, reports); `Alpha Vantage` by two. In the original two-layer
   model nothing sat between those servers and the API, so the free-tier rate limit, cache, and
   key were effectively duplicated per server and any Finnhub outage hit four capabilities at
   once.
   → **Fix:** Layer 0 `ProviderRegistry` with a *global* rate governor, shared cache, circuit
   breaker, and fallback chains (§3, §5).

2. **Mis-placed cross-cutting tool.** `cross_check` showed up as a high-betweenness bridge
   spanning the technical, sentiment, and reports communities, yet it was filed as a leaf of the
   `reports` server — implying either duplicated data fetching or a sideways server dependency.
   → **Fix:** relocated to a dedicated `synthesis` server that composes other capabilities'
   read-only outputs (§4.5).

Both changes preserve the properties the graph confirmed were already good: no import cycles
(the dependency rule in §3 keeps it that way) and clean, single-purpose communities (each
capability server still maps to one community; Layer 0 simply becomes their shared sink).

Re-run the graph after implementation (`/graphify . --update`) to confirm `ProviderRegistry`
becomes the new provider-side hub and that no Layer-1 server depends on another except
`synthesis`.
