"""Financial Modeling Prep (FMP) provider adapter (see CONTRACT.md §8.7).

FMP serves analyst- and disclosure-oriented capabilities for MakeCrazyPenny:

  * ``congress_trades``     — Senate + House trading disclosures (merged).
  * ``analyst_ratings``     — analyst recommendation distribution.
  * ``price_targets``       — price-target consensus.
  * ``upgrades_downgrades`` — rating-change events.
  * ``fundamentals``        — key metrics + financial ratios.

All HTTP access goes to ``financialmodelingprep.com/api`` via ``httpx`` and
requires the ``FMP_API_KEY`` environment variable. Following the global
engineering mandates (CONTRACT.md §2):

  * ``httpx`` is lazy-imported inside :meth:`fetch` so importing this module
    never requires the library and never hits the network.
  * A missing key raises :class:`~makecrazypenny.core.errors.MissingApiKey` so
    the registry falls through the capability chain.
  * An unsupported capability raises ``NotImplementedError`` so the registry
    skips this provider.
  * Every raw payload is normalized into the matching ``core/types`` dataclass
    and returned as ``to_dict()`` output (JSON-serializable).
"""

from __future__ import annotations

from typing import Any

from ..core.types import (
    AnalystRating,
    CongressTrade,
    PriceTarget,
    Provenance,
    UpgradeDowngrade,
    utcnow_iso,
)
from .base import Provider, register_provider

# Stable v3 base; capability methods append their endpoint path.
_BASE_URL = "https://financialmodelingprep.com/api/v3"
_STABLE_URL = "https://financialmodelingprep.com/api/v4"
_TIMEOUT_S = 20.0


