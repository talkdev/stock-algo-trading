"""
data_import.py - load REAL 5-minute (or daily) OHLCV CSV exports into the DB.

WHY THIS EXISTS
  The Upstox API only returns TODAY's intraday bars (limitation #1), so
  multi-day backtests and tuning on REAL Nifty-100 data need history from
  somewhere else: a broker export, a data vendor, a screener, or the bars
  this engine itself accumulated over time. Import any CSV once and the whole
  pipeline (backtest.py, tune.py, the engine's replay) runs on real data.

FORMAT
  Header row; column names are auto-matched (case-insensitive):
    symbol  : symbol | symbol_name | nse_symbol | scrip | token
    time    : timestamp | bar_time | time | datetime | date_time | trade_time
    o/h/l/c : open|o   high|h   low|l   close|c|last
    volume  : volume | vol
  Timestamps accepted: 'YYYY-MM-DD HH:MM[:SS]' (assumed IST),
  'YYYY-MM-DDTHH:MM[:SS]', epoch seconds or milliseconds.
  If the CSV has no symbol column, pass --symbol.

EXAMPLES
  python main.py --import-csv nifty_5m.csv --kind 5m
  python main.py --import-csv RELIANCE_5m.csv --kind 5m --symbol RELIANCE
  python main.py --import-csv dailies.csv --kind daily --symbol RELIANCE
  python main.py --import-csv nifty_5m.csv --kind 5m --source myvendor

Rows are upserted with the given --source tag (default 'import') so they can
be audited or removed later; completed-bar semantics are unchanged.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import db
from mkttime import IST

_SYM_COLS = ("symbol", "symbol_name", "nse_symbol", "scrip", "token", "name")
_TIME_COLS = ("timestamp", "bar_time", "time", "datetime", "date_time",
              "trade_time", "date", "bar")
_OHLC = {"o": ("open", "o"), "h": ("high", "h"), "l": ("low", "l"),
         "c": ("close", "c", "last", "ltp")}
_VOL_COLS = ("volume", "vol")


def _norm(row: dict) -> dict:
    return {(k or "").strip().lower(): (v if v is not None else "")
            for k, v in row.items()}


def _pick(row: dict, names: tuple) -> str:
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def parse_time(raw: str) -> datetime | None:
    raw = str(raw).strip()
    if not raw:
        return None
    if raw.isdigit():
        ep = int(raw)
        if ep > 10**12:          # milliseconds
            ep //= 1000
        try:
            return datetime.fromtimestamp(ep, IST)
        except Exception:
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=IST)
        except Exception:
            continue
    return None


def has_time_component(raw: str) -> bool:
    return ":" in str(raw) or len(str(raw).strip()) > 10


def import_csv(conn, path, kind: str = "5m", symbol: str | None = None,
               source: str = "import") -> dict:
    """Import one CSV. Returns per-symbol stats + totals."""
    path = Path(path)
    if kind not in ("5m", "daily"):
        raise SystemExit(f"kind must be '5m' or 'daily', got {kind!r}")
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    if kind == "5m" and not symbol:
        # maybe the CSV carries a symbol column - peek at the header
        with open(path, newline="", encoding="utf-8-sig") as f:
            head = next(csv.reader(f), [])
        has_sym = any(h.strip().lower() in _SYM_COLS for h in head)
        if not has_sym:
            raise SystemExit("no symbol column in the CSV - pass --symbol")

    per_sym: dict = {}
    bad = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = _norm(row)
            s = _pick(row, _SYM_COLS) or symbol
            raw_t = _pick(row, _TIME_COLS)
            if kind == "5m" and not has_time_component(raw_t):
                bad += 1  # a 5-min bar needs a time, not just a date
                continue
            t = parse_time(raw_t)
            if not s or t is None:
                bad += 1
                continue
            try:
                o = float(_pick(row, _OHLC["o"]))
                h = float(_pick(row, _OHLC["h"]))
                l = float(_pick(row, _OHLC["l"]))
                c = float(_pick(row, _OHLC["c"]))
                v = int(float(_pick(row, _VOL_COLS) or 0))
            except (ValueError, KeyError):
                bad += 1
                continue
            s = s.upper()
            if kind == "5m":
                t = t.replace(microsecond=0)
                db.upsert_candle(conn, s, t.strftime("%Y-%m-%d %H:%M:%S"),
                                 o, h, l, c, v, source)
            else:
                db.upsert_daily(conn, s, t.strftime("%Y-%m-%d"), o, h, l, c,
                                v, source)
            st = per_sym.setdefault(s, {"rows": 0, "min": None, "max": None})
            st["rows"] += 1
            key = t.strftime("%Y-%m-%d %H:%M:%S") if kind == "5m" \
                else t.strftime("%Y-%m-%d")
            st["min"] = key if st["min"] is None or key < st["min"] else st["min"]
            st["max"] = key if st["max"] is None or key > st["max"] else st["max"]
    conn.commit()
    return {"per_sym": per_sym, "bad": bad,
            "total": sum(st["rows"] for st in per_sym.values())}


def run(file, kind="5m", symbol=None, source="import", db_path=None) -> int:
    import config
    from console import banner, table
    conn = db.get_conn(db_path or config.DB_PATH)
    db.init_db(conn)
    stats = import_csv(conn, file, kind, symbol, source)
    banner("CSV IMPORT COMPLETE",
           [f"file: {file}  kind: {kind}  source tag: {source}",
            f"rows imported: {stats['total']}  (skipped unparseable: {stats['bad']})"])
    rows = [[s, st["rows"], st["min"], st["max"]]
            for s, st in sorted(stats["per_sym"].items())]
    table(["symbol", "rows", "from", "to"], rows, aligns=["l", "r", "l", "l"])
    print(f"\ndatabase: {db_path or config.DB_PATH}")
    if stats["total"]:
        print("now: python backtest.py  /  python tune.py  /  python main.py")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="import OHLCV CSV into the STMR DB")
    ap.add_argument("file")
    ap.add_argument("--kind", choices=["5m", "daily"], default="5m")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--source", default="import")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    return run(a.file, a.kind, a.symbol, a.source, a.db)


if __name__ == "__main__":
    import sys
    sys.exit(main())
