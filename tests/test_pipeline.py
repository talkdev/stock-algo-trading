"""
End-to-end pipeline test against the offline mock market.

Exercises the parts the other suites cannot: universe resolution, the capture log and
its "do not dirty the database" guarantee, the full gate cascade over the whole
universe, and restart safety across separate processes.

Runs on a throwaway database under JFOU_HOME so it never touches data/jfou.sqlite3.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

def _project_root() -> Path:
    """First ancestor that holds main.py -- the root, whatever the layout depth is."""
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "main.py").is_file():
            return cand
    return here.parent.parent


BASE = _project_root()
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def run(home: str, *args: str, timeout: int = 1200) -> tuple[int, str]:
    # JFOU_HOME is resolved to an absolute, canonical path: on Windows
    # tempfile.mkdtemp() may hand back an 8.3 short path, and a short path plus a
    # relative DB path is how two runs end up writing two different databases.
    # text=True without an explicit encoding decodes the child with the ANSI
    # codepage on Windows -- the reports use U+2500 and would raise there.
    home_path = Path(home).expanduser().resolve()
    env = dict(os.environ)
    env.update({"JFOU_HOME": str(home_path),
                "JFOU_DB": str(home_path / "data" / "jfou.sqlite3"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(BASE)})
    p = subprocess.run([sys.executable, str(BASE / "main.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=str(BASE), timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    home = tempfile.mkdtemp(prefix="jfou_pipeline_")
    dbpath = Path(home) / "data" / "jfou.sqlite3"

    print("=" * 78)
    print("End-to-end pipeline test (offline mock, throwaway database)")
    print(f"  JFOU_HOME = {home}")
    print("=" * 78)

    # ---------------------------------------------------------------- load
    rc, out = run(home, "load-universe")
    check("load-universe exits 0", rc == 0, f"rc={rc}")
    check("universe resolved", "Resolved" in out and "/ 98" in out,
          out.strip().splitlines()[-1][:70] if out.strip() else "")

    from jfou.db import Database
    db = Database(dbpath)
    n_uni = db.scalar("SELECT COUNT(*) FROM universe WHERE is_current=1")
    n_inst = db.scalar("SELECT COUNT(*) FROM instruments")
    check("98 universe rows stored", n_uni == 98, str(n_uni))
    check("98 instruments resolved", n_inst == 98, str(n_inst))
    check("no fabricated ISINs",
          db.scalar("SELECT COUNT(*) FROM instruments WHERE isin IS NOT NULL AND isin<>''")
          == 0)

    # ---------------------------------------------------------------- scan
    rc, out = run(home, "scan", "--date", "2026-09-03")
    check("scan exits 0", rc == 0, f"rc={rc}")
    check("scan reports the funnel", "Funnel:" in out)
    check("scan reaches G1 for the whole universe", "G1 98" in out,
          [l for l in out.splitlines() if "Funnel" in l][0].strip()
          if "Funnel" in out else "")
    check("scan prints per-name reasoning", "BLOCKED BY" in out or "confluence" in out)
    check("scan is paper mode", "PAPER" in out)

    daily = db.scalar("SELECT COUNT(*) FROM daily_ohlcv")
    intraday = db.scalar("SELECT COUNT(*) FROM intraday_5m")
    caps = db.scalar("SELECT COUNT(*) FROM data_capture_log WHERE status='OK'")
    check("daily bars stored", daily > 10000, f"{daily:,}")
    check("5-min bars stored for names that reached G3", intraday > 0, f"{intraday:,}")
    check("capture log populated", caps > 0, str(caps))
    check("no capture left in ERROR",
          db.scalar("SELECT COUNT(*) FROM data_capture_log WHERE status='ERROR'") == 0)

    # ---------------------------------------------- the "do not dirty" guarantee
    before_daily = daily
    before_caps = caps
    rc, out = run(home, "scan", "--date", "2026-09-03")
    check("re-scan exits 0", rc == 0, f"rc={rc}")
    check("re-scan makes zero API calls", "api calls made                    0" in out
          or "api calls made                   0" in out,
          [l for l in out.splitlines() if "api calls" in l][0].strip()
          if "api calls" in out else "")
    check("re-scan writes zero new rows",
          db.scalar("SELECT COUNT(*) FROM daily_ohlcv") == before_daily,
          f"{db.scalar('SELECT COUNT(*) FROM daily_ohlcv'):,} vs {before_daily:,}")
    skipped = [l for l in out.splitlines() if "skipped" in l]
    n_skipped = int(skipped[0].split()[-1]) if skipped else 0
    check("re-scan reports skipped fetches", n_skipped > 50,
          skipped[0].strip() if skipped else "line not found")

    # ---------------------------------------------------------------- restart
    rc, out = run(home, "status")
    check("status exits 0 after restart", rc == 0, f"rc={rc}")
    check("status sees the stored scan runs",
          db.scalar("SELECT COUNT(*) FROM scan_runs") >= 2)
    check("status sees the gate audit trail",
          db.scalar("SELECT COUNT(*) FROM gate_results") > 100,
          str(db.scalar("SELECT COUNT(*) FROM gate_results")))
    check("status sees the capture history",
          db.scalar("SELECT COUNT(*) FROM data_capture_log") >= before_caps)

    # ---------------------------------------------------------------- report
    rc, out = run(home, "report")
    check("report exits 0", rc == 0, f"rc={rc}")
    check("report names the macro tier", "tier" in out)
    check("report shows the gate funnel", "Gate funnel" in out)

    # ---------------------------------------------------------------- manage
    rc, out = run(home, "manage", "--date", "2026-09-03")
    check("manage exits 0 with no open positions", rc == 0, f"rc={rc}")
    check("manage reports an empty book", "none open" in out)

    # ---------------------------------------------------------------- backtest
    rc, out = run(home, "backtest", "--from", "2026-08-01", "--to", "2026-09-11")
    check("backtest exits 0", rc == 0, f"rc={rc}")
    check("backtest declares it uses no network", "NO NETWORK" in out)
    check("backtest warns about survivorship bias", "Survivorship bias" in out)
    check("backtest counts stored sessions", "sessions" in out)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("RESULT: all pipeline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