def _to_float(value: Any) -> float | None:
    """Best-effort coercion of an FMP numeric field to ``float`` (or ``None``)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int:
    """Best-effort coercion of an FMP count field to ``int`` (defaulting to 0)."""
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    """Return the first present, non-empty string value among ``keys``."""
    for key in keys:
        val = payload.get(key)
        if val not in (None, ""):
            return str(val)
    return None


@register_provider
class FMPProvider(Provider):
    """Financial Modeling Prep adapter (analyst + congress + fundamentals)."""

    name = "fmp"
    supported = {
        "congress_trades",
        "analyst_ratings",
        "price_targets",
        "upgrades_downgrades",
        "fundamentals",
    }
    rate_per_min = 0  # free tier; the registry's bucket treats 0 as unlimited.
    cost = 1
    requires_key = "FMP_API_KEY"

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from FMP.

        Args:
            capability: One of :attr:`supported`.
            **params: Capability-specific parameters (e.g. ``symbol``).

        Returns:
            A normalized core type's ``to_dict()`` output, or a list thereof.

        Raises:
            MissingApiKey: If ``FMP_API_KEY`` is absent.
            NotImplementedError: If ``capability`` is not supported.
        """
        self.ensure_supported(capability)
        key = self.api_key()  # raises MissingApiKey when absent

        # Lazy-import httpx so module import never requires the library.
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            if capability == "congress_trades":
                return await self._congress_trades(client, key, **params)
            if capability == "analyst_ratings":
                return await self._analyst_ratings(client, key, **params)
            if capability == "price_targets":
                return await self._price_targets(client, key, **params)
            if capability == "upgrades_downgrades":
                return await self._upgrades_downgrades(client, key, **params)
            if capability == "fundamentals":
                return await self._fundamentals(client, key, **params)

        # Unreachable: ensure_supported guards the capability set.
        raise NotImplementedError(
            f"Provider {self.name!r} does not support capability {capability!r}."
        )

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    async def _get_json(
        self,
        client: Any,
        url: str,
        key: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET ``url`` with the API key attached, returning parsed JSON.

        Raises:
            httpx.HTTPStatusError: On a non-2xx response (the registry treats
                this as a genuine failure and trips the circuit breaker).
        """
        query: dict[str, Any] = dict(params or {})
        query["apikey"] = key
        resp = await client.get(url, params=query)
        resp.raise_for_status()
        return resp.json()

    def _provenance(self) -> Provenance:
        """Provenance stamped at fetch time (``cached=False`` per §8.7)."""
        return Provenance(provider=self.name, fetched_at=utcnow_iso(), cached=False)

    # ------------------------------------------------------------------ #
    # congress_trades — Senate + House trading, merged
    # ------------------------------------------------------------------ #

    async def _congress_trades(
        self, client: Any, key: str, *, symbol: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        """Fetch Senate + House disclosures (optionally for one symbol) and merge."""
        if symbol:
            sym = symbol.upper().strip()
            senate_url = f"{_STABLE_URL}/senate-trading"
            house_url = f"{_STABLE_URL}/house-trading"
            senate_params: dict[str, Any] = {"symbol": sym}
            house_params: dict[str, Any] = {"symbol": sym}
        else:
            sym = None
            senate_url = f"{_STABLE_URL}/senate-trading-rss-feed"
            house_url = f"{_STABLE_URL}/house-trading-rss-feed"
            senate_params = {"page": 0}
            house_params = {"page": 0}

        trades: list[CongressTrade] = []
        for chamber, url, qp in (
            ("Senate", senate_url, senate_params),
            ("House", house_url, house_params),
        ):
            try:
                rows = await self._get_json(client, url, key, qp)
            except Exception:
                # One chamber failing should not lose the other; skip it.
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    trades.append(self._normalize_congress_trade(row, chamber, sym))

        return [t.to_dict() for t in trades]

    def _normalize_congress_trade(
        self, row: dict[str, Any], chamber: str, fallback_symbol: str | None
    ) -> CongressTrade:
        """Map a single FMP senate/house trading row to ``CongressTrade``."""
        symbol = _first_str(row, "symbol", "ticker") or (fallback_symbol or "")
        first = _first_str(row, "firstName", "first_name") or ""
        last = _first_str(row, "lastName", "last_name") or ""
        member = (
            _first_str(row, "representative", "office", "senator", "member")
            or f"{first} {last}".strip()
            or "Unknown"
        )
        transaction = _first_str(row, "type", "transactionType", "transaction") or "unknown"
        amount = _first_str(row, "amount", "range", "amountRange")
        tx_date = _first_str(row, "transactionDate", "transaction_date", "dateRecieved", "date")
        disc_date = _first_str(
            row, "disclosureDate", "disclosure_date", "dateReceived", "filingDate"
        )
        return CongressTrade(
            symbol=symbol.upper() if symbol else "",
            member=member,
            chamber=chamber,
            transaction=transaction,
            amount_range=amount,
            transaction_date=tx_date,
            disclosure_date=disc_date,
            provenance=self._provenance(),
        )

    # ------------------------------------------------------------------ #
    # analyst_ratings — recommendation distribution
    # ------------------------------------------------------------------ #

    async def _analyst_ratings(
        self, client: Any, key: str, *, symbol: str, **_: Any
    ) -> list[dict[str, Any]]:
        """Fetch analyst recommendation distribution for ``symbol``."""
        sym = symbol.upper().strip()
        url = f"{_BASE_URL}/analyst-stock-recommendations/{sym}"
        rows = await self._get_json(client, url, key)
        if not isinstance(rows, list):
            rows = []
        ratings = [
            self._normalize_analyst_rating(row, sym)
            for row in rows
            if isinstance(row, dict)
        ]
        return [r.to_dict() for r in ratings]

    def _normalize_analyst_rating(self, row: dict[str, Any], symbol: str) -> AnalystRating:
        """Map an FMP ``analyst-stock-recommendations`` row to ``AnalystRating``."""
        period = _first_str(row, "date", "period") or ""
        return AnalystRating(
            symbol=_first_str(row, "symbol") or symbol,
            period=period,
            strong_buy=_to_int(row.get("analystRatingsStrongBuy")),
            buy=_to_int(row.get("analystRatingsbuy") or row.get("analystRatingsBuy")),
            hold=_to_int(row.get("analystRatingsHold")),
            sell=_to_int(row.get("analystRatingsSell")),
            strong_sell=_to_int(row.get("analystRatingsStrongSell")),
            provenance=self._provenance(),
        )

    # ------------------------------------------------------------------ #
    # price_targets — consensus
    # ------------------------------------------------------------------ #

    async def _price_targets(
        self, client: Any, key: str, *, symbol: str, **_: Any
    ) -> dict[str, Any]:
        """Fetch price-target consensus for ``symbol``."""
        sym = symbol.upper().strip()
        url = f"{_STABLE_URL}/price-target-consensus"
        data = await self._get_json(client, url, key, {"symbol": sym})
        # FMP returns a list with a single consensus object (or an empty list).
        row: dict[str, Any] = {}
        if isinstance(data, list) and data and isinstance(data[0], dict):
            row = data[0]
        elif isinstance(data, dict):
            row = data
        return self._normalize_price_target(row, sym).to_dict()

    def _normalize_price_target(self, row: dict[str, Any], symbol: str) -> PriceTarget:
        """Map an FMP ``price-target-consensus`` row to ``PriceTarget``."""
        return PriceTarget(
            symbol=_first_str(row, "symbol") or symbol,
            mean=_to_float(
                row.get("targetConsensus")
                if row.get("targetConsensus") is not None
                else row.get("targetMedian")
            ),
            high=_to_float(row.get("targetHigh")),
            low=_to_float(row.get("targetLow")),
            current=_to_float(row.get("lastPrice") or row.get("currentPrice")),
            provenance=self._provenance(),
        )

    # ------------------------------------------------------------------ #
    # upgrades_downgrades — rating-change events
    # ------------------------------------------------------------------ #

    async def _upgrades_downgrades(
        self, client: Any, key: str, *, symbol: str, **_: Any
    ) -> list[dict[str, Any]]:
        """Fetch analyst upgrade/downgrade events for ``symbol``."""
        sym = symbol.upper().strip()
        url = f"{_STABLE_URL}/upgrades-downgrades"
        rows = await self._get_json(client, url, key, {"symbol": sym})
        if not isinstance(rows, list):
            rows = []
        events = [
            self._normalize_upgrade_downgrade(row, sym)
            for row in rows
            if isinstance(row, dict)
        ]
        return [e.to_dict() for e in events]

    def _normalize_upgrade_downgrade(
        self, row: dict[str, Any], symbol: str
    ) -> UpgradeDowngrade:
        """Map an FMP ``upgrades-downgrades`` row to ``UpgradeDowngrade``."""
        from_grade = _first_str(row, "previousGrade", "fromGrade", "from_grade")
        to_grade = _first_str(row, "newGrade", "toGrade", "to_grade")
        action = (
            _first_str(row, "action", "gradingAction")
            or self._infer_action(from_grade, to_grade)
        )
        return UpgradeDowngrade(
            symbol=_first_str(row, "symbol") or symbol,
            firm=_first_str(row, "gradingCompany", "firm", "analystCompany") or "Unknown",
            from_grade=from_grade,
            to_grade=to_grade,
            action=action,
            date=_first_str(row, "publishedDate", "date"),
            provenance=self._provenance(),
        )

    @staticmethod
    def _infer_action(from_grade: str | None, to_grade: str | None) -> str:
        """Infer an action label when FMP does not supply one explicitly."""
        if from_grade and to_grade and from_grade != to_grade:
            return "change"
        if to_grade and not from_grade:
            return "initiate"
        return "maintain"

    # ------------------------------------------------------------------ #
    # fundamentals — key metrics + ratios
    # ------------------------------------------------------------------ #

    async def _fundamentals(
        self, client: Any, key: str, *, symbol: str, period: str = "annual", **_: Any
    ) -> dict[str, Any]:
        """Fetch key metrics + ratios for ``symbol`` (merged into one dict).

        There is no dedicated ``fundamentals`` core dataclass, so this returns a
        normalized plain dict (already JSON-serializable) carrying the latest
        key-metrics and ratios fields plus provenance.
        """
        sym = symbol.upper().strip()
        qp = {"period": period, "limit": 1}
        metrics_url = f"{_BASE_URL}/key-metrics/{sym}"
        ratios_url = f"{_BASE_URL}/ratios/{sym}"

        key_metrics: dict[str, Any] = {}
        ratios: dict[str, Any] = {}

        try:
            km = await self._get_json(client, metrics_url, key, qp)
            if isinstance(km, list) and km and isinstance(km[0], dict):
                key_metrics = km[0]
        except Exception:
            key_metrics = {}

        try:
            rt = await self._get_json(client, ratios_url, key, qp)
            if isinstance(rt, list) and rt and isinstance(rt[0], dict):
                ratios = rt[0]
        except Exception:
            ratios = {}

        return {
            "symbol": sym,
            "period": _first_str(key_metrics, "period") or period,
            "date": _first_str(key_metrics, "date") or _first_str(ratios, "date"),
            "key_metrics": key_metrics,
            "ratios": ratios,
            "provenance": self._provenance().to_dict(),
        }


__all__ = ["FMPProvider"]
