"""Synthesis capability server (see CONTRACT.md §9.2 ``synthesis``).

``synthesis`` is the *only* Layer-1 server permitted to consume multiple
capabilities and to compose the read-only logic of sibling servers. Its single
tool, :func:`cross_check`, reconciles three independent views of a symbol:

  * **Analyst consensus** — the recommendation distribution (``analyst_ratings``)
    and the price-target consensus (``price_targets``), i.e. what the sell-side
    *says* (the ``reports`` domain).
  * **Price / technicals** — the current price relative to its moving averages
    plus a coarse trend read derived from ``ohlcv`` (the ``technical`` domain).
  * **Fundamentals** — valuation and, crucially, *margin trend*
    (``fundamentals``), to catch the classic "consensus Buy but margins
    compressing" divergence.

It then computes a **divergence assessment**: per-pair agreement signals
(consensus-vs-price, consensus-vs-fundamentals, price-vs-fundamentals) and a set
of human-readable mismatch flags, with an overall divergence label/score so an
agent can reason about how much the three views disagree.

Design / engineering mandates honored here:

  * **Dependency rule (CONTRACT.md §2.6 / §9.2).** Synthesis reaches Layer-0 only
    through the module-level :func:`get_registry` indirection (monkeypatchable in
    tests) and, *optionally*, the read-only ``technical``/``reports`` **logic
    functions**. It never imports another server's MCP wiring and must never be
    imported by ``technical``/``reports`` (keeps the dependency graph acyclic).
  * **Soft sibling composition.** The ``technical`` and ``reports`` modules may
    not exist yet (or may be absent in a minimal env). Their logic functions are
    imported lazily and best-effort; when unavailable, ``cross_check`` falls back
    to fetching the same capabilities directly via the registry. Either way the
    behaviour and output shape are identical.
  * **Import safety (CONTRACT.md §2.2).** Importing this module never requires the
    SDK (``servers/_sdk.py`` shims), never requires a network or any key, and
    never imports a heavy optional library at module top.
  * **MCP return shape (CONTRACT.md §2.4).** The ``@tool``-wrapped entrypoint
    returns ``text_result(...)``; ``cross_check`` itself returns a plain ``dict``
    so it stays directly unit-testable.
"""

from __future__ import annotations

from typing import Any

from ..providers import get_registry as _provider_get_registry
from ._common import normalize_symbol, text_result
from ._sdk import create_sdk_mcp_server, tool

# ---------------------------------------------------------------------------
# Registry indirection (monkeypatch target for tests — CONTRACT.md §9.1.2)
# ---------------------------------------------------------------------------


def get_registry() -> Any:
    """Return the process-wide provider registry.

    A thin indirection over :func:`makecrazypenny.providers.get_registry` so unit
    tests can monkeypatch *this* module's ``get_registry`` to inject a fake
    registry (returning canned ``fetch`` envelopes) without any network access.

    Returns:
        The shared :class:`~makecrazypenny.providers.ProviderRegistry`.
    """
    return _provider_get_registry()


# ---------------------------------------------------------------------------
# Tunables for the divergence heuristics. Deliberately conservative and
# explained inline; an agent receives both the raw inputs and these verdicts.
# ---------------------------------------------------------------------------

# Moving-average windows (in bars) used for the price-vs-MA read.
_MA_WINDOWS: tuple[int, ...] = (20, 50, 200)

# A consensus "score" in [-1, 1] derived from the rating distribution. These
# thresholds bucket it into Buy / Hold / Sell leanings.
_CONSENSUS_BUY_THRESHOLD = 0.20
_CONSENSUS_SELL_THRESHOLD = -0.20

# Margin-trend sensitivity: relative change between the two most recent margin
# observations beyond this fraction counts as expanding / compressing.
_MARGIN_TREND_EPS = 0.02  # 2% relative change

# Price-vs-target: how far below the mean target counts as "rich vs price"
# (i.e. lots of implied upside the market is not pricing in -> a divergence flag
# when paired with bearish technicals).
_TARGET_UPSIDE_FLAG = 0.10  # >=10% implied upside


