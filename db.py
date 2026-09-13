"""
Persistence layer.

Design rules that make the database safe:

1. WAL journal + synchronous=NORMAL  -> a crash mid-write cannot corrupt the file.
2. Every table has a natural primary key, so re-running an ingest is idempotent
   (INSERT ... ON CONFLICT DO UPDATE). Re-running the script never duplicates rows.
3. Every multi-row write happens inside one transaction.
4. `data_capture_log` records dataset + key range + row count. Nothing is fetched
   from the API if a completed capture row already covers the requested range --
   this is what keeps the DB from being "made dirty" when the script is run
   repeatedly at arbitrary times.
5. Trade state lives entirely here. Restarting the process reloads positions,
   pending orders and the day's scan verdicts; no state is held in memory only.
6. The raw tables (daily_ohlcv, intraday_5m, vix_daily, index_daily) form the
   replay corpus. backtest.py reads ONLY these tables and never calls the API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS instruments (
    instrument_key   TEXT PRIMARY KEY,
    exchange_token   TEXT,
    trading_symbol   TEXT,
    name             TEXT,
    segment          TEXT,
    isin             TEXT,
    lot_size         INTEGER,
    tick_size        REAL,
    freeze_quantity  REAL,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_inst_symbol ON instruments(trading_symbol);

CREATE TABLE IF NOT EXISTS universe (
    symbol      TEXT PRIMARY KEY,
    source      TEXT,
    as_of       TEXT,
    is_current  INTEGER DEFAULT 1,
    sector      TEXT,                       -- external feed; NULL until supplied
    added_at    TEXT
);

-- ---------------------------------------------------------------- replay corpus
CREATE TABLE IF NOT EXISTS daily_ohlcv (
    instrument_key TEXT NOT NULL,
    ts_date        TEXT NOT NULL,           -- YYYY-MM-DD
    open  REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (instrument_key, ts_date)
);

CREATE TABLE IF NOT EXISTS intraday_5m (
    instrument_key TEXT NOT NULL,
    ts             TEXT NOT NULL,           -- YYYY-MM-DD HH:MM:SS (IST)
    open  REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (instrument_key, ts)
);

CREATE TABLE IF NOT EXISTS index_daily (
    index_key TEXT NOT NULL,
    ts_date   TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (index_key, ts_date)
);

CREATE TABLE IF NOT EXISTS vix_daily (
    ts_date TEXT PRIMARY KEY,
    open REAL, high REAL, low REAL, close REAL
);

CREATE TABLE IF NOT EXISTS intraday_quotes (
    instrument_key TEXT NOT NULL,
    ts             TEXT NOT NULL,
    ltp REAL, open REAL, high REAL, low REAL, close REAL,
    volume REAL, upper_circuit REAL, lower_circuit REAL,
    PRIMARY KEY (instrument_key, ts)
);

CREATE TABLE IF NOT EXISTS macro_obs (
    ts_date  TEXT PRIMARY KEY,
    brent    REAL,
    usdinr   REAL,
    gsec10y  REAL,
    fpi_net  REAL,
    source   TEXT
);

CREATE TABLE IF NOT EXISTS corporate_events (
    symbol     TEXT NOT NULL,
    event_type TEXT NOT NULL,               -- EARNINGS | BOARD_MEET | SPLIT | BONUS | DIVIDEND
    event_date TEXT NOT NULL,               -- YYYY-MM-DD
    detail     TEXT,
    source     TEXT,
    PRIMARY KEY (symbol, event_type, event_date)
);

CREATE TABLE IF NOT EXISTS circuit_hits (
    symbol  TEXT NOT NULL,
    ts_date TEXT NOT NULL,
    band    TEXT,
    PRIMARY KEY (symbol, ts_date)
);

-- ---------------------------------------------------------------- derived caches
CREATE TABLE IF NOT EXISTS seasonal_factors (
    instrument_key TEXT NOT NULL,
    slot           INTEGER NOT NULL,        -- 1..75
    s_m            REAL,
    sample_size    INTEGER,
    as_of          TEXT,
    PRIMARY KEY (instrument_key, slot)
);

-- ---------------------------------------------------------------- capture log
CREATE TABLE IF NOT EXISTS data_capture_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset        TEXT NOT NULL,           -- daily | intraday5m | vix | index | instruments | macro
    instrument_key TEXT,
    range_from     TEXT,
    range_to       TEXT,
    rows_written   INTEGER DEFAULT 0,
    status         TEXT NOT NULL,           -- RUNNING | OK | ERROR
    started_at     TEXT,
    finished_at    TEXT,
    message        TEXT
);
CREATE INDEX IF NOT EXISTS ix_cap ON data_capture_log(dataset, instrument_key, status);

-- ---------------------------------------------------------------- scan / signals
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id        TEXT PRIMARY KEY,
    session_date  TEXT NOT NULL,
    phase         TEXT,
    macro_tier    TEXT,
    macro_detail  TEXT,
    universe_size INTEGER,
    candidates    INTEGER DEFAULT 0,
    selected      INTEGER DEFAULT 0,
    status        TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS ix_scan_session ON scan_runs(session_date);

-- one row per stock per gate: the full audit trail behind every decision
CREATE TABLE IF NOT EXISTS gate_results (
    run_id         TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    gate           TEXT NOT NULL,           -- G0_UNIVERSE | G1_TRADABLE | G2_TREND | ...
    passed         INTEGER,
    verdict        TEXT,                    -- PASS | FAIL | SKIP | REJ_*
    value          REAL,
    detail         TEXT,                    -- JSON of every number used
    PRIMARY KEY (run_id, instrument_key, gate)
);

CREATE TABLE IF NOT EXISTS candidates (
    run_id         TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    symbol         TEXT,
    confluence_pts INTEGER,
    score          REAL,
    rank           INTEGER,
    decision       TEXT,                    -- SELECTED | REJECTED | DEFERRED
    reject_code    TEXT,
    reason         TEXT,                    -- human-readable justification
    trigger_price  REAL,
    stop_price     REAL,
    r_value        REAL,
    b_net          REAL,
    prob_p         REAL,
    lots           INTEGER,
    payload        TEXT,                    -- JSON with all indicators
    PRIMARY KEY (run_id, instrument_key)
);

-- ---------------------------------------------------------------- trade state
CREATE TABLE IF NOT EXISTS positions (
    position_id    TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    symbol         TEXT,
    sector         TEXT,
    mode           TEXT NOT NULL,           -- PAPER | LIVE
    session_date   TEXT,
    qty            INTEGER,
    lots           INTEGER,
    lot_size       INTEGER,
    entry_price    REAL,
    stop_price     REAL,
    r_value        REAL,
    target1_price  REAL,
    target2_price  REAL,
    tau_halflife   REAL,
    prob_p         REAL,
    b_net          REAL,
    risk_rupees    REAL,
    state          TEXT NOT NULL,           -- PENDING_ENTRY | OPEN | T1_HIT | CLOSED | CANCELLED
    opened_at      TEXT,
    closed_at      TEXT,
    close_reason   TEXT,
    realized_pnl   REAL DEFAULT 0,
    realized_r     REAL DEFAULT 0,
    payload        TEXT
);
CREATE INDEX IF NOT EXISTS ix_pos_state ON positions(state);

CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    position_id    TEXT,
    instrument_key TEXT NOT NULL,
    symbol         TEXT,
    mode           TEXT NOT NULL,
    side           TEXT,
    order_type     TEXT,
    trigger_price  REAL,
    limit_price    REAL,
    qty            INTEGER,
    validity       TEXT,
    status         TEXT,                    -- ARMED | PLACED | FILLED | CANCELLED | REJECTED | EXPIRED
    reason         TEXT,
    broker_order_id TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    filled_at      TEXT,
    fill_price     REAL,
    fill_qty       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ord_status ON orders(status);

-- append-only audit trail; never UPDATEd, so history survives any restart
CREATE TABLE IF NOT EXISTS position_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_evt_pos ON position_events(position_id, id);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT,
    order_id    TEXT,
    ts          TEXT,
    side        TEXT,
    qty         INTEGER,
    price       REAL,
    charges     REAL,
    mode        TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    mode               TEXT,
    equity             REAL,
    unencumbered_cash  REAL,
    collateral         REAL,
    positions_open     INTEGER,
    heat_pct           REAL,
    macro_tier         TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""


class Database:
    """Thin, thread-safe wrapper over SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-statement write."""
        with self._write_lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def query_df(self, sql: str, params: Iterable = ()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=tuple(params))

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return r

    def scalar(self, sql: str, params: Iterable = (), default=None):
        r = self.one(sql, params)
        return default if r is None else r[0]

    # ------------------------------------------------------------------ kv store
    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), _now()),
        )

    def kv_get(self, key: str, default=None):
        raw = self.scalar("SELECT value FROM kv WHERE key=?", (key,))
        return default if raw is None else json.loads(raw)

    # ------------------------------------------------------------------ capture log
    def capture_exists(self, dataset: str, instrument_key: str | None,
                       range_from: str, range_to: str) -> bool:
        """True if a completed capture already covers exactly this range."""
        row = self.one(
            """SELECT id FROM data_capture_log
               WHERE dataset=? AND IFNULL(instrument_key,'')=IFNULL(?,'')
                 AND range_from=? AND range_to=? AND status='OK'
               LIMIT 1""",
            (dataset, instrument_key, range_from, range_to),
        )
        return row is not None

    def capture_last_ok(self, dataset: str, instrument_key: str | None = None) -> str | None:
        return self.scalar(
            """SELECT MAX(range_to) FROM data_capture_log
               WHERE dataset=? AND IFNULL(instrument_key,'')=IFNULL(?,'') AND status='OK'""",
            (dataset, instrument_key),
        )

    @contextmanager
    def capture(self, dataset: str, instrument_key: str | None,
                range_from: str, range_to: str) -> Iterator[dict]:
        """Context manager that logs a capture attempt and its outcome."""
        cur = self.execute(
            """INSERT INTO data_capture_log
               (dataset,instrument_key,range_from,range_to,status,started_at)
               VALUES(?,?,?,?,?,?)""",
            (dataset, instrument_key, range_from, range_to, "RUNNING", _now()),
        )
        cap_id = cur.lastrowid
        box: dict = {"rows": 0}
        try:
            yield box
            self.execute(
                """UPDATE data_capture_log SET status='OK', rows_written=?, finished_at=?
                   WHERE id=?""",
                (box.get("rows", 0), _now(), cap_id),
            )
        except Exception as exc:  # record the failure but never lose the attempt
            self.execute(
                """UPDATE data_capture_log SET status='ERROR', finished_at=?, message=?
                   WHERE id=?""",
                (_now(), str(exc)[:500], cap_id),
            )
            raise

    # ------------------------------------------------------------------ upserts
    def upsert_ohlcv(self, table: str, key_col: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ["open", "high", "low", "close", "volume"]
        with self.tx() as c:
            c.executemany(
                f"""INSERT INTO {table}(instrument_key,{key_col},{','.join(cols)})
                    VALUES(:instrument_key,:ts,{','.join(':'+x for x in cols)})
                    ON CONFLICT(instrument_key,{key_col}) DO UPDATE SET
                      open=excluded.open, high=excluded.high, low=excluded.low,
                      close=excluded.close, volume=excluded.volume""",
                [{"instrument_key": r["instrument_key"], "ts": r["ts"],
                  **{k: r.get(k) for k in cols}} for r in rows],
            )
        return len(rows)

    def upsert_instruments(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        now = _now()
        # Different sources populate different subsets of these columns. Defaulting here
        # keeps every caller honest instead of forcing each one to know the full schema.
        defaults = {"exchange_token": "", "trading_symbol": "", "name": "",
                    "segment": "", "isin": "", "lot_size": 1, "tick_size": 0.05,
                    "freeze_quantity": None}
        rows = [{**defaults, **{k: v for k, v in r.items() if k in defaults},
                 "instrument_key": r["instrument_key"]} for r in rows]
        with self.tx() as c:
            c.executemany(
                """INSERT INTO instruments(instrument_key,exchange_token,trading_symbol,name,
                       segment,isin,lot_size,tick_size,freeze_quantity,updated_at)
                   VALUES(:instrument_key,:exchange_token,:trading_symbol,:name,:segment,
                          :isin,:lot_size,:tick_size,:freeze_quantity,:updated_at)
                   ON CONFLICT(instrument_key) DO UPDATE SET
                     trading_symbol=excluded.trading_symbol, name=excluded.name,
                     lot_size=excluded.lot_size, tick_size=excluded.tick_size,
                     freeze_quantity=excluded.freeze_quantity, updated_at=excluded.updated_at""",
                [{**r, "updated_at": now} for r in rows],
            )
        return len(rows)

    # ------------------------------------------------------------------ readers
    def daily_df(self, instrument_key: str, limit: int | None = None) -> pd.DataFrame:
        sql = """SELECT ts_date, open, high, low, close, volume FROM daily_ohlcv
                 WHERE instrument_key=? ORDER BY ts_date"""
        df = self.query_df(sql, (instrument_key,))
        if limit:
            df = df.tail(limit)
        if not df.empty:
            df["ts_date"] = pd.to_datetime(df["ts_date"])
            df = df.set_index("ts_date")
        return df

    def intraday_df(self, instrument_key: str, since: str | None = None) -> pd.DataFrame:
        sql = "SELECT ts, open, high, low, close, volume FROM intraday_5m WHERE instrument_key=?"
        params: list = [instrument_key]
        if since:
            sql += " AND ts>=?"
            params.append(since)
        sql += " ORDER BY ts"
        df = self.query_df(sql, params)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.set_index("ts")
        return df

    def latest_daily_date(self, instrument_key: str) -> str | None:
        return self.scalar(
            "SELECT MAX(ts_date) FROM daily_ohlcv WHERE instrument_key=?", (instrument_key,)
        )

    def open_positions(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM positions WHERE state IN ('PENDING_ENTRY','OPEN','T1_HIT') ORDER BY opened_at"
        )

    def log_event(self, position_id: str, event: str, detail: dict | None = None) -> None:
        self.execute(
            "INSERT INTO position_events(position_id,ts,event,detail) VALUES(?,?,?,?)",
            (position_id, _now(), event, json.dumps(detail or {}, default=str)),
        )


def _now() -> str:
    from .clock import now_ist
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")
