"""
Calibration tests for the quantitative core.

These are not decorative: each one pins down a number the spec depends on, and two of
them exist because the first implementation was measurably wrong. Run with:

    python -m pytest tests/ -v      (or)      python tests/test_indicators.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

def _bootstrap_path() -> str:
    """Directory to import `jfou` from, found by walking up from __file__.

    A hard-coded parent-of-parent only happens to be right for one layout; this works
    from tests/, from the project root, from a symlinked checkout and from any working
    directory on either OS.
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "jfou").is_dir() or (cand / "main.py").is_file():
            return str(cand)
    return str(here.parent)


_ROOT = _bootstrap_path()
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from jfou import indicators as I  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# ---------------------------------------------------------------- Hurst
def test_hurst_null_is_half():
    """A pure random walk must NOT clear the H > 0.60 gate.

    Naive R/S over 100 points returns H > 0.60 for pure noise ~68% of the time, which
    is why hurst() uses variance scaling instead.
    """
    section("Hurst: null distribution of a random walk (window=100)")
    for est_name, est in [("naive R/S", I.hurst_rs_naive), ("agg-var (used)", I.hurst_aggvar)]:
        hs = []
        for seed in range(200):
            rng = np.random.default_rng(seed)
            rw = 100 + np.cumsum(rng.normal(0, 0.5, 300))
            hs.append(est(rw, 100))
        hs = np.array([h for h in hs if np.isfinite(h)])
        leak = float(np.mean(hs > 0.60))
        print(f"    {est_name:16s} mean={hs.mean():.3f} median={np.median(hs):.3f} "
              f"P(H>0.60)={leak:.1%}")
        if est is I.hurst_aggvar:
            check("agg-var null mean within 0.05 of 0.50", abs(hs.mean() - 0.5) < 0.05,
                  f"mean={hs.mean():.3f}")
            check("agg-var false-positive rate < 20%", leak < 0.20, f"leak={leak:.1%}")
        else:
            check("naive R/S is demonstrably biased (documents the deviation)",
                  leak > 0.40, f"leak={leak:.1%}")


def test_hurst_discriminates():
    """Hurst measures autocorrelation of INCREMENTS, not drift.

    A random walk with constant drift still has iid increments and H = 0.5 exactly --
    an earlier version of this test used a drifting series and concluded the estimator
    had no power. Persistence requires momentum: r_t = phi * r_{t-1} + eps with phi > 0.
    """
    section("Hurst: separates momentum / random walk / mean reversion")

    def momentum(n, phi, seed):
        rng = np.random.default_rng(seed)
        r = np.zeros(n)
        for i in range(1, n):
            r[i] = phi * r[i - 1] + rng.normal(0, 0.004)
        return 100 * np.exp(np.cumsum(r))

    def meanrev(n, seed):
        rng = np.random.default_rng(seed)
        x = np.zeros(n); x[0] = 100
        for i in range(1, n):
            x[i] = x[i - 1] + 0.35 * (100 - x[i - 1]) + rng.normal(0, 0.4)
        return x

    N = 400
    hp = np.nanmean([I.hurst(momentum(N, 0.5, s), 200) for s in range(60)])
    hm = np.nanmean([I.hurst(meanrev(N, s), 200) for s in range(60)])
    rng = np.random.default_rng(11)
    hr = np.nanmean([I.hurst(100 + np.cumsum(np.random.default_rng(s).normal(0, 0.5, N)), 200)
                     for s in range(60)])
    print(f"    momentum(phi=0.5)={hp:.3f}  mean-revert={hm:.3f}  random-walk={hr:.3f}")
    check("momentum H > 0.55", hp > 0.55, f"{hp:.3f}")
    check("mean-reverting H < 0.50", hm < 0.50, f"{hm:.3f}")
    check("random walk 0.42 <= H <= 0.55", 0.42 <= hr <= 0.55, f"{hr:.3f}")
    check("estimator orders momentum > random walk > mean-reverting", hp > hr > hm,
          f"{hp:.3f} > {hr:.3f} > {hm:.3f}")

    noiseless = 100 * np.exp(np.cumsum(np.full(N, 0.004)))
    check("noiseless deterministic path does not return NaN",
          np.isfinite(I.hurst_aggvar(noiseless, 100)), f"{I.hurst_aggvar(noiseless,100)}")


