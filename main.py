#!/usr/bin/env python3
"""
================================================================================
JF-OU / NSE 2026  --  Jump-Filtered Ornstein-Uhlenbeck Trend-Conditioned
                      Mean Reversion, implemented end to end
================================================================================
Specification : JF_OU_NSE_2026.md (Parts 0-XXIV)
Universe      : NIFTY 100 (universe_nifty100.json) -- all of it, every run
Broker API    : Upstox, end to end
State         : SQLite (data/jfou.sqlite3) -- every bar, gate, signal, order,
                fill and state transition
Mode          : PAPER by default. Nothing reaches a broker unless you pass
                --live AND export JFOU_UPSTOX_TOKEN AND set paper_trade=False.

--------------------------------------------------------------------------------
WHERE THE UPSTOX API IS NOT SUFFICIENT FOR THIS ALGORITHM
--------------------------------------------------------------------------------
This is the honest list. Each item is a real gap, not a caveat, and each says what
the program does about it. Full analysis in Part XXIII of the specification.

 1. 5-MINUTE DEPTH -- the binding constraint.
    Upstox serves 5-minute candles from roughly Jan-2022 (~4.7 years). The
    regime-stratified CPCV validation in Part XVI wants the March-2020 crash as a
    natural stress test. Upstox cannot supply it at 5-minute resolution. Daily
    bars reach back to Jan-2000, so daily validation is fine; intraday validation
    of the 2020 regime is not.
    -> dataclient.historical() raises rather than returning an empty frame, and the
       message names the depth floor. A reference-data vendor is required for the
       2018-2021 intraday layer.

 2. BACKFILL WALL CLOCK.
    5-minute history is one month per call and the account limit is 2000 requests
    per 30 minutes. The 100-name universe needs ~4,700 calls; the 30-minute cap,
    not the per-second rate, dominates: ~7.2 hours cold.
    -> the rate limiter is built around the 30-minute window and persists its
       timestamps in kv, so a restart does not buy a fresh budget and get the
       account throttled.

 3. NO POINT-IN-TIME INDEX CONSTITUENCY.
    No endpoint answers "which names were in the NIFTY 100 on 2021-03-15". Any
    backtest on today's list is survivorship-biased -- DHFL and Jet Airways are
    absent from it and were not absent from 2018.
    -> universe_nifty100.json is labelled a non-PIT snapshot with an as_of stamp,
       and old rows are kept with is_current = 0 rather than overwritten.

 4. NO CORPORATE-ACTIONS ENDPOINT.
    Gate G1.4 blocks a name around ex-dates, splits and results. Upstox has a
    Fundamentals API but no reliable corporate-actions calendar with ex-dates.
    -> corporate_events is fed externally. When it is empty, G1.4 reports
       "unknown" and passes conservatively; it does not pretend to know.

 5. NO SECTOR CLASSIFICATION.
    The Part XIV concentration caps and the Part 4.3 macro overlay both need a
    sector per name.
    -> sectors come from the universe file as approximate tags. Where a sector is
       unknown the sector cap fails closed rather than assuming every name is a
       different sector.

 6. INDIA VIX AVAILABILITY -- UNVERIFIED.
    Whether NSE_INDEX|India VIX is subscribable on a given account could not be
    confirmed from the build environment.
    -> Ingestor.ensure_vix() tolerates the failure, marks the macro tier DEGRADED,
       and DEGRADED is treated as Amber, never as Green.

 7. NO VERIFIED PRE-OPEN SNAPSHOT AT 09:08.
    The GAP-CANCEL rule needs the 09:08-09:15 pre-open equilibrium price.
    -> the clock resolves the phase regardless, and gap-cancel uses the first
       available open. A missing pre-open print is logged, not guessed.

 8. INSTRUMENT MASTER REACHABILITY.
    https://assets.upstox.com/market-quote/instruments.json returned HTTP 403 from
    the build environment, with and without a browser User-Agent.
    -> NO ISIN is hard-coded anywhere. symbol -> instrument_key is resolved at
       runtime and cached in the instruments table, so a failed refresh falls back
       to the cache instead of breaking a live run.

 9. ORDER-TYPE AND COMPLIANCE CONSTRAINTS.
    SEBI's algo framework from 1 Apr 2026 requires an Algo-ID, a static IP and
    Indian hosting. Bracket/cover order availability after the SEBI restriction
    could not be verified.
    -> the stop-loss is built as an independent order rather than relying on a
       bracket leg that may not exist.

WHAT IS NOT AFFECTED: Hurst, Kalman, de-seasonalised BNS, GARCH, ADF, OU half-life,
Student-t CDF and CPCV/DSR are all local numerics and need nothing from the broker.
Daily OHLCV from Jan-2000 is sufficient for every daily-resolution gate.
--------------------------------------------------------------------------------

USAGE
    python main.py status              show DB state, captures, open book
    python main.py load-universe       resolve + validate the NIFTY 100 symbols
    python main.py scan [--date D]     run the full gate cascade and arm orders
    python main.py manage [--date D]   walk the exit ladder on open positions
    python main.py report [run_id]     human-readable justification for a run
    python main.py backtest --from D --to D
                                     replay the stored database, no network
    python main.py run                 scan + manage on the session clock
    python main.py test                run the calibration suite

Running the same command twice does not re-fetch anything already stored; see
engine.Ingestor for the capture-gating rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PATCH_LEVEL = 2          # repair scripts applied: patch_v1.py, patch_v2.py

BASE = Path(os.path.normpath(str(Path(__file__).resolve().parent)))
if str(BASE) not in sys.path:                 # no duplicate entries on re-import
    sys.path.insert(0, str(BASE))


def _force_utf8_stdio() -> None:
    """The reports print box-drawing characters; make sure the stream can take them.

    On Windows the default stdio encoding is the active ANSI codepage (cp1252, cp437,
    ...), where "─" raises UnicodeEncodeError from inside print(). Redirecting to a
    file has the same default. Reconfigure to UTF-8 with replacement, so the worst
    case is a question mark, never a traceback from a cosmetic character.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:                  # pythonw.exe has no console at all
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except (AttributeError, ValueError, OSError):
            pass
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name, io.TextIOWrapper(
                    buf, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


_force_utf8_stdio()

try:
    from jfou import console as con                                   # noqa: E402
    from jfou.clock import now_ist, resolve_phase, session_date_for   # noqa: E402
    from jfou.config import (CFG, DB_PATH, LAKH, CRORE,               # noqa: E402
                             ensure_dirs, resolve_universe_file)
    from jfou.dataclient import MockClient, UpstoxClient, UpstoxError  # noqa: E402
    from jfou.db import Database                                      # noqa: E402
    from jfou.engine import Engine                                    # noqa: E402
except ModuleNotFoundError as exc:                                    # noqa: E402
    if (exc.name or "").split(".")[0] in {"numpy", "pandas", "scipy", "statsmodels",
                                          "arch", "requests"}:
        # The usual first run on a fresh Windows box: the interpreter is the system
        # one and no venv was activated. Say so instead of showing a traceback from
        # deep inside pandas.
        for _line in (
                f"JF-OU needs the scientific stack (missing: {exc.name}).",
                "  python -m venv .venv",
                r"  Windows:  .venv\Scripts\python -m pip install -r requirements.txt",
                r"  Linux:    .venv/bin/python    -m pip install -r requirements.txt"):
            print(_line, file=sys.stderr)
        raise SystemExit(2)
    raise ModuleNotFoundError(
        f"{exc}. The `jfou` package must sit next to main.py -- looked for at "
        f"{BASE / 'jfou'}. Run `python patch_v1.py` and then `python patch_v2.py` "
        f"in the repository root to repair the layout."
    ) from None


def _client(db: Database, args) -> object:
    """Paper mode is the default and is enforced here, again, independently."""
    if getattr(args, "live", False):
        if not os.environ.get("JFOU_UPSTOX_TOKEN"):
            con.warn("--live was passed but JFOU_UPSTOX_TOKEN is not set. "
                     "Falling back to the offline mock market.")
        elif CFG.paper_trade:
            con.warn("--live was passed but CFG.paper_trade is still True. "
                     "Falling back to the offline mock market. Set paper_trade=False "
                     "in jfou/config.py only when you mean it.")
        else:
            return UpstoxClient(db)
    syms = _universe_symbols(db)
    return MockClient(db, symbols=syms)


def _universe_symbols(db: Database) -> list[str]:
    rows = db.query("SELECT symbol FROM universe WHERE is_current=1 ORDER BY symbol")
    if rows:
        return [r["symbol"] for r in rows]
    return _file_symbols()


def _universe_path() -> Path:
    """Absolute path of the universe file, resolved independently of the cwd."""
    return resolve_universe_file(CFG.universe_file, roots=(BASE,))


def _read_json(path: Path) -> dict:
    # Explicit encoding: the platform default on Windows is cp1252/cp437, where a
    # UTF-8 BOM or a rupee sign in the JSON is a UnicodeDecodeError. utf-8-sig
    # swallows the BOM if an editor left one behind.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_symbols() -> list[str]:
    p = _universe_path()
    if not p.is_file():
        return []
    try:
        data = _read_json(p)
    except (OSError, ValueError) as exc:
        con.warn(f"could not read the universe file {p}: {exc}")
        return []
    return [m["symbol"] for m in data.get("members", [])]


def _mode(args) -> str:
    return "LIVE" if (getattr(args, "live", False) and not CFG.paper_trade
                      and os.environ.get("JFOU_UPSTOX_TOKEN")) else "PAPER"


def _iso_date(value, label: str) -> str | None:
    """Validate a command-line date and normalise it to YYYY-MM-DD.

    Every consumer compares these as strings against a SQLite column, so a malformed
    value is not merely cosmetic: one that never matches reads as "no data today",
    and one that pandas cannot parse surfaces as a ValueError from inside a command,
    forty frames away from the typo that caused it. cmd.exe and PowerShell also
    rewrite an unquoted separator, so the common variants -- 2026/09/03, 2026-9-3,
    20260903, "quoted" -- are accepted and normalised here instead of guessed at.
    (chr(47) is the slash and chr(92) the backslash; spelled as chr() so this file
    never has to argue about escape depth between the patch and the patched.)
    """
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().strip('"').replace(chr(47), "-").replace(chr(92), "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise SystemExit(f"{label}: {value!r} is not a date. Use YYYY-MM-DD, "
                     f"for example: --date 2026-09-03")


# --------------------------------------------------------------------- commands
def cmd_status(db: Database, args) -> int:
    con.banner(["JF-OU / NSE 2026  --  STATUS",
                f"{now_ist():%Y-%m-%d %H:%M:%S %Z} | phase {resolve_phase(now_ist())} | "
                f"db {DB_PATH}"])
    con.head("Database")
    counts = {}
    for t in ("instruments", "universe", "daily_ohlcv", "intraday_5m", "index_daily",
              "vix_daily", "seasonal_factors", "data_capture_log", "scan_runs",
              "gate_results", "candidates", "positions", "orders", "position_events",
              "fills", "portfolio_snapshots"):
        counts[t] = db.scalar(f"SELECT COUNT(*) FROM {t}") or 0
    con.make_table([["table", "rows"], *[[k, f"{v:,}"] for k, v in counts.items()]])

    con.head("Capture log (last 10)")
    rows = db.query("SELECT dataset, instrument_key, range_from, range_to, rows_written, "
                    "status, started_at FROM data_capture_log ORDER BY id DESC LIMIT 10")
    if rows:
        con.make_table([["dataset", "instrument", "from", "to", "rows", "status"],
                        *[[r["dataset"], (r["instrument_key"] or "-")[-18:],
                           r["range_from"] or "-", r["range_to"] or "-",
                           str(r["rows_written"]), r["status"]] for r in rows]])
    else:
        con.info("captures", "none yet")

    con.head("Open book")
    pos = db.query("SELECT symbol, state, lots, entry_price, stop_price, realized_pnl "
                   "FROM positions WHERE state IN ('PENDING_ENTRY','OPEN','T1_HIT') "
                   "ORDER BY symbol")
    if pos:
        con.make_table([["symbol", "state", "lots", "entry", "stop", "P&L"],
                        *[[p["symbol"] or "?", p["state"], str(p["lots"] or 0),
                           f"{p['entry_price'] or 0:,.2f}", f"{p['stop_price'] or 0:,.2f}",
                           f"{p['realized_pnl'] or 0:,.0f}"] for p in pos]])
    else:
        con.info("book", "no open positions")

    realized = db.scalar("SELECT COALESCE(SUM(realized_pnl),0) FROM positions") or 0
    realized_r = db.scalar("SELECT COALESCE(SUM(realized_r),0) FROM positions") or 0
    closed = db.scalar("SELECT COUNT(*) FROM positions WHERE state='CLOSED'") or 0
    con.head("Lifetime")
    con.make_table([["metric", "value"],
                    ["closed trades", str(closed)],
                    ["realized P&L", con.money(float(realized))],
                    ["realized R", f"{float(realized_r):+.2f}R"],
                    ["mode", "PAPER (default)" if CFG.paper_trade else "LIVE"]])
    return 0


def cmd_load_universe(db: Database, args) -> int:
    """Resolve every symbol against the instrument master and report what fails."""
    p = _universe_path()
    if not p.is_file():
        con.warn(f"universe file not found: {p}. Place universe_nifty100.json in the "
                 f"repository root, or point CFG.universe_file at an absolute path.")
        return 2
    data = _read_json(p)
    members = data.get("members", [])
    con.banner(["LOAD UNIVERSE",
                f"{len(members)} members from {p.name} | as_of {data.get('as_of')} | "
                f"{data.get('source')}"])
    con.warn("This list is NOT point-in-time. Backtests run on it are "
             "survivorship-biased. See the header of the file.")

    client = _client(db, args)
    syms = [m["symbol"] for m in members]
    con.head(f"Resolving {len(syms)} symbols via {client.name}")
    try:
        resolved = client.resolve_instruments(syms)
    except UpstoxError as exc:
        con.warn(f"resolution failed: {exc}")
        resolved = {}

    now = now_ist().strftime("%Y-%m-%d")
    ok, bad = [], []
    with db.tx() as c:
        for m in members:
            sym = m["symbol"]
            rec = resolved.get(sym.upper())
            if rec:
                c.execute("""INSERT INTO universe(symbol, source, as_of, is_current, sector,
                             added_at) VALUES(?,?,?,?,?,?)
                             ON CONFLICT(symbol) DO UPDATE SET sector=excluded.sector,
                               as_of=excluded.as_of, is_current=1, source=excluded.source""",
                          (sym, m.get("src", ""), data.get("as_of", now), 1,
                           m.get("sector"), now))
                ok.append((sym, rec["instrument_key"], m.get("src", "")))
            else:
                c.execute("""INSERT INTO universe(symbol, source, as_of, is_current, sector,
                             added_at) VALUES(?,?,?,?,?,?)
                             ON CONFLICT(symbol) DO UPDATE SET is_current=1,
                               sector=excluded.sector""",
                          (sym, m.get("src", ""), data.get("as_of", now), 1,
                           m.get("sector"), now))
                bad.append((sym, m.get("src", ""), "UNRESOLVED" if not m.get("verify")
                            else "UNRESOLVED (flagged for verification)"))
    if resolved and hasattr(client, "db"):
        db.upsert_instruments(list(resolved.values()))

    con.head(f"Resolved {len(ok)} / {len(syms)}")
    if ok[:10]:
        con.make_table([["symbol", "instrument_key", "src"],
                        *[[s, k, src] for s, k, src in ok[:10]]])
        if len(ok) > 10:
            con.info("...", f"and {len(ok)-10} more")
    if bad:
        con.head(f"UNRESOLVED {len(bad)} -- these will be skipped by every scan")
        con.make_table([["symbol", "src", "status"], *[[s, src, st] for s, src, st in bad]])
        con.warn("Unresolved symbols are dropped, never invented. Under the offline "
                 "mock every symbol resolves; under live Upstox this table is the "
                 "authoritative check on the universe file.")
    return 0


def cmd_scan(db: Database, args) -> int:
    as_of = _iso_date(args.date, "--date") or str(session_date_for(now_ist()))
    client = _client(db, args)
    if not db.scalar("SELECT COUNT(*) FROM universe WHERE is_current=1"):
        con.warn("the universe table is empty. Run `python main.py load-universe` first.")
        return 2
    eng = Engine(db, client, mode=_mode(args))
    eng.scan(as_of, limit=args.limit)
    con.head("Data capture")
    con.make_table([["metric", "value"],
                    ["api calls made", str(eng.ing.stats["api_calls"])],
                    ["rows written", f"{eng.ing.stats['rows']:,}"],
                    ["fetches skipped (already stored)", str(eng.ing.stats["skipped"])]])
    con.info("note", "re-running this command fetches nothing already in the database")
    return 0


def cmd_manage(db: Database, args) -> int:
    as_of = _iso_date(args.date, "--date") or str(session_date_for(now_ist()))
    eng = Engine(db, _client(db, args), mode=_mode(args))
    eng.manage_open_positions(as_of)
    return 0


def cmd_report(db: Database, args) -> int:
    Engine(db, _client(db, args), mode=_mode(args)).report(args.run_id)
    return 0


def cmd_run(db: Database, args) -> int:
    """Scan and manage on the session clock. Safe to start at any hour."""
    con.banner(["JF-OU / NSE 2026  --  SESSION RUNNER",
                f"mode {_mode(args)} | poll {CFG.poll_seconds}s | "
                f"paper_trade={CFG.paper_trade}"])
    while True:
        t = now_ist()
        phase = resolve_phase(t)
        # `run --date D` was accepted by the parser and then ignored, which turns a
        # deliberate replay of one session into a scan of whatever today is.
        as_of = _iso_date(getattr(args, "date", None), "--date") or str(session_date_for(t))
        con.head(f"{t:%Y-%m-%d %H:%M:%S %Z}  phase={phase}  session={as_of}")
        try:
            if phase in ("POST_CLOSE", "PRE_MARKET", "IDLE"):
                cmd_scan(db, argparse.Namespace(date=as_of, live=args.live, limit=args.limit))
            elif phase in ("PRE_OPEN", "PLACE"):
                cmd_manage(db, argparse.Namespace(date=as_of, live=args.live))
            elif phase in ("MANAGE", "CANCEL"):
                cmd_manage(db, argparse.Namespace(date=as_of, live=args.live))
            else:
                con.info("waiting", f"phase {phase} has no work")
        except KeyboardInterrupt:
            raise
        except Exception as exc:                     # never die on a data outage
            con.warn(f"phase {phase} failed: {exc}")
        if not CFG.loop:
            con.info("single pass complete", "use loop=True in config for continuous mode")
            return 0
        time.sleep(CFG.poll_seconds)


def cmd_backtest(db: Database, args) -> int:
    from jfou.backtest import Backtest
    start = _iso_date(args.start, "--from")
    end = _iso_date(args.end, "--to")
    if not start or not end:
        con.warn("backtest needs both --from and --to, as YYYY-MM-DD")
        return 2
    if start > end:
        con.warn(f"--from {start} is after --to {end}; there is nothing to replay")
        return 2
    bt = Backtest(db)
    return bt.run(start, end, warmup=args.warmup)


def _suite(name: str) -> Path | None:
    """tests/ is the documented home; the project root is accepted too."""
    for cand in (BASE / "tests" / name, BASE / name, BASE / "jfou" / "tests" / name):
        if cand.is_file():
            return cand
    return None


def cmd_test(db: Database, args) -> int:
    import subprocess
    path = _suite("test_indicators.py")
    if path is None:
        con.warn(f"test_indicators.py not found under {BASE / 'tests'} or {BASE}")
        return 1
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(BASE)}
    return subprocess.call([sys.executable, str(path)], cwd=str(BASE), env=env)


