"""Layer 2: the crypto decision engine (CONTRACT.md §16).

The crypto analogue of :mod:`makecrazypenny.orchestration.debate`, specialized for
**very-short-window leveraged perpetual-futures** trading. It turns the crypto
evidence (multi-timeframe price action + the derivatives metrics that have no
equity analogue) into an explicit ``BUY`` / ``SHORT`` / ``AVOID`` decision and —
crucially — attaches a **leverage-aware plan** (suggested leverage, liquidation
price, stop/target, funding cost, margin) instead of the unlevered equity sizing.

Reuse over reinvention:

* the deterministic mapper :func:`makecrazypenny.orchestration.debate.decide_from_scores`
  is asset-agnostic and is reused as-is;
* the technical-signal and momentum/trend factor scorers from ``debate`` are reused
  on the crypto dossier (same shapes);
* the new derivatives signals come from :mod:`makecrazypenny.analysis.crypto_metrics`,
  the regime from :mod:`makecrazypenny.analysis.crypto_regime`, and the sizing from
  :mod:`makecrazypenny.analysis.leverage`.

Pure orchestration + cached I/O; AI-free. The bull/bear debate over a crypto
verdict is run by the MCP host via the ``decide_crypto`` prompt. Never raises out
of :func:`decide_crypto` for transient data issues — evidence gathering is tolerant.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..analysis.crypto_metrics import (
    cvd_signal,
    fear_greed_signal,
    funding_signal,
    funding_z_signal,
    long_short_signal,
    oi_price_signal,
    predicted_funding_signal,
    social_velocity_signal,
    taker_flow_signal,
    top_trader_spread_signal,
)
from ..analysis.crypto_regime import crypto_regime
from ..analysis.factors import factor_values
from ..analysis.leverage import leverage_plan
from ..core.config import Settings
from ..core.symbols import canonical_crypto
from ..core.types import TradeDecision, utcnow_iso
from .debate import _factor, _score_factors, _score_signals, decide_from_scores

# ---------------------------------------------------------------------------
# Crypto-tuned scoring weights (derivatives/microstructure heavy).
# ---------------------------------------------------------------------------

_MTF_WEIGHT = 1.5
_FUNDING_WEIGHT = 2.0
_OI_WEIGHT = 2.0
_LS_WEIGHT = 1.0
_FNG_WEIGHT = 1.0
# Swarm extension (CONTRACT.md §18): aggressor flow is the engine's strongest
# new evidence (OFI literature); funding history/forward adds carry context;
# deterministic social velocity is intentionally the smallest voice.
_TAKER_FLOW_WEIGHT = 1.5
_CVD_WEIGHT = 1.0
_TOP_TRADER_WEIGHT = 1.0
_FUNDING_Z_WEIGHT = 1.0
_PREDICTED_FUNDING_WEIGHT = 0.75
_SOCIAL_WEIGHT = 0.5

# With ~5 more independent categories available than the equity engine, demand
# broader coverage for the same conviction and stricter corroboration before
# taking a position (these ride in the scored dict; debate.py reads them).
_CATEGORY_NORM = 5.0
_CORROBORATION_MIN_CATEGORIES = 3
_STRONG_NET_MULT = 2.5

#: Map an entry interval to the trade horizon label + an expected hold (hours).
_HORIZON_BY_INTERVAL: dict[str, tuple[str, float]] = {
    "1m": ("scalp", 1.0), "3m": ("scalp", 2.0), "5m": ("scalp", 3.0),
    "15m": ("intraday", 6.0), "30m": ("intraday", 8.0),
    "1h": ("intraday", 12.0), "2h": ("swing", 24.0), "4h": ("swing", 36.0),
    "1d": ("position", 96.0),
}

#: Minutes per bar for the supported entry intervals.
_INTERVAL_MINUTES: dict[str, float] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}

#: Saturations for the price factors on crypto *daily* bars. Crypto's annual
#: moves dwarf equities', so the daily anchors are wider than debate.py's
#: equity defaults (0.30 / 0.10 / 0.15). Sub-daily saturations scale these by
#: sqrt(interval/1d) — a return's typical magnitude grows ~sqrt(time).
_CRYPTO_MOM_SAT_1D = 0.60
_CRYPTO_TREND_SAT_1D = 0.20
_CRYPTO_P52_BAND_1D = 0.30
#: Floors so ultra-short intervals don't saturate on pure noise.
_MIN_MOM_SAT = 0.01
_MIN_TREND_SAT = 0.004
_MIN_P52_BAND = 0.005


def _horizon_for(interval: str) -> tuple[str, float]:
    """Return ``(horizon_label, expected_hold_hours)`` for an entry interval."""
    return _HORIZON_BY_INTERVAL.get(str(interval).strip().lower(), ("intraday", 8.0))


def _interval_minutes(interval: str) -> float:
    """Minutes per bar for ``interval`` (defaults to 15m for unknown aliases)."""
    return _INTERVAL_MINUTES.get(str(interval).strip().lower(), 15.0)


def periods_per_year(interval: str) -> float:
    """Bars per year for ``interval`` on a 24/7 market (crypto trades 365 days)."""
    return 365.0 * 1440.0 / _interval_minutes(interval)


def factor_saturations(interval: str) -> dict[str, float]:
    """Interval-appropriate saturations for the price-factor scorers.

    The factor windows are fixed in *bars* (252-bar momentum, 200-bar SMA), so
    their calendar horizon shrinks with the interval; the move that should count
    as "saturated" shrinks ~sqrt(time) with it. Anchored at the crypto daily
    values and floored to avoid scoring sub-noise moves at 1m.
    """
    scale = (_interval_minutes(interval) / 1440.0) ** 0.5
    return {
        "mom_saturation": max(_CRYPTO_MOM_SAT_1D * scale, _MIN_MOM_SAT),
        "trend_saturation": max(_CRYPTO_TREND_SAT_1D * scale, _MIN_TREND_SAT),
        "p52_band": max(_CRYPTO_P52_BAND_1D * scale, _MIN_P52_BAND),
    }


# ---------------------------------------------------------------------------
# Phase 1 — gather crypto evidence (concurrent, tolerant)
# ---------------------------------------------------------------------------


async def _compute_crypto_factors(symbol: str, interval: str) -> dict[str, Any]:
    """Fetch ~500 perp bars at ``interval`` and compute price-only factors.

    Reuses :func:`analysis.factors.factor_values`; on crypto bars the lookbacks
    are bar-based (intermediate momentum / trend vs a 200-bar SMA / ATR), which is
    exactly the short-window context a leveraged trader wants. Never raises.
    """
    from ..servers import crypto as cx

    try:
        ohlcv = await cx.crypto_ohlcv(symbol, interval=interval, limit=500)
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    return factor_values(bars, periods_per_year=periods_per_year(interval))


async def gather_crypto_evidence(
    symbol: str,
    *,
    interval: str = "15m",
    settings: Settings | None = None,
    include_chatter: bool = True,
) -> dict[str, Any]:
    """Fan out across the crypto capability server for ``symbol``.

    Tolerant: a single failure becomes an ``{"_error": ...}`` marker instead of
    aborting the sweep. Returns a dossier keyed by ``mtf``, ``signals``,
    ``derivatives``, ``sentiment``, ``factors``, ``flow``, ``hl``, and (when
    ``include_chatter``) ``social`` + ``news``. The chatter sources ride slow,
    shared rate buckets (CoinGecko keyless ~5/min), so bulk callers like the
    screener pass ``include_chatter=False`` to avoid serializing on them.
    """
    sym = canonical_crypto(symbol)
    from ..servers import crypto as cx

    tasks: dict[str, Any] = {
        "mtf": cx.multi_timeframe(sym),
        "signals": cx.crypto_signals(sym, interval),
        "derivatives": cx.derivatives(sym, interval),
        "sentiment": cx.crypto_sentiment(),
        "factors": _compute_crypto_factors(sym, interval),
        # Swarm extension (CONTRACT.md §18): aggressor flow, HL-native context,
        # deterministic social counts, and the (unscored) news headlines.
        "flow": cx.flow_metrics(sym, interval),
        "hl": cx.hl_context(sym),
    }
    if include_chatter:
        tasks["social"] = cx.social_scan(sym)
        tasks["news"] = cx.news_feed(sym, limit=15)
    keys = list(tasks)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    dossier: dict[str, Any] = {"symbol": sym, "interval": interval}
    for key, res in zip(keys, results):
        dossier[key] = {"_error": f"{type(res).__name__}: {res}"} if isinstance(res, BaseException) else res
    return dossier


# ---------------------------------------------------------------------------
# Phase 2 — deterministic crypto scoring (pure, offline-testable)
# ---------------------------------------------------------------------------


def _score_mtf(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score multi-timeframe trend alignment into one factor."""
    block = dossier.get("mtf")
    if not isinstance(block, dict):
        return
    tfs = block.get("timeframes")
    if not isinstance(tfs, dict):
        return
    dirs = [v.get("trend") for v in tfs.values() if isinstance(v, dict)]
    bull = dirs.count("bullish")
    bear = dirs.count("bearish")
    decided = bull + bear
    if decided == 0 or bull == bear:
        return
    strength = (bull - bear) / decided
    factors.append(_factor("trend", "mtf_alignment", strength * _MTF_WEIGHT, f"{bull} bull / {bear} bear timeframes"))


