"""Crypto Fear & Greed Index provider (CONTRACT.md §16).

Keyless market-wide sentiment from Alternative.me's Crypto Fear & Greed Index
(0 = extreme fear, 100 = extreme greed). Mapped onto the shared
:class:`~makecrazypenny.core.types.SentimentScore` (``score`` in ``[-1, 1]``) so
the crypto engine can treat it like any other sentiment input — most useful as a
*contrarian* signal at the extremes.

Capability: ``crypto_sentiment``. No API key required. ``httpx`` is imported
lazily so importing this module never hits the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..core.types import Provenance, SentimentScore, utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import Settings

_PROVIDER_NAME = "fear_greed"
_BASE_URL = "https://api.alternative.me"


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on missing/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@register_provider
class FearGreedProvider(Provider):
    """Alternative.me Crypto Fear & Greed Index adapter (keyless)."""

    name = _PROVIDER_NAME
    supported = {"crypto_sentiment"}
    rate_per_min = 60
    requires_key = None

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.rate_key = _PROVIDER_NAME

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        import httpx  # lazy import (CONTRACT.md §2.2)

        async with httpx.AsyncClient(base_url=_BASE_URL, timeout=20.0) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    def _provenance(self) -> Provenance:
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize the Fear & Greed Index into :class:`SentimentScore`."""
        self.ensure_supported(capability)
        return await self._fetch_sentiment(**params)

    async def _fetch_sentiment(self, **_: Any) -> dict[str, Any]:
        """Map the latest index value (0..100) to a ``[-1, 1]`` sentiment score.

        Pulls two points so a short-term trend (rising/falling) can be noted in
        the drivers. ``score = (value - 50) / 50`` (50 = neutral).
        """
        payload = await self._get("/fng/", {"limit": 2, "format": "json"})
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data if isinstance(data, list) else []
        latest = rows[0] if rows and isinstance(rows[0], dict) else {}
        value = _to_float(latest.get("value"))
        classification = str(latest.get("value_classification") or "neutral")

        if value is None:
            raise ValueError("Fear & Greed Index returned no value")

        score = (value - 50.0) / 50.0
        drivers = [f"Fear&Greed {int(value)}/100 ({classification})"]
        prev = _to_float(rows[1].get("value")) if len(rows) > 1 and isinstance(rows[1], dict) else None
        if prev is not None:
            trend = "rising" if value > prev else "falling" if value < prev else "flat"
            drivers.append(f"trend {trend} (prev {int(prev)})")

        ts = latest.get("timestamp")
        ts_iso: str | None = None
        f_ts = _to_float(ts)
        if f_ts is not None:
            try:
                ts_iso = datetime.fromtimestamp(f_ts, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                ts_iso = None

        result = SentimentScore.normalize(
            symbol="CRYPTO",
            score=score,
            label=classification.lower(),
            n_articles=0,
            drivers=drivers,
            provenance=self._provenance(),
        ).to_dict()
        result["value"] = value
        result["as_of"] = ts_iso
        return result


__all__ = ["FearGreedProvider"]