# ---------------------------------------------------------------------------
# Small numeric / payload helpers (pure, dependency-light)
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    """Best-effort coercion to a finite ``float``; ``None`` on failure/NaN/inf."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN or +/-inf
        return None
    return out


def _envelope_data(envelope: Any) -> Any:
    """Extract the ``data`` payload from a registry ``fetch`` envelope.

    The registry returns ``{"provider", "data", "cached"}``. Sibling logic
    functions may instead return their own shaped dict; in that case the whole
    object is the payload. This accepts either.
    """
    if isinstance(envelope, dict) and "data" in envelope and "provider" in envelope:
        return envelope.get("data")
    return envelope


async def _safe_fetch(capability: str, **params: Any) -> dict[str, Any]:
    """Fetch one capability, never raising; returns a small status wrapper.

    On success returns ``{"ok": True, "provider", "cached", "data"}``; on any
    failure (including ``AllProvidersFailed``) returns
    ``{"ok": False, "error": <str>, "data": None}`` so ``cross_check`` can still
    assemble a partial, useful assessment when one view is unavailable.
    """
    try:
        envelope = await get_registry().fetch(capability, **params)
    except Exception as exc:  # AllProvidersFailed et al. — degrade gracefully.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "data": None}
    if isinstance(envelope, dict):
        return {
            "ok": True,
            "provider": envelope.get("provider"),
            "cached": envelope.get("cached"),
            "data": envelope.get("data"),
        }
    return {"ok": True, "provider": None, "cached": None, "data": envelope}


# ---------------------------------------------------------------------------
# Optional, read-only composition of sibling logic functions (CONTRACT §9.2).
# These are imported lazily/defensively: the modules may not exist yet, and we
# must never hard-depend on them. When present we prefer their richer output;
# otherwise we fall back to a direct registry fetch of the same capability.
# ---------------------------------------------------------------------------


async def _sibling_call(module_name: str, func_name: str, **kwargs: Any) -> Any | None:
    """Best-effort call into a sibling server's read-only logic function.

    Imports ``makecrazypenny.servers.<module_name>`` lazily and, if it exposes a
    callable ``<func_name>``, awaits it with ``kwargs``. Any import error,
    missing attribute, or runtime error yields ``None`` so the caller falls back
    to a direct registry fetch. This keeps the dependency on ``technical`` /
    ``reports`` strictly optional and the module graph acyclic.
    """
    import importlib

    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except Exception:
        return None
    func = getattr(module, func_name, None)
    if not callable(func):
        return None
    try:
        return await func(**kwargs)
    except Exception:
        return None


async def _view_via_sibling_or_fetch(
    module_name: str,
    func_name: str,
    capability: str,
    **params: Any,
) -> dict[str, Any]:
    """Resolve one view via a sibling logic function, else a direct fetch.

    Prefers the read-only sibling logic function
    ``servers.<module_name>.<func_name>`` (CONTRACT.md §9.2 composition); when it
    is unavailable or fails, falls back to ``get_registry().fetch(capability)``.
    Either way returns the uniform ``_safe_fetch``-style status wrapper so the
    caller treats both paths identically.
    """
    sibling = await _sibling_call(module_name, func_name, **params)
    if sibling is not None:
        return {
            "ok": True,
            "provider": f"{module_name}.{func_name}",
            "cached": None,
            "data": _envelope_data(sibling),
        }
    return await _safe_fetch(capability, **params)


# ---------------------------------------------------------------------------
# View extraction: turn raw capability payloads into compact, comparable reads.
# ---------------------------------------------------------------------------


def _consensus_from_ratings(ratings_data: Any) -> dict[str, Any]:
    """Reduce an ``analyst_ratings`` payload to a consensus read.

    ``analyst_ratings`` is a *list* of period buckets (most recent first by
    convention). The most recent bucket with any votes is used. A consensus
    score in ``[-1, 1]`` is computed as a weighted average of the five rating
    tiers (strong_buy=+1 .. strong_sell=-1), then bucketed into a Buy/Hold/Sell
    label.

    Returns a dict with ``available`` plus, when available, ``label``, ``score``,
    ``counts``, ``n_analysts`` and ``period``.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(ratings_data, list):
        rows = [r for r in ratings_data if isinstance(r, dict)]
    elif isinstance(ratings_data, dict):
        # A sibling/reports payload may wrap the period buckets under a key
        # (e.g. ``{"ratings": [...]}``); prefer the first list-of-dicts found,
        # otherwise treat the dict itself as a single rating row.
        nested = None
        for key in ("ratings", "analyst_ratings", "data", "rows"):
            candidate = ratings_data.get(key)
            if isinstance(candidate, list):
                nested = [r for r in candidate if isinstance(r, dict)]
                break
        rows = nested if nested is not None else [ratings_data]

    chosen: dict[str, Any] | None = None
    for row in rows:
        total = (
            _to_int(row.get("strong_buy"))
            + _to_int(row.get("buy"))
            + _to_int(row.get("hold"))
            + _to_int(row.get("sell"))
            + _to_int(row.get("strong_sell"))
        )
        if total > 0:
            chosen = row
            break

    if chosen is None:
        return {"available": False}

    sb = _to_int(chosen.get("strong_buy"))
    b = _to_int(chosen.get("buy"))
    h = _to_int(chosen.get("hold"))
    s = _to_int(chosen.get("sell"))
    ss = _to_int(chosen.get("strong_sell"))
    n = sb + b + h + s + ss
    # Weighted mean on a [-1, 1] tier scale.
    score = (sb * 1.0 + b * 0.5 + h * 0.0 + s * -0.5 + ss * -1.0) / n if n else 0.0

    if score >= _CONSENSUS_BUY_THRESHOLD:
        label = "Buy"
    elif score <= _CONSENSUS_SELL_THRESHOLD:
        label = "Sell"
    else:
        label = "Hold"

    return {
        "available": True,
        "label": label,
        "score": round(score, 4),
        "counts": {
            "strong_buy": sb,
            "buy": b,
            "hold": h,
            "sell": s,
            "strong_sell": ss,
        },
        "n_analysts": n,
        "period": chosen.get("period"),
    }


