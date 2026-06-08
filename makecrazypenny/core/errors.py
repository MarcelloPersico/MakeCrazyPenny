"""Error taxonomy for the provider layer (see CONTRACT.md §6).

All provider-layer failures derive from :class:`ProviderError` so callers can
catch the whole family with a single ``except``. The registry distinguishes
*skips* (``MissingApiKey``, ``NotImplementedError``) from genuine runtime
failures: only the latter trip a provider's circuit breaker.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider-layer error."""


class RateLimited(ProviderError):
    """Raised when a token bucket's configured max-wait is exceeded, or an
    upstream API returns HTTP 429.

    By default token buckets *wait* rather than raise; this error is reserved
    for an explicit max-wait timeout or a propagated upstream 429.
    """


class CircuitOpen(ProviderError):
    """Raised/used when a provider's circuit breaker is open and the call is
    therefore skipped."""


class AllProvidersFailed(ProviderError):
    """Raised by the registry when every provider in a capability's fallback
    chain failed, was skipped, or was unavailable.

    The offending ``capability`` is stored on the instance so callers/agents
    can report which capability could not be served.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"All providers failed for capability: {capability!r}")


class MissingApiKey(ProviderError):
    """Raised by a provider's ``fetch()`` when a required API key is absent.

    Carries both the provider ``name`` and the missing ``env_var`` so the
    message is actionable. The registry treats this as a silent skip (it does
    NOT trip the circuit breaker) and falls through to the next provider.
    """

    def __init__(self, provider: str, env_var: str) -> None:
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"Provider {provider!r} requires API key from environment "
            f"variable {env_var!r}, which is not set."
        )
