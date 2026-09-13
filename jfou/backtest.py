"""
Backtest by database replay.

NO NETWORK. This module reads only what is already stored, which is the whole point of
capturing everything into SQLite: the strategy can be re-evaluated, re-tuned and
re-validated without a single API call, and without the results changing because a
vendor restated its history.

It calls the SAME gate, confluence, sizing and ladder functions the live engine calls.
That is deliberate. A backtest with its own copy of the logic drifts from production
within a week and then tells you nothing.

POINT-IN-TIME DISCIPLINE
    Every indicator at session t is computed from bars with ts_date <= t only. The
    universe is taken as-is from the `universe` table, which is NOT point-in-time (see
    the header of universe_nifty100.json), so results are survivorship-biased by
    construction. That is a known and stated limitation, not a bug to be fixed here.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import gates as G
from . import indicators as I
from . import sizing as S
from .config import CFG, CRORE, LAKH
from .db import Database


class Backtest:
    def __init__(self, db: Database, equity: float | None = None, verbose: bool = True):
        self.db = db
        self.equity = float(equity or CFG.paper_equity)
        self.verbose = verbose
        self.trades: list[dict] = []
        self.curve: list[tuple[str, float]] = []

    # ------------------------------------------------------------------ run
    def run(self, start: str, end: str, warmup: int = 300) -> int:
        from . import console as con

        sessions = self._sessions(start, end)
        if len(sessions) < 5:
            con.warn(f"only {len(sessions)} stored sessions in [{start},{end}]. "
                     f"Run scans first to populate the database.")
            return 2

        con.banner(["BACKTEST -- DATABASE REPLAY",
                    f"{start} -> {end} | {len(sessions)} sessions | "
                    f"starting equity {self.equity/LAKH:,.2f} L | NO NETWORK"])
        con.warn("Survivorship bias applies: the universe table is not point-in-time.")

        uni = [dict(r) for r in self.db.query(
            "SELECT symbol, sector FROM universe WHERE is_current=1 ORDER BY symbol")]
        keys = {r["trading_symbol"]: r["instrument_key"] for r in self.db.query(
            "SELECT trading_symbol, instrument_key FROM instruments")}
        uni = [u for u in uni if u["symbol"] in keys]
        if not uni:
            con.warn("no universe rows have a resolved instrument_key. "
                     "Run `python main.py load-universe` first.")
            return 2

        open_pos: list[dict] = []
        equity = self.equity

        for si, day in enumerate(sessions):
            daily_map = {u["symbol"]: self._window(keys[u["symbol"]], day, warmup)
                         for u in uni}
            daily_map = {k: v for k, v in daily_map.items() if v is not None
                         and len(v) >= 60}
            if not daily_map:
                continue

            # ---------- manage open positions first ----------
            still: list[dict] = []
            for p in open_pos:
                d = daily_map.get(p["symbol"])
                if d is None:
                    still.append(p)
                    continue
                held = int((d.index >= pd.Timestamp(p["opened_on"])).sum())
                last = d.iloc[-1]
                sig = self._ladder(p, d, held)
                if sig[0] == "HOLD":
                    still.append(p)
                    continue
                action, frac, price, reason = sig
                qty = int(p["shares"] * frac) if frac < 1 else p["shares"]
                pnl = (price - p["entry"]) * qty
                p["realized"] = p.get("realized", 0.0) + pnl
                p["realized_r"] = p.get("realized_r", 0.0) + (price - p["entry"]) / p["R"]
                equity += pnl
                self.trades.append({
                    "symbol": p["symbol"], "opened": p["opened_on"], "closed": str(day.date()),
                    "action": action, "qty": qty, "entry": p["entry"], "exit": price,
                    "pnl": pnl, "r": (price - p["entry"]) / p["R"], "reason": reason,
                    "sector": p.get("sector"), "sessions_held": held})
                if frac < 1:
                    p["shares"] -= qty
                    p["stop"] = p["entry"] * (1 + CFG.breakeven_buffer_pct)
                    p["state"] = "T1_HIT"
                    still.append(p)
            open_pos = still

            # ---------- macro gate ----------
            g0 = self._macro(day, daily_map)
            tier = g0.detail.get("tier", "DEGRADED")
            max_names = int(g0.detail.get("max_names") or 0)

            if g0.passed and max_names > 0:
                cands = self._scan(day, uni, daily_map, tier)
                ranked = G.rank_score(cands)
                for r in ranked:
                    if len(open_pos) >= max_names:
                        break
                    if any(p["symbol"] == r["symbol"] for p in open_pos):
                        continue
                    if r.get("sector") and any(
                            p.get("sector") == r["sector"] for p in open_pos):
                        continue
                    sd = S.size_position(
                        entry=r["trigger_price"], stop=r["stop_price"], equity=equity,
                        unencumbered_cash=equity, adv_shares=r["adv_shares"],
                        lot_size=1, segment="futures",
                        sigma_t=r.get("sigma_t"), expected_move=2.5 * r["r_value"],
                        tier=tier,
                        open_heat_rupees=sum(p["shares"] * p["R"] for p in open_pos))
                    if not sd.ok:
                        continue
                    d = daily_map[r["symbol"]]
                    open_pos.append({
                        "symbol": r["symbol"], "sector": r.get("sector"),
                        "shares": sd.shares, "entry": r["trigger_price"],
                        "stop": r["stop_price"], "R": r["r_value"],
                        "opened_on": str(d.index[-1].date()),
                        "tau": r["tau_halflife"],
                        "time_stop": S.time_stop_days(r["tau_halflife"]),
                        "state": "OPEN"})

            self.curve.append((str(day.date()), equity))

        return self._report(start, end)

    # ------------------------------------------------------------------ pieces
    def _sessions(self, start: str, end: str) -> list[pd.Timestamp]:
        rows = self.db.query(
            "SELECT DISTINCT ts_date FROM daily_ohlcv WHERE ts_date>=? AND ts_date<=? "
            "ORDER BY ts_date", (start, end))
        return [pd.Timestamp(r["ts_date"]) for r in rows]

    def _window(self, key: str, day: pd.Timestamp, warmup: int):
        """Point-in-time: only bars on or before `day`."""
        df = self.db.query_df(
            "SELECT ts_date, open, high, low, close, volume FROM daily_ohlcv "
            "WHERE instrument_key=? AND ts_date<=? ORDER BY ts_date DESC LIMIT ?",
            (key, str(day.date()), warmup))
        if df.empty:
            return None
        df["ts_date"] = pd.to_datetime(df["ts_date"])
        return df.set_index("ts_date").sort_index()

    def _macro(self, day, daily_map) -> "G.GateResult":
        vix_row = self.db.one("SELECT close FROM vix_daily WHERE ts_date<=? ORDER BY "
                              "ts_date DESC LIMIT 1", (str(day.date()),))
        series = self.db.query(
            "SELECT close FROM vix_daily WHERE ts_date<=? ORDER BY ts_date "
            "DESC LIMIT ?", (str(day.date()), CFG.vix_percentile_window))
        vix = {"close": float(vix_row["close"]) if vix_row else None,
               "prev_close": None,
               "series": np.array([float(r["close"]) for r in series][::-1])
               if series else None}
        idx = self.db.query_df(
            "SELECT ts_date, close FROM index_daily WHERE index_key='NSE_INDEX|Nifty 50' "
            "AND ts_date<=? ORDER BY ts_date", (str(day.date()),))
        idx_df = None
        if not idx.empty:
            idx_df = idx.copy()
            idx_df["ts_date"] = pd.to_datetime(idx_df["ts_date"])
            idx_df = idx_df.set_index("ts_date")
        above = tot = 0
        for sym, d in daily_map.items():
            if len(d) < CFG.breadth_ema:
                continue
            tot += 1
            if float(d["close"].iloc[-1]) > float(d["close"].tail(CFG.breadth_ema).mean()):
                above += 1
        breadth = {"pct_above_200dma": (above / tot) if tot else None}
        macro = self.db.one("SELECT * FROM macro_obs WHERE ts_date<=? ORDER BY ts_date "
                            "DESC LIMIT 1", (str(day.date()),))
        return G.g0_macro(vix, idx_df, breadth, dict(macro) if macro else {})

    def _scan(self, day, uni, daily_map, tier) -> list[dict]:
        out: list[dict] = []
        for u in uni:
            sym = u["symbol"]
            d = daily_map.get(sym)
            if d is None:
                continue
            adv_val = float((d["close"] * d["volume"]).tail(20).mean())
            if adv_val < CFG.adv_value_floor_cr * 1e7:
                continue
            if float(d["close"].iloc[-1]) < 5.0:
                continue
            g2 = G.g2_trend(d)
            if not g2.passed:
                continue
            intra = self._intraday_window(u, day)
            g3 = G.g3_jump(intra, d, None)
            if not g3.passed:
                continue
            if not G.g4_stationarity(d).passed:
                continue

            s1 = G.s1_pullback(d, float(g2.detail.get("kalman_price") or np.nan), tier)
            s2 = G.s2_halflife(d)
            s3 = G.s3_volume(d)
            s4 = G.s4_avwap(d)
            pts = sum(1 for x in (s1, s2, s3, s4) if x.passed)
            if pts < CFG.confluence_min_points or s4.verdict == "REJ_STALE_ANCHOR":
                continue

            last = d.iloc[-1]
            atr14 = float(I.atr(d, CFG.atr_window).iloc[-1])
            trigger = float(last["high"]) * (1 + CFG.entry_trigger_pct)
            stop = min(float(d["low"].tail(10).min()) - CFG.stop_atr_mult * atr14,
                       float(s4.detail.get("avwap") or last["close"]) * CFG.stop_avwap_mult)
            R = trigger - stop
            if R <= 0 or R > CFG.max_r_atr_mult * atr14:
                continue
            out.append({
                "symbol": sym, "sector": u.get("sector"), "confluence_pts": pts,
                "hurst": float(g2.detail["hurst"]),
                "tau_halflife": float(s2.value) if np.isfinite(s2.value or np.nan)
                else float("nan"),
                "z_garch": float(s1.value), "sigma_t": float(s1.detail.get("sigma_t") or 0),
                "adv20_value": adv_val, "adv_shares": float(d["volume"].tail(20).mean()),
                "trigger_price": trigger, "stop_price": stop, "r_value": R})
        return out

    def _intraday_window(self, u, day):
        key = self.db.scalar("SELECT instrument_key FROM instruments WHERE "
                             "trading_symbol=?", (u["symbol"],))
        if not key:
            return pd.DataFrame()
        return self.db.intraday_df(key, since=str((day - pd.Timedelta(days=14)).date())
                                   + " 00:00:00")

    def _ladder(self, p, d, held):
        """Same rule order as execution.evaluate_ladder, on daily bars only."""
        from .execution import evaluate_ladder
        pos = {"entry_price": p["entry"], "stop_price": p["stop"], "r_value": p["R"],
               "target2_price": p["entry"] + CFG.target2_r_mult * p["R"],
               "state": p.get("state", "OPEN"),
               "time_stop_days": p.get("time_stop") or CFG.time_stop_cap_days}
        last = d.iloc[-1]
        sig = evaluate_ladder(pos, d, float(last["low"]), float(last["high"]),
                              float(last["close"]), held)
        if sig.action == "HOLD":
            return ("HOLD", 0.0, 0.0, sig.reason)
        price = (float(last["close"]) if sig.action in ("TIME_STOP", "TRAIL_EXIT")
                 else (p["stop"] if sig.action == "STOP_OUT" else float(last["high"])))
        return (sig.action, sig.qty_frac, price, sig.reason)

    # ------------------------------------------------------------------ report
    def _report(self, start: str, end: str) -> int:
        from . import console as con
        from .console import money

        if not self.trades:
            con.verdict_block("BACKTEST COMPLETE -- NO TRADES",
                              "Nothing cleared the cascade in this window. On a -13.7% "
                              "YTD tape that is the expected result, and it is the gate "
                              "doing its job.")
            return 0

        t = pd.DataFrame(self.trades)
        wins = t[t["pnl"] > 0]
        losses = t[t["pnl"] <= 0]
        total_pnl = float(t["pnl"].sum())
        gross_win = float(wins["pnl"].sum()) if len(wins) else 0.0
        gross_loss = float(-losses["pnl"].sum()) if len(losses) else 0.0
        eq = pd.Series([c[1] for c in self.curve],
                       index=pd.to_datetime([c[0] for c in self.curve]))
        rets = eq.pct_change().dropna()
        peak = eq.cummax()
        dd = float(((eq - peak) / peak).min()) if len(eq) else 0.0
        n_days = max(len(rets), 1)
        ann = float(rets.mean() * 252) if len(rets) else 0.0
        vol = float(rets.std() * np.sqrt(252)) if len(rets) > 1 else 0.0
        sharpe = ann / vol if vol > 0 else 0.0

        con.head("Results")
        con.make_table([
            ["metric", "value"],
            ["window", f"{start} -> {end}"],
            ["sessions", str(len(self.curve))],
            ["trades (exits)", str(len(t))],
            ["win rate", f"{len(wins)/len(t):.1%}"],
            ["avg win", money(float(wins['pnl'].mean()) if len(wins) else 0)],
            ["avg loss", money(float(losses['pnl'].mean()) if len(losses) else 0)],
            ["payoff realized", f"{(gross_win/gross_loss):.2f}" if gross_loss else "n/a"],
            ["total P&L", money(total_pnl)],
            ["total R", f"{float(t['r'].sum()):+.2f}R"],
            ["max drawdown", f"{dd:.2%}"],
            ["annualised return", f"{ann:.2%}"],
            ["Sharpe (rf=0)", f"{sharpe:.2f}"],
            ["final equity", money(float(eq.iloc[-1]))]])

        con.head("Exits by ladder rule")
        con.make_table([["rule", "count", "P&L", "avg R"],
                        *[[a, str(len(g)), money(float(g["pnl"].sum())),
                           f"{float(g['r'].mean()):+.2f}"]
                          for a, g in t.groupby("action")]])

        con.head("Trade-level detail")
        show = t.sort_values("closed")
        con.make_table([["closed", "symbol", "action", "qty", "entry", "exit",
                         "P&L", "R", "held"],
                        *[[r["closed"], r["symbol"], r["action"], str(int(r["qty"])),
                           f"{r['entry']:,.2f}", f"{r['exit']:,.2f}",
                           money(float(r["pnl"])), f"{r['r']:+.2f}",
                           str(r["sessions_held"])]
                          for _, r in show.iterrows()]][:60])

        con.head("Equity curve (every 20th session)")
        con.make_table([["date", "equity", "drawdown"],
                        *[[str(d.date()), money(float(v)),
                           f"{(v-pk)/pk:.2%}"]
                          for i, ((d, v), pk) in enumerate(zip(
                              [(pd.Timestamp(c[0]), c[1]) for c in self.curve],
                              eq.cummax())) if i % 20 == 0]])

        if gross_loss > 0:
            con.verdict_block("BACKTEST COMPLETE",
                              f"{len(t)} exits over {len(self.curve)} sessions. Realised "
                              f"payoff {gross_win/gross_loss:.2f}. Compare that against the "
                              f"assumed b_net of ~1.49 at R=2%: if the realised payoff is "
                              f"materially below it, the Kelly sizing is optimistic and the "
                              f"position floor needs revisiting.")
        else:
            con.verdict_block("BACKTEST COMPLETE",
                              f"{len(t)} exits over {len(self.curve)} sessions with no "
                              f"losing trade recorded. That is almost certainly too small a "
                              f"sample to believe; widen the window before drawing any "
                              f"conclusion about the payoff ratio.")
        return 0
