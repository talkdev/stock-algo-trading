#!/usr/bin/env python3
"""
Run every test in the project.

    python run_tests.py

Exits non-zero if any suite fails, so it can be used as a pre-commit or CI gate.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(os.path.normpath(str(Path(__file__).resolve().parent)))

# A suite is addressed by file name, never by a hard-coded depth: tests/ is the
# documented home, the project root is accepted as well, and the paths come from
# __file__ rather than the working directory, so `python run_tests.py` works from
# wherever the shell happens to be.
SUITES = [
    ("indicators", "test_indicators.py",
     "numerical calibration: Hurst, Kalman, OU half-life, ADF, BNS, Kelly"),
    ("lifecycle", "test_lifecycle.py",
     "arm -> fill -> exit ladder -> report -> restart, through the real code path"),
    ("pipeline", "test_pipeline.py",
     "end-to-end scan against the offline mock: gates, capture log, restart safety"),
]


def locate(filename: str) -> Path | None:
    for cand in (BASE / "tests" / filename, BASE / filename,
                 BASE / "jfou" / "tests" / filename):
        if cand.is_file():
            return cand
    return None


def child_env() -> dict:
    """UTF-8 for the children.

    The suites print box-drawing characters, and on Windows both the child's stdout
    and the parent's decoding of it default to the ANSI codepage. Setting PYTHONUTF8
    makes the two agree; PYTHONPATH lets a suite import `jfou` without a .pth file.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONPATH", str(BASE))
    return env


def main() -> int:
    print("=" * 78)
    print("JF-OU test suite")
    print("=" * 78)
    results = []
    for name, filename, what in SUITES:
        path = locate(filename)
        if path is None:
            print(f"\n### {name}: MISSING ({BASE / 'tests' / filename})")
            results.append((name, 1, 0.0))
            continue
        print(f"\n### {name} -- {what}")
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(path)], cwd=str(BASE),
                             env=child_env())
        dt = time.time() - t0
        results.append((name, rc, dt))

    print("\n" + "=" * 78)
    print(f"{'suite':<14}{'result':<10}{'seconds':>9}")
    print("-" * 34)
    failed = 0
    for name, rc, dt in results:
        print(f"{name:<14}{'PASS' if rc == 0 else 'FAIL':<10}{dt:9.1f}")
        failed += 1 if rc else 0
    print("-" * 34)
    print(f"{len(results) - failed}/{len(results)} suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
