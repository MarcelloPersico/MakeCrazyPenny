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
# Crypto layer (see CONTRACT.md §16). Perpetual-futures derivatives metrics that
# have no equity analogue: funding rate, open interest, long/short positioning,
# and global market context. Plain value objects — no SDK, no I/O.
# ---------------------------------------------------------------------------


@dataclass
class FundingRate:
    """The current perpetual-swap funding rate for a symbol.

    ``rate`` is the per-interval funding rate (e.g. ``0.0001`` = 1bp every
    ``interval_hours``). Positive => longs pay shorts (crowded long).
    """

    symbol: str
    rate: float
    mark_price: float | None = None
    index_price: float | None = None
    next_funding_time: str | None = None
    interval_hours: float = 8.0
    provenance: Provenance | None = None

    def annualized(self) -> float:
        """Annualize the funding rate (``rate`` per interval -> per year)."""
        per_day = (24.0 / self.interval_hours) if self.interval_hours else 3.0
        return self.rate * per_day * 365.0

    def basis(self) -> float | None:
        """Mark-vs-index premium as a fraction (perp basis), or ``None``."""
        if self.mark_price and self.index_price and self.index_price > 0:
            return self.mark_price / self.index_price - 1.0
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "rate": self.rate,
            "annualized": self.annualized(),
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "basis": self.basis(),
            "next_funding_time": self.next_funding_time,
            "interval_hours": self.interval_hours,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class OpenInterest:
    """Open interest (sum of outstanding contracts) for a perpetual symbol."""

    symbol: str
    open_interest: float
    value: float | None = None  # notional value in quote currency (e.g. USDT)
    ts: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "open_interest": self.open_interest,
            "value": self.value,
            "ts": self.ts,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class LongShortRatio:
    """The aggregate long/short account (or position) ratio for a symbol.

    ``ratio`` is longs/shorts (>1 => more longs). ``long_pct``/``short_pct`` are
    fractions in ``[0, 1]`` when the source provides them.
    """

    symbol: str
    ratio: float | None = None
    long_pct: float | None = None
    short_pct: float | None = None
    ts: str | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "symbol": self.symbol,
            "ratio": self.ratio,
            "long_pct": self.long_pct,
            "short_pct": self.short_pct,
            "ts": self.ts,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass
class CryptoGlobal:
    """Global crypto-market context (total cap, volume, BTC/ETH dominance)."""

    total_market_cap: float | None = None
    total_volume: float | None = None
    btc_dominance: float | None = None
    eth_dominance: float | None = None
    market_cap_change_24h: float | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "total_market_cap": self.total_market_cap,
            "total_volume": self.total_volume,
            "btc_dominance": self.btc_dominance,
            "eth_dominance": self.eth_dominance,
            "market_cap_change_24h": self.market_cap_change_24h,
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
    sizing: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    #: ``"equity"`` (default) or ``"crypto"`` — which engine produced this.
    asset_class: str = "equity"
    #: Leverage-aware plan (liquidation price, suggested leverage, funding cost,
    #: notional/margin %). Empty for unlevered equity decisions (see §16).
    leverage: dict[str, Any] = field(default_factory=dict)
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
            "sizing": dict(self.sizing),
            "regime": dict(self.regime),
            "asset_class": self.asset_class,
            "leverage": dict(self.leverage),
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "note": self.note,
            "disclaimer": self.disclaimer,
        }


