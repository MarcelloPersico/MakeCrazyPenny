export const meta = {
  name: 'trade-swarm-fanout',
  description: 'Swarm fan-out: hype-scout + news-reader in parallel, chart-analyst on the scout flags',
  whenToUse: 'Step 1 of /trade-swarm — returns {scout, news, charts} for the host to fuse; never trades',
  phases: [
    { title: 'Scout', detail: 'hype-scout: new listings, hype velocity, funding extremes', model: 'haiku' },
    { title: 'News', detail: 'news-reader: fresh tradable catalysts from the feed (skipped in scalp mode)', model: 'sonnet' },
    { title: 'Charts', detail: 'chart-analyst: ranked setups with levels on the flagged coins', model: 'opus' },
  ],
}

// args: { goal?: string, focus?: string, scalp?: boolean } — the standing goal,
// an optional focus symbol/override from the /trade-swarm argument, and the
// scalp flag (5m horizon: news leg skipped, chart leg stays on opus, majors only).
// Trading stays with the host: none of these agent types has paper_* tools.
// args may arrive JSON-encoded as a string depending on the caller; tolerate
// both shapes — a silently unparsed args would drop the scalp flag and run a
// full 15m cycle while claiming to scalp (observed 2026-06-10).
let a = args
if (typeof a === 'string') {
  try {
    a = JSON.parse(a)
  } catch {
    a = {}
  }
}
const goal = (a && a.goal) || 'grow testnet equity with leveraged perp trades, risk-gated'
const focus = (a && a.focus) || ''
const scalp = !!(a && a.scalp)
log(scalp ? 'mode: SCALP (5m, majors only, news skipped, charts on opus)' : 'mode: standard (full 3-leg fan-out)')
const focusLine = focus ? ` Focus this cycle: ${focus}.` : ''
const scalpLine = scalp
  ? ' SCALP MODE: 5m interval everywhere; liquid majors ONLY (BTC/ETH/SOL/BNB — testnet long-tail books are unreliable); flag/keep only what taker flow and the book confirm RIGHT NOW.'
  : ''
const preamble = `One trading-swarm cycle. Standing goal: ${goal}.${focusLine}${scalpLine}`

const SCOUT_SCHEMA = {
  type: 'object',
  required: ['flagged', 'summary'],
  properties: {
    summary: { type: 'string', description: 'One-line market-wide read; "nothing-notable" if quiet' },
    flagged: {
      type: 'array',
      items: {
        type: 'object',
        required: ['symbol', 'fired', 'tradable'],
        properties: {
          symbol: { type: 'string', description: 'Exchange symbol, e.g. BNBUSDT' },
          fired: { type: 'string', description: 'What fired (listing / velocity / funding extreme / trending) with the numbers' },
          tradable: { type: 'boolean', description: 'Tradable testnet perp per paper_pairs' },
          crowd: { type: 'string', description: 'One-line crowd-direction read' },
        },
      },
    },
  },
}

const NEWS_SCHEMA = {
  type: 'object',
  required: ['catalysts', 'summary'],
  properties: {
    summary: { type: 'string', description: 'One-line read; "no-tradable-news" when the feed is quiet' },
    catalysts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['coins', 'direction', 'magnitude', 'freshness', 'horizon', 'source'],
        properties: {
          coins: { type: 'array', items: { type: 'string' }, description: 'Exchange symbols affected' },
          direction: { type: 'string', enum: ['bullish', 'bearish', 'unclear'] },
          magnitude: { type: 'string', enum: ['minor', 'meaningful', 'major'] },
          freshness: { type: 'string', description: 'New information vs already-priced' },
          horizon: { type: 'string', enum: ['hours', 'days', 'weeks'] },
          source: { type: 'string' },
          note: { type: 'string' },
        },
      },
    },
  },
}

