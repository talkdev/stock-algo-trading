"""
console.py - human-readable console output for STMR.

Design goals:
  * ASCII only - renders correctly in legacy Windows cmd / PowerShell.
  * Clear section banners, aligned tables.
  * Every decision line carries its "why" (data-backed justification),
    e.g. which z-score dipped, what the ATR stop is, why a stock was skipped.
"""
from __future__ import annotations

import sys
from datetime import datetime

try:  # make sure UTF-8 (rupee signs etc. never used, but be safe on Win)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

WIDTH = 100


def now_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def info(msg: str) -> None:
    print(f"[{now_str()}] {msg}")


def ok(msg: str) -> None:
    print(f"[{now_str()}] + {msg}")


def warn(msg: str) -> None:
    print(f"[{now_str()}] ! {msg}")


def err(msg: str) -> None:
    print(f"[{now_str()}] X {msg}")


def hr(ch: str = "=") -> None:
    print(ch * WIDTH)


def banner(title: str, lines: list[str] | None = None) -> None:
    hr("=")
    for ln in [title] + (lines or []):
        print(f"  {ln}")
    hr("=")


def kv(key: str, value, key_w: int = 16) -> None:
    print(f"  {key:<{key_w}}: {value}")


def table(headers: list[str], rows: list[list], aligns: list[str] | None = None) -> None:
    """Print an aligned ASCII table. aligns: 'l' or 'r' per column."""
    cols = len(headers)
    aligns = aligns or ["l"] * cols
    str_rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in str_rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(r[i]))
    widths = [min(w, 40) for w in widths]

    def fmt_row(cells):
        out = []
        for i, c in enumerate(cells):
            c = c[:40]
            out.append(c.rjust(widths[i]) if aligns[i] == "r" else c.ljust(widths[i]))
        return "  ".join(out).rstrip()

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for r in str_rows:
        print(fmt_row(r))


def inr(x: float, dec: int = 2) -> str:
    """Format a number with Indian digit grouping: 10,00,000.00"""
    x = float(x)
    neg = x < 0
    x = abs(round(x, dec))
    ip, fp = f"{x:.{dec}f}".split(".")
    if len(ip) > 3:
        last3 = ip[-3:]
        rest = ip[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        ip = ",".join(groups) + "," + last3
    return f"{'-' if neg else ''}{ip}.{fp}"


def pct(x: float, dec: int = 2) -> str:
    return f"{x:+.{dec}f}%"


def signed(x: float, dec: int = 2) -> str:
    return f"{x:+.{dec}f}"
