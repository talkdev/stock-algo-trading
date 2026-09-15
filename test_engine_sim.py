"""
Offline simulation of the FULL live engine against a mock Upstox client.
Validates (no network needed):
  * pre-open context build
  * mid-session initial sync + per-boundary scan
  * entries on bar closes
  * tick-level exits via quotes
  * EOD flatten (intraday-only)
  * post-close finalisation (final bar + daily bar + EOD report)
  * no partial daily bar before 15:35 (DB stays clean)
  * RESTART SAFETY: a second engine instance on the same DB resumes without
    double-trading and with open positions restored
  * REAL MODE (mock orders): stale overnight position flattened at open,
    protective SL-M per entry, SL follows partials (breakeven ratchet),
    every SL cancelled at final close, no orphaned orders, real equity
    proxy drives the equity curve
"""
import os, sys, tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import config, db, demo_data
import mkttime
import data_fetch
import engine as engine_mod
from mkttime import IST, fmt_t, parse_t
from engine import Engine

# simulated session date = last business day <= actual today (IST)
_today = datetime.now(IST).date()
while _today.weekday() >= 5:
    _today -= timedelta(days=1)
TODAY = _today.strftime("%Y-%m-%d")

SYMS = ["RELIANCE", "TCS", "HDFCBANK", "ITC", "SBIN", "LT"]
td = tempfile.mkdtemp(prefix="stmr_sim_")
DB = os.path.join(td, "trading.db")
config.DB_PATH = Path(DB)
config.REPORTS_DIR = Path(td) / "reports"
config.REPORTS_DIR.mkdir()
config.TOKEN_FILE = Path(td) / "tok.json"
config.FM_TOKEN_FILE = Path(td) / "fm.json"

conn = db.get_conn(DB)
db.init_db(conn)
# seed history INCLUDING the session day (for the mock's data + daily rows),
# then remove that day's 5-min bars + context so the engine itself has to
# capture them via the (mock) API - exactly like a live session.
first, last, n = demo_data.seed_demo(conn, days=10, symbols=SYMS, seed=11,
                                     end_date=TODAY, quiet=True)
assert last == TODAY
for s in SYMS:
    db.save_instrument(conn, s, f"NSE_EQ|{s}", 1, "", "test")
conn.commit()
BAR = {s: db.get_day_bars(conn, s, TODAY) for s in SYMS}
conn.execute("DELETE FROM candles_5m WHERE bar_time LIKE ?", (TODAY + " %",))
conn.execute("DELETE FROM stock_context WHERE date=?", (TODAY,))
conn.commit()
assert all(len(v) == 75 for v in BAR.values())
DAILY = {s: [db.Bar(r["date"] + " 00:00:00", r["open"], r["high"], r["low"],
                    r["close"], r["volume"])
             for r in conn.execute(
                 "SELECT * FROM daily_candles WHERE symbol=? ORDER BY date", (s,))]
         for s in SYMS}


ORDERS: list = []
_oid = [1000]


