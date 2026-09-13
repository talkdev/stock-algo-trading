"""
Gates G0-G4 and the scored confluence S1-S4 (spec Parts IV-IX).

Every gate returns a GateResult carrying the numbers it used and a human-readable
justification. Nothing here prints or touches the network: gates are pure functions of
a data snapshot, which is what lets backtest.py replay them from the database through
exactly this code path.

Design note on ordering: gates are evaluated cheapest-first and short-circuit. A name
that fails G1 (tradability) is never sent through the Kalman filter or the GARCH fit.
On a 100-name universe that is the difference between a scan that finishes and one that
does not.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import indicators as I
from .config import CFG


@dataclass
class GateResult:
    gate: str
    passed: bool
    verdict: str = ""                 # PASS | FAIL | SKIP | REJ_*
    value: float | None = None
    headline: str = ""                # one-line human summary
    justification: str = ""           # why, with the numbers
    detail: dict = field(default_factory=dict)

    def to_row(self, run_id: str, instrument_key: str) -> dict:
        return {"run_id": run_id, "instrument_key": instrument_key, "gate": self.gate,
                "passed": int(bool(self.passed)), "verdict": self.verdict or
                ("PASS" if self.passed else "FAIL"),
                "value": self.value, "detail": json.dumps(self.detail, default=str)}


def _g0_reject(hard_block: bool, tier: str, close, reasons: list[str]) -> str:
    """Name the gate that actually closed the macro door.

    A single flat REJ_MACRO_VIX here was wrong: on a session where VIX is 11.12 (Green)
    but the Nifty is below its EMA50, the rejection is the index-trend gate, and the
    audit trail must say so or the operator tunes the wrong threshold.
    """
    if tier in ("Red", "Crisis"):
        return "REJ_MACRO_VIX"
    joined = " ".join(reasons)
    if "VIX percentile" in joined or "intraday >=" in joined:
        return "REJ_MACRO_VIX"
    if "EMA" in joined or "Nifty history" in joined:
        return "REJ_MACRO_INDEX"
    if "breadth" in joined:
        return "REJ_MACRO_INDEX"
    return "REJ_MACRO_VIX" if close is not None else "REJ_MACRO_INDEX"


def _res(gate: str, ok: bool, headline: str, justification: str,
         value: float | None = None, verdict: str = "", **detail) -> GateResult:
    return GateResult(gate=gate, passed=ok, verdict=verdict or ("PASS" if ok else "FAIL"),
                      value=value, headline=headline, justification=justification,
                      detail=detail)


# ====================================================================== G0 macro
def g0_macro(vix: dict, index: pd.DataFrame, breadth: dict, macro: dict) -> GateResult:
    """Part IV. Evaluated once per session, before any name is scanned.

    vix     : {close, prev_close, intraday_high, series}   series = 250d closes
    index   : Nifty 50 daily OHLCV
    breadth : {pct_above_200dma}
    macro   : {brent, usdinr_series, gsec10y, fpi_streak}
    """
    reasons: list[str] = []
    detail: dict = {}

    close = vix.get("close")
    detail["vix"] = close
    detail["breadth"] = breadth.get("pct_above_200dma")

    # ---- tier ----
    if close is None:
        tier = "DEGRADED"
        reasons.append("India VIX unavailable -> tier DEGRADED, treated as Amber")
        max_names = CFG.max_names_amber
    elif close < CFG.vix_green_max:
        tier = "Green"; max_names = CFG.max_names_green
        reasons.append(f"VIX {close:.2f} < {CFG.vix_green_max} -> Green, up to "
                       f"{max_names} concurrent names")
    elif close < CFG.vix_amber_max:
        tier = "Amber"; max_names = CFG.max_names_amber
        reasons.append(f"VIX {close:.2f} in [{CFG.vix_green_max},{CFG.vix_amber_max}) -> "
                       f"Amber, max {max_names} names, z-band tightened, risk halved")
    elif close < CFG.vix_crisis:
        tier = "Red"; max_names = 0
        reasons.append(f"VIX {close:.2f} >= {CFG.vix_amber_max} -> Red, NO new entries")
    else:
        tier = "Crisis"; max_names = 0
        reasons.append(f"VIX {close:.2f} >= {CFG.vix_crisis} -> Crisis, no entries and "
                       f"every open stop tightens to Entry-0.75R")

    block = tier in ("Red", "Crisis", "DEGRADED") and tier != "DEGRADED"
    hard_block = tier in ("Red", "Crisis")

    # ---- percentile overlay ----
    series = vix.get("series")
    if series is not None and len(series) >= 20:
        pct = float(np.mean(np.asarray(series, float) <= close)) if close is not None else np.nan
        detail["vix_percentile"] = pct
        if np.isfinite(pct) and pct >= CFG.vix_percentile_block:
            hard_block = True
            reasons.append(f"VIX percentile {pct:.0%} of trailing {len(series)} sessions "
                           f">= {CFG.vix_percentile_block:.0%} -> block new entries "
                           f"(adapts if the calm-vol centre drifts)")

    # ---- intraday expansion ----
    prev = vix.get("prev_close")
    if close is not None and prev:
        chg = close / prev - 1.0
        detail["vix_change"] = chg
        if chg >= CFG.vix_intraday_expand_block:
            hard_block = True
            reasons.append(f"VIX +{chg:.1%} intraday >= +{CFG.vix_intraday_expand_block:.0%} "
                           f"-> block. India VIX moved +25% on 2-Mar-2026 and +23% on "
                           f"4-Mar; this catches shock onset a day before the level gate")

    # ---- index trend ----
    ema = None
    if index is not None and len(index) >= CFG.index_ema:
        c = index["close"].to_numpy(dtype=float)
        ema = float(I.ema(pd.Series(c), CFG.index_ema).iloc[-1])
        last = float(c[-1])
        detail["index_close"] = last
        detail["index_ema50"] = ema
        if last <= ema:
            hard_block = True
            reasons.append(f"Nifty {last:,.0f} <= EMA{CFG.index_ema} {ema:,.0f} -> index "
                           f"trend gate closed")
        else:
            reasons.append(f"Nifty {last:,.0f} > EMA{CFG.index_ema} {ema:,.0f} "
                           f"(+{last/ema-1:.2%}) -> index trend gate open")
    else:
        reasons.append("insufficient Nifty history -> index trend gate treated as closed")
        hard_block = True

    # ---- breadth ----
    br = breadth.get("pct_above_200dma")
    if br is not None:
        if br < CFG.breadth_min:
            hard_block = True
            reasons.append(f"breadth {br:.1%} below own 200-DMA < {CFG.breadth_min:.0%} -> "
                           f"block. In a -13.7% YTD year this is what stops you buying "
                           f"bear-market rallies that look like individual uptrends")
        else:
            reasons.append(f"breadth {br:.1%} >= {CFG.breadth_min:.0%}")

    # ---- 2026 macro overlays (reduce the pool; they do not veto) ----
    excluded_sectors: list[str] = []
    if macro:
        b = macro.get("brent")
        if b and b > CFG.brent_exclude_above:
            excluded_sectors += ["OMC", "PAINTS", "AVIATION", "TYRES", "SPECIALTY_CHEM"]
            reasons.append(f"Brent ${b:.1f} > ${CFG.brent_exclude_above} -> excluding "
                           f"input-cost-compressed sectors")
        fx = macro.get("usdinr_series")
        if fx is not None and len(fx) > CFG.usdinr_move_window:
            mv = float(fx[-1] / fx[-1 - CFG.usdinr_move_window] - 1.0)
            detail["usdinr_move_20"] = mv
            if mv > CFG.usdinr_move_block:
                excluded_sectors += ["CONSUMER_IMPORT", "NBFC"]
                reasons.append(f"USDINR +{mv:.2%} over {CFG.usdinr_move_window} sessions "
                               f"> {CFG.usdinr_move_block:.0%} -> excluding import-heavy "
                               f"consumer and NBFCs")
        g = macro.get("gsec10y")
        if g and g > CFG.gsec10y_exclude_above:
            excluded_sectors += ["BANK", "NBFC", "REALTY", "AUTO", "CAPGOODS"]
            reasons.append(f"India 10Y {g:.2f}% > {CFG.gsec10y_exclude_above}% -> excluding "
                           f"rate-sensitive sectors (NIM compression)")
        streak = int(macro.get("fpi_streak") or 0)
        if streak >= CFG.fpi_seller_streak_block:
            max_names = min(max_names, CFG.max_names_amber)
            reasons.append(f"FPI net seller {streak} consecutive sessions >= "
                           f"{CFG.fpi_seller_streak_block} -> max concurrent names cut to "
                           f"{max_names}; supply overwhelms mean reversion")

    detail.update({"tier": tier, "max_names": max_names,
                   "excluded_sectors": excluded_sectors})
    ok = not hard_block
    return _res("G0_MACRO", ok,
                f"macro tier {tier}" + ("" if ok else " - NEW ENTRIES BLOCKED"),
                " | ".join(reasons), value=close,
                verdict=("PASS" if ok else _g0_reject(hard_block, tier, close, reasons)),
                **detail)


# ====================================================================== G1 tradability
def g1_tradable(daily: pd.DataFrame, instrument: dict, events: pd.DataFrame,
                circuit_hits: int, as_of: str, adv_shares: float) -> GateResult:
    """Part V. Cheap arithmetic only -- this runs on all 100 names."""
    r: list[str] = []
    c = CFG

    if daily is None or len(daily) < 60:
        return _res("G1_TRADABLE", False, "insufficient history",
                    f"only {0 if daily is None else len(daily)} daily bars; need 60",
                    verdict="REJ_UNIVERSE")

    close = float(daily["close"].iloc[-1])
    # G1.1 liquidity floor
    adv_val = float((daily["close"] * daily["volume"]).tail(20).mean())
    r.append(f"ADV20 {adv_val/1e7:.1f} Cr")
    if adv_val < c.adv_value_floor_cr * 1e7:
        return _res("G1_TRADABLE", False, "ADV below the derived floor",
                    f"ADV20 {adv_val/1e7:.2f} Cr < {c.adv_value_floor_cr:.0f} Cr floor. "
                    f"Smallest legal position is 2 lots = Rs30L; at a "
                    f"{c.participation_cap:.1%} cap this name needs "
                    f"Rs{2*15e5/c.participation_cap/1e7:.0f} Cr ADV or no compliant trade "
                    f"exists (Part 11.5)", value=adv_val, verdict="REJ_CAPACITY",
                    adv20_value_cr=adv_val / 1e7)
    if close < 5.0:
        return _res("G1_TRADABLE", False, "penny stock",
                    f"close {close:.2f} below the Rs5 penny floor", value=close,
                    verdict="REJ_UNIVERSE")

    # G1.3 circuit hit in the last 30 sessions
    if circuit_hits > 0:
        return _res("G1_TRADABLE", False, "price circuit hit recently",
                    f"{circuit_hits} circuit hit(s) in the last "
                    f"{c.circuit_lookback_sessions} sessions -> cash-segment fallback is "
                    f"unreliable, block", verdict="REJ_EVENT", circuit_hits=circuit_hits)

    # G1.4 earnings blackout
    if events is not None and len(events):
        ev = events.copy()
        ev["event_date"] = pd.to_datetime(ev["event_date"], errors="coerce")
        ev = ev.dropna(subset=["event_date"])
        asof = pd.Timestamp(as_of)
        future = ev[ev["event_date"] > asof].sort_values("event_date")
        past = ev[ev["event_date"] <= asof].sort_values("event_date")
        if len(future):
            days = int((future["event_date"].iloc[0] - asof).days)
            if days <= c.earnings_min_sessions_ahead:
                return _res(
                    "G1_TRADABLE", False, "earnings blackout",
                    f"{future['event_type'].iloc[0]} on "
                    f"{future['event_date'].iloc[0]:%Y-%m-%d} is {days}d away "
                    f"(<= {c.earnings_min_sessions_ahead}). The intraday jump test is blind "
                    f"to FUTURE announcements; a 10% overnight earnings gap bypasses the "
                    f"stop entirely", verdict="REJ_EVENT", days_to_event=days)
            r.append(f"next event {days}d away (> {c.earnings_min_sessions_ahead}d, clear)")
        elif len(past):
            since = int((asof - past["event_date"].iloc[-1]).days)
            if since <= c.earnings_recent_ok_sessions:
                r.append(f"result published {since}d ago (<= "
                         f"{c.earnings_recent_ok_sessions}d, allowed)")
    else:
        r.append("no corporate-event feed loaded -> G1.4 unknown, conservative pass")

    return _res("G1_TRADABLE", True, "tradable", " | ".join(r), value=adv_val,
                adv20_value_cr=adv_val / 1e7, close=close,
                adv_shares=adv_shares)


def g1_event_blackout(as_of: str, expiry_calendar: dict) -> GateResult:
    """G1.5 -- index-level noise days. Part V."""
    d = pd.Timestamp(as_of)
    hits: list[str] = []
    if (d.month, d.day) == (2, 1):
        hits.append("Union Budget day")
    for name, dates in (expiry_calendar or {}).items():
        if d.strftime("%Y-%m-%d") in [str(x) for x in (dates or [])]:
            hits.append(name)
    if hits:
        return _res("G1_EVENT_DAY", False, "event blackout",
                    f"{', '.join(hits)} -> no new entries. Expiry afternoons and policy "
                    f"days inject index-level noise that contaminates stock-level stop "
                    f"placement", verdict="REJ_EVENT")
    return _res("G1_EVENT_DAY", True, "no event blackout", f"{as_of} is a clear session")


# ====================================================================== G2 trend
def g2_trend(daily: pd.DataFrame) -> GateResult:
    """Part VI. Three independent conditions, all required."""
    c = daily["close"].to_numpy(dtype=float)
    out: dict = {}
    fails: list[str] = []
    oks: list[str] = []

    # 6.1 Hurst
    h = I.hurst(c, CFG.hurst_window)
    out["hurst"] = h
    if not np.isfinite(h) or h <= CFG.hurst_min:
        fails.append(f"H={h:.3f} <= {CFG.hurst_min} over {CFG.hurst_window} sessions: the "
                     f"series is not structurally persistent, so 'trend' is noise")
    else:
        oks.append(f"H={h:.3f} > {CFG.hurst_min} (persistent)")

    # 6.2 Kalman velocity, Q/R from EM on a 60-session block
    q, rr = I.kalman_qr_em(c, CFG.kalman_em_window)
    ks = I.kalman_local_linear(c, q_over_r=(q / rr if rr > 0 else CFG.kalman_q_over_r))
    out.update({"kalman_velocity": ks.velocity, "vel_std": ks.vel_std,
                "kalman_price": ks.price_state, "kalman_q": q, "kalman_r": rr,
                "slope_ok": ks.slope_ok})
    if ks.velocity <= 0:
        fails.append(f"Kalman velocity {ks.velocity:+.5f} <= 0")
    elif not ks.slope_ok:
        fails.append(f"velocity {ks.velocity:+.5f} is positive but not at the "
                     f"{CFG.kalman_slope_confidence:.0%} lower bound "
                     f"({ks.velocity - 1.96*ks.vel_std:+.5f})")
    else:
        oks.append(f"velocity {ks.velocity:+.5f} (se {ks.vel_std:.5f}) positive at the "
                   f"95% bound")

    # 6.3 drift confirmation
    n = min(CFG.ols_window, len(c))
    sl, t = I.ols_slope_tstat(np.log(c), n)
    ema50 = float(I.ema(pd.Series(c), CFG.ema_trend).iloc[-1])
    out.update({"ols_slope": sl, "ols_tstat": t, "ema50": ema50,
                "close_above_ema50": bool(c[-1] > ema50)})
    if t < CFG.ols_tstat_min:
        fails.append(f"60-session OLS t={t:.2f} < {CFG.ols_tstat_min} "
                     f"(~99% confidence required)")
    else:
        oks.append(f"OLS t={t:.2f} >= {CFG.ols_tstat_min}")
    if c[-1] <= ema50:
        fails.append(f"close {c[-1]:,.2f} <= EMA{CFG.ema_trend} {ema50:,.2f}")
    else:
        oks.append(f"close {c[-1]:,.2f} > EMA{CFG.ema_trend} {ema50:,.2f}")

    ok = not fails
    return _res("G2_TREND", ok,
                "trend persistent" if ok else "trend not established",
                ("; ".join(oks) + " || BLOCKED BY: " + "; ".join(fails)) if fails
                else "; ".join(oks),
                value=h, verdict=("PASS" if ok else "REJ_TREND"), **out)


# ====================================================================== G3 jump
def g3_jump(intraday: pd.DataFrame, daily: pd.DataFrame, s_m: np.ndarray | None,
            gift_divergence: float | None = None) -> GateResult:
    """Part VII. The dip must be continuous diffusion, not a Poisson jump."""
    out: dict = {}
    fails: list[str] = []
    oks: list[str] = []

    # 7.2 de-seasonalized BNS on the current dip window
    if intraday is None or len(intraday) < 30:
        return _res("G3_JUMP", False, "no intraday data",
                    "fewer than 30 five-minute bars available; the jump test cannot run. "
                    "Note Upstox 5-minute depth starts ~2022-01 (Part XXIII 23.1)",
                    verdict="REJ_JUMP_INTRADAY")
    # CRITICAL: returns must be computed WITHIN each session.
    # The gap between a 15:25 close and the next 09:15 open is routinely 10x+ the
    # median intraday bar. A flat pct_change() over a multi-session frame splices those
    # overnight gaps into the series and the bipower statistic reports a Poisson jump
    # on virtually every name, every day. Measured on clean synthetic data with no jump
    # present: flat pct_change Z = 11.9, intra-session returns Z = 0.57.
    # An earlier version tried to fix this with a positional slice of the return array,
    # which removed the right NUMBER of rows but not the right rows. groupby().pct_change()
    # is correct by construction. Overnight gaps are tested separately by rule 7.3
    # against 1.5 x sigma_daily, which is where they belong.
    sess = pd.Series(intraday.index.date, index=intraday.index)
    slot = pd.Series(intraday.groupby(sess).cumcount() + 1, index=intraday.index)
    r5_s = intraday["close"].groupby(sess).pct_change()
    mask = r5_s.notna().to_numpy()
    r5 = r5_s.dropna().to_numpy(dtype=float)

    # Align the seasonal profile to each bar's own slot. Tiling a 75-element profile
    # across a multi-session window misaligns every session after the first.
    aligned_sm = None
    if s_m is not None and len(s_m) > 0:
        arr = np.asarray(s_m, dtype=float)
        n_bars = int(CFG.bars_per_session)
        if len(arr) == n_bars:
            idx = np.clip(slot.to_numpy() - 1, 0, n_bars - 1)[mask]
            aligned_sm = arr[idx]
        elif len(arr) == len(r5):
            aligned_sm = arr

    z, nbars = I.bns_jump_z(r5, aligned_sm)
    out.update({"bns_z": z, "bars": nbars,
                "bars_dropped_at_boundaries": int(sess.duplicated().sum()),
                "deseasonalised": bool(aligned_sm is not None)})
    if not np.isfinite(z):
        fails.append("BNS statistic undefined")
    elif z >= CFG.bns_z_max:
        fails.append(f"Z_jump {z:.2f} >= {CFG.bns_z_max} (p<0.05): this dip is a Poisson "
                     f"jump, i.e. the start of a markdown, not diffusion")
    else:
        oks.append(f"Z_jump {z:.2f} < {CFG.bns_z_max} over {nbars} bars: continuous "
                   f"diffusion" + (" (de-seasonalised)" if out["deseasonalised"] else
                                   " (RAW - seasonal factors unavailable, so ordinary "
                                   "opening prints may read as jumps)"))

    # 7.3 overnight gap
    c = daily["close"].to_numpy(dtype=float)
    o = daily["open"].to_numpy(dtype=float)
    if len(c) > 21:
        sigma = float(np.std(np.diff(np.log(c[-21:])), ddof=1))
        gap = abs(math.log(o[-1]) - math.log(c[-2]))
        limit = CFG.overnight_gap_sigma_max * sigma
        out.update({"overnight_gap": gap, "sigma_daily": sigma, "gap_limit": limit})
        if gap > limit:
            fails.append(f"overnight gap {gap:.2%} > {CFG.overnight_gap_sigma_max} x "
                         f"sigma_daily ({limit:.2%}). >70% of shocks land overnight and a "
                         f"stock that gaps down then trades quietly returns Z_jump ~ 0 and "
                         f"fools the intraday test")
        else:
            oks.append(f"overnight gap {gap:.2%} <= {limit:.2%}")

    # 7.4 GIFT Nifty divergence -- logged, never a rejection
    if gift_divergence is not None:
        out["gift_divergence"] = gift_divergence
        if gift_divergence < -0.01:
            oks.append(f"GIFT Nifty implied {gift_divergence:.2%} but the cash open held: "
                       f"the dip is being absorbed by a specific buyer (positive signal, "
                       f"logged not rejected)")

    ok = not fails
    return _res("G3_JUMP", ok, "diffusion confirmed" if ok else "jump detected",
                "; ".join(oks + (["BLOCKED: " + "; ".join(fails)] if fails else [])),
                value=z, verdict=("PASS" if ok else "REJ_JUMP_INTRADAY"), **out)


# ====================================================================== G4 stationarity
def g4_stationarity(daily: pd.DataFrame, ema_span: int = 20) -> GateResult:
    """Part VIII. The half-life formula is only valid if the spread is I(0)."""
    x = I.ema_spread(daily["close"].to_numpy(dtype=float), ema_span)
    if len(x) < 40:
        return _res("G4_STATIONARY", False, "insufficient spread history",
                    f"only {len(x)} usable spread observations after the EMA warm-up",
                    verdict="REJ_NONSTATIONARY")
    p = I.adf_pvalue(x)
    if not np.isfinite(p):
        return _res("G4_STATIONARY", False, "ADF undefined",
                    "the Augmented Dickey-Fuller test did not converge",
                    verdict="REJ_NONSTATIONARY")
    ok = p < CFG.adf_p_max
    return _res("G4_STATIONARY", ok,
                "spread is I(0)" if ok else "spread is non-stationary",
                (f"ADF p={p:.4f} < {CFG.adf_p_max}: x = ln(P)-ln(EMA{ema_span}) is "
                 f"stationary, so the OU half-life is meaningful") if ok else
                (f"ADF p={p:.4f} >= {CFG.adf_p_max}: the spread behaves like a random walk "
                 f"with drift. AR(1) on an integrated series returns a SPURIOUS "
                 f"mean-reversion coefficient, so no half-life is computed and the name "
                 f"is rejected"),
                value=p, verdict=("PASS" if ok else "REJ_NONSTATIONARY"), adf_p=p,
                n_obs=len(x))


# ====================================================================== S1-S4
def s1_pullback(daily: pd.DataFrame, kalman_price: float, tier: str) -> GateResult:
    """S1 -- GARCH-studentized pullback depth. Part IX."""
    c = daily["close"].to_numpy(dtype=float)
    r = np.diff(np.log(c))
    sig, garch_params = I.garch11_sigma(r, CFG.garch_window)
    mu = kalman_price if np.isfinite(kalman_price) else float(
        I.ema(pd.Series(c), 20).iloc[-1])
    # UNIT CORRECTION. The spec writes Z = (P_t - mu_t)/sigma_t, but sigma_t comes from
    # a GARCH fit on LOG RETURNS, so it is in return units (~1.6%/day), not rupees.
    # Dividing a price difference by a return sd inflates Z by a factor of roughly
    # 1/sigma and makes it scale with the price level -- a Rs1000 stock scored ~60x
    # deeper than a Rs100 stock at the same percentage discount. Computed in log space
    # instead, which is both dimensionally consistent and the natural scale for the
    # [-2.20, -1.40] band.
    if sig > 0 and mu > 0 and c[-1] > 0:
        z = (math.log(float(c[-1])) - math.log(mu)) / sig
    else:
        z = float("nan")
    # Guard: the local-linear Kalman filter is a lagging state estimate and can sit far
    # behind price after a long trend. A "discount" that is really filter lag is not a
    # pullback, so cap the reference at the EMA20 the spec names as the fallback.
    ema20 = float(I.ema(pd.Series(c), 20).iloc[-1])
    if np.isfinite(kalman_price) and ema20 > 0:
        dev_kalman = abs(math.log(float(c[-1])) - math.log(kalman_price))
        dev_ema = abs(math.log(float(c[-1])) - math.log(ema20))
        if dev_kalman > 3 * dev_ema and dev_kalman > 0.10:
            z = (math.log(float(c[-1])) - math.log(ema20)) / sig if sig > 0 else float("nan")
            mu = ema20
    lo, hi = (CFG.z_lo_amber, CFG.z_hi_amber) if tier == "Amber" else (CFG.z_lo, CFG.z_hi)
    ok = np.isfinite(z) and lo <= z <= hi
    why = (f"Z_GARCH {z:+.2f} inside [{lo},{hi}]: discounted enough to have edge, but not "
           f"an extreme outlier") if ok else (
        f"Z_GARCH {z:+.2f} above {hi}: not discounted enough, no edge" if
        (np.isfinite(z) and z > hi) else
        f"Z_GARCH {z:+.2f} below {lo}: extreme outlier. In 2026 that usually means an "
        f"FPI-driven block dump or an oil-shock re-rating, with high continuation risk")
    return _res("S1_PULLBACK", ok, f"Z_GARCH {z:+.2f}", why, value=z,
                verdict=("POINT" if ok else "NO_POINT"), sigma_t=sig, mu_t=mu,
                mu_is_kalman=bool(mu == kalman_price),
                band=[lo, hi], garch_params=garch_params)


def s2_halflife(daily: pd.DataFrame) -> GateResult:
    """S2 -- OU half-life. Part IX, corrected per Part XXIV 24.2."""
    c = daily["close"].to_numpy(dtype=float)
    tau, lam, p, n = I.ou_halflife(c, CFG.ou_episode_lookback)
    ctx = I.ou_context(c, CFG.ou_episode_lookback)
    ok = (np.isfinite(lam) and lam < 0 and p < CFG.adf_p_max
          and np.isfinite(tau) and CFG.halflife_min <= tau <= CFG.halflife_max)
    if ok:
        why = (f"lambda {lam:+.4f} (p={p:.2g}), tau_half {tau:.2f} sessions inside "
               f"[{CFG.halflife_min},{CFG.halflife_max}]: the deficit closes fast enough "
               f"for a 1-8 session horizon")
    elif np.isfinite(tau) and tau > CFG.halflife_max:
        why = (f"tau_half {tau:.2f} > {CFG.halflife_max} sessions: reversion is real but "
               f"too slow for the horizon. If three other points score, the trade may "
               f"still enter with the Day-8 default time stop")
    elif not np.isfinite(tau) or (np.isfinite(lam) and lam >= 0):
        why = "lambda >= 0: the spread is not mean-reverting over the lookback"
    else:
        why = (f"tau_half {tau:.2f} < {CFG.halflife_min} session: implausibly fast, "
               f"usually a microstructure artifact")
    if ctx["episode_count"] < 3:
        why += (f". CAUTION: only {ctx['episode_count']} completed pullback episodes in "
                f"{CFG.ou_episode_lookback} sessions, so this estimate says little about "
                f"dip recovery (Part XXIV 24.2)")
    return _res("S2_HALFLIFE", ok, f"tau_half {tau:.2f}d", why, value=tau,
                verdict=("POINT" if ok else "NO_POINT"), lam=lam, p_value=p, n_obs=n,
                episode_count=ctx["episode_count"],
                pct_time_in_deficit=ctx["pct_time_in_deficit"])


def s3_volume(daily: pd.DataFrame) -> GateResult:
    """S3 -- volume exhaustion. Both conditions required. Part IX."""
    v = daily["volume"].to_numpy(dtype=float)
    c = daily["close"].to_numpy(dtype=float)
    o = daily["open"].to_numpy(dtype=float)
    vr = I.v_ratio(daily)
    sma = float(np.mean(v[-CFG.vol_sma:]))
    drying = bool(v[-1] < sma)
    ok = bool(np.isfinite(vr) and vr < CFG.v_ratio_max and drying)
    why = (f"V_ratio {vr:.2f} < {CFG.v_ratio_max} and volume {v[-1]:,.0f} < "
           f"SMA{CFG.vol_sma} {sma:,.0f}: participation is drying up, so the dip is "
           f"seller exhaustion rather than distribution") if ok else (
        f"V_ratio {vr:.2f} (limit {CFG.v_ratio_max}) / volume vs SMA{CFG.vol_sma} "
        f"{v[-1]:,.0f} vs {sma:,.0f}: heavy volume on down days in 2026 is FPI "
        f"distribution, not retail capitulation")
    return _res("S3_VOLUME", ok, f"V_ratio {vr:.2f}", why, value=vr,
                verdict=("POINT" if ok else "NO_POINT"), v_ratio=vr, vol_sma20=sma,
                volume_drying=drying)


def s4_avwap(daily: pd.DataFrame) -> GateResult:
    """S4 -- anchored VWAP confluence. Part IX."""
    off, method = I.anchor_t0(daily, win_primary=CFG.avwap_primary_win,
                              win_fallback=CFG.avwap_fallback_win,
                              donchian=CFG.avwap_donchian,
                              vol_mult=CFG.avwap_vol_expansion)
    av = I.avwap(daily, off)
    last = daily.iloc[-1]
    close = float(last["close"]); low = float(last["low"])
    atr14 = float(I.atr(daily, CFG.atr_window).iloc[-1])
    tol = 0.25 * atr14 / close if close > 0 else 0.0
    stale = abs(close - av) / av if av > 0 else float("inf")

    if stale > CFG.avwap_stale_max:
        return _res("S4_AVWAP", False, f"anchor stale ({stale:.2%})",
                    f"|close - AVWAP|/AVWAP = {stale:.2%} > {CFG.avwap_stale_max:.0%}: the "
                    f"anchor at t-40..t-5 no longer describes this dip, discard the "
                    f"candidate", value=stale, verdict="REJ_STALE_ANCHOR",
                    avwap=av, anchor_offset=off, anchor_method=method)

    tests = low <= av * (1 + tol)
    holds = close >= av
    ok = bool(tests and holds)
    why = (f"low {low:,.2f} tests AVWAP {av:,.2f} (tol {tol:.2%} = 0.25 x ATR14 "
           f"{atr14:,.2f}) and close {close:,.2f} holds above it: the dip found a buyer "
           f"at the anchored level") if ok else (
        f"low {low:,.2f} vs AVWAP {av:,.2f} (tol {tol:.2%}), close {close:,.2f}: "
        + ("price never tested the anchored level" if not tests else
           "tested but the close did not hold above it"))
    return _res("S4_AVWAP", ok, f"AVWAP {av:,.2f}", why, value=av,
                verdict=("POINT" if ok else "NO_POINT"), avwap=av, anchor_offset=off,
                anchor_method=method, atr14=atr14, tol=tol, staleness=stale)


# ====================================================================== ranker
def rank_score(rows: list[dict]) -> list[dict]:
    """Part X. Cross-sectional z-scores across the day's candidate pool.

    Score = 1/3 z(H) + 1/3 z(-tau_half) + 1/3 z(-Z_GARCH) + 1{4/4 confluence}
    Ties break on higher ADV.
    """
    if not rows:
        return []

    def zscore(vals: list[float]) -> list[float]:
        a = np.asarray([v for v in vals], dtype=float)
        a = np.where(np.isfinite(a), a, np.nan)
        if np.all(~np.isfinite(a)) or np.nanstd(a) < 1e-12:
            return [0.0] * len(vals)
        mu = float(np.nanmean(a)); sd = float(np.nanstd(a, ddof=0)) or 1e-12
        return [float((v - mu) / sd) if np.isfinite(v) else 0.0 for v in vals]

    zh = zscore([r.get("hurst", np.nan) for r in rows])
    zt = zscore([-r["tau_halflife"] if np.isfinite(r.get("tau_halflife", np.nan))
                 else np.nan for r in rows])
    zg = zscore([-r.get("z_garch", np.nan) for r in rows])

    out = []
    for r, a, b, cc in zip(rows, zh, zt, zg):
        bonus = 1.0 if r.get("confluence_pts", 0) >= 4 else 0.0
        s = (a + b + cc) / 3.0 + bonus
        rr = dict(r)
        rr["score"] = s
        rr["z_hurst"], rr["z_tau"], rr["z_garch_rank"] = a, b, cc
        out.append(rr)
    out.sort(key=lambda r: (-r["score"], -float(r.get("adv20_value", 0.0))))
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return out