def _to_int(value: Any) -> int:
    """Best-effort int coercion (defaulting to ``0``) for rating counts."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _price_target_read(pt_data: Any, *, fallback_price: float | None) -> dict[str, Any]:
    """Reduce a ``price_targets`` payload to mean/high/low + implied upside."""
    row: dict[str, Any] = {}
    if isinstance(pt_data, dict):
        row = pt_data
    elif isinstance(pt_data, list) and pt_data and isinstance(pt_data[0], dict):
        row = pt_data[0]

    mean = _to_float(row.get("mean"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    current = _to_float(row.get("current"))
    if current is None:
        current = fallback_price

    upside_pct: float | None = None
    if mean is not None and current is not None and current != 0:
        upside_pct = round((mean - current) / current * 100.0, 2)

    return {
        "available": mean is not None or high is not None or low is not None,
        "mean": mean,
        "high": high,
        "low": low,
        "current": current,
        "implied_upside_pct": upside_pct,
    }


def _closes_from_ohlcv(ohlcv_data: Any) -> list[float]:
    """Extract the ordered list of closing prices from an ``ohlcv`` payload."""
    bars: Any = None
    if isinstance(ohlcv_data, dict):
        bars = ohlcv_data.get("bars")
    elif isinstance(ohlcv_data, list):
        bars = ohlcv_data
    if not isinstance(bars, list):
        return []
    closes: list[float] = []
    for bar in bars:
        if isinstance(bar, dict):
            c = _to_float(bar.get("close"))
        else:
            c = _to_float(bar)
        if c is not None:
            closes.append(c)
    return closes


def _sma(values: list[float], window: int) -> float | None:
    """Simple moving average over the last ``window`` values, or ``None``."""
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def _technical_read(
    *, closes: list[float], quote_price: float | None
) -> dict[str, Any]:
    """Derive a price-vs-MA / trend read from closes + an optional live price.

    Computes SMAs for the configured windows, the current price's position
    relative to each available MA, a short-vs-long trend label, and an overall
    coarse technical stance (bullish / bearish / mixed / neutral).
    """
    price = quote_price if quote_price is not None else (closes[-1] if closes else None)

    mas: dict[str, float | None] = {}
    for window in _MA_WINDOWS:
        mas[f"sma_{window}"] = _sma(closes, window)

    available_mas = {k: v for k, v in mas.items() if v is not None}

    above: list[str] = []
    below: list[str] = []
    if price is not None:
        for name, ma in available_mas.items():
            if price >= ma:
                above.append(name)
            else:
                below.append(name)

    below_all_mas = bool(available_mas) and len(below) == len(available_mas)
    above_all_mas = bool(available_mas) and len(above) == len(available_mas)

    # Short-vs-long trend (golden/death-cross style) when both MAs exist.
    trend: str | None = None
    short_ma = mas.get("sma_50")
    long_ma = mas.get("sma_200")
    if short_ma is not None and long_ma is not None:
        if short_ma > long_ma:
            trend = "uptrend"
        elif short_ma < long_ma:
            trend = "downtrend"
        else:
            trend = "flat"

    # Coarse overall stance. Price position vs. the MAs is the primary driver
    # (price below ALL MAs is the canonical bearish read); the short-vs-long
    # trend only *vetoes* when it actively contradicts that position (e.g. price
    # below all MAs but a confirmed uptrend -> "mixed"). A flat or absent trend
    # does not downgrade an otherwise clean above-/below-all read.
    if not available_mas or price is None:
        stance = "unknown"
    elif above_all_mas and trend != "downtrend":
        stance = "bullish"
    elif below_all_mas and trend != "uptrend":
        stance = "bearish"
    else:
        stance = "mixed"

    return {
        "available": price is not None and bool(available_mas),
        "price": price,
        "moving_averages": mas,
        "above_mas": above,
        "below_mas": below,
        "below_all_mas": below_all_mas,
        "above_all_mas": above_all_mas,
        "trend": trend,
        "stance": stance,
        "n_bars": len(closes),
    }


# Candidate fundamentals keys, in priority order, across the provider shapes
# (yfinance ``Ticker.info`` camelCase; FMP key-metrics/ratios). The first match
# wins. Values are fractions (e.g. 0.42) or percentages depending on provider;
# we only ever compare them to each other for *trend*, never assume units.
_MARGIN_KEYS: dict[str, tuple[str, ...]] = {
    "gross_margin": ("grossMargins", "grossProfitMargin", "grossMargin"),
    "operating_margin": ("operatingMargins", "operatingProfitMargin", "operatingMargin"),
    "net_margin": ("profitMargins", "netProfitMargin", "netMargin"),
}

_VALUATION_KEYS: dict[str, tuple[str, ...]] = {
    "pe": ("trailingPE", "peRatio", "priceEarningsRatio", "forwardPE"),
    "pb": ("priceToBook", "pbRatio", "priceBookValueRatio"),
    "ps": ("priceToSalesTrailing12Months", "priceToSalesRatio", "psRatio"),
}


def _flatten_fundamentals(fund_data: Any) -> dict[str, Any]:
    """Merge the various fundamentals payload shapes into one flat dict.

    Handles the yfinance shape ``{"fundamentals": {...}}`` and the FMP shape
    ``{"key_metrics": {...}, "ratios": {...}}``, plus a bare flat dict. Later
    sources do not clobber earlier non-null values.
    """
    if not isinstance(fund_data, dict):
        return {}
    flat: dict[str, Any] = {}
    sub_blocks = ("fundamentals", "key_metrics", "ratios", "metrics")
    # First, fold in any recognized sub-blocks.
    for block in sub_blocks:
        block_val = fund_data.get(block)
        if isinstance(block_val, dict):
            for k, v in block_val.items():
                if k not in flat or flat[k] is None:
                    flat[k] = v
    # Then fold in top-level scalar keys that are not the sub-blocks themselves.
    for k, v in fund_data.items():
        if k in sub_blocks or k == "provenance":
            continue
        if k not in flat or flat[k] is None:
            flat[k] = v
    return flat


def _first_present(flat: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first finite float among ``keys`` in ``flat`` (or ``None``)."""
    for key in keys:
        if key in flat:
            val = _to_float(flat[key])
            if val is not None:
                return val
    return None


