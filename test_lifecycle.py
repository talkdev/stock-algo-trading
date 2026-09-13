"""
Lifecycle test: arm -> fill -> exit -> report, entirely through the real code path.

The live scan legitimately produces zero candidates on most sessions (G2 rejects 93 of
98 names in a -13.7% YTD tape), which means a scan-only smoke test never touches
arm_entry, place_or_fill, evaluate_ladder, apply_exit or the report. This test drives
those functions directly with a constructed setup so they are actually executed.

It runs against a throwaway database and never touches data/jfou.sqlite3.
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jfou import execution as X                     # noqa: E402
from jfou import sizing as S                        # noqa: E402
from jfou.clock import now_ist                      # noqa: E402
from jfou.config import CFG, LAKH                   # noqa: E402
from jfou.db import Database                        # noqa: E402
from jfou.engine import Engine                      # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "lifecycle.sqlite3"))

    # ---------------------------------------------------------------- fixture
    n = 300
    idx = pd.bdate_range(end=pd.Timestamp("2026-09-11"), periods=n)
    rng = np.random.default_rng(5)
    base = 1000 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, n)))
    # carve a genuine pullback at the end: up-trend, then a 6% dip on drying volume
    base[-12:] = base[-13] * np.linspace(1.0, 0.94, 12)
    vol = np.full(n, 8e6)
    vol[-4:] = 3e6                                    # exhaustion
    daily = pd.DataFrame({
        "open": base * 0.998, "high": base * 1.012, "low": base * 0.985,
        "close": base, "volume": vol * rng.uniform(0.8, 1.2, n)}, index=idx)
    daily.loc[daily.index[-1], "high"] = float(base[-1]) * 1.006

    key = "NSE_EQ|TESTLIFECYCLE"
    rows = [{"instrument_key": key, "ts": d.strftime("%Y-%m-%d"),
             "open": float(r.open), "high": float(r.high), "low": float(r.low),
             "close": float(r.close), "volume": float(r.volume)}
            for d, r in daily.iterrows()]
    db.upsert_ohlcv("daily_ohlcv", "ts_date", rows)
    db.upsert_instruments([{"instrument_key": key, "trading_symbol": "TESTSYM",
                            "name": "lifecycle test", "segment": "NSE|EQ",
                            "lot_size": 1, "tick_size": 0.05}])
    db.execute("INSERT OR REPLACE INTO universe(symbol,source,as_of,is_current,sector,"
               "added_at) VALUES('TESTSYM','test','2026-09-11',1,'TEST','2026-09-11')")

    print("=" * 78)
    print("Lifecycle test: arm -> fill -> exit -> report")
    print("=" * 78)

    # ---------------------------------------------------------------- sizing
    last = daily.iloc[-1]
    trigger = float(last["high"]) * (1 + CFG.entry_trigger_pct)
    stop = float(daily["low"].tail(12).min()) - 0.75 * 20.0
    sd = S.size_position(entry=trigger, stop=stop, equity=50 * LAKH,
                         unencumbered_cash=50 * LAKH, adv_shares=6e6, lot_size=1,
                         segment="futures", sigma_t=0.016, expected_move=2.5 * (trigger - stop),
                         tier="Green", open_heat_rupees=0.0)
    print(f"\n  sizing: lots={sd.lots} notional={sd.notional/LAKH:.2f}L "
          f"risk={sd.risk_rupees:,.0f} p={sd.prob_p:.3f} b={sd.b_net:.3f}")
    check("sizing produces a legal position", sd.ok and sd.lots >= CFG.min_lots,
          sd.reject_code or f"{sd.lots} lots")
    check("risk stays within the 1.5% cap",
          sd.risk_rupees <= CFG.risk_cap_pct * 50 * LAKH + 1,
          f"{sd.risk_rupees:,.0f} vs cap {CFG.risk_cap_pct*50*LAKH:,.0f}")

    # ---------------------------------------------------------------- arm
    cand = {"instrument_key": key, "symbol": "TESTSYM", "sector": "TEST",
            "session_date": "2026-09-11", "confluence_pts": 4,
            "trigger_price": trigger, "limit_price": trigger * 1.002,
            "stop_price": stop, "r_value": trigger - stop, "atr14": 20.0,
            "target1_price": float(base[-1]) * 1.02,
            "target2_price": trigger + 2.5 * (trigger - stop),
            "tau_halflife": 2.5, "prob_p": sd.prob_p, "b_net": sd.b_net,
            "lots": sd.lots, "shares": sd.shares, "risk_rupees": sd.risk_rupees,
            "adv20_value": 6e8, "reason": "lifecycle fixture"}
    res = X.arm_entry(db, "RUN-TEST", cand, "PAPER")
    pid = res["position_id"]
    check("arm_entry creates a PENDING_ENTRY position",
          db.scalar("SELECT state FROM positions WHERE position_id=?", (pid,))
          == "PENDING_ENTRY", pid)
    check("arm_entry creates an ARMED order",
          db.scalar("SELECT COUNT(*) FROM orders WHERE position_id=? AND status='ARMED'",
                    (pid,)) == 1)
    check("arm_entry writes to the audit trail",
          db.scalar("SELECT COUNT(*) FROM position_events WHERE position_id=?",
                    (pid,)) == 1)

    # idempotency: re-arming the same run must not duplicate
    res2 = X.arm_entry(db, "RUN-TEST", cand, "PAPER")
    check("re-arming the same run is idempotent", res2.get("already_armed") is True,
          str(res2))
    check("no duplicate positions",
          db.scalar("SELECT COUNT(*) FROM positions") == 1)

    # ---------------------------------------------------------------- fill
    oid = db.scalar("SELECT order_id FROM orders WHERE position_id=?", (pid,))
    fill = X.place_or_fill(db, oid, None, trigger, "PAPER")
    check("paper fill executes", fill.get("filled") is True, str(fill)[:60])
    check("position moves to OPEN",
          db.scalar("SELECT state FROM positions WHERE position_id=?", (pid,)) == "OPEN")
    check("fill row recorded",
          db.scalar("SELECT COUNT(*) FROM fills WHERE position_id=?", (pid,)) == 1)

    # ---------------------------------------------------------------- ladder
    pos = dict(db.one("SELECT * FROM positions WHERE position_id=?", (pid,)))
    # rule 1: stop out
    sig = X.evaluate_ladder(pos, daily, float(stop) - 1.0, float(stop) + 1.0,
                            float(stop), 1)
    check("rule 1 fires on a stop breach", sig.action == "STOP_OUT" and sig.rule == 1,
          sig.action)

    # rule 2: target 1
    up = daily.copy()
    up.loc[up.index[-1], "high"] = float(pos["entry_price"]) * 1.05
    sig2 = X.evaluate_ladder(pos, up, float(pos["entry_price"]) * 0.99,
                             float(pos["entry_price"]) * 1.05,
                             float(pos["entry_price"]) * 1.04, 1)
    check("rule 2 fires at Target 1 and sells 50%",
          sig2.action == "TARGET1" and abs(sig2.qty_frac - 0.5) < 1e-9,
          f"{sig2.action} frac={sig2.qty_frac}")

    # rule 3: target 2
    t2 = float(pos["entry_price"]) + 2.5 * float(pos["r_value"])
    up2 = daily.copy()
    up2.loc[up2.index[-1], "high"] = t2 * 1.01
    sig3 = X.evaluate_ladder(pos, up2, float(pos["entry_price"]) * 0.99, t2 * 1.01,
                             t2, 1)
    check("rule 3 fires at 2.5R", sig3.action == "TARGET2" and sig3.rule == 3, sig3.action)

    # rule 5: time stop
    sig5 = X.evaluate_ladder(pos, daily, float(pos["stop_price"]) + 5.0,
                             float(pos["stop_price"]) + 10.0,
                             float(pos["stop_price"]) + 8.0, 99)
    check("rule 5 fires on the OU time stop", sig5.action == "TIME_STOP" and sig5.rule == 5,
          sig5.action)

    # ---------------------------------------------------------------- exit
    ex = X.apply_exit(db, pid, sig5, float(pos["entry_price"]) - 1.0, "PAPER")
    check("apply_exit closes the position",
          db.scalar("SELECT state FROM positions WHERE position_id=?", (pid,)) == "CLOSED",
          str(ex)[:60])
    check("realized P&L is recorded",
          abs(float(db.scalar("SELECT realized_pnl FROM positions WHERE position_id=?",
                              (pid,)))) > 0)
    events = db.scalar("SELECT COUNT(*) FROM position_events WHERE position_id=?", (pid,))
    trail = [e["event"] for e in db.query(
        "SELECT event FROM position_events WHERE position_id=? ORDER BY id", (pid,))]
    check("audit trail records ARMED -> FILLED -> EXIT in order",
          trail == ["ARMED", "FILLED", "EXIT_RULE_5"], str(trail))

    # ---------------------------------------------------------------- report
    print("\n  --- report output ---")
    db.execute("""INSERT OR REPLACE INTO scan_runs (run_id, session_date, phase, macro_tier,
                  universe_size, candidates, selected, status, started_at, notes)
                  VALUES ('RUN-TEST','2026-09-11','POST_CLOSE','Green',1,1,1,'OK',?, 'test')""",
               (now_ist().strftime("%Y-%m-%d %H:%M:%S"),))
    eng = Engine(db, None, mode="PAPER", verbose=False)
    eng.report("RUN-TEST")

    # ---------------------------------------------------------------- restart
    print("\n  --- restart safety ---")
    db2 = Database(os.path.join(tmp, "lifecycle.sqlite3"))
    closed = db2.query("SELECT * FROM positions WHERE state='CLOSED'")
    check("a fresh connection sees the closed trade", len(closed) == 1)
    check("a fresh connection sees the audit trail",
          db2.scalar("SELECT COUNT(*) FROM position_events") == events, str(events))
    check("a fresh connection sees the fills",
          db2.scalar("SELECT COUNT(*) FROM fills") >= 2)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("RESULT: all lifecycle checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
