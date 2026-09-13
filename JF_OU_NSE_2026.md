# JF-OU / NSE 2026 — Production Algorithm
## Jump-Filtered Ornstein–Uhlenbeck Trend-Conditioned Mean Reversion
### Recalibrated for Indian equity market structure and the 2026 regime

**Venue:** NSE India. **Segments:** Stock Futures (primary) and Cash Delivery (fallback).
**Horizon:** 1–8 trading sessions. **Direction:** long only. **Clock:** IST throughout.

This is a complete, standalone specification. Every parameter is defined once in the Master Table (Part XVIII) and referenced by name elsewhere. Where a generic parameter was wrong for India 2026, the correction and its evidence are recorded in Part XX.

---

# PART 0 — The 2026 Indian regime, and why parameters moved

Recycling US-calibrated constants into NSE 2026 breaks the model in four specific ways. Each is measured below.

## 0.1 Market state

| Fact (2026) | Value | Consequence for this algo |
|---|---|---|
| Nifty 50 level, 11 Sep 2026 | ~23,262 (3-month low) | Index-trend gate off for long stretches |
| Nifty 50 YTD price return through 9 Sep 2026 | ≈ **−13.7%** | A bear-ish year: fewer valid long setups by design |
| Brent crude, Sep 2026 | > $108/bbl, ~+12% in a week | Oil-import sectors structurally impaired |
| USDINR, Sep 2026 | ~95.79, RBI intervening | Exporters vs importers diverge hard |
| India 10Y G-sec, Sep 2026 | > 7.0%, 3-month high | Rate-sensitive sectors under pressure |
| US 10Y, Sep 2026 | near 5.0% | Global risk-off backdrop |
| FPI flows, 28 Feb – 20 Mar 2026 | ≈ **−$9.6 bn**, net sellers every session | Sustained supply into rallies |
| India VIX, Jul–Sep 2026 | **10.6 – 12.4** | Calm-vol regime |
| India VIX, Feb–Mar 2026 (US–Iran shock) | 13.7 → **27.17** intraday peak (23 Mar) | Sharp, discrete vol regime change |
| India VIX 52-week range | **8.72 – 28.91** | The old "VIX < 21" gate is mis-calibrated |
| March 2026 path | Nifty major decline, then Apr/Jun/Jul recovery, Aug–Sep weakness | Regime switches ~4× in 9 months |

**Implication.** 2026 is not a steady uptrend. It is a choppy, headline-driven year with a violent February–March volatility shock, oil and currency stress from August, and persistent FPI supply. A long-only mean-reversion system must therefore be **gated hard by regime** and must expect long idle periods. That is a feature: the gate is what keeps you out of March 2026.

## 0.2 India VIX does not behave like the CBOE VIX

The generic spec used `VIX < 21.0`. Against actual 2026 India VIX closes this is close to useless:

| Threshold | Passes Jul–Sep calm days | Passes Feb–Mar shock days |
|---|---|---|
| VIX < 13 | **100%** | **0%** |
| VIX < 16 | 100% | 6% |
| VIX < 21 (old rule) | 100% | **39%** |

A flat 21 let entries through on 39% of the war-shock sessions while never distinguishing calm from stressed. India VIX structurally runs ~10–13 in calm and 21–27 in crisis. **Replaced with a tiered 13 / 16 / 21 gate (Part IV).**

## 0.3 SEBI's ₹15 lakh minimum contract value changed position sizing

From 20 Nov 2024 SEBI requires every new derivative contract to carry a notional value of **₹15–20 lakh**. Lot sizes were rebased accordingly and are re-cut periodically:

| Index | Lot size (Jan 2026 series) |
|---|---|
| Nifty 50 | **65** (was 75) |
| Nifty Bank | **30** (was 35) |
| Nifty Fin Services | **60** (was 65) |
| Nifty Midcap Select | **120** (was 140) |

Bank Nifty **weekly expiries were discontinued**; Nifty weekly expiry moved to **Tuesday**; Sensex expires Thursday.

This single rule breaks the conventional Kelly sizing engine, because the smallest legal position is now ₹15 lakh of notional and the scale-out rule needs **two** lots. Verified consequences are in Part XI.

## 0.4 Costs changed on 1 April 2026

Budget 2026 raised F&O STT sharply. Equity delivery was untouched.

| Segment | Side | Rate from 1 Apr 2026 | Before |
|---|---|---|---|
| Equity delivery | buy **and** sell | 0.10% | unchanged |
| Equity intraday | sell | 0.025% | unchanged |
| **Futures** | sell | **0.05%** | 0.02% (**+150%**) |
| **Options premium** | sell | **0.15%** | 0.10% |
| Options exercise | intrinsic | 0.15% | 0.125% |

Exact round-trip cost is computed in Part XV and feeds the payoff ratio `b`.

## 0.5 Algo trading is now regulated

SEBI's *Safer Participation of Retail Investors in Algorithmic Trading* framework (circular 4 Feb 2025) became **mandatory for all brokers from 1 April 2026**:

- Every automated order carries an exchange-assigned **Algo-ID**.
- **Static IP whitelisting** is mandatory for API access; orders from other IPs are rejected. Hosting must be on **Indian servers**.
- **10 orders/second per exchange per client** is the registration threshold. This strategy places ≤5 orders/day, far below it — no separate strategy registration needed; the broker handles tagging.
- Brokers are **principals** and carry accountability. OAuth + 2FA are required.

---

# PART I — What the system does

Buy a name that is in a statistically proven uptrend and has made a short, statistically anomalous dip which is:

1. continuous liquidity imbalance, not a news jump;
2. fast enough to mean-revert in days;
3. occurring on drying volume;
4. landing on a level where institutions actually bought;
5. free of scheduled binary events inside the holding window;
6. in a market regime that permits long exposure at all.

