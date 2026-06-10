# MakeCrazyPenny — project guide

AI-free, MCP-driven quant **trade-decision** toolkit (stocks + crypto perps) plus a
**Hyperliquid testnet paper-trading** execution layer. Console scripts: `makecrazypenny`
(CLI), `makecrazypenny-mcp` (stdio MCP server), `makecrazypenny-dashboard`.

## Read first — memory notes (persisted context for this project)

To work in this repo, and especially to use/extend the crypto paper-trading tool with the
most efficiency, read these local memory files (under
`~/.claude/projects/C--Users-persi-Desktop-MakeCrazyPenny/memory/`):

- [Crypto trading guide](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MakeCrazyPenny\memory\makecrazypenny-crypto-trading-guide.md)
  — **start here to trade**: prerequisites, the `paper_*` tools, the decide→place workflow, and the gotchas.
- [Execution layer](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MakeCrazyPenny\memory\makecrazypenny-execution-layer.md)
  — the Hyperliquid testnet write path, design choices, and live-verified operational findings (agent vs master wallet, spot vs perp collateral).
- [Crypto expansion](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MakeCrazyPenny\memory\makecrazypenny-crypto-expansion.md)
  — the leveraged-perpetuals decision track (data sources, risk preset, leverage plan).
- [Architecture](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MakeCrazyPenny\memory\makecrazypenny-architecture.md)
  — how the layers (`providers/` → `servers/` → `orchestration/`, plus `execution/`) fit and where to extend.
- Index of all memory: [MEMORY.md](C:\Users\persi\.claude\projects\C--Users-persi-Desktop-MakeCrazyPenny\memory\MEMORY.md)

## Authoritative docs in-repo
- `CONTRACT.md` — the spec. §16 = crypto track; §17 = Hyperliquid execution layer (incl. §17.5 operational notes).
- `README.md` — install + usage, incl. the "Paper trade on the Hyperliquid testnet" section.

## Trade-crypto quickstart
1. `paper_pairs` (keyless) — confirm the coin is a tradable testnet perp.
2. `crypto_decide SYMBOL` or `crypto_screen` — leverage-aware verdict (screener already filters to HL-listed perps).
3. `paper_account` — check `tradable_usdc` (perp + spot).
4. `paper_trade_decision SYMBOL` — decide + place the engine-sized order (or `paper_open` for a manual order).
5. `paper_account` / `paper_orders` to monitor; `paper_close` / `paper_cancel` to exit.

Needs `MCP_HL_PRIVATE_KEY` (testnet wallet) and, for an API/agent key, `MCP_HL_ACCOUNT_ADDRESS`
(the funded master). Testnet only, live (no dry-run). See the trading guide for the full runbook.

## Conventions
- Import-safe (lazy heavy imports / SDK; no network at import). Keyless-first; every user output carries the `DISCLAIMER`. CLI output is strictly ASCII.
- Tests are offline/no-network (`pytest`, `asyncio_mode=auto`); the SDK is mocked via the `_build_clients` seam. Run: `.venv\Scripts\python.exe -m pytest -q`.
- The knowledge graph in `graphify-out/` (graph.html / GRAPH_REPORT.md) is queryable via `/graphify query "..."`.
