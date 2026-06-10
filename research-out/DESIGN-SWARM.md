# Swarm upgrade design (2026-06-10)

Goal: refine MakeCrazyPenny into an always-on multi-agent crypto trading system.
Free APIs / scraping only; the app STAYS an MCP server; models come from the user's
Claude Code subscription (no API key). Roles: haiku scouts (social/new listings),
sonnet news reader, opus chart analyst, host frontier model (fable) fuses + trades
on the Hyperliquid TESTNET via the existing paper_* tools. PnL tracked in a journal.

Research inputs: see sibling files pipeMap/provMap/execMap/docsMap/sentimentSrc/
newsSrc/hlSrc/metricsRes/orchRes/critique (.json) in this directory.

## Verified-from-this-host facts (probed 2026-06-10)

- Binance fapi REACHABLE: /fapi/v1/klines, /futures/data/topLongShortPositionRatio,
  /futures/data/takerlongshortRatio, /fapi/v1/fundingRate all 200, keyless.
- reddit.com hard-403s (datacenter egress). Use Arctic Shift mirror instead:
  https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=X&limit=N (200, keyless).
- StockTwits symbol stream 200: https://api.stocktwits.com/api/2/streams/symbol/BTC.X.json
  (messages carry platform-native Bullish/Bearish labels -> deterministic counting).
