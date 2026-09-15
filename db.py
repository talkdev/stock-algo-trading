"""
db.py - SQLite persistence layer. The database is the SINGLE SOURCE OF TRUTH.

Why this design (maps to the user requirements):
  * "Restart must not lose trade data"  -> open positions, cash, stops and
    processed-bar markers all live in tables; the engine re-hydrates from DB.
  * "DB must not be made dirty"         -> candles_5m stores ONLY completed
    bars (immutable, upsert-on-conflict-nothing); the in-progress bar lives in
    the transient live_5m table; today's daily bar is written only post-close.
  * "Replay the backtest against the DB" -> every 5-min bar + daily bar +
    context row is stored with its source tag, so backtest.py re-runs the same
    engine logic over exactly what the live engine saw.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config
from console import now_str
from indicators import Bar

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol         TEXT PRIMARY KEY,
    instrument_key TEXT,
    lot_size       INTEGER DEFAULT 1,
    isin           TEXT,
    source         TEXT DEFAULT 'nse'
);
CREATE TABLE IF NOT EXISTS daily_candles (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL, source TEXT DEFAULT 'upstox',
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS candles_5m (
    symbol TEXT NOT NULL, bar_time TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL, source TEXT DEFAULT 'upstox',
    PRIMARY KEY (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS ix_c5_time ON candles_5m (bar_time);
CREATE TABLE IF NOT EXISTS live_5m (
    symbol TEXT PRIMARY KEY, bar_time TEXT,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_context (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    daily_close REAL, sma20 REAL, sma50 REAL, atr14 REAL,
    avg_vol20 INTEGER, trend_ok INTEGER, source TEXT DEFAULT 'upstox',
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, symbol TEXT, bar_time TEXT, kind TEXT,
    price REAL, z REAL, rsi REAL, atr REAL, detail TEXT,
    source TEXT DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, source TEXT DEFAULT 'paper',
    qty INTEGER NOT NULL, qty_remaining INTEGER,
    entry_fill REAL, entry_time TEXT,
    stop REAL, target REAL, atr_entry REAL, peak_price REAL,
    entry_fees REAL, realized_pnl REAL DEFAULT 0, partial_count INTEGER DEFAULT 0,
    exit_fill REAL, exit_time TEXT, exit_reason TEXT, exit_fees REAL,
    pnl_net REAL,
    detail_entry TEXT, detail_exit TEXT,
    order_buy TEXT, order_sell TEXT, sl_order TEXT,
    created_at TEXT, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_entry ON trades (symbol, entry_time);
CREATE TABLE IF NOT EXISTS processed_bars (
    symbol TEXT, bar_time TEXT, kind TEXT,
    PRIMARY KEY (symbol, bar_time, kind)
);
CREATE TABLE IF NOT EXISTS equity_curve (
    ts TEXT PRIMARY KEY, cash REAL, equity REAL,
    open_positions INTEGER, realized_today REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT
);
"""