COMMANDS = ("status", "load-universe", "scan", "manage", "report", "run", "backtest",
            "test")


def _version_string() -> str:
    """Engine version plus the patch level, so a bug report is self-describing."""
    try:
        from jfou import __version__ as ver
    except Exception:
        ver = "unknown"
    return f"jfou {ver}  (patch level {PATCH_LEVEL})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="jfou",
        description="JF-OU / NSE 2026 mean-reversion engine",
        epilog="commands: " + ", ".join(COMMANDS) + "\n"
               "examples:\n"
               "  python main.py status\n"
               "  python main.py load-universe\n"
               "  python main.py scan --date 2026-09-03\n"
               "  python main.py backtest --from 2026-08-01 --to 2026-09-11\n"
               "  python main.py test\n"
               "with no command, status is printed. every command takes -h.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=_version_string())
    ap.add_argument("--live", action="store_true", default=False,
                    help="attempt live Upstox (also needs JFOU_UPSTOX_TOKEN and "
                         "paper_trade=False). Default is paper.")
    # --live is accepted after the subcommand too, because that is how it gets typed;
    # it used to be answered with "error: unrecognized arguments: --live". SUPPRESS is
    # what lets the two positions coexist: without it the subparser would write its
    # own default back over a `main.py --live scan` and silently downgrade to paper.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--live", dest="live", action="store_true",
                        default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=False)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    add("status", help="show DB state, captures and the open book")

    add("load-universe", help="resolve and validate the NIFTY 100 symbols")

    p = add("scan", help="run the gate cascade and arm orders")
    p.add_argument("--date"); p.add_argument("--limit", type=int)

    p = add("manage", help="walk the exit ladder on open positions")
    p.add_argument("--date")

    p = add("report", help="human-readable justification for a run")
    p.add_argument("run_id", nargs="?")

    p = add("run", help="scan and manage on the session clock")
    p.add_argument("--date"); p.add_argument("--limit", type=int)

    p = add("backtest", help="replay the stored database, no network")
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--warmup", type=int, default=300)

    add("test", help="run the indicator calibration suite")

    args = ap.parse_args(argv)

    # No command is not an error. `python main.py` from a bare console used to exit 2
    # on argparse's "the following arguments are required: cmd", which reads exactly
    # like a crash when you are following a README. Print what exists, then run the
    # one command that is always safe and always informative: the read-only status.
    if not getattr(args, "cmd", None):
        con.info("no command given",
                 "running `status`.  commands: " + ", ".join(COMMANDS) +
                 "  (each takes -h)")
        args.cmd = "status"

    ensure_dirs()                      # data/ and logs/, from the resolved config
    db = Database(DB_PATH)

    con.banner(["JF-OU / NSE 2026",
                f"mode {'LIVE' if (args.live and not CFG.paper_trade and os.environ.get('JFOU_UPSTOX_TOKEN')) else 'PAPER (default)'}"
                f" | db {DB_PATH}",
                "spec JF_OU_NSE_2026.md Parts 0-XXIV"])
    if CFG.paper_trade:
        con.info("paper trading", "no order will reach a broker in this mode")

    handlers = {"status": cmd_status, "load-universe": cmd_load_universe,
                "scan": cmd_scan, "manage": cmd_manage, "report": cmd_report,
                "run": cmd_run, "backtest": cmd_backtest, "test": cmd_test}
    for attr in ("date", "limit", "run_id", "start", "end", "warmup"):
        if not hasattr(args, attr):
            setattr(args, attr, None if attr != "warmup" else 300)
    return handlers[args.cmd](db, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted - all state is in SQLite, restart is safe")
        raise SystemExit(130)
