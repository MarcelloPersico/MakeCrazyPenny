"""CoinGecko provider (CONTRACT.md §16).

Keyless source of **global crypto-market context** — total market cap, total
volume, and BTC/ETH dominance — plus a coin price fallback. An optional demo API
key (``COINGECKO_API_KEY``) lifts the rate limit but is never required. Talks to
``api.coingecko.com/api/v3`` over ``httpx``.

Capabilities: ``crypto_global``, ``crypto_quote`` (fallback). ``httpx`` is
imported lazily so importing this module never hits the network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.symbols import base_asset
from ..core.types import CryptoGlobal, Provenance, Quote, utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "coingecko"
_BASE_URL = "https://api.coingecko.com/api/v3"


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@register_provider
class CoinGeckoProvider(Provider):
    """CoinGecko REST adapter (keyless; optional demo key)."""

    name = _PROVIDER_NAME
    supported = {"crypto_global", "crypto_quote"}
    rate_per_min = 10  # keyless public tier is 5-15/min; stay conservative
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """GET a CoinGecko endpoint, attaching the demo key header when present."""
        import httpx  # lazy import (CONTRACT.md §2.2)

        headers: dict[str, str] = {}
        key = self.settings.coingecko_api_key
        if key:
            headers["x-cg-demo-api-key"] = key
        async with httpx.AsyncClient(base_url=_BASE_URL, timeout=20.0, headers=headers) as client:
            response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
            response.raise_for_status()
            return response.json()

    def _provenance(self) -> Provenance:
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from CoinGecko."""
        self.ensure_supported(capability)
        if capability == "crypto_global":
            return await self._fetch_global(**params)
        if capability == "crypto_quote":
            return await self._fetch_quote(**params)
        raise NotImplementedError(
            f"Provider {self.name!r} does not support capability {capability!r}."
        )

    async def _fetch_global(self, **_: Any) -> dict[str, Any]:
        """Normalize ``/global`` into :class:`CryptoGlobal`."""
        payload = await self._get("/global", {})
        data = payload.get("data") if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        mcap = data.get("total_market_cap") or {}
        vol = data.get("total_volume") or {}
        dom = data.get("market_cap_percentage") or {}
        return CryptoGlobal(
            total_market_cap=_to_float(mcap.get("usd")) if isinstance(mcap, dict) else None,
            total_volume=_to_float(vol.get("usd")) if isinstance(vol, dict) else None,
            btc_dominance=_to_float(dom.get("btc")) if isinstance(dom, dict) else None,
            eth_dominance=_to_float(dom.get("eth")) if isinstance(dom, dict) else None,
            market_cap_change_24h=_to_float(data.get("market_cap_change_percentage_24h_usd")),
            provenance=self._provenance(),
        ).to_dict()

    async def _fetch_quote(self, symbol: str, **_: Any) -> dict[str, Any]:
        """Normalize ``/coins/markets`` (by symbol) into :class:`Quote`."""
        base = base_asset(symbol).lower()
        payload = await self._get(
            "/coins/markets", {"vs_currency": "usd", "symbols": base, "per_page": 1, "page": 1}
        )
        row = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else {}
        price = _to_float(row.get("current_price"))
        if price is None:
            raise ValueError(f"CoinGecko returned no price for {base!r}")
        return Quote(
            symbol=base.upper(),
            price=price,
            change=_to_float(row.get("price_change_24h")),
            change_pct=_to_float(row.get("price_change_percentage_24h")),
            provenance=self._provenance(),
        ).to_dict()


__all__ = ["CoinGeckoProvider"]
