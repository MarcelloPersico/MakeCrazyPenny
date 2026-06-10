"""Layer 0: provider / data-access layer.

The only code that talks to an external API. Exposes a single shared
``ProviderRegistry`` that owns rate limiting, caching, retries, circuit
breaking, and per-capability fallback chains.

On import this package attempts to import every concrete provider submodule so
that each module's ``@register_provider`` decorator runs and populates
``PROVIDER_REGISTRY``. Each import is wrapped in ``try/except`` so a not-yet-
written or broken provider module cannot break the package. Provider modules use
LAZY imports for heavy/optional libs, so importing them never requires API keys
or hits the network.

``get_registry()`` returns a process-wide singleton ``ProviderRegistry`` built
via ``ProviderRegistry.default()``.
"""

from __future__ import annotations

import importlib

from .base import PROVIDER_REGISTRY, Provider, register_provider
from .cache import CacheResult, TTLCache
from .circuit import CircuitBreaker
from .ratelimit import TokenBucket
from .registry import ProviderRegistry

# Concrete provider submodules to import for their registration side effects.
# A module that does not yet exist or fails to import is skipped silently so
# the package (and the rest of the providers) remain usable.
_PROVIDER_MODULES = (
    "yfinance_provider",
    "alpha_vantage",
    "finnhub",
    "fmp",
    "edgar",
    "stockwatcher",
    "marketaux",
    # Crypto extension (CONTRACT.md §16): all keyless.
    "binance",
    "bybit",
    "coingecko",
    "fear_greed",
    # Swarm extension (CONTRACT.md §18): keyless HL info reads + social/news.
    "hyperliquid_info",
    "social",
    "news_rss",
)

for _mod in _PROVIDER_MODULES:
    try:
        importlib.import_module(f".{_mod}", __name__)
    except Exception:
        # A missing or broken provider module must not break the package.
        continue

_registry_singleton: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """Return the process-wide singleton :class:`ProviderRegistry`.

    The registry is built lazily on first call via
    :meth:`ProviderRegistry.default` (which reads ``Settings.from_env()`` and
    the auto-registered ``PROVIDER_REGISTRY``).

    Returns:
        The shared :class:`ProviderRegistry` instance.
    """
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ProviderRegistry.default()
    return _registry_singleton


def reset_registry() -> None:
    """Discard the cached singleton so the next ``get_registry()`` rebuilds it.

    Useful in tests after mutating the environment.
    """
    global _registry_singleton
    _registry_singleton = None


__all__ = [
    "Provider",
    "PROVIDER_REGISTRY",
    "register_provider",
    "ProviderRegistry",
    "TokenBucket",
    "TTLCache",
    "CacheResult",
    "CircuitBreaker",
    "get_registry",
    "reset_registry",
]