class MockClient:
    def ensure_token(self):
        pass

    def fetch_instrument_master(self, cache_file=None):
        return {s: {"instrument_key": f"NSE_EQ|{s}", "lot_size": 1, "isin": ""}
                for s in SYMS}

    # ---- order machinery (real-mode simulation) ----
    def place_order(self, key, qty, side, product="INTRADAY",
                    order_type="MARKET", price=0.0, trigger_price=0.0,
                    validity="DAY"):
        _oid[0] += 1
        oid = f"MOCK-{_oid[0]}"
        o = {"order_id": oid, "key": key, "side": side, "quantity": int(qty),
             "order_type": order_type, "trigger_price": trigger_price,
             "status": "COMPLETE", "average_trade_price": None}
        if order_type == "MARKET":
            q = self.get_quote(key)
            base = q["last_price"] if q else 0.0
            s = config.SLIPPAGE_BPS / 1e4
            o["average_trade_price"] = round(
                base * (1 + s) if side == "BUY" else base * (1 - s), 2)
        else:  # SL-M etc. rest at the broker until triggered/cancelled
            o["status"] = "OPEN"
        ORDERS.append(o)
        return oid

    def wait_fill(self, oid, timeout=15.0):
        return next((o for o in ORDERS if o["order_id"] == oid), {})

    def cancel_order(self, oid):
        for o in ORDERS:
            if o["order_id"] == oid and o["status"] == "OPEN":
                o["status"] = "CANCELED"
        return {}

    def get_order(self, oid):
        return next((o for o in ORDERS if o["order_id"] == oid), {})

    def list_orders(self, status=None):
        return [o for o in ORDERS if not status or o["status"] == status]

    def get_candles(self, key, interval, start, end):
        s = key.split("|")[1]
        if interval == "d":
            return [b for b in DAILY[s]
                    if mkttime.ist_str_to_epoch(b.t[:10] + " 00:00:00") >= start
                    and mkttime.ist_str_to_epoch(b.t[:10] + " 00:00:00")
                    <= mkttime.ist_str_to_epoch(b.t[:10] + " 23:59:59")]
        lo = mkttime.epoch_to_ist_str(start)
        hi = mkttime.epoch_to_ist_str(end)
        return [b for b in BAR[s] if lo <= b.t <= hi]

    def get_quote(self, key):
        s = key.split("|")[1]
        now = FAKE_NOW[0]
        for b in BAR[s]:
            t2 = parse_t(b.t) + timedelta(minutes=5)
            if b.t <= fmt_t(now) < fmt_t(t2):
                return {"last_price": b.c, "open": b.o, "high": b.h,
                        "low": b.l, "volume": b.v}
        return None


FAKE_NOW = [None]


