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


# ---------------------------------------------------------------------------
# Decision layer (see CONTRACT.md §5.1, §10.3). The debate-driven decision
# engine turns the read-only evidence dossier into an explicit, autonomous
# trade decision. These are plain value objects too — no SDK, no I/O.
# ---------------------------------------------------------------------------


@dataclass
class DebateArgument:
    """One side's argument in a single debate round.

    Attributes:
        side: ``"bull"`` or ``"bear"`` — which case this argument makes.
        round: 1-based round index (round 1 = opening; >1 = rebuttal).
        thesis: The argument's headline claim (one or two sentences).
        key_points: The supporting points, each tied to concrete evidence.
        cited_evidence: Short references to the dossier items relied on
            (e.g. ``"RSI 24 (oversold)"``, ``"consensus 18 buy / 2 sell"``).
        conviction: The advocate's self-rated strength of its own case in
            ``[0, 1]`` (``None`` if it did not provide one).
        rebuts: For rebuttal rounds, the opponent points this argument answers.
    """

    side: str
    round: int
    thesis: str
    key_points: list[str] = field(default_factory=list)
    cited_evidence: list[str] = field(default_factory=list)
    conviction: float | None = None
    rebuts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "side": self.side,
            "round": self.round,
            "thesis": self.thesis,
            "key_points": list(self.key_points),
            "cited_evidence": list(self.cited_evidence),
            "conviction": self.conviction,
            "rebuts": list(self.rebuts),
        }


@dataclass
class DebateTranscript:
    """The full bull-vs-bear debate for one symbol.

    Attributes:
        symbol: The ticker debated.
        rounds: Number of rounds actually run.
        arguments: All arguments in chronological order (bull/bear interleaved).
    """

    symbol: str
    rounds: int = 0
    arguments: list[DebateArgument] = field(default_factory=list)

    def for_side(self, side: str) -> list[DebateArgument]:
        """Return this side's arguments in round order."""
        return [a for a in self.arguments if a.side == side]

    def latest(self, side: str) -> DebateArgument | None:
        """Return the most recent argument for ``side`` (or ``None``)."""
        args = self.for_side(side)
        return args[-1] if args else None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "rounds": self.rounds,
            "arguments": [a.to_dict() for a in self.arguments],
        }


@dataclass
class TradeDecision:
    """An autonomous trade decision for one symbol (see CONTRACT.md §10.3).

    The decision space is intentionally small and unambiguous:

      * ``action`` ∈ {``"BUY"``, ``"SHORT"``, ``"AVOID"``} — what to do.
      * ``direction`` ∈ {``"LONG"``, ``"SHORT"``, ``"FLAT"``} — the resulting
        exposure.

    Attributes:
        symbol: The ticker decided on.
        action: ``"BUY"`` (open long), ``"SHORT"`` (open short), or ``"AVOID"``.
        direction: ``"LONG"`` / ``"SHORT"`` / ``"FLAT"`` (mirrors ``action``).
        conviction: Confidence in ``[0, 1]``.
        horizon: Intended holding horizon
            (``"intraday"`` / ``"swing"`` / ``"position"`` / ``"long_term"``).
        suggested_sizing: Qualitative position size (e.g. ``"small"`` when
            uncertainty is high). Never a dollar amount — this is not advice.
        summary: One-line human-readable verdict.
        rationale: The decisive reasons behind the verdict.
        bull_case: The strongest points for going long.
        bear_case: The strongest points against / for shorting.
        risks: What could go wrong with this decision.
        invalidation: The condition that would flip the thesis.
        net_score: Quant backbone net score (positive = bullish).
        bull_score: Sum of bullish quant contributions.
        bear_score: Sum of bearish quant contributions.
        factors: The per-factor quant breakdown (deterministic backbone).
        method: ``"quant"`` (the deterministic baseline) or ``"debate"`` (the
            host's bull/bear/judge verdict merged with the quant backbone via the
            ``finalize_decision`` MCP tool).
        data_quality: Coverage / missing-data / divergence notes affecting trust.
        transcript: An optional structured debate transcript. The debate itself
            runs in the MCP host, so this is usually ``None``.
        note: Optional operational note.
        disclaimer: The not-investment-advice disclaimer (always present).
    """

    symbol: str
    action: str
    direction: str
    conviction: float
    horizon: str = "swing"
    suggested_sizing: str = "small"
    summary: str = ""
    rationale: list[str] = field(default_factory=list)
    bull_case: list[str] = field(default_factory=list)
    bear_case: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidation: str | None = None
    net_score: float = 0.0
    bull_score: float = 0.0
    bear_score: float = 0.0
    factors: list[dict[str, Any]] = field(default_factory=list)
    method: str = "quant-only"
    data_quality: dict[str, Any] = field(default_factory=dict)
    transcript: DebateTranscript | None = None
    note: str | None = None
    disclaimer: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "action": self.action,
            "direction": self.direction,
            "conviction": self.conviction,
            "horizon": self.horizon,
            "suggested_sizing": self.suggested_sizing,
            "summary": self.summary,
            "rationale": list(self.rationale),
            "bull_case": list(self.bull_case),
            "bear_case": list(self.bear_case),
            "risks": list(self.risks),
            "invalidation": self.invalidation,
            "net_score": self.net_score,
            "bull_score": self.bull_score,
            "bear_score": self.bear_score,
            "factors": [dict(f) for f in self.factors],
            "method": self.method,
            "data_quality": dict(self.data_quality),
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "note": self.note,
            "disclaimer": self.disclaimer,
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
    "DebateArgument",
    "DebateTranscript",
    "TradeDecision",
    "to_dict",
    "utcnow_iso",
]
