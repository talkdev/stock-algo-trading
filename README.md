# JF-OU / NSE 2026

**Jump-Filtered Ornstein–Uhlenbeck Trend-Conditioned Mean Reversion**, implemented end
to end for the NSE NIFTY 100 universe.

Specification: [`JF_OU_NSE_2026.md`](JF_OU_NSE_2026.md) (Parts 0–XXIV). This package is
the executable form of that document. Every parameter in the code carries a comment
pointing at the part of the spec it comes from.

---

## Quick start

```bash
pip install -r requirements.txt

python main.py load-universe      # resolve + validate the 98 symbols
python main.py scan               # run the gate cascade, arm orders (paper)
python main.py manage             # walk the exit ladder on open positions
python main.py report             # human-readable justification for the last run
python main.py status             # DB state, capture log, open book
python main.py backtest --from 2025-06-01 --to 2026-09-11
python run_tests.py               # 3 suites, all must pass
```

**Paper mode is the default.** Nothing reaches a broker unless you pass `--live` *and*
export `JFOU_UPSTOX_TOKEN` *and* set `paper_trade = False` in `jfou/config.py`. All
three are required; any one of them alone keeps you in paper mode.

Without a token the program runs against `MockClient`, a deterministic synthetic market
with the same interface as the live client. That is what the test suite uses, and it is
what makes the whole pipeline runnable with no credentials.

---

## Where the Upstox API is not sufficient

The authoritative list is the header block of `main.py`. Summary, worst first:

| # | Gap | Consequence | What the code does |
|---|---|---|---|
| 1 | **5-minute depth starts ~Jan-2022** | Part XVI's regime-stratified CPCV wants the March-2020 crash. Unavailable intraday. | `historical()` **raises** rather than returning an empty frame, naming the depth floor. A reference-data vendor is needed for 2018–2021. |
| 2 | **2000 requests / 30 minutes** | ~7.2 h cold backfill for 100 names; the 30-min cap dominates, not the per-second rate. | Rate limiter tracks the 30-min window and **persists timestamps in `kv`**, so a restart cannot buy a fresh budget and get the account throttled. |
| 3 | **No point-in-time constituency** | Backtests on today's list are survivorship-biased. | Universe file is stamped `as_of` and labelled non-PIT; old rows keep `is_current = 0`. |
| 4 | **No corporate-actions endpoint** | Gate G1.4 needs ex-dates. | `corporate_events` is fed externally; when empty G1.4 reports "unknown" and passes conservatively. |
| 5 | **No sector classification** | Part XIV caps and the Part 4.3 overlay need sectors. | Approximate tags in the universe file; unknown sector fails closed. |
| 6 | **India VIX availability unverified** | G0 tiering needs it. | Failure marks the tier `DEGRADED`, treated as **Amber, never Green**. |
| 7 | **No verified 09:08 pre-open snapshot** | GAP-CANCEL needs it. | Phase still resolves; missing print is logged, not guessed. |
| 8 | **Instrument master returned HTTP 403** | Cannot pre-verify symbol→key mapping. | **No ISIN is hard-coded anywhere.** Resolved at runtime, cached in `instruments`. |
| 9 | **SEBI algo framework, 1 Apr 2026** | Algo-ID, static IP, Indian hosting; bracket orders unverified. | Stop-loss built as an **independent order**, not a bracket leg. |

Daily OHLCV from Jan-2000 is sufficient for every daily-resolution gate. Hurst, Kalman,
BNS, GARCH, ADF, OU and Student-t are all local numerics and need nothing from the
broker.

---

## Design guarantees

**Restart safety.** All state is in SQLite. Positions, orders, fills and every
transition live in the database; `position_events` is append-only and never `UPDATE`d.
On startup the engine reconstructs pending and open positions from `positions` and
their armed orders from `orders`. Nothing is held in memory across a phase boundary.

**The database is not dirtied by re-runs.** Every fetch is gated on what is already
stored:

- daily bars — fetch only `(last_stored + 1 session) .. today`; skip entirely if current
- 5-minute bars — fetched **only for names that reach gate G3**, and only for the dip
  window. This is the expensive feed; fetching it for all 100 names on every run burns
  the 2000-request budget for nothing.
- every attempt is logged in `data_capture_log` with its range and status, so a repeat
  of an identical range is provably a no-op and a failed fetch is visible

Writes are idempotent (natural primary keys + `ON CONFLICT DO UPDATE`). Verified by
`tests/test_pipeline.py`: a second scan makes **0 API calls, writes 0 rows, skips 103
fetches**.

**The backtest uses the live code.** `backtest.py` calls the same gate, confluence,
sizing and ladder functions as the engine. A backtest with its own copy of the logic
drifts from production within a week and then tells you nothing.

---

## Layout

