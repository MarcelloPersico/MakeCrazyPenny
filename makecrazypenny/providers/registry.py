"""The provider registry (see CONTRACT.md §8.5).

``ProviderRegistry`` ties together the Layer-0 primitives: it instantiates
providers, owns one :class:`TokenBucket` per ``rate_key``, one
:class:`CircuitBreaker` per provider, and a shared :class:`TTLCache`. Its
``fetch`` walks a capability's fallback chain, honoring rate limits, the cache
(with single-flight), and the circuit breaker, and returns a uniform envelope.

The registry is fully unit-testable offline: providers are injected/looked up
by name via :meth:`register`, so tests can supply fakes without any network.
"""

from __future__ import annotations

from typing import Any

from ..core.config import Settings, default_ttl
from ..core.errors import AllProvidersFailed, MissingApiKey, RateLimited
from ..core.redact import redact_secrets
from .base import PROVIDER_REGISTRY, Provider
from .cache import TTLCache
from .circuit import CircuitBreaker
from .ratelimit import TokenBucket

#: Ceiling on how long ``fetch`` will sleep for rate-limit tokens before moving
#: on to the next provider in the chain. Generous enough for the politest free
#: tiers (CoinGecko refills 1 token / 12s), but bounded so a drained bucket can
#: never freeze a tool call indefinitely.
_MAX_RATE_WAIT_S = 90.0


class ProviderRegistry:
    """Capability-oriented facade over a set of providers.

    Attributes:
        settings: The active :class:`Settings`.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize empty maps and the shared cache.

        Providers are *not* auto-instantiated here; use :meth:`register` (or the
        :meth:`default` classmethod which registers the auto-discovered set).

        Args:
            settings: Process configuration (keys, cache dir, chains, guards).
        """
        self.settings = settings
        self._providers: dict[str, Provider] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._circuits: dict[str, CircuitBreaker] = {}
        self._cache = TTLCache(
            settings.resolve_cache_dir(),
            l2_enabled=settings.l2_cache_enabled,
        )

    # -- registration -------------------------------------------------------

    def register(self, provider: Provider) -> None:
        """Register a provider instance and build its bucket + circuit.

        Args:
            provider: A constructed :class:`Provider` instance.
        """
        self._providers[provider.name] = provider
        if provider.rate_key not in self._buckets:
            self._buckets[provider.rate_key] = TokenBucket(provider.rate_per_min)
        self._circuits[provider.name] = CircuitBreaker(
            fail_threshold=self.settings.circuit_fail_threshold,
            cooldown_s=self.settings.circuit_cooldown_s,
        )

    def get(self, name: str) -> Provider | None:
        """Return the registered provider by ``name``, or ``None``."""
        return self._providers.get(name)

    @classmethod
    def default(cls) -> "ProviderRegistry":
        """Build a registry from ``Settings.from_env()`` + ``PROVIDER_REGISTRY``.

        Each auto-registered provider class is instantiated; a class that fails
        to construct is skipped (so one broken provider cannot break the whole
        registry).

        Returns:
            A ready-to-use :class:`ProviderRegistry`.
        """
        settings = Settings.from_env()
        registry = cls(settings)
        for provider_cls in PROVIDER_REGISTRY:
            try:
                registry.register(provider_cls(settings))
            except Exception:
                # A provider that cannot even be constructed is skipped; the
                # chain simply will not include it.
                continue
        return registry

    # -- fetch --------------------------------------------------------------

    async def fetch(self, capability: str, *, ttl: float | None = None, **params: Any) -> dict:
        """Fetch ``capability`` via its fallback chain.

        Walks ``settings.CAPABILITY_CHAINS[capability]`` in order. For each
        provider it skips ones that are missing, do not support the capability,
        or whose circuit is open; acquires rate-limit tokens (bounded by
        ``_MAX_RATE_WAIT_S``); and fetches through the cache (single-flight).
        ``MissingApiKey``, ``NotImplementedError``, and ``RateLimited`` are
        silent skips that do NOT trip the breaker; any other exception records
        a failure and continues down the chain.

        Args:
            capability: One of the FROZEN capability names.
            ttl: Optional cache TTL override (seconds). Defaults to
                :func:`default_ttl` for the capability.
            **params: Capability-specific parameters (e.g. ``symbol``).

        Returns:
            ``{"provider": <name>, "data": <json-ready>, "cached": <bool>}``.

        Raises:
            AllProvidersFailed: If every provider in the chain was skipped or
                failed.
        """
        chain = self.settings.CAPABILITY_CHAINS.get(capability, [])
        effective_ttl = ttl if ttl is not None else default_ttl(capability)
        reasons: dict[str, str] = {}

        for name in chain:
            provider = self._providers.get(name)
            if provider is None:
                reasons[name] = "not registered"
                continue
            if capability not in provider.supported:
                reasons[name] = "capability not supported"
                continue
            circuit = self._circuits.get(name)
            if circuit is not None and not circuit.allow():
                reasons[name] = "circuit open"
                continue

            try:
                bucket = self._buckets.get(provider.rate_key)
                if bucket is not None:
                    await bucket.acquire(provider.cost, max_wait=_MAX_RATE_WAIT_S)

                result = await self._cache.get_or_fetch(
                    key=(name, capability, params),
                    ttl=effective_ttl,
                    factory=lambda p=provider: p.fetch(capability, **params),
                )
            except RateLimited as exc:
                # Congestion, not a provider fault: skip down the chain without
                # tripping the breaker; the bucket refills on its own.
                reasons[name] = f"rate limited: {exc}"
                continue
            except MissingApiKey as exc:
                # Configuration fact: skip silently, do not trip the breaker.
                reasons[name] = f"missing API key: {exc.env_var}"
                continue
            except NotImplementedError:
                reasons[name] = "capability not implemented"
                continue
            except Exception as exc:
                if circuit is not None:
                    circuit.record_failure()
                # Redact any API key embedded in the message (e.g. a failing URL
                # carries `?token=...`) before it reaches reasons/UI/logs.
                reasons[name] = redact_secrets(f"{type(exc).__name__}: {exc}")
                continue

            if circuit is not None:
                circuit.record_success()
            return {"provider": name, "data": result.value, "cached": result.cached}

        raise AllProvidersFailed(capability, reasons)


__all__ = ["ProviderRegistry"]
