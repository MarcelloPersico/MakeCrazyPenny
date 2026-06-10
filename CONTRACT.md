# MakeCrazyPenny — Implementation Contract

This is the authoritative, self-contained build specification for **MakeCrazyPenny**,
derived from [`plan.md`](./plan.md). A builder should be able to implement the entire
system from this document without further context. Where the source spec was ambiguous,
the resolved decision is recorded inline and collected in
[§13 Resolved ambiguities](#13-resolved-ambiguities).

> **Not investment advice.** Every user-facing report this system produces is informational
> only and must carry the disclaimer baked into `core/disclaimer.py`.

---

## 1. Overview

MakeCrazyPenny is an agentic financial-analysis platform. A mother orchestrator agent
(Claude, via the Claude Agent SDK) plans an analysis, delegates to specialized sub-agents
(technical, sentiment, congressional-trade, expert-report), and synthesizes a single
cross-checked report. Every capability is exposed over MCP so any MCP-capable host can
drive it.

The system is built in **three layers** with a strict, acyclic dependency rule
(**Layer 2 → Layer 1 → Layer 0**, never upward, no sideways calls within Layer 1 except
`synthesis`):

| Layer | Package | Role |
|------|---------|------|
| 0 | `makecrazypenny/providers/` | Shared data-access. One client per external API behind a single `ProviderRegistry` that owns rate limiting, caching, retries, circuit breaking, and per-capability fallback chains. Knows nothing about agents or MCP. |
| 1 | `makecrazypenny/servers/` | Agent-agnostic capability MCP servers (technical, sentiment, congress, reports, synthesis, orchestration). Pure async logic + MCP wiring. Depend only on Layer 0 + core. |
| 2 | `makecrazypenny/orchestration/` | Claude Agent SDK reasoning. Sub-agent definitions + mother-orchestrator options + CLI entrypoint. |
| — | `makecrazypenny/core/` | Cross-cutting primitives: types, config, disclaimer, errors. Imported by all layers. |

Import name: **`makecrazypenny`**.

---

## 2. Global engineering mandates (apply everywhere)

These are non-negotiable and tested:

1. **Python 3.11+**. **async-first** for all I/O. **Type hints on every public function.**
2. **Import safety.** Providers and servers MUST be importable WITHOUT optional heavy
   libraries (`yfinance`, `pandas`, `ta`) or any API key present. Achieve this with:
   - **Lazy imports** — `import yfinance` / `import pandas` / `import ta` go *inside the
     function body*, never at module top.
   - **Graceful SDK shims** — see [`servers/_sdk.py`](#71-_sdkpy--graceful-sdk-shims).
   - **Importing a module must never hit the network** and never raise on a missing key.
3. **HTTP.** Use `httpx.AsyncClient` for our own REST providers (Alpha Vantage, Finnhub,
   FMP, EDGAR, Stock Watcher, Marketaux). `yfinance` is a *sync* library → wrap its calls
   in `asyncio.to_thread(...)`.
4. **MCP tool return shape.** Every MCP tool returns exactly:
   ```json
   {"content": [{"type": "text", "text": "<json-encoded string>"}]}
   ```
   Build it with the helper `makecrazypenny.servers._common.text_result(obj)`.
5. **Disclaimer.** Any output that reaches a user carries the not-investment-advice
   disclaimer from `core/disclaimer.py`.
6. **Dependency rule.** Servers depend ONLY on the provider registry + core. No server
   imports another server, **EXCEPT** `synthesis`, which may import the read-only *logic
   functions* of `technical` and `reports` (read-only composition; never their MCP wiring).

---

## 3. File layout

```
pyproject.toml            build + deps + dev extras + ruff + pytest config
README.md                 overview, install, run, architecture summary, disclaimer
.env.example              all API key names + MCP_CACHE_DIR (empty values)   [DONE]
CONTRACT.md               this contract                                       [DONE]
makecrazypenny/__init__.py                                                    [DONE]
makecrazypenny/core/__init__.py                                               [DONE]
makecrazypenny/core/types.py        dataclasses (see §5)
makecrazypenny/core/config.py       Settings + CAPABILITY_CHAINS default
makecrazypenny/core/disclaimer.py   DISCLAIMER: str; with_disclaimer(text)->str
makecrazypenny/core/errors.py       error taxonomy (see §6)
makecrazypenny/core/sectors.py      curated GICS sector -> constituents map + resolver (§10.5)
makecrazypenny/core/universe.py     live-fetched + cached S&P 500 universe (§10.5.1)
makecrazypenny/analysis/factors.py  factor signals (momentum/trend/value/quality) (§10.7.1)
makecrazypenny/analysis/risk.py     ATR stops + vol-target/fractional-Kelly sizing (§10.7.2)
makecrazypenny/analysis/regime.py   market-regime filter -> gross-exposure scalar (§10.7.3)
makecrazypenny/analysis/backtest.py walk-forward backtest + deflated Sharpe (§10.7.4)
makecrazypenny/providers/__init__.py  imports every provider module; get_registry()  [DONE]
makecrazypenny/providers/base.py       Provider ABC + @register_provider + PROVIDER_REGISTRY
makecrazypenny/providers/ratelimit.py  TokenBucket
makecrazypenny/providers/cache.py      TTLCache (L1 mem + L2 disk; single-flight)
makecrazypenny/providers/circuit.py    CircuitBreaker
makecrazypenny/providers/registry.py   ProviderRegistry (see §8)
makecrazypenny/providers/yfinance_provider.py
makecrazypenny/providers/alpha_vantage.py
makecrazypenny/providers/finnhub.py
makecrazypenny/providers/fmp.py
makecrazypenny/providers/edgar.py
makecrazypenny/providers/stockwatcher.py
makecrazypenny/providers/marketaux.py
makecrazypenny/servers/__init__.py                                            [DONE]
makecrazypenny/servers/_sdk.py        graceful SDK shims
makecrazypenny/servers/_common.py     text_result, json_default, normalize_symbol
makecrazypenny/servers/technical.py
makecrazypenny/servers/sentiment.py
makecrazypenny/servers/congress.py
makecrazypenny/servers/reports.py
makecrazypenny/servers/synthesis.py
makecrazypenny/servers/orchestration.py
makecrazypenny/mcp_server.py            host-driven FastMCP server: tools + debate prompts (§10.4)
makecrazypenny/orchestration/__init__.py                                      [DONE]
makecrazypenny/orchestration/agents.py  AgentDefinitions + build_options() (report mode)
makecrazypenny/orchestration/debate.py  deterministic decision engine (see §10.3)
makecrazypenny/orchestration/market.py  sector-wide scan engine (see §10.5)
makecrazypenny/orchestration/screen.py  whole-universe prefilter->deep-dive funnel (§10.5.1)
makecrazypenny/orchestration/portfolio.py  conviction x inverse-vol portfolio builder (§10.6)
makecrazypenny/orchestration/main.py    CLI entrypoint (decide | report | --sector | --market | --regime | --backtest)
tests/                                 mirrors source (see §11)
```

`[DONE]` marks files created in this scaffolding pass. **No logic `.py` modules are
implemented in this pass** — only `__init__.py`, `.env.example`, and this contract exist.
Everything else is to be built per this contract.

---

## 4. Capability names (FROZEN)

Servers call `registry.fetch(capability=<one of these>, **params)`. This vocabulary is the
contract between Layer 1 and Layer 0 and **must not change**:

```
ohlcv, quote, fundamentals,
company_news, news_sentiment, social_sentiment,
congress_trades, insider_transactions,
analyst_ratings, price_targets, upgrades_downgrades, sec_filings
```

---

## 5. Core types (`core/types.py`)

Plain `@dataclass`es. **Frozen where natural** (immutable value objects: `Provenance`,
`OHLCVBar`). Containers that may be assembled incrementally (`OHLCV`) need not be frozen.
**Every optional field defaults to `None`.** Every type provides a `to_dict()` returning a
JSON-serializable `dict`. Provide `from_provider(...)` / normalizer classmethods where a
provider's raw payload needs mapping into the type.

| Type | Fields |
|------|--------|
| `Provenance` | `provider: str`, `fetched_at: str` (ISO-8601 UTC), `cached: bool` |
| `OHLCVBar` | `ts: str`, `open: float`, `high: float`, `low: float`, `close: float`, `volume: float` |
| `OHLCV` | `symbol: str`, `interval: str`, `bars: list[OHLCVBar]`, `provenance: Provenance` |
| `Quote` | `symbol: str`, `price: float`, `change: float \| None`, `change_pct: float \| None`, `provenance: Provenance` |
| `NewsItem` | `symbol: str`, `headline: str`, `source: str \| None`, `url: str \| None`, `published_at: str \| None`, `summary: str \| None` |
| `SentimentScore` | `symbol: str`, `score: float` (−1..1), `label: str`, `n_articles: int`, `drivers: list[str]`, `provenance: Provenance` |
| `CongressTrade` | `symbol: str`, `member: str`, `chamber: str`, `transaction: str`, `amount_range: str \| None`, `transaction_date: str \| None`, `disclosure_date: str \| None`, `provenance: Provenance` |
| `InsiderTransaction` | `symbol: str`, `insider: str`, `role: str \| None`, `transaction: str`, `shares: float \| None`, `value: float \| None`, `date: str \| None`, `provenance: Provenance` |
| `AnalystRating` | `symbol: str`, `period: str`, `strong_buy: int`, `buy: int`, `hold: int`, `sell: int`, `strong_sell: int`, `provenance: Provenance` |
| `PriceTarget` | `symbol: str`, `mean: float \| None`, `high: float \| None`, `low: float \| None`, `current: float \| None`, `provenance: Provenance` |
| `UpgradeDowngrade` | `symbol: str`, `firm: str`, `from_grade: str \| None`, `to_grade: str \| None`, `action: str`, `date: str \| None`, `provenance: Provenance` |
| `Filing` | `symbol: str`, `form: str`, `title: str \| None`, `filed_at: str \| None`, `url: str \| None`, `provenance: Provenance` |

**`to_dict()` rule:** nested dataclasses (e.g. `provenance`, each `OHLCVBar`) are recursively
converted to dicts so the result is directly JSON-encodable. The `score` field of
`SentimentScore` is clamped to `[-1.0, 1.0]` by the normalizer.

### 5.1 Decision types (decision layer, see §10.3)

The debate-driven decision engine adds three value types (same conventions: plain
`@dataclass`, `to_dict()`, optional fields default sensibly):

| Type | Fields |
|------|--------|
| `DebateArgument` | `side: str` (`"bull"`/`"bear"`), `round: int`, `thesis: str`, `key_points: list[str]`, `cited_evidence: list[str]`, `conviction: float \| None` (0..1), `rebuts: list[str]` |
| `DebateTranscript` | `symbol: str`, `rounds: int`, `arguments: list[DebateArgument]`; helpers `for_side(side)`, `latest(side)` |
| `TradeDecision` | `symbol`, `action` (`"BUY"`/`"SHORT"`/`"AVOID"`), `direction` (`"LONG"`/`"SHORT"`/`"FLAT"`), `conviction: float` (0..1), `horizon`, `suggested_sizing`, `summary`, `rationale: list[str]`, `bull_case: list[str]`, `bear_case: list[str]`, `risks: list[str]`, `invalidation: str \| None`, `net_score`, `bull_score`, `bear_score`, `factors: list[dict]`, `method` (`"quant"`/`"debate"`), `data_quality: dict`, `sizing: dict` (stop/target/position % — §10.7.2), `regime: dict` (market regime — §10.7.3), `transcript: DebateTranscript \| None`, `note: str \| None`, `disclaimer: str` |
| `SectorScan` | `sector`, `stance` (`"overweight"`/`"underweight"`/`"neutral"`), `n_requested`, `n_analyzed`, `net_tilt: float`, `avg_conviction: float`, `breadth: dict` (buy/short/avoid + bullish_pct/bearish_pct), `rankings: list[dict]`, `top_longs: list[dict]`, `top_shorts: list[dict]`, `errors: list[dict]`, `method`, `summary`, `disclaimer` |
| `MarketScreen` | `universe`, `universe_source` (`"live"`/`"cache"`/`"fallback"`/`"explicit"`), `universe_count`, `as_of`, `n_prefiltered`, `n_evaluated`, `regime: dict`, `top_longs: list[dict]` (full `TradeDecision`s), `top_shorts: list[dict]`, `long_shortlist: list[dict]`, `short_shortlist: list[dict]`, `errors: list[dict]`, `method`, `summary`, `disclaimer` |

`TradeDecision` always carries the not-investment-advice `disclaimer` and the quant
`factors` breakdown for transparency, even when an LLM judge sets the final call.

---

## 6. Error taxonomy (`core/errors.py`)

```python
class ProviderError(Exception): ...        # base for all provider-layer errors
class RateLimited(ProviderError): ...       # token bucket exhausted / upstream 429
class CircuitOpen(ProviderError): ...        # provider circuit is open
class AllProvidersFailed(ProviderError): ... # every provider in a chain failed/skipped
class MissingApiKey(ProviderError): ...      # required API key absent from Settings
```

`AllProvidersFailed.__init__` takes the capability name and stores it (`self.capability`)
so callers/agents can report which capability could not be served. `MissingApiKey` should
carry the provider name and the env var name in its message.

---

## 7. Servers support helpers

### 7.1 `_sdk.py` — graceful SDK shims

Attempt to import from `claude_agent_sdk`; on `ImportError`, provide no-op fallbacks so every
server module imports and its logic functions remain testable without the SDK.

```python
try:
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient,
        ClaudeAgentOptions, AgentDefinition,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

    def tool(name, description, input_schema):
        """No-op decorator: returns the function unchanged, tagging metadata."""
        def deco(fn):
            fn.__mcp_tool__ = {"name": name, "description": description, "schema": input_schema}
            return fn
        return deco

    def create_sdk_mcp_server(*, name, version="0.1.0", tools=()):
        """Return a lightweight descriptor object instead of a real server."""
        return {"name": name, "version": version, "tools": list(tools), "_stub": True}

    class ClaudeSDKClient:  # minimal stub; methods raise a clear error if used
        ...
    class ClaudeAgentOptions:  # accept and store kwargs
        ...
    class AgentDefinition:     # accept and store kwargs
        ...
```

- Expose `SDK_AVAILABLE: bool`.
- The real and shim `tool` decorators must both be usable as `@tool(name, desc, schema)`
  and must leave the wrapped **async logic function directly callable** (so tests can call
  the logic without going through MCP). **Decision:** the `@tool` wrapper preserves a
  reference to the raw async function (e.g. via `fn.__wrapped__` or by registering the
  decorated object's `.handler`), so server logic is always unit-testable.

### 7.2 `_common.py`

```python
def text_result(obj: Any) -> dict:
    """Wrap any JSON-serializable object as an MCP text-content result."""
    return {"content": [{"type": "text", "text": json.dumps(obj, default=json_default)}]}

def json_default(o: Any) -> Any:
    """json.dumps default: handle dataclasses (.to_dict), datetimes (.isoformat), sets→list."""

def normalize_symbol(symbol: str) -> str:
    """Uppercase, strip whitespace, strip a leading '$'. e.g. ' $aapl ' -> 'AAPL'."""
```

---

## 8. Layer 0 — providers

### 8.1 `base.py` — Provider ABC, registry list, decorator

```python
PROVIDER_REGISTRY: list[type[Provider]] = []   # auto-registration target

def register_provider(cls):                     # class decorator
    PROVIDER_REGISTRY.append(cls)
    return cls

class Provider(ABC):
    name: str                         # e.g. "finnhub"
    supported: set[str]               # subset of the FROZEN capability names
    rate_key: str                     # token-bucket key (default == name)
    rate_per_min: int                 # used to build the bucket; 0/None => effectively unlimited
    cost: int = 1                     # tokens consumed per fetch
    requires_key: str | None = None   # env attribute name on Settings, or None

    def __init__(self, settings: "Settings") -> None: ...

    @abstractmethod
    async def fetch(self, capability: str, **params) -> Any:
        """Return a normalized core type (or list thereof) for the capability.
        MUST raise MissingApiKey if requires_key is set but absent.
        MUST raise NotImplementedError if capability not in self.supported."""
```

`rate_key` lets distinct providers share a bucket if they share a key (not needed by the
current set, but the registry keys buckets by `rate_key`). Default `rate_key == name`.

### 8.2 `ratelimit.py` — `TokenBucket`

```python
class TokenBucket:
    def __init__(self, rate_per_min: int, capacity: int | None = None): ...
    async def acquire(self, cost: int = 1) -> None:
        """Block (async sleep) until `cost` tokens are available, then consume them.
        Refills continuously at rate_per_min/60 tokens per second up to capacity.
        rate_per_min <= 0 means unlimited (acquire returns immediately)."""
```

- Capacity defaults to `rate_per_min` (one minute of burst).
- Use a monotonic clock and an `asyncio.Lock` to keep refill/consume atomic.
- **Decision:** `acquire` *waits* (does not raise). A typed `RateLimited` is raised by the
  registry only if a configured max wait is exceeded; default behavior is to wait.

### 8.3 `cache.py` — `TTLCache`

In-memory **L1** + on-disk **L2** JSON cache under the configured cache dir, with async
**single-flight**.

```python
class TTLCache:
    def __init__(self, cache_dir: str | os.PathLike, *, l2_enabled: bool = True): ...
    async def get_or_fetch(self, key, ttl: float, factory: Callable[[], Awaitable[Any]]) -> CacheResult:
        """Return cached value if fresh; else call factory() exactly once even under
        concurrent identical keys (single-flight via per-key asyncio.Lock/Future),
        store with expiry = now + ttl, mirror to L2 JSON, and return it.
        CacheResult carries `.value` and `.cached: bool`."""
```

- **Key** is the tuple `(provider_name, capability, sorted(params.items()))`; serialize to a
  stable string (e.g. `json.dumps(..., sort_keys=True)`) and hash for the L2 filename.
- **Decision:** `get_or_fetch` returns a small `CacheResult(value, cached)` (or `(value,
  cached)` tuple) so the registry can report whether the value was served from cache. Cache
  *hit* ⇒ `cached=True`; freshly fetched ⇒ `cached=False`.
- L2 stores `{"expires_at": <epoch>, "value": <json>}`. Corrupt/expired L2 entries are
  ignored. L2 failures (disk errors) degrade silently to L1-only — never raise to caller.
- Single-flight: keep a dict of in-flight `asyncio.Future`s keyed by the serialized key;
  concurrent identical requests await the same future.

### 8.4 `circuit.py` — `CircuitBreaker`

```python
class CircuitBreaker:
    def __init__(self, fail_threshold: int = 5, cooldown_s: float = 60.0): ...
    def allow(self) -> bool:          # True if closed, or half-open trial permitted
    def record_success(self) -> None: # reset to closed
    def record_failure(self) -> None: # increment; open when >= fail_threshold
```

States: **closed** (normal), **open** (skip until cooldown elapses), **half-open** (after
cooldown, allow one trial; success → closed, failure → open again). `allow()` transitions
open→half-open when `cooldown_s` has passed since opening.

### 8.5 `registry.py` — `ProviderRegistry`

```python
class ProviderRegistry:
    def __init__(self, settings: Settings):
        # Instantiate providers, build:
        #   self._providers: dict[str, Provider]              (by provider.name)
        #   self._buckets:   dict[str, TokenBucket]           (by provider.rate_key)
        #   self._circuits:  dict[str, CircuitBreaker]        (by provider.name)
        #   self._cache:     TTLCache(settings.cache_dir)
        ...

    def register(self, provider: Provider) -> None: ...

    @classmethod
    def default(cls) -> "ProviderRegistry":
        """Build from Settings.from_env() and auto-registered PROVIDER_REGISTRY classes."""

    async def fetch(self, capability: str, *, ttl: float | None = None, **params) -> dict:
        chain = self.settings.CAPABILITY_CHAINS[capability]
        for name in chain:
            provider = self._providers.get(name)
            if provider is None or capability not in provider.supported:
                continue
            if not self._circuits[name].allow():
                continue
            try:
                await self._buckets[provider.rate_key].acquire(provider.cost)
                res = await self._cache.get_or_fetch(
                    key=(name, capability, params),
                    ttl=ttl or default_ttl(capability),
                    factory=lambda p=provider: p.fetch(capability, **params),
                )
                self._circuits[name].record_success()
                return {"provider": name, "data": res.value, "cached": res.cached}
            except (MissingApiKey, NotImplementedError):
                continue                     # fall through, do NOT trip the breaker
            except Exception:
                self._circuits[name].record_failure()
                continue
        raise AllProvidersFailed(capability)
```

- The returned `data` is the provider's normalized result (a core type's `to_dict()` output,
  or a list of them) — **already JSON-serializable**, so servers can hand it straight to
  `text_result`.
- **`default_ttl(capability)`** (module-level function, seconds):

  | capability | TTL |
  |-----------|-----|
  | `quote` | 15 |
  | `ohlcv` | 300 |
  | `company_news`, `news_sentiment`, `social_sentiment` | 600 |
  | `congress_trades`, `insider_transactions`, `sec_filings` | 3600 |
  | `analyst_ratings`, `price_targets`, `upgrades_downgrades` | 3600 |
  | `fundamentals` | 3600 |

- **Single-flight** is implemented in the cache layer (§8.3); the registry relies on it so
  identical concurrent `(name, capability, params)` collapse to one upstream call.
- **Decision:** `MissingApiKey` and `NotImplementedError` cause a *silent skip* (the chain
  continues) and **do not** record a circuit failure — they are configuration/support facts,
  not provider health failures. Only genuine runtime exceptions trip the breaker.

### 8.6 `providers/__init__.py` behavior

On import: import every provider module (`yfinance_provider`, `alpha_vantage`, `finnhub`,
`fmp`, `edgar`, `stockwatcher`, `marketaux`) so each `@register_provider` runs and populates
`PROVIDER_REGISTRY`. Expose `get_registry() -> ProviderRegistry` returning a lazily-built
process-wide singleton (`ProviderRegistry.default()`).

### 8.7 Provider capability support matrix

Each provider sets `supported = set(...)`, `name`, `rate_per_min`, and `requires_key`.

| Provider (`name`) | `supported` capabilities | Key (`requires_key` → env) | Rate | Notes |
|---|---|---|---|---|
| `yfinance` | `ohlcv`, `quote`, `fundamentals` | none | n/a (be polite) | `yfinance` lib via `asyncio.to_thread`; lazy-import `yfinance`+`pandas` |
| `alpha_vantage` | `ohlcv`, `quote`, `news_sentiment`, `fundamentals` | `ALPHA_VANTAGE_API_KEY` | 5/min (500/day) | `httpx`; daily cap noted in §13 |
| `finnhub` | `ohlcv`, `quote`, `company_news`, `news_sentiment`, `social_sentiment`, `congress_trades`, `insider_transactions`, `analyst_ratings`, `price_targets`, `upgrades_downgrades` | `FINNHUB_API_KEY` | 60/min | `httpx` |
| `fmp` | `congress_trades`, `analyst_ratings`, `price_targets`, `upgrades_downgrades`, `fundamentals` | `FMP_API_KEY` | (free tier) | `httpx` |
| `edgar` | `sec_filings`, `insider_transactions` | none | be polite | SEC EDGAR; **MUST send a descriptive `User-Agent`** (e.g. `"MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"`) |
| `stockwatcher` | `congress_trades` | none | be polite | House/Senate Stock Watcher bulk JSON on GitHub |
| `marketaux` | `company_news` | `MARKETAUX_API_KEY` | (free tier) | `httpx` |

Behavioral contract for every provider:
- Missing required key ⇒ raise `MissingApiKey` from `fetch()` (registry falls through).
- Unsupported capability ⇒ raise `NotImplementedError` (registry skips).
- `fetch()` normalizes the raw payload into the corresponding core type(s) and returns
  `to_dict()` output. Set `provenance.cached=False` at fetch time (the registry/cache reports
  the true cached status separately in its envelope).

---

## 9. Layer 1 — capability servers

### 9.1 Per-server pattern (apply to every server module)

1. **Pure async logic functions** — `async def <tool>(<typed params>) -> dict`. Each calls
   `get_registry().fetch(...)` and shapes a compact result dict. These are importable and
   unit-testable with a mocked/injected registry.
2. **Module-level `get_registry()`** — a thin indirection (re-exported from
   `providers.get_registry`) that tests can monkeypatch to inject a fake registry.
3. **MCP wiring** — `from ._sdk import tool, create_sdk_mcp_server`; wrap each logic fn with
   `@tool(name, description, schema)`; build
   `server = create_sdk_mcp_server(name=<server>, version="0.1.0", tools=[...])`.
4. **stdio guard** — `if __name__ == "__main__":` run the stdio server; if the real SDK is
   missing, print a clear message and exit non-zero.

**Decision (logic vs. tool separation):** define each tool as a plain `async def` logic
function first, then apply the `@tool` decorator to a thin wrapper *or* keep the decorated
object's `.handler` pointing at the logic fn. Either way the raw logic function stays
directly importable and callable for tests (the `@tool` shim/real decorator must not hide
it). Tool input schemas use the SDK's simple `{ "param": type }` form.

### 9.2 Tool inventory

**`technical`** — uses the pure-Python `ta` lib + `pandas` (lazy-imported) for indicators.
- `get_ohlcv(symbol, interval="1d", period="6mo") -> dict`
- `compute_indicators(symbol, indicators=["rsi","macd","bbands","sma","ema","atr","stoch","adx","obv"]) -> dict`
- `detect_signals(symbol) -> dict` — golden/death cross, RSI extremes, MACD cross, BB breaks.
- `support_resistance(symbol) -> dict`
- `multi_timeframe_summary(symbol) -> dict`

**`sentiment`**
- `get_news(symbol, days=7) -> dict`
- `news_sentiment(symbol) -> dict`
- `social_sentiment(symbol) -> dict`
- `aggregate_sentiment(symbol) -> dict` — blended score + drivers.

**`congress`** — note disclosure lag (often 30–45 days) in output.
- `congress_trades(symbol_or_member, since=None) -> dict`
- `recent_congress_activity(days=7) -> dict`
- `insider_transactions(symbol) -> dict`
- `new_disclosures(watchlist, since) -> dict` — alert feed.

**`reports`**
- `analyst_ratings(symbol) -> dict`
- `price_targets(symbol) -> dict`
- `upgrades_downgrades(symbol, since=None) -> dict`
- `sec_filings(symbol, forms=["10-K","10-Q","8-K"]) -> dict`

**`synthesis`** — the ONLY server permitted to consume multiple capabilities.
- `cross_check(symbol) -> dict` — reconcile analyst consensus vs. price/technicals vs.
  fundamentals; flag divergences (e.g. consensus *Buy* but price below all MAs and margins
  compressing). May import `technical` and `reports` **logic functions** and call them
  (read-only), plus `registry.fetch` for `fundamentals`. Must NOT import another server's
  MCP wiring, and must not be imported by `technical`/`reports` (keeps the graph acyclic).

**`orchestration`** — recursion + alerts.
- `spawn_analyst(role, task, context=None, model=None, depth=0) -> dict` — bounded recursive
  nested `ClaudeSDKClient`. **HARD guards:** `max_depth` default **3**, `max_budget_usd`
  default **1.0**; refuse beyond guards (return a clear error dict, do not raise to crash the
  caller). If the SDK is missing, return a clear stub error dict (`{"error": "...", "sdk":
  false}`). Guard values are read from `Settings` (overridable).
- `register_alert(watchlist, kinds) -> dict` — persist alert config under the cache dir.
- `check_alerts() -> dict` — congress + report deltas since last run; emit to
  console/file/webhook sinks; persist state (last-seen disclosures/ratings) under the cache
  dir so deltas survive restarts. Delta detection reads through the Layer-0 cache so a sweep
  across a large watchlist does not blow the rate budget.

All tool logic functions return plain dicts (shaped via core types' `to_dict()` and small
summary fields). MCP wrappers pass them through `text_result(...)`.

---

## 10. Layer 2 — orchestration

### 10.1 `agents.py`

Build `AgentDefinition`s (guard SDK import via `_sdk` style try/except so the module imports
without the SDK):

| Agent | Model | Tools |
|---|---|---|
| `technical-analyst` | `claude-sonnet-4-6` | `mcp__technical__*` |
| `sentiment-analyst` | `claude-haiku-4-5` | `mcp__sentiment__*`, `WebSearch`, `WebFetch` |
| `congress-tracker` | `claude-haiku-4-5` | `mcp__congress__*` |
| `report-checker` | `claude-sonnet-4-6` | `mcp__reports__*`, `mcp__synthesis__cross_check`, `mcp__orchestration__spawn_analyst` |

`build_options() -> ClaudeAgentOptions` with:
- `model="claude-opus-4-8"`
- `mcp_servers={...}` — all **6** servers (technical, sentiment, congress, reports,
  synthesis, orchestration).
- `allowed_tools=["WebSearch", "WebFetch", "Agent", "mcp__technical__*",
  "mcp__sentiment__*", "mcp__congress__*", "mcp__reports__*",
  "mcp__synthesis__cross_check", "mcp__orchestration__spawn_analyst"]`
- `agents={...}` — the four definitions above.

If `SDK_AVAILABLE` is `False`, `build_options()` may return a plain descriptor / raise a
clear error only when actually invoked — but **importing `agents.py` must not fail.**

### 10.2 `main.py`

`argparse` CLI: `python -m makecrazypenny.orchestration.main SYMBOL [--mode decide|report]
[--depth N]`.
- **`--mode decide` (default):** runs the deterministic decision engine (§10.3) and prints a
  `TradeDecision` (BUY/SHORT/AVOID + conviction, cases, risks, invalidation) with the
  disclaimer. **AI-free — no SDK or API key required**, always exits 0. Output includes a tip
  to run the full bull/bear debate via the MCP server (§10.4).
- **`--mode report`:** runs the legacy mother orchestrator on `SYMBOL` (still SDK-backed) and
  prints the cross-checked report. If the SDK is not installed, print install instructions and
  exit **non-zero** gracefully (no traceback).
- CLI output is strictly **ASCII** so it prints on any console.

### 10.3 `debate.py` — deterministic decision engine

The **pure, AI-free** core that turns evidence into a `TradeDecision`. It never calls a model
and never needs an API key; the bull-vs-bear *debate* that can override it is run by an MCP
**host** via §10.4. Fully offline-testable.

```
gather_evidence(symbol)        # fan out across ALL capability logic fns (tolerant)
  → score_evidence(dossier)    # deterministic quant backbone (weighted factors)
    → decide_from_scores(...)  # quant decision, optionally merged with a host verdict
        → decide(symbol)        # top-level convenience (method="quant")
```

- **`gather_evidence(symbol, *, settings=None) -> dict`** — concurrent fan-out across the
  `technical`/`sentiment`/`congress`/`reports`/`synthesis` **logic functions**; one failure
  becomes an `{"_error": ...}` marker, never aborts the sweep.
- **`score_evidence(dossier) -> dict`** — pure. Weights technical signals (golden/death cross
  strongest), blended sentiment, analyst-consensus tilt, price-target upside, and
  congressional/insider net flow into `factors` + `net_score`/`bull_score`/`bear_score`, plus a
  cross-check `divergence_penalty`. Positive = bullish. The two analyst signals are **de-meaned
  against their structural optimism skew** (consensus tilt vs a +0.30 baseline; target upside vs a
  +10% baseline) so an ordinary stock scores ~0 on both, and flow is **dollar-size-weighted**
  (insider `value` / congress `amount_range` midpoint; unit weight when unknown). Price factors are
  computed on **split/dividend-adjusted** bars (yfinance `auto_adjust=True`).
- **`decide_from_scores(symbol, scored, *, transcript=None, verdict=None, method="quant",
  note=None) -> TradeDecision`** — pure. Quant rule: take a position only when net passes the
  threshold, conviction passes the floor, **and** the evidence is corroborated (≥2 categories or
  a strongly stacked single category) — else `AVOID`. When the host hands back a structured
  `verdict` (via the `finalize_decision` MCP tool), its validated fields override the
  human-facing call while the quant scores/factors are preserved.
- **`decide(symbol, *, settings=None) -> TradeDecision`** — gather → score → decide
  (`method="quant"`). Always returns a real decision carrying the disclaimer.

**Dependency direction.** `debate.py` is Layer 2: it imports `core` and the server **logic
functions** (read-only) only. Never imported by a Layer-1 server (keeps the graph acyclic).

### 10.4 `mcp_server.py` — host-driven MCP server (primary surface)

A standalone **FastMCP stdio server** (built on the `mcp` package) that an MCP host (Claude
Desktop / Claude Code) mounts. The host's own model — the user's subscription — runs the
bull-vs-bear debate and the judgment; **no Anthropic API key, nothing billed per token.** Run
via the `makecrazypenny-mcp` console script (or `python -m makecrazypenny.mcp_server`).

- **Tools (deterministic, AI-free):** `decide` (quant baseline), `gather_evidence` (dossier +
  quant), `technical_analysis`, `sentiment_analysis`, `congress_activity`, `analyst_reports`,
  `cross_check`, and `finalize_decision(symbol, action, …)` which merges the host's debated
  verdict with the quant backbone into the canonical `TradeDecision`. Each returns a JSON
  string and never calls a model.
- **Prompts (run by the host's model):** `decide` (orchestrates evidence → bull → bear →
  rebuttals → judge → `finalize_decision`, suggesting host sub-agents for genuine adversarial
  separation), plus `bull_case` / `bear_case` / `judge` personas for stepwise use. Symbols are
  normalized; every prompt ends with the not-investment-advice reminder.

Import-safe (no network at import); tools fetch lazily. Logs go to stderr so stdout stays a
clean JSON-RPC stream.

### 10.5 `market.py` + `core/sectors.py` — sector-wide scan

Extends the single-ticker engine to **a broad slice of the market**. `core/sectors.py` is a
curated, deterministic map of the **eleven GICS sectors** to representative liquid constituents
(`SECTORS`), with a tolerant `resolve_sector(name)` (aliases, case, unique-substring) and
`sector_constituents(name)` — pure stdlib, offline, no key (a future revision could back it
with live ETF-holdings data behind the same interface).

`orchestration/market.py` runs the deterministic decision engine (§10.3) on each constituent
and aggregates:

- **`scan_sector(sector, *, limit=None, top_n=5, settings=None) -> SectorScan`** — resolves the
  sector, analyses constituents concurrently under a bounded semaphore
  (`MAX_CONCURRENCY=5`, so a wide scan stays within rate budgets), and aggregates. Each ticker
  is independent — one failure becomes an `errors` entry, never aborting the sweep. An unknown
  sector returns an empty scan whose `errors` explains why (never raises).
- **`aggregate_scan(sector, decisions, errors, *, n_requested, top_n=5) -> SectorScan`** — pure.
  Computes `net_tilt` (mean net score = sector momentum), breadth (BUY/SHORT/AVOID +
  bullish/bearish %), `avg_conviction`, the full `rankings` (most→least bullish), and the top
  long/short ideas. Derives a sector **`stance`**: *overweight* (net_tilt ≥ 1 and ≥40% bullish),
  *underweight* (net_tilt ≤ −1 and ≥40% bearish), else *neutral*.

**MCP surface** (in `mcp_server.py`): tools `list_sectors`, `sector_constituents`,
`scan_sector` (deterministic), and the `decide_sector` prompt — the host scans the sector, then
debates the top long/short candidates and synthesizes a sector playbook (stance + ranked ideas
+ risks). **CLI:** `makecrazypenny --sector tech [--limit N] [--top N]` prints the scan.

AI-free and offline-testable; the AI debate over a scan is run by the host (the user's
subscription), like the single-ticker flow.

#### 10.5.1 `screen.py` — whole-universe screen (the S&P 500 funnel)

`core/universe.py` supplies the universe: **`fetch_sp500(*, settings=None, force_refresh=False)
-> dict`** live-fetches the current S&P 500 constituents from a maintained, key-free CSV,
normalizes symbols to the yfinance convention (`BRK.B` → `BRK-B`), and caches them under the
cache dir with a weekly TTL. Resolution order is **live → (stale) cache → curated-sector
fallback**, and the result is tagged with its `source` so callers know how fresh it is. Never
raises; the blocking HTTP/disk work runs via `asyncio.to_thread`.

`orchestration/screen.py` runs a two-stage **funnel** (running the full evidence engine on 500
names per call is neither fast nor free):

- **Stage 1 — prefilter (whole universe, cheap).** `prefilter_universe(symbols)` computes
  price-only factors from a single free OHLCV pull per name (no `.info`, no key) and scores each
  with `prefilter_score` (a momentum/trend/52-week-high composite mirroring the §10.3 factor
  weights). Bounded by `MAX_PREFILTER_CONCURRENCY=8`; one bad name becomes an `errors` entry.
- **Stage 2 — deep dive (shortlist only).** The strongest `shortlist` long candidates and
  `shortlist` short candidates are deep-dived with the full `decide` engine (evidence + regime +
  ATR sizing) under `MAX_DEEP_CONCURRENCY=5`. The best `top_n` BUY and `top_n` SHORT *verdicts*
  are surfaced as complete `TradeDecision`s — so the result says both **what** and **how** to
  trade (entry/stop/target, size, invalidation).

**`screen_market(*, symbols=None, shortlist=15, top_n=3, force_refresh=False, settings=None) ->
MarketScreen`** ties it together (universe → regime once → prefilter → deep dive → aggregate);
an explicit `symbols` list overrides the fetched universe. Never raises — data/fetch failures
are captured under `errors`.

**MCP surface** (in `mcp_server.py`): tool `screen_market` (deterministic) and the
`decide_market` prompt — the host screens the universe, then debates the long/short finalists
and lays out each plan. **CLI:** `makecrazypenny --market [--shortlist N] [--top N]` prints the
screen. AI-free and offline-testable (monkeypatch the two fetch points + the universe fetch);
the debate over the screen is run by the host.

### 10.6 `portfolio.py` — portfolio construction

`build_portfolio(symbols, *, max_positions, max_weight, regime=None) -> dict` runs the engine on
each name, keeps the BUY/SHORT verdicts, and weights each side by **conviction × inverse-volatility**.
Per-name caps are enforced by *iterative* clamp-and-redistribute (a single clamp+renormalize can
push a clamped name back over the cap); the cap auto-relaxes to ≥ equal-weight when names are too
few to fill the side. Gross exposure is scaled by the **market regime** (§10.7.3).
`build_sector_portfolio(sector, ...)` is the sector convenience wrapper. Returns longs/shorts with
weights, gross/net exposure, the regime, errors, and the disclaimer. AI-free; bounded concurrency.
**MCP:** `build_portfolio`, `build_sector_portfolio`.

### 10.7 `analysis/` — quantitative primitives

Pure cores (operate on plain bars/dicts, unit-testable offline) + thin async fetchers (pull data
through the Layer-0 cached registry). Research basis + ranked shortlist: **plan.md §10**.

- **`factors.py` (§10.7.1)** — `factor_values(bars, fundamentals)` → momentum 12-1, 52-week-high
  proximity, trend vs 200-DMA, realized vol (from OHLCV), plus value (E/P, B/P, FCF yield) and
  quality (gross profitability, ROE, margins) when free fundamentals are present; also `last_close`
  + `atr14` for sizing. `compute_factors(symbol)` fetches and computes. Folded into
  `debate.score_evidence` as new factor categories.
- **`risk.py` (§10.7.2)** — `atr`, `kelly_fraction_from_conviction` (half-Kelly), and
  `position_sizing(...)` → stop/target (ATR), vol-target weight, the conservative min position %
  (capped + regime-scaled), R-multiple. Attached to `TradeDecision.sizing`.
- **`regime.py` (§10.7.3)** — `regime_from_bars` / `market_regime(benchmark="SPY")` → risk-on /
  caution / risk-off + a 0..1 gross-exposure scalar (200-DMA trend, 12-1 TS momentum, vol overlay).
  Attached to `TradeDecision.regime`; scales sizing + portfolios. **MCP:** `market_regime`.
- **`backtest.py` (§10.7.4)** — `backtest_long_flat(bars)` walk-forward trend+momentum long/flat,
  net of costs → CAGR/Sharpe/maxDD/hit-rate/exposure vs buy-and-hold, plus
  `probabilistic_sharpe_ratio` and `deflated_sharpe_ratio` (Bailey & López de Prado) to discount for
  sample length, non-normality, and trials. Only price/factor signals (free history); others
  excluded to avoid look-ahead. **MCP:** `backtest`.

The decision engine (`debate.decide` / `enrich_decision`) folds factor scores into the verdict and
attaches `sizing` + `regime` to every `TradeDecision`.

---

## 11. Tests (`tests/`, pytest + pytest-asyncio, `asyncio_mode=auto`, NO network)

Mirror the source tree. All tests deterministic and offline.

| Test file | Asserts |
|---|---|
| `test_ratelimit.py` | TokenBucket refill/consume, waiting behavior, unlimited mode (pure logic). |
| `test_cache.py` | Fresh-vs-expired, `cached` flag, single-flight (one factory call under N concurrent identical keys), L2 round-trip, corrupt-L2 tolerance. |
| `test_circuit.py` | closed→open after threshold, open skips, half-open trial, recovery (pure logic). |
| `test_registry.py` | fake providers: **fallback order**, **circuit-open skip**, **missing-key fall-through**, **single-flight** (one upstream call for concurrent identical), **`AllProvidersFailed`**. |
| `test_providers_*.py` | mock `httpx` via `respx` (or monkeypatch) and `yfinance` via monkeypatch; assert normalization to core types + `MissingApiKey` behavior. |
| `test_servers_*.py` | monkeypatch each server's `get_registry()` to a fake returning canned data; assert tool logic shapes the correct content dict; technical indicators computed on a synthetic OHLCV frame. |
| `test_imports.py` | importing every module in `makecrazypenny.*` succeeds with no keys and (ideally) without optional libs present. |
| `test_debate.py` | quant backbone scores bullish/bearish/thin dossiers correctly; `decide_from_scores` maps to BUY/SHORT/AVOID and a host verdict overrides; `gather_evidence` tolerates failures; `decide` is deterministic `method="quant"`; CLI output is ASCII. |
| `test_mcp_server.py` | FastMCP tools + prompts registered (incl. sector tools + `decide_sector`); prompt builders normalize the symbol and embed the bull/bear/judge flow; `decide`/`gather_evidence`/`finalize_decision` tools return correct JSON (verdict overrides quant); sector tools resolve + scan; per-domain tools tolerate a single failure. All AI-free/offline. |
| `test_market.py` | sector resolver (aliases/case/substring/unknown); `aggregate_scan` ranks, classifies breadth, and derives overweight/underweight/neutral stance; `scan_sector` analyses constituents (evidence monkeypatched), respects `limit`, tolerates a failed name, and errors cleanly on an unknown sector. |
| `test_universe.py` | symbol normalization (`BRK.B`→`BRK-B`); CSV parse dedups + skips blanks; `fetch_sp500` resolution order live→cache→fallback, `force_refresh` bypasses cache, stale cache beats a failed live fetch (fetch + cache dir monkeypatched). |
| `test_screen.py` | `prefilter_score` direction/composition; `prefilter_universe` ranks + collects errors; `screen_market` selects top-N longs/shorts from the deep-dive verdicts, caps each side, limits the deep dive to the shortlist, uses the live universe, tolerates a deep-dive failure, and handles an empty universe (both fetch points monkeypatched). |
| `test_analysis.py` | factor signals (momentum/trend/52w-high/vol; value/quality extraction) on synthetic bars; ATR + half-Kelly + `position_sizing` (stops, regime scaling, FLAT=0); regime risk-on/off/caution; backtest long/flat runs + PSR/DSR monotonicity + norm CDF/PPF sanity. All pure/offline. |
| `test_portfolio.py` | `_weight_side` caps + normalizes + inverse-vol tilt; `build_portfolio` weights, regime-scaled exposure (evidence/regime monkeypatched); unknown sector errors cleanly. |

---

## 12. Dependencies & tooling (`pyproject.toml`)

- **Runtime:** `claude-agent-sdk`, `yfinance`, `pandas`, `ta`, `httpx`, `python-dotenv`.
- **Dev extra:** `pytest`, `pytest-asyncio`, `respx`, `ruff`.
- **ruff:** line-length **100**, sane defaults.
- **pytest:** `asyncio_mode=auto`, `testpaths=tests`.
- Build backend: a standard PEP 621 `[project]` table; `requires-python = ">=3.11"`.

> **Note (import-safety vs. declared deps):** although `yfinance`/`pandas`/`ta`/`claude-agent-sdk`
> are declared runtime deps, the code must still import safely if they are absent (lazy imports
> + SDK shims), because `test_imports.py` and CI may run in a minimal environment.

---

## 13. Resolved ambiguities

Decisions made where the source spec was underspecified:

1. **`get_or_fetch` return type.** Spec implies a value but the registry needs the
   `cached` flag. **Resolved:** `get_or_fetch` returns `CacheResult(value, cached)`; the
   registry's envelope `cached` field comes from it.
2. **Rate-limit on exhaustion.** **Resolved:** `TokenBucket.acquire` *waits* (async) rather
   than raising; `RateLimited` is reserved for a configurable max-wait timeout and upstream
   429s. Default config never times out (waits).
3. **`MissingApiKey` / `NotImplementedError` vs. circuit breaker.** **Resolved:** these are
   skips, not failures — they do **not** trip the breaker; only genuine runtime exceptions do.
4. **`@tool` and testability.** **Resolved:** the `@tool` decorator (real and shim) keeps the
   underlying async logic function directly importable/callable so tests bypass MCP. Logic
   lives in a plain `async def`; the decorator wraps a thin adapter.
5. **Provider `data` is pre-serialized.** **Resolved:** providers return `to_dict()` output
   (JSON-ready); servers pass `registry.fetch(...)["data"]` straight into `text_result`.
6. **`rate_key` vs `name`.** **Resolved:** buckets are keyed by `provider.rate_key`, which
   defaults to `provider.name`; this allows future key-sharing without changing the registry.
7. **EDGAR User-Agent.** SEC requires a descriptive UA. **Resolved:** default
   `"MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"`, overridable via Settings.
8. **Alpha Vantage daily cap.** The 500/day cap is not enforceable by a per-minute bucket
   alone. **Resolved:** model the per-minute bucket (5/min) now; the daily cap is documented
   and may be added as a second bucket later (out of scope for this contract's MVP).
9. **`MCP_CACHE_DIR` default.** **Resolved:** if unset, default to
   `<tempdir>/.mcpenny_cache` (created on first use); `Settings.cache_dir` resolves and
   creates the directory.
10. **`spawn_analyst` guard breach.** **Resolved:** returns a structured error dict
    (`{"error": "...", "refused": true}`) rather than raising, so the calling agent can reason
    about the refusal.
11. **Config override of chains.** `CAPABILITY_CHAINS` defaults live in `core/config.py`;
    **Resolved:** overridable via an env var (e.g. `MCP_CAPABILITY_CHAINS` as JSON, or
    per-capability `MCP_CHAIN_<CAPABILITY>` comma lists) — implementer's choice, documented in
    `config.py`. Defaults are the table in §14.

---

## 14. Default fallback chains (`core/config.CAPABILITY_CHAINS`)

```python
CAPABILITY_CHAINS = {
    "ohlcv":                ["yfinance", "alpha_vantage", "finnhub"],
    "quote":                ["yfinance", "finnhub", "alpha_vantage"],
    "fundamentals":         ["yfinance", "fmp", "alpha_vantage"],
    "company_news":         ["finnhub", "marketaux", "alpha_vantage"],
    "news_sentiment":       ["alpha_vantage", "finnhub"],
    "social_sentiment":     ["finnhub"],
    "congress_trades":      ["finnhub", "fmp", "stockwatcher"],
    "insider_transactions": ["finnhub", "edgar"],
    "analyst_ratings":      ["finnhub", "fmp"],
    "price_targets":        ["finnhub", "fmp"],
    "upgrades_downgrades":  ["fmp", "finnhub"],
    "sec_filings":          ["edgar"],
}
```

`Settings` (in `core/config.py`) loads API keys + `MCP_CACHE_DIR` from `.env`
(via `python-dotenv`), resolves `cache_dir`, holds `CAPABILITY_CHAINS` (with env override per
§13.11), and the orchestration guards (`max_depth=3`, `max_budget_usd=1.0`) and EDGAR UA.
`Settings.from_env()` is the constructor used by `ProviderRegistry.default()`.

---

## 15. Build order (suggested)

1. `core/` (errors → types → disclaimer → config).
2. `providers/` primitives (`ratelimit`, `cache`, `circuit`, `base`) → `registry` → provider
   adapters → `__init__` wiring.
3. `servers/_sdk`, `servers/_common` → capability servers (technical, sentiment, congress,
   reports) → `synthesis` → `orchestration`.
4. `orchestration/agents` → `orchestration/main`.
5. Tests alongside each step; `test_imports.py` and the primitive tests first to lock import
   safety.
6. After implementation, re-run the knowledge graph (`/graphify . --update`) to confirm
   `ProviderRegistry` is the provider-side hub and no Layer-1 server depends on another except
   `synthesis`.
```

---

## 16. Crypto extension — very-short-window leveraged perpetuals

A **parallel crypto track** added alongside the frozen equity path (it reuses the asset-agnostic
quant cores and leaves §4's equity capabilities untouched). Built for short-window **leveraged
perpetual-futures** trading. All data sources are **keyless**, matching the keyless-first ethos.

### 16.1 New capabilities (routed to crypto providers only)

Added to `core/config.CAPABILITIES` / `CAPABILITY_CHAINS` (the registry keys chains by capability,
so there is no asset-class collision with §4):

```
crypto_ohlcv    -> [binance, bybit]            # perpetual klines (1m..1d)
crypto_quote    -> [binance, bybit, coingecko]
funding_rate    -> [binance, bybit]
open_interest   -> [binance, bybit]            # returns current + a short history list
long_short_ratio-> [binance, bybit]
crypto_sentiment-> [fear_greed]                # Alternative.me Fear & Greed
crypto_global   -> [coingecko]                 # total mcap, BTC/ETH dominance
```

`crypto_ohlcv` pulls **perpetual** klines (what you trade leveraged). Binance global
(`fapi.binance.com`) is geo-blocked (HTTP 451) from US IPs; that is a normal runtime failure, so
`registry.fetch` records it and falls through to **Bybit** automatically.

### 16.2 New providers (Layer 0, keyless; `httpx`)

| Provider | Capabilities | Endpoints |
|---|---|---|
| `binance` | the five derivatives caps + `crypto_quote` | `/fapi/v1/klines`, `/fapi/v1/premiumIndex`, `/futures/data/openInterestHist`, `/futures/data/globalLongShortAccountRatio`, `/fapi/v1/ticker/24hr` |
| `bybit` | same | `/v5/market/kline`, `/v5/market/tickers` (price+funding+OI), `/v5/market/open-interest`, `/v5/market/account-ratio` |
| `coingecko` | `crypto_global`, `crypto_quote` | `/api/v3/global`, `/api/v3/coins/markets` (optional demo key header) |
| `fear_greed` | `crypto_sentiment` | `https://api.alternative.me/fng/` |

### 16.3 New core types (`core/types.py`)

`FundingRate` (rate, mark/index, `annualized()`, `basis()`), `OpenInterest`, `LongShortRatio`,
`CryptoGlobal`. `TradeDecision` gains two back-compat optional fields: `asset_class`
(`"equity"`|`"crypto"`) and `leverage: dict` (the leverage plan; empty for equities).
`core/symbols.py` canonicalizes any input (`BTC`/`BTC-USD`/`BTC/USDT`/`BTCUSD` → `BTCUSDT`).

### 16.4 New analysis cores (`analysis/`)

- `indicators.py` — shared `ta`/`pandas` frame+indicator+signal helpers, extracted from
  `servers/technical.py` so both the equity `technical` and the crypto server reuse them (keeps the
  "servers don't import servers" rule).
- `leverage.py` — `liquidation_price`, `max_safe_leverage` (caps leverage so the ATR stop sits
  inside liquidation by `crypto_liq_buffer`), `funding_cost`, and `leverage_plan` (suggested
  leverage, liquidation, stop/target, notional/margin %, funding cost). Sized to
  `crypto_risk_per_trade`. The *suggested* leverage runs at `DEFAULT_LEVERAGE_FRACTION` (½) of the
  max-safe value — same notional/risk, double the liquidation cushion against wicks and the
  model's own approximations; `max_safe_leverage` is still reported alongside.
- `crypto_metrics.py` — `funding_signal` (contrarian, centered on the +0.01%/8h equilibrium
  baseline so resting funding scores 0), `oi_price_signal` (OI×price matrix), `long_short_signal`
  (contrarian), `fear_greed_signal` (contrarian **at the extremes only**: ≥75 / ≤25; mid-range
  scores 0), `basis_value`.
- `crypto_regime.py` — BTC trend + 12-1 momentum + crypto-tuned vol overlay (daily vol annualized
  with √365 — crypto trades every calendar day) + Fear & Greed extreme overlay →
  risk-on/caution/risk-off + a 0..1 gross-exposure scalar.

### 16.5 Server, engine, screen (Layers 1–2)

- `servers/crypto.py` — `crypto_ohlcv`, `crypto_indicators`, `crypto_signals`, `multi_timeframe`
  (5m/15m/1h), `derivatives` (funding+OI(+change)+long/short+basis, tolerant), `crypto_sentiment`.
- `orchestration/crypto.py` — `gather_crypto_evidence` → `score_crypto_evidence` (reuses the equity
  signal/factor scorers with **interval-aware saturations** — the 252/200-bar factor windows span
  hours on intraday bars, so the daily thresholds are rescaled by √(interval/1d), anchored at the
  crypto-daily values; vol is annualized to the bar frequency — + adds the crypto derivatives
  factors) → reuses `decide_from_scores` → `enrich_crypto_decision` (attaches `crypto_regime` +
  `leverage_plan`, sets `asset_class="crypto"`, horizon from the interval).
  `decide_crypto(symbol, interval, leverage_cap)` ties it together. The Binance/Bybit providers
  discover each symbol's real funding interval (4h/1h perps exist) instead of assuming 8h, and a
  missing funding rate raises (falls through the chain) rather than masquerading as 0.
- `core/crypto_universe.py` (top perps by 24h volume; live→cache→fallback) +
  `orchestration/crypto_screen.py` (two-stage funnel → best leveraged long/short `TradeDecision`s).

### 16.6 MCP + CLI surface

- **Tools:** `crypto_decide`, `crypto_evidence`, `derivatives`, `funding_rate`, `crypto_technicals`,
  `crypto_regime`, `crypto_screen`, `crypto_finalize_decision`.
- **Prompts (host-run):** `decide_crypto`, `bull_case_crypto`, `bear_case_crypto`,
  `decide_crypto_market` — tuned to leverage/liquidation/funding risk and the contrarian derivatives.
- **CLI:** `makecrazypenny --crypto SYMBOL [--interval TF] [--leverage N]`, `--crypto-market`,
  `--crypto-regime`.

### 16.7 Settings (the "aggressive" preset; env-overridable)

`crypto_max_leverage=20`, `crypto_risk_per_trade=0.025`, `crypto_maint_margin_rate=0.005`,
`crypto_target_vol=0.80`, `crypto_liq_buffer=0.5`, plus `coingecko_api_key`, `binance_base_url`,
`bybit_base_url`. AI-free and offline-testable; the leverage plan is informational only (carries the
`DISCLAIMER`) — liquidation is an isolated-margin **estimate**.

## 17. Execution extension — Hyperliquid testnet paper trading

The toolkit's first and only **authenticated, state-mutating** path: placing **paper trades on the
Hyperliquid testnet** (fake USDC). Everything in §1–§16 is a read-only, keyless
`Provider.fetch(capability)`; this is the deliberate exception, isolated in a new `execution/`
layer so the keyless/read-only invariants above stay intact. It carries the same `DISCLAIMER` and
remains informational/educational — testnet only, no real funds.

### 17.1 Safety rails (non-negotiable)

- **Testnet-locked (write path).** Every **signed** request (orders, leverage, transfers) goes
  through `Settings.hyperliquid_testnet_url` only; there is **no main-network base URL field on
  the write path anywhere**, and no Settings attribute name may contain the substring "mainnet"
  (asserted by the test suite). No env var or argument can route a signed order at real funds.
  (§18's `hyperliquid_info_url` is a separate, strictly read-only market-DATA endpoint — keyless,
  unsigned, used by `providers/hyperliquid_info.py` only; no order, signature, or key ever
  touches it.)
- **Import-safe.** The official `hyperliquid-python-sdk` (+ `eth_account`) is imported lazily inside
  `execution.hyperliquid._build_clients`; importing the package never needs the `trade` extra and
  never hits the network. The offline test suite runs without the SDK.
- **Secret-safe.** The signing key comes from `MCP_HL_PRIVATE_KEY`; `core/redact.py` masks any
  64-hex private key (a 40-hex wallet *address* is not secret and stays readable), and every
  execution error is routed through `redact_secrets`.
- **Live by default.** Tools place real testnet orders immediately (no dry-run gate).

### 17.2 Layers

- `execution/hyperliquid.py` — `HyperliquidPaperClient`, a synchronous wrapper over the SDK
  `Info`/`Exchange` clients (the SDK does the msgpack action-hash + EIP-712 signing). Reads:
  `account_state`, `open_orders` (incl. trigger metadata via `frontend_open_orders`, falling back
  to the plain listing), `recent_fills`. Writes: `set_leverage`, `open_position` (market via
  `market_open`, limit via `order` GTC; optional `stop_loss`/`take_profit` attach after the fill),
  `set_position_tpsl` (exchange-side stop-loss/take-profit), `close_position` (`market_close`),
  `cancel_order`. Validates the coin against `info.meta()`, rounds size to `szDecimals`, rounds
  limit prices to the perp wire format (5 sig figs, ≤`6 - szDecimals` decimals), and enforces the
  **$10 min order notional**. `_build_clients(settings)` is the monkeypatch seam tests replace.
- **Position TP/SL (§17.6).** `set_position_tpsl` places reduce-only **trigger-market** orders
  (`{"trigger": {triggerPx, isMarket: true, tpsl: "sl"|"tp"}}`) via `bulk_orders` under the
  **`positionTpsl` grouping**, so the exchange OCO-links them to the position: one leg executing
  (or the position closing) cancels the other, and they keep protecting the trade with no client
  connected. The closing side/size derive from the live position (`size` overridable); a trigger
  on the wrong side of the live mid (a stop that would fire instantly) is **rejected**; the fired
  market leg's `limit_px` bounds slippage at `trigger × (1 ± hl_default_slippage)`. On
  `open_position`, TP/SL attach only once the entry actually **fills** (a resting limit entry
  reports `tpsl.skipped`), and an attach failure never voids the entry — it surfaces under
  `"tpsl"`.
- `orchestration/paper_trade.py` — async wrappers (`asyncio.to_thread`, tolerant `{"error": ...}`
  on failure) plus the decision-driven `open_from_decision`: runs `decide_crypto`, sizes
  `notional = account_equity * plan.notional_pct` (override `notional_usd`), leverage =
  `plan.suggested_leverage`, side = plan direction, and (with `attach_tpsl`, the default) places
  the plan's `stop_price`/`target_price` as the position's exchange-side TP/SL; an `AVOID`/flat
  verdict places nothing.
- `core/crypto_universe.fetch_hyperliquid_perps` — a **keyless** read of the exchange's own
  listing (`POST /info {"type":"meta"}`; no key, no SDK), cached live→cache→`unavailable` (no
  curated fallback). Powers two things: the `paper_pairs` listing, and **constraining
  suggestions** — `screen_crypto(hyperliquid_only=True)` (the default) drops any universe symbol
  whose base coin isn't a listed Hyperliquid perp, so the screener never surfaces an impossible
  trade. Tolerant: an unavailable listing passes the universe through unchanged. Order placement
  is the hard backstop — `open_position` validates the coin against `info.meta()` regardless.

### 17.3 MCP + CLI surface

- **Tools:** `paper_pairs` (list tradable testnet perps — **keyless**), and the order tools that
  place REAL testnet orders and need `MCP_HL_PRIVATE_KEY`: `paper_account`, `paper_orders`
  (open orders + recent fills), `paper_trade_decision` (decide + place + plan TP/SL;
  `attach_tpsl=false` to skip), `paper_open` (optional `stop_loss`/`take_profit`),
  `paper_set_tpsl` (attach/replace protection on an existing position), `paper_close`,
  `paper_cancel`, `paper_set_leverage`.
- **CLI:** `makecrazypenny --paper-pairs` (keyless), `--paper-account`,
  `--paper-trade SYMBOL [--interval --leverage --notional]`,
  `--paper-open SYMBOL --side LONG|SHORT (--size | --notional) [--leverage --limit-price
  --stop-loss --take-profit]`, `--paper-set-tpsl SYMBOL [--stop-loss --take-profit --size]`,
  `--paper-close SYMBOL [--size]`.

### 17.4 Settings (env-overridable)

`hyperliquid_testnet_url` (`MCP_HL_TESTNET_URL`), `hl_private_key` (`MCP_HL_PRIVATE_KEY`),
`hl_account_address` (`MCP_HL_ACCOUNT_ADDRESS`, for API/agent wallets), `hl_default_slippage`
(`MCP_HL_SLIPPAGE`, default 0.05). Symbols map to Hyperliquid coins via
`core/symbols.to_hyperliquid_coin` (`BTCUSDT` → `BTC`). Install with `pip install
'makecrazypenny[trade]'`; fund a testnet wallet at https://app.hyperliquid-testnet.xyz/drip.

### 17.5 Operational notes (live-verified)

A full round trip was exercised against the live testnet (`paper_pairs` → `paper_account` →
`paper_open`/`paper_close` → `paper_trade_decision`). Two real-world facts the model surfaced and
that the toolkit now accommodates:

- **Agent (API) wallet vs master account.** A Hyperliquid **API/agent wallet** is authorized to
  *trade* on behalf of a funded **master** account, but its own address holds no balance. If the
  configured key is an agent, set `MCP_HL_ACCOUNT_ADDRESS` to the **master** address so reads
  (`account_state`/positions/fills) hit the account the fills actually land in — otherwise orders
  "fill" while the account looks empty. The exchange's `userRole` info call (`{"role":"agent",
  "data":{"user":<master>}}`) reveals the master. **Agent wallets cannot move funds**:
  `usd_class_transfer`/withdrawals are rejected (`"Must deposit before performing actions"`) and
  must be signed by the master/owner wallet (or done in the testnet UI).
- **Spot vs perp collateral.** USDC lives in separate **spot** and **perp** wallets; Hyperliquid
  draws spot collateral into perp as a position needs it, so a flat perp account reads `$0` while
  the funds sit in spot. `account_state` therefore also reports `spot_usdc` and a combined
  `tradable_usdc` (perp equity + free spot), and `open_from_decision` sizes against
  `tradable_usdc` so the decision path works when funds are in spot. Order placement still
  validates the coin against `info.meta()` as the hard backstop.

## 18. Swarm extension — multi-agent trading loop (free data only)

Turns the toolkit into an always-on **multi-agent trading swarm** while keeping every prior
invariant: the app remains a pure MCP server, the engine stays AI-free and deterministic, all new
data is keyless (free APIs / RSS / public mirrors), and the only write path is §17's testnet one.
The swarm's MODELS come from the MCP host's own subscription (Claude Code sub-agents); the server
supplies tools, the playbook prompt, and the persistent state.

### 18.1 Division of labor (the architecture decision)

- **MCP sampling is NOT used**: as of 2026-06 neither Claude Code nor Claude Desktop implements
  `sampling/createMessage`, and FastMCP's fallback requires an API key (violates
  subscription-only). Re-evaluate if anthropics/claude-code#1785 ships.
- **The server ships:** deterministic data tools (§18.2), the swarm state (standing goal +
  journal, §18.4), the risk gate (§18.5), and the `trade_swarm` PROMPT — the playbook the host's
  model executes. **The repo ships:** `.claude/agents/` role definitions with `model:` frontmatter
  (`hype-scout` = haiku, `news-reader` = sonnet, `chart-analyst` = opus; all three EXCLUDE the
  order-placing `paper_*` tools — the scout may read the keyless `paper_pairs` listing — so only
  the host session can trade) and `.claude/commands/trade-swarm.md`.
- **The loop:** interactively `/loop 15m /trade-swarm` (or the `trade_swarm` MCP prompt); the
  standing goal + journal live server-side precisely so stateless scheduled cycles share memory.

### 18.2 New capabilities (keyless; routed per §14 chains)

`hl_asset_ctx`, `hl_predicted_funding`, `hl_l2book`, `hl_funding_history`, `hl_market_pulse`
(→ `providers/hyperliquid_info.py`, POST `{"type": ...}` to `hyperliquid_info_url`;
`hl_market_pulse` also diffs the perp universe against a cached snapshot to detect NEW LISTINGS);
`taker_flow`, `top_trader_ratio`, `funding_history` (→ `providers/binance.py` futures-data
endpoints; klines additionally parse field 9 → `taker_buy_volume` for CVD); `social_scan`
(→ `providers/social.py`: Arctic Shift Reddit mirror — reddit.com 403s datacenter IPs —,
StockTwits platform-native Bullish/Bearish label COUNTING, 4chan /biz/ mention counting,
CoinGecko trending; all text ASCII-sanitized at the boundary); `news_feed`
(→ `providers/news_rss.py`: CoinTelegraph + CoinDesk + Google News RSS, stdlib XML, deduped).

### 18.3 New scored factors (engine stays deterministic)

`analysis/crypto_metrics.py`: `taker_flow_signal` (w 1.5, "flow"), `cvd_signal` (w 1.0, "flow"),
`top_trader_spread_signal` (w 1.0, "positioning", FOLLOWS top traders), `funding_z_signal`
(w 1.0, "funding", contrarian beyond |z|>=1.5 vs 21d), `predicted_funding_signal` (w 0.75,
"funding", HL hourly + cross-venue forward), `social_velocity_signal` (w 0.5, "social",
deterministic counting ONLY). `depth_imbalance` + `venue_divergence` are **order-time gates**
(surfaced by the `orderflow` tool), never scored. LLM interpretations (news readings, scout
narratives) NEVER enter `score_crypto_evidence` — they merge at host level via
`crypto_finalize_decision`, same as the debate verdict. With more independent categories the
crypto scorer demands broader corroboration: coverage norm 5 (vs 4), >=3 categories (or
|net| >= 2.5) to act — carried in the scored dict, equity behavior unchanged. The leverage
plan's funding COST is HL-first (hourly, the venue traded on), CEX fallback.
`TradeDecision.as_of` stamps decision freshness.

### 18.4 Journal + standing goal (`orchestration/journal.py`)

Append-only JSONL under `cache_dir/journal/` (`decisions`, `cycles`, `equity`) + `goal.json`;
tolerant readers skip corrupt lines. `record_decision` (auto-called on placement, with a
generated `cloid` + exchange oids), `record_cycle`, `snapshot_equity`, `reconcile` (cloid → oid →
symbol+window matching; realized PnL, R-multiple, win/loss), `performance()` (scoreboard:
hit rate, avg R, PnL by symbol, equity tail), `goal_get`/`goal_set`. Journaling failure NEVER
blocks an order. `digest()` is the CONTEXT-LEAN read for looped sessions: one bounded payload
(goal, `cycles_since_review` computed from §18.7's `kind: "strategy-review"` tags, the last
review memo, and hard-clipped one-line rows for recent cycles/decisions + equity tail; the
verbose per-leg cycle fields are dropped by design) — cycle starts call it INSTEAD of
`recent`+`performance`, and it is the rehydration call after a host-side context compaction.
`record_cycle` entries may carry a free-form `kind` (exposed as a `journal_record` tool param).
MCP tools: `swarm_goal_get`/`swarm_goal_set`, `journal_record`, `journal_recent`,
`journal_digest`, `journal_performance`; plus data tools `market_pulse`, `orderflow`,
`social_scan`, `news_feed`.

### 18.5 Risk gate (in `paper_trade`, before any order)

Same-direction position cap (`MCP_SWARM_MAX_POSITIONS`, default 3; same-coin adds exempt);
daily-loss kill-switch (`MCP_SWARM_MAX_DAILY_LOSS_PCT`, default 5.0, vs the UTC-midnight equity
snapshot — breach refuses new risk-increasing orders); correlated-exposure cap
(`analysis/risk.correlated_exposure_check`: BTC-beta buckets, cap 2x equity, auto-downsize);
`reduce_only` bypasses (risk-reducing). Refusals are `{placed: false, reason}` envelopes, never
exceptions; every sub-check degrades defensively. `MCP_SWARM_RISK_GATE=off` disables (dangerous,
documented). Sizing upgrades in `analysis/risk.py`: `parkinson_vol`/`yang_zhang_vol` (efficient
estimators), `kelly_calibrated` (p shrunk toward realized journal hit-rate; quarter-Kelly until
50 closed trades).

### 18.6 Settings (env-overridable)

`hyperliquid_info_url` (`MCP_HL_INFO_URL`; read-only data, see §17.1); risk-gate knobs above are
read from the environment at call time (no Settings fields). New TTLs per §8.5 table in
`core/config.py` (ctx/pulse 30s, l2book 5s, histories 300s, social 120s, news 300s).

### 18.7 Hourly strategy review (every 4th cycle)

Every 4th swarm cycle (~hourly at the 15m loop) the host runs `/strategy-review`
(`.claude/commands/strategy-review.md` + `.claude/workflows/strategy-review.js`): four read-only
legs routed cheap-jobs-to-cheap-models — regime/pulse/funding survey (haiku, mechanical relay),
journal-performance audit (sonnet, cites scoreboard numbers), macro news scan (sonnet
`news-reader`, days-to-weeks themes + scheduled events), BTC/ETH/SOL multi-timeframe +
positioning deep-dive (opus `chart-analyst`). The HOST fuses them into a strategy memo, may take
risk-REDUCING position actions only (close/tighten; entries remain §18.1's cycle path), and
persists the strategy by rewriting the standing goal as
`<core objective> || STRATEGY @ <UTC>: bias/gross/focus/avoid/timeframes/fixes` (the block after
`" || STRATEGY"` is replaced wholesale; the core objective is never touched) — subsequent cycles
pipe it verbatim into the sub-agent preambles. Cadence is STATELESS, derived from §18.4's journal:
the review records a cycle entry with `kind: "strategy-review"`, and §18.4's `digest()` computes
`cycles_since_review` server-side — each `/trade-swarm` cycle reads it off `journal_digest`
(due at >= 4; scalp loops additionally require the last review to be > ~50 min old).