const CHARTS_SCHEMA = {
  type: 'object',
  required: ['regime', 'setups', 'summary'],
  properties: {
    regime: { type: 'string', description: 'Regime read and its gross-exposure implication' },
    summary: { type: 'string', description: 'One-line read; "no-setup" when nothing clears the bar' },
    positions: {
      type: 'array',
      description: 'Re-verdicts for the open positions listed in the goal/focus (omit when none are listed)',
      items: {
        type: 'object',
        required: ['symbol', 'verdict', 'evidence'],
        properties: {
          symbol: { type: 'string' },
          verdict: { type: 'string', enum: ['HOLD', 'CLOSE', 'TIGHTEN'] },
          evidence: { type: 'string', description: 'One line of evidence for the verdict' },
          new_stop: { type: 'string', description: 'Replacement stop, TIGHTEN only' },
        },
      },
    },
    setups: {
      type: 'array',
      maxItems: 3,
      items: {
        type: 'object',
        required: ['symbol', 'direction', 'interval', 'entry_zone', 'stop', 'target', 'conviction', 'reason_for', 'reason_against', 'invalidation'],
        properties: {
          symbol: { type: 'string' },
          direction: { type: 'string', enum: ['long', 'short'] },
          interval: { type: 'string', description: 'Chosen timeframe, e.g. 15m' },
          entry_zone: { type: 'string' },
          stop: { type: 'string' },
          target: { type: 'string' },
          conviction: { type: 'number', description: "The engine's conviction for the setup" },
          leverage_plan: { type: 'string' },
          reason_for: { type: 'string', description: 'Single strongest reason FOR' },
          reason_against: { type: 'string', description: 'Single strongest reason AGAINST' },
          invalidation: { type: 'string', description: 'What kills the setup' },
        },
      },
    },
  },
}

// News runs independently; the chart leg waits only on the scout so flagged
// coins flow straight into the analyst. Models are pinned explicitly so the
// cheap-jobs-to-cheap-models routing can never degrade to the host model.
// Scalp mode: news skipped (catalyst horizons of hours/days are noise at 5m,
// and its WebFetches are the slowest leg); charts stay on opus — promoted
// from sonnet 2026-06-10 because 5m entries live or die on the flow/book
// read, exactly where the stronger model earns its cost.
const [news, chartLeg] = await parallel([
  () =>
    scalp
      ? Promise.resolve(null)
      : agent(
          `${preamble} Run your news playbook: scan the feed, read the few articles that matter, and report tradable catalysts.`,
          { agentType: 'news-reader', model: 'sonnet', phase: 'News', label: 'news-reader', schema: NEWS_SCHEMA },
        ),
  async () => {
    const scout = await agent(
      `${preamble} Run your scout playbook and flag what is genuinely hot.`,
      { agentType: 'hype-scout', model: 'haiku', phase: 'Scout', label: 'hype-scout', schema: SCOUT_SCHEMA },
    )
    const flagged = scout && Array.isArray(scout.flagged)
      ? scout.flagged.filter((f) => f && f.tradable && f.symbol).map((f) => f.symbol)
      : []
    log(
      flagged.length
        ? `scout flagged ${flagged.join(', ')} -> chart-analyst vets them`
        : 'scout found nothing notable -> chart-analyst sweeps the screener',
    )
    const charts = await agent(
      `${preamble} ${
        flagged.length
          ? `Candidate coins from the scout: ${flagged.join(', ')}. Vet them per your playbook; if they all die you may sweep the screener instead.`
          : 'No scout candidates; sweep via the screener per your playbook.'
      }${
        focus
          ? ` FIRST PRIORITY: for each open position listed in the goal/focus, return a positions[] entry with a HOLD/CLOSE/TIGHTEN verdict and one line of evidence — do this before vetting new candidates.`
          : ''
      }`,
      {
        agentType: 'chart-analyst',
        model: 'opus',
        phase: 'Charts',
        label: 'chart-analyst',
        schema: CHARTS_SCHEMA,
      },
    )
    return { scout: scout, charts: charts }
  },
])

if (!news && !scalp) log('news-reader leg failed; fuse without it')
if (!chartLeg) log('scout/chart leg failed; fuse without it')

return {
  scout: chartLeg ? chartLeg.scout : null,
  news: news || null,
  charts: chartLeg ? chartLeg.charts : null,
}
