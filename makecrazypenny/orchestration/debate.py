"""Layer 2: the deterministic decision engine (CONTRACT.md §10.3).

Turns the read-only *evidence* the capability servers produce into an explicit
**trade decision** — ``BUY`` (go long), ``SHORT``, or ``AVOID`` — with a
conviction, sizing, rationale, risks and an invalidation condition.

This module is **pure and AI-free**: it never calls a model and never needs an
API key. The actual bull-vs-bear *debate* is run by an MCP **host** (Claude
Desktop / Claude Code) through the prompts exposed by
:mod:`makecrazypenny.mcp_server` — the host's model does the reasoning on the
user's own subscription (see CONTRACT.md §10.4). This engine provides the host
with three things:

* :func:`gather_evidence` — fan out across ALL capability servers into a dossier.
* :func:`score_evidence` — a deterministic quant backbone (weighted factors).
* :func:`decide_from_scores` / :func:`decide` — synthesize a :class:`TradeDecision`,
  optionally merging a structured ``verdict`` the host hands back after debating.

Because it is pure, the whole engine is unit-testable offline with no SDK, no
network, and no keys.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.factors import compute_factors
from ..analysis.regime import market_regime
from ..analysis.risk import position_sizing
from ..core.config import Settings
from ..core.disclaimer import DISCLAIMER
from ..core.types import DebateTranscript, TradeDecision
from ..servers._common import normalize_symbol

# ---------------------------------------------------------------------------
# Tunable scoring constants (the deterministic quant backbone).
# Positive contributions are bullish; negative are bearish.
# ---------------------------------------------------------------------------

#: Per-signal weights for ``technical.detect_signals`` output, keyed by name.
_SIGNAL_WEIGHTS: dict[str, float] = {
    "golden_cross": 2.0,
    "death_cross": 2.0,
    "macd_bullish_cross": 1.5,
    "macd_bearish_cross": 1.5,
    "rsi_oversold": 1.0,
    "rsi_overbought": 1.0,
    "bollinger_break_up": 1.0,
    "bollinger_break_down": 1.0,
}
_DEFAULT_SIGNAL_WEIGHT = 1.0

#: Weight applied to the blended sentiment score (which is already in [-1, 1]).
_SENTIMENT_WEIGHT = 2.0
#: Weight applied to the analyst-consensus tilt (computed into [-1, 1]).
_CONSENSUS_WEIGHT = 2.0
#: Weight applied to price-target upside (saturates at ±_UPSIDE_SATURATION).
_PRICE_TARGET_WEIGHT = 1.5
_UPSIDE_SATURATION = 0.20  # ±20% upside -> full weight
#: Weight applied to disclosed congressional / insider net buying.
_CONGRESS_WEIGHT = 1.0
_INSIDER_WEIGHT = 1.0
_FLOW_SATURATION = 3  # net of 3+ trades -> full weight

#: Factor weights (research-backed; see plan.md §10). Folded into the same
#: weighted-factor scoring as the other evidence.
_FACTOR_MOMENTUM_WEIGHT = 2.0
_FACTOR_TREND_WEIGHT = 1.5
_FACTOR_52WHIGH_WEIGHT = 1.0
_FACTOR_VALUE_WEIGHT = 1.5
_FACTOR_QUALITY_WEIGHT = 1.0

#: Decision thresholds on the net quant score.
_LONG_THRESHOLD = 1.0
_SHORT_THRESHOLD = 1.0
#: Minimum conviction required to take a position at all (else AVOID).
_MIN_CONVICTION = 0.10

#: Buy/sell keyword classification for congressional + insider transaction text.
_BUY_WORDS = ("purchas", "buy", "acqui", "bought")
_SELL_WORDS = ("sale", "sell", "dispos", "sold")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lo, hi]``."""
    return max(lo, min(hi, value))


