"""Streamlit dashboard for MakeCrazyPenny.

A single-ticker analyst cockpit that renders, in one page:

* **Overview** — the ``synthesis.cross_check`` reconciliation (consensus vs.
  price/technicals vs. fundamentals) with its divergence verdict.
* **Technical** — price chart, latest indicators, and detected signals.
* **Sentiment** — blended news + social sentiment and recent headlines.
* **Congress** — disclosed congressional + insider trades (with the lag caveat).
* **Reports** — analyst rating distribution, price targets, rating changes, filings.

Design notes
------------
* This module is import-safe **without** Streamlit installed: the import is guarded
  and the app body only runs under the ``streamlit run`` runtime. That keeps the
  recursive import smoke-test green in environments that lack the UI extra.
* It calls the Layer-1 server **logic functions** (e.g. ``technical.get_ohlcv``),
  which return plain dicts — *not* the MCP ``text_result`` envelope used by the
  ``*_tool`` wrappers. No business logic lives here.
* Data is fetched lazily, only when the user clicks **Analyze**, and cached for a
  few minutes so switching tabs does not re-hit the providers.

Run it with::

    makecrazypenny-dashboard            # console script
    # or
    streamlit run makecrazypenny/ui/dashboard.py

Informational only; NOT investment advice.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd

from makecrazypenny.core.disclaimer import DISCLAIMER
from makecrazypenny.core.errors import AllProvidersFailed
from makecrazypenny.core.redact import redact_secrets

try:  # UI is an optional extra; stay importable without it.
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover - exercised only when extra absent
    st = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Pure helpers (no Streamlit dependency — unit-testable).                      #
# --------------------------------------------------------------------------- #


def fmt_num(value: Any, ndigits: int = 2) -> str:
    """Format a number for display, tolerating ``None``/non-numeric input."""
    try:
        if value is None:
            return "—"
        return f"{float(value):,.{ndigits}f}"
    except (TypeError, ValueError):
        return str(value)


def fmt_pct(value: Any, ndigits: int = 1) -> str:
    """Format a fraction (e.g. ``0.123``) as a percentage string."""
    try:
        if value is None:
            return "—"
        return f"{float(value) * 100:.{ndigits}f}%"
    except (TypeError, ValueError):
        return str(value)


def as_records(value: Any) -> list[dict]:
    """Coerce a payload into a list of dict records.

    Accepts a list (returned as-is, dicts only), a single dict (wrapped), or
    anything else (``[]``). Used to feed provider payloads to a DataFrame.
    """
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def df_from_bars(bars: Any) -> pd.DataFrame:
    """Build a time-indexed OHLCV DataFrame from a list of bar dicts.

    Each bar is expected to have ``ts`` plus ``open/high/low/close/volume``.
    Returns an empty DataFrame when there is nothing usable.
    """
    records = as_records(bars)
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
        df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def has_error(payload: Any) -> str | None:
    """Return an error string if ``payload`` carries one, else ``None``."""
    if isinstance(payload, dict):
        err = payload.get("_error") or payload.get("error")
        return str(err) if err else None
    return None


def explain_failure(exc: BaseException) -> str:
    """Turn a fetch failure into an actionable, non-alarming message.

    A chain exhausted purely because of missing API keys is a *configuration*
    state, not a malfunction — surface the exact env vars to set instead of a
    generic "all providers failed".
    """
    if isinstance(exc, AllProvidersFailed):
        keys = exc.missing_api_keys
        if keys:
            return "needs an API key — set " + " or ".join(keys) + " in your .env"
        return redact_secrets(str(exc))
    return redact_secrets(f"{type(exc).__name__}: {exc}")


def under_streamlit() -> bool:
    """True only while executing under the ``streamlit run`` runtime."""
    try:
        from streamlit.runtime import exists

        return bool(exists())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Data loading (async fan-out across the Layer-1 logic functions).            #
# --------------------------------------------------------------------------- #

# Section -> capability key list, used to label the loading set. The actual
# coroutine wiring lives in ``_gather`` so it can import lazily.
PERIODS = ["3mo", "6mo", "1y", "2y", "5y"]
INTERVALS = ["1d", "1wk", "1mo"]


async def _gather(symbol: str, interval: str, period: str, news_days: int) -> dict[str, Any]:
    """Fetch every panel's data concurrently; never raises.

    Each task's result is the server logic function's plain dict, or an
    ``{"_error": ...}`` marker if that single call raised.
    """
    from makecrazypenny.servers import congress as cong
    from makecrazypenny.servers import reports as rep
    from makecrazypenny.servers import sentiment as sent
    from makecrazypenny.servers import synthesis as syn
    from makecrazypenny.servers import technical as tech

    tasks: dict[str, Any] = {
        "cross_check": syn.cross_check(symbol),
        "ohlcv": tech.get_ohlcv(symbol, interval, period),
        "indicators": tech.compute_indicators(symbol),
        "signals": tech.detect_signals(symbol),
        "support_resistance": tech.support_resistance(symbol),
        "mtf": tech.multi_timeframe_summary(symbol),
        "aggregate_sentiment": sent.aggregate_sentiment(symbol),
        "news": sent.get_news(symbol, news_days),
        "congress_trades": cong.congress_trades(symbol),
        "insider": cong.insider_transactions(symbol),
        "ratings": rep.analyst_ratings(symbol),
        "price_targets": rep.price_targets(symbol),
        "upgrades": rep.upgrades_downgrades(symbol),
        "filings": rep.sec_filings(symbol),
    }
    keys = list(tasks)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out: dict[str, Any] = {}
    for key, res in zip(keys, results):
        if isinstance(res, BaseException):
            out[key] = {"_error": explain_failure(res)}
        else:
            out[key] = res
    return out


def run_load(symbol: str, interval: str, period: str, news_days: int) -> dict[str, Any]:
    """Synchronous wrapper around :func:`_gather` (its own event loop)."""
    return asyncio.run(_gather(symbol, interval, period, news_days))


# --------------------------------------------------------------------------- #
# Rendering (Streamlit). Each panel is defensive: one bad shape never breaks   #
# the page — it degrades to a warning + raw JSON.                              #
# --------------------------------------------------------------------------- #


def _safe_panel(title: str, payload: Any, render_fn) -> None:
    """Render one panel, catching shape surprises and showing raw JSON instead."""
    err = has_error(payload)
    if err:
        st.info(f"{title}: no data — {err}")
        return
    try:
        render_fn(payload)
    except Exception as exc:  # pragma: no cover - UI guardrail
        st.warning(f"{title}: could not render ({type(exc).__name__}: {exc}).")
        st.json(payload)


def _decision_dossier(symbol: str, data: dict[str, Any]) -> dict[str, Any]:
    """Map the dashboard's fetched ``data`` into the debate-engine dossier shape.

    The scorer is defensive, so ``{"_error": ...}`` markers simply contribute
    nothing rather than breaking the decision.
    """
    return {
        "symbol": symbol,
        "signals": data.get("signals"),
        "mtf": data.get("mtf"),
        "sentiment": data.get("aggregate_sentiment"),
        "congress": data.get("congress_trades"),
        "insider": data.get("insider"),
        "ratings": data.get("ratings"),
        "price_targets": data.get("price_targets"),
        "upgrades": data.get("upgrades"),
        "cross_check": data.get("cross_check"),
    }


def _render_decision(symbol: str, data: dict[str, Any]) -> None:
    """Render the autonomous BUY/SHORT/AVOID decision (headline panel).

    The instant verdict is the deterministic quant backbone computed from the
    already-fetched evidence. A button runs the full bull-vs-bear AI debate +
    orchestrator judge (needs the Claude Agent SDK and API keys).
    """
    from makecrazypenny.orchestration.debate import (
        decide_from_scores,
        score_evidence,
    )

    st.caption(
        "An autonomous decision: a bull and a bear debate the evidence and an "
        "orchestrator decides. The instant verdict below is the deterministic "
        "quant backbone; run the full AI debate for the reasoned version."
    )

    dossier = _decision_dossier(symbol, data)
    scored = score_evidence(dossier)
    dec = decide_from_scores(symbol, scored, method="quant-only").to_dict()

    action = dec["action"]
    badge = {"BUY": "🟢 BUY (go long)", "SHORT": "🔴 SHORT", "AVOID": "🟡 AVOID"}.get(action, action)
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.metric("Verdict", badge)
    c2.metric("Conviction", fmt_pct(dec["conviction"]))
    c3.metric("Net score", fmt_num(dec["net_score"]))
    st.progress(min(1.0, max(0.0, float(dec["conviction"]))))
    if dec.get("summary"):
        st.markdown(f"**{dec['summary']}**")

    bcol, rcol = st.columns(2)
    with bcol:
        st.markdown("**🟢 Bull case**")
        for pt in dec.get("bull_case") or ["—"]:
            st.markdown(f"- {pt}")
    with rcol:
        st.markdown("**🔴 Bear case**")
        for pt in dec.get("bear_case") or ["—"]:
            st.markdown(f"- {pt}")

    factors = dec.get("factors") or []
    if factors:
        with st.expander("Quant factor breakdown"):
            st.dataframe(
                pd.DataFrame(factors)[["category", "name", "side", "contribution", "detail"]],
                use_container_width=True,
                hide_index=True,
            )

    st.divider()
    st.markdown("**Full bull vs bear AI debate**")
    st.caption(
        "The verdict above is the deterministic quant baseline. The full "
        "bull-vs-bear debate runs on **your own Claude subscription** via the MCP "
        "server — no API key, nothing billed per token. Mount it in your MCP host "
        "(Claude Desktop / Claude Code) and run the `decide` prompt:"
    )
    st.code("claude mcp add makecrazypenny -- makecrazypenny-mcp", language="bash")
    st.markdown(f"Then in the host, run the **/decide** prompt for `{symbol}` (it orchestrates bull → bear → judge using this server's tools).")
    from makecrazypenny.mcp_server import build_decide_prompt

    with st.expander("Preview the `decide` prompt the host will run"):
        st.code(build_decide_prompt(symbol), language="text")


def _render_overview(data: dict[str, Any]) -> None:
    cc = data.get("cross_check", {})
    err = has_error(cc)
    if err:
        st.error(f"Cross-check unavailable — {err}")
        return

    st.subheader("Cross-check verdict")
    summary = cc.get("summary")
    if summary:
        st.markdown(f"> {summary}")

    div = cc.get("divergence", {}) or {}
    pt = cc.get("price_target", {}) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Divergence", str(div.get("label", "—")))
    c2.metric("Divergence score", fmt_num(div.get("score")))
    upside = pt.get("upside") if isinstance(pt, dict) else None
    c3.metric(
        "Target (mean)",
        fmt_num(pt.get("mean")) if isinstance(pt, dict) else "—",
        delta=fmt_pct(upside) if upside is not None else None,
    )

    flags = (
        div.get("flags")
        or div.get("mismatch_flags")
        or div.get("mismatches")
        or []
    )
    if flags:
        st.markdown("**Flagged divergences**")
        for flag in flags:
            st.markdown(f"- {flag}")

    signals = div.get("signals") or div.get("pairwise")
    if signals:
        with st.expander("Pairwise agreement"):
            st.json(signals)

    with st.expander("Views & sources"):
        st.write("**Views**")
        st.json(cc.get("views", {}))
        st.write("**Sources** (which provider served each view)")
        st.json(cc.get("sources", {}))
        if cc.get("errors"):
            st.write("**Per-view errors**")
            st.json(cc["errors"])


def _render_technical(data: dict[str, Any]) -> None:
    ohlcv = data.get("ohlcv", {})

    def _price(payload: dict) -> None:
        df = df_from_bars(payload.get("bars"))
        if df.empty or "close" not in df.columns:
            st.info("No price bars available.")
            return
        last = df["close"].iloc[-1]
        first = df["close"].iloc[0]
        chg = (last - first) / first if first else None
        m1, m2 = st.columns(2)
        m1.metric("Last close", fmt_num(last), delta=fmt_pct(chg) if chg is not None else None)
        m2.metric("Bars", str(payload.get("n_bars", len(df))))
        st.line_chart(df[["close"]])
        st.caption(f"Source: {payload.get('provider', '?')} · period {payload.get('period', '?')}")

    _safe_panel("Price", ohlcv, _price)

    st.divider()
    st.subheader("Latest indicators")

    def _indicators(payload: dict) -> None:
        block = payload.get("indicators", {}) or {}
        if not block:
            st.info("No indicators computed.")
            return
        scalars = {k: v for k, v in block.items() if not isinstance(v, (dict, list))}
        if scalars:
            cols = st.columns(min(4, len(scalars)) or 1)
            for i, (name, val) in enumerate(scalars.items()):
                cols[i % len(cols)].metric(name.upper(), fmt_num(val))
        for name, val in block.items():
            if isinstance(val, dict):
                st.markdown(f"**{name.upper()}**")
                sub = st.columns(min(4, len(val)) or 1)
                for i, (k, v) in enumerate(val.items()):
                    sub[i % len(sub)].metric(k, fmt_num(v))

    _safe_panel("Indicators", data.get("indicators", {}), _indicators)

    st.divider()
    st.subheader("Signals")

    def _signals(payload: dict) -> None:
        sigs = payload.get("signals") or []
        if sigs:
            st.dataframe(pd.DataFrame(sigs), use_container_width=True, hide_index=True)
        else:
            st.info("No notable signals at the latest bar.")
        if payload.get("values"):
            with st.expander("Underlying values"):
                st.json(payload["values"])

    _safe_panel("Signals", data.get("signals", {}), _signals)

    with st.expander("Support / resistance"):
        st.json(data.get("support_resistance", {}))
    with st.expander("Multi-timeframe summary"):
        st.json(data.get("mtf", {}))


def _render_sentiment(data: dict[str, Any]) -> None:
    def _agg(payload: dict) -> None:
        c1, c2 = st.columns(2)
        c1.metric("Blended score", fmt_num(payload.get("score")))
        c2.metric("Label", str(payload.get("label", "—")))
        drivers = payload.get("drivers") or []
        if drivers:
            st.markdown("**Top drivers**")
            for d in drivers:
                st.markdown(f"- {d}")
        comps = payload.get("components")
        if comps:
            with st.expander("Components (news / social)"):
                st.json(comps)

    _safe_panel("Sentiment", data.get("aggregate_sentiment", {}), _agg)

    st.divider()
    st.subheader("Recent headlines")

    def _news(payload: dict) -> None:
        articles = as_records(payload.get("articles"))
        if not articles:
            st.info("No recent news (a provider key may be required).")
            return
        df = pd.DataFrame(articles)
        cols = [c for c in ("published_at", "headline", "source", "url") if c in df.columns]
        view = df[cols] if cols else df
        config = {}
        if "url" in view.columns and hasattr(st, "column_config"):
            try:
                config["url"] = st.column_config.LinkColumn("link")
            except Exception:
                config = {}
        st.dataframe(view, use_container_width=True, hide_index=True, column_config=config)
        st.caption(f"{payload.get('count', len(articles))} articles · {payload.get('provider', '?')}")

    _safe_panel("News", data.get("news", {}), _news)


def _render_congress(data: dict[str, Any]) -> None:
    st.caption(
        "Disclosure caveat: congressional and insider filings lag the actual trade "
        "(often 30–45 days). 'Recent' refers to disclosure date, not trade date."
    )

    def _trades(payload: dict) -> None:
        trades = as_records(payload.get("trades"))
        st.metric("Congressional trades", str(payload.get("count", len(trades))))
        if trades:
            st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
        else:
            st.info("No congressional trades found for this symbol.")

    _safe_panel("Congress trades", data.get("congress_trades", {}), _trades)

    st.divider()

    def _insider(payload: dict) -> None:
        txns = as_records(payload.get("transactions"))
        st.metric("Insider transactions", str(payload.get("count", len(txns))))
        if txns:
            st.dataframe(pd.DataFrame(txns), use_container_width=True, hide_index=True)
        else:
            st.info("No insider transactions found for this symbol.")

    _safe_panel("Insider transactions", data.get("insider", {}), _insider)


def _render_reports(data: dict[str, Any]) -> None:
    def _ratings(payload: dict) -> None:
        records = as_records(payload.get("ratings"))
        if not records:
            st.info("No analyst ratings available.")
            return
        latest = records[0]
        order = ["strong_buy", "buy", "hold", "sell", "strong_sell"]
        counts = {k: latest.get(k) for k in order if k in latest}
        if counts:
            chart = pd.DataFrame({"analysts": counts}).reindex(order).dropna()
            st.bar_chart(chart)
        st.caption(f"Period {latest.get('period', '?')} · {payload.get('provider', '?')}")

    _safe_panel("Analyst ratings", data.get("ratings", {}), _ratings)

    st.divider()
    st.subheader("Price targets")

    def _targets(payload: dict) -> None:
        t = payload.get("targets") or {}
        if isinstance(t, list):
            t = t[0] if t else {}
        if not t:
            st.info("No price targets available.")
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mean", fmt_num(t.get("mean")))
        c2.metric("High", fmt_num(t.get("high")))
        c3.metric("Low", fmt_num(t.get("low")))
        c4.metric("Current", fmt_num(t.get("current")))

    _safe_panel("Price targets", data.get("price_targets", {}), _targets)

    st.divider()
    st.subheader("Rating changes")

    def _upgrades(payload: dict) -> None:
        events = as_records(payload.get("events"))
        if events:
            st.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
        else:
            st.info("No recent upgrades/downgrades.")

    _safe_panel("Upgrades/downgrades", data.get("upgrades", {}), _upgrades)

    st.divider()
    st.subheader("SEC filings")

    def _filings(payload: dict) -> None:
        filings = as_records(payload.get("filings"))
        if filings:
            df = pd.DataFrame(filings)
            config = {}
            if "url" in df.columns and hasattr(st, "column_config"):
                try:
                    config["url"] = st.column_config.LinkColumn("link")
                except Exception:
                    config = {}
            st.dataframe(df, use_container_width=True, hide_index=True, column_config=config)
        else:
            st.info("No filings found.")

    _safe_panel("Filings", data.get("filings", {}), _filings)


def render() -> None:
    """Build and run the dashboard. Requires the Streamlit runtime."""
    st.set_page_config(page_title="MakeCrazyPenny", page_icon="📈", layout="wide")
    st.title("📈 MakeCrazyPenny")
    st.caption("Agentic financial analysis over MCP capability servers.")
    st.warning(
        "Informational only — **NOT investment advice.** "
        "Data comes from free-tier providers and may be delayed, partial, or absent "
        "(some panels need an API key in your `.env`).",
        icon="⚠️",
    )

    with st.sidebar:
        st.header("Analyze a ticker")
        with st.form("controls"):
            symbol = st.text_input("Ticker", value="AAPL", help="e.g. AAPL, MSFT, NVDA").strip()
            interval = st.selectbox("Interval", INTERVALS, index=0)
            period = st.selectbox("History", PERIODS, index=2)
            news_days = st.slider("News window (days)", 1, 30, 7)
            submitted = st.form_submit_button("Analyze", type="primary")
        st.caption("Keys (optional) live in `.env`; providers fall through their chain when absent.")

    if submitted and symbol:
        st.session_state["params"] = {
            "symbol": symbol,
            "interval": interval,
            "period": period,
            "news_days": int(news_days),
        }

    params = st.session_state.get("params")
    if not params:
        st.info("Enter a ticker in the sidebar and click **Analyze** to begin.")
        return

    loader = st.cache_data(ttl=300, show_spinner=False)(run_load)
    with st.spinner(f"Analyzing {params['symbol']}…"):
        data = loader(**params)

    st.subheader(f"Results for {params['symbol']}")
    tab_decision, tab_overview, tab_tech, tab_sent, tab_cong, tab_rep = st.tabs(
        ["⚖️ Decision", "📊 Overview", "📈 Technical", "📰 Sentiment", "🏛 Congress", "🎯 Reports"]
    )
    with tab_decision:
        _safe_panel("Decision", data, lambda d: _render_decision(params["symbol"], d))
    with tab_overview:
        _render_overview(data)
    with tab_tech:
        _render_technical(data)
    with tab_sent:
        _render_sentiment(data)
    with tab_cong:
        _render_congress(data)
    with tab_rep:
        _render_reports(data)

    st.divider()
    st.caption(DISCLAIMER)


# Only run the app when executing under `streamlit run`, so a plain import
# (e.g. the recursive import smoke test) does not build widgets or fetch data.
if st is not None and under_streamlit():  # pragma: no cover - requires streamlit runtime
    render()
