export const meta = {
  name: 'strategy-review',
  description: 'Hourly swarm strategy review: performance audit + regime survey + macro themes + majors deep-dive',
  whenToUse: 'Step 1 of /strategy-review (auto-triggered every 4th /trade-swarm cycle) — returns four independent legs for the host to fuse into a refreshed standing goal; never trades',
  phases: [
    { title: 'Audit', detail: 'performance auditor: journal scoreboard — what works, what leaks', model: 'sonnet' },
    { title: 'Regime', detail: 'regime surveyor: regime read + universe pulse + funding map (mechanical relay)', model: 'haiku' },
    { title: 'Macro', detail: 'news-reader on a days-to-weeks horizon: themes + scheduled events', model: 'sonnet' },
    { title: 'Deep dive', detail: 'chart-analyst: BTC/ETH/SOL multi-timeframe structure + positioning', model: 'opus' },
  ],
}

// args: { goal?: string, concern?: string, positions?: string } — the CORE
// standing objective (without any existing "|| STRATEGY" block), an optional
// host concern to investigate (e.g. "why are shorts bleeding"), and a one-line
// list of open positions. Tolerate JSON-encoded string args like the fan-out
// script does — a silently unparsed args would run a generic review.
let a = args
if (typeof a === 'string') {
  try {
    a = JSON.parse(a)
  } catch {
    a = {}
  }
}
const goal = (a && a.goal) || 'grow testnet equity with leveraged perp trades, risk-gated'
const concern = (a && a.concern) || ''
const positions = (a && a.positions) || 'none reported'
const concernLine = concern ? ` The host specifically wants this investigated: ${concern}.` : ''
const preamble =
  `Hourly STRATEGY REVIEW for the trading swarm (not a trade cycle - nobody trades here). ` +
  `Core objective: ${goal}. Open positions right now: ${positions}.${concernLine} ` +
  `Think in hours-to-days, not the next 15 minutes.`

const AUDIT_SCHEMA = {
  type: 'object',
  required: ['summary', 'what_works', 'what_fails', 'process_fixes'],
  properties: {
    summary: { type: 'string', description: 'One-line scoreboard read: hit rate, avg R, realized PnL trend' },
    what_works: { type: 'array', items: { type: 'string' }, description: 'Patterns that made money (symbol/timeframe/leg/setup type), with the numbers' },
    what_fails: { type: 'array', items: { type: 'string' }, description: 'Patterns that lost money or never filled, with the numbers' },
    process_fixes: { type: 'array', items: { type: 'string' }, description: 'Concrete behavior changes for the next cycles (sizing, timeframes, symbols to avoid, unfilled-order hygiene)' },
    open_position_notes: { type: 'array', items: { type: 'string' }, description: 'Per open position: thesis age, drawdown, anything the journal says about it' },
  },
}

const REGIME_SCHEMA = {
  type: 'object',
  required: ['regime', 'summary'],
  properties: {
    regime: { type: 'string', description: 'crypto_regime verbatim verdict + its gross-exposure scalar' },
    movers: { type: 'array', items: { type: 'string' }, description: 'Top movers from market_pulse with the percentage' },
    funding_extremes: { type: 'array', items: { type: 'string' }, description: 'Coins at funding extremes with the rate (crowding map)' },
    new_listings: { type: 'array', items: { type: 'string' }, description: 'Newly listed perps since the last universe snapshot' },
    summary: { type: 'string', description: 'One-line breadth read: risk-on/risk-off, broad or narrow' },
  },
}

const MACRO_SCHEMA = {
  type: 'object',
  required: ['themes', 'events', 'summary'],
  properties: {
    summary: { type: 'string', description: 'One-line macro read; "no-dominant-theme" when quiet' },
    themes: {
      type: 'array',
      items: {
        type: 'object',
        required: ['theme', 'direction', 'coins', 'horizon'],
        properties: {
          theme: { type: 'string' },
          direction: { type: 'string', enum: ['bullish', 'bearish', 'unclear'] },
          coins: { type: 'array', items: { type: 'string' }, description: 'Exchange symbols this theme moves' },
          horizon: { type: 'string', enum: ['days', 'weeks'] },
          evidence: { type: 'string', description: 'The strongest single piece of evidence, with source' },
        },
      },
    },
    events: {
      type: 'array',
      items: {
        type: 'object',
        required: ['event', 'when', 'why_it_matters'],
        properties: {
          event: { type: 'string', description: 'Scheduled catalyst: FOMC/CPI, unlock, upgrade, listing, deadline' },
          when: { type: 'string', description: 'Date or "next N days"' },
          why_it_matters: { type: 'string' },
        },
      },
    },
  },
}

