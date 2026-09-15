"""
verify_all.py - offline end-to-end self check.

Runs without any network / Upstox credentials:
  1. all modules import
  2. indicator sanity (SMA / RSI / ATR / z-score / VWAP)
  3. fee model sanity
  4. DB round-trips (immutable candles, idempotent processed-bar guard,
     open/close position)
  5. strategy: a hand-built dip day MUST produce an entry, then a mean exit
  6. paper broker round-trip (cash + P&L consistency)
  7. session-time helpers (phases, bar boundaries)
  8. full pipeline: seed synthetic data -> backtest -> report (temp DB)

Exit code 0 = all green. Safe to run on Windows or Linux, anywhere.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import config
import db
from console import inr
from execution import PaperBroker, compute_fees, slip
from indicators import Bar, atr, rsi, sma, vwap, zscores
from mkttime import (bar_times, completed_bar_until, session_phase)
from strategy import StockContext, Strategy

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        PASSES.append(name)
        print(f"  [PASS] {name}")
    except Exception:
        FAILS.append(name)
        print(f"  [FAIL] {name}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
def test_imports():
    import backtest, data_fetch, demo_data, engine, execution, indicators
    import main, patch, strategy, upstox_client, mkttime, console  # noqa


def test_indicators():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    up = list(range(100, 140))
    assert abs(rsi(up, 14)[-1] - 100.0) < 1e-9
    down = list(range(140, 100, -1))
    assert abs(rsi(down, 14)[-1]) < 1e-9
    bars = [Bar(f"2026-01-01 09:{15 + 5 * i:02d}:00", 100, 101, 99, 100, 10)
            for i in range(30)]
    a = atr(bars, 14)
    assert a[-1] is not None and abs(a[-1] - 2.0) < 1e-9  # TR = 2 constant
    z = zscores([100.0] * 25 + [99.0], 20)
    assert z[-1] is not None and z[-1] < -0.9
    bv = [Bar("t", 10, 11, 9, 10, 100), Bar("t2", 10, 12, 10, 12, 100)]
    # typical price = (h+l+c)/3
    assert abs(vwap(bv) - (((11 + 9 + 10) / 3 + (12 + 10 + 12) / 3) / 2)) < 1e-9


def test_fees():
    # value = 1000 x 100 = INR 1,00,000
    f = compute_fees("BUY", 1000.0, 100)
    assert abs(f["brokerage"] - 20.0) < 1e-9
    assert abs(f["stt"] - 25.0) < 1e-9          # 0.025% of 1e5
    assert abs(f["exchange"] - 19.0) < 1e-9     # 0.019% of 1e5
    assert abs(f["sebi"] - 0.1) < 1e-9          # 0.0001% of 1e5
    assert abs(f["stamp"] - 15.0) < 1e-9        # 0.015% of 1e5
    assert abs(f["gst"] - 0.18 * (20 + 19 + 0.1)) < 1e-9
    assert abs(f["total"] - (20 + 25 + 19 + 0.1 + 15 + 0.18 * 39.1)) < 1e-9
    s = compute_fees("SELL", 1000.0, 100)
    assert s["stamp"] == 0.0
    assert abs(slip(1000, "BUY") - 1000.5) < 1e-9    # 5 bps = 0.05%
    assert abs(slip(1000, "SELL") - 999.5) < 1e-9


def test_db_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        conn = db.get_conn(os.path.join(td, "t.db"))
        db.init_db(conn)
        t = "2026-09-14 09:15:00"
        assert db.upsert_candle(conn, "X", t, 1, 2, 0.5, 1.5, 10) is True
        assert db.upsert_candle(conn, "X", t, 9, 9, 9, 9, 99) is False  # immutable
        bars = db.get_day_bars(conn, "X", "2026-09-14")
        assert len(bars) == 1 and bars[0].c == 1.5
        assert db.mark_processed(conn, "X", t, "scan") is True
        assert db.mark_processed(conn, "X", t, "scan") is False
        pid = db.open_position(conn, "X", "paper", 10, 100.0, t, 99.0, 101.0,
                               0.5, 10.0, "test", None)
        assert db.get_open_positions(conn)[0]["id"] == pid
        db.close_position(conn, pid, 102.0, "2026-09-14 10:00:00", "TARGET",
                          10.0, "test", None)
        p = db.get_position(conn, pid)
        assert p["exit_reason"] == "TARGET"
        assert abs(p["pnl_net"] - (2.0 * 10 - 10.0 - 10.0)) < 1e-9
        db.upsert_live(conn, "X", "t2", 1, 2, 0.5, 1.5, 5)
        assert db.get_live(conn, "X")[6] == 5
        db.del_live(conn, "X")
        assert db.get_live(conn, "X") is None


def _dip_bars() -> list[Bar]:
    """45 calm bars ~100, a 3-bar dip, then green reclaim bars."""
    bars = []
    prev = 100.0
    for i in range(45):
        c = 100.0 + 0.30 * math.sin(i * 0.5)
        o = prev
        bars.append(Bar(f"2026-09-14 09:{15 + 5 * i:02d}:00" if i < 7
                        else f"2026-09-14 {10 + (i - 7) * 5 // 60:02d}:"
                             f"{(15 + (i - 7) * 5) % 60:02d}:00",
                        o, max(o, c) * 1.0004, min(o, c) * 0.9996, c, 1000))
        prev = c
    dip = [prev * 0.9945, prev * 0.9895, prev * 0.9845]
    for j, c in enumerate(dip):
        o = bars[-1].c
        bars.append(Bar(f"2026-09-14 12:{15 + 5 * j:02d}:00", o,
                        max(o, c), min(o, c), c, 1500))
    rec = [dip[-1] * 1.005, dip[-1] * 1.010, dip[-1] * 1.014, dip[-1] * 1.017]
    for j, c in enumerate(rec):
        o = bars[-1].c
        bars.append(Bar(f"2026-09-14 12:{30 + 5 * j:02d}:00", o,
                        max(o, c), min(o, c), c, 1200))
    return bars


CTX_UP = StockContext(symbol="X", date="2026-09-14", daily_close=100.0,
                      sma20=98.0, sma50=95.0, atr14=1.0, avg_vol20=500000,
                      trend_ok=1)


def test_strategy_entry_exit():
    strat = Strategy()
    bars = _dip_bars()
    entry_idx = None
    for i in range(45, len(bars)):
        plan, sigs, skip = strat.evaluate_entry("X", CTX_UP, bars[: i + 1],
                                                now_hm="12:30",
                                                equity=1_000_000, cash=1_000_000)
        if plan:
            entry_idx = i
            plan_obj = plan
            break
    assert entry_idx is not None, "expected an entry on the reclaim bar"
    assert plan_obj.qty >= 1
    assert plan_obj.stop < plan_obj.price < plan_obj.target

    # now ride it back to the mean -> a TARGET/MEAN/EOD exit must fire
    pos = {"stop": plan_obj.stop, "target": plan_obj.target}
    last = bars[-1].c
    extra = []
    for j in range(12):
        c = last * 1.002
        o = last
        t = f"2026-09-14 13:{5 + 5 * j:02d}:00"
        extra.append(Bar(t, o, max(o, c), min(o, c), c, 800))
        last = c
    exit_fired = None
    for j in range(len(extra)):
        allb = bars + extra[: j + 1]
        call, new_stop = strat.evaluate_exit(pos, allb, now=parse_ist(
            extra[j].t), is_last_bar=(j == len(extra) - 1))
        if call:
            exit_fired = (call.reason, j)
            break
    assert exit_fired is not None, "expected a mean-reversion exit"
    assert exit_fired[0] in ("TARGET", "MEAN", "EOD")
    # guards
    _, _, skip = strat.evaluate_entry("X", CTX_UP, bars[:20], "12:30", 1e6, 1e6)
    assert skip == "warmup"
    _, _, skip2 = strat.evaluate_entry("X", None, bars, "12:30", 1e6, 1e6)
    assert skip2.startswith("trend filter")
    _, _, skip3 = strat.evaluate_entry("X", CTX_UP, bars, "15:00", 1e6, 1e6)
    assert skip3 == "after last-entry time"


def parse_ist(s):
    from mkttime import parse_t
    return parse_t(s)


def test_paper_broker():
    with tempfile.TemporaryDirectory() as td:
        conn = db.get_conn(os.path.join(td, "t.db"))
        db.init_db(conn)
        b = PaperBroker(conn)
        c0 = b.cash()
        assert abs(c0 - config.PAPER_START_EQUITY) < 1e-9
        pid = b.buy("X", 100.0, 10, "2026-09-14 10:00:00", 99.0, 101.0, 0.5, "t")
        assert pid
        c1 = b.cash()
        assert c1 < c0
        fees_buy = compute_fees("BUY", slip(100.0, "BUY"), 10)["total"]
        _, eq = b.equity({"X": 100.0})
        # equity drops only by the slippage cost (marked at 100) + fees
        expected = c0 - (slip(100.0, "BUY") - 100.0) * 10 - fees_buy
        assert abs(eq - expected) < 1e-6
        pnl = b.sell(pid, 110.0, "2026-09-14 11:00:00", "TARGET", "t")
        assert pnl is not None and pnl > 80  # ~100 gross minus fees/slippage
        assert abs(b.cash() - (c1 + 110.0 * 10 * (1 - config.SLIPPAGE_BPS / 1e4)
                               - compute_fees("SELL", slip(110.0, "SELL"), 10)["total"])
               ) < 1e-6
        p = db.get_position(conn, pid)
        assert p["exit_reason"] == "TARGET"
        assert not db.get_open_positions(conn)


def test_mkttime():
    from mkttime import ist_now  # noqa
    mon = datetime(2026, 9, 14, 9, 0)
    sat = datetime(2026, 9, 19, 10, 0)
    for d, phase in [(datetime(2026, 9, 14, 8, 0), "CLOSED"),
                     (datetime(2026, 9, 14, 8, 50), "PRE_OPEN"),
                     (datetime(2026, 9, 14, 9, 20), "OPEN"),
                     (datetime(2026, 9, 14, 15, 25), "OPEN"),
                     (datetime(2026, 9, 14, 15, 40), "POST_CLOSE"),
                     (datetime(2026, 9, 14, 20, 0), "CLOSED"),
                     (sat, "CLOSED")]:
        assert session_phase(d) == phase, (d, session_phase(d), phase)
    assert len(bar_times("2026-09-14")) == 75
    cb = completed_bar_until(datetime(2026, 9, 14, 9, 20, 30))
    assert cb and cb.strftime("%H:%M") == "09:15"
    assert completed_bar_until(datetime(2026, 9, 14, 9, 16, 10)) is None


def test_full_pipeline():
    import backtest
    import demo_data
    with tempfile.TemporaryDirectory() as td:
        conn = db.get_conn(os.path.join(td, "bt.db"))
        db.init_db(conn)
        syms = config.load_universe()[:12]
        first, last, n = demo_data.seed_demo(conn, days=6, symbols=syms,
                                             seed=7, quiet=True)
        assert n == 12
        res = backtest.run_backtest(conn, first, last, syms,
                                    config.PAPER_START_EQUITY, quiet=True)
        assert len(res["days"]) == 6
        assert res["n_trades"] >= 3, f"demo data should produce trades, got {res['n_trades']}"
        assert res["equity"]
        assert res["end_equity"] > 0
        out = os.path.join(td, "report.md")
        backtest.write_report(res, Path(out))
        assert os.path.exists(out)
        # purge-demo must clean up
        before = db.count_rows(conn, "candles_5m")
        outp = db.purge_demo(conn)
        assert outp.get("candles_5m", 0) == before
        assert db.count_rows(conn, "candles_5m") == 0


def test_engine_simulation():
    """Full live-engine lifecycle against a mock Upstox client (subprocess):
    pre-open context, sync, scan, entries, tick exits, EOD flatten,
    post-close finalisation, and RESTART safety on the same DB."""
    import subprocess
    import sys as _sys
    root = Path(__file__).resolve().parent
    p = subprocess.run([_sys.executable, str(root / "test_engine_sim.py")],
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise AssertionError(
            "engine simulation failed:\n" + (p.stdout or "")[-3000:]
            + "\n-- stderr --\n" + (p.stderr or "")[-3000:])
    last = [ln for ln in p.stdout.splitlines() if ln.strip()][-3:]
    print("    " + "\n    ".join(last))


def main() -> int:
    print("=" * 70)
    print("STMR verify_all - offline self check")
    print("=" * 70)
    check("imports (all modules)", test_imports)
    check("indicators (sma/rsi/atr/zscore/vwap)", test_indicators)
    check("fee model", test_fees)
    check("db round-trips (immutable bars, processed guard, trades)",
          test_db_roundtrip)
    check("strategy: dip -> entry -> mean exit (+guards)", test_strategy_entry_exit)
    check("paper broker round-trip (cash & pnl)", test_paper_broker)
    check("market-time helpers (phases, boundaries)", test_mkttime)
    check("full pipeline: demo seed -> backtest -> report -> purge",
          test_full_pipeline)
    check("engine simulation: full session + restart safety (mock API)",
          test_engine_simulation)
    print()
    print(f"  {len(PASSES)} passed, {len(FAILS)} failed")
    if FAILS:
        print("  FAILED: " + ", ".join(FAILS))
        return 1
    print("  ALL GREEN - the engine is ready to run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
