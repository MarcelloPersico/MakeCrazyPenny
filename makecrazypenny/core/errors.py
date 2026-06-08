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
    can report which capability could not be served. ``reasons`` maps each
    attempted provider name to a short explanation of why it was skipped or
    failed (e.g. ``"missing API key: FINNHUB_API_KEY"``, ``"circuit open"``),
    which is folded into the message and exposed for friendlier UI/agent output.
    """

    def __init__(self, capability: str, reasons: dict[str, str] | None = None) -> None:
        self.capability = capability
        self.reasons: dict[str, str] = dict(reasons or {})
        message = f"All providers failed for capability: {capability!r}"
        if self.reasons:
            detail = "; ".join(f"{name}: {why}" for name, why in self.reasons.items())
            message = f"{message} ({detail})"
        super().__init__(message)

    @property
    def missing_api_keys(self) -> list[str]:
        """De-duplicated env-var names of providers skipped for a missing key.

        Empty when no attempted provider was skipped purely because its API key
        was absent. Useful for telling a user exactly which keys would unlock the
        capability, rather than surfacing a generic "all providers failed".
        """
        marker = "missing API key:"
        keys: list[str] = []
        for why in self.reasons.values():
            if marker in why:
                env = why.split(marker, 1)[1].strip()
                if env and env not in keys:
                    keys.append(env)
        return keys


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