# ---------------------------------------------------------------- Kalman
def test_kalman_recovers_slope():
    section("Kalman: recovers a known velocity and rejects a flat series")
    rng = np.random.default_rng(3)
    y = 100 + np.arange(200) * 0.5 + rng.normal(0, 0.3, 200)
    ks = I.kalman_local_linear(y)
    print(f"    true=0.500  est={ks.velocity:.4f}  se={ks.vel_std:.4f}  slope_ok={ks.slope_ok}")
    check("velocity within 10% of true slope", abs(ks.velocity - 0.5) < 0.05, f"{ks.velocity:.4f}")
    check("95% lower bound positive on a real trend", ks.slope_ok)

    flat = 100 + rng.normal(0, 0.3, 200)
    kf = I.kalman_local_linear(flat)
    print(f"    flat series velocity={kf.velocity:+.4f} slope_ok={kf.slope_ok}")
    check("flat series rejected by the 95% bound", not kf.slope_ok)

    q, r = I.kalman_qr_em(y, 60)
    check("EM returns finite positive Q and R",
          np.isfinite(q) and np.isfinite(r) and q > 0 and r > 0, f"Q={q:.2e} R={r:.2e}")


# ---------------------------------------------------------------- OU
def test_ar1_estimator_unbiased():
    """The AR(1) estimator itself must recover the injected lambda."""
    section("OU: AR(1) estimator recovers injected lambda")
    for lam in (-0.20, -0.30, -0.50):
        rng = np.random.default_rng(5)
        x = np.zeros(6000)
        for i in range(1, 6000):
            x[i] = (1 + lam) * x[i - 1] + rng.normal(0, 0.02)
        got, p, n = I._ar1(x[:-1], np.diff(x))
        tau = -math.log(2) / math.log(1 + lam)
        print(f"    injected lambda={lam:+.2f} -> fitted={got:+.4f} p={p:.2g} "
              f"(tau_true={tau:.2f}d)")
        check(f"AR(1) recovers lambda={lam:+.2f} within 0.02", abs(got - lam) < 0.02,
              f"fitted={got:+.4f}")


def test_ou_uses_the_unbiased_sample():
    """Every episode-selection rule biases lambda negative; the full spread does not.

    This is why ou_halflife fits the full spread. Fitting on x<0 points alone returns
    lambda=-0.39 when the truth is -0.20, collapsing tau from 3.11d to 1.41d -- which
    would make every candidate look faster-reverting than it is, and since positions
    are sized off tau, that bias becomes real money.
    """
    section("OU: selection-rule bias study (drives the deviation from the spec)")
    def ar1(L, D):
        X = np.column_stack([L, np.ones(len(L))])
        b, *_ = np.linalg.lstsq(X, D, rcond=None)
        return b[0]

    for lam in (-0.20, -0.30):
        res = {k: [] for k in ("full_spread", "fixed_window", "x_lt_0", "full_recovery")}
        for seed in range(40):
            rng = np.random.default_rng(seed)
            x = np.zeros(3000)
            for i in range(1, 3000):
                x[i] = (1 + lam) * x[i - 1] + rng.normal(0, 0.02)
            res["full_spread"].append(ar1(x[:-1], np.diff(x)))
            # x < 0 only
            neg = x[x < 0]
            if len(neg) > 20:
                res["x_lt_0"].append(ar1(neg[:-1], np.diff(neg)))
            # fixed 10-bar window after each downward crossing
            L, D = [], []
            for a, b in [(i - 1, min(i + 10, 2999)) for i in range(1, 3000)
                         if x[i] < 0 <= x[i - 1]]:
                for t in range(a + 1, b + 1):
                    L.append(x[t - 1]); D.append(x[t] - x[t - 1])
            if len(L) >= 20:
                res["fixed_window"].append(ar1(np.array(L), np.array(D)))
            # full down-and-back recovery path
            L2, D2 = [], []
            for a, b in I.find_pullback_episodes(x):
                for t in range(a + 1, b + 1):
                    L2.append(x[t - 1]); D2.append(x[t] - x[t - 1])
            if len(L2) >= 20:
                res["full_recovery"].append(ar1(np.array(L2), np.array(D2)))

        print(f"    true lambda={lam:+.2f}")
        for k in ("full_spread", "fixed_window", "x_lt_0", "full_recovery"):
            m = float(np.mean(res[k]))
            print(f"      {k:14s} mean={m:+.4f}  bias={m-lam:+.4f}")
        check(f"full_spread is unbiased for lambda={lam:+.2f}",
              abs(np.mean(res["full_spread"]) - lam) < 0.02,
              f"{np.mean(res['full_spread']):+.4f}")
        check(f"episode rules are demonstrably biased for lambda={lam:+.2f}",
              abs(np.mean(res["full_recovery"]) - lam) > 0.08,
              f"full_recovery={np.mean(res['full_recovery']):+.4f}")


