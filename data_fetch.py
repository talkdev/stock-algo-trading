"""
data_fetch.py - WHEN and HOW data is fetched, persisted and validated.

KEY RULES (the "never dirty the DB" contract)
  * candles_5m    : ONLY fully-completed 5-min bars
                    (bar_end <= now - BOUNDARY_LAG_SEC). Immutable upserts.
  * live_5m       : transient row for the bar in progress (upserted constantly,
                    dropped once the bar is finalised into candles_5m).
  * daily_candles : past days only. Today's row is written ONLY in POST_CLOSE
                    (after 15:35), so no partial daily bar ever leaks in.
  * stock_context : built in PRE_OPEN from daily data strictly BEFORE the
                    trading day (no lookahead), idempotent per (symbol, date).
  * Outside the session window the fetcher does nothing at all - no calls,
    no writes.
  * Every write is an idempotent upsert keyed by (symbol, time): restarting or
    re-running can never duplicate or mutate history.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import config
import db
from indicators import Bar, atr, sma
from mkttime import (business_days_before, completed_bar_until, current_bar_start,
                     fmt_t, hm, ist_now, ist_str_to_epoch, parse_t, today_str)
from upstox_client import UpstoxError

from console import warn

# ---------------------------------------------------------------------------
# instruments (symbol -> Upstox instrument_key)
# ---------------------------------------------------------------------------
def ensure_instruments(client, conn, symbols: list[str]) -> tuple[dict, list[str]]:
    """
    Make sure every symbol has an instrument_key in the DB.
    Order of sources: DB cache -> data/instrument_keys.json (manual) ->
    NSE instrument master via client. Returns (mapping, still_missing).
    """
    have = {r["symbol"]: r for r in conn.execute("SELECT * FROM instruments")}
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for s in symbols:
        r = have.get(s)
        if r and r["instrument_key"]:
            mapping[s] = r["instrument_key"]
        else:
            missing.append(s)

    if missing and config.INSTRUMENT_KEYS_FILE.exists():
        try:
            manual = json.loads(config.INSTRUMENT_KEYS_FILE.read_text(encoding="utf-8"))
            for s in list(missing):
                if s in manual and manual[s]:
                    db.save_instrument(conn, s, manual[s], 1, "", "manual")
                    mapping[s] = manual[s]
                    missing.remove(s)
        except Exception as e:
            warn(f"instrument_keys.json unreadable: {e}")

    if missing:
        try:
            master = client.fetch_instrument_master()
            for s in list(missing):
                if s in master:
                    m = master[s]
                    db.save_instrument(conn, s, m["instrument_key"], m["lot_size"],
                                       m["isin"], "nse")
                    mapping[s] = m["instrument_key"]
                    missing.remove(s)
        except UpstoxError as e:
            warn(f"instrument master unavailable ({e})")

    conn.commit()
    if missing:
        warn("no instrument key for: " + ", ".join(missing)
             + "  -> these symbols are SKIPPED (limitation #5, see README)")
    return mapping, missing


# ---------------------------------------------------------------------------
# daily context (pre-open)
# ---------------------------------------------------------------------------
def compute_context(symbol: str, rows: list[Bar], date_str: str) -> dict | None:
    """
    Trend/volatility context from daily bars strictly BEFORE date_str.
    rows must be ascending daily Bars with t == date.
    """
    if len(rows) < 55:
        return None
    closes = [r.c for r in rows]
    vols = [r.v for r in rows]
    s20 = sma(closes, 20)[-1]
    s50 = sma(closes, 50)[-1]
    a14 = atr(rows, 14)[-1]
    avg_vol20 = sum(vols[-20:]) / 20.0
    trend_ok = 1 if (s20 and s50 and closes[-1] > s20 and s20 > s50) else 0
    return {
        "symbol": symbol, "date": date_str,
        "daily_close": closes[-1], "sma20": s20, "sma50": s50,
        "atr14": a14, "avg_vol20": int(avg_vol20), "trend_ok": trend_ok,
    }


def build_day_context(client, conn, date_str: str, symbols: list[str],
                      keys: dict) -> int:
    """
    Fetch daily candles and compute today's per-symbol context (idempotent).
    Uses only daily bars dated BEFORE date_str (no lookahead).
    Returns the number of symbols processed.
    """
    todo = [s for s in symbols if s in keys and not db.get_context(conn, s, date_str)]
    n_done = 0
    end_d = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    start_d = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=140)).strftime("%Y-%m-%d")
    for i, s in enumerate(todo):
        try:
            rows = client.get_candles(keys[s], "d",
                                      ist_str_to_epoch(start_d + " 00:00:00"),
                                      ist_str_to_epoch(end_d + " 23:59:59"))
        except UpstoxError as e:
            warn(f"  {s}: daily fetch failed ({e}) - context SKIPPED today")
            continue
        rows = [r for r in rows if r.t[:10] < date_str]
        if not rows:
            warn(f"  {s}: no daily data - context SKIPPED today")
            continue
        ctx = compute_context(s, rows, date_str)
        if ctx is None:
            warn(f"  {s}: only {len(rows)} daily bars (need 55+) - context SKIPPED today")
            continue
        for r in rows:
            db.upsert_daily(conn, s, r.t[:10], r.o, r.h, r.l, r.c, r.v, "upstox")
        db.save_context(conn, s, date_str, ctx["daily_close"], ctx["sma20"],
                        ctx["sma50"], ctx["atr14"], ctx["avg_vol20"], ctx["trend_ok"],
                        "upstox")
        n_done += 1
        print(f"  ctx  {s:<11} close={ctx['daily_close']:.2f} "
              f"SMA20={ctx['sma20']:.2f} SMA50={ctx['sma50']:.2f} "
              f"ATR14={ctx['atr14']:.2f} avgVol20={ctx['avg_vol20']:>9,.0f} "
              f"trend={'UP  ' if ctx['trend_ok'] else 'no   '}")
    conn.commit()
    return n_done


def context_from_daily(conn, symbol: str, date_str: str) -> dict | None:
    """Backtest fallback: derive context from already-stored daily candles."""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM daily_candles "
        "WHERE symbol=? AND date<? ORDER BY date", (symbol, date_str)
    ).fetchall()
    bars = [Bar(r["date"] + " 00:00:00", r["open"], r["high"], r["low"],
                r["close"], r["volume"]) for r in rows]
    return compute_context(symbol, bars, date_str)


# ---------------------------------------------------------------------------
# 5-minute bars
# ---------------------------------------------------------------------------
def sync_day_bars(client, conn, symbol: str, key: str, date_str: str,
                  now: datetime) -> tuple[int, Bar | None]:
    """
    Bring `symbol`'s 5-min data for `date_str` up to `now` (adaptive range:
    from the bar after the last stored one, or from 09:15 if nothing stored).
    Completed bars -> candles_5m (immutable). The in-progress bar (if any)
    -> live_5m (transient). Returns (n_new_completed, live_bar_or_None).
    """
    last = db.last_bar_time(conn, symbol, date_str)
    if last is None:
        start_t = date_str + " 09:15:00"
    else:
        start_t = fmt_t(parse_t(last) + timedelta(minutes=config.BAR_MINUTES))
    end_adj = now - timedelta(seconds=config.BOUNDARY_LAG_SEC)
    if hm(end_adj) < config.SESSION_OPEN_AT:
        return 0, None
    if hm(end_adj) > config.SESSION_CLOSE_AT:
        end_adj = parse_t(date_str + " 15:30:00")

    rows = client.get_candles(key, "5m", ist_str_to_epoch(start_t),
                              ist_str_to_epoch(fmt_t(end_adj)))
    rows = [r for r in rows if r.t[:10] == date_str]
    n_new = 0
    live: Bar | None = None
    for r in rows:
        bar_end = parse_t(r.t) + timedelta(minutes=config.BAR_MINUTES)
        if bar_end <= end_adj:
            if db.upsert_candle(conn, symbol, r.t, r.o, r.h, r.l, r.c, r.v, "upstox"):
                n_new += 1
        else:
            live = r  # in-progress bar (the latest one wins)
    if live is not None:
        db.upsert_live(conn, symbol, live.t, live.o, live.h, live.l, live.c, live.v)
    else:
        db.del_live(conn, symbol)
    conn.commit()
    return n_new, live


def poll_live_bar(client, conn, symbol: str, key: str, now: datetime) -> Bar | None:
    """
    Mid-bar quote poll for the in-progress 5-min bar (cheaper than the candle
    API). Returns a partial Bar and refreshes live_5m.
    """
    q = client.get_quote(key)
    if not q:
        return None
    cs = fmt_t(current_bar_start(now))
    prev = db.get_live(conn, symbol)
    if prev and prev[0] == cs:
        o, h, l = prev[2], prev[3], prev[4]
    else:
        o = q.get("open") or q["last_price"]
        h, l = o, o
    price = q["last_price"]
    h = max(h, price)
    l = min(l, price)
    bar = Bar(cs, o, h, l, price, int(q.get("volume") or 0))
    db.upsert_live(conn, symbol, cs, o, h, l, price, int(q.get("volume") or 0))
    conn.commit()
    return bar


def finalize_day(client, conn, date_str: str, symbols: list[str], keys: dict) -> bool:
    """
    POST_CLOSE housekeeping (idempotent via meta flag):
      * persist the final 15:25-15:30 bar for every symbol,
      * after 15:35 persist today's COMPLETED daily bar.
    """
    flag = f"day_final_{date_str}"
    if db.get_meta(conn, flag):
        return False
    for s in symbols:
        key = keys.get(s)
        if not key:
            continue
        try:
            rows = client.get_candles(key, "5m",
                                      ist_str_to_epoch(date_str + " 15:25:00"),
                                      ist_str_to_epoch(date_str + " 15:30:00"))
        except UpstoxError:
            continue
        for r in rows:
            if r.t == date_str + " 15:25:00":
                db.upsert_candle(conn, s, r.t, r.o, r.h, r.l, r.c, r.v, "upstox")
        db.del_live(conn, s)
    now = ist_now()
    if hm(now) >= "15:35":
        for s in symbols:
            key = keys.get(s)
            if not key:
                continue
            try:
                rows = client.get_candles(key, "d",
                                          ist_str_to_epoch(date_str + " 00:00:00"),
                                          ist_str_to_epoch(date_str + " 23:59:59"))
            except UpstoxError:
                continue
            rows = [r for r in rows if r.t[:10] == date_str]
            if rows:
                r = rows[-1]
                db.upsert_daily(conn, s, date_str, r.o, r.h, r.l, r.c, r.v, "upstox")
    db.set_meta(conn, flag, "1")
    conn.commit()
    return True
