"""
indicators.py - pure-Python technical indicators (no numpy/pandas).

All functions are list-in / list-out with None padding so that index i of the
output always corresponds to input index i. This keeps the strategy module
deterministic, dependency-free and trivially unit-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Bar:
    """One OHLCV bar. `t` is the bar START time, IST 'YYYY-MM-DD HH:MM:SS'."""
    t: str
    o: float
    h: float
    l: float
    c: float
    v: int = 0


def sma(vals: list[float], n: int) -> list[float | None]:
    """Simple moving average (None until n values are available)."""
    out: list[float | None] = [None] * len(vals)
    s = 0.0
    for i, x in enumerate(vals):
        s += x
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_std(vals: list[float], n: int) -> list[float | None]:
    """Population standard deviation of the trailing n values."""
    out: list[float | None] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1: i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def zscores(closes: list[float], n: int) -> list[float | None]:
    """z = (close - SMA_n) / std_n  - the core mean-reversion statistic."""
    s = sma(closes, n)
    sd = rolling_std(closes, n)
    out: list[float | None] = []
    for i in range(len(closes)):
        if s[i] is None or sd[i] is None or sd[i] < 1e-9:
            out.append(None)
        else:
            out.append((closes[i] - s[i]) / sd[i])
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI. Needs n+1 closes for the first value (index n)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_g, avg_l = gains / n, losses / n

    def _val(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = _val(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
        out[i] = _val(avg_g, avg_l)
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    """Wilder's ATR. Needs n+1 bars for the first value (index n)."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n + 1:
        return out
    trs: list[float] = [0.0]
    for i in range(1, len(bars)):
        tr = max(
            bars[i].h - bars[i].l,
            abs(bars[i].h - bars[i - 1].c),
            abs(bars[i].l - bars[i - 1].c),
        )
        trs.append(tr)
    a = sum(trs[1: n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def vwap(bars: list[Bar]) -> float | None:
    """Day-anchored VWAP (typical price volume-weighted)."""
    pv = 0.0
    v = 0
    for b in bars:
        tp = (b.h + b.l + b.c) / 3.0
        pv += tp * b.v
        v += b.v
    return pv / v if v > 0 else None


def last_valid(seq: list, k: int):
    """The k-th most recent non-None value (k=1 -> last). None if missing."""
    seen = 0
    for x in reversed(seq):
        if x is not None:
            seen += 1
            if seen == k:
                return x
    return None