const DEEP_SCHEMA = {
  type: 'object',
  required: ['majors', 'market_bias', 'summary'],
  properties: {
    majors: {
      type: 'array',
      items: {
        type: 'object',
        required: ['symbol', 'structure', 'key_levels', 'positioning', 'bias'],
        properties: {
          symbol: { type: 'string' },
          structure: { type: 'string', description: '1d + 4h trend/range read: where price sits in the structure' },
          key_levels: { type: 'string', description: 'The 2-3 levels that matter for the next sessions' },
          positioning: { type: 'string', description: 'Derivatives read: funding, OI, top-trader vs crowd, taker flow' },
          bias: { type: 'string', enum: ['long', 'short', 'neutral'] },
          flip_condition: { type: 'string', description: 'What flips this bias' },
        },
      },
    },
    market_bias: { type: 'string', description: 'Fused directional bias for the whole tape with a confidence word' },
    rotation: { type: 'string', description: 'Where the strength/weakness is rotating (majors vs alts, sectors)' },
    summary: { type: 'string', description: 'One-line read for the portfolio manager' },
  },
}

// Four independent legs, cheap-jobs-to-cheap-models: the regime survey is a
// mechanical fetch-and-relay (haiku), the journal audit and macro scan are
// bounded analysis (sonnet), and only the multi-timeframe majors read gets
// opus. All legs are read-only; synthesis and the goal update stay with the
// host. A failed leg returns null - the host fuses what survives.
const [audit, regime, macro, majors] = await parallel([
  () =>
    agent(
      `${preamble} You are the swarm's performance auditor. Use ToolSearch to load ` +
        `mcp__makecrazypenny__journal_performance and mcp__makecrazypenny__journal_recent, then call ` +
        `journal_performance and journal_recent (n=20). Audit the swarm's recent trading: hit rate and avg R ` +
        `overall and per symbol, which timeframes/setups paid, what kept losing or going unfilled, recurring ` +
        `mistakes in the cycle notes, and anything notable about the open positions. Cite numbers from the ` +
        `journal, not impressions. Read-only: those two tools and nothing else.`,
      { model: 'sonnet', phase: 'Audit', label: 'performance-auditor', schema: AUDIT_SCHEMA },
    ),
  () =>
    agent(
      `${preamble} You are a read-only market surveyor. Use ToolSearch to load ` +
        `mcp__makecrazypenny__crypto_regime, mcp__makecrazypenny__market_pulse and ` +
        `mcp__makecrazypenny__funding_rate, then: call crypto_regime once; call market_pulse once; call ` +
        `funding_rate for BTCUSDT and ETHUSDT. Relay what the tools say - verbatim numbers, no narrative ` +
        `speculation, no extra tool calls.`,
      { model: 'haiku', phase: 'Regime', label: 'regime-surveyor', schema: REGIME_SCHEMA },
    ),
  () =>
    agent(
      `${preamble} Run your news playbook with a STRATEGIC lens: instead of immediate tradable catalysts, ` +
        `extract the dominant themes over the next days-to-weeks and the scheduled events (macro prints, ` +
        `unlocks, upgrades, regulatory deadlines) the swarm should trade around. Already-priced narratives ` +
        `are noise; flag only what still has runway.`,
      { agentType: 'news-reader', model: 'sonnet', phase: 'Macro', label: 'macro-reader', schema: MACRO_SCHEMA },
    ),
  () =>
    agent(
      `${preamble} Skip the candidate hunt this time: this is the hourly DEEP market read. For BTCUSDT, ` +
        `ETHUSDT and SOLUSDT, build the multi-timeframe picture (1d and 4h structure with 1h context via ` +
        `crypto_technicals/crypto_decide) and the positioning picture (derivatives + orderflow: funding, OI, ` +
        `top-trader vs crowd, taker flow, CVD). Start from crypto_regime. Then fuse a market-wide bias and ` +
        `say where strength is rotating and what would flip the read.`,
      { agentType: 'chart-analyst', model: 'opus', phase: 'Deep dive', label: 'majors-deep-dive', schema: DEEP_SCHEMA },
    ),
])

const legs = { audit: !!audit, regime: !!regime, macro: !!macro, majors: !!majors }
const failed = Object.keys(legs).filter((k) => !legs[k])
if (failed.length) log(`leg(s) failed: ${failed.join(', ')} - fuse without them`)

return {
  audit: audit || null,
  regime: regime || null,
  macro: macro || null,
  majors: majors || null,
}