Enter only on **confirmation** (reclaim of the prior day's high). Size on a **fat-tail-corrected half-Kelly risk budget**. Exit on a **deterministic ladder**.

Nothing here predicts a rise. The edge is the asymmetry of the exits plus the rejection rate, not the hit rate.

---

# PART II — Universe construction

## 2.1 Inclusion

- **Segment:** NSE F&O-eligible equities only (primary). No hard price circuits, only dynamic cooling bands — so a stop-loss can actually execute.
- **Liquidity floor:** 20-day average daily traded value ≥ **₹20 crore** at the strict 1.5% participation cap. See Part XI.5 — this is a *derived* floor, not a preference. Relaxing participation to 5% lowers it to ₹6 crore.
- **Point-in-time membership:** on backtest date *t*, the universe is exactly the index membership on date *t*. NSE publishes only the current snapshot, so this must be reconstructed from index press releases / archived monthly reports. Testing 2018–2026 on today's Nifty 500 inflates win rate by roughly 4–8% annualized.
- **Ban-period screen:** exclude any name in the F&O **ban period** (MWPL ≥ 95%). Liquidity evaporates and fills become punitive. SEBI's Oct 2025 rules also cap an individual at 10% of MWPL in a single stock.
- **Circuit screen:** if the cash segment is used as fallback, exclude any name that hit a price circuit filter in the last **30 sessions**. A market stop in a locked lower circuit is unexecutable; losses can exceed 1.0R by multiples.

## 2.2 Data integrity (non-negotiable)

1. **Anchored VWAP uses adjusted price × adjusted volume** from one consistent series. Mixing adjusted price with raw volume corrupts `AVWAP = ΣP·V/ΣV` and produces false rejections after bonus/split events — common in Indian midcaps.
2. **ADV and volume ratios use raw (unadjusted) volume.** Never feed adjusted volume into liquidity math.
3. **Verify the vendor's adjustment behaviour empirically.** Most Indian feeds adjust price for splits and are silent on dividends. Pull a symbol across a known bonus ex-date and check whether volume scales with the ratio. If it does, rebuild the raw series from corporate-action records.
4. Drop halted and zero-volume bars *before* computing window statistics, then backfill so every window stays full length.

---

# PART III — Data and pipeline

## 3.1 Series required

| Series | Granularity | Used by |
|---|---|---|
| Adjusted OHLCV | daily, 250+ sessions | trend, z, OU, ATR, AVWAP |
| Raw OHLCV | daily | ADV, V_ratio, lot math |
| 5-minute returns | 75 bars/session | jump test |
| Seasonal factors `s_m` | per 5-min slot, rolling 60 sessions | de-seasonalization |
| India VIX | daily + intraday | macro gate |
| Nifty 50, Nifty 500 | daily | macro gate, breadth |
| Brent crude, USDINR, India 10Y | daily | 2026 factor overlay |
| FPI/DII provisional flows | daily | regime confirmation |
| Sector / sub-industry tag | point-in-time | concentration cap |
| Earnings & board-meeting calendar | event dates | binary-event gate |
| Event calendar (Budget, MPC, FOMC, CPI, expiry) | dates | blackout windows |
| Circuit-hit log, MWPL/ban list | daily | tradability screens |
| PIT index constituents | by date | survivorship-free backtest |
| F&O lot sizes | current contract file | quantization |
| Unencumbered settled cash | live | sizing base |

## 3.2 Session clock (IST)

| Time | Job |
|---|---|
| 09:00–09:08 | NSE pre-open. Do not compute signals. |
| **09:08** | Ingest pre-open discovery price → **Gap-Cancel Rule** on every armed order |
| **09:15:01** | Place surviving **GFD** stop-limit orders (Algo-ID tagged) |
| 09:15–15:30 | 75 five-minute bars. Manage open positions on the ladder. |
| 15:20 | **Hard-cancel** all unfilled orders. Signal expires. |
| 15:40–16:00 | Closing Auction Session. Do not place new orders. |
| **15:35 batch** | Run the full scan on the **final** daily bar + complete 5-min arrays. Rank. Write tomorrow's order list. |

**Why 15:35 and not 15:20:** at 15:20 the day-*t* bar is incomplete and the last 5-minute bar has not printed, so the jump test runs on a phantom session. **Why not trade at 15:35:** you would place orders next morning regardless — which is what the 09:15:01 step does, but with the gap check inserted first.

## 3.3 Sourcing

Upstox (or an equivalent broker API) covers daily and 5-minute candles, lot sizes, funds with a cash/pledge/unsettled split, sector classification, corporate actions, orders and GTT. Three inputs must come from outside any broker API: **earnings calendar**, **circuit-hit history**, **point-in-time constituents**. All three are available from NSE-side sources.

Rate limits to design around: standard APIs 50/sec, 500/min, **2000/30 min**; order APIs 10/sec. A 500-name daily hot path is ~1,000 requests and fits one window. The 2000/30-min ceiling, not per-second throughput, is the binding constraint — cache daily candles and corporate actions, and run weekly refreshes in a separate window.

---

# PART IV — G0: Macro regime gate (2026-calibrated)

Individual alpha collapses when stock-to-index correlation approaches 1.0. This gate is evaluated once per session before any name is scanned.

## 4.1 India VIX — tiered, not flat

| Tier | Condition | Action |
|---|---|---|
| **Green** | India VIX < **13.0** | Full operation. Up to 5 concurrent names. |
| **Amber** | 13.0 ≤ VIX < **16.0** | Max **2** concurrent names. Tighten z-band to −2.00 … −1.50. Halve the risk budget. |
| **Red** | VIX ≥ **16.0** | **No new entries.** Existing positions run their own ladder. |
| **Crisis** | VIX ≥ **21.0** | No new entries; tighten every open stop to `Entry − 0.75R`. |

Plus a **percentile overlay**: independently block new entries if VIX is in the **top quartile of its trailing 250 sessions**, regardless of absolute level. This adapts if the calm-vol centre drifts.

Plus an **expansion rule**: block if VIX is up more than **10%** intraday. India VIX moved +25% in a single session on 2 Mar 2026 and +23% on 4 Mar, so this catches shock onset a day before the level threshold does.

## 4.2 Index trend

- Nifty 50 close > **EMA₅₀(Nifty 50)** — hard requirement for new entries.
- Breadth confirmation: **≥ 45%** of Nifty 500 constituents above their own 200-DMA. In a −13.7% YTD year this is the gate that prevents buying bear-market rallies that look like individual uptrends.

## 4.3 2026 macro-factor overlay

Applied to the *sector* dimension, not the signal. In 2026 the cross-section is being driven by oil, the rupee and rates more than by idiosyncratic news.

| Condition | Exclude | Rationale |
|---|---|---|
| Brent > $95 | OMCs, paints, aviation, tyres, specialty chemicals | Input-cost compression |
| USDINR up > 1% over 20 sessions | Import-heavy consumer, NBFCs | Margin and funding stress |
| India 10Y > 7.0% | Banks, NBFCs, realty, auto, capital goods | NIM compression, demand pull-forward |
| FPI net seller 10 consecutive sessions | All — cut max concurrent names to 2 | Supply overwhelms mean reversion |

These are **overlays**, not gates: they reduce the candidate pool and the position count, they do not veto an otherwise perfect statistical setup in a sector that is merely unfashionable.

---

# PART V — G1: Tradability and event gates

- **G1.1** Liquidity floor (Part 2.1) and price above penny levels.
- **G1.2** Not in F&O ban period (MWPL ≥ 95%).
- **G1.3** No price-circuit hit in the last 30 sessions (cash-segment fallback).
- **G1.4 Earnings blackout.** Next scheduled result or board meeting must be **more than 10 trading days away**, OR the result must have been published **within the prior 3 sessions**. The intraday jump test is blind to *future* announcements; a 10% overnight earnings gap bypasses the stop entirely.
  Indian results are filed within 45 days of quarter end, so the heavy blackout windows are **mid-Apr→mid-May (Q4), mid-Jul→mid-Aug (Q1), mid-Oct→mid-Nov (Q2), mid-Jan→mid-Feb (Q3)**. As of September 2026, the Q2 FY27 window opens in roughly four weeks — expect the pool to shrink sharply.
- **G1.5 Event blackout.** No new entries on: **Union Budget day (1 Feb)**, **RBI MPC decision days**, **US FOMC days**, **Nifty weekly expiry (Tuesday)** after 13:00, **monthly expiry (last Tuesday)** after 13:00, and India CPI release day. Expiry afternoons inject index-level noise that contaminates stock-level stop placement.
- **G1.6** Point-in-time index membership.

---

# PART VI — G2: Trend persistence

Three independent conditions, all required.

## 6.1 Hurst exponent

Rolling **100-session** Rescaled Range (R/S) on daily log prices. Require **H > 0.60**. Below that the series is not structurally persistent and "trend" is noise. In a 2026-style choppy year this is the first filter to fail and it should.

## 6.2 Kalman velocity

```
state:   x_t = [price_t, velocity_t]ᵀ
predict: x_t = F·x_{t−1} + w_t ,   F = [[1,1],[0,1]]
observe: y_t = H·x_t + v_t ,       H = [1,0]
noise:   w_t ~ N(0,Q) ,  v_t ~ N(0,R)
```

- Require **velocity_t > 0**.
- Require the slope positive at the **95% confidence bound**, using the filter covariance for the velocity standard error.
- **Do not hardcode Q/R.** Estimate both by **EM on rolling 60-session blocks**, or set `Q = κ·σ²_t` from the GARCH conditional variance. Too-high Q/R makes velocity oscillate on noise; too-low turns the filter into a lagging moving average that misses turns by 10+ sessions. In 2026's regime-switching tape, a static Q/R is guaranteed to be wrong in at least two of the four regimes.

## 6.3 Drift confirmation

- OLS of `ln(Close)` on time over **60 sessions**: slope t-statistic **t_β ≥ 2.5** (≈99% confidence).
- **Close > EMA₅₀.**

G2.2 and G2.3 are complements: plain 60-day OLS is endpoint-sensitive and lags momentum shifts; the Kalman velocity tracks slope without that lag.

---

# PART VII — G3: Jump classification

The dip must be continuous diffusion, not a Poisson jump. A jump-driven dip is the start of a markdown.

## 7.1 De-seasonalize first

NSE variance is sharply U-shaped. The 09:15–09:45 and 15:00–15:30 windows routinely carry 300–500% more variance than the 12:00–14:00 lull. Raw BNS on unadjusted 5-minute returns reads ordinary opening prints as jumps and falsely aborts more than half of clean pullbacks.

```
r*_m = r_m / s_m ,   m ∈ [1,75]
s_m  = rolling 60-session average |return| for slot m
```

## 7.2 Bipower-variation statistic

```
RV_t   = Σ (r*_m)²
BV_t   = (π/2) · Σ |r*_m|·|r*_{m−1}|
Z_jump = √n · (RV_t − BV_t) / RV_t
```

Require **Z_jump < 2.15** (p > 0.05). At or above → **abort**.

## 7.3 Overnight gap check

More than 70% of shocks land overnight, and Indian markets are exposed to the US close, GIFT Nifty and Middle-East headlines between sessions. A stock that gaps down 6% and then trades quietly returns `Z_jump ≈ 0` and fools the intraday test.

```
|ln(Open_t) − ln(Close_{t−1})| ≤ 1.5 · σ_daily
```

## 7.4 GIFT Nifty divergence (India-specific)

If GIFT Nifty implied a gap worse than −1.0% but the cash open came in flat or positive, the dip is being absorbed by a specific buyer — that is a *positive* signal, not a jump. Log it; do not reject on it. Conversely, a cash open that merely matches a bad GIFT Nifty print means the shock is genuine and unabsorbed.

---

# PART VIII — G4: Stationarity

The half-life formula is valid only if the spread is I(0). In a real breakdown `x_t = ln(P_t) − ln(EMA₂₀)` behaves like a random walk with drift, and AR(1) on an integrated series returns a **spurious** mean-reversion coefficient.

- Run an **Augmented Dickey–Fuller test on `x_t`**.
- Require **ADF p < 0.05**. Non-stationary → **reject**, and do not compute a half-life at all.

---

# PART IX — Scored confluence (need ≥ 3 of 4)

Eight stacked hard `AND` gates would produce fewer than 1–2 trades per quarter across 500 names. In a −13.7% YTD year it would produce near zero. Mandatory gates stay binary; the four *quality* signals are scored. **Require ≥ 3 of 4.** A 4/4 earns ranker priority.

## S1 — Pullback magnitude (GARCH studentized z)

Do not use a raw rolling standard deviation: it assumes homoskedasticity and misreads regime shifts as pullbacks. Fit **GARCH(1,1)** on daily returns (window 60) for conditional variance.

```
Z_GARCH = (P_t − μ_t) / σ_t      μ_t = Kalman state price (fallback EMA₂₀)
```

**Point if −2.20 ≤ Z_GARCH ≤ −1.40** (Amber regime: −2.00 … −1.50).

Above the upper bound: not discounted enough, no edge. Below the lower bound: extreme outlier — in 2026 that usually means an FPI-driven block dump or an oil-shock re-rating, with high continuation risk.

## S2 — Reversion speed (OU half-life)

**Fit on historical pullback episodes, not a rolling calendar window.** Fitting AR(1) on the last 30 sessions when the stock trended for 26 and dipped for 4 means ~85% of the sample is the trending regime; the resulting θ describes volatility *around* the trend, not recovery *from a deficit*.

Over the past **250 sessions**, isolate pullback episodes (consecutive down/flat sessions following a local high inside an uptrend leg), stack their `x_t` observations, and fit:

```
Δx_t = λ·x_{t−1} + ε_t
φ = 1 + λ ,  θ = −ln(1+λ) ,  τ½ = ln(2)/θ = −ln(2)/ln(1+λ)
```

**Point if** λ < 0 with **p < 0.05** and **1.0 ≤ τ½ ≤ 3.5 sessions**.

*Verified:* λ = −0.20 → τ½ = 3.11 d ✓ | λ = −0.30 → 1.94 d ✓ | λ = −0.50 → 1.00 d ✓ | λ = −0.10 → 6.58 d ✗

If S2 misses the band but G4 passed, the trade may still enter on three other points and the time stop falls back to the **Day-8 default** because no valid τ exists.

## S3 — Volume exhaustion

- **V_ratio < 0.60** — volume and candle-body size over the last 2–4 down sessions versus a recent average.
- **Volume_t < SMA₂₀(Volume)** — total participation is drying, not just red candles shrinking.

Both required. Heavy volume on down days in 2026 is distribution by FPIs, not retail capitulation.

## S4 — Anchored VWAP confluence

Deterministic anchor — no discretionary "eyeball the swing low":

1. **Primary:** search `i ∈ [t−40, t−5]` for the most recent bar that broke a **20-day Donchian high** with volume expansion (`Volume_i > 1.5 × SMA₂₀(Volume)_i`). Set `t₀ = i`.
2. **Fallback:** `t₀ = argmin(Low_i)` over `i ∈ [t−30, t−5]`, confirmed as a pivot (no lower low within ±3 bars either side).
3. **Staleness:** if `|Close_t − AVWAP_{t₀,t}| / AVWAP_{t₀,t} > 0.04`, the anchor is stale → **discard the candidate**.

```
AVWAP_{t₀,t} = Σ_{i=t₀}^{t} P_i·V_i / Σ_{i=t₀}^{t} V_i      (adjusted series)
```

**Point if** the current low tests the band — `Low_t ≤ AVWAP × (1 + tol)`, `tol ≈ 0.25 × ATR₁₄` as a percentage — **and** the close holds above it.

---

# PART X — Cross-sectional ranker

On an index rebound day 15–25 names can clear every gate at once. Take only the top **3–5** (max 2 in Amber).

```
Score = ⅓·z(H) + ⅓·z(−τ½) + ⅓·z(−Z_GARCH) + 1{4/4 confluence}
```

- `z(H)` — steepest structural trend
- `z(−τ½)` — fastest expected recovery
- `z(−Z_GARCH)` — deepest discount

`z(·)` is a cross-sectional z-score across the day's candidate pool. Ties break on higher ADV (better fill, and it keeps you away from the ₹20 crore floor).

---

# PART XI — Position sizing for NSE 2026

This is where the generic spec breaks hardest, because of the ₹15 lakh minimum contract value.

## 11.1 Payoff ratio is dynamic, not a constant

The ladder scales 50% at Target 1 (~1.0R) and 50% at Target 2 (2.5R):

```
b_gross = 0.5(1.0R) + 0.5(2.5R) = 1.75R
b_net   = 1.75R − friction_in_R
```

Friction depends on R, so `b` must be recomputed per trade. Exact 2026 costs are in Part XV.

| R as % of price | Round-trip friction | Friction in R | **b_net** |
|---|---|---|---|
| 1.50% | 0.524% | 0.350R | **1.400** |
| 2.00% | 0.524% | 0.262R | **1.488** |
| 2.50% | 0.524% | 0.210R | **1.540** |
| 3.00% | 0.524% | 0.175R | **1.575** |

*(0.524% = 0.224% measured cash-delivery charges + 0.30% assumed slippage. On stock futures the charge component drops to ~0.058%, raising b_net by roughly 0.08R.)*

**Using a fixed b = 2.5 or even 1.60 over-levers the book.** At the typical R ≈ 2%, the correct value is **1.488**.

## 11.2 Probability — fat-tail corrected, higher floor

The standard normal CDF understates how long extreme dips take to revert. Use the **empirical CDF** of the stock's residuals, or a **Student's t CDF with ν = 4 or 5**, fitted per symbol:

```
p = F_{t,ν}( expected reversion move / σ_t )
```

Kelly turns negative below `p = 1/(1+b)`. With b = 1.488 that is **p = 0.4020** — meaning the old `p > 0.40` floor sat *on* the break-even and would have authorised near-zero-edge trades.

**Hard floor: p > 0.43.** Below that → size zero, abort. Log as `REJ_LOW_PROB`.

## 11.3 Kelly applied to margin, not to notional

This is the central 2026 correction. Two readings of "half-Kelly" give wildly different answers under a ₹15 lakh minimum contract.

**Reading A — Kelly as a fraction of equity in notional (the generic reading).** Verified requirement for the smallest legal 2-lot position (₹30 lakh notional):

Computed at b = 1.488 exactly:

| p | half-Kelly f | Equity needed for 2 lots |
|---|---|---|
| 0.45 | 0.0402 | **₹7.47 Cr** |
| 0.50 | 0.0820 | ₹3.66 Cr |
| 0.55 | 0.1238 | ₹2.42 Cr |
| 0.60 | 0.1656 | ₹1.81 Cr |

Under this reading the strategy is inaccessible below roughly ₹2 crore. It is also wrong: in F&O you do not commit notional, you commit **margin**.

**Reading B — Kelly as a fraction of equity in margin, notional grossed up by 1/margin_rate.** With SEBI peak-margin rules, stock futures require ~13–18% of notional. Taking 15%:

```
margin_target   = f_halfkelly × Equity          (capital Kelly commits)
notional_target = margin_target / margin_rate   (grossed up by leverage)
```

Equity ₹50 lakh, p = 0.55, f = 0.1238 → notional **₹41.3 lakh** ≈ 2.75 lots → risk at R = 2% is **₹82,517 = 1.65%** of equity → **the 1.5% cap binds** → capped notional ₹37.5 lakh → final size **2 lots**.

Reading B is used throughout. Reading A is documented here only so the discrepancy is visible.

## 11.4 The four constraints, minimum wins

```
risk_budget = min( 0.015 × Total_Equity ,  f_halfkelly × Equity / margin_rate × (R/Entry) )
shares_risk = risk_budget / R
shares_adv  = participation_cap × ADV20_shares
shares      = min(shares_risk, shares_adv)
```

Then quantize (§11.6) and re-verify the margin requirement against unencumbered cash.

## 11.5 The ADV cap and the lot rule collide — the derived liquidity floor

The smallest legal position is 2 lots. For a participation cap to permit it:

```
ADV ≥ 2 × min_contract_value / participation_cap
```

| Min contract value | 2-lot notional | ADV needed @ 1.5% | ADV needed @ 5% |
|---|---|---|---|
| ₹15 L | ₹30 L | **₹20.0 Cr** | ₹6.0 Cr |
| ₹18 L | ₹35 L | ₹23.3 Cr | ₹7.0 Cr |
| ₹20 L | ₹40 L | ₹26.7 Cr | ₹8.0 Cr |

**This is a hard structural result, not a preference.** A stock with ₹15 crore ADV has a 1.5% cap of ₹22.5 lakh — *below* the ₹30 lakh minimum position. No compliant, legal trade exists. Hence the ₹20 crore ADV floor in Part 2.1.

1.5% is the conservative institutional default for an aggressive order. A GFD stop-limit that may never fill is passive; **5% is defensible** and reopens the midcap universe at a ₹6 crore floor. Choose explicitly and log which one is live.

## 11.6 Even-lot quantization

A market order to sell 50% of an odd lot count is **rejected by the exchange**.

```
N_lots = floor( shares / (2 × Lot_Size) ) × 2
if N_lots < 2:  REJECT_CAPACITY      (capital insufficient for a two-tranche scale-out)
```

## 11.7 Minimum viable capital — verified

| Equity | Risk budget (1.5%) | Notional cap @R=2% | Lots @₹15L | Margin @15% |
|---|---|---|---|---|
| ₹20 L | ₹0.30 L | ₹15.0 L | **0** | — |
| ₹50 L | ₹0.75 L | ₹37.5 L | **2** | ₹4.50 L |
| ₹1 Cr | ₹1.50 L | ₹75.0 L | 4 | ₹9.00 L |
| ₹2.5 Cr | ₹3.75 L | ₹187.5 L | 12 | ₹27.00 L |
| ₹5 Cr | ₹7.50 L | ₹375.0 L | 24 | ₹54.00 L |

Minimum equity for the smallest legal F&O position: **₹30 L at R=1.5%, ₹40 L at R=2.0%, ₹50 L at R=2.5%.**

Note the consequence: **position count is capped by the risk rule, not by conviction.** A ₹50 lakh account can never exceed 2 lots at R=2% no matter how extreme the mispricing. Kelly modulates *whether* to trade and *how much of the 1.5%* to use — it cannot buy more lots.

## 11.8 Capital base — regulatory reality

Never size off nominal NAV. Feed the engine **unencumbered settled cash**:

- **F&O overnight** requires a minimum **50:50 cash-to-collateral** ratio. Pledged equity is not deployable into new futures.
- **Cash segment** proceeds are subject to **T+1 settlement** and do not recycle into same-day triggers.
- SEBI peak-margin rules require **100% upfront margin** for F&O. There is no leverage beyond SPAN+ELM.

```
C_deployable = unencumbered_settled_cash − margin_committed
```

---

# PART XII — Order construction and execution

## 12.1 Entry

```
Trigger = High_t × 1.0005              (prior day's high + 0.05%)
Limit   = Trigger × 1.0020             (0.20% slippage buffer)
Type    = Stop-Limit, Good-For-Day, Algo-ID tagged
```

**Two execution bugs this fixes.** Setting trigger = limit means a fast momentum spike gaps the ask over your limit and the order rests unfilled. And if day *t+1* opens well above yesterday's high, you fill high while the stop stays at the dip low, so `R` nearly doubles and the 2.5R target becomes unreachable. Hence:

```
GAP-CANCEL (evaluated 09:08 on pre-open discovery price):
  if Open_{t+1} > High_t × 1.012  OR  Open_{t+1} > High_t + 0.75 × ATR₁₄:
      cancel; signal void
```

In 2026's headline-driven tape, overnight gaps from GIFT Nifty and Middle-East news are routine. This rule will fire often, and that is correct.

## 12.2 Order lifetime

**Hard GFD.** If day *t+1* trades as an inside bar and never breaks yesterday's high, the order is **hard-cancelled at 15:20 on day t+1**. A trigger on day *t+2* is trading a decayed OU half-life — the edge has expired. Recompute from scratch.

## 12.3 Risk unit

```
Stop S = min( Low_dip − 0.75 × ATR₁₄ ,  AVWAP_{t₀} × 0.992 )
R      = Entry − S
Reject if R > 1.25 × ATR₁₄        (stop too far; entry too extended; b no longer supports)
```

**The buffer is 0.75 × ATR₁₄, not 0.25.** A 0.25 buffer is ~0.3–0.6% on Indian large/midcaps; liquidity sweeps that wick below the dip low before moving up will stop you out on day 1–2 more than 40% of the time even when the direction is right. Indian small/midcaps are wick-heavy in the first 30 minutes — the wider buffer is mandatory, not optional.

---

# PART XIII — Exit ladder

| # | Trigger | Action |
|---|---|---|
| 1 | Price ≤ S (1.0R hard stop) | Immediate market order, full exit |
| 2 | Price ≥ Kalman state mean **or** ≥ EMA₂₀ | **Target 1:** sell 50%; move remaining stop to breakeven (`Entry × 1.0005`) |
| 3 | Price ≥ Entry + 2.5R | **Target 2:** limit-sell the remainder |
| 4 | After Target 1, daily close < EMA₉ | Market order next open (rides persistent right-tail momentum) |
| 5 | Held ≥ `min(ceil(2.5 × τ½), 8)` sessions | **OU time stop**, market-on-close. If it has not reverted in 2.5 half-lives, the thesis failed |

*Verified time stops:* τ½ = 1.0 → 3 d | 2.0 → 5 d | 2.8 → 7 d | 3.5 → ceil(8.75)=9 → **capped at 8 d**. The `min(·,8)` reconciles the OU-scaled stop with a fixed Day-8 backstop; if S2 failed, use 8 directly.

**Rules 1, 2, 3 and 5 are what make `b` empirical rather than theoretical.** Without a deterministic stop, target and time stop, Kelly is sizing against a number that was never earned and losers become open-ended bag-holding.

**Do not mix ladders.** If you hold 100% to the 2.5R target with no scale-out, `b` becomes ~2.2 net and the even-lot requirement disappears — but then §11.1, §11.6 and this table must all change together.

---

# PART XIV — Portfolio constraints

- **P1 Sector:** maximum **1 concurrent position per sector/sub-industry**. Six IT midcaps can trigger identical signals during a Nifty-IT-only correction; taking four is an 80% single-industry bet that stops out concurrently on a sector downgrade. In 2026, with IT weak and oil-driven divergence, this matters more than usual.
- **P2 Sector exposure:** total ≤ **25%** of book.
- **P3 Heat:** verify `Σ(shares × R) ≤ 5 × 1.5% = 7.5%` of equity before placing. In Amber, ≤ 3.0%. If exceeded, drop the lowest-ranked name.
- **P4 Macro suspension:** a G0 failure blocks new entries but does **not** force-close open positions — they run their own ladder.
- **P5 Concurrency:** max 5 names in Green, 2 in Amber, 0 in Red.

---

# PART XV — 2026 Indian cost model (exact)

Rates effective 1 April 2026, discount broker, NSE.

## 15.1 Cash delivery round trip

| Component | Rate | On ₹10 L |
|---|---|---|
| STT | 0.10% buy + 0.10% sell | ₹2,000 |
| Exchange txn (NSE) | 0.00307% each side | ₹61 |
| SEBI turnover | ₹10/crore each side | ₹2 |
| Stamp duty | 0.015% buy only | ₹150 |
| GST | 18% on (brokerage + txn + SEBI) | ₹11 |
| DP charges | ~₹18 per scrip per day on sell | ₹18 |
| **Total** | | **₹2,243 = 0.2243%** |

Stable at ~0.223–0.226% across ₹5 L–₹30 L notionals.

## 15.2 Stock futures round trip

| Component | Rate | On ₹30 L |
|---|---|---|
| STT | **0.05%** sell only | ₹1,500 |
| Exchange txn | 0.00183% each side | ₹110 |
| SEBI turnover | ₹10/crore each side | ₹6 |
| Stamp duty | 0.002% buy only | ₹60 |
| Brokerage | ₹20 per side | ₹40 |
| GST | 18% on (brokerage + txn + SEBI) | ₹28 |
| **Total** | | **₹1,744 = 0.0581%** |

**Futures are ~3.8× cheaper than delivery.** That is a real argument for the F&O segment — offset entirely by the ₹15 lakh lot floor.

## 15.3 Slippage

Assume **0.30%** round trip for mid/large caps on a stop-limit with a 0.20% buffer. On thinner names raise it; on Nifty-50 constituents 0.15% is defensible.

Combined: `0.2243% + 0.30% = 0.524%` in cash, `0.058% + 0.30% = 0.358%` in F&O. These feed §11.1.

---

# PART XVI — Validation before capital

The framework has ~14 tunable parameters. An annualized Sharpe > 2.5 is **mathematically trivial to find by chance** at that parameter count.

1. **Combinatorial Purged Cross-Validation (CPCV)** with embargo windows so overlapping labels cannot leak across splits.
2. **Deflated Sharpe Ratio (López de Prado)**, correcting for trial count and for the non-normality and autocorrelation of returns.

**Accept only if DSR p ≤ 0.05.** Above that the result is parameter overfitting, not alpha.

**2026-specific requirement — regime-stratified reporting.** Report Sharpe, win rate and realized `b` separately for:

| Regime | 2026 example |
|---|---|
| Calm uptrend | Apr–Jul 2026 recovery windows |
| Shock | Feb–Mar 2026 (VIX 13.7 → 27.2) |
| Grinding decline | Aug–Sep 2026 (oil, INR, yields) |

A single blended number hides the fact that the system makes almost all its money in the first and is *designed* to be silent in the other two. Also require point-in-time universe, full 2026 costs from Part XV, and a comparison of **realized** `b` and `p` against assumed values after the first 60 live trades.

---

# PART XVII — Pseudocode

```python
# ---------------- 15:35 IST BATCH ----------------
def scan(date):
    g0 = macro_gate(date)                     # Part IV
    if g0.tier in ("RED","CRISIS"): return []
    max_names = 5 if g0.tier=="GREEN" else 2

    universe = pit_constituents(date)                     # 2.1
    universe = [s for s in universe
                if adv20_value(s) >= 20*CRORE             # derived floor 11.5
                and not in_fno_ban(s)                     # 2.1
                and not circuit_hit_within(s, 30)         # 2.1
                and earnings_clear(s, 10, prior_ok=3)     # G1.4
                and not event_blackout(date)]             # G1.5
    universe = apply_macro_overlay(universe, g0)          # 4.3

    cash = unencumbered_settled_cash()                    # 11.8
    out  = []
    for s in universe:
        d, r = adjusted_ohlcv(s, 250), raw_ohlcv(s, 60)

        # G2 trend
        if hurst(d.close, 100) <= 0.60:                      continue
        vel, ok = kalman_velocity(d.close, QR=em_60d_or_garch(d))
        if not (vel > 0 and ok):                             continue
        if tstat_slope(d.close, 60) < 2.5:                   continue
        if d.close[-1] <= ema(d.close, 50)[-1]:              continue

        # G3 jump
        r5 = deseasonalise(intraday_5min(s, dip_window))     # r_m / s_m
        if bns_z(r5) >= 2.15:                                continue
        if abs(log(d.open[-1]/d.close[-2])) > 1.5*sig_daily(d): continue

        # G4 stationarity
        x = log(d.close) - log(ema(d.close, 20))
        if adf_p(x) >= 0.05:                                 continue

        # S1-S4 confluence
        pts, detail = 0, {}
        zb = (-2.00,-1.50) if g0.tier=="AMBER" else (-2.20,-1.40)
        z  = z_garch(x, d);                 pts += zb[0] <= z <= zb[1]
        tau, lam_ok = ou_halflife_episodes(x, 250)
        pts += lam_ok and 1.0 <= tau <= 3.5
        pts += v_ratio(r) < 0.60 and r.vol[-1] < sma(r.vol,20)[-1]
        t0 = anchor_t0(d, r, win=(40,5))
        av = avwap(d, r, t0)
        if abs(d.close[-1]-av)/av > 0.04:                    continue   # stale anchor
        pts += d.low[-1] <= av*(1+band(d)) and d.close[-1] > av

        if pts < 3:                                          continue

        stop = min(dip_low(d) - 0.75*atr(d,14), av*0.992)
        out.append(Cand(s, tau, t0, av, pts, z,
                        trigger=d.high[-1]*1.0005, stop=stop))

    out = [c for c in out if (c.trigger-c.stop) <= 1.25*atr_of(c)]   # 12.3
    return apply_caps(rank(out)[:max_names], cash)                  # X, XIV


# ---------------- 09:08 IST ----------------
def gap_ok(c):
    o = preopen_discovery_price(c.sym)
    return not (o > c.prev_high*1.012 or o > c.prev_high + 0.75*c.atr)


# ---------------- 09:15:01 IST ----------------
def place(cands):
    for c in cands:
        if not gap_ok(c):                            log("CANCEL_GAP", c); continue
        R = c.trigger - c.stop
        b = 1.75 - (cost_pct(SEGMENT) + 0.0030)/(R/c.trigger)        # 11.1
        p = student_t_cdf(c.x, nu=4)
        if p <= 0.43:                                log("REJ_LOW_PROB", c); continue
        f = max(0.5*((p*(b+1)-1)/b), 0)                              # 11.2
        risk_budget = min(0.015*EQUITY, f*EQUITY/MARGIN_RATE*(R/c.trigger))
        sh = min(risk_budget/R, PARTIC*adv20_shares(c))              # 11.4
        lots = (sh // (2*c.lot))*2                                   # 11.6
        if lots < 2:                                 log("REJ_CAPACITY", c); continue
        if lots*c.lot*c.trigger*MARGIN_RATE > C_CASH:log("REJ_MARGIN", c);  continue
        submit(stop_limit(trigger=c.trigger, limit=c.trigger*1.0020,
                          qty=lots*c.lot, validity="GFD"))           # 12.1


# ---------------- 15:20 IST, day t+1 ----------------
cancel_all_unfilled()                                                # 12.2


# ---------------- while holding ----------------
def manage(pos):
    if pos.px <= pos.stop:                       exit_market()          # 1
    elif not pos.t1 and pos.px >= pos.t1:        sell(0.5); stop_to_be()# 2
    elif pos.t1 and close_ema9_break():          exit_next_open()       # 4
    elif pos.px >= pos.entry + 2.5*pos.R:        sell(remainder)        # 3
    if pos.days >= min(ceil(2.5*pos.tau), 8):    exit_moc()             # 5
```

---

# PART XVIII — Master parameter table

| # | Parameter | Value | Ref |
|---|---|---|---|
| 1 | Segment | NSE stock futures; cash delivery fallback | 2.1 |
| 2 | ADV floor (traded value) | ≥ ₹20 Cr @1.5% participation; ≥ ₹6 Cr @5% | 11.5 |
| 3 | F&O ban screen | MWPL < 95% | 2.1 |
| 4 | Circuit screen (cash) | no circuit hit in 30 sessions | 2.1 |
| 5 | India VIX tiers | Green <13 / Amber <16 / Red ≥16 / Crisis ≥21 | 4.1 |
| 6 | VIX percentile overlay | block if top quartile of 250 sessions | 4.1 |
| 7 | VIX intraday expansion | block if > +10% | 4.1 |
| 8 | Index trend | Nifty 50 > EMA₅₀ | 4.2 |
| 9 | Breadth | ≥45% of Nifty 500 above 200-DMA | 4.2 |
| 10 | Macro overlay | Brent, USDINR, 10Y, FPI streak | 4.3 |
| 11 | Earnings blackout | >10 sessions ahead, or within prior 3 | 5 G1.4 |
| 12 | Event blackout | Budget, MPC, FOMC, CPI, expiry pm | 5 G1.5 |
| 13 | Hurst window / cutoff | 100 sessions / H > 0.60 | 6.1 |
| 14 | Kalman Q,R | EM on 60-session blocks, or Q = κσ²_t | 6.2 |
| 15 | OLS lookback / t-stat | 60 sessions / t_β ≥ 2.5 | 6.3 |
| 16 | Velocity filter | Close > EMA₅₀ | 6.3 |
| 17 | De-seasonalization | rolling 60-session `s_m`, 75 bars | 7.1 |
| 18 | Jump threshold | Z_jump < 2.15 | 7.2 |
| 19 | Overnight gap | ≤ 1.5 σ_daily | 7.3 |
| 20 | ADF stationarity | p < 0.05 | 8 |
| 21 | GARCH window | 60 | 9 S1 |
| 22 | z-band | −2.20 … −1.40 (Amber: −2.00 … −1.50) | 9 S1 |
| 23 | OU sample | pullback episodes over 250 sessions | 9 S2 |
| 24 | Half-life band | 1.0 ≤ τ½ ≤ 3.5 sessions | 9 S2 |
| 25 | Volume exhaustion | V_ratio < 0.60 and Vol < SMA₂₀ | 9 S3 |
| 26 | AVWAP anchor window | [t−40,t−5] primary; [t−30,t−5] fallback | 9 S4 |
| 27 | AVWAP staleness | ≤ 0.04 | 9 S4 |
| 28 | Confluence | ≥ 3 of 4 | 9 |
| 29 | Ranker | top 3–5 (Green), 2 (Amber) | 10 |
| 30 | Payoff b | dynamic: 1.75 − friction/R (≈1.488 at R=2%) | 11.1 |
| 31 | Probability model | Student's t ν=4–5, or ECDF | 11.2 |
| 32 | Probability floor | **p > 0.43** | 11.2 |
| 33 | Kelly | 0.5 × full, applied to **margin** | 11.3 |
| 34 | Margin rate assumption | 15% (SPAN+ELM) | 11.3 |
| 35 | Risk cap | 1.5% of equity per trade | 11.4 |
| 36 | Participation cap | 1.5% of 20-day ADV (or 5%, declared) | 11.5 |
| 37 | Lot quantization | even multiples, ≥ 2 lots | 11.6 |
| 38 | Capital base | unencumbered settled cash | 11.8 |
| 39 | Entry trigger | High_t × 1.0005 | 12.1 |
| 40 | Limit buffer | +0.20% | 12.1 |
| 41 | Gap-cancel | > +1.2% or > +0.75 ATR₁₄ | 12.1 |
| 42 | Validity | GFD; cancel 15:20 day t+1 | 12.2 |
| 43 | Stop buffer | 0.75 × ATR₁₄ | 12.3 |
| 44 | Max risk distance | R ≤ 1.25 × ATR₁₄ | 12.3 |
| 45 | Target 1 | Kalman mean or EMA₂₀ → sell 50% | 13 |
| 46 | Target 2 | Entry + 2.5R → sell remainder | 13 |
| 47 | Time stop | min(ceil(2.5 τ½), 8) sessions | 13 |
| 48 | Sector cap | 1 position/sector; ≤25% book | 14 |
| 49 | Portfolio heat | ≤7.5% Green, ≤3.0% Amber | 14 |
| 50 | Validation | CPCV + DSR p ≤ 0.05, regime-stratified | 16 |

---

# PART XIX — Rejection taxonomy

| Code | Meaning |
|---|---|
| `REJ_MACRO_VIX` | VIX in Red/Crisis tier, top quartile, or +10% intraday |
| `REJ_MACRO_INDEX` | Nifty below EMA₅₀ or breadth < 45% |
| `REJ_UNIVERSE` | ADV floor, ban period, or circuit history |
| `REJ_EVENT` | earnings inside 10 sessions, or calendar blackout |
| `REJ_TREND` | H ≤ 0.60, velocity ≤ 0, t_β < 2.5, or below EMA₅₀ |
| `REJ_JUMP_INTRADAY` | Z_jump ≥ 2.15 |
| `REJ_JUMP_OVERNIGHT` | gap > 1.5 σ_daily |
| `REJ_NONSTATIONARY` | ADF p ≥ 0.05 |
| `REJ_STALE_ANCHOR` | anchor > 4% from current price |
| `REJ_CONFLUENCE` | fewer than 3 of 4 |
| `REJ_STOP_TOO_FAR` | R > 1.25 × ATR₁₄ |
| `REJ_LOW_PROB` | p ≤ 0.43 |
| `REJ_CAPACITY` | ADV cap or even-lot rule gave < 2 lots |
| `REJ_MARGIN` | margin requirement exceeds unencumbered cash |
| `REJ_SECTOR` | sector cap or 25% book exposure |
| `REJ_HEAT` | portfolio heat exceeded |
| `CANCEL_GAP` | gap-cancel at 09:08 |
| `CANCEL_GFD` | untriggered, cancelled 15:20 day t+1 |

Log every one. `REJ_CAPACITY` and `REJ_MARGIN` are **capital** rejections and must never be counted against signal quality — that distinction is the only way to tell whether the model is bad or the account is too small.

---

# PART XX — What changed from the generic spec, and why

| Item | Generic | **NSE 2026** | Evidence |
|---|---|---|---|
| VIX gate | flat < 21.0 | **tiered 13 / 16 / 21 + percentile** | VIX<21 passed 39% of Feb–Mar 2026 shock days; VIX<13 passed 100% of calm days and 0% of shock days |
| Payoff b | 2.5, then 1.60 | **dynamic, 1.488 at R=2%** | exact 2026 charges 0.2243% + 0.30% slippage = 0.262R |
| p floor | > 0.40 | **> 0.43** | Kelly break-even at b=1.488 is p=0.4020; 0.40 sat on it |
| Kelly basis | fraction of equity as notional | **fraction of equity as margin** | notional reading needs ₹1.8–7.7 Cr for the smallest legal position; margin reading gives 2 lots on ₹50 L |
| ADV floor | unstated | **≥ ₹20 Cr (derived)** | 2 lots = ₹30 L; at 1.5% cap this needs ADV ≥ ₹20 Cr or no legal trade exists |
| Min capital | unstated | **₹40 L at R=2%** | 1.5% risk cap vs ₹30 L minimum position |
| Macro gate | index + VIX only | **+ breadth, crude, INR, 10Y, FPI streak** | 2026 is driven by oil >$108, INR 95.79, 10Y >7%, −$9.6 bn FPI |
| Liquidity screen | ADV preference | **+ MWPL ban period** | SEBI Oct-2025 rules; ban-period liquidity evaporates |
| Event gate | earnings only | **+ Budget, MPC, FOMC, CPI, expiry pm** | Nifty weekly expiry moved to Tuesday; expiry afternoons are noisy |
| Regime reporting | single Sharpe | **stratified calm/shock/decline** | 2026 switched regime ~4× in 9 months |
| Segment | unspecified | **futures primary, cash fallback** | futures 3.8× cheaper; but ₹15 L lot floor excludes small accounts |

## Conflict resolutions carried forward

| Topic | Resolution |
|---|---|
| Stop buffer 0.25 vs 0.75 ATR | **0.75 × ATR₁₄**; 1.25 ATR is a cap on the risk *distance*, not the buffer |
| Time stop Day 8 vs 2.5τ | **min(ceil(2.5τ½), 8)** |
| OU fit: rolling 30d vs episodes | **episodes over 250 sessions** |
| z-band: rolling σ vs GARCH | **GARCH** |
| Half-life 1.5–5.0 vs 1.0–3.5 | **1.0–3.5** |
| AVWAP anchor | **Donchian breakout + volume; pivot-low fallback** |
| 8 hard ANDs vs scored | **G0–G4 binary; S1–S4 scored ≥3/4** |
| b = 2.5 vs 1.60 | **neither — recomputed per trade from Part XV** |

---

# PART XXI — Honest limitations

1. **This is a filter, not a forecast.** It isolates "trend pause, not trend death." The edge is in the exits and the rejection rate.
2. **2026 produces few signals by construction.** Nifty ≈ −13.7% YTD, FPIs net sellers, VIX spiking to 27 in March. The index and breadth gates will be off for long stretches. Expect weeks with zero trades and treat that as the system working, not failing.
3. **₹15 lakh lot rule is a hard constraint.** Below ~₹40 lakh equity, F&O is mathematically unavailable at a 1.5% risk budget, regardless of signal quality. Use the cash segment — accept the higher friction (b_net falls by ~0.08R) and the circuit risk.
4. **Jump detection has holes.** Overnight gaps are only partly covered by §7.3. Halts, illiquid names and multi-day circuit lock-downs defeat both tests.
5. **Short-window estimation error.** GARCH, Kalman Q/R and OU parameters are all estimated on limited data and can be badly wrong after a regime switch — and 2026 switched roughly four times.
6. **Student's t is still parametric.** ν = 4–5 is a convention. Refit per symbol and monitor drift.
7. **Half-Kelly still loses in clusters.** It scales *relative* size only. A sector downgrade hitting four correlated names is one portfolio event, not four independent losses.
8. **Nothing here is backtested.** Every number in this document is either an exact published rate, an arithmetic derivation shown in-line, or a reported 2026 market observation. The DSR/CPCV gates in Part XVI exist precisely because a 14-parameter system can trivially fake a Sharpe above 2.5.
9. **Execution assumptions need broker validation.** Fill at trigger + buffer, MOC availability, pre-open price retrieval at 09:08, and DP charge treatment must all be confirmed against your broker's actual behaviour before Part XV's `b` means anything.

**Deployment order:** paper-trade → smallest legal live size for 60+ trades → compare realized `b` and `p` against assumed → only then scale.

---

# PART XXII — Broker data sourcing: DhanHQ v2 vs Upstox v2/v3

Assessed against published documentation (accessed 2026-09-13). Both brokers cover the raw OHLCV this algorithm actually computes on; the differences are in **ancillary data** and **execution primitives**.

## 22.1 DhanHQ v2 — what it covers well

| §3.1 requirement | DhanHQ endpoint | Note |
|---|---|---|
| Daily OHLCV, 250+ sessions | `POST /charts/historical` | Back to **inception** of the scrip |
| 5-minute bars | `POST /charts/intraday`, `interval=5` | **Native** 1/5/15/25/60 min; **5 years** of history; **90 days per poll** |
| Seasonal factors `s_m` | same | one 90-day poll covers the 60-session window |
| Lot sizes | instrument/security list | per-contract |
| Unencumbered cash (§11.8) | `GET /fundlimit` | returns `availabelBalance`, `collateralAmount`, `receiveableAmount`, `withdrawableBalance` — enough to separate pledged collateral from settled cash and to see T+1 receivables |
| Margin per order (§11.3) | `POST /margincalculator` (+ `/multi`) | span, exposure, VAR, brokerage, leverage |
| Entry stop-limit (§12.1) | Orders API, `LIMIT` with `triggerPrice` | |
| Exit ladder (§13) | **Super Order** (bracket/cover with SL + target) and **Forever Order** (GTT/OCO) | a genuine advantage — T1/T2 can be expressed natively |
| Portfolio circuit breaker (§14 P3) | **P&L-based exit** + **kill switch** | maps directly onto the heat cap |
| Current circuit bands (§2.1) | market quote returns `upper_circuit_limit` / `lower_circuit_limit` | current bands only, **not** the 30-session hit history |
| Market impact calibration (§15.3) | **200-level depth** on websocket, 5,000 instruments/connection | Upstox offers 5 levels — this is the single best input for calibrating the 0.30% slippage assumption |
| Index data (G0.2) | `IDX_I` exchange segment | Nifty 50 / Nifty 500 |

## 22.2 The one gap that is worse than Upstox

**Dhan has no corporate-actions endpoint.** Dhan confirms in its own support documentation that daily historical data **is** adjusted for bonuses and splits. That is exactly the problem: §2.2 requires **two** series — adjusted price+volume for AVWAP, and **raw** volume for the ADV cap and `V_ratio`. Dhan hands you the adjusted series with no way to reconstruct the raw one.

Consequence if ignored: after a 1:1 bonus, adjusted volume doubles historically while true traded volume did not. The 1.5% ADV participation cap and the `V_ratio < 0.60` gate are then computed on a series that is wrong by the split ratio — silently, and only for affected names.

**Fix:** source corporate actions externally (NSE corporate announcements / a vendor), then either rebuild the raw series from the adjusted one, or build the adjusted series yourself from raw. Upstox's `GET /fundamentals/:isin/corporate-actions` does this in one call; with Dhan it is an external dependency.

## 22.3 The other three gaps (same as any broker)

Earnings/board-meeting calendar, circuit-hit **history**, and point-in-time index constituents are not in DhanHQ's API surface (no fundamentals or sector module at all). All three must come from NSE-side sources. Sector tags in particular have to be sourced externally — Upstox at least returns a sector classification from its Company Profile endpoint.

## 22.4 Execution behaviour that must be coded around

**Market orders placed via API are converted to LIMIT at the Market Protection Price.** This landed with the March regulatory changes. §13's hard stop says "immediate market order" — on Dhan that becomes a limit order at MPP, which can leave you **unfilled during a fast markdown**, precisely when the stop matters most.

Mitigation: use **SL-M with an explicit trigger** for the hard stop, or SL-L with a limit set well below the trigger (0.5–1.0% on midcaps, wider on thin names), and verify fill behaviour in the mock-trading session before going live.

Also: **static IP whitelisting** has been mandatory for API orders since 1 April, per the SEBI algo framework — same as Upstox. Orders from any other IP are rejected.

## 22.5 Rate-limit budget (500-name universe)

Dhan: Order APIs 10/sec, 250/min, 1,000/hr, 7,000/day. Data APIs 5/sec, **100,000/day, with no per-minute or 30-minute rolling cap**.

| Job | Requests | Time at 5/sec | Daily cap usage |
|---|---|---|---|
| Daily hot path (500 daily + 500 × 5-min) | 1,000 | **3.3 min** | 1.0% |
| One-time 5-year 5-min backfill (21 polls × 500) | 10,500 | **35 min** | 10.5% |
| Seasonal-factor build `s_m` | 500 (single 90-day pass) | 1.7 min | 0.5% |
| Orders + mods + cancels | ~15/day | — | 0.2% of 7,000 |

**Market Quote batches up to 1,000 instruments per request**, so a full-universe ADV/volume snapshot and the 09:08 gap-check are each **one** call, not 500.

Against Upstox, the 5-year backfill is roughly **12× faster in wall-clock terms** — Upstox's 5-minute history is capped at 1 month per request and its 2,000-requests-per-30-minutes ceiling turns the same job into ~7 hours.

## 22.6 Verdict

| Dimension | Winner | Why |
|---|---|---|
| Price data depth & backfill speed | **Dhan** | 5 yr intraday, 90-day polls, no 30-min cap |
| Corporate actions / adjusted-vs-raw | **Upstox** | has the endpoint; Dhan does not |
| Sector tags | **Upstox** | Company Profile; Dhan has none |
| Cash / collateral breakdown | tie | both sufficient; Upstox more granular |
| Exit-ladder primitives | **Dhan** | Super Order, Forever/OCO, P&L exit, kill switch |
| Slippage calibration | **Dhan** | 200-level depth vs 5-level |
| Circuit bands | **Dhan** (partial) | current bands in the quote; neither has history |
| Earnings calendar / PIT constituents | neither | external in both cases |

**Bottom line.** Yes — DhanHQ is sufficient for everything the algorithm *computes*, and it is materially better than Upstox for backfilling the 5-minute history that the jump test and `s_m` need. The three NSE-side gaps (earnings, circuit history, PIT constituents) are identical either way.

The two things to handle explicitly on Dhan: **(a)** source corporate actions externally so the ADV cap and `V_ratio` run on raw volume, and **(b)** re-express the hard stop as SL-M/SL-L rather than a market order, because API market orders are converted to limit at MPP.

Everything else — Hurst, Kalman, de-seasonalized BNS, GARCH, ADF, OU, Student-t CDF, CPCV/DSR — is local numerics and needs nothing from the broker.

*(Unverified: India VIX availability as a Dhan security ID, and whether a pre-open discovery price is retrievable at 09:08. `IDX_I` is a documented segment and India VIX is an NSE index, so resolution is likely — confirm both from an authenticated session before building G0.1 and §12.1 against them.)*

---

# PART XXIII — Broker selection for NSE 2026

## 23.1 The deciding metric: 5-minute history depth

The jump test (§7.2) and the seasonal factors `s_m` (§7.1) both need 5-minute bars. In **live** trading you only need the current dip window plus 60 sessions of history — every broker copes. The depth question matters entirely for **Part XVI validation**, where regime-stratified CPCV wants the March-2020 crash as a natural stress test.

Computed wall-clock to backfill 500 symbols at each broker's deepest 5-minute history:

| Broker | 5-min depth | Days/poll | Req/sec | Polls/sym | Requests | Wall clock | Reaches Mar-2020? |
|---|---|---|---|---|---|---|---|
| **Kite Connect** | **~2015 (≈11 yr)** | 90 | 3 | 45 | 22,500 | ~2.1 hr | **YES** |
| Dhan | 5 yr (~2021) | 90 | 5 | 21 | 10,500 | **35 min** | no |
| Fyers | ~3–4 yr | 100 | 10 | 15 | 7,500 | 22 min | no |
| Upstox | ~4.7 yr (from Jan 2022) | 30 | 50 | 58 | 29,000 | ~7.2 hr | no |
| Angel SmartAPI | *unconfirmed* | 100 | 10 | — | — | — | *unconfirmed* |

Kite is the only one whose 5-minute history reaches the March-2020 crash. Dhan is the fastest per-request but stops at ~2021.

*Confidence notes:* Kite's "intraday from 2015" and the 90-day 5-minute window come from Kite forum answers (the day-window table dates to 2018 and may have moved). Fyers' "3–4 years" is a user report, not official documentation. Angel's per-interval window is documented (5-min = 100 days) but total depth is not — **treat Angel's depth as unverified.**

## 23.2 Scored comparison

| Criterion | Kite | Dhan | Upstox | Fyers | Angel |
|---|---|---|---|---|---|
| 5-min depth | **best** | good | fair | fair | unknown |
| Daily depth | 2015+ | inception | Jan 2000 | long | decades |
| Backfill speed | slow (3/s) | **best** | worst (30-min cap) | good | — |
| **Corporate actions API** | no | no | **yes** | no | no |
| Sector classification | no | no | **yes** | no | no |
| Earnings / board-meeting calendar | no | no | no | no | no |
| Cash vs collateral split | yes | yes | **most granular** | yes | yes |
| Exit-ladder primitives | GTT, OCO | **Super Order, Forever/OCO, P&L exit, kill switch** | GTT | GTT | GTT |
| Market depth | 5 levels | **200 levels** | 5 levels | 5 levels | 5 levels |
| Circuit bands in quote | yes | yes | limited | yes | yes |
| API cost | ₹500/mo | **free** | paid | **free** | **free** |
| SEBI static-IP compliance | yes | yes | yes | yes | yes |

## 23.3 The architectural conclusion

**No Indian broker API covers this algorithm alone.** The four hardest inputs — corporate actions, earnings/board-meeting calendar, circuit-hit history, point-in-time constituents — are absent from every broker. Only Upstox has corporate actions and sector tags, and even it has neither an earnings calendar nor PIT constituents.

The correct build is **two layers**, not one broker:

**Layer 1 — Broker (execution + OHLCV + funds).**
- **Kite Connect (₹500/month)** if you want to validate through March 2020. It is the only source with 5-minute data back to ~2015, and Part XVI's regime stratification is worth ₹6,000/year — about 0.06% of a ₹1 Cr book.
- **Dhan (free)** if 2021 onward is enough, or if you want the strongest execution primitives (Super Order, Forever/OCO, kill switch) and 200-level depth for calibrating the slippage assumption in §15.3.

**Layer 2 — Reference data vendor.** An NSE-authorised vendor such as **TrueData** or **GlobalDataFeeds** supplies what no broker does:
- **Corporate actions API** (dividends, bonus, splits, rights, buybacks with ex-dates) → solves the adjusted-vs-raw dual-series problem in §2.2
- **Corporate announcements API** (board meetings, financial results, prior intimation) → **this is the earnings calendar for gate G1.4**, which is otherwise unavailable anywhere
- Fundamentals and shareholding patterns

**Layer 3 — DIY from NSE.** Circuit-filter hit history and point-in-time index constituents still have to be archived from NSE sources. No vendor or broker provides them as a clean historical series.

## 23.4 Recommendation

**Primary: Kite Connect for data + execution, TrueData for the reference layer.**

Choose Dhan over Kite only if: (a) you accept backtesting from 2021, and (b) the Super Order / kill-switch primitives materially simplify your exit and heat-cap implementation, and (c) you want 200-level depth to replace the guessed 0.30% slippage with a measured one.

Either way, budget for the reference-data subscription. Running this algorithm on broker data alone means the earnings blackout (G1.4) and the raw-volume requirement (§2.2) cannot be satisfied at all — and both are load-bearing.

## 23.5 Open items to confirm before committing

1. **Bracket/cover order availability.** SEBI restricted retail bracket and cover orders; Dhan's Super Order (launched March 2025) is the modern replacement. Confirm the current product status with the broker before designing §13 around it — the fallback is GTT/OCO legs, which every broker supports.
2. **API market-order handling.** Dhan converts API market orders to LIMIT at MPP. Check the equivalent behaviour on Kite and build the hard stop as SL-M/SL-L regardless.
3. **Angel SmartAPI 5-minute depth** — undocumented; test before considering it.
4. **India VIX and pre-open discovery price** — unverified on both Dhan and Upstox. Confirm from an authenticated session before building G0.1 and §12.1.

---

# PART XXIV — Numerical corrections found during implementation

Two components specified in Parts VII and VIII do not survive contact with synthetic
data of known parameters. Both are load-bearing: they decide which names enter the
trade book. Both are corrected in `jfou/indicators.py`, and the evidence is pinned by
`tests/test_indicators.py`.

## 24.1 Gate G2.1 — the Hurst estimator

**Specification:** Rescaled-range (R/S) analysis over a 100-session window, gate at
H > 0.60.

**Measured:** on 300 synthetic pure random walks, window = 100:

| estimator | null mean | null sd | P(H > 0.60 \| pure noise) |
|---|---|---|---|
| R/S naive (as specified) | 0.618 | 0.100 | **54.3 %** |
| R/S with Anis–Lloyd correction | 0.525 | 0.100 | 22.0 % |
| aggregated-variance, lags ≤ 10 % of window | 0.466 | 0.088 | **4.7 %** |

As specified, the gate passes pure noise more often than it rejects it — a 54 % false
positive rate means G2.1 is not a filter, it is a coin flip with a bias toward letting
mean-reverting-looking noise through into a trend-following gate.

**Correction:** estimate H from the scaling of k-period return variance,
Var(r_k) ~ k^(2H), with lags capped at 10 % of the window. Under a random walk the
slope is exactly 1 and H = 0.5 by construction. Its small downward null bias (0.466)
is in the safe direction: it makes the persistence gate harder to pass.

Discrimination check, 200-session window: momentum (AR(1) φ = 0.5) → 0.618, random
walk → 0.470, mean-reverting → 0.142. Correctly ordered.

**Note on test design.** An earlier check concluded the estimator had no power because
it used a *drifting* series as the trending case. A random walk with constant drift
still has i.i.d. increments and H = 0.5 exactly; Hurst measures autocorrelation of
increments, not drift. The alternative must be a momentum process.

## 24.2 Gate S2 — the OU half-life sample

**Specification:** fit AR(1) on stacked historical pullback episodes rather than the
full spread, because "if a stock trended strongly for 26 days and pulled back for 4
days, 85 % of your regression points represent the trending regime."

**Measured:** every episode-selection rule that conditions on the path biases λ
negative. Synthetic AR(1), 40 replications of 3,000 observations:

| selection rule | true λ = −0.20 | true λ = −0.30 |
|---|---|---|
| **full spread** | **−0.1995** | **−0.2988** |
| fixed 10-bar window after each downward crossing | −0.2512 | −0.3578 |
| x < 0 points only | −0.3863 | −0.5286 |
| full down-and-back recovery path | −0.4459 | −0.6081 |

The x < 0 rule halves the apparent half-life: λ = −0.20 → τ½ = 1.41 d instead of
3.11 d. Since positions are sized off τ½, that bias converts directly into overstated
edge — the system would believe dips revert twice as fast as they do and would size
accordingly.

**Correction:** fit AR(1) on the full spread, which is unbiased. The spec's
regime-contamination concern is legitimate but is addressed by a *validity guard*
rather than by subsampling: `ou_context()` reports the number of completed pullback
episodes in the lookback, and a name with too few episodes has an estimate that says
nothing about dip recovery and must not be trusted.

## 24.3 EMA warm-up (bug, not a specification issue)

`ewm(min_periods=n)` leaves the first n − 1 values undefined. Left in the array they
propagate through `np.linalg.lstsq` and return NaN, silently voiding gate S2 for every
name in the universe — no error, just an empty candidate list. `ema_spread()` now drops
non-finite values before any regression touches the spread.
