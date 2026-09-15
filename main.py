"""
main.py - STMR entry point (PAPER TRADE by default).

  python main.py                     # live loop, paper mode (default)
  python main.py --once              # single engine pass (cron-friendly)
  python main.py --status            # read-only status (never writes, no API)
  python main.py --observe           # scan + log everything, place no orders
  python main.py --mode real --i-understand-real-risk
                                     # LIVE orders via Upstox (read README first!)
  python main.py --import-csv FILE --kind 5m   # load real OHLCV history

The engine itself may be started at ANY time:
  * before/after market hours it captures NO data and writes NO rows
    (the DB stays clean);
  * mid-day it syncs today's bars once and resumes seamlessly;
  * restarts lose nothing (all state is in SQLite).
"""
from __future__ import annotations

import argparse
import sys

import config
import db
from console import banner, hr, inr, now_str, pct, signed, table
from mkttime import ist_now, session_phase, today_str


def show_status(db_path) -> int:
    """Read-only snapshot of the DB. No API calls, no writes."""
    conn = db.get_conn(db_path or config.DB_PATH)
    db.init_db(conn)
    now = ist_now()
    today = today_str(now)

    banner("STMR STATUS", [
        f"time    : {now_str()} IST   session phase: {session_phase(now)}",
        f"database: {db_path or config.DB_PATH}",
    ])

    pos = db.get_open_positions(conn)
    cov = db.day_coverage(conn, today)
    lp = {s: (conn.execute(
        "SELECT COALESCE((SELECT close FROM live_5m WHERE symbol=?), "
        "(SELECT close FROM candles_5m WHERE symbol=? "
        "ORDER BY bar_time DESC LIMIT 1))", (s, s)).fetchone()[0]) for s in cov}

    cash = float(db.get_meta(conn, "paper_cash", config.PAPER_START_EQUITY))
    start_eq = float(db.get_meta(conn, "paper_start_equity",
                                 config.PAPER_START_EQUITY))
    def held(p):
        try:
            return p["qty_remaining"] if p["qty_remaining"] is not None else p["qty"]
        except (IndexError, KeyError):
            return p["qty"]

    mv = sum(held(p) * lp.get(p["symbol"], p["entry_fill"]) for p in pos)
    equity = cash + mv
    realized_today = db.closed_pnl_on(conn, today)

    print()
    print(f"  phase            : {session_phase(now)}")
    print(f"  portfolio (paper): INR {inr(equity)}   "
          f"(start {inr(start_eq)}, day {pct(realized_today / start_eq * 100)})")
    print(f"  cash             : INR {inr(cash)} | open positions: {len(pos)}")
    print(f"  today's bars     : {len(cov)} symbols | "
          f"max {max(cov.values()) if cov else 0} bars | "
          f"realized P&L INR {signed(realized_today)}")
    print()
    if pos:
        print("  open positions:")
        table(["sym", "qty", "entry", "time", "stop", "target", "last", "uP&L"],
              [[p["symbol"], held(p), f"{p['entry_fill']:.2f}",
                p["entry_time"][11:16], f"{p['stop']:.2f}", f"{p['target']:.2f}",
                f"{lp.get(p['symbol'], p['entry_fill']):.2f}",
                signed((lp.get(p["symbol"], p["entry_fill"]) - p["entry_fill"])
                       * held(p))]
               for p in pos],
              aligns=["l", "r", "r", "l", "r", "r", "r", "r"])
    else:
        print("  open positions : none")

    print()
    trades = db.trades_in_range(conn, today, today)
    if trades:
        print(f"  today's trades ({len(trades)}):")
        table(["sym", "qty", "entry @", "exit @", "reason", "net P&L"],
              [[t["symbol"], t["qty"],
                f"{t['entry_fill']:.2f} {t['entry_time'][11:16]}",
                (f"{t['exit_fill']:.2f} {t['exit_time'][11:16]}"
                 if t["exit_time"] else "open"),
                t["exit_reason"] or "OPEN", signed(t["pnl_net"] or 0)]
               for t in trades],
              aligns=["l", "r", "r", "r", "l", "r"])
    else:
        print("  today's trades : none yet")

    last_eq = conn.execute(
        "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT 1").fetchone()
    if last_eq:
        print(f"\n  last equity mark : {last_eq['ts']}  INR {inr(last_eq['equity'])}")

    import glob
    eod = sorted(glob.glob(str(config.REPORTS_DIR / "eod_*.md")), reverse=True)
    if eod:
        print(f"  last EOD report  : {eod[0]}")
    print()
    for t in ("candles_5m", "daily_candles", "trades", "signals"):
        print(f"  rows {t:<14}: {db.count_rows(conn, t)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="STMR - short-term mean reversion engine (paper by default)")
    ap.add_argument("--mode", choices=["paper", "real"], default="paper",
                    help="default: paper (simulated fills, real Upstox data)")
    ap.add_argument("--once", action="store_true",
                    help="run a single engine pass and exit (for cron)")
    ap.add_argument("--observe", action="store_true",
                    help="scan and log everything but place NO orders")
    ap.add_argument("--status", action="store_true",
                    help="read-only status snapshot (no API, no writes)")
    ap.add_argument("--db", default=None, help="alternate SQLite path")
    ap.add_argument("--quiet", action="store_true", help="less console chatter")
    ap.add_argument("--i-understand-real-risk", action="store_true",
                    help="required to run --mode real")
    ap.add_argument("--import-csv", default=None, metavar="FILE",
                    help="import a real OHLCV CSV into the DB and exit")
    ap.add_argument("--kind", choices=["5m", "daily"], default="5m",
                    help="bar kind for --import-csv")
    ap.add_argument("--symbol", default=None,
                    help="symbol for --import-csv when the CSV has no column")
    ap.add_argument("--source", default="import",
                    help="source tag for --import-csv rows")
    a = ap.parse_args()

    if a.import_csv:
        import data_import
        return data_import.run(a.import_csv, a.kind, a.symbol, a.source, a.db)
    if a.status:
        return show_status(a.db)

    if a.mode == "real" and not a.i_understand_real_risk:
        print("REFUSED: --mode real places LIVE orders with real money.\n"
              "Read README ('Real mode') and pass --i-understand-real-risk.")
        return 2
    if a.mode == "real" and not config.UPSTOX_CLIENT_ID:
        print("REFUSED: UPSTOX_CLIENT_ID is not set (see README 'Upstox setup').")
        return 2
    if a.mode == "real":
        print("!" * 100)
        print("!  REAL MODE - live Upstox orders will be placed. "
              f"Sizing base = INR {config.REAL_CAPITAL:,.0f} (STMR_REAL_CAPITAL).")
        print("!" * 100)

    from engine import Engine
    Engine(a.mode, a.once, a.observe, a.db, a.quiet).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