def set_now(hm):
    if ":" not in hm[3:]:
        hm = hm + ":00"
    FAKE_NOW[0] = datetime.strptime(f"{TODAY} {hm}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=mkttime.IST)


def patch_time():
    mkttime.ist_now = lambda: FAKE_NOW[0]
    data_fetch.ist_now = lambda: FAKE_NOW[0]
    engine_mod.ist_now = lambda: FAKE_NOW[0]
    engine_mod.now_str = lambda: FAKE_NOW[0].strftime("%Y-%m-%d %H:%M:%S")


patch_time()

# inject the mock client everywhere the engine constructs an UpstoxClient
import upstox_client
upstox_client.UpstoxClient = MockClient


def run_from_to(eng, hm_from, hm_to, step=4):
    t = datetime.strptime(f"{TODAY} {hm_from}", "%Y-%m-%d %H:%M")
    t_end = datetime.strptime(f"{TODAY} {hm_to}", "%Y-%m-%d %H:%M")
    while t <= t_end:
        set_now(t.strftime("%H:%M:%S"))
        eng.pass_once()
        t += timedelta(minutes=step)


# ---------------------------------------------------------------- phase A
print("== phase A: 08:50 -> 14:08 (first engine instance) ==")
eng = Engine("paper", db_path=DB)
set_now("08:50")
eng.pass_once()                                   # pre-open context
set_now("09:16")
eng.pass_once()                                   # initial sync
run_from_to(eng, "09:20", "14:08")

open_A = {p["symbol"]: p for p in db.get_open_positions(conn)}
n_trades_A = len(db.trades_in_range(conn, TODAY, TODAY))
# engine-captured daily bars for today must not exist before 15:35
# (demo-seeded rows are a different source and predate the engine)
daily_today_A = conn.execute(
    "SELECT COUNT(*) FROM daily_candles WHERE date=? AND source='upstox'",
    (TODAY,)).fetchone()[0]
print(f"  trades so far: {n_trades_A} | open: {list(open_A)} | "
      f"engine daily bars for today (must be 0): {daily_today_A}")
assert daily_today_A == 0, "today's daily bar must not be captured before 15:35"
ctx_rows = conn.execute("SELECT COUNT(*) FROM stock_context WHERE date=?",
                        (TODAY,)).fetchone()[0]
print(f"  context rows for today: {ctx_rows} (expect {len(SYMS)})")
assert ctx_rows == len(SYMS)

# ensure the restart test has a live position to restore
if not open_A:
    s = SYMS[0]
    b = BAR[s][-1]
    db.open_position(conn, s, "paper", 100, b.c, f"{TODAY} 13:55:00",
                     b.c * 0.99, b.c * 1.002, 1.0, 50.0, "sim-injected", None)
    conn.commit()
    open_A = {s: db.get_open_positions(conn)[0] for s in (s,)}
    print(f"  (no position was open at 14:08 - injected one on {s} for the restart test)")

# ---------------------------------------------------------------- phase B
print("\n== phase B: RESTART - new engine instance at 14:12 ==")
eng2 = Engine("paper", db_path=DB)
set_now("14:12")
eng2.pass_once()
trades_B = len(db.trades_in_range(conn, TODAY, TODAY))
open_B = {p["symbol"]: p for p in db.get_open_positions(conn)}
print(f"  trades: {n_trades_A} -> {trades_B} | open now: {list(open_B)}")
cnt = Counter((r["symbol"], r["bar_time"], r["kind"])
              for r in conn.execute("SELECT * FROM processed_bars"))
assert all(v == 1 for v in cnt.values()), \
    f"a bar was processed twice! { [k for k, v in cnt.items() if v > 1][:5] }"
# the injected position must have survived the restart: either still open
# with the same id, or closed BY THE POST-RESTART ENGINE - never silently
# lost. Earliest legitimate post-restart exit: the 14:05 bar is the oldest
# stored-but-unprocessed completed bar (the first engine stopped at 14:08,
# after the 14:00 bar), so any exit at/after the 14:05 bar was decided by
# the restarted engine replaying it.
if SYMS[0] in open_A:
    pA = open_A[SYMS[0]]
    if pA["entry_time"] == f"{TODAY} 13:55:00":
        p = db.get_position(conn, pA["id"])
        assert p is not None, "position lost on restart!"
        if p["exit_time"] is None:
            assert p["symbol"] == SYMS[0]
            print(f"  position {SYMS[0]} id={pA['id']} still open after restart  OK")
        else:
            assert p["exit_time"] >= f"{TODAY} 14:05:00", \
                f"position closed before restart window: {p['exit_time']}"
            print(f"  position {SYMS[0]} id={pA['id']} restored, then exited "
                  f"by the restarted engine at {p['exit_time']} ({p['exit_reason']})  OK")

# ---------------------------------------------------------------- phase C
print("\n== phase C: resume 14:16 -> 15:29, then post-close ==")
run_from_to(eng2, "14:16", "15:29")
set_now("15:38")
eng2.pass_once()
eng2.pass_once()
assert db.get_meta(conn, f"day_final_{TODAY}") == "1"

# ---------------------------------------------------------------- phase D
trades = db.trades_in_range(conn, TODAY, TODAY)
closed = [t for t in trades if t["exit_time"]]
daily_today = conn.execute(
    "SELECT COUNT(*) FROM daily_candles WHERE date=?", (TODAY,)).fetchone()[0]
print("\n== phase D: final assertions ==")
print(f"  trades today      : {len(trades)} (closed {len(closed)})")
print(f"  exit reasons      : {sorted(set(t['exit_reason'] for t in closed))}")
print(f"  open positions    : {len(db.get_open_positions(conn))} (must be 0)")
print(f"  live_5m residue   : {db.count_rows(conn, 'live_5m')} (must be 0)")
print(f"  equity rows       : {db.count_rows(conn, 'equity_curve')}")
print(f"  daily bars today  : {daily_today} (expect {len(SYMS)} after 15:35)")
print(f"  eod report        : {list(config.REPORTS_DIR.glob('eod_*.md'))}")
assert len(trades) > 0, "engine should have traded the demo dips"
assert len(db.get_open_positions(conn)) == 0, "must be flat at EOD"
assert db.count_rows(conn, "live_5m") == 0, "no transient residue"
assert daily_today == len(SYMS), "today's daily bar written post-close"
assert list(config.REPORTS_DIR.glob("eod_*.md")), "EOD report missing"
for t in closed:
    assert t["exit_time"][:10] == TODAY
    assert t["exit_reason"] in ("STOP", "TARGET", "TARGET2", "MEAN", "EOD",
                                "DATA_END", "TIME")
    assert t["qty_remaining"] == 0, f"closed trade {t['id']} not fully settled"

# ---------------------------------------------------------------- phase E
print("\n== phase E: REAL mode (order/stop consistency, stale close, proxy) ==")
DB2 = os.path.join(td, "real.db")
conn2 = db.get_conn(DB2)
db.init_db(conn2)
demo_data.seed_demo(conn2, days=10, symbols=SYMS, seed=11, end_date=TODAY,
                    quiet=True)
for s in SYMS:
    db.save_instrument(conn2, s, f"NSE_EQ|{s}", 1, "", "test")
conn2.commit()

# fixture: a position that survived from the previous session (crash), with
# its broker-side SL still resting open
yday = (datetime.strptime(TODAY, "%Y-%m-%d") - timedelta(days=1)).strftime(
    "%Y-%m-%d")
stale_pid = db.open_position(conn2, "TCS", "real", 20, 100.0,
                             yday + " 14:35:00", 99.0, 101.0, 0.5, 8.0,
                             "stale fixture", "MOCK-STALE-BUY")
ORDERS.append({"order_id": "MOCK-STALE-SL", "key": "NSE_EQ|TCS", "side": "SELL",
               "quantity": 20, "order_type": "SL-M", "trigger_price": 99.0,
               "status": "OPEN", "average_trade_price": None})
db.update_position(conn2, stale_pid, sl_order="MOCK-STALE-SL")
conn2.commit()

eng3 = Engine("real", db_path=DB2)
assert eng3.broker is None, "real mode must not paper before API auth"
set_now("09:16")
eng3.pass_once()   # OPEN: stale close + order reconciliation, then sync

stale = db.get_position(conn2, stale_pid)
assert stale["exit_reason"] == "STALE_RESTART" and stale["qty_remaining"] == 0, \
    "stale REAL position must be flattened at market open (intraday mandate)"
assert next(o for o in ORDERS if o["order_id"] == "MOCK-STALE-SL")["status"] \
    == "CANCELED", "stale SL order must be cancelled"
stale_sells = [o for o in ORDERS if o["key"] == "NSE_EQ|TCS"
               and o["order_type"] == "MARKET" and o["side"] == "SELL"
               and o["status"] == "COMPLETE"]
assert stale_sells, "stale close must place a real market SELL"
assert conn2.execute(
    "SELECT COUNT(*) FROM run_events WHERE level='ERROR' "
    "AND msg LIKE 'STALE real position%'").fetchone()[0] >= 1, \
    "stale real close must be logged as ERROR"

run_from_to(eng3, "09:20", "15:29")
set_now("15:38")
eng3.pass_once()
eng3.pass_once()

trades_r = db.trades_in_range(conn2, TODAY, TODAY)
closed_r = [t for t in trades_r if t["exit_time"]]
assert len(trades_r) > 0, "real-mode engine should have traded the demo dips"
assert all(t["source"] == "real" for t in trades_r)
assert all(t["qty_remaining"] == 0 for t in closed_r)
assert db.get_open_positions(conn2) == [], "real mode must be flat at EOD"

slm = [o for o in ORDERS if o["order_type"] == "SL-M"
       and o["order_id"] != "MOCK-STALE-SL"]
n_entries = len(trades_r)
assert len(slm) >= n_entries, \
    f"every entry needs a protective SL-M (entries {n_entries}, SL-Ms {len(slm)})"
assert not [o for o in ORDERS if o["status"] == "OPEN"], \
    "no order (entry or SL) may be left open at the end of the day"
for t in closed_r:
    key = f"NSE_EQ|{t['symbol']}"
    assert [o for o in slm if o["key"] == key], \
        f"no protective SL-M ever for {t['symbol']} (trade {t['id']})"
    if t["partial_count"]:
        assert [o for o in slm if o["key"] == key
                and o["trigger_price"] >= t["entry_fill"] - 1e-9], \
            f"partial on {t['symbol']} must ratchet the SL to breakeven"

eq_rows = conn2.execute(
    "SELECT equity, cash FROM equity_curve WHERE note='OPEN'").fetchall()
assert eq_rows, "real mode must write the (proxy) equity curve"
assert all(r["equity"] > 0 for r in eq_rows)

n_orders_before = len(ORDERS)
eng4 = Engine("real", db_path=DB2)
set_now("17:00")
eng4.pass_once()   # CLOSED: no writes, no orders
assert len(ORDERS) == n_orders_before, "CLOSED session must place no orders"
assert db.get_open_positions(conn2) == []

print(f"  real trades       : {len(trades_r)} (closed {len(closed_r)}) | "
      f"SL-M orders: {len(slm)} | all cancelled: OK")
print("\nSIMULATION GREEN - paper + real, restart-safe, flat at EOD, "
      "order/stop-consistent, clean DB.")