def _margin_trend(
    latest_flat: dict[str, Any], prior_flat: dict[str, Any] | None
) -> dict[str, Any]:
    """Assess margin direction from latest vs. (optional) prior fundamentals.

    For each margin we compare the latest value to the prior period's value (when
    a prior snapshot is supplied). Direction is ``expanding`` /
    ``compressing`` / ``stable`` based on relative change vs. ``_MARGIN_TREND_EPS``.
    ``overall`` summarizes across margins: ``compressing`` if any compresses and
    none expands, ``expanding`` if any expands and none compresses, else
    ``mixed`` / ``stable`` / ``unknown``.
    """
    margins: dict[str, Any] = {}
    directions: list[str] = []
    for label, keys in _MARGIN_KEYS.items():
        latest = _first_present(latest_flat, keys)
        prior = _first_present(prior_flat, keys) if prior_flat else None
        direction: str | None = None
        if latest is not None and prior is not None:
            base = abs(prior) if prior != 0 else None
            if base is None:
                direction = "stable"
            else:
                rel = (latest - prior) / base
                if rel > _MARGIN_TREND_EPS:
                    direction = "expanding"
                elif rel < -_MARGIN_TREND_EPS:
                    direction = "compressing"
                else:
                    direction = "stable"
            directions.append(direction)
        margins[label] = {"latest": latest, "prior": prior, "direction": direction}

    if not directions:
        overall = "unknown"
    elif "compressing" in directions and "expanding" not in directions:
        overall = "compressing"
    elif "expanding" in directions and "compressing" not in directions:
        overall = "expanding"
    elif "expanding" in directions and "compressing" in directions:
        overall = "mixed"
    else:
        overall = "stable"

    return {"margins": margins, "overall": overall}