@dataclass
class SectorScan:
    """A sector-wide aggregation of per-ticker decisions (see CONTRACT.md §10.5).

    Produced by :func:`makecrazypenny.orchestration.market.scan_sector`: the
    deterministic decision engine is run on each constituent and the results are
    aggregated into a sector read — a ``stance``, breadth statistics, and ranked
    long/short ideas.

    Attributes:
        sector: The canonical sector name analysed.
        stance: ``"overweight"`` / ``"underweight"`` / ``"neutral"`` — the
            sector-level tilt from breadth + net momentum.
        n_requested: How many constituents were requested (after any ``limit``).
        n_analyzed: How many produced a decision (the rest are in ``errors``).
        net_tilt: Mean quant ``net_score`` across analysed names (sector momentum).
        avg_conviction: Mean conviction across analysed names.
        breadth: Counts + percentages: ``buy``/``short``/``avoid`` and
            ``bullish_pct``/``bearish_pct``.
        rankings: All analysed names as compact dicts, sorted most→least bullish.
        top_longs: The strongest BUY ideas (compact dicts).
        top_shorts: The strongest SHORT ideas (compact dicts).
        errors: ``{"symbol", "error"}`` for any constituent that failed.
        method: Decision method tag (``"quant"``).
        summary: One-line human-readable sector verdict.
        disclaimer: The not-investment-advice disclaimer (always present).
    """

    sector: str
    stance: str = "neutral"
    n_requested: int = 0
    n_analyzed: int = 0
    net_tilt: float = 0.0
    avg_conviction: float = 0.0
    breadth: dict[str, Any] = field(default_factory=dict)
    rankings: list[dict[str, Any]] = field(default_factory=list)
    top_longs: list[dict[str, Any]] = field(default_factory=list)
    top_shorts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    method: str = "quant"
    summary: str = ""
    disclaimer: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "sector": self.sector,
            "stance": self.stance,
            "n_requested": self.n_requested,
            "n_analyzed": self.n_analyzed,
            "net_tilt": self.net_tilt,
            "avg_conviction": self.avg_conviction,
            "breadth": dict(self.breadth),
            "rankings": [dict(r) for r in self.rankings],
            "top_longs": [dict(r) for r in self.top_longs],
            "top_shorts": [dict(r) for r in self.top_shorts],
            "errors": [dict(e) for e in self.errors],
            "method": self.method,
            "summary": self.summary,
            "disclaimer": self.disclaimer,
        }


@dataclass
class MarketScreen:
    """A whole-universe screen funnelled down to the best trade ideas.

    Produced by :func:`makecrazypenny.orchestration.screen.screen_market`: a cheap
    price-factor **prefilter** ranks the entire universe (e.g. the S&P 500), the
    strongest long and short candidates are shortlisted, and the full decision
    engine is run only on those survivors. The result surfaces the best long and
    short ideas — each a complete :class:`TradeDecision` (with sizing, stop/target,
    regime, and invalidation) so the user sees not just *what* to trade but *how*.

    Attributes:
        universe: Label for the screened universe (e.g. ``"S&P 500"``).
        universe_source: How the constituent list was obtained
            (``"live"`` / ``"cache"`` / ``"fallback"``).
        universe_count: How many constituents the universe held.
        as_of: When the universe list was sourced (ISO-8601, or ``None``).
        n_prefiltered: How many names produced a valid prefilter score.
        n_evaluated: How many shortlisted names got a full decision.
        regime: The market regime read (risk-on/off + gross-exposure scalar).
        top_longs: The best BUY ideas, each a full ``TradeDecision`` dict.
        top_shorts: The best SHORT ideas, each a full ``TradeDecision`` dict.
        long_shortlist: Compact prefilter entries that fed the long deep-dive.
        short_shortlist: Compact prefilter entries that fed the short deep-dive.
        errors: ``{"symbol", "error"}`` for any name that failed (capped).
        method: Decision method tag (``"quant"``).
        summary: One-line human-readable verdict.
        disclaimer: The not-investment-advice disclaimer (always present).
    """

    universe: str = "S&P 500"
    universe_source: str = ""
    universe_count: int = 0
    as_of: str | None = None
    n_prefiltered: int = 0
    n_evaluated: int = 0
    regime: dict[str, Any] = field(default_factory=dict)
    top_longs: list[dict[str, Any]] = field(default_factory=list)
    top_shorts: list[dict[str, Any]] = field(default_factory=list)
    long_shortlist: list[dict[str, Any]] = field(default_factory=list)
    short_shortlist: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    method: str = "quant"
    summary: str = ""
    disclaimer: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "universe": self.universe,
            "universe_source": self.universe_source,
            "universe_count": self.universe_count,
            "as_of": self.as_of,
            "n_prefiltered": self.n_prefiltered,
            "n_evaluated": self.n_evaluated,
            "regime": dict(self.regime),
            "top_longs": [dict(r) for r in self.top_longs],
            "top_shorts": [dict(r) for r in self.top_shorts],
            "long_shortlist": [dict(r) for r in self.long_shortlist],
            "short_shortlist": [dict(r) for r in self.short_shortlist],
            "errors": [dict(e) for e in self.errors],
            "method": self.method,
            "summary": self.summary,
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
    "FundingRate",
    "OpenInterest",
    "LongShortRatio",
    "CryptoGlobal",
    "DebateArgument",
    "DebateTranscript",
    "TradeDecision",
    "SectorScan",
    "MarketScreen",
    "to_dict",
    "utcnow_iso",
]
