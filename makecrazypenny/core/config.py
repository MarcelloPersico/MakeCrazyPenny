"""Configuration: ``Settings``, default fallback chains, and TTL policy.

See CONTRACT.md §13.9, §13.11, §14, and §8.5.

``Settings`` loads API keys + ``MCP_CACHE_DIR`` from the environment (optionally
from a ``.env`` file via ``python-dotenv`` when available), resolves and creates
the cache directory on demand, holds the per-capability fallback chains (with
env override), the orchestration guards, and the EDGAR User-Agent string.

Importing this module never hits the network and never requires any key.
``python-dotenv`` is imported lazily and treated as optional.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Capability vocabulary (FROZEN — see CONTRACT.md §4) and default chains (§14).
# ---------------------------------------------------------------------------

CAPABILITIES: tuple[str, ...] = (
    "ohlcv",
    "quote",
    "fundamentals",
    "company_news",
    "news_sentiment",
    "social_sentiment",
    "congress_trades",
    "insider_transactions",
    "analyst_ratings",
    "price_targets",
    "upgrades_downgrades",
    "sec_filings",
    # --- Crypto extension (CONTRACT.md §16) -------------------------------
    # A parallel vocabulary for digital-asset / perpetual-futures data. These
    # names are routed to crypto providers only; the equity chains above are
    # untouched (the registry keys chains by capability, not by asset class).
    "crypto_ohlcv",
    "crypto_quote",
    "funding_rate",
    "open_interest",
    "long_short_ratio",
    "crypto_sentiment",
    "crypto_global",
)

CAPABILITY_CHAINS: dict[str, list[str]] = {
    "ohlcv": ["yfinance", "alpha_vantage", "finnhub"],
    "quote": ["yfinance", "finnhub", "alpha_vantage"],
    "fundamentals": ["yfinance", "fmp", "alpha_vantage"],
    "company_news": ["finnhub", "marketaux", "alpha_vantage"],
    "news_sentiment": ["alpha_vantage", "finnhub"],
    "social_sentiment": ["finnhub"],
    "congress_trades": ["finnhub", "fmp", "stockwatcher"],
    "insider_transactions": ["finnhub", "edgar"],
    "analyst_ratings": ["finnhub", "fmp"],
    "price_targets": ["finnhub", "fmp"],
    "upgrades_downgrades": ["fmp", "finnhub"],
    "sec_filings": ["edgar"],
    # --- Crypto extension. Binance is richest but geo-blocked from US IPs;
    # the chain falls through to Bybit automatically on any runtime failure. ---
    "crypto_ohlcv": ["binance", "bybit"],
    "crypto_quote": ["binance", "bybit", "coingecko"],
    "funding_rate": ["binance", "bybit"],
    "open_interest": ["binance", "bybit"],
    "long_short_ratio": ["binance", "bybit"],
    "crypto_sentiment": ["fear_greed"],
    "crypto_global": ["coingecko"],
}

# Per-capability cache TTLs in seconds (see CONTRACT.md §8.5).
_DEFAULT_TTLS: dict[str, float] = {
    "quote": 15.0,
    "ohlcv": 300.0,
    "company_news": 600.0,
    "news_sentiment": 600.0,
    "social_sentiment": 600.0,
    "congress_trades": 3600.0,
    "insider_transactions": 3600.0,
    "sec_filings": 3600.0,
    "analyst_ratings": 3600.0,
    "price_targets": 3600.0,
    "upgrades_downgrades": 3600.0,
    "fundamentals": 3600.0,
    # Crypto: short windows demand fresh data; derivatives move fast.
    "crypto_ohlcv": 60.0,
    "crypto_quote": 15.0,
    "funding_rate": 60.0,
    "open_interest": 60.0,
    "long_short_ratio": 300.0,
    "crypto_sentiment": 600.0,
    "crypto_global": 300.0,
}

# Fallback TTL for any capability not explicitly listed above.
_FALLBACK_TTL: float = 600.0

# Default descriptive User-Agent for SEC EDGAR (see CONTRACT.md §13.7).
DEFAULT_EDGAR_USER_AGENT: str = "MakeCrazyPenny/0.1 (persico.mlo@gmail.com)"


def default_ttl(capability: str) -> float:
    """Return the default cache TTL (seconds) for a capability.

    Args:
        capability: One of the FROZEN capability names.

    Returns:
        The configured TTL, or a conservative fallback for unknown capabilities.
    """
    return _DEFAULT_TTLS.get(capability, _FALLBACK_TTL)


def _load_dotenv_if_available() -> None:
    """Best-effort load of a ``.env`` file via ``python-dotenv`` (optional)."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        load_dotenv(override=False)
    except Exception:
        # A malformed .env must never break import/configuration.
        pass


