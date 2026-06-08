"""Provider ABC, auto-registration list, and decorator (see CONTRACT.md §8.1).

A ``Provider`` is the only code that talks to an external API. Concrete adapters
subclass this, declare which capabilities they ``support``, and implement an
async ``fetch``. The ``@register_provider`` decorator appends the class to the
module-level ``PROVIDER_REGISTRY`` so ``ProviderRegistry.default()`` can build
the full set automatically.

Importing this module never hits the network and never requires a key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..core.errors import MissingApiKey

if TYPE_CHECKING:  # avoid an import cycle at runtime; only needed for typing
    from ..core.config import Settings

# Auto-registration target: every @register_provider class lands here.
PROVIDER_REGISTRY: list[type["Provider"]] = []


def register_provider(cls: type["Provider"]) -> type["Provider"]:
    """Class decorator: append ``cls`` to :data:`PROVIDER_REGISTRY`.

    Idempotent — re-importing a provider module will not duplicate the entry.

    Args:
        cls: A concrete ``Provider`` subclass.

    Returns:
        ``cls`` unchanged (so it remains usable as a normal class).
    """
    if cls not in PROVIDER_REGISTRY:
        PROVIDER_REGISTRY.append(cls)
    return cls


class Provider(ABC):
    """Abstract base for a single external-data provider adapter.

    Subclasses set the class attributes below and implement :meth:`fetch`.

    Class attributes:
        name: Unique provider identifier (e.g. ``"finnhub"``).
        supported: Subset of the FROZEN capability names this provider serves.
        rate_per_min: Requests/minute used to size the shared token bucket;
            ``0`` (or negative) means effectively unlimited.
        cost: Tokens consumed from the bucket per :meth:`fetch` call.
        requires_key: Name of the env var holding this provider's API key, or
            ``None`` if the provider needs no key.

    Instance attributes:
        rate_key: Token-bucket key. Defaults to :attr:`name`, letting providers
            that share an upstream key share a bucket.
    """

    name: str = ""
    supported: set[str] = set()
    rate_per_min: int = 0
    cost: int = 1
    requires_key: str | None = None

    def __init__(self, settings: "Settings") -> None:
        """Store settings and derive the (default) rate key.

        Args:
            settings: Process configuration providing API keys, UA, etc.
        """
        self.settings = settings
        self.rate_key: str = self.name

    def api_key(self) -> str:
        """Return this provider's API key, raising if it is required but absent.

        Returns:
            The configured key value.

        Raises:
            MissingApiKey: If :attr:`requires_key` is set but no value is found.
        """
        if not self.requires_key:
            return ""
        value = self.settings.get_api_key(self.requires_key)
        if not value:
            raise MissingApiKey(self.name, self.requires_key)
        return value

    def ensure_supported(self, capability: str) -> None:
        """Raise ``NotImplementedError`` if ``capability`` is not supported.

        Args:
            capability: Capability the registry is requesting.

        Raises:
            NotImplementedError: If ``capability`` is not in :attr:`supported`.
        """
        if capability not in self.supported:
            raise NotImplementedError(
                f"Provider {self.name!r} does not support capability {capability!r}."
            )

    @abstractmethod
    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize data for ``capability``.

        Implementations MUST:
          * raise :class:`~makecrazypenny.core.errors.MissingApiKey` when
            :attr:`requires_key` is set but the key is absent;
          * raise ``NotImplementedError`` when ``capability`` is not supported;
          * return a normalized core type's ``to_dict()`` output (or a list of
            them) — already JSON-serializable.

        Args:
            capability: One of the FROZEN capability names.
            **params: Capability-specific parameters (e.g. ``symbol``).

        Returns:
            A JSON-serializable normalized result.
        """
        raise NotImplementedError


__all__ = ["Provider", "PROVIDER_REGISTRY", "register_provider"]
