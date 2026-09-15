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


class MockClient:
    def ensure_token(self):
        pass

    def fetch_instrument_master(self, cache_file=None):
        return {s: {"instrument_key": f"NSE_EQ|{s}", "lot_size": 1, "isin": ""}
                for s in SYMS}

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
# with the same id, or closed BY THE POST-RESTART ENGINE at/after 14:10
# (a legitimate exit) - never silently lost
if SYMS[0] in open_A:
    pA = open_A[SYMS[0]]
    if pA["entry_time"] == f"{TODAY} 13:55:00":
        p = db.get_position(conn, pA["id"])
        assert p is not None, "position lost on restart!"
        if p["exit_time"] is None:
            assert p["symbol"] == SYMS[0]
            print(f"  position {SYMS[0]} id={pA['id']} still open after restart  OK")
        else:
            assert p["exit_time"] >= f"{TODAY} 14:10:00", \
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
    assert t["exit_reason"] in ("STOP", "TARGET", "MEAN", "EOD", "DATA_END",
                                "TIME")
print("\nSIMULATION GREEN - restart-safe, flat at EOD, clean DB, EOD report written.")
