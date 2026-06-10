---
description: Run one full trading-swarm cycle (scout -> news -> TA -> fuse -> trade -> journal) on the Hyperliquid testnet
argument-hint: [scalp] [optional standing-goal override or focus symbol]
---

Run ONE full cycle of the MakeCrazyPenny trading swarm. You (the host model)
are the portfolio manager and the ONLY agent allowed to touch `paper_*` tools.
$ARGUMENTS

## 0. Context (ONE compact call + the account)
- `journal_digest` — the single cycle-start read: standing goal, recent cycle
  one-liners, open theses/decisions, equity tail, and `cycles_since_review`.
  If the goal is unset and no argument was given, use: "grow testnet equity
  with leveraged perp trades, risk-gated". Any " || STRATEGY @ ..." block in
  the goal is the latest hourly strategy review — treat its
  bias/focus/avoid/fixes as standing orders for this cycle.
- `paper_account` — equity, margin in use, open positions.
- Do NOT call `journal_recent` or `journal_performance` here: the digest
  replaces them at cycle start (the full scoreboard belongs to the strategy
  review), and every extra dump bloats the looped session's context.

## 0.5 Strategy review cadence (every 4th cycle ~ hourly at the 15m loop)
A review is DUE when the digest's `cycles_since_review` >= 4. In SCALP mode
(5m loop) additionally require the digest's `last_review.ts` to be older
than ~50 minutes, so the deep review stays hourly. When due: run the FULL
`/strategy-review` playbook (`.claude/commands/strategy-review.md`) NOW — it
fans out its own workflow, refreshes the standing goal, and journals itself
with `kind: "strategy-review"` (which resets the counter) — then continue
this cycle under the refreshed strategy.

## 1. Fan out (Workflow tool — live progress visible in /workflows)
Launch the repo workflow `.claude/workflows/trade-swarm-fanout.js`:

    Workflow({scriptPath: ".claude/workflows/trade-swarm-fanout.js",
              args: {goal: <standing goal>, focus: <focus symbol/override from the argument, if any>,
                     scalp: <true when the argument contains the word "scalp">}})

It runs `hype-scout` (haiku) and `news-reader` (sonnet) in parallel, feeds the
scout's tradable flags to `chart-analyst` (opus), and returns structured
`{scout, news, charts}` for you to fuse. Models are pinned inside the script.
The user watches it live via /workflows.

SCALP MODE (argument contains "scalp"): the workflow skips the news leg; the
chart leg stays on opus (5m entries live or die on the flow/book read). You
must use `interval: "5m"` for every decide and trade call, restrict
candidates to liquid majors (BTC/ETH/SOL/BNB), and require taker-flow + book
confirmation — a 5m setup without live flow behind it is a pass.

Fallback: if the Workflow tool is unavailable or the run errors, spawn the
three subagents directly IN PARALLEL via the Task tool instead (`hype-scout`,
`news-reader`, then `chart-analyst` with the scout's flagged coins). Either
way: if one leg fails (null), continue with the others and note the gap.

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
  The `summary` must stand alone as ONE line — the digest clips it to ~200
  chars and future cycles (and post-compaction rehydration) see only that.
- Report to the user COMPACTLY (~10 lines max): equity change since last
  cycle (digest equity tail vs `paper_account`), open positions with their
  theses, what you did this cycle, and what would change your mind before
  the next cycle.

## Context hygiene (looped sessions run for hours)
- The JOURNAL is your memory; the chat is disposable. Never paste raw
  workflow JSON or tool dumps into your reply — report fused conclusions
  only. Everything worth remembering goes through `journal_record`.
- If you are missing context about earlier cycles (e.g. after a context
  compaction), call `journal_digest` — do not reconstruct from the
  conversation, do not re-ask the user.
- Verbose evidence-gathering belongs in the workflow's subagents (their
  context is separate); keep host-side tool calls to the few listed here.

Safety: testnet only, paper money; still treat it as real. Never raise
`leverage_cap` above the engine default; never disable the risk gate.