def _fundamentals_read(latest: Any, prior: Any) -> dict[str, Any]:
    """Reduce fundamentals payload(s) to valuation + margin-trend read."""
    latest_flat = _flatten_fundamentals(latest)
    prior_flat = _flatten_fundamentals(prior) if prior is not None else None

    valuation = {
        name: _first_present(latest_flat, keys) for name, keys in _VALUATION_KEYS.items()
    }
    trend = _margin_trend(latest_flat, prior_flat)

    has_any = bool(latest_flat) and (
        any(v is not None for v in valuation.values())
        or any(m["latest"] is not None for m in trend["margins"].values())
    )

    return {
        "available": has_any,
        "valuation": valuation,
        "margin_trend": trend,
        "have_prior_period": prior_flat is not None,
    }


# ---------------------------------------------------------------------------
# Divergence assessment: reconcile the three reads into pairwise agreement +
# named mismatch flags + an overall label/score.
# ---------------------------------------------------------------------------


def _assess_divergence(
    consensus: dict[str, Any],
    technical: dict[str, Any],
    price_target: dict[str, Any],
    fundamentals: dict[str, Any],
) -> dict[str, Any]:
    """Compute pairwise agreement, mismatch flags, and an overall divergence read.

    Each pairwise signal is ``agree`` / ``conflict`` / ``unknown``. Flags are
    human-readable strings describing a concrete mismatch (the contract's
    canonical example: consensus Buy but price below all MAs and margins
    compressing). The overall ``divergence_score`` in ``[0, 1]`` is the fraction
    of *evaluable* pairwise signals that conflict; it maps to a label.
    """
    flags: list[str] = []
    signals: dict[str, str] = {}

    c_label = consensus.get("label") if consensus.get("available") else None
    t_stance = technical.get("stance") if technical.get("available") else None
    f_overall = (
        fundamentals.get("margin_trend", {}).get("overall")
        if fundamentals.get("available")
        else None
    )

    # -- consensus vs. price/technicals -----------------------------------
    if c_label is not None and t_stance in ("bullish", "bearish", "mixed"):
        if c_label == "Buy" and t_stance == "bearish":
            signals["consensus_vs_price"] = "conflict"
            detail = "consensus Buy but price is below all moving averages"
            if technical.get("trend") == "downtrend":
                detail += " in a downtrend"
            flags.append(detail)
        elif c_label == "Sell" and t_stance == "bullish":
            signals["consensus_vs_price"] = "conflict"
            flags.append("consensus Sell but price is above all moving averages in an uptrend")
        elif (c_label == "Buy" and t_stance == "bullish") or (
            c_label == "Sell" and t_stance == "bearish"
        ):
            signals["consensus_vs_price"] = "agree"
        else:
            signals["consensus_vs_price"] = "mixed"
    else:
        signals["consensus_vs_price"] = "unknown"

    # -- consensus vs. fundamentals (margin trend) ------------------------
    if c_label is not None and f_overall in ("expanding", "compressing", "mixed", "stable"):
        if c_label == "Buy" and f_overall == "compressing":
            signals["consensus_vs_fundamentals"] = "conflict"
            flags.append("consensus Buy but margins are compressing")
        elif c_label == "Sell" and f_overall == "expanding":
            signals["consensus_vs_fundamentals"] = "conflict"
            flags.append("consensus Sell but margins are expanding")
        elif (c_label == "Buy" and f_overall == "expanding") or (
            c_label == "Sell" and f_overall == "compressing"
        ):
            signals["consensus_vs_fundamentals"] = "agree"
        else:
            signals["consensus_vs_fundamentals"] = "mixed"
    else:
        signals["consensus_vs_fundamentals"] = "unknown"

    # -- price/technicals vs. fundamentals --------------------------------
    if t_stance in ("bullish", "bearish") and f_overall in ("expanding", "compressing"):
        if (t_stance == "bullish" and f_overall == "expanding") or (
            t_stance == "bearish" and f_overall == "compressing"
        ):
            signals["price_vs_fundamentals"] = "agree"
        else:
            signals["price_vs_fundamentals"] = "conflict"
            flags.append(
                f"price action is {t_stance} but margins are {f_overall}"
            )
    else:
        signals["price_vs_fundamentals"] = "unknown"

    # -- price-target richness vs. bearish technicals ---------------------
    upside = price_target.get("implied_upside_pct") if price_target.get("available") else None
    if upside is not None and t_stance == "bearish" and upside >= _TARGET_UPSIDE_FLAG * 100.0:
        flags.append(
            f"analyst mean target implies {upside:.1f}% upside but price is below all MAs"
        )

    # -- overall divergence score -----------------------------------------
    evaluable = [v for v in signals.values() if v in ("agree", "conflict", "mixed")]
    conflicts = sum(1 for v in evaluable if v == "conflict")
    mixed = sum(1 for v in evaluable if v == "mixed")
    if evaluable:
        # Conflicts weigh fully; "mixed" counts as a half-conflict.
        divergence_score = round((conflicts + 0.5 * mixed) / len(evaluable), 4)
    else:
        divergence_score = None

    if divergence_score is None:
        label = "insufficient_data"
    elif divergence_score >= 0.5 or conflicts >= 2:
        label = "high_divergence"
    elif divergence_score > 0.0:
        label = "some_divergence"
    else:
        label = "aligned"

    return {
        "label": label,
        "divergence_score": divergence_score,
        "signals": signals,
        "flags": flags,
        "n_conflicts": conflicts,
    }


