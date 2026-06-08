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
makecrazypenny/orchestration/__init__.py                                      [DONE]
makecrazypenny/orchestration/agents.py  AgentDefinitions + build_options()
makecrazypenny/orchestration/main.py    CLI entrypoint
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

`argparse` CLI: `python -m makecrazypenny.orchestration.main SYMBOL [--depth N]`.
- Runs the mother orchestrator on `SYMBOL`, prints the cross-checked report with the
  disclaimer (`core.disclaimer.with_disclaimer`).
- If the SDK is not installed, print install instructions (`pip install
  'makecrazypenny[...]'` / `pip install claude-agent-sdk`) and exit **non-zero** gracefully
  (no traceback).

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
