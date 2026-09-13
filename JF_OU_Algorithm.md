# JF-OU Production Trading Algorithm
## Jump-Filtered Ornstein–Uhlenbeck Trend-Conditioned Mean Reversion
### Complete Engineering Specification — v2.0 (rewritten from scratch)

**Market:** NSE India (cash + F&O). **Holding horizon:** 1–8 trading days. **Direction:** long only.

This document is a self-contained, implementation-ready specification. Every parameter appears exactly once in the Master Parameter Table (§12) and is referenced by name everywhere else. Where the source material contradicted itself, the resolution is recorded in §13 (Conflict Resolution Register) — nothing is silently chosen.

---

# PART I — What the system is trying to do

Buy a stock that is in a **statistically proven uptrend** and has made a **short, statistically anomalous dip** that is:

- caused by continuous liquidity imbalance (not a news jump),
- fast enough to mean-revert within days,
- happening on drying volume (sellers exhausting),
- landing on a level where institutions bought (anchored VWAP),
- with no scheduled binary event inside the holding window.

Then enter only on **confirmation** (a reclaim of the prior day's high), size with a **fat-tail-corrected half-Kelly**, and exit on a **deterministic ladder**.

No step guarantees a rise. The system's job is to reject ~99% of setups and survive the ones it takes.

---

# PART II — Data & infrastructure prerequisites

## 2.1 Data series required per symbol

| Series | Granularity | Purpose |
|---|---|---|
| OHLCV, split-**and**-dividend-adjusted | Daily, 250+ sessions | Trend, z-score, OU, ATR |
| Unadjusted OHLCV | Daily | ADV, volume ratio (volume must not be price-adjusted) |
| 5-minute returns | Intraday, 75 bars/session | Jump test |
| Intraday seasonal factors `s_m` | Per 5-min slot, rolling 60 sessions | De-seasonalization |
| India VIX | Daily + intraday | Macro gate |
| Benchmark index (Nifty 50) | Daily | Macro gate |
| Sector / sub-industry tag | Static, point-in-time | Concentration cap |
| Earnings & board-meeting calendar | Event dates | Binary-event gate |
| Circuit-filter hit log | Per session | Circuit screen |
| Point-in-time index constituents | By date | Survivorship-bias-free backtest |
| F&O lot sizes | Current contract | Lot quantization |
| Unencumbered settled cash | Live from broker | Sizing base |

## 2.2 Data-integrity rules (non-negotiable)

1. **Anchored VWAP must use adjusted price × adjusted volume** from a consistent series. Mixing adjusted price with raw volume corrupts `AVWAP = ΣP·V / ΣV` and produces false rejections after bonus/split events.
2. **ADV and volume ratios must use raw (unadjusted) volume.** Do not feed adjusted volume into liquidity math.
3. **Point-in-time universe:** on backtest date *t*, the eligible universe is exactly the index membership on date *t*. Using today's Nifty 500 for 2018–2026 inflates win rate by roughly 4–8% annualized.
4. Missing bars, halted sessions, and zero-volume bars are dropped before any window statistic is computed, and the window is refilled from history so all windows stay full-length.

## 2.3 Pipeline clock (IST)

| Time | Job |
|---|---|
| **15:35** | Batch scan on **final** daily bar + complete 5-minute arrays → rank candidates → write next-day order list |
| **09:08** | Ingest NSE **Pre-Open Discovery Price** → run Gap-Cancel Rule on every armed order |
| **09:15:01** | Place surviving **GFD** buy stop-limit orders |
| **15:20 (day t+1)** | Hard-cancel every unfilled order. Signal expires. Recompute from scratch on day t+1 close. |

**Why not scan at 15:20?** The day-*t* bar is incomplete and the final 5-minute bar has not printed, so the jump test is computed on a phantom session. **Why not wait until 15:35 to trade?** You would place orders the next morning anyway — which is exactly what the 09:15:01 step does, but with the gap check inserted first.

---

# PART III — Mandatory gates (binary; any failure = reject)

## G0 — Macro regime gate (market-wide circuit breaker)

Individual alpha collapses when stock-to-index correlation approaches 1.0.

- **G0.1** Benchmark close > EMA₅₀(benchmark)
- **G0.2** India VIX < **21.0**
- **G0.3** VIX not expanding more than **10%** intraday

Any failure → **suspend all new entries** for the session. Existing positions keep their normal exit ladder.

## G1 — Universe & tradability gates

- **G1.1 Liquidity:** minimum average daily traded value (configurable; suggested ₹50 Cr) and non-penny price.
- **G1.2 Segment:** prefer NSE **F&O-eligible** equities (dynamic cooling bands, not hard circuits).
- **G1.3 Circuit screen:** if trading cash-only non-F&O names, **exclude any stock that hit a price circuit filter in the last 30 sessions.** A market stop-loss in a locked-lower-circuit midcap is unexecutable; loss can far exceed 1.0R.
- **G1.4 Binary-event screen:** the next scheduled earnings result / board meeting must be **more than 10 trading days away**, OR earnings must have occurred **within the prior 3 sessions**. The intraday jump test is blind to *future* announcements; an overnight 10% earnings gap bypasses the stop entirely.
- **G1.5 Point-in-time membership** (see §2.2.3).

## G2 — Trend persistence gate

Three independent conditions, **all** required.

**G2.1 Hurst exponent.** Rolling **100-day** Rescaled Range (R/S) on daily log prices. Require **H > 0.60**. Below that the series is not structurally persistent and "trend" is noise.

**G2.2 Kalman velocity.** Local-linear state-space model:

```
state:   x_t = [price_t, velocity_t]ᵀ
predict: x_t = F · x_{t−1} + w_t ,   F = [[1, 1], [0, 1]]
observe: y_t = H · x_t + v_t ,       H = [1, 0]
noise:   w_t ~ N(0, Q) ,  v_t ~ N(0, R)
```

- Require **velocity_t > 0**
- Require the slope to be positive at the **95% confidence bound** (use the filter covariance `P_t` for the velocity standard error)

**Q/R must not be hardcoded.** Estimate `Q` and `R` by **Expectation-Maximisation on rolling 60-day blocks**, or scale process noise to the GARCH conditional variance: `Q = κ · σ²_t`. Too-high Q/R makes velocity oscillate on noise (false signals); too-low Q/R turns the filter into a sluggish moving average that lags turns by 10+ sessions.

**G2.3 Baseline drift confirmation.**
- OLS of `ln(Close)` on time over **60 days**: t-statistic of slope **t_β ≥ 2.5** (≈99% confidence in upward drift)
- **Close_t > EMA₅₀**

*Why both G2.2 and G2.3:* plain 60-day OLS is endpoint-sensitive and lags momentum shifts; the Kalman velocity tracks slope without that lag. They are used together, not as substitutes.

## G3 — Jump classification gate

The dip must be a **continuous diffusion** move, not a **Poisson jump**. A jump-driven dip is the start of a markdown, not a mean-reversion opportunity.

**G3.1 De-seasonalize intraday returns first.** Equity variance is U-shaped: the first 30 minutes (09:15–09:45) and last 30 minutes (15:00–15:30) routinely show 300–500% higher variance than the midday lull. Raw BNS on unadjusted 5-minute returns misreads ordinary open prints as jumps and falsely aborts more than half of clean pullbacks.

```
r*_m = r_m / s_m        m ∈ [1, 75]
s_m  = rolling historical average |return| for 5-minute slot m (60 sessions)
```

**G3.2 Bipower-variation jump statistic** on the de-seasonalized returns over the dip window:

```
RV_t  = Σ (r*_m)²
BV_t  = (π/2) · Σ |r*_m| · |r*_{m−1}|
Z_jump = √n · (RV_t − BV_t) / RV_t
```

Require **Z_jump < 2.15** (p > 0.05). At or above 2.15 → **abort; structural shock.**

**G3.3 Overnight gap check (mandatory — BNS is intraday-only).** More than 70% of news shocks land overnight. A stock that gaps down 6% and then trades quietly all day returns `Z_jump ≈ 0` and fools the intraday test.

```
|ln(Open_t) − ln(Close_{t−1})| ≤ 1.5 · σ_daily
```

Fail → **abort.**

## G4 — Stationarity gate (protects all downstream OU math)

The half-life formula is valid **only if the spread is I(0)**. In a genuine breakdown, `x_t = ln(P_t) − ln(EMA₂₀)` behaves like a random walk with drift, and an AR(1) fit on an integrated series returns a **spurious** mean-reversion coefficient.

- Run an **Augmented Dickey–Fuller test on `x_t`**
- Require **ADF p-value < 0.05**
- Non-stationary → **reject** (do not compute a half-life at all)

## G5 — Sizing feasibility gate (computed after ranking; see §Part V)

Even-lot quantization, ADV participation cap, unencumbered cash. Failures here reject the trade on **capacity**, not on signal quality — logged separately so it is never confused with a bad signal.

---

# PART IV — Scored confluence (need ≥ 3 of 4)

Eight stacked hard `AND` filters would produce fewer than 1–2 trades per quarter even across 500 names. Mandatory integrity/trend/jump gates stay binary; the four *quality* signals are scored. **Require ≥ 3 of 4 points.** A **4/4** earns ranker priority (§Part VI).

## S1 — Pullback magnitude (GARCH studentized z-score)

Do **not** use a raw rolling standard deviation: it assumes homoskedasticity and misclassifies regime shifts as pullbacks. Fit **GARCH(1,1)** on daily returns (window 60) to get conditional variance `σ²_t`.

```
Z_GARCH = (P_t − μ_t) / σ_t        μ_t = Kalman state price (fallback: EMA₂₀)
```

**Point if −2.20 ≤ Z_GARCH ≤ −1.40.**

- Above −1.40: not discounted enough — no edge.
- Below −2.20: extreme outlier, usually liquidation or fundamental break — high continuation risk.

GARCH automatically **widens** the band in stress and **tightens** it in calm drift, which is what prevents premature entries.

## S2 — Reversion speed (OU half-life)

**Fit on historical pullback episodes, not a rolling calendar window.** Fitting AR(1) on the last 30 days when the stock trended for 26 and dipped for 4 means ~85% of the sample is the trending regime; the resulting θ describes volatility *around* the trend, not recovery speed *from a deficit*.

**Procedure:** over the past **250 sessions**, identify pullback episodes (consecutive down/flat sessions following a local high within an uptrend leg). Stack the `x_t` observations from those episodes and fit:

```
Δx_t = λ · x_{t−1} + ε_t
φ = 1 + λ ,   θ = −ln(1 + λ) ,   τ_½ = ln(2)/θ = −ln(2)/ln(1 + λ)
```

**Point if:**
- λ < 0 with **p < 0.05** (statistically real mean reversion), **and**
- **1.0 ≤ τ_½ ≤ 3.5 trading days**

Longer than 3.5 days → recovery too sluggish for the holding window.

*Verified:* λ = −0.20 → θ = 0.2231 → τ_½ = **3.11 d** ✓ | λ = −0.50 → τ_½ = **1.00 d** ✓ | λ = −0.10 → τ_½ = 6.58 d ✗

**If S2 fails the point but G4 passed** (stationary, just out of band), the trade may still enter on 3 other points, and the time stop falls back to the **Day-8 default** (§Part VII) because no valid τ is available.

## S3 — Volume exhaustion

- **V_ratio < 0.60** — volume and candle-body size on the last 2–4 down sessions versus a recent average
- **Volume_t < SMA₂₀(Volume)** — confirms total participation is drying up, not just red candles shrinking

Both required for the point. Heavy volume on down days = distribution, not exhaustion.

## S4 — Anchored VWAP confluence

Oscillators have no memory of where size entered. AVWAP does.

**Deterministic anchor `t₀` (no discretionary "eyeball the swing low"):**

1. **Primary — volumetric breakout origin:** search `i ∈ [t−40, t−5]` for the most recent bar that broke a **20-day Donchian high** *with* volume expansion (Volume_i > 1.5 × SMA₂₀(Volume)_i). Set `t₀ = i`.
2. **Fallback — structural pivot low:** if no such breakout exists, set `t₀ = argmin(Low_i)` over `i ∈ [t−30, t−5]`, confirmed as a pivot by a lower low on neither side within ±3 bars.
3. **Staleness constraint:** if `|Close_t − AVWAP_{t₀,t}| / AVWAP_{t₀,t} > 0.04`, the anchor is stale → **discard the candidate.**

```
AVWAP_{t₀,t} = Σ_{i=t₀}^{t} (P_i · V_i) / Σ_{i=t₀}^{t} V_i     (adjusted series — §2.2.1)
```

**Point if the current candle's low tests the band:** `Low_t ≤ AVWAP × (1 + tol)` with the close holding above it (`tol` ≈ 0.25 × ATR₁₄ expressed as a percentage, configurable). Institutional breakout buyers defend their average cost basis there.

---

# PART V — Position sizing engine

## 5.1 Payoff ratio — calibrated, not assumed

The exit ladder scales 50% at Target 1 (~1.0R) and 50% at Target 2 (2.5R):

```
b_gross = 0.5(1.0R) + 0.5(2.5R) = 1.75R
b_net   = 1.75R − 0.15R (round-trip fees, slippage, STT) = 1.60R
```

**b = 1.60.** Using the headline 2.5R in Kelly would over-lever the book by ~36%. *Alternative:* if you instead hold 100% to the 2.5R target with no scale-out, `b = 2.5` becomes correct — but the exit ladder below must then change. Do not mix the two.

## 5.2 Reversion probability — fat-tail corrected

The standard normal CDF understates how long extreme dips take to revert. Use the **Empirical CDF** of the stock's historical residuals, or a **Student's t CDF with ν = 4 or 5** degrees of freedom, fitted on that stock's residuals:

```
p = F_t,ν( expected reversion move / σ_t )
```

**Hard sizing floor: if p ≤ 0.40 → size = 0, abort the trade.**

*Why 0.40:* Kelly turns negative below `p = 1/(1+b) = 1/2.60 = 0.3846`, which would instruct the engine to *short* a stock in a proven uptrend. The 0.40 floor sits safely above that break-even.

## 5.3 Fractional half-Kelly

```
f*      = (p·(b+1) − 1) / b          full Kelly, b = 1.60
f_trade = 0.5 · f*                   fractional half-Kelly
f_trade = max(f_trade, 0)            never negative
```

*Verified values (b = 1.60):*

| p | f* | half-Kelly f |
|---|---|---|
| 0.38 | −0.0075 | 0 (floored) |
| 0.40 | +0.0250 | 0.0125 |
| 0.45 | +0.1063 | 0.0531 |
| 0.50 | +0.1875 | 0.0938 |
| 0.55 | +0.2688 | 0.1344 |
| 0.60 | +0.3500 | 0.1750 |

## 5.4 Capital base — regulatory reality

**Never size off nominal NAV.** Feed the engine **unencumbered settled cash** only:

- **F&O overnight:** SEBI requires a minimum **50:50 cash-to-collateral** ratio. Pledged equity is not deployable into new futures without unencumbered cash.
- **Cash segment:** Day-*t* Target-1 proceeds are subject to **T+1 settlement** and do not recycle into same-day triggers.

```
C_deployable = unencumbered_settled_cash − margin_already_committed
```

## 5.5 The four sizing constraints (all applied, minimum wins)

```
shares_kelly = (f_trade · C_deployable) / Entry
shares_risk  = (0.015 · Total_Equity) / R          # 1.5% hard capital stop
shares_adv   = 0.015 · ADV20_shares                # Barra participation ceiling
shares       = min(shares_kelly, shares_risk, shares_adv)
```

**Square-root impact law.** Impact ≈ `Y · σ · √(Q/ADV)`. Half-Kelly sizes on σ_t and p and is completely blind to order-book depth; on a midcap with ₹15 Cr ADV, a ₹40–50 lakh conviction order will move the book against you and eat the entire 1.0R margin. The **1.5% of 20-day ADV** cap is an absolute ceiling on a single order.

## 5.6 F&O lot quantization

Contracts are discrete integers. A market order to "sell 50%" of an odd lot count is **rejected by the exchange** ("order quantity not a multiple of lot size").

```
N_lots = floor( shares / (2 · Lot_Size) ) · 2      # even integer multiple
if N_lots < 2:  REJECT (capital insufficient for a multi-target scale-out)
final_shares = N_lots · Lot_Size
```

*Why even:* you cannot sell 50% of 3 lots. Even multiples guarantee both scale-out tranches are integer lots.

## 5.7 ⚠ Verified structural finding — read before deploying

Two consequences fall out of §5.5–5.6 that the source material does not mention. Both were computed, not assumed.

**(a) The 1.5% risk cap almost never binds in the cash segment.**
When unencumbered cash ≈ equity, risk per trade = `f_trade × (R/Entry)`:

| p | f_trade | R/E = 1% | R/E = 2% | R/E = 3% |
|---|---|---|---|---|
| 0.45 | 0.0531 | 0.053% | 0.106% | 0.159% |
| 0.50 | 0.0938 | 0.094% | 0.188% | 0.281% |
| 0.55 | 0.1344 | 0.134% | 0.269% | 0.403% |
| 0.60 | 0.1750 | 0.175% | 0.350% | 0.525% |

To make the cap bind at R/E = 2% you would need `f > 0.75`, but half-Kelly maxes out near **0.175**. It only binds at roughly **5.6× leverage** (i.e. in the F&O segment). The engine is therefore **structurally conservative in cash** — Kelly notional is the binding constraint, not the risk cap. This is safe but under-levered; if you want the 1.5% risk cap to be the operative rule, invert the order and use `shares_risk` as the target with `shares_kelly` as the ceiling.

**(b) Even-lot quantization rejects most signals on small accounts.**
Because sizing scales with *cash* while lots are fixed-size, there is a hard minimum account size. Minimum **unencumbered cash** for half-Kelly alone to reach the 2-lot floor:

| Symbol | Lot | Price | p = 0.45 | p = 0.50 | p = 0.55 |
|---|---|---|---|---|---|
| RELIANCE | 250 | ₹1,400 | 131.8 L | 74.7 L | 52.1 L |
| INFY | 400 | ₹1,800 | 271.1 L | 153.6 L | 107.2 L |
| TCS | 175 | ₹3,900 | 256.9 L | 145.6 L | 101.6 L |
| ADANIENT | 300 | ₹2,400 | 271.1 L | 153.6 L | 107.2 L |
| BANKNIFTY | 35 | ₹400 | 5.3 L | 3.0 L | 2.1 L |

A ₹50 lakh account with ₹20 lakh unencumbered, at p = 0.55, gets **269 shares** — which quantizes to **0 lots** and is rejected on capacity, not signal. Log this as `REJECT_CAPACITY`, never as `REJECT_SIGNAL`.

---

# PART VI — Cross-sectional ranker (concurrency collision)

On an index rebound, 15–25 names may pass every gate at once. At ~8% each you would need 160–200% of capital. **Take only the top 3–5.**

```
Score = w₁ · z(H)  +  w₂ · z(−τ_½)  +  w₃ · z(−Z_GARCH)      (suggest w₁ = w₂ = w₃ = 1/3)
```

- **z(H)** — steepest structural trend (highest Hurst)
- **z(−τ_½)** — fastest expected recovery (shortest half-life)
- **z(−Z_GARCH)** — deepest discount

`z(·)` = cross-sectional z-score across the day's candidate pool. **+1 bonus for 4/4 confluence.** Ties break on higher ADV (better fill).

---

# PART VII — Order construction & exit protocol

## 7.1 Entry order

```
Trigger price = High_t × 1.0005              # High of signal day + 0.05%
Limit price   = Trigger price × 1.0020       # 0.20% slippage buffer
Order type    = Stop-Limit, Good-For-Day (GFD)
```

**Two execution bugs this fixes:**

- **Phantom-fill / stranded order.** Setting trigger = limit means a fast momentum spike gaps the ask over your limit and the order rests unfilled. The 0.20% buffer absorbs it.
- **R-inflation from a gap-up.** If day *t+1* opens well above yesterday's high, you fill high while the stop stays at the dip low, so `R = Entry − Stop` nearly doubles and the 2.5R target becomes unreachable. Hence:

```
GAP-CANCEL RULE (evaluated at 09:08 on pre-open discovery price):
  if Open_{t+1} > High_t × 1.012  OR  Open_{t+1} > High_t + 0.75 · ATR₁₄:
      cancel the order; signal is void
```

**Order lifetime — hard GFD.** If day *t+1* trades as an inside bar and never breaks yesterday's high, the order is **hard-cancelled at 15:20 on day t+1**. A trigger on day *t+2* or *t+3* is trading a decayed OU half-life — the statistical edge has expired. Recompute from scratch.

## 7.2 Risk unit

```
Stop price S = min( Low_dip − 0.75 · ATR₁₄ ,  AVWAP_{t₀} × 0.992 )
R            = Entry − S
```

**Reject the trade if R > 1.25 · ATR₁₄** — the stop is too far, the entry is too extended, and the reward-to-risk no longer supports b = 1.60.

**The buffer is 0.75 × ATR₁₄, not 0.25 × ATR₁₄.** A 0.25 buffer is only ~0.3–0.6% on large/midcaps; intraday liquidity sweeps that wick below the dip low before moving up will stop you out on day 1–2 more than 40% of the time even when the direction is right.

## 7.3 Exit ladder (deterministic — Kelly requires an empirical b)

| # | Trigger | Action |
|---|---|---|
| **1** | Price ≤ S (hard stop, 1.0R) | Immediate market order, full exit |
| **2** | Price ≥ Kalman state mean **or** ≥ EMA₂₀ | **Target 1:** sell 50%; move stop on remaining 50% to breakeven (`Entry × 1.0005`) |
| **3** | Price ≥ Entry + 2.5 · R | **Target 2:** limit-sell the remaining 50% |
| **4** | After Target 1, daily close < EMA₉ | Dynamic trail: market order next open (rides persistent right-tail momentum) |
| **5** | Holding ≥ `min(ceil(2.5 · τ_½), 8)` sessions | **OU time stop:** market-on-close. If it has not reverted within 2.5 half-lives, the thesis failed |

*Verified time stops:* τ = 1.0 → 3 d | τ = 2.0 → 5 d | τ = 2.8 → 7 d | τ = 3.5 → ceil(8.75) = 9 → **capped at 8 d**. The `min(·, 8)` reconciles the OU-scaled stop with the fixed Day-8 backstop; if S2 failed, use 8 directly.

**Exit rules 1, 2, 3, 5 are what make `b` empirical rather than theoretical.** Without a deterministic stop, target, and time stop, Kelly is sizing off a number that was never earned, and losing trades become open-ended bag-holding.

---

# PART VIII — Portfolio-level constraints

- **P1 Sector cap:** **maximum 1 concurrent position per sector/sub-industry.** Six IT midcaps can trigger identical signals during a Nifty-IT-only correction; taking four is an 80% single-industry bet that stops out concurrently on a sector downgrade.
- **P2 Sector exposure:** total sector exposure ≤ **25%** of book.
- **P3 Portfolio heat:** with the top-3–5 ranker and half-Kelly, verify `Σ (shares × R) ≤ 5 × 1.5% = 7.5%` of equity before placing. If exceeded, drop the lowest-ranked candidate.
- **P4 Macro suspension:** G0 failure suspends new entries but does **not** force-close existing positions (they run their own ladder).

---

# PART IX — Backtest validation (mandatory before real capital)

The framework has ~14 tunable parameters. With that many, an annualized Sharpe > 2.5 is **mathematically trivial to find by chance**.

**Parameter count:** Hurst window (100), Hurst cutoff (0.60), OLS lookback (60), t-stat cutoff (2.5), GARCH window (60), z-bounds (−2.20/−1.40), OU window (30), half-life bounds (1.0/3.5), volume exhaustion (0.60), gap threshold (1.2%), stop buffer (0.75 ATR), ADV cap (1.5%), payoff b (1.60), p-floor (0.40).

1. **Combinatorial Purged Cross-Validation (CPCV)** — with embargo windows so overlapping labels do not leak across train/test splits.
2. **Deflated Sharpe Ratio (López de Prado)** — corrects for the number of trials and the non-normality/autocorrelation of returns.

**Acceptance rule: DSR p-value ≤ 0.05.** If DSR p > 0.05, the backtest result is an artifact of parameter overfitting, not alpha — **do not deploy.**

Also required: point-in-time universe (§2.2.3), and report results with fees/slippage/STT included so `b = 1.60` matches realized execution.

---

# PART X — Pseudocode

```python
# ---------- 15:35 IST BATCH ----------
def daily_scan(date):
    if not macro_gate():                      # G0
        return []                             # VIX >= 21 or index < EMA50 or VIX +10%
    universe = pit_constituents(date)         # G1.5 survivorship-free
    cash     = unencumbered_settled_cash()    # 5.4

    candidates = []
    for s in universe:
        d = adjusted_ohlcv(s, 250); r = raw_ohlcv(s, 60)

        if not tradable(s, date):             # G1.1-G1.4 liquidity/circuit/earnings
            continue

        # --- G2 trend ---
        if hurst(d.close, 100) <= 0.60:                       continue
        vel, ok = kalman_velocity(d.close, QR=em_or_garch(d)) # F=[[1,1],[0,1]]
        if not (vel > 0 and ok):                              continue
        if tstat_ols_slope(d.close, 60) < 2.5:                continue
        if d.close[-1] <= ema(d.close, 50)[-1]:               continue

        # --- G3 jump ---
        r5 = deseasonalize(intraday_5min(s, dip_window))      # r_m / s_m
        if bns_z(r5) >= 2.15:                                 continue
        if abs(log(d.open[-1]/d.close[-2])) > 1.5*sigma_daily: continue

        # --- G4 stationarity ---
        x = log(d.close) - log(ema(d.close, 20))
        if adf_pvalue(x) >= 0.05:                             continue

        # --- S1-S4 scored confluence ---
        pts = 0
        pts += -2.20 <= z_garch(x, d) <= -1.40                                    # S1
        tau, lam_ok = ou_halflife_on_pullback_episodes(x, 250)                    # S2
        pts += lam_ok and 1.0 <= tau <= 3.5
        pts += v_ratio(r) < 0.60 and r.volume[-1] < sma(r.volume, 20)[-1]         # S3
        t0 = anchor_t0(d, r, window=(40, 5))                                      # S4
        av = avwap(d, r, t0)
        if abs(d.close[-1]-av)/av > 0.04:                     continue            # stale
        pts += d.low[-1] <= av*(1+band(d)) and d.close[-1] > av

        if pts < 3:                                           continue

        candidates.append(Cand(s, tau, t0=t0, avwap=av, pts=pts,
                              trigger=d.high[-1]*1.0005,
                              stop=min(dip_low(d) - 0.75*atr(d,14), av*0.992)))

    # --- rank + enforce concurrency, sector, heat ---
    for c in candidates: c.stop_rejected = (c.trigger - c.stop) > 1.25*atr_of(c)
    ranked = rank(candidates)[:5]                             # Part VI
    return apply_sector_and_heat_caps(ranked, cash)

# ---------- 09:08 IST ----------
def pre_open_check(c):
    gap = nse_preopen_discovery_price(c.sym)
    return not (gap > c.prev_high*1.012 or gap > c.prev_high + 0.75*c.atr)

# ---------- 09:15:01 IST ----------
def place_orders(ranked):
    for c in ranked:
        if not pre_open_check(c):          continue          # gap-cancel
        R     = c.trigger - c.stop
        if R > 1.25*c.atr:                 continue          # risk distance cap
        p     = student_t_cdf(c.x, nu=4)                     # 5.2
        if p <= 0.40:                      continue          # hard floor
        f     = max(0.5*((p*(1.6+1)-1)/1.6), 0)              # 5.3
        sh    = min(f*CASH/c.trigger, 0.015*EQUITY/R, 0.015*c.adv20)   # 5.5
        lots  = (sh // (2*c.lot))*2                          # 5.6
        if lots < 2:                       log("REJECT_CAPACITY", c); continue
        submit(stop_limit(trigger=c.trigger,
                          limit=c.trigger*1.0020,
                          qty=lots*c.lot, validity="GFD"))   # 7.1

# ---------- 15:20 IST day t+1 ----------
cancel_all_unfilled()                                        # hard GFD

# ---------- while holding ----------
def manage(pos):
    if pos.price <= pos.stop:                       exit_market()           # 7.3-1
    elif not pos.t1 and pos.price >= pos.t1_level:  sell(0.50); move_stop_to_bre()  # 2
    elif pos.t1 and close_ema9_break():             exit_market_next_open()  # 4
    elif pos.price >= pos.entry + 2.5*pos.R:        sell(remainder)          # 3
    if pos.hold_days >= min(ceil(2.5*pos.tau), 8):  exit_moc()               # 5
```

---

# PART XI — Rejection taxonomy (log every one — you cannot tune what you cannot count)

| Code | Meaning |
|---|---|
| `REJ_MACRO` | G0: index below EMA50, VIX ≥ 21, or VIX expanding >10% |
| `REJ_UNIVERSE` | G1: liquidity, circuit hit, or binary event inside 10 sessions |
| `REJ_TREND` | G2: H ≤ 0.60, Kalman velocity ≤ 0, t_β < 2.5, or below EMA50 |
| `REJ_JUMP_INTRADAY` | G3.2: Z_jump ≥ 2.15 |
| `REJ_JUMP_OVERNIGHT` | G3.3: overnight gap > 1.5σ |
| `REJ_NONSTATIONARY` | G4: ADF p ≥ 0.05 (spurious mean reversion) |
| `REJ_STALE_ANCHOR` | S4: anchor more than 4% from current price |
| `REJ_CONFLUENCE` | fewer than 3 of 4 scored points |
| `REJ_STOP_TOO_FAR` | R > 1.25 × ATR₁₄ |
| `REJ_LOW_PROB` | p ≤ 0.40 |
| `REJ_CAPACITY` | ADV cap or lot quantization gave < 2 lots |
| `REJ_SECTOR` | sector cap / 25% book exposure |
| `REJ_HEAT` | portfolio heat exceeded, dropped by rank |
| `CANCEL_GAP` | gap-cancel rule at 09:08 |
| `CANCEL_GFD` | untriggered, cancelled 15:20 day t+1 |

---

# PART XII — Master parameter table

| # | Parameter | Value | Section |
|---|---|---|---|
| 1 | Hurst window / cutoff | 100 days / H > 0.60 | G2.1 |
| 2 | Kalman Q,R | EM on 60-day blocks, or Q = κ·σ²_t | G2.2 |
| 3 | OLS lookback / t-stat cutoff | 60 days / t_β ≥ 2.5 | G2.3 |
| 4 | Velocity filter | Close > EMA₅₀ | G2.3 |
| 5 | Intraday de-seasonalization | rolling 60-session `s_m`, 75 bars | G3.1 |
| 6 | Jump threshold | Z_jump < 2.15 | G3.2 |
| 7 | Overnight gap limit | ≤ 1.5 σ_daily | G3.3 |
| 8 | ADF stationarity | p < 0.05 | G4 |
| 9 | GARCH window | 60 | S1 |
| 10 | GARCH z-band | −2.20 ≤ Z ≤ −1.40 | S1 |
| 11 | OU training sample | pullback episodes, 250 sessions | S2 |
| 12 | Half-life band | 1.0 ≤ τ_½ ≤ 3.5 days | S2 |
| 13 | Volume exhaustion | V_ratio < 0.60 and Vol < SMA₂₀ | S3 |
| 14 | AVWAP anchor window | [t−40, t−5] primary, [t−30, t−5] fallback | S4 |
| 15 | AVWAP staleness | ≤ 0.04 | S4 |
| 16 | Confluence threshold | ≥ 3 of 4 | Part IV |
| 17 | Earnings blackout | > 10 sessions ahead, or within prior 3 | G1.4 |
| 18 | Circuit screen | no circuit hit in last 30 sessions | G1.3 |
| 19 | VIX ceiling | < 21.0, and < +10% intraday | G0 |
| 20 | Payoff ratio | **b = 1.60** (net) | 5.1 |
| 21 | Probability distribution | Student's t, ν = 4 or 5 (or ECDF) | 5.2 |
| 22 | Probability floor | p > 0.40 | 5.2 |
| 23 | Kelly fraction | 0.5 × full Kelly | 5.3 |
| 24 | Risk cap per trade | 1.5% of equity | 5.5 |
| 25 | ADV participation cap | 1.5% of 20-day ADV (shares) | 5.5 |
| 26 | Lot quantization | even multiples; ≥ 2 lots | 5.6 |
| 27 | Entry trigger | High_t × 1.0005 | 7.1 |
| 28 | Limit slippage buffer | +0.20% | 7.1 |
| 29 | Gap-cancel | > High_t +1.2% or +0.75 ATR₁₄ | 7.1 |
| 30 | Order validity | GFD; cancel 15:20 day t+1 | 7.1 |
| 31 | Stop buffer | 0.75 × ATR₁₄ | 7.2 |
| 32 | Max risk distance | R ≤ 1.25 × ATR₁₄ | 7.2 |
| 33 | Target 1 | Kalman mean or EMA₂₀ → sell 50% | 7.3 |
| 34 | Target 2 | Entry + 2.5R → sell remainder | 7.3 |
| 35 | Time stop | min(ceil(2.5 τ_½), 8) sessions | 7.3 |
| 36 | Sector cap | 1 position/sector, ≤ 25% book | Part VIII |
| 37 | Ranker | top 3–5 by composite score | Part VI |
| 38 | Validation | CPCV + DSR p ≤ 0.05 | Part IX |

---

# PART XIII — Conflict resolution register

Every place the source material contradicted itself, and what this spec uses:

| Topic | Earlier version | Later version | **This spec** | Reason |
|---|---|---|---|---|
| Kelly payoff b | 2.5 | 1.60 | **1.60** | Verified: 0.5(1.0R)+0.5(2.5R)=1.75R gross, −0.15R friction = 1.60R net |
| Probability model | Gaussian Φ | Student's t / ECDF | **Student's t ν=4–5** | Fat tails; also avoids the negative-Kelly short signal |
| p floor | none | ≥ 0.40 | **> 0.40** | Kelly goes negative below 1/2.60 = 0.3846 |
| Stop buffer | 0.25 × ATR₁₄ | 0.75 × ATR₁₄ | **0.75 × ATR₁₄** | 0.25 gets whipsawed >40% of the time |
| "1.25 × ATR" mentions | used as the stop itself | — | **max risk *distance* cap** | The structural buffer is 0.75 ATR; 1.25 ATR is the ceiling on R |
| Time stop | Day 8 | ceil(2.5 τ_½) | **min(ceil(2.5 τ_½), 8)** | Satisfies both; OU-scaled when available |
| OU fit window | rolling 30 days | pullback episodes, 250 days | **episodes** | Rolling window is regime-contaminated |
| z-band | −2.5 to −1.5 (rolling σ) | −2.20 to −1.40 (GARCH) | **GARCH band** | Removes homoskedasticity assumption |
| Half-life band | 1.5–5.0 | 1.0–3.5 | **1.0–3.5** | Matches the 1–8 day holding window |
| AVWAP anchor `t₀` | "last major swing low" | argmax(High) / Donchian break | **Donchian breakout + volume, pivot-low fallback** | Deterministic; no discretionary read |
| Filter structure | 8 hard AND gates | 3-of-4 scored confluence | **Gates G0–G5 binary; S1–S4 scored ≥3/4** | 8 ANDs yield ~1–2 trades/quarter |
| Sizing constraint order | Kelly then 1.5% cap | — | **min() of all three, as specified** | But see §5.7(a): in cash the cap never binds |

---

# PART XIV — Completeness audit

| Component | Status | Implementation |
|---|---|---|
| Mathematical mechanics | Complete | Hurst + Kalman + de-seasonalized BNS + GARCH + ADF-gated OU |
| Trade execution | Complete | GFD stop-limit, 0.20% buffer, gap-cancel, 24-hour expiry |
| Risk architecture | Complete | Half-Kelly, b = 1.60, p > 0.40, 1.5% hard capital stop |
| Portfolio bounds | Complete | 1 position/sector, ≤25% sector, VIX macro gate, heat cap |
| Microstructure guards | Complete | Intraday de-seasonalization, overnight gap, circuit screen, ADV cap, even-lot quantization |
| Binary-event risk | Complete | Earnings >10 sessions away or within prior 3 |
| Data integrity | Complete | Adjusted price+volume for AVWAP, raw volume for ADV, PIT universe |
| Backtest validity | Complete | CPCV + DSR p ≤ 0.05 |
| Regulatory margins | Complete | Sized on unencumbered settled cash; 50:50 collateral respected |

---

# PART XV — Honest limitations

1. **This is not a prediction.** It is a filter that isolates "trend pause, not trend death." It will be wrong often; the edge is in the asymmetry of the exits, not in the win rate.
2. **Jump detection has holes.** Overnight gaps are only partially covered by G3.3. Halts, illiquid names, and multi-day lock-downs defeat both tests.
3. **Short-window estimation error.** GARCH, Kalman Q/R, and OU parameters are all estimated on limited data and can be badly wrong in a new regime.
4. **Student's t is still parametric.** ν = 4 or 5 is a convention, not a law. Refit per symbol and monitor drift.
5. **Capacity is the real binding constraint.** §5.7 shows the even-lot rule and ADV cap will reject most signals on accounts under roughly ₹1 Cr, and the 1.5% risk cap is inert in the cash segment.
6. **Half-Kelly still loses in clusters.** It only scales *relative* size. A sector downgrade that hits four correlated positions simultaneously is a portfolio event, not four independent losses.
7. **Execution assumptions.** Fill at trigger+buffer, MOC availability, and STT treatment all need to be validated against your broker's actual behavior before the backtest's `b = 1.60` means anything.

Deploy in this order: paper-trade → smallest possible live size for 60+ trades → compare realized `b` and `p` against assumed → only then scale.

---

# PART XVI — Data sourcing: Upstox API coverage map

Assessed against published Upstox developer documentation (accessed 2026-09-13). Verdict: **Upstox covers roughly 80% of §2.1 natively; three inputs must come from outside, and two behaviours must be tested before you trust them.**

## 16.1 Covered natively

| §2.1 requirement | Upstox endpoint | Notes |
|---|---|---|
| Daily OHLCV, 250+ sessions | `GET /v3/historical-candle` (`days`) | Available from **Jan 2000**, max 1 decade/request |
| 5-minute intraday bars | `GET /v3/historical-candle` (`minutes`, interval 5) | Available from **Jan 2022**, max **1 month/request** for intervals 1–15 min |
| Seasonal factors `s_m` | derived from the above | 2 monthly slices per symbol, then cache |
| Nifty 50 benchmark (G0.1) | `NSE_INDEX|Nifty 50` | Confirmed as an `underlying_key` in the instrument schema |
| F&O lot sizes (5.6) | Instrument master `lot_size`, `minimum_lot`, `segment=NSE_FO` | Directly usable |
| Unencumbered settled cash (5.4) | **Get Fund and Margin V3** | Exposes exactly the split needed: `available_to_trade.cash_available_to_trade` vs `pledge_available_to_trade` vs `unavailable_to_trade.cash_unavailable_to_trade.unsettled_profit` |
| Margin requirement per order | `POST /v2/charges/margin` | Up to 20 instruments/request; validates the 50:50 collateral rule live |
| Sector / sub-industry tag (P1) | **Get Company Profile** (Fundamentals API, live 2026-05-11) | Returns sector classification; **Get Competitors** as a sub-industry proxy |
| Corporate actions (2.2.1) | **Get Corporate Actions** by ISIN | Dividends, bonus, splits, rights — with ex-dates, record dates, ratios |
| Order execution (7.1) | Place Order V3 | Stop-limit + validity, GFD supported |
| Exit ladder (7.3) | **GTT Order API** (place/modify/cancel/details) | Native bracket-style ladder for T1/T2 — a genuine bonus |
| Market status / session | `GET /v2/market/status` | `PRE_OPEN_START`, `PRE_OPEN_END`, `CLOSING_START` for the pipeline clock |

## 16.2 Must come from outside Upstox

| §2.1 requirement | Why Upstox can't supply it | Where to get it |
|---|---|---|
| **Earnings / board-meeting calendar** (G1.4) | No endpoint. The Fundamentals suite has statements and ratios, **not** scheduled event dates | NSE corporate announcements feed, BSE, or a vendor (e.g. an earnings-calendar API) |
| **Circuit-filter hit history** (G1.3) | No historical circuit-hit log | NSE publishes a daily circuit-filter list; scrape and archive it. A market quote gives *current* band levels only |
| **Point-in-time index constituents** (G1.5) | NSE publishes only the **current** snapshot; Upstox has no constituents endpoint at all | Reconstruct from NSE Indices press releases / archived monthly reports, or use a third-party PIT dataset |

The PIT gap is the most consequential: without it §Part IX (DSR/CPCV) is computed on a survivorship-biased universe and the 4–8% annualized inflation stands.

## 16.3 Two behaviours to verify before trusting

**1. Adjustment handling is the sharpest risk.** §2.2 requires **two** series — adjusted price+volume for AVWAP, and **raw** volume for ADV and `V_ratio`. Upstox states its historical data is *adjusted for stock splits* and does not document dividend adjustment. If volume is adjusted alongside price, the 1.5% ADV cap (§5.5) and the volume-exhaustion gate (S3) are silently wrong.

Test empirically: pull a symbol across a known split/bonus ex-date and check whether volume jumps by the split ratio. Then either (a) confirm volume is raw, or (b) use **Get Corporate Actions** to reconstruct the raw series yourself and build the dividend-adjusted series on top.

**2. Pre-open discovery price at 09:08.** The Gap-Cancel Rule (§7.1) reads the pre-open price at 09:08. Upstox exposes `PRE_OPEN_START/END` status and a CAS (closing auction) equilibrium feed, but I could not confirm from the docs that a *pre-open* indicative equilibrium price is retrievable at 09:08. Fallback if it is not: run the gap check on the first regular-session print at 09:15 and place at 09:15:01 — which loses the pre-open advantage but still blocks gap-inflated entries.

*(I attempted to fetch the instrument master to confirm the India VIX instrument key directly; the request returned HTTP 403 from this environment, so **India VIX availability via the API is unverified**. India VIX appears as an NSE index on Upstox's own site, and `NSE_INDEX` is a documented segment, so `NSE_INDEX|India VIX` is very likely resolvable — confirm it in the master file from an authenticated session before building G0.2 against it.)*

## 16.4 Rate-limit budget (500-symbol universe)

Documented limits: standard APIs **50/sec, 500/min, 2000/30 min**; order APIs **10/sec, 500/min, 2000/30 min**.

| Job | Requests | Fits? |
|---|---|---|
| Daily batch hot path (500 daily + 500 × 5-min) | 1,000 | ✅ ~2 min at 500/min, inside one 30-min window |
| Weekly cache refresh (corporate actions + sector) | +1,000 | ✅ only if run in a **separate** 30-min window |
| One-time 5-min backfill, Jan 2022 → Sep 2026 | 28,000 (56 monthly slices × 500) | ✅ but ~7 h throttled at 2000/30min |
| Seasonal-factor build `s_m` | 1,000 (2 slices × 500), cached after | ✅ |
| Order placement 09:15:01 | ≤ 5 | ✅ trivially |

The **2000/30 min** ceiling is the binding constraint, not per-second throughput. Build a token-bucket limiter around the 30-minute window and cache aggressively — daily candles and corporate actions should never be re-fetched in full.

## 16.5 Bottom line

Everything the algorithm needs to **compute** (Hurst, Kalman, de-seasonalized BNS, GARCH, ADF, OU, Student-t CDF, CPCV/DSR) is pure local numerics — no API required beyond raw OHLCV. Upstox covers that raw data plus funds, lot sizes, sectors, corporate actions, and execution.

The three genuine gaps are **earnings calendar**, **circuit-hit history**, and **point-in-time constituents** — all resolvable from NSE-side sources, none requiring a different broker. Add a data layer that merges Upstox with those three feeds, and the spec is buildable end-to-end.