def _build_summary(
    symbol: str,
    consensus: dict[str, Any],
    technical: dict[str, Any],
    fundamentals: dict[str, Any],
    assessment: dict[str, Any],
) -> str:
    """Render a one-line human-readable summary of the cross-check."""
    parts: list[str] = [f"{symbol}:"]
    parts.append(
        f"consensus={consensus.get('label', 'n/a')}"
        if consensus.get("available")
        else "consensus=n/a"
    )
    parts.append(
        f"technical={technical.get('stance', 'n/a')}"
        if technical.get("available")
        else "technical=n/a"
    )
    if fundamentals.get("available"):
        parts.append(f"margins={fundamentals['margin_trend']['overall']}")
    else:
        parts.append("margins=n/a")
    parts.append(f"-> {assessment['label']}")
    if assessment["flags"]:
        parts.append(f"({len(assessment['flags'])} mismatch flag(s))")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Pure logic entrypoint (unit-testable; returns a plain dict)
# ---------------------------------------------------------------------------


async def cross_check(symbol: str) -> dict[str, Any]:
    """Reconcile analyst consensus vs. price/technicals vs. fundamentals.

    Gathers four capability views for ``symbol`` — ``analyst_ratings`` and
    ``price_targets`` (the *consensus* view, from the ``reports`` domain),
    ``quote``/``ohlcv`` (the *price/technical* view), and ``fundamentals`` (one
    current snapshot plus, when obtainable, a prior-period snapshot for the
    margin trend) — then computes a divergence assessment that flags concrete
    mismatches (e.g. *consensus Buy but price below all MAs and margins
    compressing*).

    Sibling ``technical``/``reports`` logic functions are used opportunistically
    when present (read-only composition); otherwise the same capabilities are
    fetched directly through the registry. Missing/failed views degrade
    gracefully into a partial assessment rather than raising.

    Args:
        symbol: Ticker symbol (any case; a leading ``$`` is tolerated).

    Returns:
        A JSON-serializable dict with ``symbol``, the three ``views`` (consensus,
        technical, fundamentals) plus ``price_target``, the ``divergence``
        assessment (label, score, pairwise signals, mismatch flags), a one-line
        ``summary``, and a ``sources`` map of which provider served each view.
    """
    sym = normalize_symbol(symbol)

    # --- gather raw capability payloads (best-effort, never raising) -----
    # Prefer sibling logic where available; fall back to direct registry fetch.

    ratings_env = await _view_via_sibling_or_fetch(
        "reports", "analyst_ratings", "analyst_ratings", symbol=sym
    )
    targets_env = await _view_via_sibling_or_fetch(
        "reports", "price_targets", "price_targets", symbol=sym
    )

    quote_env = await _safe_fetch("quote", symbol=sym)
    ohlcv_env = await _safe_fetch("ohlcv", symbol=sym, interval="1d", period="1y")
    fundamentals_env = await _safe_fetch("fundamentals", symbol=sym)
    # A prior-period fundamentals snapshot (FMP supports period/limit). yfinance
    # ignores extra params; on a failure we simply lack a margin trend.
    prior_fund_env = await _safe_fetch("fundamentals", symbol=sym, period="annual", limit=2)

    # --- reduce to compact, comparable reads -----------------------------

    quote_price = None
    if quote_env["ok"] and isinstance(quote_env["data"], dict):
        quote_price = _to_float(quote_env["data"].get("price"))

    closes = _closes_from_ohlcv(ohlcv_env["data"]) if ohlcv_env["ok"] else []

    consensus = (
        _consensus_from_ratings(ratings_env.get("data"))
        if ratings_env.get("ok")
        else {"available": False}
    )
    technical = _technical_read(closes=closes, quote_price=quote_price)
    price_target = _price_target_read(
        targets_env.get("data") if targets_env.get("ok") else None,
        fallback_price=quote_price if quote_price is not None else (closes[-1] if closes else None),
    )

    prior_payload = _extract_prior_fundamentals(
        prior_fund_env["data"] if prior_fund_env["ok"] else None
    )
    fundamentals = _fundamentals_read(
        fundamentals_env["data"] if fundamentals_env["ok"] else None,
        prior_payload,
    )

    # --- assess divergence -----------------------------------------------

    assessment = _assess_divergence(consensus, technical, price_target, fundamentals)
    summary = _build_summary(sym, consensus, technical, fundamentals, assessment)

    return {
        "symbol": sym,
        "summary": summary,
        "divergence": assessment,
        "views": {
            "consensus": consensus,
            "technical": technical,
            "fundamentals": fundamentals,
        },
        "price_target": price_target,
        "sources": {
            "analyst_ratings": _source_of(ratings_env),
            "price_targets": _source_of(targets_env),
            "quote": _source_of(quote_env),
            "ohlcv": _source_of(ohlcv_env),
            "fundamentals": _source_of(fundamentals_env),
        },
        "errors": _collect_errors(
            analyst_ratings=ratings_env,
            price_targets=targets_env,
            quote=quote_env,
            ohlcv=ohlcv_env,
            fundamentals=fundamentals_env,
        ),
    }


