"""
demo_data.py - synthetic but realistic market data for OFFLINE testing.

Purpose: the Upstox API only returns TODAY's intraday bars (see
upstox_client.py limitation #1), so multi-day backtests need history. Before
the live system has accumulated a few days of real bars in the DB, this
generator lets you verify the ENTIRE pipeline (strategy -> execution ->
reporting) offline.

Rules:
  * All rows are tagged source='demo' and can be removed with
        python patch.py purge-demo
  * Deterministic for a given (seed, days, end_date, symbols) - re-running
    produces identical data.
  * Data shape: daily random walk with up/flat/down regimes (so the daily
    trend filter has a mix of pass/fail), intraday mean-reverting prices with
    ~1/3 of days containing a 0.8-1.6% dip that recovers (the pattern the
    strategy hunts), U-shaped volume.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
from indicators import Bar
from mkttime import bar_times, business_days_before, today_str


def _intraday_bars(rng: random.Random, d: date, open_price: float, drift: float,
                   dip_prob: float, base_vol: float) -> list[Bar]:
    """75 five-minute bars for one day (IST 09:15-15:30)."""
    times = bar_times(d.strftime("%Y-%m-%d"))
    n = len(times)
    mean = open_price * (1 + drift * 15)  # slight intraday drift
    p = open_price

    # pre-plan an optional dip: 2-3 bars down, then recovery over ~10 bars
    dip = None
    if rng.random() < dip_prob:
        k = rng.randint(15, 50)
        depth = rng.uniform(0.008, 0.016)
        dip = {"k": k, "depth": depth}

    extras: dict[int, float] = {}
    if dip:
        k, depth = dip["k"], dip["depth"]
        down_bars = min(3, n - k)
        for i in range(down_bars):
            extras[k + i] = extras.get(k + i, 0.0) - depth / (down_bars * 1.4)
        rec_total = depth * 1.15
        rec_bars = 10
        for i in range(rec_bars):
            j = k + down_bars + i
            if j < n:
                extras[j] = extras.get(j, 0.0) + rec_total / rec_bars

    bars: list[Bar] = []
    for i, t in enumerate(times):
        r = 0.05 * (mean - p) / p + rng.gauss(0, 0.0016) + extras.get(i, 0.0)
        o = p if i == 0 else bars[-1].c
        c = max(1.0, o * (1 + r))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0008)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.0008)))
        shape = 1.55 - 0.95 * math.sin(math.pi * i / (n - 1))
        v = int(base_vol * shape * rng.lognormvariate(0, 0.35))
        bars.append(Bar(t.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, max(1, v)))
        p = c
    return bars


def seed_demo(conn, days: int = 10, symbols: list[str] | None = None,
              seed: int = 42, end_date: str | None = None,
              quiet: bool = True) -> tuple[str, str, int]:
    """
    Generate `days` business days of demo data (ending at end_date, default
    today) for `symbols` (default: the full universe). Returns
    (first_day, last_day, n_symbols).
    """
    rng = random.Random(seed)
    symbols = symbols or config.load_universe()
    end = (date.fromisoformat(end_date) if end_date
           else datetime.now(ZoneInfo(config.IST_TZ)).date())
    day_list: list[date] = []
    d = end
    while len(day_list) < days:
        if d.weekday() < 5:
            day_list.append(d)
        d -= timedelta(days=1)
    day_list.reverse()
    first_day, last_day = day_list[0].strftime("%Y-%m-%d"), day_list[-1].strftime("%Y-%m-%d")

    for s in symbols:
        trend = rng.choices(["up", "flat", "down"], weights=[5, 3, 2])[0]
        drift = {"up": 0.0009, "flat": 0.0, "down": -0.0006}[trend]
        base_price = round(rng.uniform(120, 3200), 2)
        base_vol = rng.uniform(1.0e5, 3.0e6)

        # ---- 130 business days of daily history (context for the filters)
        hist = business_days_before(day_list[0], 130)
        price = base_price * rng.uniform(0.90, 1.10)
        daily_rows: list[tuple] = []
        for d in hist:
            ret = drift + rng.gauss(0, 0.011)
            o = price * (1 + rng.gauss(0, 0.002))
            c = o * (1 + ret)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
            v = int(rng.uniform(1.0e5, 2.0e6))
            daily_rows.append((s, d.strftime("%Y-%m-%d"), o, h, l, c, v))
            price = c

        # ---- window days: intraday 5m bars + daily aggregate
        prev_close = price
        daily_rows_window: list[tuple] = []
        for d in day_list:
            open_ = prev_close * (1 + rng.gauss(0, 0.0025))
            bars = _intraday_bars(rng, d, open_, drift, 0.32, base_vol)
            for b in bars:
                db.upsert_candle(conn, s, b.t, b.o, b.h, b.l, b.c, b.v, "demo")
            o = bars[0].o
            h = max(b.h for b in bars)
            l = min(b.l for b in bars)
            c = bars[-1].c
            v = sum(b.v for b in bars)
            daily_rows_window.append((s, d.strftime("%Y-%m-%d"), o, h, l, c, v))
            prev_close = c

        for row in daily_rows + daily_rows_window:
            db.upsert_daily(conn, row[0], row[1], row[2], row[3], row[4],
                            row[5], row[6], "demo")

        # ---- context for each window day, computed strictly from earlier days
        all_daily = (daily_rows + daily_rows_window)
        all_daily.sort(key=lambda r: r[1])
        for widx, wrow in enumerate(daily_rows_window):
            earlier = [r for r in all_daily if r[1] < wrow[1]]
            bars = [Bar(r[1] + " 00:00:00", r[2], r[3], r[4], r[5], r[6])
                    for r in earlier]
            ctx = _compute_ctx(s, wrow[1], bars)
            if ctx:
                db.save_context(conn, s, wrow[1], ctx["daily_close"], ctx["sma20"],
                                ctx["sma50"], ctx["atr14"], ctx["avg_vol20"],
                                ctx["trend_ok"], "demo")
    conn.commit()
    if not quiet:
        print(f"[demo] seeded {len(symbols)} symbols x {days} days "
              f"({first_day} .. {last_day}), seed={seed}")
    return first_day, last_day, len(symbols)


def _compute_ctx(symbol, date_str, bars: list[Bar]) -> dict | None:
    import data_fetch
    return data_fetch.compute_context(symbol, bars, date_str)
