# MakeCrazyPenny — Research basis for the edge features

Evidence behind the alpha / risk / backtesting / regime features (see `plan.md` §10,
`CONTRACT.md` §10.7). Produced by a deep-research pass (fan-out web search → source
fetch → 3-vote adversarial verification). Each claim below was extracted from a primary
source *with a supporting quote* and then stress-tested by skeptic voters; we tag each
**[verified]** (survived adversarial review), **[contested]** (voters split / refuted),
or **[unresolved]** (verification incomplete). Treat *contested* as "directionally
useful but do not over-rely."

> Not investment advice. Replicated edges tilt odds; they do not guarantee returns.

---

## 0. The replication reality (why we built few features, not many) — [verified]

- **Most published factors do not survive scrutiny.** Hou, Xue & Zhang, *Replicating
  Anomalies*: of 447 anomalies, **286 (64%) are insignificant** at 5% once microcaps are
  handled (NYSE breakpoints, value-weighting); raising the bar to t>3 pushes failures to
  **380 (85%)**. Liquidity/trading-friction anomalies are worst: **95/102 (93%) fail.**
- **The factor zoo is a multiple-testing problem.** Harvey, Liu & Zhu catalogue **316
  factors** across 313+ works and argue a *newly* discovered factor must clear **t > 3.0**,
  not 2.0 — this is exactly the logic behind the **Deflated Sharpe Ratio** we use.
- **Costs and decay erode the rest.** McLean & Pontiff: post-publication returns are
  **~58% lower** (26% lower even pre-publication, out-of-sample). A Federal Reserve study
  (citing Chen & Velikov) notes **effective bid-ask spreads wipe out most post-publication
  returns** for a large set of anomalies.

**Implication, implemented:** prefer a few replicated signals; always backtest **net of
costs**; judge edges by **PSR/DSR > 0.95**, not raw Sharpe; expect alpha to decay.

---

## 1. Alpha signals & factors

- **Gross profitability ≈ value in power** — [verified]. Novy-Marx (JFE 2013): gross
  profits / total assets "has roughly the same power as book-to-market predicting the
  cross-section"; long-short ≈ **0.31%/mo (t=2.49)**, **0.52%/mo FF3 alpha (t=4.49)**.
  → implemented as the `gross_profitability` quality factor.
- **Combine value + quality (they're negatively correlated)** — [verified]. Novy-Marx:
  profitability and value returns correlate **−0.57**; a 50/50 mix reaches **Sharpe 0.85
  (t=5.87)** vs 0.34 for the market. → we score `value` and `quality` as separate factors
  so a composite forms when both agree.
- **52-week-high momentum** — [verified construction]. George & Hwang (2004): rank on
  `price / 52-week-high`, long the top 30% / short the bottom 30%. Needs only daily prices.
  → implemented as the `pct_52w_high` factor. (Note: the stronger claim that 52w-high
  *subsumes* JT/industry momentum was **[contested]** by the voters — we treat 52w-high as
  *complementary* to 12-1 momentum, not a replacement.)
- **Momentum (12-1)** — [verified] (Jegadeesh-Titman; corroborated across the sources).
  → the `momentum_12_1` factor (skips the most recent month to avoid short-term reversal).

## 2. Risk & position sizing

- **Volatility targeting reduces tail risk / drawdowns** — [verified for risk control].
  Across 60+ assets it "consistently reduces the likelihood of extreme returns … and
  reduces maximum drawdowns," because left-tail events cluster in high-vol periods when a
  vol-target holds small exposure.
- **…but its Sharpe benefit is contested** — [contested]. Moreira-Muir report Sharpe gains;
  however Cederburg-O'Doherty-Wang-Yan (Lehigh "COWY") find that across **103 equity
  strategies, vol-managed versions win only 53 vs 50**, with **only 8 significant**, mostly
  momentum. **Our stance:** we use vol-targeting for **risk control / sizing** (robust), not
  as a promised alpha booster.
- **Fractional (½) Kelly, never full** — [verified rationale]. Full Kelly is hyper-sensitive
  to estimation error and produces brutal drawdowns; fractional Kelly trades a little growth
  for much lower variance. → `kelly_fraction_from_conviction(..., fraction=0.5)`.

## 3. Backtesting & calibration

- **Raise the significance bar** — [verified]: t>3, not 2.0 (Harvey-Liu-Zhu). → Deflated &
  Probabilistic Sharpe (Bailey & López de Prado) in `analysis/backtest.py`.
- **Model transaction costs** — [verified]: costs/​spreads erase most paper edges. → the
  backtest charges `cost_bps` on position changes; default 10 bps.
- **Avoid look-ahead / survivorship** — [verified]: only price/factor signals (which have
  free history) are backtested; analyst/congress/sentiment are excluded (no free
  point-in-time history). Walk-forward by construction.

## 4. Portfolio & market regime

- **Trend timing works out-of-sample** — [verified] (Faber, *A Quantitative Approach to
  Tactical Asset Allocation*; time-series momentum literature): price vs the 10-month/200-day
  SMA delivers equity-like returns with materially smaller drawdowns. → `analysis/regime.py`
  (SPY 200-DMA + 12-1 TS-momentum + vol overlay → gross-exposure scalar).
- **Composite/diversified construction** beats single-name concentration → conviction ×
  inverse-volatility weights with caps in `orchestration/portfolio.py`.

---

## Caveats this research forces us to keep

1. **Alpha decays** (McLean-Pontiff ~58% post-publication) — these signals are public; size humbly.
2. **Cross-sectional > absolute** — value/quality are strongest ranked within a peer set; our
   single-name absolute thresholds are a simplification.
3. **Vol-targeting ≠ free Sharpe** (COWY) — we use it for drawdown control.
4. **The replication debate is unsettled** — Hou-Xue-Zhang ("most fail") vs open-source
   replication camps ("highly replicable") disagree; we side with the conservative view.

## Primary sources

- Hou, Xue & Zhang — *Replicating Anomalies* — https://www.researchgate.net/publication/345507035_Replicating_Anomalies
- Harvey, Liu & Zhu — *… and the Cross-Section of Expected Returns* — https://www.nber.org/system/files/working_papers/w20592/w20592.pdf
- Novy-Marx — *The Other Side of Value: The Gross Profitability Premium* (JFE 2013) — https://oldschoolvalue-files.s3.amazonaws.com/pdf/Novy-Marx_Gross-Profitability-Anomaly_JFE_2013.pdf
- George & Hwang — *The 52-Week High and Momentum Investing* (JF 2004) — https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf
- McLean & Pontiff — *Does Academic Research Destroy Stock Return Predictability?* (JF 2016) — https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365
- Chen & Velikov via FEDS 2021-037 (post-publication, net of costs) — https://www.federalreserve.gov/econres/feds/files/2021-037pap.pdf
- Moreira & Muir — *Volatility-Managed Portfolios* (NBER w22208) — https://www.nber.org/papers/w22208
- Cederburg, O'Doherty, Wang & Yan — *On the performance of volatility-managed portfolios* — https://www.lehigh.edu/~xuy219/research/COWY.pdf
- Harvey/Liu — volatility targeting & risk control (SSRN 3175538) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3175538
- Faber — *A Quantitative Approach to Tactical Asset Allocation* — https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf
- Bailey & López de Prado — *The Deflated Sharpe Ratio* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Moskowitz, Ooi & Pedersen — *Time Series Momentum* — http://docs.lhpedersen.com/TimeSeriesMomentum.pdf
