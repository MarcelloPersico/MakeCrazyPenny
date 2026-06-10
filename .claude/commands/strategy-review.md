---
description: Hourly swarm strategy review - performance audit + in-depth market analysis; refreshes the standing goal (manages positions, never opens new ones)
argument-hint: [optional concern to investigate, e.g. "why are shorts bleeding"]
---

Run the swarm's STRATEGY REVIEW. You (the host model) are the portfolio
manager stepping back from the 15-minute tape: assess whether the strategy is
working, read the broader market in depth, and refresh the standing goal so
the next trade-swarm cycles inherit the updated strategy. This playbook never
OPENS positions - that stays with /trade-swarm.
$ARGUMENTS

## 0. Context
- `journal_digest` - the goal (split it at " || STRATEGY" into the CORE
  objective and the previous strategy block), the recent cycle one-liners,
  and `cycles_since_review` for the record.
- `journal_performance` - the full scoreboard you will judge the strategy
  against (this is the ONE place per hour it gets called - cycles use the
  digest only).
- `paper_account` - equity, margin in use, open positions (one-line list).

## 1. Fan out (Workflow tool - live progress visible in /workflows)
Launch the repo workflow `.claude/workflows/strategy-review.js`:

    Workflow({scriptPath: ".claude/workflows/strategy-review.js",
              args: {goal: <CORE objective only>, concern: <the argument, if any>,
                     positions: <one-line open-positions list>}})

Four read-only legs, easy jobs on small models: a haiku regime/pulse/funding
survey, a sonnet journal performance audit, a sonnet macro news scan
(days-to-weeks themes + scheduled events), and an opus BTC/ETH/SOL
multi-timeframe deep-dive with positioning. Returns
`{audit, regime, macro, majors}` for you to fuse.

Fallback: if the Workflow tool is unavailable or the run errors, gather the
four legs yourself in this session (journal_performance + journal_recent;
crypto_regime + market_pulse; news_feed; crypto_decide/derivatives/orderflow
on BTC/ETH/SOL at 4h/1d). If one leg fails (null), fuse the rest and note the gap.

## 2. Synthesize the strategy memo (you, the frontier model)
Fuse the four legs plus the scoreboard into a memo with exactly these parts:
- **Verdict on the current strategy**: is the previous strategy block (if any)
  paying? Cite the audit's numbers (hit rate, avg R, by-symbol PnL).
- **Market bias for the next hours**: regime + majors deep-dive + macro fused
  into long/short/neutral with a gross-exposure stance, plus what flips it.
- **Focus / avoid**: symbols or themes to hunt next cycles (catalysts with
  runway, rotating strength) and what to stop touching (the audit's losers,
  crowded trades per the funding map).
- **Timeframe mix**: which intervals deserve the next hour given what paid.
- **Process fixes**: behavior changes from the audit (sizing, unfilled-order
  hygiene, leg-weighting), phrased as instructions a future cycle can follow.

## 3. Manage existing positions (only risk-reducing actions)
For each open position contradicted by the new view: `paper_close` it or
tighten via `paper_set_tpsl`. Record why in the memo. Do NOT open anything,
do NOT add size - AVOID is a position, and entries belong to /trade-swarm.

## 4. Persist the strategy
- `swarm_goal_set` with: `<CORE objective> || STRATEGY @ <UTC time, e.g.
  2026-06-10T18:00Z>: bias=<...>; gross=<...>; focus=<...>; avoid=<...>;
  timeframes=<...>; fixes=<...>` - replace any previous block after
  " || STRATEGY", never the core objective. Keep the whole block compact
  (under ~600 characters): every future cycle pipes it verbatim into the
  subagent preambles.
- `journal_record` the review with `kind: "strategy-review"` (the tool's
  `kind` parameter) plus the memo's five parts in `summary` (one dense line,
  ~400 chars: the digest clips it and future cycles rehydrate from it) and
  any position actions taken. The trade-swarm cadence is
  `journal_digest`'s `cycles_since_review`, which resets only on this
  tagged record - skipping it makes every subsequent cycle re-run the review.

## 5. Report
Tell the user COMPACTLY (~12 lines): the verdict on the strategy so far
(with numbers), the new bias and why, what the next cycles will hunt/avoid,
any positions closed or tightened, and the exact goal string now standing.
Never paste raw workflow JSON - the journal record, not the chat, is the
durable copy.

Safety: testnet only, paper money; still treat it as real. Never raise
`leverage_cap` above the engine default; never disable the risk gate.
