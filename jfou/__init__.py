"""
JF-OU / NSE 2026 -- Jump-Filtered Ornstein-Uhlenbeck mean-reversion engine.

Package layout (the layout every path in this repository assumes):

    <root>/main.py            CLI entry point -- imports `jfou.*`
    <root>/run_tests.py       suite runner -- imports nothing, spawns tests/
    <root>/universe_nifty100.json
    <root>/data/jfou.sqlite3
    <root>/tests/test_*.py
    <root>/jfou/*.py          this package; modules use relative imports

Submodules are imported lazily on purpose: `import jfou` must not require numpy,
pandas or scipy, so a bare install can still read the config and the clock.
"""
from __future__ import annotations

__version__ = "1.0.1"

__all__ = [
    "backtest", "clock", "config", "console", "dataclient", "db", "engine",
    "execution", "gates", "indicators", "sizing",
]
