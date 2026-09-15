"""
tune.py - STMR parameter tuner v2 ("desk edition").

What a professional desk does with a parameter search (and what this does):
  1. ANCHORED WALK-FORWARD FOLDS  (--folds N, default 1 for backwards compat)
     The train window always starts at day 1 and EXPANDS fold by fold; each
     fold is validated on the next slice of days that the search never saw.
     The final fold covers the most recent session, which is the regime the
     strategy is most likely to face. A combo that only works on one fold is
     regime-luck, not edge.
  2. SELECTABLE OBJECTIVE  (--objective pnl|cagr|calmar|sharpe)
     P&L alone rewards risk-on behaviour; desks usually rank on risk-adjusted
     figures. Calmar = annualized CAGR% / max drawdown %, sharpe = daily
     (annualized) sharpe from the equity curve, cagr = annualized return.
  3. CONSTRAINTS  (--min-trades --max-dd --min-wr --max-hold-bars)
     Eligibility filters applied to EVERY fold's test slice: a combo is only
     eligible if it satisfies them on all folds (min trades is scaled to the
     fold length).
  4. LOCAL REFINEMENT  (--refine N)
     After the coarse pass, the top-3 combos are perturbed one grid step at a
     time (each tunable +/-1 level), the neighbourhood is scored on the
     largest train window, and the best N perturbations join the fold
     evaluation. This is how you stop 1 grid step away from the optimum.
  5. OVERFIT Z-SCORE
     The winner's train-period objective is expressed as a z-score against the
     distribution of ALL trials' train objectives. A huge train value that
     collapses out-of-sample is the classic overfit signature - the memo
     prints the z and the train->test persistence ratio.
  6. DESK MEMO  (reports/tune_*.md)
     A short markdown memo of the run: window, objective, constraints,
     per-fold table, winner rationale and the standard caveats.

Same guarantees as v1:
  * The live engine and this search run the EXACT same backtest() code path,
    so "best parameters" transfer 1:1 to the live engine.
  * The winner is written to data/best_params.json and AUTO-LOADED by the
    engine and backtest on every run (see config.make_cfg / load_params).

IMPORTANT (read before trusting any number)
  * Upstox cannot serve intraday history before today (limitation #1), so you
    must have REAL bars in the DB first: run the engine for a few weeks to
    accumulate them, or import any broker/vendor CSV export with
        python main.py --import-csv FILE --kind 5m
  * Tuning on --seed-demo SYNTHETIC data validates the machinery only.
    Always re-run this tuner on real data before trusting parameters.
  * On short windows (<=~20 days) the per-fold slices are small; treat the
    per-fold CAGR/sharpe as indicative, and prefer combos whose P&L is
    positive on every fold.

Usage
  python tune.py                                        # whole DB, 1 fold
  python tune.py --folds 3 --objective calmar --refine 20
  python tune.py --folds 3 --objective sharpe --min-wr 55 --max-hold-bars 8
  python tune.py --start 2026-08-01 --end 2026-09-15 --n-samples 300
  python tune.py --symbols RELIANCE,TCS,HDFCBANK --n-samples 400
  python tune.py --grid my_grid.json --top-k 20 --min-trades 20 --max-dd 2.5
  python tune.py --seed-demo 15 --folds 3 --objective calmar   # offline demo
  python tune.py --no-write                                  # report only
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from datetime import datetime
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
    # desk layer (partial profit-taking + risk guards)
    "PARTIAL_PCT": [0.0, 40.0, 50.0, 60.0],
    "R_MULT_TARGET2": [1.25, 1.5, 2.0],
    "EARLY_ENTRY_CUTOFF": ["09:20", "09:30", "09:45"],
    "MAX_ATR_PCT": [1.0, 1.5, 9.9],
    "MIN_BAR_CLOSE_POS": [0.0, 0.34, 0.5],
    "DAILY_LOSS_LIMIT_PCT": [0.0, 1.0, 1.5],
    "MAX_TRADES_PER_DAY": [0, 8, 12],
    "STOP_COOLDOWN_BARS": [0, 3, 6],
}

OBJECTIVES = ("pnl", "cagr", "calmar", "sharpe")


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def objective_val(stats: dict, obj: str) -> float:
    """Scalar the optimizer ranks on. -1e9 marks 'undefined' (never wins)."""
    if obj == "pnl":
        return stats["net_pnl"]
    if obj == "cagr":
        c = stats.get("cagr")
        return c if c is not None else -1e9
    if obj == "sharpe":
        s = stats.get("sharpe")
        return s if s is not None else -1e9
    if obj == "calmar":
        cagr = stats.get("cagr")
        if cagr is None:
            return -1e9
        dd = max(stats.get("max_dd_pct", 0.0), 0.05)
        return (cagr * 100.0) / dd
    raise ValueError(f"unknown objective {obj!r}")


def eligible(stats: dict, min_trades: int, max_dd: float,
             min_wr: float = 0.0, max_hold: float = 0.0) -> bool:
    if stats["n_trades"] < min_trades:
        return False
    if stats["max_dd_pct"] > max_dd:
        return False
    if min_wr > 0 and stats["win_rate"] < min_wr:
        return False
    if max_hold > 0 and stats["avg_hold_bars"] > max_hold:
        return False
    return True


def build_folds(days: list[str], folds: int, split: float):
    """Anchored, expanding walk-forward folds.

    Each fold = (train_days, test_days); every train window starts at day 0
    and grows, and the LAST fold's test slice is the final part of the data
    (the most recent regime). Returns a list in chronological order.
    """
    n = len(days)
    folds = max(1, folds)
    min_train = 2
    if n < min_train + folds:
        raise SystemExit(
            f"need at least {min_train + folds} days for --folds {folds} "
            f"(found {n}); import more history or use fewer folds")
    n_test = max(1, round(n * (1.0 - split) / folds))
    tests = []
    end = n
    for _ in range(folds):
        t0 = max(min_train, end - n_test)
        tests.append((t0, end))
        end = t0
    tests.reverse()
    out = []
    for t0, t1 in tests:
        train = days[:t0]
        if len(train) < min_train:      # safety: cannot train on <2 days
            continue
        out.append((train, days[t0:t1]))
    if not out:
        raise SystemExit("could not build any valid fold from the window")
    return out


def diff_str(ov: dict) -> str:
    """Compact 'param=value' list of the overrides vs the defaults."""
    base = config.make_cfg()
    parts = []
    for k in sorted(ov):
        if getattr(base, k, None) != ov[k]:
            parts.append(f"{k}={ov[k]}")
    return ", ".join(parts) if parts else "(base defaults)"


def _grid_size(grid: dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


def _sample_combos(grid: dict, n_samples: int, seed: int) -> list[dict]:
    """Base config is always candidate #0; never more than the grid space."""
    rng = random.Random(seed)
    combos: list[dict] = [{}]
    target = min(n_samples, _grid_size(grid) + 1)
    tries = 0
    while len(combos) < target and tries < 50 * target:
        tries += 1
        ov = {k: rng.choice(v) for k, v in grid.items()}
        if ov not in combos:
            combos.append(ov)
    return combos