def _extract_prior_fundamentals(prior_data: Any) -> Any:
    """Pull a prior-period fundamentals snapshot from a multi-period payload.

    FMP's fundamentals call with ``limit=2`` may return a richer shape; the
    provider in this codebase collapses to the latest period, so in practice a
    distinct prior snapshot is often unavailable. This helper accepts either a
    list of period dicts (returns the second) or a single dict (no prior ->
    ``None``), keeping the margin-trend logic robust to either shape.
    """
    if isinstance(prior_data, list) and len(prior_data) >= 2:
        return prior_data[1]
    if isinstance(prior_data, dict):
        # Some shapes nest period lists under a sub-block.
        for block in ("key_metrics", "ratios", "fundamentals"):
            seq = prior_data.get(block)
            if isinstance(seq, list) and len(seq) >= 2:
                return {block: seq[1]}
    return None


def _source_of(env: dict[str, Any]) -> str | None:
    """Return the provider name for a view, or ``None`` if it was unavailable."""
    if env.get("ok"):
        return env.get("provider")
    return None


def _collect_errors(**envs: dict[str, Any]) -> dict[str, str]:
    """Collect per-view error strings for views that failed to fetch."""
    out: dict[str, str] = {}
    for name, env in envs.items():
        if not env.get("ok") and env.get("error"):
            out[name] = env["error"]
    return out