def _resolve_chains_from_env(base: dict[str, list[str]]) -> dict[str, list[str]]:
    """Apply env overrides to the default capability chains (see §13.11).

    Two override mechanisms are supported (the more specific wins per capability):
      * ``MCP_CAPABILITY_CHAINS`` — a JSON object mapping capability -> list[str].
      * ``MCP_CHAIN_<CAPABILITY>`` — a comma-separated provider list for a single
        capability (capability name upper-cased).

    Unknown capabilities in overrides are ignored.
    """
    chains = copy.deepcopy(base)

    raw_json = os.environ.get("MCP_CAPABILITY_CHAINS")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                for cap, providers in parsed.items():
                    if cap in chains and isinstance(providers, list):
                        chains[cap] = [str(p).strip() for p in providers if str(p).strip()]
        except (ValueError, TypeError):
            # Malformed JSON override is ignored; defaults stand.
            pass

    for cap in chains:
        env_name = f"MCP_CHAIN_{cap.upper()}"
        raw = os.environ.get(env_name)
        if raw:
            providers = [p.strip() for p in raw.split(",") if p.strip()]
            if providers:
                chains[cap] = providers

    return chains


@dataclass
class Settings:
    """Process configuration loaded from the environment.

    Attributes:
        alpha_vantage_api_key: ``ALPHA_VANTAGE_API_KEY`` (or ``None``).
        finnhub_api_key: ``FINNHUB_API_KEY`` (or ``None``).
        fmp_api_key: ``FMP_API_KEY`` (or ``None``).
        marketaux_api_key: ``MARKETAUX_API_KEY`` (or ``None``).
        cache_dir: Resolved on-disk cache directory (created on first use).
        capability_chains: Per-capability provider fallback order.
        edgar_user_agent: Descriptive UA sent to SEC EDGAR.
        max_depth: Hard recursion guard for ``spawn_analyst`` (default 3).
        max_budget_usd: Hard budget guard for ``spawn_analyst`` (default 1.0).
        circuit_fail_threshold: Failures before a provider circuit opens.
        circuit_cooldown_s: Cooldown before a circuit moves to half-open.
        l2_cache_enabled: Whether the on-disk L2 cache is active.
    """

    alpha_vantage_api_key: str | None = None
    finnhub_api_key: str | None = None
    fmp_api_key: str | None = None
    marketaux_api_key: str | None = None
    #: Optional CoinGecko demo key (sent as a header when present). Never required.
    coingecko_api_key: str | None = None

    #: Crypto exchange REST base URLs (overridable for proxies / regional mirrors).
    binance_base_url: str = "https://fapi.binance.com"
    bybit_base_url: str = "https://api.bybit.com"

    #: Hyperliquid **testnet** paper-trading (CONTRACT.md §17). This is the only
    #: authenticated, state-mutating path in the toolkit. It is intentionally
    #: locked to testnet — there is no mainnet base URL field, so no amount of
    #: misconfiguration can route a signed order at real funds.
    hyperliquid_testnet_url: str = "https://api.hyperliquid-testnet.xyz"
    #: Wallet private key used to SIGN testnet orders (``MCP_HL_PRIVATE_KEY``).
    #: A secret — never logged; error strings are routed through ``redact_secrets``.
    hl_private_key: str | None = None
    #: Optional funded account/vault address when signing with an API/agent wallet
    #: whose address differs from the signer's (``MCP_HL_ACCOUNT_ADDRESS``).
    #: Defaults to the signer key's own address when unset.
    hl_account_address: str | None = None
    #: Default market-order slippage cap (fraction) for paper market orders.
    hl_default_slippage: float = 0.05

    #: Crypto leverage-risk policy (informational sizing; tunable per §16). The
    #: defaults below are the "aggressive" preset chosen at build time.
    crypto_max_leverage: float = 20.0
    crypto_risk_per_trade: float = 0.025
    crypto_maint_margin_rate: float = 0.005
    crypto_target_vol: float = 0.80
    #: The ATR stop must sit at least this fraction inside the liquidation distance
    #: (0.5 => liquidation is >=1.5x the stop distance away from entry).
    crypto_liq_buffer: float = 0.5

    cache_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / ".mcpenny_cache")
    capability_chains: dict[str, list[str]] = field(
        default_factory=lambda: copy.deepcopy(CAPABILITY_CHAINS)
    )
    edgar_user_agent: str = DEFAULT_EDGAR_USER_AGENT

    max_depth: int = 3
    max_budget_usd: float = 1.0

    #: Number of bull-vs-bear rebuttal rounds in the decision debate (§10.3).
    debate_rounds: int = 2

    circuit_fail_threshold: int = 5
    circuit_cooldown_s: float = 60.0
    l2_cache_enabled: bool = True

    # Uppercase alias kept for parity with the CONTRACT's prose
    # (``settings.CAPABILITY_CHAINS[capability]``).
    @property
    def CAPABILITY_CHAINS(self) -> dict[str, list[str]]:  # noqa: N802 (contract spelling)
        """Alias for :attr:`capability_chains` (contract uses upper-case)."""
        return self.capability_chains

    def get_api_key(self, env_var: str) -> str | None:
        """Return the configured value for a key by its env-var name.

        Used by providers via their ``requires_key`` attribute so each provider
        need not know which ``Settings`` field backs its key.

        Args:
            env_var: Environment variable name (e.g. ``"FINNHUB_API_KEY"``).

        Returns:
            The key value if set and non-empty, else ``None``.
        """
        mapping = {
            "ALPHA_VANTAGE_API_KEY": self.alpha_vantage_api_key,
            "FINNHUB_API_KEY": self.finnhub_api_key,
            "FMP_API_KEY": self.fmp_api_key,
            "MARKETAUX_API_KEY": self.marketaux_api_key,
            "COINGECKO_API_KEY": self.coingecko_api_key,
            "MCP_HL_PRIVATE_KEY": self.hl_private_key,
        }
        if env_var in mapping:
            # A known key: the injected Settings is authoritative. An explicit
            # None means "disabled" — do NOT silently fall back to ambient env
            # (that would defeat dependency injection and test isolation).
            return mapping[env_var] or None
        # Unknown var name: fall back to a live environment lookup.
        return os.environ.get(env_var) or None

    def resolve_cache_dir(self) -> Path:
        """Resolve and create the cache directory, returning its ``Path``."""
        path = Path(self.cache_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Disk issues must not crash configuration; the cache degrades to
            # L1-only at use time.
            pass
        return path

    @classmethod
    def from_env(cls) -> "Settings":
        """Build ``Settings`` from the environment (optionally via ``.env``).

        Loads ``python-dotenv`` if installed, reads all API keys and
        ``MCP_CACHE_DIR``, applies capability-chain env overrides, and resolves
        the cache directory. Never raises on missing keys.
        """
        _load_dotenv_if_available()

        cache_env = os.environ.get("MCP_CACHE_DIR")
        cache_dir = (
            Path(cache_env)
            if cache_env
            else Path(tempfile.gettempdir()) / ".mcpenny_cache"
        )

        def _float_env(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        def _int_env(name: str, default: int) -> int:
            raw = os.environ.get(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        settings = cls(
            alpha_vantage_api_key=os.environ.get("ALPHA_VANTAGE_API_KEY") or None,
            finnhub_api_key=os.environ.get("FINNHUB_API_KEY") or None,
            fmp_api_key=os.environ.get("FMP_API_KEY") or None,
            marketaux_api_key=os.environ.get("MARKETAUX_API_KEY") or None,
            coingecko_api_key=os.environ.get("COINGECKO_API_KEY") or None,
            binance_base_url=os.environ.get("MCP_BINANCE_BASE_URL") or "https://fapi.binance.com",
            bybit_base_url=os.environ.get("MCP_BYBIT_BASE_URL") or "https://api.bybit.com",
            hyperliquid_testnet_url=(
                os.environ.get("MCP_HL_TESTNET_URL") or "https://api.hyperliquid-testnet.xyz"
            ),
            hl_private_key=os.environ.get("MCP_HL_PRIVATE_KEY") or None,
            hl_account_address=os.environ.get("MCP_HL_ACCOUNT_ADDRESS") or None,
            hl_default_slippage=_float_env("MCP_HL_SLIPPAGE", 0.05),
            crypto_max_leverage=_float_env("MCP_CRYPTO_MAX_LEVERAGE", 20.0),
            crypto_risk_per_trade=_float_env("MCP_CRYPTO_RISK_PER_TRADE", 0.025),
            crypto_maint_margin_rate=_float_env("MCP_CRYPTO_MAINT_MARGIN", 0.005),
            crypto_target_vol=_float_env("MCP_CRYPTO_TARGET_VOL", 0.80),
            crypto_liq_buffer=_float_env("MCP_CRYPTO_LIQ_BUFFER", 0.5),
            cache_dir=cache_dir,
            capability_chains=_resolve_chains_from_env(CAPABILITY_CHAINS),
            edgar_user_agent=os.environ.get("MCP_EDGAR_USER_AGENT") or DEFAULT_EDGAR_USER_AGENT,
            max_depth=_int_env("MCP_MAX_DEPTH", 3),
            max_budget_usd=_float_env("MCP_MAX_BUDGET_USD", 1.0),
            debate_rounds=_int_env("MCP_DEBATE_ROUNDS", 2),
            circuit_fail_threshold=_int_env("MCP_CIRCUIT_FAIL_THRESHOLD", 5),
            circuit_cooldown_s=_float_env("MCP_CIRCUIT_COOLDOWN_S", 60.0),
            l2_cache_enabled=(os.environ.get("MCP_L2_CACHE", "1").strip() not in ("0", "false", "False")),
        )
        return settings


__all__ = [
    "Settings",
    "CAPABILITY_CHAINS",
    "CAPABILITIES",
    "default_ttl",
    "DEFAULT_EDGAR_USER_AGENT",
]