- 4chan /biz/ catalog 200: https://a.4cdn.org/biz/catalog.json (mention counting only).
- CoinTelegraph RSS 200 (https://cointelegraph.com/rss); Google News RSS per-query 200
  (https://news.google.com/rss/search?q=...); CoinDesk RSS 308-redirects (httpx
  follow_redirects=True required).
- CoinGecko /search/trending 200 keyless; real budget ~5 req/min keyless (provider
  declares 10 — lower it to 5).
- Hyperliquid testnet /info works; main info API for MARKET DATA reads is
  https://api.hyperliquid.xyz/info (POST {"type": ...}).

## Hard constraints (CONTRACT + critique V1-V7)

1. Engine stays AI-free and deterministic. LLM interpretations NEVER enter
   score_crypto_evidence; they merge only at host level via crypto_finalize_decision.
   Deterministic social/news METRICS (counts, velocity z-scores, platform-native
   bullish/bearish tallies) MAY be scored as factors.
2. Testnet-only WRITE path unchanged. The HL read provider may hit the main info URL
   for market data, but no Settings attribute name may contain "mainnet"
   (tests/test_execution.py:274). Field name: `hyperliquid_info_url`.
   CONTRACT.md gets an amendment scoping the testnet invariant to the signed write path.
3. Keyless-first; optional keyed providers use the MissingApiKey silent-skip contract.
4. Strict ASCII in every user-facing output: sanitize titles (Reddit/4chan/news are
   arbitrary Unicode) — strip non-ASCII, keep tickers via regex.
5. Import-safe: lazy httpx imports, no network at import, providers registered via
   @register_provider; offline tests mock via respx or the get_registry seam.
6. Unattended loop safety: portfolio risk gate + daily-loss kill-switch must exist
   before any scheduled headless trading.

## New capabilities -> providers (data shapes are the integration contract)

Provider modules and the capability payloads they return under the registry envelope
{"provider", "data", "cached"}:

### providers/hyperliquid_info.py  (NEW, keyless; owner: agent W1)
Base: settings.hyperliquid_info_url (default https://api.hyperliquid.xyz/info), POST JSON.
- "hl_asset_ctx" (symbol) -> {coin, mark_price, oracle_price, mid_price,
  funding_hourly, funding_annualized, open_interest, premium, day_volume_usd,
  max_leverage, impact_bid, impact_ask, prev_day_price, as_of}
  from {"type":"metaAndAssetCtxs"}; symbol matched against universe coin names
  (BTCUSDT -> BTC mapping via core.symbols helpers).
- "hl_predicted_funding" (symbol) -> {coin, venues: [{venue, rate, interval_hours}], as_of}
  from {"type":"predictedFundings"}.
- "hl_l2book" (symbol, optional n_sig_figs) -> {coin, bids: [[px, sz], ...],
  asks: [[px, sz], ...], as_of} from {"type":"l2Book","coin":...}. Top 20 levels each side.
- "hl_funding_history" (symbol, hours=72) -> {coin, rates: [{time, rate}], as_of}
  from {"type":"fundingHistory","coin":...,"startTime":ms}.
- "hl_market_pulse" () -> {assets: [{coin, mark_price, day_change_pct, funding_hourly,
  funding_annualized, open_interest_usd, day_volume_usd, max_leverage, premium}],
  new_listings: [coin, ...], as_of} from one metaAndAssetCtxs call; new_listings =
  diff vs a previous universe snapshot persisted under settings.resolve_cache_dir()
  (file hl_universe_snapshot.json; first run -> empty list, write snapshot).
Rate: 60/min shared bucket. TTLs: ctx/pulse 30s, predicted 60s, l2book 5s, funding hist 300s.

### providers/binance.py  (EXTEND in place; owner: agent W2)
- Extend kline parsing to keep field 5 (volume), field 7 (quote volume) and field 9
  (taker buy base volume) -> bars rows gain "volume", "quote_volume", "taker_buy_volume".
  KEEP existing keys unchanged (additive only — factors/indicators consume o/h/l/c).
- "taker_flow" (symbol, interval, limit=48) -> {series: [{time, buy_sell_ratio}], as_of}
  from /futures/data/takerlongshortRatio.
- "top_trader_ratio" (symbol, interval, limit=48) -> {series: [{time, ratio}], as_of}
  from /futures/data/topLongShortPositionRatio (positions, not accounts).
- "funding_history" (symbol, limit=66) -> {rates: [{time, rate}], as_of}
  from /fapi/v1/fundingRate (66 ~= 22 days of 8h fundings).
bybit.py: do NOT add these capabilities (no equivalent keyless data) — chains simply
have a single provider; registry tolerates AllProvidersFailed and the dossier carries
_error markers.

### providers/social.py  (NEW, keyless; owner: agent W3)
- "social_scan" (symbol or "CRYPTO", limit=25) -> {reddit: {posts: [{title_ascii,
  created_utc, score, num_comments, subreddit}], post_velocity_per_hr, prev_velocity_per_hr},
  stocktwits: {bullish, bearish, neutral, n_messages, newest_ts},
  fourchan_biz: {thread_mentions, total_threads},
  trending: {coins: [{id, symbol, rank}], symbol_trending: bool}, as_of}
  Sources: Arctic Shift (subreddits CryptoCurrency, CryptoMarkets, SatoshiStreetBets,
  Hyperliquid + coin-specific when symbol given), StockTwits SYMBOL.X stream,
  a.4cdn.org/biz/catalog.json, CoinGecko /search/trending.
  Each sub-source independently tolerant (failure -> {"_error": ...} for that key).
  ALL text ASCII-sanitized at the provider boundary.

### providers/news_rss.py  (NEW, keyless; owner: agent W3)
- "news_feed" (symbol or "CRYPTO", limit=30) -> {items: [{title_ascii, source,
  published_utc, url, age_minutes}], as_of}
  Feeds: cointelegraph.com/rss, coindesk RSS (follow redirects), Google News RSS
  (query "<coin name> OR <symbol> crypto" when symbol given; "crypto OR bitcoin"
  otherwise). Parse with stdlib xml.etree (no new deps); dedupe by normalized title;
  sort newest first.

## New pure metrics in analysis/  (owner: agent W4)

analysis/crypto_metrics.py additions (same (strength, rationale) tuple convention,
None when not computable):
- taker_flow_signal(series) -> mean log(buy_sell_ratio) over last N windows scaled by
  saturation 0.10; pro-trend (flow confirms direction). Weight 1.5, category "flow".
- cvd_signal(bars) -> CVD = cumsum(2*taker_buy_volume - volume); signal = sign of
  CVD slope over last 20 bars vs price slope: confirm (+) / diverge (-) scaled.
  Weight 1.0, category "flow".
- top_trader_spread_signal(top_ratio, crowd_ratio) -> log(top/crowd) clamp at ln(1.5);
  FOLLOW top traders (positive when tops longer than crowd). Weight 1.0, "positioning".
- funding_z_signal(rates, current) -> z vs trailing 21d; contrarian beyond |z|>=1.5,
  0 inside. Weight 1.0, "funding".
- predicted_funding_signal(current_hourly, predicted_venues) -> flip/extreme detector:
  contrarian strength when predicted moves further against position crowding; 0 when
  aligned/small. Weight 0.75, "funding".
- social_velocity_signal(post_velocity, prev_velocity, st_bull, st_bear) ->
  velocity_ratio = velocity/prev (cap 5x); bull_share = bull/(bull+bear).
  strength = (bull_share - 0.5) * 2 * min(1, log1p(velocity_ratio)); weight 0.5,
  category "social". (Deterministic counting only.)
- depth_imbalance(bids, asks, mid, band_bps=20) -> (bid_depth-ask_depth)/(sum) within
  the band. NOT scored — order-time entry gate (returned via orderflow tool).
- venue_divergence(hl_mark, cex_mark) -> abs bps; NOT scored — execution sanity gate.
analysis/risk.py additions:
- parkinson_vol(bars, periods_per_year), yang_zhang_vol(bars, periods_per_year).
- kelly_calibrated(conviction, journal_stats) -> p_eff = min(p_conviction,
  (wins+2)/(n+4)); quarter-Kelly until n>=50 closed trades, then half-Kelly.
- correlated_exposure_check(positions, candidate, betas, cap_mult=2.0) ->
  {allowed, scaled_notional, reason}; beta clusters: corr>0.7 to BTC -> one bucket.

## Scoring integration  (owner: ME, after agents land)

orchestration/crypto.py:
- gather_crypto_evidence gains tasks: "flow" (taker_flow + top_trader_ratio +
  funding_history via binance), "hl" (hl_asset_ctx + hl_predicted_funding),
  "social" (social_scan), "news" (news_feed) — each tolerant.
- score_crypto_evidence adds: _score_flow (taker_flow_signal, cvd_signal),
  _score_positioning2 (top_trader_spread_signal), _score_funding_z (funding_z_signal,
  predicted_funding_signal), _score_social (social_velocity_signal).
  News is NOT scored (host-side interpretation only) but rides in the dossier.
- Funding COST for leverage_plan: HL-first (funding_hourly, interval 1h) with
  Binance fallback — fixes the wrong-venue carry number.
- Conviction: corroboration divisor n_categories/4 -> /5; corroboration gate
  >=2 categories -> >=3 categories OR |net| >= 2.5 (more available evidence demands
  broader agreement). Action thresholds stay +/-1.0.
- decision.as_of (UTC ISO) set in enrich_crypto_decision; staleness handled by journal.

## Journal + risk gate  (owner: agent W5)

orchestration/journal.py (NEW):
- JSONL files under settings.resolve_cache_dir()/journal/: decisions.jsonl,
  cycles.jsonl, equity.jsonl. Append-only helpers + tolerant readers.
- record_decision(decision_dict, context) -> entry with id (uuid hex), ts, symbol,
  action, interval, conviction, entry/stop/target/leverage, cloid, swarm context
  (scout/news/ta verdict summaries as plain strings).
- reconcile(fills, decisions) -> per-decision outcome: filled?, exit fill, realized
  pnl, R-multiple, win/loss; uses cloid match first, falls back to symbol+time window.
- performance() -> {n_closed, hit_rate, avg_R, total_realized_pnl, by_symbol,
  open_positions_marked, equity_curve_tail}; consumes paper_trade account()/recent_fills().
orchestration/paper_trade.py:
- open_from_decision/open_manual gain cloid pass-through + journal append (lazy import;
  journaling failure never blocks the order) + RISK GATE before placing:
  correlated_exposure_check + max same-direction positions (default 3, env
  MCP_SWARM_MAX_POSITIONS) + daily-loss kill-switch (env MCP_SWARM_MAX_DAILY_LOSS_PCT
  default 5.0; compares today's realized+unrealized vs equity at UTC midnight from
  equity.jsonl; when breached -> refuse new risk-increasing orders with reason).

## MCP surface  (owner: ME)

New tools (mcp_server.py): market_pulse, orderflow(symbol), social_scan(symbol?),
news_feed(symbol?), swarm_goal_get/swarm_goal_set(goal), journal_performance(),
journal_recent(n). New prompt trade_swarm(goal?, interval_policy=auto):
the playbook — read goal+journal, spawn hype-scout (haiku) / news-reader (sonnet) /
chart-analyst (opus) subagents in parallel, fuse, pick timeframe AUTONOMOUSLY
(map signal horizon -> interval), decide, risk-gate, place via paper_trade_decision,
journal the cycle, report.

## Claude Code assets  (owner: ME)

.claude/agents/hype-scout.md (model: haiku — social_scan/market_pulse/paper_pairs only),
news-reader.md (model: sonnet — news_feed + WebSearch/WebFetch),
chart-analyst.md (model: opus — crypto_* read tools + orderflow; paper_* EXCLUDED for all
three), .claude/commands/trade-swarm.md, .claude/settings.json (allow
mcp__makecrazypenny__* read tools). Loop: /loop 15m /trade-swarm interactively, or
Task Scheduler headless claude -p later.

## Test plan  (each agent ships its own offline tests)

tests/test_hl_info.py (respx-mocked POST bodies per type), tests/test_binance_flow.py,
tests/test_social_news.py (ASCII sanitization asserted), tests/test_metrics_v2.py
(pure functions incl. edge cases), tests/test_journal.py (tmp cache dir; risk gate
refusal paths; kill-switch). Suite must stay green: .venv\Scripts\python.exe -m pytest -q.