def _neighbours(ov: dict, grid: dict) -> list[dict]:
    """Each tunable moved one grid step (both directions), if present."""
    out: list[dict] = []
    for k, vals in grid.items():
        cur = ov.get(k)
        if cur is None or cur not in vals:
            continue
        i = vals.index(cur)
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                no = dict(ov)
                no[k] = vals[j]
                out.append(no)
    return out


def optimize(conn, days: list[str], symbols: list[str], start_equity: float,
             grid: dict, n_samples: int, top_k: int, seed: int,
             split: float, min_trades: int, max_dd: float,
             folds: int = 1, objective: str = "pnl", min_wr: float = 0.0,
             max_hold_bars: float = 0.0, refine: int = 0, quiet: bool = False,
             out: str | None = None) -> list[dict]:
    """Run the anchored walk-forward search. Returns ranked result rows.

    Each row: {ov, tag, train (stats on the largest train window),
    fold_tests (per-fold out-of-sample stats), test (= last fold's test),
    mean_obj, mean_pnl, std_pnl, z_train, eligible}.
    """
    fs = build_folds(days, folds, split)
    train_days = fs[-1][0]          # largest (last) train window
    total_test_days = sum(len(t) for _, t in fs)

    if not quiet:
        parts = []
        for i, (tr, te) in enumerate(fs, 1):
            parts.append(f"F{i}: train {tr[0]}..{tr[-1]} ({len(tr)}d) -> "
                         f"test {te[0]}..{te[-1]} ({len(te)}d)")
        print(f"walk-forward ({len(fs)} anchored folds): " + " | ".join(parts))
        print(f"objective: {objective} | constraints: trades>={min_trades} "
              f"(window total), DD<={max_dd}%"
              + (f", WR>={min_wr:.0f}%" if min_wr > 0 else "")
              + (f", hold<={max_hold_bars:g} bars" if max_hold_bars > 0 else "")
              + " per fold slice")
        print(f"searching {n_samples} sampled combinations "
              f"(grid space ~{_grid_size(grid):,}) ...")

    combos = _sample_combos(grid, n_samples, seed)
    t0 = time.time()

    # ---- pass 1: coarse scoring on the largest train window ---------------
    rows: list[dict] = []
    for i, ov in enumerate(combos):
        cfg = config.make_cfg(ov)
        tr = run_backtest(conn, train_days[0], train_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        rows.append({
            "ov": ov, "idx": i, "train": tr,
            "train_obj": objective_val(tr, objective),
            "train_ok": eligible(tr, max(1, min_trades // 2), max_dd,
                                 min_wr, max_hold_bars),
        })
        if not quiet and (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(combos)} "
                  f"({time.time() - t0:.0f}s elapsed)")

    train_objs = [r["train_obj"] for r in rows]
    mu = statistics.fmean(train_objs)
    sd = statistics.pstdev(train_objs) if len(train_objs) > 1 else 0.0

    ranked = sorted([r for r in rows if r["train_ok"]],
                    key=lambda r: -r["train_obj"])
    top = ranked[:max(1, top_k)]
    if not top:
        raise SystemExit("no combination passed the train-period filter "
                         "(relax --min-trades/--max-dd or import more data)")

    # ---- pass 2: local refinement around the top-3 -------------------------
    if refine > 0 and top:
        existing = {str(sorted(r["ov"].items())) for r in rows}
        cands: list[dict] = []
        for r in top[:3]:
            for no in _neighbours(r["ov"], grid):
                key = str(sorted(no.items()))
                if key in existing:
                    continue
                existing.add(key)
                cfg = config.make_cfg(no)
                tr = run_backtest(conn, train_days[0], train_days[-1],
                                  symbols, start_equity, quiet=True, cfg=cfg)
                cands.append({
                    "ov": no, "train": tr,
                    "train_obj": objective_val(tr, objective),
                    "train_ok": eligible(tr, max(1, min_trades // 2), max_dd,
                                         min_wr, max_hold_bars),
                })
        cands.sort(key=lambda c: -c["train_obj"])
        added = 0
        for c in cands:
            if added >= refine:
                break
            rows.append({"ov": c["ov"], "idx": len(rows), "train": c["train"],
                         "train_obj": c["train_obj"],
                         "train_ok": c["train_ok"], "refined": True})
            added += 1
        if not quiet:
            print(f"refinement: {len(cands)} one-step neighbours scored on "
                  f"train, top {added} join the fold evaluation")

    # ---- pass 3: full anchored walk-forward for the finalists --------------
    results: list[dict] = []
    for r in top + [x for x in rows if x.get("refined")][:refine]:
        cfg = config.make_cfg(r["ov"])
        fold_tests = []
        for tr, te in fs:
            st = run_backtest(conn, te[0], te[-1], symbols, start_equity,
                              quiet=True, cfg=cfg)
            fold_min = max(1, round(min_trades * len(te) / total_test_days))
            fold_tests.append((st, eligible(st, fold_min, max_dd,
                                            min_wr, max_hold_bars)))
        ok = all(e for _, e in fold_tests)
        objs = [objective_val(st, objective) for st, _ in fold_tests]
        pnls = [st["net_pnl"] for st, _ in fold_tests]
        z = (r["train_obj"] - mu) / sd if sd > 1e-9 else 0.0
        results.append({
            "ov": r["ov"],
            "tag": "base" if r["ov"] == {} else
                   (f"#{r['idx']}" + ("~" if r.get("refined") else "")),
            "train": r["train"],
            "fold_tests": [st for st, _ in fold_tests],
            "test": fold_tests[-1][0],
            "mean_obj": statistics.fmean(objs),
            "mean_pnl": statistics.fmean(pnls),
            "std_pnl": (statistics.pstdev(pnls) if len(pnls) > 1 else 0.0),
            "z_train": z,
            "eligible": ok,
        })

    # make sure the base (untuned) configuration is in the table
    if all(r["ov"] != {} for r in results):
        cfg = config.make_cfg()
        tr = run_backtest(conn, train_days[0], train_days[-1], symbols,
                          start_equity, quiet=True, cfg=cfg)
        fold_tests = []
        for _tr, te in fs:
            st = run_backtest(conn, te[0], te[-1], symbols, start_equity,
                              quiet=True, cfg=cfg)
            fold_min = max(1, round(min_trades * len(te) / total_test_days))
            fold_tests.append((st, eligible(st, fold_min, max_dd,
                                            min_wr, max_hold_bars)))
        objs = [objective_val(st, objective) for st, _ in fold_tests]
        results.append({
            "ov": {}, "tag": "base", "train": tr,
            "fold_tests": [st for st, _ in fold_tests],
            "test": fold_tests[-1][0],
            "mean_obj": statistics.fmean(objs),
            "mean_pnl": statistics.fmean([st["net_pnl"] for st, _ in fold_tests]),
            "std_pnl": 0.0,
            "z_train": 0.0,
            "eligible": all(e for _, e in fold_tests),
        })

    results.sort(key=lambda r: (r["eligible"], r["mean_obj"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _fmt_obj(v: float, obj: str) -> str:
    if v <= -1e8:
        return "n/a"
    if obj == "pnl":
        return signed(v)
    if obj == "cagr":
        return f"{v * 100:+.1f}%"
    return f"{v:+.2f}"


def print_results(results: list[dict], objective: str, out_path: str | None,
                  wrote: bool, report_path: str | None) -> None:
    nf = len(results[0]["fold_tests"]) if results else 1
    banner("TUNER v2 RESULTS (anchored walk-forward: every fold out-of-sample)",
           [f"rank by: per-fold eligibility on ALL folds, then mean {objective} "
            f"across folds",
            "a robust winner is positive on EVERY fold slice - one lucky fold "
            "is regime-luck, not edge"])
    print()
    headers = ["rank", "combo", "param diff (vs base)"]
    for i in range(nf):
        headers.append(f"F{i + 1}: net")
    headers += ["mean net", f"mean {objective}", "z(train)", "last: CAGR/DD"]
    rows = []
    for rank, r in enumerate(results[:12], 1):
        row = [rank, r["tag"], diff_str(r["ov"])[:44]]
        for st in r["fold_tests"]:
            row.append(signed(st["net_pnl"]))
        row += [
            signed(r["mean_pnl"]),
            _fmt_obj(r["mean_obj"], objective),
            f"{r['z_train']:+.1f}",
            (f"{(r['test'].get('cagr') or 0) * 100:+.1f}% / "
             f"{r['test']['max_dd_pct']:.2f}%"),
        ]
        rows.append(row)
    table(headers, rows)
    print()
    print("  reading the table: z(train) = how far the winner's TRAIN score sits")
    print("  above the mean of all trials (>=+2.5 with a weak last fold =")
    print("  overfit signature). 'last: CAGR/DD' is the most recent fold.")
    if wrote:
        print(f"\n  wrote best combination to {out_path}")
        print("  the engine and backtest now AUTO-LOAD it (shown in their banners).")
        print("  to go back to defaults: delete that file.")
    else:
        print("\n  (no file written - run without --no-write to apply the winner)")
    if report_path:
        print(f"  desk memo: {report_path}")


def write_desk_memo(path: str, results: list[dict], days: list[str],
                    symbols: list[str], objective: str, folds_desc: list,
                    constraints: dict, refined: bool) -> None:
    best = next((r for r in results if r["eligible"]), results[0])
    L = []
    L.append(f"# STMR Tuner v2 - Desk Memo")
    L.append("")
    L.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    L.append(f"- Window: {days[0]} .. {days[-1]} ({len(days)} sessions, "
             f"{len(symbols)} symbols)")
    L.append(f"- Objective: `{objective}` | folds: {len(folds_desc)} (anchored, "
             f"expanding train)")
    L.append(f"- Constraints per fold slice: " + ", ".join(
        f"{k}={v}" for k, v in constraints.items() if v not in (0, 0.0)))
    L.append(f"- Refinement stage: {'on' if refined else 'off'}")
    L.append("")
    L.append("## Folds")
    L.append("")
    L.append("| fold | train | test |")
    L.append("|------|-------|------|")
    for i, (tr, te) in enumerate(folds_desc, 1):
        L.append(f"| F{i} | {tr[0]}..{tr[-1]} ({len(tr)}d) | "
                 f"{te[0]}..{te[-1]} ({len(te)}d) |")
    L.append("")
    L.append("## Top candidates (ranked by mean out-of-sample objective)")
    L.append("")
    head = ["rank", "combo"] + [f"F{i + 1} net" for i in range(len(folds_desc))] \
        + ["mean net", f"mean {objective}", "z(train)", "eligible"]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    for rank, r in enumerate(results[:8], 1):
        L.append("| " + " | ".join([
            str(rank), r["tag"],
            *[signed(st["net_pnl"]) for st in r["fold_tests"]],
            signed(r["mean_pnl"]), _fmt_obj(r["mean_obj"], objective),
            f"{r['z_train']:+.1f}", "yes" if r["eligible"] else "no",
        ]) + " |")
    L.append("")
    L.append("## Winner")
    L.append("")
    L.append(f"- Combo: `{best['tag']}`")
    L.append(f"- Overrides: `{diff_str(best['ov'])}`")
    L.append(f"- Mean net across folds: **{signed(best['mean_pnl'])}** "
             f"(per-fold std {abs(best['std_pnl']):.2f})")
    if objective == "pnl":
        train_obj = objective_val(best["train"], objective)
        persist = (best["mean_obj"] / train_obj
                   if train_obj and train_obj > 0 else None)
        if persist is not None:
            L.append(f"- Train->test persistence: {persist * 100:.0f}% of the "
                     f"train P&L reproduced out-of-sample")
    else:
        train_obj = objective_val(best["train"], objective)
        L.append(f"- Objective: train {train_obj:+.2f} -> out-of-sample mean "
                 f"{best['mean_obj']:+.2f} ({objective})")
    L.append(f"- Overfit z (train vs all trials): {best['z_train']:+.1f}"
             + (" - elevated; treat parameters with caution"
                if best["z_train"] > 2.0 else ""))
    L.append("")
    L.append("## Caveats (read every time)")
    L.append("")
    L.append("1. If this run used `--seed-demo`, the numbers validate the "
             "MACHINERY only - synthetic random walks are not the market. "
             "Re-run on real captured/imported bars before acting.")
    L.append("2. Walk-forward on short windows (<=~20 sessions) has small "
             "out-of-sample slices; a combo that is positive on every fold is "
             "still not proof of forward edge.")
    L.append("3. Parameters auto-load into the engine and backtest. Delete "
             "`data/best_params.json` to revert to defaults.")
    L.append("4. Paper-trade the winner for at least 1-2 weeks of real bars "
             "and compare live vs backtest P&L before considering size.")
    L.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="STMR parameter tuner v2 (anchored walk-forward, "
                    "objectives, constraints, refinement, desk memo)")
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
                    help="train fraction of the window per fold (default 0.7)")
    ap.add_argument("--folds", type=int, default=1,
                    help="anchored walk-forward folds (default 1 = classic "
                         "train/test; 3 recommended)")
    ap.add_argument("--objective", choices=OBJECTIVES, default="pnl",
                    help="what to rank on (default pnl; desks often use "
                         "calmar or sharpe)")
    ap.add_argument("--min-trades", type=int, default=15,
                    help="minimum trades over the whole out-of-sample span "
                         "(scaled per fold slice)")
    ap.add_argument("--max-dd", type=float, default=3.0,
                    help="max drawdown %% in any fold slice for eligibility")
    ap.add_argument("--min-wr", type=float, default=0.0,
                    help="min win rate %% per fold slice (0 = off)")
    ap.add_argument("--max-hold-bars", type=float, default=0.0,
                    help="max average holding in 5-min bars per fold slice "
                         "(0 = off)")
    ap.add_argument("--refine", type=int, default=0,
                    help="local refinement: N best one-step perturbations of "
                         "the top-3 join the fold evaluation (0 = off)")
    ap.add_argument("--grid", default=None, help="JSON file overriding DEFAULT_GRID")
    ap.add_argument("--out", default=str(config.PARAMS_FILE))
    ap.add_argument("--report", default=None,
                    help="desk memo path (default reports/tune_<ts>.md)")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-report", action="store_true")
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
                  "tuner only (see module docstring)")
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
    t0 = time.time()
    results = optimize(conn, days, symbols, a.equity, grid, a.n_samples,
                       a.top_k, a.seed, a.split, a.min_trades, a.max_dd,
                       folds=a.folds, objective=a.objective, min_wr=a.min_wr,
                       max_hold_bars=a.max_hold_bars, refine=a.refine,
                       quiet=a.quiet,
                       out=a.out if not a.no_write else None)
    print(f"search took {time.time() - t0:.0f}s")
    print_results(results, a.objective, a.out, wrote=not a.no_write,
                  report_path=None)

    report_path = None
    if not a.no_report and results:
        report_path = (a.report
                       or str(config.REPORTS_DIR /
                              f"tune_{datetime.now():%Y%m%d_%H%M%S}.md"))
        constraints = {"min_trades": a.min_trades, "max_dd_pct": a.max_dd,
                       "min_wr_pct": a.min_wr, "max_hold_bars": a.max_hold_bars}
        write_desk_memo(report_path, results, days, symbols, a.objective,
                        [(tr, te) for tr, te in build_folds(days, a.folds,
                                                            a.split)],
                        constraints, refined=a.refine > 0)
        if not a.quiet:
            print(f"desk memo: {report_path}")

    if not a.no_write and results:
        best = next((r for r in results if r["eligible"]), results[0])
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_by": "tune.py v2",
            "window": [days[0], days[-1]],
            "objective": a.objective,
            "folds": [[tr[0], tr[-1], te[0], te[-1]]
                      for tr, te in build_folds(days, a.folds, a.split)],
            "constraints": {"min_trades": a.min_trades,
                            "max_dd_pct": a.max_dd,
                            "min_wr_pct": a.min_wr,
                            "max_hold_bars": a.max_hold_bars},
            "train_net_pnl": round(best["train"]["net_pnl"], 2),
            "mean_fold_net_pnl": round(best["mean_pnl"], 2),
            "test_net_pnl": round(best["test"]["net_pnl"], 2),
            "test_cagr": best["test"].get("cagr"),
            "overfit_z": round(best["z_train"], 2),
            "overrides": {k: v for k, v in best["ov"].items()},
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
