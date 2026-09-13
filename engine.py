"""
Phase orchestration, data ingestion, and the scan pipeline.

THE "DO NOT DIRTY THE DATABASE" RULE
====================================
The script may be started at any time, repeatedly. Every fetch is therefore gated on
what the database already holds:

  * daily bars  -- fetch only (last_stored_date + 1 session) .. today. If the stored
                   date is already today, the call is skipped entirely.
  * 5-min bars  -- fetched only for names that actually reach gate G3, and only for the
                   dip window. This is the expensive feed; fetching it for all 100 names
                   on every run is what burns the 2000-request budget for nothing.
  * every attempt is recorded in data_capture_log with its range and status, so a
                   re-run of the identical range is provably a no-op and a failed fetch
                   is visible rather than silent.

Writes are idempotent: natural primary keys plus ON CONFLICT DO UPDATE mean re-ingesting
the same range overwrites the same rows and changes nothing else.

RESTART SAFETY
==============
All trade state lives in SQLite. On startup the engine reconstructs pending and open
positions from `positions` and their armed orders from `orders`; nothing is held in
memory across a phase boundary. position_events is append-only, so the history of what
happened survives any crash.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import gates as G
from . import execution as X
from . import indicators as I
from . import sizing as S
from .clock import now_ist, resolve_phase, session_date_for
from .config import CFG, LAKH, CRORE
from . import console as con
from .dataclient import MarketDataClient, MockClient, UpstoxError
from .db import Database


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _ds(ts) -> str:
    """String form of a date expression.

    pd.Timestamp(date) - Timedelta returns a datetime.date, which has no __getitem__.
    Passing that straight to client.historical() breaks any client that slices the
    argument with [:10]. Always coerce.
    """
    return str(_d(ts))


# ===================================================================== ingestion
class Ingestor:
    """Fetches only what is missing. Every decision is logged and reported."""

    def __init__(self, db: Database, client: MarketDataClient, verbose: bool = True):
        self.db = db
        self.client = client
        self.verbose = verbose
        self.stats = {"api_calls": 0, "rows": 0, "skipped": 0}

    # ------------------------------------------------------------------ daily
    def ensure_daily(self, instrument_key: str, need_from: str, as_of: str,
                     force: bool = False) -> int:
        """Top up daily bars. Returns rows written; 0 means nothing was fetched."""
        last = self.db.latest_daily_date(instrument_key)
        start = need_from
        if last and not force:
            nxt = _ds(pd.Timestamp(last) + pd.Timedelta(days=1))
            if nxt > as_of:
                self.stats["skipped"] += 1
                return 0
            start = max(start, nxt)
        if start > as_of:
            self.stats["skipped"] += 1
            return 0
        if self.db.capture_exists("daily", instrument_key, start, as_of):
            self.stats["skipped"] += 1
            return 0

        with self.db.capture("daily", instrument_key, start, as_of) as box:
            df = self.client.historical(instrument_key, "day", start, as_of)
            self.stats["api_calls"] += 1
            if df is None or df.empty:
                box["rows"] = 0
                return 0
            rows = [{"instrument_key": instrument_key, "ts": _d(i),
                     "open": float(r.open), "high": float(r.high), "low": float(r.low),
                     "close": float(r.close), "volume": float(r.volume)}
                    for i, r in df.iterrows()]
            n = self.db.upsert_ohlcv("daily_ohlcv", "ts_date", rows)
            box["rows"] = n
            self.stats["rows"] += n
            return n

    # ------------------------------------------------------------------ intraday
    def ensure_intraday(self, instrument_key: str, from_date: str, as_of: str) -> int:
        """5-minute bars. Deliberately called only for names that reach G3."""
        if self.db.capture_exists("intraday5m", instrument_key, from_date, as_of):
            self.stats["skipped"] += 1
            return 0
        with self.db.capture("intraday5m", instrument_key, from_date, as_of) as box:
            df = self.client.historical(instrument_key, "5minute", from_date, as_of)
            self.stats["api_calls"] += 1
            if df is None or df.empty:
                box["rows"] = 0
                return 0
            rows = [{"instrument_key": instrument_key,
                     "ts": pd.Timestamp(i).strftime("%Y-%m-%d %H:%M:%S"),
                     "open": float(r.open), "high": float(r.high), "low": float(r.low),
                     "close": float(r.close), "volume": float(r.volume)}
                    for i, r in df.iterrows()]
            n = self.db.upsert_ohlcv("intraday_5m", "ts", rows)
            box["rows"] = n
            box["rows"] = n
            self.stats["rows"] += n
            return n

    # ------------------------------------------------------------------ macro
    def ensure_vix(self, as_of: str, lookback: int = 300) -> dict:
        start = _ds(pd.Timestamp(as_of) - pd.Timedelta(days=int(lookback * 1.6)))
        have = self.db.scalar("SELECT MAX(ts_date) FROM vix_daily")
        if not (have and have >= as_of):
            try:
                with self.db.capture("vix", None, start, as_of) as box:
                    df = self.client.historical("NSE_INDEX|India VIX", "day", start, as_of)
                    self.stats["api_calls"] += 1
                    if df is not None and not df.empty:
                        rows = [{"ts": _d(i), "open": float(r.open), "high": float(r.high),
                                 "low": float(r.low), "close": float(r.close)}
                                for i, r in df.iterrows()]
                        with self.db.tx() as c:
                            c.executemany(
                                """INSERT INTO vix_daily(ts_date,open,high,low,close)
                                   VALUES(:ts,:open,:high,:low,:close)
                                   ON CONFLICT(ts_date) DO UPDATE SET
                                     open=excluded.open,high=excluded.high,
                                     low=excluded.low,close=excluded.close""", rows)
                        box["rows"] = len(rows)
            except (UpstoxError, Exception) as exc:
                if self.verbose:
                    con.warn(f"India VIX unavailable from {self.client.name}: {exc}")
        # Truncate to as_of for the same reason as ensure_index: the percentile
        # overlay must use only VIX prints that existed on the session being scanned.
        df = self.db.query_df("SELECT ts_date, close FROM vix_daily WHERE ts_date<=? "
                              "ORDER BY ts_date", (str(as_of)[:10],))
        if df.empty:
            return {"close": None, "prev_close": None, "series": None, "source": "MISSING"}
        s = df["close"].astype(float).to_numpy()
        return {"close": float(s[-1]),
                "prev_close": float(s[-2]) if len(s) > 1 else None,
                "series": s[-CFG.vix_percentile_window:], "source": "db"}

    def ensure_index(self, as_of: str, lookback: int = 300) -> pd.DataFrame:
        start = _ds(pd.Timestamp(as_of) - pd.Timedelta(days=int(lookback * 1.6)))
        have = self.db.scalar("SELECT MAX(ts_date) FROM index_daily WHERE index_key=?",
                              ("NSE_INDEX|Nifty 50",))
        if not (have and have >= as_of):
            try:
                with self.db.capture("index", "NSE_INDEX|Nifty 50", start, as_of) as box:
                    df = self.client.historical("NSE_INDEX|Nifty 50", "day", start, as_of)
                    self.stats["api_calls"] += 1
                    if df is not None and not df.empty:
                        rows = [{"index_key": "NSE_INDEX|Nifty 50", "ts": _d(i),
                                 "open": float(r.open), "high": float(r.high),
                                 "low": float(r.low), "close": float(r.close),
                                 "volume": float(r.volume)} for i, r in df.iterrows()]
                        with self.db.tx() as c:
                            c.executemany(
                                """INSERT INTO index_daily(index_key,ts_date,open,high,low,
                                     close,volume) VALUES(:index_key,:ts,:open,:high,:low,
                                     :close,:volume)
                                   ON CONFLICT(index_key,ts_date) DO UPDATE SET
                                     open=excluded.open,high=excluded.high,low=excluded.low,
                                     close=excluded.close,volume=excluded.volume""", rows)
                        box["rows"] = len(rows)
            except Exception as exc:
                if self.verbose:
                    con.warn(f"Nifty 50 unavailable: {exc}")
        # Truncate to as_of. Without this, scanning a past session evaluates the index
        # trend using bars that did not exist yet -- a look-ahead leak that silently
        # invalidates both the historical scan and any backtest built on it.
        return self.db.query_df(
            "SELECT ts_date, open, high, low, close, volume FROM index_daily "
            "WHERE index_key='NSE_INDEX|Nifty 50' AND ts_date<=? ORDER BY ts_date",
            (str(as_of)[:10],))

    def universe(self) -> list[dict]:
        """Universe joined to its resolved instrument metadata.

        The universe table stores symbols only; instrument_key, lot_size and tick_size
        live in `instruments`, populated by the runtime resolution step. An earlier
        version selected from `universe` alone, so instrument_key was absent for every
        row and the scan loop's `if not key: continue` silently skipped all 98 names.
        LEFT JOIN, not INNER: an unresolved name must still be visible so it can be
        reported rather than vanishing.
        """
        rows = self.db.query(
            """SELECT u.symbol, u.sector, u.source, u.as_of,
                      i.instrument_key, i.exchange_token, i.isin, i.name,
                      COALESCE(i.lot_size, 1)  AS lot_size,
                      COALESCE(i.tick_size, 0.05) AS tick_size
               FROM universe u
               LEFT JOIN instruments i ON UPPER(i.trading_symbol) = UPPER(u.symbol)
               WHERE u.is_current = 1
               ORDER BY u.symbol""")
        out = [dict(r) for r in rows]
        missing = [r["symbol"] for r in out if not r["instrument_key"]]
        if missing and self.verbose:
            con.warn(f"{len(missing)} universe symbol(s) have no resolved instrument_key "
                     f"and will be skipped: {', '.join(missing[:8])}"
                     + (" ..." if len(missing) > 8 else "")
                     + " Run `python main.py load-universe` to resolve them.")
        return out


# ===================================================================== the engine
class Engine:
    def __init__(self, db: Database, client: MarketDataClient, mode: str = "PAPER",
                 verbose: bool = True):
        self.db = db
        self.client = client
        self.mode = mode
        self.verbose = verbose
        self.ing = Ingestor(db, client, verbose)

    # ------------------------------------------------------------------ helpers
    def _adv_shares(self, df: pd.DataFrame) -> float:
        if df is None or len(df) < 20:
            return 0.0
        return float(df["volume"].tail(20).mean())

    def _sector(self, symbol: str) -> str | None:
        return self.db.scalar("SELECT sector FROM universe WHERE symbol=?", (symbol,))

    def _events(self, symbol: str) -> pd.DataFrame:
        return self.db.query_df(
            "SELECT event_type, event_date FROM corporate_events WHERE symbol=?", (symbol,))

    def _circuit_hits(self, symbol: str, as_of: str) -> int:
        start = _ds(pd.Timestamp(as_of)
                    - pd.Timedelta(days=CFG.circuit_lookback_sessions * 2))
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM circuit_hits WHERE symbol=? AND ts_date>=? AND ts_date<=?",
            (symbol, start, as_of)) or 0)

    def _seasonal(self, instrument_key: str, as_of: str) -> np.ndarray | None:
        """Build s_m (Part 7.1) from stored 5-min bars and cache it per session."""
        cached = self.db.query(
            "SELECT slot, s_m FROM seasonal_factors WHERE instrument_key=? AND as_of=?",
            (instrument_key, as_of))
        if cached:
            arr = self._fill_slots(cached)
            return arr if arr is not None else None

        frm = _ds(pd.Timestamp(as_of) - pd.Timedelta(days=int(
            CFG.seasonal_window_sessions * 1.6)))
        intra = self.db.intraday_df(instrument_key, since=frm)
        if intra.empty or len(intra) < CFG.bars_per_session * 10:
            return None
        s = I.seasonal_factors(intra, sessions=CFG.seasonal_window_sessions,
                               bars=CFG.bars_per_session)
        if s.empty:
            return None
        rows = [{"instrument_key": instrument_key, "slot": int(k), "s_m": float(v),
                 "sample_size": len(intra) // CFG.bars_per_session, "as_of": as_of}
                for k, v in s.items()]
        with self.db.tx() as c:
            c.executemany(
                """INSERT INTO seasonal_factors(instrument_key,slot,s_m,sample_size,as_of)
                   VALUES(:instrument_key,:slot,:s_m,:sample_size,:as_of)
                   ON CONFLICT(instrument_key,slot) DO UPDATE SET s_m=excluded.s_m,
                     sample_size=excluded.sample_size, as_of=excluded.as_of""", rows)
        return self._fill_slots(rows)

    @staticmethod
    def _fill_slots(rows) -> np.ndarray | None:
        """Map slot -> s_m into a dense 75-element array, or None if incomplete.

        seasonal_factors() returns a pandas Series whose index is the slot number, so
        r["slot"] is the slot and r["s_m"] the factor. An earlier version tested
        `1 <= r["slot"] <= 75` against a Series index that was already 1..75 but read
        through the wrong key, so every lookup missed and s_m came back None for the
        whole universe. That silently disabled de-seasonalisation, and raw BNS on
        U-shaped intraday variance reports ordinary opening prints as jumps.
        """
        arr = np.full(CFG.bars_per_session, np.nan)
        n_set = 0
        for r in rows:
            try:
                slot = int(r["slot"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= slot <= CFG.bars_per_session:
                arr[slot - 1] = float(r["s_m"])
                n_set += 1
        if n_set < CFG.bars_per_session:
            return None
        if not np.isfinite(arr).all() or not (arr > 0).all():
            return None
        return arr

    # ------------------------------------------------------------------ scan
    def scan(self, as_of: str, run_id: str | None = None,
             limit: int | None = None) -> dict:
        """Run the full cascade over the universe. Returns the run summary."""
        # session_date_for() returns a datetime.date, which has no __getitem__; every
        # client slices these arguments with [:10]. Coerce once at the boundary.
        as_of = _d(as_of)
        run_id = run_id or f"SCAN-{pd.Timestamp(as_of):%Y%m%d}-{int(time.time())}"
        mode_name = self.mode
        started = _d(now_ist()) + " " + now_ist().strftime("%H:%M:%S")

        uni = self.ing.universe()
        if limit:
            uni = uni[:limit]
        con.banner([f"SCAN {as_of}",
            f"run {run_id} | {mode_name} | universe {len(uni)} names | "
            f"data via {self.client.name}"])

        # ---------- G0 macro, once per session ----------
        vix = self.ing.ensure_vix(as_of)
        idx = self.ing.ensure_index(as_of)
        idx_df = None
        if not idx.empty:
            idx_df = idx.copy()
            idx_df["ts_date"] = pd.to_datetime(idx_df["ts_date"])
            idx_df = idx_df.set_index("ts_date")
        macro_row = self.db.one("SELECT * FROM macro_obs WHERE ts_date<=? ORDER BY ts_date "
                                "DESC LIMIT 1", (as_of,))
        macro = dict(macro_row) if macro_row else {}
        breadth = self._breadth(as_of)
        g0 = G.g0_macro(vix, idx_df, breadth, macro)

        self.db.execute(
            """INSERT OR REPLACE INTO scan_runs (run_id, session_date, phase, macro_tier,
               macro_detail, universe_size, candidates, selected, status, started_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, as_of, resolve_phase(now_ist()), g0.detail.get("tier"),
             g0.justification[:1000], len(uni), 0, 0, "RUNNING", started, mode_name))
        self.db.execute(
            "INSERT OR REPLACE INTO gate_results (run_id, instrument_key, gate, passed, "
            "verdict, value, detail) VALUES (?,?,?,?,?,?,?)",
            (run_id, "__MACRO__", "G0_MACRO", int(g0.passed), g0.verdict, g0.value,
             json.dumps(g0.detail, default=str)))

        con.show_gate(g0)
        tier = g0.detail.get("tier", "DEGRADED")
        max_names = int(g0.detail.get("max_names") or 0)
        excluded = set(g0.detail.get("excluded_sectors") or [])

        survivors: list[dict] = []
        funnel = {"G1": 0, "G2": 0, "G3": 0, "G4": 0, "CONFLUENCE": 0}

        if not g0.passed:
            con.verdict_block("MACRO GATE CLOSED",
                        "No new entries this session. Open positions keep running their "
                        "own ladder (Part XIV P4).")
            self._finish_run(run_id, as_of, 0, 0, "G0_BLOCKED")
            return {"run_id": run_id, "candidates": [], "g0": g0, "funnel": funnel}

        con.head("Scanning names")
        need_from = _ds(pd.Timestamp(as_of) - pd.Timedelta(days=600))

        for n, u in enumerate(uni, start=1):
            sym = u["symbol"]
            key = u.get("instrument_key")
            if not key:
                continue
            sector = self._sector(sym)

            # -- cheap gate first: tradability on daily bars only --
            self.ing.ensure_daily(key, need_from, as_of)
            daily = self.db.daily_df(key)
            if daily.empty or len(daily) < 60:
                continue

            g1 = G.g1_tradable(daily, u, self._events(sym),
                               self._circuit_hits(sym, as_of), as_of,
                               self._adv_shares(daily))
            self._record_gate(run_id, key, g1)
            if not g1.passed:
                if self.verbose and n <= 200:
                    con.show_gate(g1, prefix=f"{sym:12s}")
                continue
            funnel["G1"] += 1

            if sector and sector.upper() in excluded:
                con.show_gate(G.GateResult("G1_SECTOR", False, "REJ_SECTOR",
                                           headline=f"{sym} sector {sector} excluded by "
                                                    f"the 2026 macro overlay",
                                           justification=g0.justification),
                              prefix=f"{sym:12s}")
                continue

            # -- G2 trend --
            g2 = G.g2_trend(daily)
            self._record_gate(run_id, key, g2)
            if not g2.passed:
                con.show_gate(g2, prefix=f"{sym:12s}")
                continue
            funnel["G2"] += 1

            # -- G3 jump: this is the first point at which 5-min data is worth paying for --
            dip_from = _ds(pd.Timestamp(as_of) - pd.Timedelta(days=14))
            try:
                self.ing.ensure_intraday(key, dip_from, as_of)
            except UpstoxError as exc:
                con.show_gate(G.GateResult("G3_JUMP", False, "REJ_JUMP_INTRADAY",
                                           headline=f"{sym} no intraday data",
                                           justification=str(exc)),
                              prefix=f"{sym:12s}")
                continue
            intra = self.db.intraday_df(key, since=dip_from + " 00:00:00")
            s_m = self._seasonal(key, as_of)
            g3 = G.g3_jump(intra, daily, s_m)
            self._record_gate(run_id, key, g3)
            if not g3.passed:
                con.show_gate(g3, prefix=f"{sym:12s}")
                continue
            funnel["G3"] += 1

            # -- G4 stationarity --
            g4 = G.g4_stationarity(daily)
            self._record_gate(run_id, key, g4)
            if not g4.passed:
                con.show_gate(g4, prefix=f"{sym:12s}")
                continue
            funnel["G4"] += 1

            # -- scored confluence S1-S4 --
            kalman_price = float(g2.detail.get("kalman_price") or np.nan)
            s1 = G.s1_pullback(daily, kalman_price, tier)
            s2 = G.s2_halflife(daily)
            s3 = G.s3_volume(daily)
            s4 = G.s4_avwap(daily)
            for sres in (s1, s2, s3, s4):
                self._record_gate(run_id, key, sres)
            pts = sum(1 for sres in (s1, s2, s3, s4) if sres.passed)
            funnel["CONFLUENCE"] += 1

            con.info(f"{sym:12s} confluence {pts}/4",
                     f"H={g2.detail['hurst']:.3f} tau={s2.value if s2.value else float('nan'):.2f}d "
                     f"Z={s1.value:+.2f} V={s3.value:.2f} | "
                     + " ".join(f"{x.gate.split('_')[0]}:{'+'if x.passed else'-'}"
                                for x in (s1, s2, s3, s4)))

            if pts < CFG.confluence_min_points:
                self.db.execute(
                    """INSERT OR REPLACE INTO candidates (run_id, instrument_key, symbol,
                       confluence_pts, decision, reject_code, reason) VALUES (?,?,?,?,?,?,?)""",
                    (run_id, key, sym, pts, "REJECTED", "REJ_CONFLUENCE",
                     f"only {pts}/4 confluence points; {CFG.confluence_min_points} required"))
                continue

            # S4 staleness is a discard, not a lost point
            if s4.verdict == "REJ_STALE_ANCHOR":
                self.db.execute(
                    """INSERT OR REPLACE INTO candidates (run_id, instrument_key, symbol,
                       confluence_pts, decision, reject_code, reason) VALUES (?,?,?,?,?,?,?)""",
                    (run_id, key, sym, pts, "REJECTED", "REJ_STALE_ANCHOR", s4.justification))
                con.show_gate(s4, prefix=f"{sym:12s}")
                continue

            last = daily.iloc[-1]
            atr14 = float(I.atr(daily, CFG.atr_window).iloc[-1])
            entry = X.build_entry(float(last["high"]), atr14)
            low_dip = float(daily["low"].tail(10).min())
            avwap = float(s4.detail.get("avwap") or last["close"])
            stop = X.build_stop(low_dip, avwap, atr14)
            R = entry["trigger"] - stop
            if R <= 0 or R > CFG.max_r_atr_mult * atr14:
                self.db.execute(
                    """INSERT OR REPLACE INTO candidates (run_id, instrument_key, symbol,
                       confluence_pts, decision, reject_code, reason) VALUES (?,?,?,?,?,?,?)""",
                    (run_id, key, sym, pts, "REJECTED", "REJ_STOP_TOO_FAR",
                     f"R={R:.2f} vs 1.25 x ATR14 = {CFG.max_r_atr_mult*atr14:.2f}"))
                con.show_gate(G.GateResult("RISK", False, "REJ_STOP_TOO_FAR",
                                           headline=f"{sym} stop too far",
                                           justification=f"R {R:.2f} > 1.25 x ATR14 "
                                                         f"{CFG.max_r_atr_mult*atr14:.2f}: "
                                                         f"entry too extended, b no longer "
                                                         f"supports the trade"),
                              prefix=f"{sym:12s}")
                continue

            survivors.append({
                "instrument_key": key, "symbol": sym, "sector": sector,
                "session_date": as_of, "confluence_pts": pts,
                "hurst": float(g2.detail["hurst"]),
                "tau_halflife": float(s2.value) if np.isfinite(s2.value or np.nan) else float("nan"),
                "z_garch": float(s1.value), "v_ratio": float(s3.value),
                "trigger_price": entry["trigger"], "limit_price": entry["limit"],
                "stop_price": stop, "r_value": R, "atr14": atr14,
                "target1_price": round(max(kalman_price, float(last["close"])), 2),
                "target2_price": round(entry["trigger"] + CFG.target2_r_mult * R, 2),
                "adv20_value": float(g1.value), "adv_shares": self._adv_shares(daily),
                "lot_size": int(u.get("lot_size") or 1),
                "kalman_price": kalman_price, "avwap": avwap,
                "s1": s1.detail, "s2": s2.detail, "s3": s3.detail, "s4": s4.detail,
                "g2": g2.detail,
                "reason": "; ".join(x.justification for x in (s1, s2, s3, s4)
                                    if x.passed)[:600],
            })

        con.head(f"Funnel: {len(uni)} -> G1 {funnel['G1']} -> G2 {funnel['G2']} -> "
                 f"G3 {funnel['G3']} -> G4 {funnel['G4']} -> scored {funnel['CONFLUENCE']} "
                 f"-> qualified {len(survivors)}")

        ranked = G.rank_score(survivors)
        return self._select_and_arm(run_id, as_of, ranked, tier, max_names, funnel)

    # ------------------------------------------------------------------ selection
    def _select_and_arm(self, run_id, as_of, ranked, tier, max_names, funnel) -> dict:
        if not ranked:
            con.verdict_block("NO CANDIDATES",
                        "Nothing cleared all four mandatory gates plus 3/4 confluence. "
                        "That is the expected outcome on most sessions in a -13.7% YTD "
                        "year; the gate is the product.")
            self._finish_run(run_id, as_of, 0, 0, "NO_CANDIDATES")
            return {"run_id": run_id, "candidates": [], "funnel": funnel}

        con.head(f"Ranked candidates ({len(ranked)})")
        con.make_table([["#", "symbol", "pts", "score", "H", "tau", "Z", "ADV Cr"],
                   *[[str(r["rank"]), r["symbol"], str(r["confluence_pts"]),
                      f"{r['score']:+.3f}", f"{r['hurst']:.3f}",
                      f"{r['tau_halflife']:.2f}", f"{r['z_garch']:+.2f}",
                      f"{r['adv20_value']/CRORE:.1f}"] for r in ranked]])

        equity = self._equity()
        cash = self._unencumbered(equity)
        open_pos = [dict(p) for p in self.db.open_positions()]
        heat = sum(float(p.get("risk_rupees") or 0.0) for p in open_pos)
        n_open = len(open_pos)
        armed: list[dict] = []

        for r in ranked:
            if len(armed) + n_open >= max_names:
                con.warn(f"concurrency cap {max_names} reached in tier {tier}; "
                         f"{len(ranked) - len(armed)} qualified names stand down")
                break
            if not S.concurrency_ok(len(armed) + n_open, tier):
                break
            if r.get("sector") and not S.sector_slots(
                    [{**p, "sector": self._sector(p.get("symbol") or "")} for p in open_pos]
                    + [{**a, "sector": a.get("sector")} for a in armed], r["sector"]):
                con.info(f"{r['symbol']:12s} SKIP",
                         f"already holding {r['sector']} (Part XIV P1: max "
                         f"{CFG.max_per_sector} per sector)")
                continue

            sd = S.size_position(
                entry=r["trigger_price"], stop=r["stop_price"], equity=equity,
                unencumbered_cash=cash, adv_shares=r["adv_shares"],
                lot_size=r["lot_size"], segment="futures",
                sigma_t=float(r["s1"].get("sigma_t") or 0.0) or None,
                expected_move=2.5 * r["r_value"], tier=tier,
                open_heat_rupees=heat)

            if not sd.ok:
                self.db.execute(
                    """INSERT OR REPLACE INTO candidates (run_id, instrument_key, symbol,
                       confluence_pts, score, rank, decision, reject_code, reason,
                       trigger_price, stop_price, r_value, b_net, prob_p, payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, r["instrument_key"], r["symbol"], r["confluence_pts"],
                     r["score"], r["rank"], "REJECTED", sd.reject_code, sd.reason,
                     r["trigger_price"], r["stop_price"], r["r_value"], sd.b_net,
                     sd.prob_p, json.dumps(r, default=str)))
                con.show_gate(G.GateResult("SIZE", False, sd.reject_code,
                                           headline=f"{r['symbol']} not sized",
                                           justification=sd.reason),
                              prefix=f"{r['symbol']:12s}")
                continue

            if not S.sector_exposure_ok(
                    [{**p, "sector": self._sector(p.get("symbol") or ""),
                      "notional": float(p.get("qty") or 0) * float(p.get("entry_price") or 0)}
                     for p in open_pos], r["sector"], sd.notional, equity):
                con.info(f"{r['symbol']:12s} SKIP", "sector exposure would exceed 25%")
                continue

            r.update({"lots": sd.lots, "shares": sd.shares, "risk_rupees": sd.risk_rupees,
                      "prob_p": sd.prob_p, "b_net": sd.b_net, "score": r["score"],
                      "rank": r["rank"], "notional": sd.notional,
                      "time_stop_days": S.time_stop_days(r["tau_halflife"]),
                      "reason": r["reason"] + " || " + sd.reason})
            res = X.arm_entry(self.db, run_id, r, self.mode)
            heat += sd.risk_rupees
            cash -= sd.margin_required
            armed.append(r)
            con.ok(f"{r['symbol']:12s} ARMED",
                   f"{sd.lots} lots @ trig {r['trigger_price']:,.2f} stop "
                   f"{r['stop_price']:,.2f} R={r['r_value']:.2f} "
                   f"risk {sd.risk_rupees:,.0f} | T2 {r['target2_price']:,.2f} | "
                   f"time stop {r['time_stop_days']}d")
            self.db.execute(
                """INSERT OR REPLACE INTO candidates (run_id, instrument_key, symbol,
                   confluence_pts, score, rank, decision, trigger_price, stop_price,
                   r_value, b_net, prob_p, lots, reason, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, r["instrument_key"], r["symbol"], r["confluence_pts"], r["score"],
                 r["rank"], "SELECTED", r["trigger_price"], r["stop_price"], r["r_value"],
                 sd.b_net, sd.prob_p, sd.lots, r["reason"], json.dumps(r, default=str)))

        self._finish_run(run_id, as_of, len(ranked), len(armed), "OK")
        con.verdict_block(f"{len(armed)} ORDER(S) ARMED" if armed else "NOTHING ARMED",
                    f"qualified {len(ranked)}, armed {len(armed)} under tier {tier} "
                    f"(cap {max_names}). Orders are GFD stop-limits; they hard-cancel at "
                    f"15:20 on day t+1 if unfilled (Part 12.2).")
        return {"run_id": run_id, "candidates": armed, "ranked": ranked, "funnel": funnel}

    # ------------------------------------------------------------------ book
    def _equity(self) -> float:
        stored = self.db.kv_get("equity")
        if stored:
            return float(stored)
        return float(CFG.paper_equity)

    def _unencumbered(self, equity: float) -> float:
        used = self.db.scalar(
            "SELECT COALESCE(SUM(qty*entry_price),0) FROM positions "
            "WHERE state IN ('OPEN','T1_HIT')") or 0.0
        return max(0.0, float(CFG.paper_unencumbered_cash) - float(used) * CFG.margin_rate)

    def _breadth(self, as_of: str) -> dict:
        rows = self.db.query_df(
            """SELECT u.symbol, d.instrument_key FROM universe u
               JOIN instruments i ON i.trading_symbol=u.symbol
               JOIN (SELECT DISTINCT instrument_key FROM daily_ohlcv) d
                 ON d.instrument_key=i.instrument_key
               WHERE u.is_current=1""")
        if rows.empty:
            return {"pct_above_200dma": None, "n": 0}
        above = 0
        tot = 0
        for _, r in rows.iterrows():
            df = self.db.daily_df(r["instrument_key"], limit=CFG.breadth_ema + 5)
            if len(df) < CFG.breadth_ema:
                continue
            tot += 1
            sma = float(df["close"].tail(CFG.breadth_ema).mean())
            if float(df["close"].iloc[-1]) > sma:
                above += 1
        return {"pct_above_200dma": (above / tot) if tot else None, "n": tot}

    def _record_gate(self, run_id, key, res) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO gate_results (run_id, instrument_key, gate, passed,
               verdict, value, detail) VALUES (?,?,?,?,?,?,?)""",
            (run_id, key, res.gate, int(res.passed), res.verdict, res.value,
             json.dumps(res.detail, default=str)))

    def _finish_run(self, run_id, as_of, n_cand, n_sel, status) -> None:
        self.db.execute(
            """UPDATE scan_runs SET candidates=?, selected=?, status=?, finished_at=?,
               notes=COALESCE(notes,'')||? WHERE run_id=?""",
            (n_cand, n_sel, status,
             _d(now_ist()) + " " + now_ist().strftime("%H:%M:%S"),
             f" | api_calls={self.ing.stats['api_calls']} rows={self.ing.stats['rows']} "
             f"skipped={self.ing.stats['skipped']}", run_id))
        self.db.kv_set("last_run_id", run_id)

    # ------------------------------------------------------------------ manage
    def manage_open_positions(self, as_of: str) -> dict:
        """Walk the exit ladder for every open position. Restart-safe."""
        as_of = _d(as_of)
        pos = [dict(p) for p in self.db.open_positions()]
        if not pos:
            con.info("positions", "none open")
            return {"managed": 0}
        con.banner(["POSITION MANAGEMENT",
            f"{len(pos)} open/pending | {as_of} | {self.mode}"])
        acted = 0
        for p in pos:
            key = p["instrument_key"]
            sym = p["symbol"] or "?"
            daily = self.db.daily_df(key)
            if daily.empty:
                con.warn(f"{sym}: no daily bars stored, cannot evaluate the ladder")
                continue

            if p["state"] == "PENDING_ENTRY":
                self._check_entry(p, daily, as_of)
                continue

            last = daily.iloc[-1]
            held = int(p.get("sessions_held") or self._sessions_held(p, daily))
            sig = X.evaluate_ladder(p, daily, float(last["low"]), float(last["high"]),
                                    float(last["close"]), held)
            if sig.action == "HOLD":
                con.info(f"{sym:12s} HOLD", sig.reason)
                continue
            price = (float(last["close"]) if sig.action in ("TIME_STOP", "TRAIL_EXIT")
                     else (p["stop_price"] if sig.action == "STOP_OUT"
                           else float(last["high"])))
            res = X.apply_exit(self.db, p["position_id"], sig, price, self.mode)
            acted += 1
            con.ok(f"{sym:12s} {sig.action}",
                   f"rule {sig.rule}: {res.get('qty')} sh @ {price:,.2f} -> "
                   f"P&L {res.get('realized', 0):,.0f} "
                   f"({res.get('realized_r', 0):+.2f}R) | {sig.reason}")
        self._snapshot(as_of)
        return {"managed": acted}

    def _check_entry(self, p: dict, daily: pd.DataFrame, as_of: str) -> None:
        """GAP-CANCEL at 09:08, fill if the trigger is breached, else hard-cancel at 15:20."""
        oid = self.db.scalar(
            "SELECT order_id FROM orders WHERE position_id=? AND status IN "
            "('ARMED','PLACED') ORDER BY created_at DESC LIMIT 1", (p["position_id"],))
        if not oid:
            return
        last = daily.iloc[-1]
        armed_on = pd.Timestamp(str(p["opened_at"])[:10])
        today = pd.Timestamp(as_of)

        if today > armed_on:
            gap, why = X.gap_cancel(float(last["open"]), float(p["entry_price"]) /
                                    (1 + CFG.entry_trigger_pct), float(p.get("atr14") or 0.0))
            if gap:
                X.cancel_order(self.db, oid, self.client, self.mode, why)
                con.warn(f"{p['symbol']:12s} CANCEL_GAP", why)
                return
            if (today - armed_on).days > 1:
                X.cancel_order(self.db, oid, self.client, self.mode,
                               "CANCEL_GFD: order expired unfilled. A trigger on day t+2 "
                               "is trading a decayed OU half-life; the edge has expired "
                               "(Part 12.2)")
                con.warn(f"{p['symbol']:12s} CANCEL_GFD", "hard-cancelled at day t+1")
                return

        if float(last["high"]) >= float(p["entry_price"]):
            fill = max(float(p["entry_price"]), float(last["open"]))
            X.place_or_fill(self.db, oid, self.client, fill, self.mode)
            con.ok(f"{p['symbol']:12s} FILLED",
                   f"{p['qty'] or ''} @ {fill:,.2f} stop {p['stop_price']:,.2f}")
        else:
            con.info(f"{p['symbol']:12s} waiting",
                     f"high {last['high']:,.2f} < trigger {p['entry_price']:,.2f}")

    def _sessions_held(self, p: dict, daily: pd.DataFrame) -> int:
        opened = str(p.get("opened_at") or "")[:10]
        if not opened:
            return 0
        try:
            idx = daily.index
            return int((idx >= pd.Timestamp(opened)).sum())
        except Exception:
            return 0

    def _snapshot(self, as_of: str) -> None:
        eq = self._equity()
        rows = self.db.query(
            "SELECT qty, entry_price, risk_rupees FROM positions WHERE state IN "
            "('OPEN','T1_HIT')")
        heat = sum(float(r["risk_rupees"] or 0) for r in rows)
        realized = self.db.scalar("SELECT COALESCE(SUM(realized_pnl),0) FROM positions") or 0
        self.db.execute(
            """INSERT INTO portfolio_snapshots (ts, mode, equity, unencumbered_cash,
               collateral, positions_open, heat_pct, macro_tier) VALUES (?,?,?,?,?,?,?,?)""",
            (_d(now_ist()) + " " + now_ist().strftime("%H:%M:%S"), self.mode,
             eq + float(realized), self._unencumbered(eq), 0.0, len(rows),
             heat / eq if eq else 0.0,
             self.db.scalar("SELECT macro_tier FROM scan_runs ORDER BY started_at DESC "
                            "LIMIT 1")))

    # ------------------------------------------------------------------ report
    def report(self, run_id: str | None = None) -> None:
        """Human-readable justification for every decision in a run."""
        run_id = run_id or self.db.kv_get("last_run_id")
        if not run_id:
            con.warn("no scan run recorded yet")
            return
        run = self.db.one("SELECT * FROM scan_runs WHERE run_id=?", (run_id,))
        if not run:
            con.warn(f"run {run_id} not found")
            return
        con.banner([f"RUN {run_id}",
                    f"{run['session_date']} | tier {run['macro_tier']} | "
                    f"universe {run['universe_size']} | qualified {run['candidates']} | "
                    f"selected {run['selected']} | {run['status']}"])
        con.head("Macro gate G0")
        con.wrap(run["macro_detail"] or "(no detail)")

        rows = self.db.query(
            "SELECT symbol, decision, confluence_pts, reject_code, reason FROM candidates "
            "WHERE run_id=? ORDER BY decision DESC, confluence_pts DESC, symbol",
            (run_id,))
        con.make_table([["symbol", "decision", "pts", "code", "reason"],
                   *[[r["symbol"] or "?", r["decision"], str(r["confluence_pts"] or 0),
                      r["reject_code"] or "-", (r["reason"] or "")[:110]]
                     for r in rows]])

        con.head("Gate funnel")
        for g in self.db.query(
                "SELECT gate, SUM(passed) p, COUNT(*) n FROM gate_results WHERE run_id=? "
                "AND instrument_key!='__MACRO__' GROUP BY gate ORDER BY gate", (run_id,)):
            con.info(f"{g['gate']:16s}", f"{g['p']}/{g['n']} passed")

        con.head("Open book")
        pos = self.db.query(
            "SELECT symbol, state, lots, entry_price, stop_price, r_value, realized_pnl, "
            "realized_r FROM positions ORDER BY state, symbol")
        if pos:
            con.make_table([["symbol", "state", "lots", "entry", "stop", "R", "P&L", "R mult"],
                       *[[p["symbol"] or "?", p["state"], str(p["lots"] or 0),
                          f"{p['entry_price'] or 0:,.2f}", f"{p['stop_price'] or 0:,.2f}",
                          f"{p['r_value'] or 0:,.2f}", f"{p['realized_pnl'] or 0:,.0f}",
                          f"{p['realized_r'] or 0:+.2f}"] for p in pos]])
        else:
            con.info("book", "no positions recorded")
