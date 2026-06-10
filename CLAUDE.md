# MakeCrazyPenny — project guide

AI-free, MCP-driven quant **trade-decision** toolkit (stocks + crypto perps) plus a
**Hyperliquid testnet paper-trading** execution layer and a **multi-agent trading swarm**
(haiku scout / sonnet news / opus charts / host executes). Console scripts: `makecrazypenny`
(CLI), `makecrazypenny-mcp` (stdio MCP server), `makecrazypenny-dashboard`.

## Read first — memory notes (persisted context for this project)

To work in this repo, and especially to use/extend the crypto paper-trading tool and the
swarm with the most efficiency, read these local memory files (under
`~/.claude/projects/C--Users-persi-Desktop-MCPenny/memory/`):

- [Crypto trading guide](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MCPenny\memory\mcpenny-crypto-trading-guide.md)
  — **start here to trade**: prerequisites, the `paper_*` tools, the decide→place workflow, and the gotchas.
- [Trading swarm](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MCPenny\memory\mcpenny-trading-swarm.md)
  — the multi-agent loop: roles/models, the swarm tools, the journal/PnL layer, the risk gate, and how to run/loop it.
- [Architecture](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MCPenny\memory\mcpenny-architecture.md)
  — how the layers (`providers/` → `servers/` → `orchestration/`, plus `execution/`) fit and where to extend.
- Index of all memory: [MEMORY.md](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MCPenny\memory\MEMORY.md)

(The pre-2026-06 memory lived under the old `MakeCrazyPenny` project folder and was lost
in the rename to `MCPenny`; the files above are the rebuilt, current set.)

## Authoritative docs in-repo
- `CONTRACT.md` — the spec. §16 = crypto track; §17 = Hyperliquid execution layer (incl.
  §17.5 operational notes); §18 = the swarm extension (free data sources, scored factors,
  journal/PnL, risk gate, orchestration split).
- `README.md` — install + usage, incl. "Paper trade on the Hyperliquid testnet" and
  "The trading swarm".
- `research-out/` — the research dossiers behind §18 (DESIGN-SWARM.md + per-topic JSON).

## Trade-crypto quickstart
1. `paper_pairs` (keyless) — confirm the coin is a tradable testnet perp.
2. `crypto_decide SYMBOL` or `crypto_screen` — leverage-aware verdict (screener already filters to HL-listed perps).
3. `paper_account` — check `tradable_usdc` (perp + spot).
4. `paper_trade_decision SYMBOL` — decide + place the engine-sized order (or `paper_open` for a manual order).
5. `paper_account` / `paper_orders` to monitor; `paper_close` / `paper_cancel` to exit.

Needs `MCP_HL_PRIVATE_KEY` (testnet wallet) and, for an API/agent key, `MCP_HL_ACCOUNT_ADDRESS`
(the funded master). Testnet only, live (no dry-run). See the trading guide for the full runbook.

## Swarm quickstart
- One cycle: `/trade-swarm` (repo command) or the `trade_swarm` MCP prompt; loop with
  `/loop 15m /trade-swarm`. Set the objective once via `swarm_goal_set`.
- Scout/news/chart subagents live in `.claude/agents/` (haiku/sonnet/opus; no trading
  tools) — only the host session places orders, through the §18.5 risk gate.
- Scoreboard: `journal_performance` (PnL vs fills); memory: `journal_recent`.

## Conventions
- Import-safe (lazy heavy imports / SDK; no network at import). Keyless-first; every user output carries the `DISCLAIMER`. CLI output is strictly ASCII.
- The quant engine stays AI-free: LLM readings merge only via the finalize/debate path, never into `score_crypto_evidence`.
- Tests are offline/no-network (`pytest`, `asyncio_mode=auto`); the SDK is mocked via the `_build_clients` seam. Run: `.venv\Scripts\python.exe -m pytest -q`.
- The knowledge graph in `graphify-out/` (graph.html / GRAPH_REPORT.md) is queryable via `/graphify query "..."`.