def _as_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on failure."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_list(value: Any, *, limit: int = 12) -> list[str]:
    """Coerce ``value`` to a clean list of short strings (truncated)."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = [str(value)]
    cleaned = [s.strip() for s in items if str(s).strip()]
    return cleaned[:limit]


# ---------------------------------------------------------------------------
# Phase 1 — gather evidence across ALL capability servers
# ---------------------------------------------------------------------------


async def gather_evidence(symbol: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Fan out across every capability server's logic function for ``symbol``.

    Each call is independent and tolerant: a single failure becomes an
    ``{"_error": ...}`` marker for that key instead of aborting the sweep, so a
    decision can always be attempted on whatever evidence is available. Reads go
    through the Layer-0 registry (cached + rate-limited).

    Args:
        symbol: Ticker (normalized internally).
        settings: Unused today (reserved for per-call config); accepted for a
            uniform signature with the rest of the engine.

    Returns:
        A dossier dict keyed by evidence kind (``signals``, ``mtf``,
        ``sentiment``, ``congress``, ``insider``, ``ratings``, ``price_targets``,
        ``upgrades``, ``cross_check``), each holding the server logic function's
        plain dict (or an ``{"_error": ...}`` marker).
    """
    sym = normalize_symbol(symbol)

    # Imported lazily so a missing optional dep in one server never breaks import.
    from ..servers import congress as cong
    from ..servers import reports as rep
    from ..servers import sentiment as sent
    from ..servers import synthesis as syn
    from ..servers import technical as tech

    tasks: dict[str, Any] = {
        "signals": tech.detect_signals(sym),
        "mtf": tech.multi_timeframe_summary(sym),
        "sentiment": sent.aggregate_sentiment(sym),
        "congress": cong.congress_trades(sym),
        "insider": cong.insider_transactions(sym),
        "ratings": rep.analyst_ratings(sym),
        "price_targets": rep.price_targets(sym),
        "upgrades": rep.upgrades_downgrades(sym),
        "cross_check": syn.cross_check(sym),
        "factors": compute_factors(sym, settings=settings),
    }
    keys = list(tasks)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    dossier: dict[str, Any] = {"symbol": sym}
    for key, res in zip(keys, results):
        if isinstance(res, BaseException):
            dossier[key] = {"_error": f"{type(res).__name__}: {res}"}
        else:
            dossier[key] = res
    return dossier


# ---------------------------------------------------------------------------
# Phase 2 — deterministic quant scoring backbone (pure, offline-testable)
# ---------------------------------------------------------------------------


def _factor(category: str, name: str, contribution: float, detail: str) -> dict[str, Any]:
    """Build one scored factor dict."""
    side = "bull" if contribution > 0 else "bear" if contribution < 0 else "neutral"
    return {
        "category": category,
        "name": name,
        "side": side,
        "contribution": round(contribution, 4),
        "detail": detail,
    }


