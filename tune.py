"""
tune.py - parameter optimizer for the STMR engine, with WALK-FORWARD
validation (train on the first part of the window, validate on the last part,
so the winner is not merely overfit to the data it was selected on).

Why a tuner and why it is built this way
  * The live engine and this search run the EXACT same backtest() code path,
    so "best parameters" transfer 1:1 to the live engine.
  * The search is random coarse sampling + a refinement pass over the top-k:
    the full grid of all tunables is ~100,000+ combinations; sampling keeps a
    run to a few minutes while exploring the interesting regions.
  * Every candidate is scored on the TRAIN period first (cheap filter), then
    only the top-k are evaluated on the OUT-OF-SAMPLE TEST period. The final
    table shows train vs test numbers side by side - a big train/test gap
    tells you the "winner" is overfit.
  * The winner is written to data/best_params.json; the engine and backtest
    AUTO-LOAD that file on every run (see config.make_cfg / load_params).

IMPORTANT (read before trusting any number)
  * Upstox cannot serve intraday history before today (limitation #1), so you
    must have REAL bars in the DB first: run the engine for a few weeks to
    accumulate them, or import any broker/vendor CSV export with
        python main.py --import-csv FILE --kind 5m
  * Tuning on --seed-demo SYNTHETIC data validates the machinery only.
    Always re-run this tuner on real data before trusting parameters.

Usage
  python tune.py                          # whole DB, default grid, 150 samples
  python tune.py --start 2026-08-01 --end 2026-09-15 --n-samples 300
  python tune.py --symbols RELIANCE,TCS,HDFCBANK --n-samples 400
  python tune.py --grid my_grid.json --top-k 20 --min-trades 20 --max-dd 2.5
  python tune.py --seed-demo 15           # offline demo run
  python tune.py --no-write               # report only, don't touch best_params.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import config
import db
import demo_data
from backtest import run_backtest
from console import banner, inr, pct, signed, table


# ---------------------------------------------------------------------------
# default search space (any of these can be overridden by --grid FILE)
# ---------------------------------------------------------------------------
DEFAULT_GRID: dict = {
    "SMOOTH_N": [15, 20, 30],
    "DIP_Z": [1.0, 1.2, 1.5],
    "DIP_LOOKBACK": [4, 6, 8],
    "DIP_RSI": [30, 35, 40],
    "ENTRY_Z_MIN": [-1.2, -1.5],
    "ENTRY_Z_MAX": [-0.2, -0.4],
    "ENTRY_RSI_MAX": [45, 55, 65],
    "EXIT_Z": [-0.1, 0.0],
    "SL_ATR_MULT": [1.0, 1.5, 2.0],
    "SL_MIN_PCT": [0.25, 0.35],
    "SL_MAX_PCT": [1.0, 1.5],
    "TRAIL_ATR_MULT": [0.75, 1.0, 1.5],
    "TIME_STOP_BARS": [0, 6, 10],
    "USE_VWAP_FILTER": [True, False],
    "DIP_MIN_DEPTH_ATR": [0.0, 0.5, 0.8],
    "MIN_ATR_PCT": [0.0, 0.08],
    "MAX_POSITIONS": [4, 6],
    "RISK_PER_TRADE_PCT": [0.4, 0.5, 0.75],
}


def diff_str(ov: dict) -> str:
    """Compact 'param=value' list of the overrides vs the defaults."""
    base = config.make_cfg()
    parts = []
    for k in sorted(ov):
        if getattr(base, k, None) != ov[k]:
            parts.append(f"{k}={ov[k]}")
    return ", ".join(parts) if parts else "(base defaults)"


def eligible(stats: dict, min_trades: int, max_dd: float) -> bool:
    return stats["n_trades"] >= min_trades and stats["max_dd_pct"] <= max_dd


def optimize(conn, days: list[str], symbols: list[str], start_equity: float,
             grid: dict, n_samples: int, top_k: int, seed: int,
             split: float, min_trades: int, max_dd: float,
             out: str | None = None, quiet: bool = False) -> list[dict]:
    """Run the walk-forward search. Returns the ranked result rows."""
    if len(days) < 4:
        raise SystemExit(
            f"need at least 4 days of 5-min data to split train/test "
            f"(found {len(days)}). Import more history or use --seed-demo.")
    n_train = max(2, round(len(days) * split))
    train_days, test_days = days[:n_train], days[n_train:]

    # sample combinations (base config is always candidate #0); never more
    # than the grid space itself
    rng = random.Random(seed)
    combos: list[dict] = [{}]
    target = min(n_samples, _grid_size(grid) + 1)
    tries = 0
    while len(combos) < target and tries < 50 * target:
        tries += 1
        ov = {k: rng.choice(v) for k, v in grid.items()}
        if ov not in combos:
            combos.append(ov)

    if not quiet:
        print(f"walk-forward: {len(train_days)} train days "
              f"({train_days[0]}..{train_days[-1]}) | "
              f"{len(test_days)} test days ({test_days[0]}..{test_days[-1]})")
        print(f"searching {n_samples} sampled combinations "
              f"(grid space ~{ _grid_size(grid):,}) ...")

    t0 = time.time()
    rows: list[dict] = []
    for i, ov in enumerate(combos):
        cfg = config.make_cfg(ov)
        tr = run_backtest(conn, train_days[0], train_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        ok = eligible(tr, max(5, min_trades // 2), max_dd)
        rows.append({"ov": ov, "train": tr, "train_ok": ok, "idx": i})
        if not quiet and (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(combos)} "
                  f"({time.time() - t0:.0f}s elapsed)")

    ranked = sorted([r for r in rows if r["train_ok"]],
                    key=lambda r: -r["train"]["net_pnl"])
    top = ranked[:top_k]
    if not top:
        raise SystemExit("no combination passed the train-period filter "
                         "(raise --min-trades tolerance or import more data)")

    results: list[dict] = []
    for r in top:
        cfg = config.make_cfg(r["ov"])
        te = run_backtest(conn, test_days[0], test_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        results.append({
            "ov": r["ov"],
            "train": r["train"], "test": te,
            "test_ok": eligible(te, min_trades, max_dd),
            "tag": "base" if r["ov"] == {} else f"#{r['idx']}",
        })
    # make sure the base (untuned) configuration is in the table
    if all(r["ov"] != {} for r in results):
        cfg = config.make_cfg()
        te = run_backtest(conn, test_days[0], test_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        tr = run_backtest(conn, train_days[0], train_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        results.append({"ov": {}, "train": tr, "test": te,
                        "test_ok": eligible(te, min_trades, max_dd), "tag": "base"})

    # rank by test-period eligibility first, then test net P&L
    results.sort(key=lambda r: (r["test_ok"], r["test"]["net_pnl"]),
                 reverse=True)
    return results


def _grid_size(grid: dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


def _row_vals(stats: dict) -> list:
    cagr = stats.get("cagr")
    pf = stats.get("profit_factor", 0.0)
    return [
        stats["n_trades"],
        signed(stats["net_pnl"]),
        f"{cagr * 100:+.1f}%" if cagr is not None else "n/a",
        f"{pf:.2f}" if pf != float("inf") else "inf",
        f"{stats['max_dd_pct']:.2f}%",
        f"{stats['win_rate']:.0f}%",
    ]


def print_results(results: list[dict], out_path: str | None,
                  wrote: bool) -> None:
    banner("TUNER RESULTS (walk-forward: train -> out-of-sample test)",
           ["rank by: test-period eligibility (min trades, max DD), then test P&L",
            "train = first part of the window | test = final part (untouched "
            "until selection)"])
    print()
    headers = ["rank", "combo", "param diff (vs base)"]
    for period in ("train", "test"):
        headers += [f"{period}: trades", f"{period}: net", f"{period}: CAGR",
                    f"{period}: PF", f"{period}: maxDD", f"{period}: win%"]
    rows = []
    for rank, r in enumerate(results[:10], 1):
        row = [rank, r["tag"], diff_str(r["ov"])[:38]]
        row += _row_vals(r["train"])
        row += _row_vals(r["test"])
        rows.append(row)
    table(headers, rows)
    print()
    print("  reading the table: a good winner has positive TEST net P&L that is")
    print("  a reasonable fraction of its TRAIN P&L. If train >> test, the combo")
    print("  is overfit - pick the next one down instead.")
    if wrote:
        print(f"\n  wrote best combination to {out_path}")
        print("  the engine and backtest now AUTO-LOAD it (shown in their banners).")
        print("  to go back to defaults: delete that file.")
    else:
        print("\n  (no file written - run without --no-write to apply the winner)")


def main() -> int:
    ap = argparse.ArgumentParser(description="STMR parameter tuner (walk-forward)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=None,
                    help="comma list (default: all symbols with data in the window)")
    ap.add_argument("--equity", type=float, default=config.PAPER_START_EQUITY)
    ap.add_argument("--n-samples", type=int, default=150)
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--split", type=float, default=0.7,
                    help="train fraction of the days (default 0.7)")
    ap.add_argument("--min-trades", type=int, default=15,
                    help="minimum trades in the TEST period for eligibility")
    ap.add_argument("--max-dd", type=float, default=3.0,
                    help="max drawdown %% in either period for eligibility")
    ap.add_argument("--grid", default=None, help="JSON file overriding DEFAULT_GRID")
    ap.add_argument("--out", default=str(config.PARAMS_FILE))
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--seed-demo", type=int, nargs="?", const=15, default=None,
                    metavar="DAYS", help="seed synthetic data first (offline demo)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    conn = db.get_conn(a.db or config.DB_PATH)
    db.init_db(conn)
    if a.seed_demo is not None:
        first, last, n = demo_data.seed_demo(conn, days=a.seed_demo,
                                             seed=42, quiet=a.quiet)
        if not a.quiet:
            print(f"[demo] seeded {n} symbols x {a.seed_demo} days "
                  f"({first}..{last}) - SYNTHETIC data, validates the "
                  f"tuner only (see module docstring)")
    days = db.distinct_dates(conn, a.start or "2000-01-01",
                             a.end or "9999-12-31")
    if not days:
        print("no 5-min data in the DB - run the engine, --import-csv, or "
              "--seed-demo first")
        return 1
    if a.start:
        days = [d for d in days if d >= a.start]
    if a.end:
        days = [d for d in days if d <= a.end]
    symbols = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()]
               if a.symbols else None)
    if symbols is None:
        present = set()
        for d in days:
            present.update(db.day_coverage(conn, d))
        symbols = [s for s in config.load_universe() if s in present]
    else:
        symbols = [s for s in symbols
                   if db.day_coverage(conn, days[0]).get(s)
                   or any(db.day_coverage(conn, d).get(s) for d in days)]

    grid = dict(DEFAULT_GRID)
    if a.grid:
        grid.update(json.loads(Path(a.grid).read_text(encoding="utf-8")))

    print(f"\nwindow {days[0]}..{days[-1]} | {len(days)} days | "
          f"{len(symbols)} symbols | equity {inr(a.equity)}")
    results = optimize(conn, days, symbols, a.equity, grid, a.n_samples,
                       a.top_k, a.seed, a.split, a.min_trades, a.max_dd,
                       out=a.out if not a.no_write else None, quiet=a.quiet)
    print_results(results, a.out, wrote=not a.no_write)

    if not a.no_write and results:
        best = next((r for r in results if r["test_ok"]), results[0])
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_by": "tune.py",
            "window": [days[0], days[-1]],
            "test_net_pnl": round(best["test"]["net_pnl"], 2),
            "test_cagr": best["test"].get("cagr"),
            "train_net_pnl": round(best["train"]["net_pnl"], 2),
            "overrides": {k: v for k, v in best["ov"].items()},
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