def test_ou_halflife_bounds():
    section("OU: half-life formula matches the spec table")
    for lam, exp_tau in [(-0.20, 3.11), (-0.30, 1.94), (-0.50, 1.00)]:
        theta = -math.log(1 + lam)
        tau = math.log(2) / theta
        check(f"lambda={lam:+.2f} -> tau={exp_tau}", abs(tau - exp_tau) < 0.01, f"{tau:.2f}")


def test_ou_on_synthetic_ou_price():
    """End-to-end: price = exp(drift + OU deviation). Recovered tau must be sane."""
    section("OU: end-to-end on a synthetic OU price series")
    rng = np.random.default_rng(21)
    n = 600
    phi = 0.85                                     # true half-life = ln2/(-ln0.85) = 4.27d
    d = np.zeros(n)
    for i in range(1, n):
        d[i] = phi * d[i - 1] + rng.normal(0, 0.010)
    price = 100 * np.exp(np.linspace(0, 0.30, n) + d)
    tau, lam, p, nn = I.ou_halflife(price, lookback=250)
    ctx = I.ou_context(price, lookback=250)
    true_tau = math.log(2) / -math.log(phi)
    print(f"    true tau={true_tau:.2f}d  recovered tau={tau:.2f}d lambda={lam:+.4f} "
          f"p={p:.3g} n={nn}")
    print(f"    context: {ctx['episode_count']} episodes, median len "
          f"{ctx['episode_median_len']}, {ctx['pct_time_in_deficit']:.0%} in deficit")
    check("recovered tau is finite and positive", np.isfinite(tau) and tau > 0, f"{tau:.2f}")
    check("recovered lambda is negative", lam < 0, f"{lam:+.4f}")
    check("recovered tau within 60% of true tau", abs(tau - true_tau) / true_tau < 0.60,
          f"recovered={tau:.2f}d true={true_tau:.2f}d")
    check("ou_context found pullback episodes", ctx["episode_count"] >= 3,
          f"{ctx['episode_count']}")


# ---------------------------------------------------------------- stationarity
def test_adf():
    section("ADF: stationary passes, random walk rejected")
    rng = np.random.default_rng(9)
    st = np.zeros(500)
    for i in range(1, 500):
        st[i] = 0.7 * st[i - 1] + rng.normal(0, 0.05)
    rw = np.cumsum(rng.normal(0, 0.05, 500))
    p_st, p_rw = I.adf_pvalue(st), I.adf_pvalue(rw)
    print(f"    AR(0.7) p={p_st:.4f}   random walk p={p_rw:.4f}")
    check("stationary series p < 0.05", p_st < 0.05, f"{p_st:.4f}")
    check("random walk p >= 0.05", p_rw >= 0.05, f"{p_rw:.4f}")


