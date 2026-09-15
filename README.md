# STMR - Short-Term Equity Mean Reversion (NSE, Upstox, SQLite)

A complete, self-contained intraday trading system for the **Nifty-100 universe**
(90 names in `stock_universe.json`). It uses the **Upstox v2 API** for market
data (and, in real mode, for orders), keeps **all state in SQLite**, and ships
with a **backtest/replay engine that re-runs the exact same strategy code
against the stored bars**.

> **Default is PAPER trading.** Nothing places real orders unless you pass
> `--mode real --i-understand-real-risk` with a funded Upstox account.

---

## 1. The strategy in one minute

Long-only, intraday, "fade dips in an uptrend". On **5-minute bars** of each
name:

```
z = (close - SMA20) / rolling_std20          (the mean-reversion statistic)
```

| Stage | Rule |
|---|---|
| **Context** (daily, built pre-open, no lookahead) | daily close > SMA20 **and** SMA20 > SMA50 (uptrend bias); 20-day avg volume ≥ 50,000 (liquidity) |
| **Scan / Selection** (5m, every bar) | a **dip** = within the last 6 bars (30 min) `z ≤ -1.2` and RSI(14) touched ≤ 35 |
| **Entry** (on a bar close) | `z` back in `[-1.5, -0.4]` (still below the mean) **and** rising, **green bar** (close>open and close>prev close), RSI ≤ 55, no entries after 14:50 IST |
| **Entry quality filters** | still **below day-VWAP** (classic intraday MR) · dip **depth ≥ 0.5×ATR(14)** (real pullback, not drift) · ATR(14) ≥ 0.08% of price (volatility must cover costs) |
| **Risk** | stop = entry − 1.5×ATR(14) (clamped 0.30%–1.50%); qty = 0.5% equity risk, capped at 20% equity exposure and cash |
| **Target (desk money-maker)** | **the mean itself**: SMA20 at entry. On the first touch: sell **PARTIAL_PCT** (default 40–50%) to lock the reversion, move the stop to **breakeven**, and let the remainder run to **target-2 = entry + R_MULT × (entry − stop)** (default 1.5R). The runner is then a free trade. |
| **Exits** | `STOP` (tick or bar low, gap-through fills at open) · `PARTIAL` (slice sold at the mean; position continues) · `TARGET2` (runner closes at the second target) · `MEAN` (bar close z ≥ -0.10) · `TRAIL` (after crossing the mean, stop ratchets to close − 1.0×ATR) · `TIME` (no reversion after 8 bars / 40 min) · `EOD` (forced flat from 15:20 - intraday-only mandate) |
| **Desk guards** (entry gates) | no entries before **09:45** (auction noise) · skip **event-regime** names (5-min ATR > 1.5% of price) · entry bar must not close in the bottom half of its own range · **daily-loss kill-switch** (−1.5% vs day-start equity → no more entries today) · **max 12 entries/day** · **6-bar per-symbol cooldown after a stop-out** |

Up to **5 concurrent positions**, one per symbol. **Every parameter is
tunable** (see `config.py` and §7 Tuning).

## 2. Files

