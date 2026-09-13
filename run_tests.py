#!/usr/bin/env python3
"""
Run every test in the project.

    python run_tests.py

Exits non-zero if any suite fails, so it can be used as a pre-commit or CI gate.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SUITES = [
    ("indicators", BASE / "tests" / "test_indicators.py",
     "numerical calibration: Hurst, Kalman, OU half-life, ADF, BNS, Kelly"),
    ("lifecycle", BASE / "tests" / "test_lifecycle.py",
     "arm -> fill -> exit ladder -> report -> restart, through the real code path"),
    ("pipeline", BASE / "tests" / "test_pipeline.py",
     "end-to-end scan against the offline mock: gates, capture log, restart safety"),
]


def main() -> int:
    print("=" * 78)
    print("JF-OU test suite")
    print("=" * 78)
    results = []
    for name, path, what in SUITES:
        if not path.exists():
            print(f"\n### {name}: MISSING ({path})")
            results.append((name, 1, 0.0))
            continue
        print(f"\n### {name} -- {what}")
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(path)])
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
