"""Walk-forward backtesting + overfit-aware metrics (CONTRACT.md §10.7.4).

Validates the *price-based* engine signals honestly on free daily history. The
strategy is a deterministic **trend + time-series-momentum long/flat** rule (long
only when price is above its 200-day SMA *and* 12-1 momentum is positive — the two
most replicated price signals; plan.md §10). The backtest is walk-forward by
construction: the position on day *t* uses only data through *t* and is applied to
the *t→t+1* return, with transaction costs charged on position changes.

It reports CAGR / annualized Sharpe / max-drawdown / hit-rate / exposure versus
buy-and-hold, plus the **Probabilistic** and **Deflated Sharpe Ratio**
(Bailey & López de Prado) so a good-looking Sharpe is discounted for sample length,
non-normality, and the number of strategy variants tried.

Honest scope: only signals with free *history* can be backtested — price/factor
rules. Analyst/congress/sentiment lack free point-in-time history, so they are not
included here (avoiding look-ahead). Pure cores operate on bars; the async fetcher
pulls history through the cached registry.
"""

from __future__ import annotations

import math
from typing import Any

from .factors import _floats, momentum_12_1, trend_vs_sma

_YEAR = 252
_EULER = 0.5772156649015329  # Euler-Mascheroni constant


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _moments(rets: list[float]) -> dict[str, float]:
    """Mean, std (sample), skew, kurtosis (non-excess) of a return series."""
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    skew = kurt = 0.0
    if std > 0 and n > 2:
        m3 = sum((r - mean) ** 3 for r in rets) / n
        m4 = sum((r - mean) ** 4 for r in rets) / n
        skew = m3 / std**3
        kurt = m4 / std**4
    return {"n": n, "mean": mean, "std": std, "skew": skew, "kurt": kurt}


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float, kurt: float, sr_star: float = 0.0) -> float:
    """PSR: P(true per-period Sharpe > ``sr_star``) given the observed estimate."""
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom <= 0 or n < 2:
        return float("nan")
    return _norm_cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom))


def deflated_sharpe_ratio(sr: float, n: int, skew: float, kurt: float, n_trials: int) -> float:
    """DSR: PSR with the benchmark set to the expected max Sharpe under the null.

    ``n_trials`` is the number of strategy configurations effectively tried. The
    cross-trial Sharpe variance is approximated by the estimator variance.
    """
    if n < 2 or n_trials < 1:
        return float("nan")
    var_sr = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr) / (n - 1)
    if var_sr <= 0:
        return float("nan")
    nn = max(n_trials, 1)
    z1 = _norm_ppf(1.0 - 1.0 / nn)
    z2 = _norm_ppf(1.0 - 1.0 / (nn * math.e))
    sr0 = math.sqrt(var_sr) * ((1.0 - _EULER) * z1 + _EULER * z2)
    return probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_star=sr0)


def _max_drawdown(equity: list[float]) -> float:
    """Maximum peak-to-trough drawdown of an equity curve (negative fraction)."""
    peak = -math.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def backtest_long_flat(bars: list[dict[str, Any]], *, cost_bps: float = 10.0, n_trials: int = 10) -> dict[str, Any]:
    """Backtest the trend+momentum long/flat rule on ``bars`` (pure, walk-forward).

    Returns strategy vs buy-and-hold metrics plus PSR/DSR. Needs > ~1.2y of bars;
    otherwise returns ``{"_error": "insufficient history"}``.
    """
    closes = _floats(bars, "close")
    if len(closes) < _YEAR + 30:
        return {"_error": "insufficient history", "n_bars": len(closes)}

    cost = cost_bps / 10000.0
    strat: list[float] = []
    bh: list[float] = []
    prev_pos = 0
    invested_days = pos_days = wins = trades = 0
    for i in range(_YEAR, len(closes) - 1):
        window = closes[: i + 1]
        above = (trend_vs_sma(window, 200) or -1.0) > 0
        mom = momentum_12_1(window)
        pos = 1 if (above and mom is not None and mom > 0) else 0
        nxt = closes[i + 1] / closes[i] - 1.0
        r = pos * nxt
        if pos != prev_pos:
            r -= cost
            trades += 1
        if pos:
            pos_days += 1
            if nxt > 0:
                wins += 1
        invested_days += 1
        strat.append(r)
        bh.append(nxt)
        prev_pos = pos

    def _curve(rets: list[float]) -> list[float]:
        eq = [1.0]
        for r in rets:
            eq.append(eq[-1] * (1.0 + r))
        return eq

    m = _moments(strat)
    sr_daily = (m["mean"] / m["std"]) if m["std"] > 0 else 0.0
    sr_ann = sr_daily * math.sqrt(_YEAR)
    n_days = len(strat)
    strat_curve = _curve(strat)
    bh_curve = _curve(bh)
    total_return = strat_curve[-1] - 1.0
    cagr = strat_curve[-1] ** (_YEAR / n_days) - 1.0 if n_days > 0 else 0.0
    bh_total = bh_curve[-1] - 1.0
    bh_m = _moments(bh)
    bh_sr_ann = (bh_m["mean"] / bh_m["std"] * math.sqrt(_YEAR)) if bh_m["std"] > 0 else 0.0

    return {
        "n_bars": len(closes),
        "n_days": n_days,
        "exposure": round(pos_days / invested_days, 4) if invested_days else 0.0,
        "n_trades": trades,
        "cost_bps": cost_bps,
        "strategy": {
            "total_return": round(total_return, 4),
            "cagr": round(cagr, 4),
            "ann_vol": round(m["std"] * math.sqrt(_YEAR), 4),
            "sharpe": round(sr_ann, 4),
            "max_drawdown": round(_max_drawdown(strat_curve), 4),
            "hit_rate": round(wins / pos_days, 4) if pos_days else 0.0,
        },
        "buy_hold": {
            "total_return": round(bh_total, 4),
            "sharpe": round(bh_sr_ann, 4),
            "max_drawdown": round(_max_drawdown(bh_curve), 4),
        },
        "overfit_checks": {
            "psr_vs_0": round(probabilistic_sharpe_ratio(sr_daily, n_days, m["skew"], m["kurt"]), 4),
            "deflated_sharpe": round(deflated_sharpe_ratio(sr_daily, n_days, m["skew"], m["kurt"], n_trials), 4),
            "n_trials_assumed": n_trials,
            "note": "PSR/DSR > 0.95 is the usual bar; lower means the Sharpe may be noise/overfit.",
        },
        "signal": "trend(>200DMA) AND time-series momentum(12-1>0), long/flat",
    }


async def backtest(
    symbol: str, *, period: str = "10y", cost_bps: float = 10.0, n_trials: int = 10, settings: Any = None
) -> dict[str, Any]:
    """Fetch daily history and backtest the trend+momentum long/flat rule.

    Never raises — returns ``{"_error": ...}`` on a data failure.
    """
    from ..servers import technical as tech
    from ..servers._common import normalize_symbol

    sym = normalize_symbol(symbol)
    try:
        ohlcv = await tech.get_ohlcv(sym, interval="1d", period=period)
        bars = ohlcv.get("bars", []) if isinstance(ohlcv, dict) else []
    except Exception as exc:
        return {"symbol": sym, "_error": f"{type(exc).__name__}: {exc}"}
    result = backtest_long_flat(bars, cost_bps=cost_bps, n_trials=n_trials)
    result["symbol"] = sym
    result["period"] = period
    return result


__all__ = [
    "backtest_long_flat",
    "backtest",
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
]
