"""
engine.py - the live trading engine (PAPER by default).

Loop (every ~8 seconds):
  1. Determine the session phase in IST:
         PRE_OPEN / OPEN / POST_CLOSE / CLOSED
  2. PRE_OPEN  (08:45-09:14)  build daily trend/volume context (once, idempotent)
  3. OPEN      (09:15-15:29)
       a) one-time initial sync of today's 5-min bars for the universe
          (also covers "started mid-day" - the engine just resumes from DB)
       b) at every completed 5-min boundary: fetch the new bar for each symbol
          and run the strategy (SCAN -> SELECTION -> ENTRY / exits)
       c) between boundaries: poll quotes for open positions + the dip
          watchlist and manage exits (stop / target / trail) on ticks
       d) from 15:20: flatten everything (intraday-only mandate)
  4. POST_CLOSE (15:30-16:29) persist final bar + today's daily bar, EOD report
  5. CLOSED (all other times)  no data capture, NO writes - the DB stays clean

RESTART SAFETY
  * ALL state (open positions, stops, cash, processed-bar markers, context,
    instrument keys) lives in SQLite. A restart simply re-reads it.
  * Completed bars are immutable (upsert-ignore) and every bar evaluation is
    guarded by the processed_bars table, so nothing is ever traded twice.
  * A position that somehow survived from a previous day is closed at its last
    stored price at startup (STALE_RESTART) and loudly logged.

DATA-FETCH GATE  (the "run at any time" requirement)
  * The engine may be started at any moment. Data is captured only during
    PRE_OPEN/OPEN/POST_CLOSE windows, and only completed bars are persisted.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import config
import db
import data_fetch
from console import banner, hr, inr, now_str, pct, signed
from execution import PaperBroker, UpstoxBroker
from mkttime import (completed_bar_until, fmt_t, hm, ist_now, parse_t,
                     session_phase, today_str)
from strategy import ExitCall, StockContext, Strategy
from upstox_client import UpstoxError


def _remaining(p) -> int:
    try:
        return p["qty_remaining"]
    except (IndexError, KeyError):
        return p["qty"]


def _partial_count(p) -> int:
    try:
        return p["partial_count"]
    except (IndexError, KeyError):
        return 0


def _realized(p) -> float:
    try:
        return p["realized_pnl"]
    except (IndexError, KeyError):
        return 0.0


class Engine:
    def __init__(self, mode: str = "paper", once: bool = False,
                 observe: bool = False, db_path=None, quiet: bool = False):
        assert mode in ("paper", "real")
        self.mode = mode
        self.once = once
        self.observe = observe
        self.quiet = quiet
        self.conn = db.get_conn(db_path or config.DB_PATH)
        db.init_db(self.conn)

        self.universe = config.load_universe()
        self.params = config.load_params()
        self.cfg = config.make_cfg(self.params)
        self.strat = Strategy(self.cfg)
        # real mode: the UpstoxBroker is created in _ensure_api() (it needs
        # the authenticated client); until then self.broker is None and every
        # order path refuses to run (never silently papers a real mandate)
        self.broker = PaperBroker(self.conn) if mode == "paper" else None
        self.client = None
        self.keys: dict = {}
        self.missing: list = []

        self.last_completed: dict[str, str] = {}
        self.watch: set = set()
        self.day: str | None = None
        self._context_done = False
        self._day_synced = False
        self._finalized = False
        self._said: dict = {}
        self._closed_log = 0.0
        self._api_warned = False
        self._holiday_logged = False
        self._halted = False
        self._stale_checked = False
        self._reconciled = False

    # ------------------------------------------------------------------ boot
    def _restore_state(self) -> None:
        """Re-hydrate everything from the DB (restart-safe)."""
        today = today_str()
        rows = self.conn.execute(
            "SELECT symbol, MAX(bar_time) AS m FROM candles_5m "
            "WHERE bar_time LIKE ? GROUP BY symbol", (today + " %",)).fetchall()
        self.last_completed = {r["symbol"]: r["m"] for r in rows if r["m"]}

        # restore today's kill-switch state (restart-safe)
        self._halted = db.get_meta(self.conn, f"halt_{today}") == "1"
        if self._halted:
            print("[info] daily loss kill-switch was active today - "
                  "restored, no new entries until the next session.")

        for p in db.get_open_positions(self.conn):
            if not p["entry_time"].startswith(today):
                if self.mode == "real":
                    # cannot place orders without the API - flattened at the
                    # first OPEN pass with market access (intraday mandate)
                    db.log_event(self.conn, "ERROR",
                                 f"STALE real-mode position {p['symbol']} "
                                 f"id={p['id']} from {p['entry_time'][:10]} - "
                                 "will be flattened at market open")
                    print(f"[ERROR] stale REAL position {p['symbol']} from a "
                          "previous day - will be flattened at market open")
                    continue
                lp = (self.broker.last_prices([p["symbol"]]).get(p["symbol"])
                      or p["entry_fill"])
                pnl = self.broker.sell(p["id"], lp, now_str(), "STALE_RESTART",
                                       "position survived a restart across days; "
                                       "closed at last stored price (paper mode "
                                       "keeps the intraday-only mandate)")
                db.log_event(self.conn, "WARN",
                             f"stale position {p['symbol']} id={p['id']} closed "
                             f"at {lp:.2f} pnl={pnl if pnl is not None else 'n/a'}")
                print(f"[WARN] stale position {p['symbol']} from a previous day "
                      f"closed at {lp:.2f} (pnl {pnl:+.2f})")

    def _print_banner(self) -> None:
        now = ist_now()
        mode_txt = ("PAPER (simulated fills, Upstox market data)"
                    if self.mode == "paper" else
                    "REAL (Upstox live orders + broker-side protective SL-M "
                    "per position)")
        pos = db.get_open_positions(self.conn)
        if self.mode == "paper":
            lp = self.broker.last_prices(self.universe)
            cash, equity = self.broker.equity(lp)
            port_txt = f"INR {inr(equity)}  |  open positions: {len(pos)}"
        elif self.broker is not None:
            cash, equity = self.broker.account_state(today_str(now))
            port_txt = (f"INR {inr(equity)} (real proxy)  |  "
                        f"open positions: {len(pos)}")
        else:
            port_txt = (f"n/a until API auth (see 'Upstox setup')  |  "
                        f"open positions: {len(pos)}")
        banner(
            f"STMR v{config.VERSION} - SHORT-TERM MEAN REVERSION ENGINE",
            [
                f"mode         : {mode_txt}{' + OBSERVE (no orders)' if self.observe else ''}",
                f"date/time    : {fmt_t(now)} IST   session: {session_phase(now)}",
                f"universe     : {len(self.universe)} Nifty-100 names (long-only, intraday)",
                f"database     : {config.DB_PATH}",
                f"parameters   : "
                + (f"TUNED ({len(self.params)} overrides from {config.PARAMS_FILE.name})"
                   if self.params else "defaults (config.py)"),
                f"portfolio    : {port_txt}",
                f"strategy     : dip z<={-self.cfg.DIP_Z} (lookback {self.cfg.DIP_LOOKBACK}b)"
                                f" + green reclaim | target=SMA{self.cfg.SMOOTH_N} | "
                                f"stop={self.cfg.SL_ATR_MULT}xATR | "
                                f"flat by {self.cfg.EOD_FLAT_AT}",
            ],
        )
        if pos:
            print("  open positions (restored from DB):")
            for p in pos:
                rem = _remaining(p)
                part = (f"  [{_partial_count(p)} partial(s), "
                        f"realized {signed(_realized(p))}]"
                        if _partial_count(p) else "")
                print(f"    {p['symbol']:<11} {rem:>5} @ {p['entry_fill']:.2f}  "
                      f"stop {p['stop']:.2f}  target {p['target']:.2f}  "
                      f"entry {p['entry_time']}{part}")

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        self._print_banner()
        self._restore_state()
        db.log_event(self.conn, "INFO",
                     f"engine started mode={self.mode} observe={self.observe} "
                     f"once={self.once}")
        self.conn.commit()
        print()
        try:
            while True:
                t0 = time.time()
                try:
                    self.pass_once()
                except Exception as e:  # never let one bad pass kill the loop
                    db.log_event(self.conn, "ERROR", f"pass failed: {e!r}")
                    print(f"[{now_str()}] ERROR pass failed: {e!r}")
                if self.once:
                    break
                time.sleep(max(1.0, config.POLL_FAST_SEC - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\n[info] stopped by user - all state is safe in the database.")
            db.log_event(self.conn, "INFO", "engine stopped (Ctrl+C)")
            self.conn.commit()

    def pass_once(self) -> None:
        """One engine pass. ALWAYS leaves the connection committed - a
        dangling write transaction on a long-lived connection would starve
        the WAL (and block any second process reading the same DB)."""
        try:
            self._pass_once_impl()
        finally:
            try:
                self.conn.commit()
            except Exception:
                pass

    def _pass_once_impl(self) -> None:
        now = ist_now()
        date = today_str(now)
        if date != self.day:
            self.day = date
            self._day_synced = False
            self._finalized = False
            self.watch = set()
            self._holiday_logged = False
            self._stale_checked = False
            self._reconciled = False
            self._restore_state()

        phase = session_phase(now)

        if phase == "CLOSED":
            if time.time() - self._closed_log > 900:
                self._closed_log = time.time()
                self._say("market CLOSED - no data capture, no writes "
                          "(DB stays clean). Next session: Mon-Fri 09:15 IST.",
                          once_per=1800)
            return

        if phase == "PRE_OPEN":
            if hm(now) >= config.CONTEXT_READY_AT:
                if not self._context_done:
                    if self._ensure_api():
                        self._say(f"pre-open: building daily context for "
                                  f"{len(self.keys)} tradable symbols ...")
                        n = data_fetch.build_day_context(self.client, self.conn,
                                                         date, self.universe, self.keys)
                        self._say(f"daily context ready: {n} symbols updated "
                                  f"(trend filter {'ON' if config.TREND_FILTER else 'OFF'})")
                        self._context_done = True
                    else:
                        self._say("pre-open: Upstox API unavailable - context not built; "
                                  "will retry next pass", once_per=600)
            else:
                self._say(f"pre-open (before {config.CONTEXT_READY_AT}) - waiting for "
                          "the daily context window", once_per=300)
            return

        if phase == "POST_CLOSE":
            self._finalize_day(date, now)
            return

        # ---------------- OPEN ----------------
        if not self._ensure_api():
            return
        self._close_stale_real_positions(now)
        self._reconcile_open_orders()
        if not self._day_synced:
            self._sync_day(date, now)
            self._day_synced = True
            if self._holiday_logged:
                return

        completed = completed_bar_until(now)
        if completed is not None:
            self._scan_pass(date, completed, now)

        if hm(now) >= config.EOD_FLAT_AT:
            self._flatten_all(now)
        else:
            self._live_watch(date, now)

        self._heartbeat(date, now)

    # ----------------------------------------------------------------- setup
    def _ensure_api(self) -> bool:
        """Lazily authenticate + resolve instrument keys. False => skip session."""
        if self.client is not None and self.keys:
            return True
        try:
            if self.client is None:
                from upstox_client import UpstoxClient
                self.client = UpstoxClient()
                self.client.ensure_token()
            if self.mode == "real" and self.broker is None:
                self.broker = UpstoxBroker(self.conn, self.client)
            mapping, missing = data_fetch.ensure_instruments(self.client, self.conn,
                                                             self.universe)
            self.keys = mapping
            self.missing = missing
            if self.broker is not None and self.mode == "real":
                self.broker.set_keys(mapping)
            if not mapping:
                self._warn_once(
                    "NO instrument keys available - cannot trade or capture data. "
                    "See README 'Upstox setup' / API limitation #5.")
            return bool(mapping)
        except UpstoxError as e:
            self._warn_once(f"Upstox API unavailable: {e}  (skipping live data; "
                            "no writes made)")
            return False

    def _sync_day(self, date: str, now: datetime) -> None:
        self._say(f"initial sync: fetching today's 5-min bars for "
                  f"{len(self.keys)} symbols (one-time; restart-safe)")
        n_ok = n_fail = n_bars = n_live = 0
        for s in self.universe:
            key = self.keys.get(s)
            if not key:
                continue
            try:
                n_new, live = data_fetch.sync_day_bars(self.client, self.conn, s,
                                                       key, date, now)
                n_bars += n_new
                n_live += 1 if live else 0
                self.last_completed[s] = db.last_bar_time(self.conn, s, date) \
                    or self.last_completed.get(s)
                n_ok += 1
            except UpstoxError as e:
                n_fail += 1
                db.log_event(self.conn, "WARN", f"sync {s}: {e}")
        # holiday detection: only meaningful once the first bar could have
        # completed AND there is neither a completed nor a live bar anywhere
        if n_ok and n_bars == 0 and n_live == 0 and hm(now) >= "09:30":
            self._holiday_logged = True
            self._say("no 5-min bars returned for ANY symbol - likely a market "
                      "holiday. Skipping the session (no data to capture).",
                      once_per=600)
        db.log_event(self.conn, "INFO",
                     f"day sync: {n_ok} ok, {n_fail} failed, {n_bars} new bars, "
                     f"{n_live} live")
        self._say(f"initial sync done: {n_ok} symbols, {n_bars} bars captured, "
                  f"{n_live} live ({n_fail} failed)")

    # ------------------------------------------------ real-mode safety (open)
    def _close_stale_real_positions(self, now: datetime) -> None:
        """Flatten positions that survived from a previous day (real mode).

        A Friday-crash leftover MUST not be held overnight - the intraday
        mandate is absolute. Runs once per day, at the first OPEN pass with
        market access (needs quotes + orders).
        """
        if self.mode != "real" or self._stale_checked:
            return
        self._stale_checked = True
        today = today_str(now)
        for p in db.get_open_positions(self.conn):
            if p["entry_time"].startswith(today):
                continue
            px = None
            key = self.keys.get(p["symbol"])
            if key:
                try:
                    q = self.client.get_quote(key)
                    if q:
                        px = q["last_price"]
                except UpstoxError:
                    pass
            px = px or p["entry_fill"]
            call = ExitCall("STALE_RESTART", px,
                            "position survived from a previous day; real mode "
                            "flattens it immediately (intraday-only mandate)")
            pnl = self._real_sell(p, call, fmt_t(now), "STALE_RESTART")
            db.log_event(self.conn, "ERROR",
                         f"STALE real position {p['symbol']} id={p['id']} "
                         f"(from {p['entry_time'][:10]}) closed at {px:.2f} "
                         f"pnl={pnl if pnl is not None else 'n/a'} - "
                         "investigate why it survived")
            print(f"[ERROR] STALE real-mode position {p['symbol']} closed at "
                  f"{px:.2f} (pnl {pnl if pnl is not None else 'n/a'}) - "
                  "investigate")

    def _reconcile_open_orders(self) -> None:
        """Startup audit (real mode): flag any broker-side open order this
        system doesn't know about (e.g. orphaned by a crash mid-order).

        Deliberately does NOT auto-cancel - an unknown open order may belong
        to another strategy on the same account; a human decides.
        """
        if self.mode != "real" or self._reconciled:
            return
        self._reconciled = True
        try:
            orders = self.client.list_orders(status="OPEN")
        except UpstoxError as e:
            db.log_event(self.conn, "WARN",
                         f"open-order reconciliation skipped: {e}")
            return
        known = {p["sl_order"] for p in db.get_open_positions(self.conn)
                 if p["sl_order"]}
        for o in orders:
            oid = o.get("order_id")
            if oid and oid not in known:
                msg = (f"untracked OPEN order at startup: {oid} "
                       f"({o.get('instrument_key')} {o.get('transaction_type')} "
                       f"qty {o.get('quantity')} {o.get('order_type')}) - "
                       "review your account")
                db.log_event(self.conn, "WARN", msg)
                print(f"[WARN] {msg}")
        if not orders:
            self._say("order reconciliation: no open orders at the broker "
                      "(clean start)", once_per=0)

    # ----------------------------------------------------------------- scan
    def _scan_pass(self, date: str, completed: datetime, now: datetime) -> None:
        """Fetch the newly completed 5-min bar for every symbol and evaluate."""
        completed_str = fmt_t(completed)
        dips: list = []
        entries: list = []
        exits: list = []
        z_bottoms: list = []
        n_new = n_fail = 0

        for s in self.universe:
            key = self.keys.get(s)
            if not key:
                continue
            # Skip only the FETCH when we already have bars up to the
            # boundary - never skip evaluation: a crash between storing a
            # bar and evaluating it must not lose that bar (the
            # processed_bars guard makes re-evaluation safe).
            if not (self.last_completed.get(s)
                    and self.last_completed[s] >= completed_str):
                try:
                    n_new_bars, _ = data_fetch.sync_day_bars(self.client,
                                                             self.conn, s, key,
                                                             date, now)
                    n_new += n_new_bars
                except UpstoxError:
                    n_fail += 1
                self.last_completed[s] = db.last_bar_time(self.conn, s, date) \
                    or self.last_completed.get(s, "")
            bars = db.get_day_bars(self.conn, s, date)
            for b in bars:
                if b.t > completed_str:      # no look-ahead at any cost
                    break
                if db.mark_processed(self.conn, s, b.t, "scan"):
                    self._evaluate_bar(s, date, b.t, now, dips, entries, exits,
                                       z_bottoms)

        if entries or exits or dips or (n_new and not self.quiet):
            if self.mode == "paper":
                lp = self.broker.last_prices(self.universe)
                cash, equity = self.broker.equity(lp)
            else:
                cash, equity = self.broker.account_state(date)
            n_open = len(db.get_open_positions(self.conn))
            self._say(f"SCAN bar {completed_str[11:16]} done | "
                      f"{len(self.keys)} symbols | new bars: {n_new} "
                      f"(fails {n_fail}) | dips: {len(dips)} | entries: {len(entries)}"
                      f" | exits: {len(exits)} | open: {n_open} | "
                      f"equity INR {inr(equity)}")
            if z_bottoms:
                zb = sorted(z_bottoms, key=lambda x: x[1])[:5]
                self._say("  z-bottoms: " + "  ".join(f"{s} {z:+.2f}" for s, z in zb),
                          once_per=0)
        for s, detail in dips:
            self._say(f"  DIP    {s:<11} {detail}")

    def _evaluate_bar(self, sym: str, date: str, bar_t: str, now: datetime,
                      dips: list, entries: list, exits: list, z_bottoms: list) -> None:
        bars = db.get_day_bars(self.conn, sym, date)
        # Defensive: never look at bars AFTER the one being evaluated
        # (normal operation stores only completed bars, but a pre-seeded or
        # stale DB must not leak future prices into the decision).
        if bars and bars[-1].t > bar_t:
            bars = [b for b in bars if b.t <= bar_t]
        if not bars or bars[-1].t != bar_t:
            return
        pre = self.strat.precompute(bars)
        m = self.strat.scan(bars, pre)
        if m is None:
            return
        z_bottoms.append((sym, m["min_z"]))

        ctx_row = db.get_context(self.conn, sym, date)
        ctx = StockContext.from_row(ctx_row) if ctx_row else None

        # ---- 1) manage an open position on this symbol (exits on bar close)
        p = next((p for p in db.get_open_positions(self.conn)
                  if p["symbol"] == sym), None)
        if p:
            bars_held = max(0, int((now - parse_t(p["entry_time"])).total_seconds()
                                   // (config.BAR_MINUTES * 60)))
            call, new_stop = self.strat.evaluate_exit(p, bars, tick=None, now=now,
                                                      pre=pre, bars_held=bars_held)
            if call:
                pnl, reason, partial = self._execute_exit(p, call, bar_t, m=m)
                if partial:
                    pass  # audit-logged inside _execute_exit (PARTIAL signal)
                else:
                    exits.append(sym)
                    self._say_exit(sym, p, call, pnl, bar_t, reason)
            elif new_stop > p["stop"] + 1e-9:
                db.update_position(self.conn, p["id"], stop=new_stop)
                self.broker.sync_stop(p["id"], new_stop)  # real: ratchet SL-M
                db.record_signal(self.conn, now_str(), sym, bar_t, "TRAIL",
                                 bars[-1].c, m["z"], m["rsi"], m["atr"],
                                 f"stop {p['stop']:.2f} -> {new_stop:.2f} "
                                 f"(trailing after mean cross)", "live")
                self._say(f"  TRAIL  {sym:<11} stop {p['stop']:.2f} -> "
                          f"{new_stop:.2f} (price crossed the mean)")

        # ---- 2) entries (only when flat on this symbol and slot available)
        open_pos = db.get_open_positions(self.conn)
        if p is None and len(open_pos) < self.cfg.MAX_POSITIONS:
            blocked = self._entry_blocked(date, sym, bar_t)
            if blocked:
                return
            now_hm = bar_t[11:16]
            if self.mode == "paper":
                _, equity = self.broker.equity(self.broker.last_prices([sym]))
                cash = self.broker.cash()
            else:
                # real: sizing base + realized P&L - open position cost basis
                cash, equity = self.broker.account_state(date)
            plan, sigs, skip = self.strat.evaluate_entry(
                sym, ctx, bars, now_hm=now_hm, equity=equity, cash=cash, pre=pre)
            for kind, detail in sigs:
                db.record_signal(self.conn, now_str(), sym, bar_t, kind,
                                 bars[-1].c, m["z"], m["rsi"], m["atr"], detail, "live")
                if kind == "DIP":
                    dips.append((sym, detail))
                    self.watch.add(sym)
            if plan:
                if self.mode == "real":
                    bar_end = parse_t(bar_t) + timedelta(minutes=config.BAR_MINUTES)
                    if now - bar_end > timedelta(minutes=6):
                        self._say(f"  SKIP   {sym:<11} entry bar too old for REAL "
                                  "mode (stale signal) - skipped", level="warn")
                    else:
                        self._do_entry(plan, entries)
                else:
                    self._do_entry(plan, entries)

    def _do_entry(self, plan, entries: list) -> None:
        if self.observe:
            db.record_signal(self.conn, now_str(), plan.symbol, plan.t, "ENTRY_OBS",
                             plan.price, plan.z, plan.rsi, plan.atr, plan.reason, "live")
            self._say(f"  ENTRY* {plan.symbol:<10} [OBSERVE] would buy "
                      f"{plan.qty} @ ~{plan.price:.2f}  stop {plan.stop:.2f}  "
                      f"target {plan.target:.2f}", level="ok")
            return
        if self.mode == "paper":
            pid = self.broker.buy(plan.symbol, plan.price, plan.qty, plan.t,
                                  plan.stop, plan.target, plan.atr, plan.reason)
        else:
            pid = self.broker.buy(plan.symbol, plan.price, plan.qty, plan.t,
                                  plan.stop, plan.target, plan.atr, plan.reason)
        if pid:
            self.watch.add(plan.symbol)
            db.record_signal(self.conn, now_str(), plan.symbol, plan.t, "ENTRY",
                             plan.price, plan.z, plan.rsi, plan.atr, plan.reason,
                             "live")
            entries.append(plan.symbol)
            self._say_entry(plan)

    def _log_exit_signal(self, sym: str, t: str, kind: str, price: float,
                         m: dict | None, note: str, tick: bool = False) -> None:
        z = m["z"] if m else None
        rsi_v = m["rsi"] if m else None
        atr_v = m["atr"] if m else None
        if tick:
            note = f"{note} (tick)"
        db.record_signal(self.conn, now_str(), sym, t, kind, price, z, rsi_v,
                         atr_v, note, "live")

    def _execute_exit(self, p, call: ExitCall, t: str, m: dict | None = None,
                      tick: bool = False):
        """Execute an exit call with desk partial-profit rules.
        Owns the PARTIAL/EXIT audit log so every exit path is recorded.
        Returns (total_pnl | None, reason, partial_happened)."""
        if call.reason == "TARGET":
            act = self.strat.plan_target_exit(p)
            if act["action"] == "partial":
                try:
                    slice_pnl = self.broker.sell_partial(
                        p["id"], call.price, act["qty"], t, "PARTIAL", act["note"])
                except Exception as e:
                    db.log_event(self.conn, "ERROR",
                                 f"partial sell {p['symbol']}: {e!r}")
                    print(f"[ERROR] partial sell failed for {p['symbol']}: {e}")
                    slice_pnl = None
                if slice_pnl is not None:
                    db.update_position(self.conn, p["id"], stop=act["new_stop"],
                                       target=act["target2"])
                    self.broker.sync_stop(p["id"], act["new_stop"])
                    self._log_exit_signal(p["symbol"], t, "PARTIAL", call.price,
                                          m, act["note"], tick)
                    self.conn.commit()
                    self._say(f"  PART   {p['symbol']:<11} {act['note']} | "
                              f"slice pnl {signed(slice_pnl)}", level="ok")
                    return None, "PARTIAL", True
        reason = ("TARGET2" if call.reason == "TARGET"
                  and (p["partial_count"] or 0) > 0 else call.reason)
        try:
            pnl = (self.broker.sell(p["id"], call.price, t, reason, call.note)
                   if self.mode == "paper" else
                   self._real_sell(p, call, t, reason))
        except Exception as e:
            db.log_event(self.conn, "ERROR", f"sell {p['symbol']}: {e!r}")
            print(f"[ERROR] sell failed for {p['symbol']}: {e}")
            pnl = None
        self._log_exit_signal(p["symbol"], t, "EXIT", call.price, m,
                              f"{reason}: {call.note}", tick)
        self.conn.commit()
        return pnl, reason, False

    def _real_sell(self, p, call: ExitCall, t: str, reason: str | None = None):
        try:
            return self.broker.sell(p["id"], call.price, t,
                                    reason or call.reason, call.note)
        except Exception as e:
            db.log_event(self.conn, "ERROR", f"real sell {p['symbol']}: {e!r}")
            print(f"[ERROR] real sell failed for {p['symbol']}: {e}")
            return None

    def _entry_blocked(self, date: str, sym: str, bar_t: str):
        """Desk guards on new entries. Returns a block reason or None."""
        n_today = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE entry_time LIKE ?",
            (date + " %",)).fetchone()["n"]
        if n_today >= self.cfg.MAX_TRADES_PER_DAY:
            self._say(f"  GUARD  {sym:<11} skipped: daily trade cap "
                      f"({self.cfg.MAX_TRADES_PER_DAY}/day)", once_per=300)
            return "trade-cap"
        if self.cfg.STOP_COOLDOWN_BARS > 0:
            r = self.conn.execute(
                "SELECT MAX(exit_time) AS m FROM trades WHERE symbol=? "
                "AND exit_reason='STOP' AND exit_time LIKE ?",
                (sym, date + " %")).fetchone()
            if r and r["m"]:
                mins = (parse_t(bar_t) - parse_t(r["m"])).total_seconds() / 60
                if mins < self.cfg.STOP_COOLDOWN_BARS * config.BAR_MINUTES:
                    self._say(f"  GUARD  {sym:<11} skipped: cooldown "
                              f"({self.cfg.STOP_COOLDOWN_BARS} bars) after "
                              f"stop-out at {r['m'][11:16]}", once_per=60)
                    return "cooldown"
        if self._halted:
            self._say(f"  GUARD  {sym:<11} skipped: daily loss kill-switch "
                      f"active", once_per=300)
            return "loss-limit"
        return None

    # ------------------------------------------------------------- live watch
    def _live_watch(self, date: str, now: datetime) -> None:
        """Tick-level exit management between bar closes."""
        pos_syms = {p["symbol"] for p in db.get_open_positions(self.conn)}
        watch = list(dict.fromkeys(list(pos_syms) + list(self.watch)))
        if not watch:
            return
        for s in watch:
            key = self.keys.get(s)
            if not key:
                continue
            try:
                live_bar = data_fetch.poll_live_bar(self.client, self.conn, s, key, now)
            except UpstoxError:
                continue
            if live_bar is None:
                continue
            p = next((p for p in db.get_open_positions(self.conn)
                      if p["symbol"] == s), None)
            if not p:
                continue
            stored = [b for b in db.get_day_bars(self.conn, s, date)
                      if b.t <= live_bar.t]
            bars = stored + [live_bar]
            call, new_stop = self.strat.evaluate_exit(p, bars,
                                                      tick=live_bar.c, now=now)
            if call:
                pnl, reason, partial = self._execute_exit(p, call, fmt_t(now),
                                                          tick=True)
                if not partial:
                    self._say_exit(s, p, call, pnl, fmt_t(now), reason)
            elif new_stop > p["stop"] + 1e-9:
                db.update_position(self.conn, p["id"], stop=new_stop)
                self.broker.sync_stop(p["id"], new_stop)  # real: ratchet SL-M
                db.record_signal(self.conn, now_str(), s, live_bar.t, "TRAIL",
                                 live_bar.c, None, None, None,
                                 f"stop {p['stop']:.2f} -> {new_stop:.2f} (tick)",
                                 "live")

    def _flatten_all(self, now: datetime) -> None:
        open_pos = db.get_open_positions(self.conn)
        if not open_pos:
            self._say("EOD flat confirmed: no open positions", once_per=600)
            return
        for p in open_pos:
            key = self.keys.get(p["symbol"])
            price = None
            if key:
                try:
                    q = self.client.get_quote(key)
                    if q:
                        price = q["last_price"]
                except UpstoxError:
                    pass
            if price is None:
                price = (self.broker.last_prices([p["symbol"]]).get(p["symbol"])
                         or p["entry_fill"])
            pnl = (self.broker.sell(p["id"], price, fmt_t(now), "EOD",
                                    "forced flat (intraday-only mandate)")
                   if self.mode == "paper" else
                   self._real_sell(p, ExitCall("EOD", price, "forced flat"),
                                   fmt_t(now)))
            db.record_signal(self.conn, now_str(), p["symbol"], fmt_t(now), "EXIT",
                             price, None, None, None, "EOD flatten", "live")
            self._say_exit(p["symbol"], p, ExitCall("EOD", price, "forced flat"),
                           pnl, fmt_t(now))

    # ------------------------------------------------------------ post-close
    def _finalize_day(self, date: str, now: datetime) -> None:
        if self._finalized:
            self._say("day already finalised - idle in post-close (no writes)",
                      once_per=600)
            return
        if not self._ensure_api():
            return
        self._say(f"post-close: persisting final bar + daily bar for {date} ...")
        data_fetch.finalize_day(self.client, self.conn, date, self.universe, self.keys)
        self.conn.commit()
        self._finalized = True
        self._write_eod_report(date)
        self.conn.commit()  # every pass must end with a committed connection

    def _write_eod_report(self, date: str) -> None:
        trades = db.trades_in_range(self.conn, date, date)
        closed = [t for t in trades if t["exit_time"]]
        pnl_sum = sum(t["pnl_net"] or 0 for t in closed)
        wins = [t for t in closed if (t["pnl_net"] or 0) > 0]
        lp = self.broker.last_prices(self.universe)
        cash, equity = self.broker.equity(lp)
        start_eq = float(db.get_meta(self.conn, "paper_start_equity",
                                     config.PAPER_START_EQUITY))
        lines = []
        lines.append(f"# STMR EOD Report - {date}")
        lines.append("")
        lines.append(f"- Equity: **INR {inr(equity)}** "
                     f"(start {inr(start_eq)}, day {pct(pnl_sum / start_eq * 100)})")
        lines.append(f"- Cash: INR {inr(cash)} | trades today: {len(trades)} "
                     f"(closed {len(closed)}) | wins: {len(wins)}")
        lines.append(f"- Net P&L (closed): **INR {signed(pnl_sum)}**")
        lines.append("")
        if trades:
            lines.append("| # | Symbol | Qty | Entry | Exit | Reason | Net P&L |")
            lines.append("|---|--------|-----|-------|------|--------|---------|")
            for t in trades:
                lines.append(
                    f"| {t['id']} | {t['symbol']} | {t['qty']} | "
                    f"{t['entry_fill']:.2f} @ {t['entry_time'][11:16]} | "
                    f"{(t['exit_fill'] or 0):.2f} @ {(t['exit_time'] or 'open')[11:16]} | "
                    f"{t['exit_reason'] or 'OPEN'} | {signed(t['pnl_net'] or 0)} |")
            lines.append("")
        cov = db.day_coverage(self.conn, date)
        lines.append(f"- 5-min bar coverage: {len(cov)} symbols, "
                     f"max {max(cov.values()) if cov else 0} bars")
        path = config.REPORTS_DIR / f"eod_{date.replace('-', '')}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._say(f"EOD report written: {path}")
        self._say(f"  day P&L INR {signed(pnl_sum)} | closed {len(closed)} trades | "
                  f"equity INR {inr(equity)}")
        db.log_event(self.conn, "INFO",
                     f"EOD {date}: pnl={pnl_sum:.2f} closed={len(closed)} "
                     f"equity={equity:.2f}")

    # -------------------------------------------------------------- heartbeat
    def _heartbeat(self, date: str, now: datetime) -> None:
        if self.mode == "paper":
            lp = self.broker.last_prices(self.universe)
            cash, equity = self.broker.equity(lp)
        else:
            # real: proxy from sizing base + realized P&L + open marks
            cash, equity = self.broker.account_state(date)
        realized = db.closed_pnl_on(self.conn, date)
        db.append_equity(self.conn, now_str(), cash, equity,
                         len(db.get_open_positions(self.conn)), realized, "OPEN")
        # --- desk guard: daily-loss kill-switch (checked once per pass) ---
        if self.cfg.DAILY_LOSS_LIMIT_PCT > 0 and not self._halted:
            eq_start = db.get_meta(self.conn, f"eq_start_{date}")
            if not eq_start:
                # prefer today's earliest equity point (covers a mid-day
                # restart where a previous instance already wrote points)
                r = self.conn.execute(
                    "SELECT equity FROM equity_curve WHERE ts LIKE ? "
                    "ORDER BY ts ASC LIMIT 1", (date + " %",)).fetchone()
                eq_start = str(r["equity"]) if r else str(equity)
                db.set_meta(self.conn, f"eq_start_{date}", eq_start)
                self.conn.commit()
            elif equity <= float(eq_start) * (1 - self.cfg.DAILY_LOSS_LIMIT_PCT / 100):
                self._halted = True
                db.set_meta(self.conn, f"halt_{date}", "1")
                self.conn.commit()
                self._say(f"  DESK   daily loss limit -{self.cfg.DAILY_LOSS_LIMIT_PCT:.2f}% "
                          f"breached (equity {inr(equity)} vs day start "
                          f"{inr(float(eq_start))}) -> NO MORE ENTRIES TODAY",
                          level="warn")
        self.conn.commit()

    # ------------------------------------------------------------- messaging
    def _say(self, msg: str, level: str = "info", once_per: float = 0.0) -> None:
        if self.quiet and level == "info":
            return
        if once_per:
            k = msg[:48]
            if time.time() - self._said.get(k, 0.0) < once_per:
                return
            self._said[k] = time.time()
        prefix = {"ok": "+", "warn": "!", "err": "X"}.get(level, " ")
        print(f"[{now_str()}] {prefix} {msg}")
        if level in ("warn", "err"):
            db.log_event(self.conn, level.upper(), msg)

    def _warn_once(self, msg: str) -> None:
        if not self._api_warned:
            self._api_warned = True
            self._say(msg, level="warn")

    def _say_entry(self, plan) -> None:
        lp = self.broker.last_prices([plan.symbol])
        _, equity = self.broker.equity(lp)
        stop_pct = (plan.stop - plan.price) / plan.price * 100
        tgt_pct = (plan.target - plan.price) / plan.price * 100
        risk_amt = (plan.price - plan.stop) * plan.qty
        tag = "ENTRY*" if self.observe else "ENTRY "
        print(f"[{now_str()}] + {tag}{plan.symbol:<11} BUY {plan.qty} @ {plan.price:.2f}"
              f"   stop {plan.stop:.2f} ({stop_pct:+.2f}%)   "
              f"target {plan.target:.2f} ({tgt_pct:+.2f}%)")
        print(f"              why : {plan.reason}")
        print(f"              risk: {risk_amt / equity * 100:.2f}% of equity "
              f"(INR {inr(risk_amt)}) | value INR {inr(plan.price * plan.qty)}")

    def _say_exit(self, sym: str, p, call: ExitCall, pnl, t: str,
                  reason: str | None = None) -> None:
        pnl_txt = (f"INR {signed(pnl)} (net of fees)"
                   if pnl is not None else "n/a (real mode fill pending?)")
        held = ""
        try:
            mins = (parse_t(t) - parse_t(p["entry_time"])).total_seconds() / 60
            held = f" | held {int(mins)} min"
        except Exception:
            pass
        print(f"[{now_str()}] + EXIT  {sym:<11} SELL {_remaining(p)} @ {call.price:.2f}  "
              f"reason: {reason or call.reason}")
        print(f"              why : {call.note}")
        print(f"              pnl : {pnl_txt}{held}")


def main():  # pragma: no cover - thin wrapper
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "real"], default="paper")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--observe", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    Engine(a.mode, a.once, a.observe, a.db, a.quiet).run()


if __name__ == "__main__":
    main()