# ---------------------------------------------------------------- jump
def test_bns():
    section("BNS jump statistic")
    rng = np.random.default_rng(13)
    quiet = rng.normal(0, 0.002, 75)
    jump = np.concatenate([rng.normal(0, 0.002, 60), [0.06], rng.normal(0, 0.002, 14)])
    zq, _ = I.bns_jump_z(quiet)
    zj, _ = I.bns_jump_z(jump)
    print(f"    quiet Z={zq:.3f}   with 6% jump Z={zj:.3f}")
    check("quiet series Z < 2.15", zq < 2.15, f"{zq:.3f}")
    check("jump series Z >= 2.15", zj >= 2.15, f"{zj:.3f}")

    # Operational property that matters: a U-shaped intraday vol profile with NO jump
    # must not be flagged, and a real jump must be flagged either way.
    u = 0.001 + 0.010 * (np.linspace(-1, 1, 75) ** 4)          # strong open/close bulge
    fp = 0
    for seed in range(40):
        r = np.random.default_rng(seed).normal(0, 1, 75) * u
        z_raw, _ = I.bns_jump_z(r)
        z_adj, _ = I.bns_jump_z(r, u)
        if z_adj >= 2.15:
            fp += 1
    print(f"    U-shaped no-jump series: false-positive rate after de-seasonalisation "
          f"= {fp}/40")
    check("de-seasonalised statistic does not flag a seasonal pattern", fp <= 4,
          f"{fp}/40 false positives")

    r_jump = np.random.default_rng(99).normal(0, 1, 75) * u
    r_jump[37] = 0.05
    z_adj_j, _ = I.bns_jump_z(r_jump, u)
    print(f"    U-shaped series with a 5% jump: adjusted Z={z_adj_j:.3f}")
    check("a real jump is still flagged after de-seasonalisation", z_adj_j >= 2.15,
          f"{z_adj_j:.3f}")


def test_seasonal_factors():
    section("Seasonal factors s_m (75 five-minute slots per NSE session)")
    # NSE 09:15-15:30 = 375 min = 75 bars, last bar covering 15:25-15:30.
    days = pd.bdate_range("2026-01-05", periods=40)
    slots = pd.date_range("09:15", periods=75, freq="5min").time
    idx = pd.DatetimeIndex([pd.Timestamp(d).replace(hour=t.hour, minute=t.minute)
                            for d in days for t in slots])
    rng = np.random.default_rng(17)
    n = len(idx)
    slot = np.arange(n) % 75
    scale = np.where(slot < 6, 4.0, 1.0)            # exaggerated open volatility
    close = 100 * np.exp(np.cumsum(rng.normal(0, 1, n) * 0.001 * scale))
    df = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999,
                       "close": close, "volume": 1e5}, index=idx)
    s = I.seasonal_factors(df, sessions=40)
    check("returns exactly 75 slots", len(s) == 75, f"got {len(s)}")
    check("every slot factor is positive and finite",
          bool(np.all(np.isfinite(s.values)) and np.all(s.values > 0)))
    # Spec 7.1 line 255: "s_m = rolling 60-session average |return| for slot m".
    # So s_m is on the raw |return| scale, NOT normalised to median 1. The synthetic
    # series above injects a 4x volatility multiplier on the first 6 slots, so the
    # recovered open/midday ratio must come back near 4.
    ratio = float(s.iloc[:6].mean()) / float(s.iloc[30:45].mean())
    print(f"    open s_m={s.iloc[:6].mean():.5f}  midday={s.iloc[30:45].mean():.5f}  "
          f"ratio={ratio:.2f} (injected 4.00)")
    check("recovers the injected 4x open/midday volatility ratio", 2.5 < ratio < 6.0,
          f"ratio={ratio:.2f}")
    check("s_m is on the |return| scale (not normalised)",
          1e-5 < float(s.median()) < 1e-2, f"median={s.median():.6f}")

    # r*_m = r_m / s_m must be scale-free enough for the BNS statistic to be unaffected
    r = np.random.default_rng(31).normal(0, 1, 75) * 0.002
    z_scaled, _ = I.bns_jump_z(r, s.values)
    z_times1000, _ = I.bns_jump_z(r * 1000, s.values * 1000)
    check("BNS Z is scale-invariant under joint rescaling",
          abs(z_scaled - z_times1000) < 1e-6, f"{z_scaled:.6f} vs {z_times1000:.6f}")


