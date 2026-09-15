"""
backtest.py - replay the EXACT same strategy engine against the SQLite DB.

Why this is meaningful
  * The live engine (engine.py) and this replay use the SAME strategy.py
    functions on the SAME bars stored in the DB, so a backtest IS a
    re-execution of what the live engine saw (minus intra-bar tick exits,
    which can only make live exits earlier - the replay is conservative).
  * Exits inside a bar are emulated the standard conservative way:
        bar open <= stop      -> filled at the open (gap-through, worse)
        else bar low  <= stop -> filled at the stop
        bar open >= target    -> filled at the open (gap-through, better)
        else bar high >= target-> filled at the target
      (if stop and target both fall in one bar, the STOP is assumed first)

Usage
  python backtest.py                          # everything in the DB
  python backtest.py --start 2026-09-01 --end 2026-09-15
  python backtest.py --symbols RELIANCE,TCS
  python backtest.py --compare                # ALSO compare live paper trades
                                               # stored in the DB with replay
  python backtest.py --seed-demo 10           # seed 10 days of SYNTHETIC data
  python backtest.py --seed-demo 10 --db data/demo.db   # keep demo data isolated
  python backtest.py --seed-demo --out reports/demo.md
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import config
import db
import demo_data
from console import banner, inr, now_str, pct, signed, table
from execution import compute_fees, slip
from mkttime import bar_times, parse_t
from strategy import StockContext, Strategy


# ===========================================================================
# replay
# ===========================================================================
def run_backtest(conn, start: str, end: str, symbols: list[str],
                 start_equity: float, quiet: bool = False) -> dict:
    strat = Strategy()
    days = db.distinct_dates(conn, start, end, symbols)
    if not days:
        print(f"[backtest] no 5-min data in the DB for {start}..{end} - nothing "
              f"to replay. (The Upstox API only returns today's intraday bars, "
              f"so history must first be accumulated by running the engine, or "
              f"use --seed-demo for synthetic data.)")
        return {"days": [], "trades": [], "equity": []}

    if not quiet:
        print(f"replaying {len(days)} day(s) x {len(symbols)} symbols, "
              f"starting equity INR {inr(start_equity)} ...")

    cash = start_equity
    realized = 0.0
    positions: dict = {}          # symbol -> pos dict
    trades: list[dict] = []       # every closed trade
    equity_pts: list[tuple] = []  # (ts, equity, cash, n_open, realized)

    for day in days:
        bars_by_sym = {s: db.get_day_bars(conn, s, day) for s in symbols}
        nonempty = [b for b in bars_by_sym.values() if b]
        if not nonempty:
            continue
        n_bars = max(len(b) for b in nonempty)
        times = bar_times(day)

        # daily context for this day (no lookahead: strictly earlier days)
        ctx_by_sym: dict = {}
        for s in symbols:
            row = db.get_context(conn, s, day)
            ctx = StockContext.from_row(row) if row else None
            if ctx is None:
                import data_fetch
                c = data_fetch.context_from_daily(conn, s, day)
                ctx = StockContext(symbol=s, date=day, **{
                    "daily_close": c["daily_close"], "sma20": c["sma20"],
                    "sma50": c["sma50"], "atr14": c["atr14"],
                    "avg_vol20": c["avg_vol20"], "trend_ok": c["trend_ok"]
                }) if c else None
            ctx_by_sym[s] = ctx

        for i in range(n_bars):
            t_str = (times[i].strftime("%Y-%m-%d %H:%M:%S")
                     if i < len(times) else None)

            # ---------------- exits first (conservative ordering) ----------
            for s in list(positions.keys()):
                bs = bars_by_sym[s]
                if i >= len(bs):
                    continue
                b = bs[i]
                pos = positions[s]
                call, new_stop = strat.evaluate_exit(
                    pos, bs[: i + 1], tick=None,
                    now=parse_t(b.t), is_last_bar=(i == len(bs) - 1))
                if call:
                    fill = slip(call.price, "SELL")
                    fees = compute_fees("SELL", fill, pos["qty"])
                    pnl = ((fill - pos["entry_fill"]) * pos["qty"]
                           - pos["fees_buy"] - fees["total"])
                    cash += fill * pos["qty"] - fees["total"]
                    realized += pnl
                    rec = dict(pos)
                    rec.update({"exit_fill": fill, "exit_time": b.t,
                                "exit_reason": call.reason,
                                "exit_fees": fees["total"], "pnl_net": pnl,
                                "exit_note": call.note, "day": day})
                    trades.append(rec)
                    del positions[s]
                    if not quiet:
                        print(f"  [{day} {b.t[11:16]}] EXIT  {s:<11} "
                              f"SELL {pos['qty']} @ {fill:.2f}  "
                              f"reason: {call.reason:<8} pnl {signed(pnl)}")
                elif new_stop > pos["stop"] + 1e-9:
                    pos["stop"] = new_stop  # trail (in-place, persisted per-day only)

            # ---------------- entries --------------------------------------
            if len(positions) < config.MAX_POSITIONS:
                for s in symbols:
                    bs = bars_by_sym[s]
                    if i < config.WARMUP_BARS - 1 or i >= len(bs) or s in positions:
                        continue
                    b = bs[i]
                    plan, sigs, skip = strat.evaluate_entry(
                        s, ctx_by_sym.get(s), bs[: i + 1],
                        now_hm=b.t[11:16],
                        equity=cash + sum(p["qty"] * bars_by_sym[p["symbol"]][i].c
                                          for p in positions.values()
                                          if i < len(bars_by_sym[p["symbol"]])),
                        cash=cash)
                    if not plan:
                        continue
                    fill = slip(b.c, "BUY")
                    fees = compute_fees("BUY", fill, plan.qty)
                    cost = fill * plan.qty + fees["total"]
                    if cost > cash:
                        continue
                    cash -= cost
                    positions[s] = {
                        "symbol": s, "qty": plan.qty, "entry_fill": fill,
                        "entry_time": b.t, "stop": plan.stop, "target": plan.target,
                        "atr_entry": plan.atr, "fees_buy": fees["total"],
                        "entry_note": plan.reason, "z": plan.z, "day": day,
                    }
                    if not quiet:
                        print(f"  [{day} {b.t[11:16]}] ENTRY {s:<11} "
                              f"BUY {plan.qty} @ {fill:.2f}  stop {plan.stop:.2f}  "
                              f"target {plan.target:.2f}")

            # ---------------- mark to market -------------------------------
            mv = 0.0
            for s, pos in positions.items():
                if i < len(bars_by_sym[s]):
                    mv += pos["qty"] * bars_by_sym[s][i].c
            ts = t_str
            if ts is None:
                for s2, b2 in bars_by_sym.items():
                    if i < len(b2):
                        ts = b2[i].t
                        break
                ts = ts or day
            equity_pts.append((ts, cash + mv, cash, len(positions), realized))

        # safety: anything still open (shouldn't happen - EOD rule closes)
        for s, pos in list(positions.items()):
            bs = bars_by_sym[s]
            if bs:
                b = bs[-1]
                fill = slip(b.c, "SELL")
                fees = compute_fees("SELL", fill, pos["qty"])
                pnl = ((fill - pos["entry_fill"]) * pos["qty"]
                       - pos["fees_buy"] - fees["total"])
                cash += fill * pos["qty"] - fees["total"]
                realized += pnl
                rec = dict(pos)
                rec.update({"exit_fill": fill, "exit_time": b.t,
                            "exit_reason": "EOD", "exit_fees": fees["total"],
                            "pnl_net": pnl, "exit_note": "end-of-data flatten",
                            "day": day})
                trades.append(rec)
            del positions[s]

    # ------------------------------------------------------------------ stats
    return _stats(conn, start, end, days, trades, equity_pts,
                  start_equity, realized, cash)


def _stats(conn, start, end, days, trades, equity_pts, start_equity,
           realized, cash) -> dict:
    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    gross_win = sum(t["pnl_net"] for t in wins)
    gross_loss = -sum(t["pnl_net"] for t in losses)
    net = sum(t["pnl_net"] for t in trades)

    peak = -1e18
    max_dd = 0.0
    for _, eq, *_ in equity_pts:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    by_reason: dict[str, list] = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], []).append(t["pnl_net"])
    by_symbol: dict[str, list] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl_net"]

    # daily returns -> annualised sharpe (defensive)
    daily_eq: dict[str, float] = {}
    for ts, eq, *_ in equity_pts:
        daily_eq[ts[:10]] = eq
    rets = []
    prev = None
    for d in sorted(daily_eq):
        if prev:
            rets.append(daily_eq[d] / prev - 1)
        prev = daily_eq[d]
    sharpe = None
    if len(rets) >= 2:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        if sd > 1e-12:
            sharpe = mu / sd * math.sqrt(252)

    # avg holding bars
    hold = []
    for t in trades:
        try:
            hold.append((parse_t(t["exit_time"]) - parse_t(t["entry_time"])).total_seconds()
                        / (config.BAR_MINUTES * 60))
        except Exception:
            pass

    return {
        "start": start, "end": end, "days": days, "trades": trades,
        "equity": equity_pts,
        "start_equity": start_equity, "end_equity": cash + 0.0,
        "net_pnl": net, "return_pct": net / start_equity * 100,
        "n_trades": len(trades), "n_wins": len(wins),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        if gross_win > 0 else 0.0,
        "max_dd_pct": max_dd * 100,
        "sharpe": sharpe,
        "avg_hold_bars": (sum(hold) / len(hold)) if hold else 0.0,
        "by_reason": by_reason, "by_symbol": by_symbol, "by_day": by_day,
    }


# ===========================================================================
# reporting
# ===========================================================================
def print_report(res: dict) -> None:
    if not res["trades"]:
        print("\nNo trades generated in this window (filters may have been too "
              "tight for the data, or the trend filter excluded most names).")
        return
    banner("BACKTEST RESULTS - SHORT-TERM MEAN REVERSION",
           [f"window     : {res['start']} .. {res['end']}  ({len(res['days'])} days)",
            f"engine     : same strategy.py as the live engine (replay on stored bars)"])
    print()
    table(["metric", "value"], [
        ["net P&L", f"INR {signed(res['net_pnl'])}"],
        ["return", pct(res["return_pct"])],
        ["trades", res["n_trades"]],
        ["win rate", f"{res['win_rate']:.1f}%  ({res['n_wins']}W / {res['n_trades'] - res['n_wins']}L)"],
        ["avg win / avg loss", f"INR {signed(res['avg_win'])} / INR {signed(res['avg_loss'])}"],
        ["profit factor", f"{res['profit_factor']:.2f}" if res["profit_factor"] != float("inf") else "inf"],
        ["max drawdown", pct(res["max_dd_pct"], 2)],
        ["sharpe (daily, ann.)", f"{res['sharpe']:.2f}" if res["sharpe"] is not None else "n/a"],
        ["avg holding", f"{res['avg_hold_bars']:.1f} bars ({res['avg_hold_bars'] * 5:.0f} min)"],
        ["end equity", f"INR {inr(res['end_equity'])}"],
    ], aligns=["l", "r"])
    print()
    print("  exit reasons:")
    for reason, pnls in sorted(res["by_reason"].items(),
                               key=lambda kv: -len(kv[1])):
        w = sum(1 for p in pnls if p > 0)
        print(f"    {reason:<10} {len(pnls):>4} trades  wins {w:>3}  "
              f"net {signed(sum(pnls)):>12}")
    print()
    print("  daily P&L:")
    for d in sorted(res["by_day"]):
        print(f"    {d}   {signed(res['by_day'][d]):>12}")
    if len(res["by_day"]) > 8:
        print("    ... (see report file for the full list)")
    print()
    top = sorted(res["by_symbol"].items(),
                 key=lambda kv: -sum(t["pnl_net"] for t in kv[1]))
    print("  top / bottom symbols:")
    shown = 0
    for s, ts in top:
        if shown >= 5:
            break
        net = sum(t["pnl_net"] for t in ts)
        print(f"    {s:<12} {len(ts):>3} trades  net {signed(net):>12}")
        shown += 1
    for s, ts in reversed(top[-3:]):
        net = sum(t["pnl_net"] for t in ts)
        print(f"    {s:<12} {len(ts):>3} trades  net {signed(net):>12}   (bottom)")


def write_report(res: dict, path) -> None:
    L = []
    L.append(f"# STMR Backtest Report - {res['start']} .. {res['end']}")
    L.append("")
    L.append(f"Generated: {now_str()} IST by backtest.py v{config.VERSION}")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(f"- Net P&L: **INR {signed(res['net_pnl'])}** "
             f"({pct(res['return_pct'])} on {inr(res['start_equity'])})")
    L.append(f"- Trades: {res['n_trades']} | win rate {res['win_rate']:.1f}% | "
             f"avg win {signed(res['avg_win'])} | avg loss {signed(res['avg_loss'])}")
    pf = res["profit_factor"]
    L.append(f"- Profit factor: {pf:.2f}" if pf != float("inf")
             else "- Profit factor: inf")
    L.append(f"- Max drawdown: {res['max_dd_pct']:.2f}% | "
             f"Sharpe (daily, ann.): "
             f"{res['sharpe']:.2f}" if res["sharpe"] is not None
             else f"- Max drawdown: {res['max_dd_pct']:.2f}% | Sharpe: n/a")
    L.append(f"- Avg holding: {res['avg_hold_bars']:.1f} bars "
             f"({res['avg_hold_bars'] * 5:.0f} min)")
    L.append(f"- End equity: INR {inr(res['end_equity'])}")
    L.append("")
    L.append("## Exit reasons")
    L.append("")
    L.append("| reason | trades | wins | net P&L |")
    L.append("|--------|--------|------|---------|")
    for reason, pnls in sorted(res["by_reason"].items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for p in pnls if p > 0)
        L.append(f"| {reason} | {len(pnls)} | {w} | {signed(sum(pnls))} |")
    L.append("")
    L.append("## Per-symbol")
    L.append("")
    L.append("| symbol | trades | wins | net P&L |")
    L.append("|--------|--------|------|---------|")
    for s, ts in sorted(res["by_symbol"].items(),
                        key=lambda kv: -sum(t["pnl_net"] for t in kv[1])):
        w = sum(1 for t in ts if t["pnl_net"] > 0)
        L.append(f"| {s} | {len(ts)} | {w} | {signed(sum(t['pnl_net'] for t in ts))} |")
    L.append("")
    L.append("## Trades")
    L.append("")
    L.append("| day | symbol | qty | entry | exit | reason | net P&L | why (entry) |")
    L.append("|-----|--------|-----|-------|------|--------|---------|-------------|")
    for t in res["trades"]:
        note = (t.get("entry_note") or "").replace("|", "/")[:90]
        L.append(f"| {t['day']} | {t['symbol']} | {t['qty']} | "
                 f"{t['entry_fill']:.2f} @ {t['entry_time'][11:16]} | "
                 f"{t['exit_fill']:.2f} @ {t['exit_time'][11:16]} | "
                 f"{t['exit_reason']} | {signed(t['pnl_net'])} | {note} |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def write_trades_csv(res: dict, path) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "symbol", "qty", "entry_time", "entry_fill",
                    "stop", "target", "exit_time", "exit_fill", "exit_reason",
                    "entry_fees", "exit_fees", "pnl_net", "entry_note"])
        for t in res["trades"]:
            w.writerow([t["day"], t["symbol"], t["qty"], t["entry_time"],
                        f"{t['entry_fill']:.2f}", f"{t['stop']:.2f}",
                        f"{t['target']:.2f}", t["exit_time"],
                        f"{t['exit_fill']:.2f}", t["exit_reason"],
                        f"{t['fees_buy']:.2f}", f"{t['exit_fees']:.2f}",
                        f"{t['pnl_net']:.2f}", t.get("entry_note", "")])


# ===========================================================================
# live-vs-backtest comparison (engine validation)
# ===========================================================================
def compare_live(conn, res: dict) -> None:
    """Compare the LIVE paper trades stored in the DB (same window) with the
    replay. This is the check that 'the engine behaves like its backtest'."""
    live = db.trades_in_range(conn, res["start"], res["end"], source="paper")
    if not live:
        print("\n[compare] no live paper trades in this window to compare against.")
        return
    bt_map: dict = {}
    for t in res["trades"]:
        bt_map.setdefault((t["symbol"], t["day"]), []).append(t)

    banner("LIVE ENGINE vs BACKTEST REPLAY",
           ["live trades read from the trades table (source='paper')",
            "match = same symbol+day with entry prices within 0.5%"])
    rows = []
    n_match = 0
    for lt in live:
        day = lt["entry_time"][:10]
        cands = bt_map.get((lt["symbol"], day), [])
        match = None
        for bt in cands:
            if abs(bt["entry_fill"] - lt["entry_fill"]) / lt["entry_fill"] < 0.005:
                match = bt
                break
        if match:
            n_match += 1
            rows.append([lt["symbol"], day,
                         f"{lt['entry_time'][11:16]} @ {lt['entry_fill']:.2f}",
                         f"{match['entry_time'][11:16]} @ {match['entry_fill']:.2f}",
                         lt["exit_reason"] or "OPEN", match["exit_reason"],
                         "MATCH"])
        else:
            rows.append([lt["symbol"], day,
                         f"{lt['entry_time'][11:16]} @ {lt['entry_fill']:.2f}",
                         "n/a", lt["exit_reason"] or "OPEN", "-", "DIFF"])
    table(["symbol", "day", "live entry", "backtest entry", "live exit",
           "bt exit", "verdict"], rows,
          aligns=["l", "l", "r", "r", "l", "l", "l"])
    print(f"\n  {n_match}/{len(live)} live entries reproduced by the replay. "
          "Small differences are expected where the live engine acted on "
          "intra-bar ticks (faster exits) or on a mid-day start.")
    return n_match


# ===========================================================================
# CLI
# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="STMR backtest / replay engine")
    ap.add_argument("--db", default=None, help="SQLite DB (default data/trading.db)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: earliest in DB)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: latest in DB)")
    ap.add_argument("--symbols", default=None, help="comma-separated subset")
    ap.add_argument("--equity", type=float, default=config.PAPER_START_EQUITY,
                    help="starting equity (default 10,00,000)")
    ap.add_argument("--compare", action="store_true",
                    help="also compare live paper trades with the replay")
    ap.add_argument("--seed-demo", type=int, nargs="?", const=10, default=None,
                    metavar="DAYS", help="seed DAYS of synthetic data first (default 10)")
    ap.add_argument("--demo-seed", type=int, default=42, help="demo RNG seed")
    ap.add_argument("--demo-symbols", type=int, default=None,
                    help="limit demo data to first N symbols (default all)")
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    conn = db.get_conn(a.db or config.DB_PATH)
    db.init_db(conn)

    start, end = a.start, a.end
    if a.seed_demo is not None:
        syms = config.load_universe()
        if a.demo_symbols:
            syms = syms[: a.demo_symbols]
        first, last, n = demo_data.seed_demo(conn, days=a.seed_demo,
                                             symbols=syms, seed=a.demo_seed,
                                             quiet=a.quiet)
        start = start or first
        end = end or last
        if not a.quiet:
            print(f"[demo] seeded {n} symbols: {first} .. {last} "
                  f"(tagged source='demo'; remove with: python patch.py purge-demo)")

    days = db.distinct_dates(conn, "2000-01-01", "9999-12-31")
    if not days:
        print("DB is empty - nothing to replay. Run the engine during market "
              "hours first, or use --seed-demo.")
        return 1
    start = start or days[0]
    end = end or days[-1]

    symbols = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()]
               if a.symbols else config.load_universe())
    # restrict to symbols actually present in the window
    present = set()
    for d in db.distinct_dates(conn, start, end):
        cov = db.day_coverage(conn, d)
        present.update(cov)
    symbols = [s for s in symbols if s in present] or [s for s in config.load_universe() if s in present]

    res = run_backtest(conn, start, end, symbols, a.equity, quiet=a.quiet)
    print()
    print_report(res)

    if res["trades"] or res["equity"]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_md = Path(a.out) if a.out else config.REPORTS_DIR / f"backtest_{stamp}.md"
        write_report(res, out_md)
        csv_path = out_md.with_suffix(".md").name.replace(".md", "_trades.csv")
        csv_path = out_md.parent / csv_path
        write_trades_csv(res, csv_path)
        print(f"\n  report : {out_md}")
        print(f"  trades : {csv_path}")

    if a.compare:
        print()
        compare_live(conn, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
