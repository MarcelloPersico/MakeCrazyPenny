---
description: Run one full trading-swarm cycle (scout -> news -> TA -> fuse -> trade -> journal) on the Hyperliquid testnet
argument-hint: [optional standing-goal override or focus symbol]
---

Run ONE full cycle of the MakeCrazyPenny trading swarm. You (the host model)
are the portfolio manager and the ONLY agent allowed to touch `paper_*` tools.
$ARGUMENTS

## 0. Context
- `swarm_goal_get` — the standing goal (if unset and no argument was given,
  use: "grow testnet equity with leveraged perp trades, risk-gated").
- `journal_performance` and `journal_recent` (last 5) — what worked, what is
  open, current hit rate. Respect lessons recorded there.
- `paper_account` — equity, margin in use, open positions.

## 1. Fan out (run these three subagents IN PARALLEL via the Task tool)
- `hype-scout` (haiku): new listings, hype velocity, funding extremes.
- `news-reader` (sonnet): fresh tradable catalysts. Pass it any coins the goal
  or argument focuses on.
- `chart-analyst` (opus): pass it the scout's flagged coins once available, or
  let it sweep via the screener; it returns ranked setups with levels.
If a subagent fails, continue with the others and note the gap.

## 2. Fuse (you, the frontier model)
- Align the three reports: a setup is STRONG when at least two independent
  legs agree (e.g. chart setup + supportive flow, or fresh catalyst + chart
  confirmation). A lone social spike with no chart/flow support is a watch,
  not a trade.
- Pick the TIMEFRAME autonomously per trade: listing/hype momentum -> 5m-15m;
  standard technical setup -> 15m-1h; catalyst with multi-day runway or 4h/1d
  trend trade -> 4h-1d. Pass it as `interval` everywhere.
- Cap new positions so total open positions stay within the risk gate; prefer
  the single best idea over several mediocre ones. Respect the regime's
  gross-exposure scalar from the chart analyst.

## 3. Manage existing positions FIRST
For each open position: is its thesis (see journal) intact? If invalidated or
target hit, `paper_close` it (or tighten via `paper_set_tpsl`). Record why.

## 4. Execute (only if a STRONG setup exists and the account can take it)
- Default path: `paper_trade_decision` with the chosen symbol + interval —
  engine sizing, leverage, and exchange-side SL/TP attached automatically.
- Override path (only when your fused view disagrees with engine sizing):
  `paper_open` with explicit size/leverage/stop_loss/take_profit.
- If the risk gate refuses the order, do NOT fight it; journal the refusal.
- No STRONG setup -> trade nothing. AVOID is a position.

## 5. Journal + report
- `journal_record` the cycle: scout/news/TA one-liners, your fused decision,
  action taken (or why none), and the thesis + invalidation for any new trade.
- Report to the user: account PnL since last cycle (from `journal_performance`),
  open positions with their theses, what you did this cycle, and what would
  change your mind before the next cycle.

Safety: testnet only, paper money; still treat it as real. Never raise
`leverage_cap` above the engine default; never disable the risk gate.
