"""
strategy.py - the mean-reversion brain. PURE LOGIC: no DB, no network, no I/O.

The live engine (engine.py) and the backtester (backtest.py) both call these
exact functions on 5-minute bars, so live and replay decisions are
structurally identical. The only live-vs-replay difference is that the live
engine can also act on intra-bar ticks (quotes), which can only make an exit
EARLIER, never later.

SIGNAL MODEL (long-only, intraday, fade dips in an uptrend)
  CONTEXT (daily, built pre-open, no lookahead)
      daily close > SMA20  AND  SMA20 > SMA50   -> uptrend bias
      20-day average volume >= MIN_DAILY_AVG_VOLUME (liquidity)
  SCAN / SELECTION (5m bars, day-anchored)
      z = (close - SMA20) / std20
      DIP: within the last DIP_LOOKBACK bars, z fell to <= -DIP_Z and
           RSI(14) touched <= DIP_RSI  (price stretched far below its mean)
  ENTRY (evaluated on a bar CLOSE)
      z back in [ENTRY_Z_MIN, ENTRY_Z_MAX]  (still below the mean ...)
      z rising (z_now > z_prev)             (... and turning)
      green bar: close > open AND close > previous close (reclaim)
      RSI <= ENTRY_RSI_MAX (not already fully recovered)
      no new entries after LAST_ENTRY_AT
  RISK
      stop   = entry - 1.5*ATR(14), clamped to [0.30%, 1.50%] below entry
      target = SMA20 at entry (the mean itself - reversion is the profit)
      qty    = equity * RISK_PER_TRADE_PCT / (entry - stop), capped by
               MAX_POS_VALUE_PCT exposure and available cash
  EXITS
      STOP    price <= stop        (tick or bar low; gap-through fills at open)
      TARGET  price >= target      (mean reversion achieved)
      MEAN    bar close z >= EXIT_Z (back at the mean, slightly inside)
      TRAIL   after crossing the mean: stop ratchets to close - 1.0*ATR
      EOD     forced flat from 15:20 (intraday-only mandate)
      DATA_END forced flat on the symbol's last bar of the day (backtest)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from indicators import Bar, atr, rsi, sma, vwap, zscores


@dataclass
class StockContext:
    symbol: str
    date: str
    daily_close: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    atr14: float | None = None
    avg_vol20: int | None = None
    trend_ok: int = 0

    @classmethod
    def from_row(cls, r) -> "StockContext":
        if r is None:
            return cls("?", "?")
        return cls(
            symbol=r["symbol"], date=r["date"],
            daily_close=r["daily_close"], sma20=r["sma20"], sma50=r["sma50"],
            atr14=r["atr14"], avg_vol20=r["avg_vol20"],
            trend_ok=int(r["trend_ok"] or 0),
        )


@dataclass
class EntryPlan:
    symbol: str
    t: str                 # bar time (IST) on which the trigger fired
    price: float           # bar close (simulated fill base)
    qty: int
    stop: float
    target: float
    z: float
    rsi: float
    atr: float
    dip_z: float
    sma: float
    reason: str            # human-readable justification


@dataclass
class ExitCall:
    reason: str            # STOP | TARGET | MEAN | EOD | DATA_END
    price: float           # suggested fill price (before slippage)
    note: str              # human-readable justification


class Strategy:
    def __init__(self, cfg=None):
        # cfg may be the config module (defaults) or a make_cfg() namespace
        # (tuned overrides from best_params.json / the tuner).
        self.cfg = cfg or config.make_cfg()

    # ------------------------------------------------------------------ scan
    def precompute(self, bars: list[Bar]) -> dict:
        """Indicator series for a full bar list, computed ONCE.
        The backtest/tuner call this per (symbol, day) and slice per bar,
        which is ~10x faster than recomputing per bar (bit-identical values:
        z[i]/rsi[i]/atr[i]/sma[i] depend only on bars[:i+1])."""
        n = self.cfg
        closes = [b.c for b in bars]
        return {
            "z": zscores(closes, n.SMOOTH_N),
            "rsi": rsi(closes, n.RSI_N),
            "atr": atr(bars, n.ATR_N),
            "sma": sma(closes, n.SMOOTH_N),
        }

    def scan(self, bars: list[Bar], pre: dict | None = None) -> dict | None:
        """Indicators for the most recent bar. None if not enough data yet.
        `pre` is an optional precompute() dict aligned to `bars`."""
        n = self.cfg
        if len(bars) < n.WARMUP_BARS:
            return None
        if pre is None:
            pre = self.precompute(bars)
        zs, rsis, atrs = pre["z"], pre["rsi"], pre["atr"]
        if zs[-1] is None or zs[-2] is None or atrs[-1] is None:
            return None
        recent_z = zs[-n.DIP_LOOKBACK:]
        if any(z is None for z in recent_z):
            return None
        recent_rsi = [r for r in rsis[-n.DIP_LOOKBACK:] if r is not None]
        closes = [b.c for b in bars]
        w = closes[-n.DIP_LOOKBACK:]
        return {
            "z": zs[-1],
            "z_prev": zs[-2],
            "min_z": min(recent_z),
            "rsi": rsis[-1],
            "min_rsi": min(recent_rsi) if recent_rsi else None,
            "atr": atrs[-1],
            "sma": pre["sma"][-1],
            "depth": max(w) - min(w),
        }

    # ----------------------------------------------------------------- entry
    def evaluate_entry(self, symbol: str, ctx: StockContext | None,
                       bars: list[Bar], now_hm: str | None = None,
                       equity: float | None = None,
                       cash: float | None = None,
                       pre: dict | None = None):
        """
        Returns (plan | None, signals, skip_reason).
        `signals` is a list of (kind, detail) tuples to persist/log
        (e.g. a fresh DIP crossing). `pre` = optional precompute() dict.
        """
        n = self.cfg
        sigs: list[tuple[str, str]] = []
        m = self.scan(bars, pre)
        if m is None:
            return None, sigs, "warmup"
        z, z_prev = m["z"], m["z_prev"]

        # informational: z just crossed into dip territory
        if z_prev > -n.DIP_Z and z <= -n.DIP_Z:
            sigs.append(("DIP",
                         f"z crossed -{n.DIP_Z:.1f}: {z_prev:+.2f} -> {z:+.2f}, "
                         f"RSI {m['rsi']:.1f}" if m["rsi"] is not None
                         else f"z crossed -{n.DIP_Z:.1f}: {z_prev:+.2f} -> {z:+.2f}"))

        # ---- filters (each returns an explicit, logged reason) ----
        if n.TREND_FILTER:
            if ctx is None or ctx.trend_ok == 0:
                if ctx is None:
                    why = "no daily context for today"
                else:
                    why = (f"daily close {ctx.daily_close:.2f} vs "
                           f"SMA20 {ctx.sma20:.2f} / SMA50 {ctx.sma50:.2f}")
                return None, sigs, f"trend filter ({why})"
        if ctx is not None and ctx.avg_vol20 is not None and \
                ctx.avg_vol20 < n.MIN_DAILY_AVG_VOLUME:
            return None, sigs, (f"liquidity (avgVol20 {ctx.avg_vol20:,.0f} < "
                                f"{n.MIN_DAILY_AVG_VOLUME:,})")
        if now_hm and now_hm >= n.LAST_ENTRY_AT:
            return None, sigs, "after last-entry time"
        if now_hm and now_hm < n.EARLY_ENTRY_CUTOFF:
            return None, sigs, "pre-cutoff (opening noise)"
        if m["min_z"] > -n.DIP_Z:
            return None, sigs, "no dip in lookback"
        if m["min_rsi"] is not None and m["min_rsi"] > n.DIP_RSI:
            return None, sigs, (f"dip not oversold (min RSI {m['min_rsi']:.1f} "
                                f"> {n.DIP_RSI})")
        if z < n.ENTRY_Z_MIN or z > n.ENTRY_Z_MAX:
            return None, sigs, (f"z {z:+.2f} outside entry band "
                                f"[{n.ENTRY_Z_MIN:+.1f}, {n.ENTRY_Z_MAX:+.1f}]")
        if z <= z_prev:
            return None, sigs, "z not recovering"
        b, pb = bars[-1], bars[-2]
        if not (b.c > b.o and b.c > pb.c):
            return None, sigs, "no green reclaim bar"
        if n.MIN_BAR_CLOSE_POS > 0:
            rng_ = b.h - b.l
            cpos = 0.5 if rng_ < 1e-9 else (b.c - b.l) / rng_
            if cpos < n.MIN_BAR_CLOSE_POS:
                return None, sigs, (f"weak bar (close at {cpos:.0%} of range "
                                    f"< {n.MIN_BAR_CLOSE_POS:.0%})")
        if m["rsi"] is not None and m["rsi"] > n.ENTRY_RSI_MAX:
            return None, sigs, f"RSI too hot ({m['rsi']:.1f} > {n.ENTRY_RSI_MAX})"
        if m["atr"] is None or m["atr"] <= 0:
            return None, sigs, "ATR unavailable"
        if n.USE_VWAP_FILTER:
            vw = vwap(bars)
            if vw and b.c > vw * 1.001:
                return None, sigs, f"above VWAP ({b.c:.2f} > {vw:.2f})"
        if n.MIN_ATR_PCT > 0 and m["atr"] / b.c * 100 < n.MIN_ATR_PCT:
            return None, sigs, (f"too quiet (ATR {m['atr'] / b.c * 100:.3f}% "
                                f"< {n.MIN_ATR_PCT}%)")
        if n.MAX_ATR_PCT > 0 and m["atr"] / b.c * 100 > n.MAX_ATR_PCT:
            return None, sigs, (f"event regime (ATR {m['atr'] / b.c * 100:.3f}% "
                                f"> {n.MAX_ATR_PCT}%)")
        if n.DIP_MIN_DEPTH_ATR > 0 and m["depth"] < n.DIP_MIN_DEPTH_ATR * m["atr"]:
            return None, sigs, (f"dip too shallow ({m['depth']:.2f} < "
                                f"{n.DIP_MIN_DEPTH_ATR}x ATR {m['atr']:.2f})")

        # ---- risk geometry ----
        stop = b.c - n.SL_ATR_MULT * m["atr"]
        stop = max(stop, b.c * (1 - n.SL_MAX_PCT / 100.0))
        stop = min(stop, b.c * (1 - n.SL_MIN_PCT / 100.0))
        target = m["sma"]

        qty = 0
        if equity and cash:
            qty = self.size(equity, cash, b.c, stop)
            if qty < n.MIN_QTY:
                return None, sigs, f"position too small (qty {qty})"

        rsi_txt = f"{m['rsi']:.1f}" if m["rsi"] is not None else "n/a"
        reason = (
            f"dip z={m['min_z']:+.2f} (min RSI "
            f"{m['min_rsi']:.0f}" if m['min_rsi'] is not None
            else f"dip z={m['min_z']:+.2f} (min RSI n/a"
        )
        reason += (f") within last {n.DIP_LOOKBACK} bars; z recovering "
                   f"{z_prev:+.2f} -> {z:+.2f}; RSI {rsi_txt}; ATR(14) {m['atr']:.2f}")
        if ctx is not None:
            reason += (f"; daily trend UP (close {ctx.daily_close:.2f} > "
                       f"SMA20 {ctx.sma20:.2f} > SMA50 {ctx.sma50:.2f})")

        plan = EntryPlan(symbol, b.t, b.c, qty, stop, target, z,
                         m["rsi"] or 0.0, m["atr"], m["min_z"], m["sma"], reason)
        return plan, sigs, ""

    def size(self, equity: float, cash: float, price: float, stop: float) -> int:
        """Shares such that (price - stop) * qty == RISK_PER_TRADE_PCT of equity,
        capped by exposure limit and cash."""
        n = self.cfg
        risk_amt = equity * n.RISK_PER_TRADE_PCT / 100.0
        per_share = max(price - stop, price * 0.001)
        qty = int(risk_amt / per_share)
        qty = min(qty, int((equity * n.MAX_POS_VALUE_PCT / 100.0) / price))
        qty = min(qty, int((cash * 0.999) / price))
        return max(0, qty)

    # ----------------------------------------------------------------- exit
    def evaluate_exit(self, pos: dict, bars: list[Bar], tick: float | None = None,
                      now=None, is_last_bar: bool = False,
                      pre: dict | None = None,
                      bars_held: int | None = None):
        """
        Check exits for an open position.
        pos: mapping with at least 'stop' and 'target'.
        bars: day's bars up to and INCLUDING the current bar (the last one may
              be a live partial bar in the live engine).
        tick: latest price (live engine only; backtest passes None).
        pre: optional precompute() dict aligned to `bars`.
        bars_held: 5-min bars elapsed since entry (enables the TIME stop).
        Returns (ExitCall | None, new_stop) where new_stop may have trailed.
        """
        n = self.cfg
        b = bars[-1]
        price = tick if tick is not None else b.c
        low = min(b.l, price)
        high = max(b.h, price)
        new_stop = pos["stop"]

        # 1) EOD / data end - intraday-only mandate, checked first
        eod = now is not None and hm_of(now) >= n.EOD_FLAT_AT
        if eod or is_last_bar:
            why = ("forced flat (intraday-only mandate)" if eod
                   else "symbol's last bar of the day")
            return ExitCall("EOD" if eod else "DATA_END", price, why), new_stop

        # 2) stop loss (gap-through fills at the open, i.e. worse than stop)
        if low <= pos["stop"]:
            fill = min(price, pos["stop"])
            note = (f"price {price:.2f} breached stop {pos['stop']:.2f}"
                    if price >= pos["stop"]
                    else f"gap through stop {pos['stop']:.2f} at {price:.2f}")
            return ExitCall("STOP", fill, note), new_stop

        # 3) target = the mean itself (mean reversion achieved)
        if high >= pos["target"]:
            if b.o >= pos["target"]:
                fill, note = b.o, f"opened {b.o:.2f} above mean target {pos['target']:.2f}"
            elif tick is not None:
                fill, note = price, f"tick {price:.2f} reached mean target {pos['target']:.2f}"
            else:
                fill, note = pos["target"], f"high {b.h:.2f} reached mean target {pos['target']:.2f}"
            return ExitCall("TARGET", fill, note), new_stop

        # 4) bar-close mean reversion + trailing + time stop (needs full window)
        if len(bars) >= n.SMOOTH_N:
            if pre is not None:
                z, s, a = pre["z"][-1], pre["sma"][-1], pre["atr"][-1]
            else:
                closes = [x.c for x in bars]
                z = zscores(closes, n.SMOOTH_N)[-1]
                s = sma(closes, n.SMOOTH_N)[-1]
                a = atr(bars, n.ATR_N)[-1]
            if z is not None and z >= n.EXIT_Z:
                return ExitCall("MEAN", b.c,
                                f"close {b.c:.2f} back at mean (z {z:+.2f} >= "
                                f"{n.EXIT_Z:+.2f}, SMA20 {s:.2f})"), new_stop
            # time stop: no reversion after N bars - cut the dead money
            # (bar-close evaluations only; never on intra-bar ticks)
            if (tick is None and n.TIME_STOP_BARS and bars_held is not None
                    and bars_held >= n.TIME_STOP_BARS
                    and z is not None and z < n.EXIT_Z and b.c < pos["target"]):
                return ExitCall("TIME", b.c,
                                f"held {bars_held} bars without reversion "
                                f"(z {z:+.2f} < {n.EXIT_Z:+.2f}, still below "
                                f"mean {pos['target']:.2f})"), new_stop
            if b.c > pos["target"]:
                if a:
                    ns = max(pos["stop"], b.c - n.TRAIL_ATR_MULT * a)
                    if ns > pos["stop"] + 1e-9:
                        new_stop = ns
        return None, new_stop

    # ------------------------------------------------------- partial profits
    def plan_target_exit(self, pos) -> dict:
        """
        Decide how to take profit when price reaches the mean target - the
        classic MR money-maker:
          * sell PARTIAL_PCT of the position at the mean (lock in reversion),
          * move the stop to breakeven (free runner),
          * let the remainder run to target-2 = entry + R_MULT * (entry-stop).
        pos: mapping/dict/Row with qty, qty_remaining, partial_count,
        entry_fill, stop, target. Returns:
            {"action": "partial", "qty": q1, "new_stop": s, "target2": t2, ...}
            {"action": "full", "qty": q_left, ...}
        """
        n = self.cfg
        entry = pos["entry_fill"]
        stop = pos["stop"]
        left = _pget(pos, "qty_remaining", pos["qty"])
        done = _pget(pos, "partial_count", 0)
        if n.PARTIAL_PCT > 0 and done == 0 and left >= 2:
            q1 = int(round(pos["qty"] * n.PARTIAL_PCT / 100.0))
            q1 = max(1, min(q1, left - 1))
            r = entry - stop
            t2 = entry + r * n.R_MULT_TARGET2
            ns = max(stop, entry) if n.BE_AFTER_PARTIAL else stop
            note = (f"{q1} of {pos['qty']} sold at the mean; stop -> "
                    + (f"breakeven {ns:.2f}; " if n.BE_AFTER_PARTIAL else "")
                    + f"runner target {t2:.2f} ({n.R_MULT_TARGET2}R)")
            return {"action": "partial", "qty": q1, "new_stop": ns,
                    "target2": t2, "note": note}
        return {"action": "full", "qty": left,
                "note": "full close at mean" if done == 0
                else "runner closed at second target"}


def _pget(pos, key, default=None):
    """Field access that works on both dicts and sqlite3.Row."""
    try:
        v = pos[key]
        return default if v is None else v
    except (TypeError, KeyError, IndexError):
        return default


def hm_of(dt) -> str:
    return dt.strftime("%H:%M")