```
main.py                  CLI + the header block on Upstox's gaps
jfou/
  config.py              ~110 parameters, each annotated with its spec section
  db.py                  SQLite schema, WAL, idempotent upserts, capture log
  clock.py               IST session clock and phase resolution
  console.py             human-readable output primitives
  indicators.py          pure numerics — no I/O, so the backtest can replay through it
  dataclient.py          UpstoxClient + MockClient, one interface
  gates.py               G0–G4 and confluence S1–S4
  sizing.py              Part XI: Kelly on margin, four constraints, even-lot rule
  execution.py           order construction, entry, the five-rule exit ladder
  engine.py              phase orchestration and capture-gated ingestion
  backtest.py            database replay, no network
tests/
  test_indicators.py     numerical calibration against known ground truth
  test_lifecycle.py      arm -> fill -> exit -> report -> restart
  test_pipeline.py       end-to-end scan, capture log, restart safety
universe_nifty100.json   98 members, per-entry provenance, non-PIT
```

---

## Deviations from the spec — read this before tuning anything

Three components were changed because the specified form was measurably wrong. Each is
documented in Part XXIV of the spec and pinned by a test.

**1. Hurst estimator (gate G2.1).** Specified: R/S over 100 sessions, gate at H > 0.60.
Measured on 300 pure random walks, that estimator returns H > 0.60 **54.3 % of the
time** — the gate passes noise more often than it rejects it. Replaced with
variance-scaling (`Var(r_k) ~ k^2H`, lags ≤ 10 % of window), whose null is 0.466 with a
4.7 % false-positive rate. Discrimination: momentum 0.618 > random walk 0.470 >
mean-reverting 0.142.

**2. OU half-life sample (gate S2).** Specified: fit AR(1) on stacked pullback episodes.
Every episode-selection rule that conditions on the path biases λ negative:

| selection rule | true λ = −0.20 | true λ = −0.30 |
|---|---|---|
| **full spread (used)** | **−0.1995** | **−0.2988** |
| fixed 10-bar window | −0.2512 | −0.3578 |
| x < 0 only | −0.3863 | −0.5286 |
| full recovery path | −0.4459 | −0.6081 |

The x < 0 rule halves the apparent half-life (3.11 d → 1.41 d). Positions are sized off
τ½, so that bias converts directly into overstated edge. The fit uses the full spread;
the spec's regime-contamination concern is handled by a validity guard (`ou_context()`
reports episode count) instead.

**3. Z_GARCH units (confluence S1).** The spec writes `Z = (P_t − μ_t)/σ_t`, but σ_t
comes from a GARCH fit on **log returns** — it is in return units, not rupees. Dividing
a price difference by a return sd inflated Z by roughly 1/σ and made it scale with the
price level: a ₹1000 stock scored ~60× deeper than a ₹100 stock at the same percentage
discount. Now computed in log space.

---

## Bugs found and fixed during the build

These were found by running the code, not by reading it. They are listed because each
one would have produced plausible-looking output that was wrong.

- **EMA warm-up NaNs** propagated into `lstsq` and silently voided gate S2 for every
  name — no error, just an empty candidate list.
- **Universe query omitted the join** to `instruments`, so `instrument_key` was absent
  for all 98 rows and the scan loop skipped every name silently.
- **`datetime.date` passed to `historical()`**, which slices with `[:10]` — a `date` has
  no `__getitem__`.
- **Session-boundary returns spliced into the BNS series.** The overnight gap is ~10×
  the median intraday bar, so the jump statistic reported a Poisson jump on every name,
  every day. Z = 11.9 flat vs Z = 0.57 intra-session, with no jump present.
- **`seasonal_factors` slot 1** was computed against the previous session's close, so
  the overnight gap was baked into the seasonal profile (slot 1 = 9.5× slot 2), making
  de-seasonalisation actively harmful.
- **Tiling a 75-slot profile** across a multi-session window misaligns every session
  after the first. Z = 3.72 tiled vs 0.73 correctly aligned.
- **`look-ahead in ensure_index`/`ensure_vix`** — both pulled full history without
  truncating to `as_of`, so scanning a past date evaluated today's index.
- **Exit ladder rule order** — a bar spanning both targets fired Target 1 and scaled out
  50 % instead of recognising 2.5R.
- **`hash()` on strings** is salted per process, which would have regenerated the mock
  market on every restart and destroyed replay reproducibility. Now `zlib.crc32`.

---

## Known limitations

- **The universe is not point-in-time.** Every backtest run on it is survivorship-biased.
  This is structural, not fixable from Upstox.
- **NSE and the Upstox instrument master both return HTTP 403** from this environment,
  so the 98-symbol list could not be validated end to end. 51 symbols come from a source
  that published them explicitly; 47 were mapped from company names and are marked
  `src: mapped_2026-06`. Run `load-universe` against the live master before trusting a
  scan — it prints an explicit table of anything that fails to resolve, and unresolved
  symbols are dropped, never invented.
- **Live order placement is unverified.** The live path is written against documented
  Upstox v2 endpoints but has not been exercised against the real API from here.
- **No live scan has produced a trade** in this environment. G2 rejects 93 of 98 names
  in the mock's −13.7 %-shaped tape, which is the gate doing its job, but it means the
  arm→fill→exit path is verified by `tests/test_lifecycle.py` rather than by an organic
  signal.