# ---------------------------------------------------------------- payoff / kelly
def test_payoff_tables():
    section("Payoff b_net and Kelly reproduce the spec Part 11 tables")
    for rpct, exp in [(0.015, 1.400), (0.020, 1.488), (0.025, 1.540), (0.030, 1.575)]:
        b = I.b_net(rpct, 0.002243, 0.0030)
        check(f"b_net at R={rpct*100:.2f}% = {exp}", abs(b - exp) < 0.002, f"{b:.3f}")
    check("Kelly break-even at b=1.488 is 0.4020",
          abs(1 / (1 + 1.488) - 0.4020) < 5e-4, f"{1/(1+1.488):.4f}")
    for p, exp in [(0.45, 0.0402), (0.55, 0.1238), (0.60, 0.1656)]:
        f = I.kelly_fraction(p, 1.488, 0.5)
        check(f"half-Kelly at p={p} = {exp}", abs(f - exp) < 2e-4, f"{f:.4f}")
    check("Kelly never negative below break-even",
          I.kelly_fraction(0.30, 1.488, 0.5) == 0.0)


# ---------------------------------------------------------------- misc
def test_avwap_and_anchor():
    section("AVWAP anchor selection")
    n = 120
    rng = np.random.default_rng(23)
    close = 100 + np.cumsum(rng.normal(0.05, 0.6, n))
    df = pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.integers(1e5, 2e5, n).astype(float),
    }, index=pd.date_range("2026-01-01", periods=n))
    off, meth = I.anchor_t0(df)
    av = I.avwap(df, off)
    print(f"    offset={off} method={meth} AVWAP={av:.2f} last_close={close[-1]:.2f}")
    check("anchor within the fallback window", 0 <= off <= 40, f"off={off}")
    check("method is one of the documented strategies",
          meth in ("donchian_breakout_volume", "pivot_low_fallback",
                   "fallback_short_history", "fallback_edge"), meth)
    check("AVWAP inside the observed price range",
          float(df["low"].min()) <= av <= float(df["high"].max()), f"{av:.2f}")

    # a clean Donchian breakout with volume must be picked over the pivot fallback
    df2 = df.copy()
    df2["high"] = 100.0
    df2.loc[df2.index[-20], "high"] = 130.0
    df2["volume"] = 1e5
    df2.loc[df2.index[-20], "volume"] = 9e5
    off2, meth2 = I.anchor_t0(df2)
    print(f"    forced breakout: offset={off2} method={meth2}")
    check("Donchian breakout with volume expansion is detected",
          meth2 == "donchian_breakout_volume", meth2)


def test_vratio():
    section("Volume exhaustion V_ratio")
    n = 60
    base = pd.DataFrame({
        "open": np.full(n, 100.0), "high": np.full(n, 101.0),
        "low": np.full(n, 99.0), "close": np.full(n, 100.0),
        "volume": np.full(n, 2e5),
    }, index=pd.date_range("2026-01-01", periods=n))
    dry = base.copy(); dry.loc[dry.index[-3:], "volume"] = 4e4
    heavy = base.copy(); heavy.loc[heavy.index[-3:], "volume"] = 6e5
    r_dry, r_heavy = I.v_ratio(dry), I.v_ratio(heavy)
    print(f"    drying volume V_ratio={r_dry:.3f}   heavy volume V_ratio={r_heavy:.3f}")
    check("drying volume scores below 0.60", r_dry < 0.60, f"{r_dry:.3f}")
    check("heavy volume scores above 0.60", r_heavy > 0.60, f"{r_heavy:.3f}")


def test_student_t_vs_normal():
    section("Student-t probability is more conservative than the normal CDF")
    from scipy import stats as st
    for z in (0.5, 1.0, 1.5):
        pt = I.student_t_prob(z, 1.0, nu=4)
        pn = float(st.norm.cdf(z))
        print(f"    z={z}  t(nu=4)={pt:.4f}  normal={pn:.4f}")
        check(f"t-CDF <= normal CDF at z={z}", pt <= pn + 1e-9, f"{pt:.4f} vs {pn:.4f}")


def main() -> int:
    print("=" * 78)
    print("JF-OU indicator calibration suite")
    print("=" * 78)
    for fn in [test_hurst_null_is_half, test_hurst_discriminates, test_kalman_recovers_slope,
               test_ar1_estimator_unbiased, test_ou_uses_the_unbiased_sample,
               test_ou_halflife_bounds, test_ou_on_synthetic_ou_price, test_adf, test_bns,
               test_seasonal_factors, test_payoff_tables, test_avwap_and_anchor,
               test_vratio, test_student_t_vs_normal]:
        fn()
    print("\n" + "=" * 78)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("RESULT: all indicator checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
