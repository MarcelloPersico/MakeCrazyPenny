"""Core value types (see CONTRACT.md §5).

Plain ``@dataclass`` value objects shared across all layers. Frozen where the
object is a natural immutable value (``Provenance``, ``OHLCVBar``); mutable
where it may be assembled incrementally (``OHLCV``).

Conventions:
  * Every optional field defaults to ``None``.
  * Every type exposes ``to_dict()`` returning a JSON-serializable ``dict``
    (nested dataclasses are recursively converted).
  * ``from_provider`` / normalizer classmethods are provided where a provider's
    raw payload needs mapping into the type.

Importing this module pulls in only the standard library.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (e.g. for ``fetched_at``)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Provenance:
    """Where a piece of data came from and when it was fetched.

    Attributes:
        provider: Provider ``name`` that produced the data (e.g. ``"finnhub"``).
        fetched_at: ISO-8601 UTC timestamp of the fetch.
        cached: Whether the value was served from cache. Providers set this to
            ``False`` at fetch time; the registry/cache reports the true cached
            status separately in its envelope.
    """

    provider: str
    fetched_at: str = field(default_factory=utcnow_iso)
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "cached": self.cached,
        }


@dataclass(frozen=True)
class OHLCVBar:
    """A single open/high/low/close/volume bar."""

    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class OHLCV:
    """A series of OHLCV bars for one symbol/interval."""

    symbol: str
    interval: str
    bars: list[OHLCVBar] = field(default_factory=list)
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "bars": [b.to_dict() for b in self.bars],
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class Quote:
    """A point-in-time price quote."""

    symbol: str
    price: float
    change: float | None = None
    change_pct: float | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change": self.change,
            "change_pct": self.change_pct,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class NewsItem:
    """A single news article reference."""

    symbol: str
    headline: str
    source: str | None = None
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "headline": self.headline,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at,
            "summary": self.summary,
        }


@dataclass
class SentimentScore:
    """An aggregated sentiment score for a symbol.

    ``score`` is clamped to ``[-1.0, 1.0]`` by :meth:`normalize`.
    """

    symbol: str
    score: float
    label: str
    n_articles: int = 0
    drivers: list[str] = field(default_factory=list)
    provenance: Provenance | None = None

    @staticmethod
    def clamp_score(score: float) -> float:
        """Clamp a raw score into the inclusive range ``[-1.0, 1.0]``."""
        return max(-1.0, min(1.0, float(score)))

    @classmethod
    def normalize(
        cls,
        *,
        symbol: str,
        score: float,
        label: str,
        n_articles: int = 0,
        drivers: list[str] | None = None,
        provenance: Provenance | None = None,
    ) -> "SentimentScore":
        """Build a ``SentimentScore`` with its ``score`` clamped to ``[-1, 1]``."""
        return cls(
            symbol=symbol,
            score=cls.clamp_score(score),
            label=label,
            n_articles=n_articles,
            drivers=list(drivers) if drivers else [],
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation (score clamped)."""
        return {
            "symbol": self.symbol,
            "score": self.clamp_score(self.score),
            "label": self.label,
            "n_articles": self.n_articles,
            "drivers": list(self.drivers),
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class CongressTrade:
    """A congressional stock trade disclosure."""

    symbol: str
    member: str
    chamber: str
    transaction: str
    amount_range: str | None = None
    transaction_date: str | None = None
    disclosure_date: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "member": self.member,
            "chamber": self.chamber,
            "transaction": self.transaction,
            "amount_range": self.amount_range,
            "transaction_date": self.transaction_date,
            "disclosure_date": self.disclosure_date,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class InsiderTransaction:
    """An insider (officer/director/10%-owner) transaction."""

    symbol: str
    insider: str
    transaction: str
    role: str | None = None
    shares: float | None = None
    value: float | None = None
    date: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "insider": self.insider,
            "role": self.role,
            "transaction": self.transaction,
            "shares": self.shares,
            "value": self.value,
            "date": self.date,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class AnalystRating:
    """Analyst recommendation distribution for one period."""

    symbol: str
    period: str
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "strong_buy": self.strong_buy,
            "buy": self.buy,
            "hold": self.hold,
            "sell": self.sell,
            "strong_sell": self.strong_sell,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class PriceTarget:
    """Analyst price-target summary."""

    symbol: str
    mean: float | None = None
    high: float | None = None
    low: float | None = None
    current: float | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "mean": self.mean,
            "high": self.high,
            "low": self.low,
            "current": self.current,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class UpgradeDowngrade:
    """A single analyst rating-change event."""

    symbol: str
    firm: str
    action: str
    from_grade: str | None = None
    to_grade: str | None = None
    date: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "firm": self.firm,
            "from_grade": self.from_grade,
            "to_grade": self.to_grade,
            "action": self.action,
            "date": self.date,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class Filing:
    """An SEC filing reference."""

    symbol: str
    form: str
    title: str | None = None
    filed_at: str | None = None
    url: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "form": self.form,
            "title": self.title,
            "filed_at": self.filed_at,
            "url": self.url,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


def to_dict(obj: Any) -> Any:
    """Best-effort conversion of a core value object (or list thereof) to JSON.

    Calls ``obj.to_dict()`` when available, recurses into lists/tuples, and
    falls back to :func:`dataclasses.asdict` for plain dataclasses.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_dict(item) for item in obj]
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    try:
        return asdict(obj)
    except TypeError:
        return obj


__all__ = [
    "Provenance",
    "OHLCVBar",
    "OHLCV",
    "Quote",
    "NewsItem",
    "SentimentScore",
    "CongressTrade",
    "InsiderTransaction",
    "AnalystRating",
    "PriceTarget",
    "UpgradeDowngrade",
    "Filing",
    "to_dict",
    "utcnow_iso",
]
