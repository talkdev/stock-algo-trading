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
| **Target** | **the mean itself**: SMA20 at entry. Reversion *is* the profit. |
| **Exits** | `STOP` (tick or bar low, gap-through fills at open) · `TARGET` (price ≥ mean) · `MEAN` (bar close z ≥ -0.10) · `TRAIL` (after crossing the mean, stop ratchets to close − 1.0×ATR) · `TIME` (no reversion after 8 bars / 40 min) · `EOD` (forced flat from 15:20 - intraday-only mandate) |

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
| `tune.py` | **Walk-forward parameter optimizer** (train/test split, overfit gap table) → `data/best_params.json` |
| `data_import.py` | Import REAL 5-min/daily OHLCV CSV exports (Upstox can't serve past intraday bars) |
| `demo_data.py` | Deterministic synthetic data (tagged `source='demo'`) for offline validation |
| `test_engine_sim.py` | Full live-engine simulation on a mock API: whole session + **restart safety** |
| `verify_all.py` | Offline self-check (9 tests incl. the simulation) - `python verify_all.py` |
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

### Tuning / optimization for profitability (`tune.py`)

```bat
python tune.py                              REM whole DB, default grid, 150 samples
python tune.py --start 2026-08-01 --end 2026-09-15 --n-samples 300
python tune.py --symbols RELIANCE,TCS,HDFCBANK --n-samples 400
python tune.py --min-trades 20 --max-dd 2.5 --top-k 20
python tune.py --no-write                   REM report only
```

How it works:
* **Walk-forward**: the window is split (default 70/30). Candidates are
  filtered on the **train** part by net P&L (with min-trades / max-DD
  eligibility), then only the top-k are scored on the **out-of-sample test**
  part. The result table shows train vs test side by side (P&L, **CAGR**, PF,
  max DD, win%) so you can see the overfit gap.
* **Search**: random coarse sampling over ~18 parameters (z-windows, dip
  thresholds, entry band, exit rules, stop/trail multiples, time stop, VWAP /
  depth / ATR filters, position count, risk per trade) over a 15M+ grid —
  a 100-sample run takes ~2 minutes on 15 days x 90 symbols.
* **Application**: the winner is written to `data/best_params.json`; the
  **engine and backtest auto-load it** (their banners show "TUNED (n
  overrides)"). Delete the file to return to `config.py` defaults.

Honesty box: a parameter set can only be trusted on data you haven't tuned on.
Tuning on `--seed-demo` synthetic data validates the machinery (and the
demo numbers are *not* real market results). For real Nifty-100 tuning:
accumulate/import several weeks of 5-min bars, run `tune.py`, then keep
paper-trading the winner and let `--compare` verify live-vs-replay agreement
before you consider real mode.

### Offline validation (no API, no network)

```bat
python verify_all.py          REM 13 tests: indicators, fees, DB, strategy,
                              REM broker, time, pipeline, precompute==raw,
                              REM time-stop, tuner smoke, CSV import,
                              REM full-session engine simulation w/ restart test
python test_engine_sim.py     REM just the full-session simulation w/ restart test
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
| `trades` | every position (open + closed) with fees, P&L, reasons | **restart-safe positions** |
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
| 3 | **No GTT / bracket orders** in the public API | Engine manages stops/targets/trailing (poll + market exit); real mode adds a protective SL-M for the initial stop (broker-side, expires EOD) |
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

## 9. Disclaimer

This is software for research and paper trading. Intraday mean reversion loses
money on strong trend days; the backtest numbers on demo data validate the
*mechanics*, not future profitability. Real mode can lose real money quickly.
You are responsible for broker T&Cs, suitability and taxes.