def _score_derivatives(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score funding (contrarian), the OI/price matrix, and long/short positioning."""
    block = dossier.get("derivatives")
    if not isinstance(block, dict):
        return

    funding = block.get("funding")
    if isinstance(funding, dict) and "_error" not in funding:
        sig = funding_signal(funding.get("rate"), funding.get("annualized"))
        if sig is not None:
            factors.append(_factor("funding", "funding", sig[0] * _FUNDING_WEIGHT, sig[1]))

    sig = oi_price_signal(block.get("oi_change_pct"), block.get("price_change_pct"))
    if sig is not None:
        factors.append(_factor("open_interest", "oi_price", sig[0] * _OI_WEIGHT, sig[1]))

    ls = block.get("long_short")
    if isinstance(ls, dict) and "_error" not in ls:
        sig = long_short_signal(ls.get("ratio"))
        if sig is not None:
            factors.append(_factor("positioning", "long_short", sig[0] * _LS_WEIGHT, sig[1]))


def _score_flow(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score aggressor flow: taker buy/sell imbalance, CVD, top-trader spread."""
    block = dossier.get("flow")
    if not isinstance(block, dict):
        return

    taker = block.get("taker_flow")
    if isinstance(taker, dict) and "_error" not in taker:
        sig = taker_flow_signal(taker.get("series"))
        if sig is not None:
            factors.append(_factor("flow", "taker_flow", sig[0] * _TAKER_FLOW_WEIGHT, sig[1]))

    ohlcv = block.get("ohlcv")
    if isinstance(ohlcv, dict) and "_error" not in ohlcv:
        sig = cvd_signal(ohlcv.get("bars"))
        if sig is not None:
            factors.append(_factor("flow", "cvd", sig[0] * _CVD_WEIGHT, sig[1]))

    # Top traders vs the crowd: follow the smart side of the book.
    crowd: Any = None
    deriv = dossier.get("derivatives")
    if isinstance(deriv, dict):
        ls = deriv.get("long_short")
        if isinstance(ls, dict) and "_error" not in ls:
            crowd = ls.get("ratio")
    top = block.get("top_trader")
    if isinstance(top, dict) and "_error" not in top:
        series = top.get("series") or []
        latest = series[-1].get("ratio") if series and isinstance(series[-1], dict) else None
        sig = top_trader_spread_signal(latest, crowd)
        if sig is not None:
            factors.append(
                _factor("positioning", "top_trader_spread", sig[0] * _TOP_TRADER_WEIGHT, sig[1])
            )


def _score_funding_extras(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score funding history (z-score extremes) and HL predicted funding."""
    current: Any = None
    deriv = dossier.get("derivatives")
    if isinstance(deriv, dict):
        funding = deriv.get("funding")
        if isinstance(funding, dict) and "_error" not in funding:
            current = funding.get("rate")

    block = dossier.get("flow")
    if isinstance(block, dict):
        hist = block.get("funding_history")
        if isinstance(hist, dict) and "_error" not in hist:
            sig = funding_z_signal(hist.get("rates"), current)
            if sig is not None:
                factors.append(_factor("funding", "funding_z", sig[0] * _FUNDING_Z_WEIGHT, sig[1]))

    hl = dossier.get("hl")
    if isinstance(hl, dict):
        ctx = hl.get("asset_ctx")
        pred = hl.get("predicted_funding")
        if (
            isinstance(ctx, dict)
            and "_error" not in ctx
            and isinstance(pred, dict)
            and "_error" not in pred
        ):
            sig = predicted_funding_signal(ctx.get("funding_hourly"), pred.get("venues"))
            if sig is not None:
                factors.append(
                    _factor(
                        "funding", "predicted_funding", sig[0] * _PREDICTED_FUNDING_WEIGHT, sig[1]
                    )
                )


def _score_social(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score deterministic social velocity (Reddit cadence x StockTwits labels)."""
    block = dossier.get("social")
    if not isinstance(block, dict):
        return
    scan = block.get("scan")
    if not isinstance(scan, dict) or "_error" in scan:
        return
    reddit = scan.get("reddit")
    st = scan.get("stocktwits")
    if not isinstance(reddit, dict) or "_error" in reddit:
        return
    if not isinstance(st, dict) or "_error" in st:
        return
    sig = social_velocity_signal(
        reddit.get("post_velocity_per_hr"),
        reddit.get("prev_velocity_per_hr"),
        st.get("bullish"),
        st.get("bearish"),
    )
    if sig is not None:
        factors.append(_factor("social", "social_velocity", sig[0] * _SOCIAL_WEIGHT, sig[1]))


def _score_crypto_sentiment(dossier: dict[str, Any], factors: list[dict[str, Any]]) -> None:
    """Score the Fear & Greed Index (contrarian) into one factor."""
    block = dossier.get("sentiment")
    if not isinstance(block, dict):
        return
    fng = block.get("fear_greed")
    if isinstance(fng, dict) and "_error" not in fng:
        sig = fear_greed_signal(fng.get("value"))
        if sig is not None:
            factors.append(_factor("sentiment", "fear_greed", sig[0] * _FNG_WEIGHT, sig[1]))


def score_crypto_evidence(dossier: dict[str, Any]) -> dict[str, Any]:
    """Turn a crypto evidence dossier into a deterministic directional score.

    Pure function — same output shape as
    :func:`makecrazypenny.orchestration.debate.score_evidence` so the shared
    :func:`decide_from_scores` mapper consumes it unchanged. Reuses the equity
    technical-signal and momentum/trend factor scorers — with the saturations
    rescaled to the dossier's entry interval (the factor windows span hours on
    intraday bars, not months) — then adds the crypto derivatives + Fear & Greed
    factors.
    """
    factors: list[dict[str, Any]] = []
    _score_signals(dossier, factors)
    sat = factor_saturations(str(dossier.get("interval", "15m")))
    _score_factors(dossier, factors, **sat)
    _score_mtf(dossier, factors)
    _score_derivatives(dossier, factors)
    _score_crypto_sentiment(dossier, factors)
    _score_flow(dossier, factors)
    _score_funding_extras(dossier, factors)
    _score_social(dossier, factors)

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
        "divergence_penalty": 0.0,
        "n_factors": len(factors),
        # Conviction/corroboration tuning consumed by debate._quant_decision:
        # more independent evidence categories -> demand broader agreement.
        "category_norm": _CATEGORY_NORM,
        "corroboration_min_categories": _CORROBORATION_MIN_CATEGORIES,
        "strong_net_mult": _STRONG_NET_MULT,
    }


# ---------------------------------------------------------------------------
# Phase 3 — synthesize the decision + attach the leverage plan
# ---------------------------------------------------------------------------


async def enrich_crypto_decision(
    decision: TradeDecision,
    dossier: dict[str, Any],
    *,
    interval: str = "15m",
    leverage_cap: float | None = None,
    settings: Settings | None = None,
) -> TradeDecision:
    """Attach the crypto regime + leverage plan to a decision (mutates + returns).

    Pulls the BTC/market regime (gross-exposure scalar), then builds the
    leverage-aware plan (liquidation price, suggested leverage, stop/target,
    funding cost, margin) from the factor block (last close, ATR) and the live
    funding rate. Never raises — on a regime-fetch failure it sizes without the
    regime scalar. Also mirrors the plan's stop/target into ``sizing`` so generic
    consumers (and the CLI) can read them uniformly.
    """
    settings = settings or Settings.from_env()
    fac = dossier.get("factors") if isinstance(dossier.get("factors"), dict) else {}

    try:
        regime = await crypto_regime(settings=settings)
    except Exception as exc:  # never break the decision over a regime fetch
        regime = {"regime": "unknown", "_error": f"{type(exc).__name__}: {exc}"}
    decision.regime = regime if isinstance(regime, dict) else {}
    gross = regime.get("gross_exposure", 1.0) if isinstance(regime, dict) else 1.0

    # Funding COST is venue-specific: prefer the Hyperliquid-native hourly rate
    # (the venue trades execute on) and fall back to the CEX rate only when the
    # HL context is unavailable. HL funding is hourly and can diverge materially
    # from Binance's 8h rate — using the wrong one misprices the carry.
    funding_rate: float | None = None
    funding_interval = 8.0
    hl = dossier.get("hl")
    if isinstance(hl, dict):
        ctx = hl.get("asset_ctx")
        if isinstance(ctx, dict) and "_error" not in ctx and ctx.get("funding_hourly") is not None:
            funding_rate = ctx.get("funding_hourly")
            funding_interval = 1.0
    if funding_rate is None:
        deriv = dossier.get("derivatives")
        if isinstance(deriv, dict):
            funding = deriv.get("funding")
            if isinstance(funding, dict) and "_error" not in funding:
                funding_rate = funding.get("rate")
                funding_interval = funding.get("interval_hours", 8.0) or 8.0

    horizon, hold_hours = _horizon_for(interval)
    decision.horizon = horizon
    decision.asset_class = "crypto"

    plan = leverage_plan(
        price=fac.get("last_close"),
        atr_value=fac.get("atr14"),
        direction=decision.direction,
        conviction=decision.conviction,
        funding_rate=funding_rate,
        funding_interval_hours=funding_interval,
        expected_hold_hours=hold_hours,
        max_leverage=float(leverage_cap) if leverage_cap else settings.crypto_max_leverage,
        risk_per_trade=settings.crypto_risk_per_trade,
        mmr=settings.crypto_maint_margin_rate,
        liq_buffer=settings.crypto_liq_buffer,
        regime_scale=float(gross) if gross is not None else 1.0,
    )
    decision.leverage = plan
    # Mirror stop/target into the generic sizing block for uniform consumers.
    decision.sizing = {
        "direction": plan.get("direction"),
        "position_pct": plan.get("margin_pct", 0.0),
        "stop_price": plan.get("stop_price"),
        "target_price": plan.get("target_price"),
        "r_multiple": plan.get("r_multiple"),
        "regime_scale": plan.get("regime_scale"),
        "notes": plan.get("notes"),
    }
    if decision.invalidation is None and plan.get("stop_price") is not None:
        decision.invalidation = f"price through stop {plan['stop_price']} (or liquidation {plan.get('liquidation_price')})"
    decision.as_of = utcnow_iso()
    return decision


async def decide_crypto(
    symbol: str,
    *,
    interval: str = "15m",
    leverage_cap: float | None = None,
    settings: Settings | None = None,
    include_chatter: bool = True,
) -> TradeDecision:
    """Make the deterministic crypto decision for ``symbol`` (with leverage plan).

    Gathers crypto evidence at ``interval``, scores it, synthesizes a
    :class:`TradeDecision` via the shared mapper, then enriches it with the crypto
    regime and a leverage-aware plan. **AI-free** — the bull/bear debate that can
    override it is run by an MCP host via :mod:`makecrazypenny.mcp_server`. Always
    returns a real decision carrying the not-investment-advice disclaimer.
    """
    settings = settings or Settings.from_env()
    sym = canonical_crypto(symbol)
    dossier = await gather_crypto_evidence(
        sym, interval=interval, settings=settings, include_chatter=include_chatter
    )
    scored = score_crypto_evidence(dossier)
    decision = decide_from_scores(sym, scored, method="quant")
    return await enrich_crypto_decision(
        decision, dossier, interval=interval, leverage_cap=leverage_cap, settings=settings
    )


__all__ = [
    "gather_crypto_evidence",
    "score_crypto_evidence",
    "enrich_crypto_decision",
    "decide_crypto",
]