def get_conn(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    cur = get_meta(conn, "schema_version")
    if cur is None or int(cur) < SCHEMA_VERSION:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


# idempotent column upgrades for databases created under older versions
_MIGRATIONS = [
    ("trades", "qty_remaining", "ALTER TABLE trades ADD COLUMN qty_remaining INTEGER"),
    ("trades", "realized_pnl", "ALTER TABLE trades ADD COLUMN realized_pnl REAL DEFAULT 0"),
    ("trades", "partial_count", "ALTER TABLE trades ADD COLUMN partial_count INTEGER DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(trades)")
    have = {r[1] for r in cur.fetchall()}
    for table, column, ddl in _MIGRATIONS:
        if column not in have:
            conn.execute(ddl)
    # backfill qty_remaining for rows created before v2
    if "qty_remaining" not in have:
        conn.execute("UPDATE trades SET qty_remaining=qty WHERE qty_remaining IS NULL")
    else:
        conn.execute("UPDATE trades SET qty_remaining=qty WHERE qty_remaining IS NULL")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------
def upsert_candle(conn, symbol, t, o, h, l, c, v, source="upstox") -> bool:
    """Store a COMPLETED 5-min bar. Immutable (ON CONFLICT DO NOTHING):
    re-fetching the same bar never mutates history -> no dirty data.
    Returns True if the row was newly inserted."""
    cur = conn.execute(
        "INSERT INTO candles_5m(symbol,bar_time,open,high,low,close,volume,source) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,bar_time) DO NOTHING",
        (symbol, t, float(o), float(h), float(l), float(c), int(v or 0), source),
    )
    return cur.rowcount > 0


def upsert_daily(conn, symbol, date, o, h, l, c, v, source="upstox") -> None:
    conn.execute(
        "INSERT INTO daily_candles(symbol,date,open,high,low,close,volume,source) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET "
        "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
        (symbol, date, float(o), float(h), float(l), float(c), int(v or 0), source),
    )


def upsert_live(conn, symbol, t, o, h, l, c, v) -> None:
    conn.execute(
        "INSERT INTO live_5m(symbol,bar_time,open,high,low,close,volume,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
        "bar_time=excluded.bar_time, open=excluded.open, high=excluded.high, "
        "low=excluded.low, close=excluded.close, volume=excluded.volume, "
        "updated_at=excluded.updated_at",
        (symbol, t, float(o), float(h), float(l), float(c), int(v or 0), now_str()),
    )


def del_live(conn, symbol) -> None:
    conn.execute("DELETE FROM live_5m WHERE symbol=?", (symbol,))


def get_live(conn, symbol):
    r = conn.execute("SELECT * FROM live_5m WHERE symbol=?", (symbol,)).fetchone()
    if not r:
        return None
    return (r["symbol"], r["bar_time"], r["open"], r["high"], r["low"], r["close"], r["volume"])


def get_day_bars(conn, symbol: str, date: str) -> list[Bar]:
    rows = conn.execute(
        "SELECT * FROM candles_5m WHERE symbol=? AND bar_time LIKE ? ORDER BY bar_time",
        (symbol, date + " %"),
    ).fetchall()
    return [Bar(r["bar_time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def last_bar_time(conn, symbol: str, date: str) -> str | None:
    r = conn.execute(
        "SELECT MAX(bar_time) AS m FROM candles_5m WHERE symbol=? AND bar_time LIKE ?",
        (symbol, date + " %"),
    ).fetchone()
    return r["m"] if r and r["m"] else None


def day_coverage(conn, date: str) -> dict:
    rows = conn.execute(
        "SELECT symbol, COUNT(*) AS n FROM candles_5m WHERE bar_time LIKE ? GROUP BY symbol",
        (date + " %",),
    ).fetchall()
    return {r["symbol"]: r["n"] for r in rows}


def distinct_dates(conn, start: str, end: str, symbols: list[str] | None = None) -> list[str]:
    q = "SELECT DISTINCT substr(bar_time,1,10) AS d FROM candles_5m " \
        "WHERE substr(bar_time,1,10) BETWEEN ? AND ?"
    args: list = [start, end]
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    q += " ORDER BY d"
    return [r["d"] for r in conn.execute(q, args).fetchall()]


# ---------------------------------------------------------------------------
# context / instruments
# ---------------------------------------------------------------------------
def save_context(conn, symbol, date, daily_close, sma20, sma50, atr14,
                 avg_vol20, trend_ok, source="upstox") -> None:
    conn.execute(
        "INSERT INTO stock_context(symbol,date,daily_close,sma20,sma50,atr14,avg_vol20,trend_ok,source) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET "
        "daily_close=excluded.daily_close, sma20=excluded.sma20, sma50=excluded.sma50, "
        "atr14=excluded.atr14, avg_vol20=excluded.avg_vol20, trend_ok=excluded.trend_ok, "
        "source=excluded.source",
        (symbol, date, daily_close, sma20, sma50, atr14, avg_vol20, int(trend_ok), source),
    )


def get_context(conn, symbol: str, date: str):
    return conn.execute(
        "SELECT * FROM stock_context WHERE symbol=? AND date=?", (symbol, date)
    ).fetchone()


def save_instrument(conn, symbol, instrument_key, lot_size=1, isin="", source="nse") -> None:
    conn.execute(
        "INSERT INTO instruments(symbol,instrument_key,lot_size,isin,source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET instrument_key=excluded.instrument_key, "
        "lot_size=excluded.lot_size, isin=excluded.isin, source=excluded.source",
        (symbol, instrument_key, int(lot_size), isin, source),
    )


def get_instrument(conn, symbol: str):
    return conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()


# ---------------------------------------------------------------------------
# signals / events / equity
# ---------------------------------------------------------------------------
def record_signal(conn, ts, symbol, bar_time, kind, price, z, rsi_v, atr_v, detail, source="live") -> None:
    conn.execute(
        "INSERT INTO signals(ts,symbol,bar_time,kind,price,z,rsi,atr,detail,source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts, symbol, bar_time, kind, price, z, rsi_v, atr_v, detail, source),
    )


def log_event(conn, level: str, msg: str) -> None:
    conn.execute("INSERT INTO run_events(ts,level,msg) VALUES(?,?,?)", (now_str(), level, msg))


def append_equity(conn, ts, cash, equity, open_positions, realized_today, note) -> None:
    conn.execute(
        "INSERT INTO equity_curve(ts,cash,equity,open_positions,realized_today,note) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET cash=excluded.cash, "
        "equity=excluded.equity, open_positions=excluded.open_positions, "
        "realized_today=excluded.realized_today, note=excluded.note",
        (ts, cash, equity, open_positions, realized_today, note),
    )


def equity_at_or_before(conn, ts: str):
    r = conn.execute(
        "SELECT * FROM equity_curve WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()
    return r


# ---------------------------------------------------------------------------
# trades / positions
# ---------------------------------------------------------------------------
def open_position(conn, symbol, source, qty, entry_fill, t, stop, target, atr_v,
                  entry_fees, detail, order_id) -> int:
    cur = conn.execute(
        "INSERT INTO trades(symbol,source,qty,qty_remaining,entry_fill,entry_time,"
        "stop,target,atr_entry,entry_fees,detail_entry,order_buy,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, source, int(qty), int(qty), entry_fill, t, stop, target, atr_v,
         entry_fees, detail, order_id, now_str()),
    )
    return int(cur.lastrowid)


def get_position(conn, pid: int):
    return conn.execute("SELECT * FROM trades WHERE id=?", (pid,)).fetchone()


def get_open_positions(conn) -> list:
    return conn.execute(
        "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time"
    ).fetchall()


def update_position(conn, pid: int, **fields) -> None:
    allowed = {"stop", "target", "peak_price", "sl_order", "order_buy", "order_sell",
               "qty_remaining", "realized_pnl", "partial_count", "exit_fees"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if sets:
        conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id=?",
                     tuple(args) + (pid,))


def sell_partial(conn, pid: int, qty: int, exit_fill, t, reason, exit_fees, detail) -> float:
    """Close `qty` shares of an open position, keep the rest.
    Returns the realized P&L contribution of this slice (net of its fees).
    pnl_net stays NULL until the position is fully closed."""
    p = get_position(conn, pid)
    if not p or p["exit_time"]:
        raise ValueError(f"trade {pid} not open")
    qty = int(qty)
    left = p["qty_remaining"] if p["qty_remaining"] is not None else p["qty"]
    if qty <= 0 or qty > left:
        raise ValueError(f"partial qty {qty} invalid (remaining {left})")
    realized = (exit_fill - p["entry_fill"]) * qty - exit_fees
    conn.execute(
        "UPDATE trades SET qty_remaining=?, realized_pnl=COALESCE(realized_pnl,0)+?, "
        "exit_fees=COALESCE(exit_fees,0)+?, partial_count=COALESCE(partial_count,0)+1 "
        "WHERE id=?",
        (left - qty, realized, exit_fees, pid),
    )
    return realized


def close_position(conn, pid: int, exit_fill, t, reason, exit_fees, detail, order_id) -> None:
    """Close the REMAINING shares of a position and settle pnl_net."""
    p = get_position(conn, pid)
    if not p or p["exit_time"]:
        return
    qty = p["qty_remaining"] if p["qty_remaining"] is not None else p["qty"]
    realized = (exit_fill - p["entry_fill"]) * qty - exit_fees
    # entry fees were paid up front - settle them here so pnl_net nets out
    # ALL costs (partial slices already net of their own exit fees)
    total = (p["realized_pnl"] or 0.0) + realized - (p["entry_fees"] or 0.0)
    conn.execute(
        "UPDATE trades SET exit_fill=?, exit_time=?, exit_reason=?, "
        "exit_fees=COALESCE(exit_fees,0)+?, pnl_net=?, qty_remaining=0, "
        "detail_exit=?, order_sell=?, closed_at=? WHERE id=?",
        (exit_fill, t, reason, exit_fees, total, detail, order_id, now_str(), pid),
    )


def trades_in_range(conn, start: str, end: str, source: str | None = None) -> list:
    q = "SELECT * FROM trades WHERE entry_time >= ? AND entry_time <= ?"
    args: list = [start + " 00:00:00", end + " 23:59:59"]
    if source:
        q += " AND source=?"
        args.append(source)
    q += " ORDER BY entry_time"
    return conn.execute(q, args).fetchall()


def closed_pnl_on(conn, date: str) -> float:
    r = conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0) AS s FROM trades WHERE exit_time LIKE ? AND exit_time IS NOT NULL",
        (date + " %",),
    ).fetchone()
    return float(r["s"]) if r else 0.0


# ---------------------------------------------------------------------------
# processed-bar guard (prevents double trading after restarts)
# ---------------------------------------------------------------------------
def mark_processed(conn, symbol, t, kind) -> bool:
    """Atomically mark (symbol, bar, kind) as evaluated. Returns True only on
    the FIRST mark, so restarts / re-runs never re-execute a bar's logic."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO processed_bars(symbol,bar_time,kind) VALUES(?,?,?)",
        (symbol, t, kind),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------
def count_rows(conn, table: str) -> int:
    if table not in {"meta", "instruments", "daily_candles", "candles_5m", "live_5m",
                     "stock_context", "signals", "trades", "processed_bars",
                     "equity_curve", "run_events"}:
        raise ValueError(table)
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def purge_demo(conn) -> dict:
    """Remove everything tagged source='demo' (safe demo-data cleanup)."""
    out = {}
    for tbl in ("candles_5m", "daily_candles", "stock_context"):
        cur = conn.execute(f"DELETE FROM {tbl} WHERE source='demo'")
        out[tbl] = cur.rowcount
    conn.commit()
    return out