def _score_signals(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score ``technical.detect_signals`` output into directional factors."""
    block = dossier.get("signals")
    if not isinstance(block, dict):
        return
    for sig in block.get("signals", []) or []:
        if not isinstance(sig, dict):
            continue
        name = str(sig.get("name", "signal"))
        direction = str(sig.get("direction", "")).lower()
        weight = _SIGNAL_WEIGHTS.get(name, _DEFAULT_SIGNAL_WEIGHT)
        if direction == "bullish":
            factors.append(_factor("technical", name, weight, f"{name} (bullish)"))
        elif direction == "bearish":
            factors.append(_factor("technical", name, -weight, f"{name} (bearish)"))


def _score_sentiment(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score blended sentiment (already in [-1, 1]) into one factor."""
    block = dossier.get("sentiment")
    if not isinstance(block, dict):
        return
    score = _as_float(block.get("score"))
    if score is None:
        return
    contribution = _clamp(score, -1.0, 1.0) * _SENTIMENT_WEIGHT
    label = block.get("label", "n/a")
    if abs(contribution) > 1e-9:
        factors.append(
            _factor("sentiment", "blended_sentiment", contribution, f"sentiment {score:+.2f} ({label})")
        )


def _consensus_tilt(rating: dict[str, Any]) -> float | None:
    """Compute analyst consensus tilt in [-1, 1] from a rating distribution."""
    sb = _as_float(rating.get("strong_buy")) or 0.0
    b = _as_float(rating.get("buy")) or 0.0
    h = _as_float(rating.get("hold")) or 0.0
    s = _as_float(rating.get("sell")) or 0.0
    ss = _as_float(rating.get("strong_sell")) or 0.0
    total = sb + b + h + s + ss
    if total <= 0:
        return None
    return (2 * sb + b - s - 2 * ss) / (2 * total)


def _score_ratings(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score the latest analyst-rating distribution into a consensus factor."""
    block = dossier.get("ratings")
    if not isinstance(block, dict):
        return
    rows = block.get("ratings")
    rating = None
    if isinstance(rows, list) and rows:
        rating = rows[0] if isinstance(rows[0], dict) else None
    elif isinstance(rows, dict):
        rating = rows
    if not isinstance(rating, dict):
        return
    tilt = _consensus_tilt(rating)
    if tilt is None:
        return
    contribution = _clamp(tilt, -1.0, 1.0) * _CONSENSUS_WEIGHT
    if abs(contribution) > 1e-9:
        factors.append(
            _factor("analyst", "consensus", contribution, f"consensus tilt {tilt:+.2f}")
        )


def _score_price_target(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score analyst price-target upside vs the current price."""
    block = dossier.get("price_targets")
    if not isinstance(block, dict):
        return
    targets = block.get("targets")
    if isinstance(targets, list):
        targets = targets[0] if targets else {}
    if not isinstance(targets, dict):
        return
    mean = _as_float(targets.get("mean"))
    current = _as_float(targets.get("current"))
    if mean is None or current is None or current <= 0:
        return
    upside = (mean - current) / current
    contribution = _clamp(upside / _UPSIDE_SATURATION, -1.0, 1.0) * _PRICE_TARGET_WEIGHT
    if abs(contribution) > 1e-9:
        factors.append(
            _factor("price_target", "target_upside", contribution, f"target upside {upside:+.1%}")
        )


def _classify_flow(transaction: Any) -> int:
    """Classify a trade/transaction string: +1 buy, -1 sell, 0 unknown."""
    text = str(transaction or "").lower()
    if any(w in text for w in _BUY_WORDS):
        return 1
    if any(w in text for w in _SELL_WORDS):
        return -1
    return 0


def _score_flow(
    dossier: dict[str, Any],
    key: str,
    rows_key: str,
    category: str,
    weight: float,
    factors: list[dict[str, Any]],
) -> None:
    """Score net buying from a list of disclosed trades/transactions."""
    block = dossier.get(key)
    if not isinstance(block, dict):
        return
    rows = block.get(rows_key)
    if not isinstance(rows, list) or not rows:
        return
    net = 0
    for row in rows:
        if isinstance(row, dict):
            net += _classify_flow(row.get("transaction"))
    if net == 0:
        return
    magnitude = _clamp(abs(net) / _FLOW_SATURATION, 0.0, 1.0)
    contribution = (1 if net > 0 else -1) * magnitude * weight
    verb = "net buying" if net > 0 else "net selling"
    factors.append(_factor(category, f"{category}_flow", contribution, f"{verb} ({net:+d}) - note disclosure lag"))


def _divergence_penalty(dossier: dict[str, Any]) -> float:
    """Extract a [0, 1] caution penalty from ``synthesis.cross_check`` divergence."""
    block = dossier.get("cross_check")
    if not isinstance(block, dict):
        return 0.0
    div = block.get("divergence")
    if not isinstance(div, dict):
        return 0.0
    score = _as_float(div.get("score"))
    if score is None:
        return 0.0
    return _clamp(abs(score), 0.0, 1.0)


def _score_factors(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score the quant factor block (momentum/trend/value/quality) into factors.

    Reads ``dossier["factors"]`` (from :func:`analysis.factors.compute_factors`).
    Absolute thresholds are used for value/quality (a simplification — these are
    most powerful cross-sectionally; see plan.md §10). Realized vol is *not* scored
    directionally here — it feeds position sizing instead.
    """
    block = dossier.get("factors")
    if not isinstance(block, dict):
        return

    mom = _as_float(block.get("momentum_12_1"))
    if mom is not None:
        c = _clamp(mom / 0.30, -1.0, 1.0) * _FACTOR_MOMENTUM_WEIGHT
        if abs(c) > 1e-9:
            factors.append(_factor("momentum", "momentum_12_1", c, f"12-1 momentum {mom:+.1%}"))

    trend = _as_float(block.get("trend_200"))
    if trend is not None:
        c = _clamp(trend / 0.10, -1.0, 1.0) * _FACTOR_TREND_WEIGHT
        if abs(c) > 1e-9:
            where = "above" if trend > 0 else "below"
            factors.append(_factor("trend", "trend_200", c, f"{where} 200DMA ({trend:+.1%})"))

    p52 = _as_float(block.get("pct_52w_high"))
    if p52 is not None:
        c = _clamp((p52 - 0.85) / 0.15, -1.0, 1.0) * _FACTOR_52WHIGH_WEIGHT
        if abs(c) > 1e-9:
            factors.append(_factor("momentum", "pct_52w_high", c, f"{p52:.0%} of 52w high"))

    value_subs: list[float] = []
    ey = _as_float(block.get("earnings_yield"))
    if ey is not None:
        value_subs.append(_clamp((ey - 0.045) / 0.045, -1.0, 1.0))
    fcfy = _as_float(block.get("fcf_yield"))
    if fcfy is not None:
        value_subs.append(_clamp(fcfy / 0.06, -1.0, 1.0))
    bp = _as_float(block.get("book_to_price"))
    if bp is not None:
        value_subs.append(_clamp((bp - 0.35) / 0.35, -1.0, 1.0))
    if value_subs:
        c = (sum(value_subs) / len(value_subs)) * _FACTOR_VALUE_WEIGHT
        if abs(c) > 1e-9:
            factors.append(_factor("value", "value", c, f"value composite ({len(value_subs)} inputs)"))

    q_subs: list[float] = []
    gp = _as_float(block.get("gross_profitability"))
    if gp is not None:
        q_subs.append(_clamp((gp - 0.30) / 0.30, -1.0, 1.0))
    roe = _as_float(block.get("roe"))
    if roe is not None:
        q_subs.append(_clamp((roe - 0.10) / 0.15, -1.0, 1.0))
    pm = _as_float(block.get("profit_margin"))
    if pm is not None:
        q_subs.append(_clamp((pm - 0.05) / 0.10, -1.0, 1.0))
    if q_subs:
        c = (sum(q_subs) / len(q_subs)) * _FACTOR_QUALITY_WEIGHT
        if abs(c) > 1e-9:
            factors.append(_factor("quality", "quality", c, f"quality composite ({len(q_subs)} inputs)"))


def score_evidence(dossier: dict[str, Any]) -> dict[str, Any]:
    """Turn an evidence dossier into a deterministic directional score.

    Pure function — no I/O, no SDK. Produces a list of weighted ``factors`` (each
    bullish/bearish/neutral) plus aggregate ``net_score`` / ``bull_score`` /
    ``bear_score``, the set of contributing categories (``coverage``), and a
    ``divergence_penalty`` caution from the cross-check.

    Args:
        dossier: The output of :func:`gather_evidence`.

    Returns:
        ``{"factors": [...], "net_score", "bull_score", "bear_score",
        "categories": [...], "divergence_penalty", "n_factors"}``.
    """
    factors: list[dict[str, Any]] = []
    _score_signals(dossier, factors)
    _score_sentiment(dossier, factors)
    _score_ratings(dossier, factors)
    _score_price_target(dossier, factors)
    _score_flow(dossier, "congress", "trades", "congress", _CONGRESS_WEIGHT, factors)
    _score_flow(dossier, "insider", "transactions", "insider", _INSIDER_WEIGHT, factors)
    _score_factors(dossier, factors)

    net = sum(f["contribution"] for f in factors)
    bull = sum(f["contribution"] for f in factors if f["contribution"] > 0)
    bear = -sum(f["contribution"] for f in factors if f["contribution"] < 0)
    categories = sorted({f["category"] for f in factors if f["side"] != "neutral"})

    return {
        "factors": factors,
        "net_score": round(net, 4),
        "bull_score": round(bull, 4),
        "bear_score": round(bear, 4),
        "categories": categories,
        "divergence_penalty": round(_divergence_penalty(dossier), 4),
        "n_factors": len(factors),
    }


# ---------------------------------------------------------------------------
# Decision synthesis (pure): quant backbone, optionally merged with a host verdict
# ---------------------------------------------------------------------------


def _quant_conviction(scored: dict[str, Any]) -> float:
    """Compute the deterministic conviction in [0, 1] from scored evidence."""
    bull = float(scored.get("bull_score", 0.0))
    bear = float(scored.get("bear_score", 0.0))
    net = float(scored.get("net_score", 0.0))
    engagement = bull + bear
    raw = abs(net) / engagement if engagement > 1e-9 else 0.0
    coverage = _clamp(len(scored.get("categories", [])) / 4.0, 0.0, 1.0)
    penalty = float(scored.get("divergence_penalty", 0.0))
    return round(_clamp(raw * coverage * (1.0 - 0.3 * penalty), 0.0, 1.0), 4)


def _sizing_for(conviction: float) -> str:
    """Map a conviction to a qualitative position size."""
    if conviction >= 0.6:
        return "moderate"
    if conviction >= 0.35:
        return "small"
    return "minimal"


def _quant_decision(scored: dict[str, Any]) -> dict[str, Any]:
    """Derive action/direction/conviction from the quant backbone alone.

    Takes a position only when the evidence both *points* somewhere (net past the
    threshold, conviction past the floor) and is *corroborated* — at least two
    distinct categories agree, or a single category is strongly stacked
    (``|net| >= 2 x threshold``). One isolated weak signal stays flat: an
    autonomous trader should not act on a lone data point.
    """
    net = float(scored.get("net_score", 0.0))
    conviction = _quant_conviction(scored)
    n_categories = len(scored.get("categories", []))
    strong = abs(net) >= 2.0 * _LONG_THRESHOLD
    corroborated = n_categories >= 2 or strong

    below_threshold = -_SHORT_THRESHOLD < net < _LONG_THRESHOLD
    if conviction < _MIN_CONVICTION or below_threshold or not corroborated:
        action, direction = "AVOID", "FLAT"
    elif net >= _LONG_THRESHOLD:
        action, direction = "BUY", "LONG"
    else:
        action, direction = "SHORT", "SHORT"
    return {"action": action, "direction": direction, "conviction": conviction}


_VALID_ACTIONS = {"BUY": "LONG", "SHORT": "SHORT", "AVOID": "FLAT"}
_VALID_HORIZONS = {"intraday", "swing", "position", "long_term"}


def _cases_from_factors(scored: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Build fallback bull/bear case bullet lists from the quant factors."""
    bull = [f["detail"] for f in scored.get("factors", []) if f["side"] == "bull"]
    bear = [f["detail"] for f in scored.get("factors", []) if f["side"] == "bear"]
    return bull[:8], bear[:8]


def decide_from_scores(
    symbol: str,
    scored: dict[str, Any],
    *,
    transcript: DebateTranscript | None = None,
    verdict: dict[str, Any] | None = None,
    method: str = "quant",
    note: str | None = None,
) -> TradeDecision:
    """Compose a :class:`TradeDecision` from the quant backbone and (optionally)
    a structured ``verdict`` the host hands back after debating.

    Pure function — safe to unit-test offline. The quant backbone always sets the
    scores and a baseline decision; when a ``verdict`` is supplied (e.g. via the
    ``finalize_decision`` MCP tool, after the host's bull/bear/judge debate) its
    validated fields take precedence for the human-facing decision, while the
    quant scores/factors are preserved for transparency.

    Args:
        symbol: Ticker (already normalized).
        scored: Output of :func:`score_evidence`.
        transcript: An optional structured debate transcript to attach.
        verdict: The host's structured verdict, if available.
        method: Decision method tag for the result (``"quant"`` or ``"debate"``).
        note: Optional operational note.

    Returns:
        A fully populated :class:`TradeDecision` carrying the disclaimer.
    """
    quant = _quant_decision(scored)
    action = quant["action"]
    direction = quant["direction"]
    conviction = quant["conviction"]
    horizon = "swing"
    rationale: list[str] = []
    risks: list[str] = []
    invalidation: str | None = None

    fb_bull, fb_bear = _cases_from_factors(scored)
    bull_case, bear_case = fb_bull, fb_bear

    if verdict:
        v_action = str(verdict.get("action", "")).upper().strip()
        if v_action in _VALID_ACTIONS:
            action = v_action
            direction = _VALID_ACTIONS[v_action]
        v_conv = _as_float(verdict.get("conviction"))
        if v_conv is not None:
            conviction = round(_clamp(v_conv, 0.0, 1.0), 4)
        v_horizon = str(verdict.get("horizon", "")).lower().strip()
        if v_horizon in _VALID_HORIZONS:
            horizon = v_horizon
        rationale = _str_list(verdict.get("rationale"))
        risks = _str_list(verdict.get("risks"))
        inv = verdict.get("invalidation")
        invalidation = str(inv).strip() if inv else None
        v_bull = _str_list(verdict.get("bull_case"))
        v_bear = _str_list(verdict.get("bear_case"))
        bull_case = v_bull or bull_case
        bear_case = v_bear or bear_case

    if transcript is not None:
        if not bull_case:
            last_bull = transcript.latest("bull")
            if last_bull:
                bull_case = last_bull.key_points or [last_bull.thesis]
        if not bear_case:
            last_bear = transcript.latest("bear")
            if last_bear:
                bear_case = last_bear.key_points or [last_bear.thesis]

    sizing = (
        str(verdict.get("suggested_sizing")).strip()
        if verdict and verdict.get("suggested_sizing")
        else _sizing_for(conviction)
    )

    if not rationale:
        rationale = _rationale_fallback(action, scored)

    summary = (
        verdict.get("summary")
        if verdict and str(verdict.get("summary", "")).strip()
        else _summary_line(symbol, action, conviction)
    )

    data_quality = {
        "categories_covered": scored.get("categories", []),
        "n_factors": scored.get("n_factors", 0),
        "divergence_penalty": scored.get("divergence_penalty", 0.0),
        "coverage": round(_clamp(len(scored.get("categories", [])) / 4.0, 0.0, 1.0), 4),
    }

    return TradeDecision(
        symbol=symbol,
        action=action,
        direction=direction,
        conviction=conviction,
        horizon=horizon,
        suggested_sizing=sizing,
        summary=str(summary),
        rationale=rationale,
        bull_case=bull_case,
        bear_case=bear_case,
        risks=risks,
        invalidation=invalidation,
        net_score=float(scored.get("net_score", 0.0)),
        bull_score=float(scored.get("bull_score", 0.0)),
        bear_score=float(scored.get("bear_score", 0.0)),
        factors=list(scored.get("factors", [])),
        method=method,
        data_quality=data_quality,
        transcript=transcript,
        note=note,
        disclaimer=DISCLAIMER,
    )


def _rationale_fallback(action: str, scored: dict[str, Any]) -> list[str]:
    """Build a terse rationale from the dominant quant factors."""
    factors = sorted(scored.get("factors", []), key=lambda f: -abs(f["contribution"]))
    top = [f["detail"] for f in factors[:4]]
    head = {
        "BUY": "Net evidence leans bullish.",
        "SHORT": "Net evidence leans bearish.",
        "AVOID": "Evidence is mixed or thin - no edge.",
    }.get(action, "Decision derived from weighted evidence.")
    return [head, *top] if top else [head]


def _summary_line(symbol: str, action: str, conviction: float) -> str:
    """Build a one-line human-readable verdict."""
    verb = {"BUY": "Go long", "SHORT": "Go short", "AVOID": "Stay flat"}.get(action, action)
    return f"{verb} {symbol} - conviction {conviction:.0%}."


# ---------------------------------------------------------------------------
# Top-level deterministic decision
# ---------------------------------------------------------------------------


async def enrich_decision(
    decision: TradeDecision, dossier: dict[str, Any], *, settings: Settings | None = None
) -> TradeDecision:
    """Attach the market regime + position sizing to a decision (mutates + returns).

    Pulls the market-regime read (benchmark trend/vol → gross-exposure scalar) and
    computes ATR stops/target + a vol-target/½-Kelly position size from the factor
    block (last close, ATR, realized vol) and the decision's direction/conviction.
    Never raises — on a regime-fetch failure it sizes without the regime scalar.
    """
    fac = dossier.get("factors") if isinstance(dossier.get("factors"), dict) else {}
    try:
        regime = await market_regime(settings=settings)
    except Exception as exc:  # never break the decision over a regime fetch
        regime = {"regime": "unknown", "_error": f"{type(exc).__name__}: {exc}"}
    decision.regime = regime if isinstance(regime, dict) else {}
    gross = regime.get("gross_exposure", 1.0) if isinstance(regime, dict) else 1.0
    decision.sizing = position_sizing(
        price=fac.get("last_close"),
        atr_value=fac.get("atr14"),
        annual_vol=fac.get("realized_vol"),
        conviction=decision.conviction,
        direction=decision.direction,
        regime_scale=float(gross) if gross is not None else 1.0,
    )
    return decision


async def decide(symbol: str, *, settings: Settings | None = None) -> TradeDecision:
    """Make the deterministic quant decision for ``symbol`` (with sizing + regime).

    Gathers evidence across every capability server (incl. the quant factor block),
    scores it, synthesizes a :class:`TradeDecision`, then enriches it with the market
    regime and a sized trade (stop/target + vol-target/½-Kelly position). This is
    **AI-free** — the bull/bear debate that can override it is run by an MCP host via
    :mod:`makecrazypenny.mcp_server`. Always returns a real decision carrying the
    not-investment-advice disclaimer.

    Args:
        symbol: Ticker (normalized internally).
        settings: Optional settings (defaults to ``Settings.from_env()``).

    Returns:
        A :class:`TradeDecision` (``method="quant"``).
    """
    settings = settings or Settings.from_env()
    sym = normalize_symbol(symbol)
    dossier = await gather_evidence(sym, settings=settings)
    scored = score_evidence(dossier)
    decision = decide_from_scores(sym, scored, method="quant")
    return await enrich_decision(decision, dossier, settings=settings)


__all__ = [
    "gather_evidence",
    "score_evidence",
    "decide_from_scores",
    "enrich_decision",
    "decide",
]
