"""
patch.py - database maintenance utilities (schema, health, resets).

  python patch.py status      row counts, open positions, schema version
  python patch.py integrity   PRAGMA integrity_check + foreign_key_check
  python patch.py migrate     (re)apply the schema - idempotent, versioned
  python patch.py purge-demo  delete only rows tagged source='demo'
  python patch.py reset --yes wipe paper-trading state + captured candles
                              (keeps instrument keys). Use after testing.
"""
from __future__ import annotations

import argparse
import sys

import config
import db
from console import banner, table


def cmd_status(conn) -> int:
    banner("PATCH STATUS")
    print(f"  schema version : {db.get_meta(conn, 'schema_version', '?')}")
    rows = [[t, db.count_rows(conn, t)] for t in
            ("meta", "instruments", "daily_candles", "candles_5m", "live_5m",
             "stock_context", "signals", "trades", "processed_bars",
             "equity_curve", "run_events")]
    table(["table", "rows"], rows, aligns=["l", "r"])
    open_pos = db.get_open_positions(conn)
    print(f"\n  open positions : {len(open_pos)}")
    for p in open_pos:
        print(f"    {p['symbol']:<12} {p['qty']:>5} @ {p['entry_fill']:.2f} "
              f"({p['entry_time']})  stop {p['stop']:.2f}  target {p['target']:.2f}")
    last = conn.execute("SELECT MAX(bar_time) AS m FROM candles_5m").fetchone()
    first = conn.execute("SELECT MIN(bar_time) AS m FROM candles_5m").fetchone()
    print(f"\n  bar window     : {first['m'] if first else '-'}  ..  "
          f"{last['m'] if last else '-'}")
    src = conn.execute(
        "SELECT source, COUNT(*) AS n FROM candles_5m GROUP BY source").fetchall()
    if src:
        print("  bar sources    : " + ", ".join(f"{r['source']}={r['n']}" for r in src))
    return 0


def cmd_integrity(conn) -> int:
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    print(f"  integrity_check : {ok}")
    print(f"  foreign_key_check: {'clean' if not fk else fk}")
    return 0 if ok == "ok" and not fk else 1


def cmd_migrate(conn) -> int:
    db.init_db(conn)
    print(f"  schema applied (version {db.get_meta(conn, 'schema_version')})")
    return 0


def cmd_purge_demo(conn) -> int:
    out = db.purge_demo(conn)
    for k, v in out.items():
        print(f"  {k:<14}: removed {v} demo rows")
    print("  (demo data removed; upstox data untouched)")
    return 0


def cmd_reset(conn, yes: bool) -> int:
    if not yes:
        print("  refusing without --yes. This wipes: trades, positions, cash, "
              "candles, context, signals, events, equity curve, processed bars.")
        print("  re-run with: python patch.py reset --yes")
        return 2
    for t in ("trades", "processed_bars", "equity_curve", "signals",
              "run_events", "live_5m", "candles_5m", "daily_candles",
              "stock_context"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM meta WHERE key LIKE 'paper_%' OR key LIKE 'day_final_%'")
    conn.commit()
    print("  paper state + captured data reset (instruments kept). "
          "Start equity will re-initialise on next engine run.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="STMR database maintenance")
    ap.add_argument("cmd", choices=["status", "integrity", "migrate",
                                    "purge-demo", "reset"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--yes", action="store_true", help="confirm destructive reset")
    a = ap.parse_args()
    conn = db.get_conn(a.db or config.DB_PATH)
    db.init_db(conn)
    fns = {"status": cmd_status, "integrity": cmd_integrity,
           "migrate": cmd_migrate, "purge-demo": cmd_purge_demo}
    if a.cmd in fns:
        return fns[a.cmd](conn)
    return cmd_reset(conn, a.yes)


if __name__ == "__main__":
    sys.exit(main())