# ---------------------------------------------------------------------------
# MCP tool wiring (thin adapter -> text_result)
# ---------------------------------------------------------------------------


@tool(
    "cross_check",
    "Reconcile analyst consensus (ratings + price targets) against current "
    "price/technicals and fundamentals (margin trend, valuation) for a symbol, "
    "computing a divergence assessment and flagging concrete mismatches "
    "(e.g. consensus Buy but price below all moving averages and margins "
    "compressing). Informational only; not investment advice.",
    {"symbol": str},
)
async def cross_check_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`cross_check`.

    Reads ``symbol`` from the MCP tool ``args`` mapping, delegates to the pure
    logic function, and wraps the result in the canonical MCP text-content
    envelope. The underlying :func:`cross_check` stays directly importable and
    callable for tests.
    """
    symbol = args.get("symbol", "")
    result = await cross_check(symbol)
    return text_result(result)


# The MCP server instance for this capability (CONTRACT.md §9.1.3).
server = create_sdk_mcp_server(
    name="synthesis",
    version="0.1.0",
    tools=[cross_check_tool],
)


# ---------------------------------------------------------------------------
# stdio guard (CONTRACT.md §9.1.4)
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the synthesis MCP server over stdio.

    Returns a non-zero exit code with a clear message when the real Claude Agent
    SDK is not installed (the module still imports and its logic stays testable
    via the shims).
    """
    from ._sdk import SDK_AVAILABLE

    if not SDK_AVAILABLE:
        print(
            "claude_agent_sdk is not installed; the synthesis MCP server cannot "
            "run over stdio. Install it with: pip install claude-agent-sdk",
            flush=True,
        )
        return 1

    # When the real SDK is present, ``server`` is a genuine MCP server object.
    # The SDK exposes a stdio runner; defer to it. Imported lazily so this module
    # imports without the SDK.
    try:
        from claude_agent_sdk import run_mcp_server_stdio  # type: ignore[import-not-found]
    except Exception:
        try:
            server.run()  # type: ignore[attr-defined]
            return 0
        except Exception as exc:  # pragma: no cover - depends on SDK internals
            print(f"Failed to start synthesis server over stdio: {exc}", flush=True)
            return 1
    else:  # pragma: no cover - exercised only with the real SDK installed
        run_mcp_server_stdio(server)
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["cross_check", "cross_check_tool", "server", "get_registry", "main"]