| File | Role |
|---|---|
| `main.py` | CLI entry: live loop (paper default), `--status`, `--once`, `--observe`, `--mode real` |
| `engine.py` | The live engine: session phases, sync, scan, entries, tick exits, EOD flatten, EOD report |
| `strategy.py` | **Pure** strategy logic (no I/O). Called by *both* live engine and backtest |
| `indicators.py` | Pure-Python SMA/std/z-score/RSI/ATR/VWAP (no numpy/pandas) |
| `data_fetch.py` | When/how data is fetched & persisted (the "never dirty the DB" contract) |
| `upstox_client.py` | Upstox v2 REST + OAuth2/2FA + fm_token + instrument master. Header documents every API limitation |
| `execution.py` | `PaperBroker` (default) and `UpstoxBroker` (real), fee model, slippage |
| `db.py` | SQLite schema + all persistence helpers (WAL mode, idempotent upserts) |
| `backtest.py` | **Replay engine** over stored bars + CAGR/Sharpe/PF/DD reports + `--compare` live-vs-replay validation |
| `tune.py` | **Walk-forward parameter optimizer v2** (anchored folds, selectable objective, constraints, local refinement, overfit z-score, desk memo) → `data/best_params.json` |
| `data_import.py` | Import REAL 5-min/daily OHLCV CSV exports (Upstox can't serve past intraday bars) |
| `demo_data.py` | Deterministic synthetic data (tagged `source='demo'`) for offline validation |
| `test_engine_sim.py` | Full live-engine simulation on a mock API: whole session + **restart safety** + **real-mode order/stop consistency** (stale overnight close, protective SL-M lifecycle, equity proxy) |
| `verify_all.py` | Offline self-check (18 tests incl. partial accounting, desk guards, tuner folds, engine partial-exit/SL ratchet and the paper+real simulation) - `python verify_all.py` |
| `patch.py` | DB maintenance: status / integrity / migrate / purge-demo / reset |
| `config.py` | Every tunable (strategy, risk, fees, session times, API) |
| `stock_universe.json` | The 90 Nifty-100 symbols |
| `mkttime.py` | IST session/ bar-time helpers |
| `console.py` | Human-readable output helpers |

## 3. Install (Windows or Linux, Python 3.9+)

```bat
pip install -r requirements.txt     :: requests + tzdata (that's all)
```

## 4. Upstox setup (one time)

1. Create an API key at the Upstox developer portal (you need a funded
   demat/broker account). Note the **client_id** and pick a redirect URI
   (e.g. `http://127.0.0.1:9876/upstox`).
2. Set the environment variables (PowerShell):
   ```powershell
   $env:UPSTOX_CLIENT_ID = "xxxxxxxxxxxx"
   $env:UPSTOX_REDIRECT_URI = "http://127.0.0.1:9876/upstox"
   ```
   (Linux/macOS: `export UPSTOX_CLIENT_ID=...`)
3. Start the engine once. It prints an authorize URL, you log in, paste the
   redirect URL and your 6-digit authenticator (TOTP) code. The tokens are
   cached in `data/upstox_token.json` (gitignored) and refreshed silently
   every day afterwards.

> First login is semi-manual by design (Upstox requires TOTP) - see
> `upstox_client.py` header, limitation #4.

## 5. Run

```bat
python main.py                     REM live loop, PAPER (default) - run any time
python main.py --status            REM read-only snapshot (no API, no writes)
python main.py --once              REM single pass (cron-friendly)
python main.py --observe           REM scan + log everything, place no orders
python main.py --mode real --i-understand-real-risk   REM LIVE orders
```

* **Run at any time** - before/after market the engine captures **no data and
  writes no rows**; mid-day it syncs today's bars once and resumes seamlessly.
* **Console output is self-explaining**: every dip, entry and exit prints the
  data behind it (z path, RSI, ATR, stop/target, risk taken).

### Backtest / replay

```bat
python backtest.py                          REM everything in the DB
python backtest.py --start 2026-09-01 --end 2026-09-15
python backtest.py --symbols RELIANCE,TCS
python backtest.py --compare                REM ALSO: live paper trades vs replay
python backtest.py --seed-demo 10           REM 10 days of synthetic data (offline)
python backtest.py --seed-demo 10 --db data/demo.db   REM keep demo data isolated
```

The replay uses the **same `strategy.py`** on the **same stored bars**, with
conservative intra-bar exit emulation (stop before target in the same bar;
gap-throughs fill at the open). Reports: `reports/backtest_*.md` + `_trades.csv`
(net P&L, **CAGR**, win rate, profit factor, max DD, Sharpe, per-symbol,
per-reason, daily P&L). `--compare` is the engine-validation check: it lines
up every live paper trade in the DB against the replay (same symbol+day, entry
within 0.5%).

### Importing REAL history (needed for serious tuning)

Upstox cannot serve intraday bars before today (limitation #1). To backtest or
tune on REAL Nifty-100 data, load any broker/vendor/screener CSV export once:

```bat
python main.py --import-csv nifty_5m.csv --kind 5m
python main.py --import-csv RELIANCE_5m.csv --kind 5m --symbol RELIANCE
python main.py --import-csv dailies.csv --kind daily --symbol RELIANCE
```

Auto-detected columns (`symbol/timestamp/open/high/low/close/volume` and
common aliases; IST or epoch timestamps; idempotent re-import; tagged with
`--source`). The engine also accumulates real bars day by day as you run it.

### Tuning / optimization for profitability (`tune.py` v2)

```bat
python tune.py                               REM whole DB, 1 fold (classic train/test)
python tune.py --folds 3 --objective calmar  REM desk-standard: 3 anchored folds
python tune.py --folds 3 --objective sharpe --min-wr 55 --max-hold-bars 8
python tune.py --start 2026-08-01 --end 2026-09-15 --n-samples 300
python tune.py --symbols RELIANCE,TCS,HDFCBANK --n-samples 400
python tune.py --refine 20                   REM + local refinement stage
python tune.py --no-write                    REM report only
```

How it works (v2, "desk edition"):
* **Anchored walk-forward folds** (`--folds N`): the train window always
  starts at day 1 and **expands** fold by fold; each fold is validated on the
  next slice of days the search never saw. The last fold covers the most
  recent sessions - the regime the strategy is most likely to face. A combo
  that only works on one fold is regime-luck, not edge.
* **Selectable objective** (`--objective pnl|cagr|calmar|sharpe`): P&L alone
  rewards risk-on behaviour; desks rank on risk-adjusted figures. Calmar =
  annualized CAGR% / max DD%; sharpe = daily (annualized) sharpe of the
  equity curve.
* **Constraints** (`--min-trades --max-dd --min-wr --max-hold-bars`):
  eligibility filters applied to **every** fold's test slice (min trades is
  scaled to the slice length). A combo is only eligible if it passes on all
  folds.
* **Local refinement** (`--refine N`): after the coarse pass, the top-3
  combos are perturbed one grid step at a time, the neighbourhood is scored
  on the largest train window, and the best N perturbations join the fold
  evaluation - so you don't stop one grid step from the optimum.
* **Overfit z-score**: the winner's train objective is expressed in
  standard deviations from the distribution of ALL trials. High z with a
  weak out-of-sample fold is the classic overfit signature - the desk memo
  prints both numbers.
* **Desk memo** (`reports/tune_*.md`): short markdown record of the run -
  window, objective, constraints, per-fold table, winner rationale,
  caveats.
* **Search**: random coarse sampling over ~30 parameters (z-windows, dip
  thresholds, entry band, exit rules, stop/trail multiples, time stop, VWAP /
  depth / ATR filters, position count, risk per trade **and the whole desk
  layer**: partial %, runner R-multiple, cutoff, ATR cap, bar-strength,
  loss limit, trade cap, cooldown) over a 130B+ grid.
* **Application**: the winner is written to `data/best_params.json`; the
  **engine and backtest auto-load it** (their banners show "TUNED (n
  overrides)"). Delete the file to return to `config.py` defaults.

Honesty box: a parameter set can only be trusted on data you haven't tuned on.
Tuning on `--seed-demo` synthetic data validates the machinery (and the
demo numbers are *not* real market results). For real Nifty-100 tuning:
accumulate/import several weeks of 5-min bars, run
`python tune.py --folds 3 --objective calmar --refine 20`, then keep
paper-trading the winner and let `--compare` verify live-vs-replay agreement
before you consider real mode. On windows of ≤ ~20 sessions the per-fold
slices are small: prefer combos that are positive on **every** fold.

### The desk risk layer (v2)

The entry/exit machinery above is wrapped in the same guard set a small
proprietary desk would run intraday:

| Guard | What it does | Param |
|---|---|---|
| **Partial profit-taking** | sells `PARTIAL_PCT` at target-1 (the mean), moves stop to breakeven, runs the rest to `R_MULT_TARGET2` × R (reason `TARGET2` on the final close). The backtest and live engine share the same `plan_target_exit()` code, so numbers transfer 1:1. | `PARTIAL_PCT`, `BE_AFTER_PARTIAL`, `R_MULT_TARGET2` |
| **Opening-noise cutoff** | no new entries before `EARLY_ENTRY_CUTOFF` (default 09:45) - the first bars after the opening auction are pure noise. | `EARLY_ENTRY_CUTOFF` |
| **Event-regime ATR cap** | skips names whose 5-min ATR is a runaway fraction of price (news, orders, results) - MR stops working in event regimes. | `MAX_ATR_PCT` |
| **Bar-strength filter** | the entry bar must close in the top part of its own range (a reclaim that closes near its low is a trap, not a reversal). | `MIN_BAR_CLOSE_POS` |
| **Daily-loss kill-switch** | once equity ≤ day-start × (1 − `DAILY_LOSS_LIMIT_PCT`/100), **no more entries that day** (exits/trailing still run). Survives restarts via `meta`. | `DAILY_LOSS_LIMIT_PCT` |
| **Max trades per day** | hard cap on daily entries (overtrade / cost guard). | `MAX_TRADES_PER_DAY` |
| **Post-stop cooldown** | after a `STOP`-out, the same symbol is not re-entered for `STOP_COOLDOWN_BARS` bars (fighting a name that just broke the level is how blow-ups start). | `STOP_COOLDOWN_BARS` |

All guards are live in the backtest too (same code path), so the backtest
numbers already include their cost/benefit. Every guard prints its explicit
reason when it blocks an entry (`GUARD ...` lines, `signals` audit trail).

### Offline validation (no API, no network)

```bat
python verify_all.py          REM 18 tests: indicators, fees, DB, strategy,
                              REM broker, partial accounting, time, pipeline,
                              REM precompute==raw, time-stop, tuner smoke,
                              REM desk guards, engine partial-exit + real
                              REM broker SL ratchet, tuner folds, objectives,
                              REM CSV import, full-session engine simulation
                              REM (paper + real) w/ restart test
python test_engine_sim.py     REM just the full-session simulation (paper + real)
```

### Maintenance

```bat
python patch.py status         REM row counts, open positions, schema version
python patch.py integrity      REM PRAGMA checks
python patch.py purge-demo     REM delete only source='demo' rows
python patch.py reset --yes    REM wipe paper state + captured data (keeps keys)
```

## 6. The database (`data/trading.db`, SQLite/WAL)

| Table | What | Notes |
|---|---|---|
| `candles_5m` | 5-min OHLCV | **completed bars only**, immutable (`ON CONFLICT DO NOTHING`) |
| `live_5m` | the bar in progress | transient, dropped when the bar finalises |
| `daily_candles` | daily OHLCV | today's row written only after 15:35 |
| `stock_context` | SMA20/SMA50/ATR14/avgVol/trend per day | built pre-open from strictly earlier days |
| `trades` | every position (open + closed) with fees, P&L, reasons, **partials** (`qty_remaining`, `realized_pnl`, `partial_count`) | **restart-safe positions**, schema v2 (auto-migrated) |
| `processed_bars` | (symbol, bar, kind) evaluated once | prevents double trading after restart |
| `signals` | DIP / ENTRY / EXIT / TRAIL audit trail | |
| `equity_curve` | mark-to-market equity per pass | |
| `instruments` | symbol → Upstox instrument_key cache | |
| `run_events` | engine log | |
| `meta` | cash, start equity, schema version, day flags | |

### Why the DB is never "dirty"
* only **completed** 5-min bars reach `candles_5m` (bar end ≤ now − 20 s);
* the in-progress bar lives in transient `live_5m`;
* today's **daily** bar is persisted only in POST_CLOSE (after 15:35);
* outside the session window the system makes **no API data calls and no
  writes**;
* every write is an idempotent upsert keyed by (symbol, time) - re-running or
  restarting can never duplicate or mutate history.

### Why a restart loses nothing
Cash, open positions (with stops/targets), processed-bar markers, instrument
keys, context and the equity curve are all in SQLite. On start-up the engine
re-hydrates everything; a bar already evaluated is never evaluated again
(`processed_bars` guard); a position that somehow survived from a previous day
is closed at its last stored price and loudly logged (`STALE_RESTART`).
`test_engine_sim.py` asserts all of this on every run.

## 7. Upstox API - where it is insufficient (and what we skip)

The full list with reasoning is in the header of `upstox_client.py`. Summary:

| # | Limitation | What this system does |
|---|---|---|
| 1 | **No intraday history before today** (5m bars = current day only) | Every completed bar is persisted to SQLite as it happens; multi-day backtests replay accumulated data (or `--seed-demo` synthetic data). No history → backtest skips with a message, never fabricates |
| 2 | **No streaming over REST** (live data needs WebSocket) | REST polling: quotes for positions + dip watchlist every 8 s; full bars at each boundary. Exit latency bounded by poll cadence |
| 3 | **No GTT / bracket orders** in the public API | Engine manages stops/targets/trailing (poll + market exit). REAL mode places a broker-side protective **SL-M** per position; it is kept in sync by the engine - **replaced after every partial** (breakeven, reduced qty) and **ratcheted up on trailing** (never relaxed). A crash therefore leaves a valid broker-side stop for the day. At market open the engine also **reconciles open orders** and loudly flags any it doesn't own (no auto-cancel - another strategy may share the account) |
| 4 | **OAuth needs TOTP at first login** | Semi-manual one-time login; tokens cached & refreshed silently; if both expire, live data is **skipped** for the day with a message (no trading on stale data) |
| 5 | **No symbol → instrument_key lookup** | Keys from NSE instrument master CSV (cached 7 days), fallback `data/instrument_keys.json`; missing symbols are **skipped** and logged |
| 6 | Rate limits | Calls throttled to 1 per 0.25 s with backoff on 429/5xx |
| 7 | `fm_token` needed by data endpoints | Minted & cached 5 h in `data/upstox_fm_token.json` |
| 8 | Candles are as-traded (no split/bonus adjustment) | Documented |
| 9 | Order field names follow v2 docs as of 2025-26 | Verify if Upstox ships changes |

**Paper mode uses the Upstox API for market data only; order placement is
skipped by design (simulated fills with realistic MIS fees + 5 bps slippage).**

## 8. Paper-trade fees (MIS approximations, see `config.py`)

STT 0.025% both sides · NSE txn 0.019% · SEBI 0.0001% · flat brokerage ₹20
(capped 0.03%) · GST 18% on (brokerage+exchange+SEBI) · stamp 0.015% on buys ·
5 bps slippage/side. Good for validating the strategy - not for tax returns.

## 9. Production readiness - what is hardened, and what remains

**Hardened in this codebase (and tested offline, no network needed):**
* **Crash/restart safety** - all state in SQLite (WAL); processed-bar guard;
  stored-but-unprocessed bars are replayed on the next pass (a crash between
  storing a bar and evaluating it loses nothing); every engine pass ends
  with a committed connection (no dangling write transactions).
* **Real-mode integrity** - real mode actually routes orders through
  `UpstoxBroker` (it can no longer silently paper-trade); stale overnight
  positions in real mode are **flattened at market open** with an ERROR
  event (the intraday-only mandate is absolute); protective **SL-M orders
  follow partials and trailing** (breakeven ratchet, reduced qty, never
  relaxed) so a crash still leaves a valid broker-side stop; startup
  **order reconciliation** flags any broker-side open order this system
  doesn't own (logged, never auto-cancelled).
* **Real-mode P&L basis** - equity/cash for sizing and the **daily-loss
  kill-switch** use a real proxy (sizing base + realized P&L net of fees
  - open position cost basis, marked to last quote), not a constant.
* **No look-ahead, ever** - stored bars beyond the current boundary are
  excluded from every decision path (defensive against pre-seeded/stale DBs).
* **Full offline validation** - `verify_all.py` (18 tests) and
  `test_engine_sim.py` run a complete simulated session in **both paper and
  real mode** on a mock Upstox API, including restart, EOD flatten and the
  real-mode order/stop lifecycle.

**Inherent API-level limits (cannot be fixed in code - plan around them):**
* **Polling latency** - no WebSocket over REST: stop/target reactions are
  bounded by `POLL_FAST_SEC` (8 s). The broker-side SL-M is your backstop.
* **No funds endpoint** - real cash/equity is a proxy (documented above);
  keep `STMR_REAL_CAPITAL` at or below what you actually want at risk.
* **Semi-manual OAuth** - first login needs your TOTP; if both tokens lapse
  the engine skips live data for the day (no trading on stale data).
* **Crash mid-order** - if the process dies between order placement and the
  DB write, the startup reconciliation will flag the orphan; a human
  resolves it (deliberate - never auto-cancel).

**Ops checklist before running it seriously:**
1. NTP-synced clock (session logic is wall-clock IST).
2. Run under a process supervisor (systemd unit, or cron with `--once`
   every minute 09:10-15:30 IST); `--status` for a read-only health check.
3. Watch `run_events` (ERROR rows) and the daily EOD report; the desk
   guards print `GUARD`/`DESK` lines when they act.
4. Run in **paper mode for 1-2 weeks of real bars** and use `--compare`
   to verify live-vs-replay agreement before real mode.
5. Start real mode with a small `STMR_REAL_CAPITAL` and raise it only as
   the paper track record justifies.

## 10. Disclaimer

This is software for research and paper trading. Intraday mean reversion loses
money on strong trend days; the backtest numbers on demo data validate the
*mechanics*, not future profitability. Real mode can lose real money quickly.
You are responsible for broker T&Cs, suitability and taxes.
