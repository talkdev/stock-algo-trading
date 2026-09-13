"""
JF-OU / NSE 2026 -- Jump-Filtered Ornstein-Uhlenbeck Trend-Conditioned Mean Reversion
======================================================================================
Implementation of the specification in JF_OU_NSE_2026.md (Parts I-XXIII).

Single source of truth for every tunable parameter. Section references in
comments point at the spec's Master Parameter Table (Part XVIII).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

LAKH = 100_000
CRORE = 10_000_000

BASE_DIR = Path(os.environ.get("JFOU_HOME", Path(__file__).resolve().parent.parent))
DB_PATH = Path(os.environ.get("JFOU_DB", BASE_DIR / "data" / "jfou.sqlite3"))
LOG_DIR = BASE_DIR / "logs"


@dataclass(frozen=True)
class Config:
    # ------------------------------------------------------------------ runtime
    paper_trade: bool = True              # DEFAULT: paper mode (never touches the broker)
    loop: bool = False                    # True = keep running as a scheduler
    poll_seconds: int = 60
    timezone: str = "Asia/Kolkata"

    # ------------------------------------------------------------------ universe
    universe_file: str = "universe_nifty100.json"
    # Part 11.5: derived liquidity floor. 2 lots @ Rs15L = Rs30L notional;
    # at a 1.5% participation cap the ADV must be >= Rs20 Cr or no legal trade exists.
    adv_value_floor_cr: float = 20.0
    participation_cap: float = 0.015       # 1.5% of 20-day ADV (share count)
    participation_cap_relaxed: float = 0.05
    circuit_lookback_sessions: int = 30    # G1.3

    # ------------------------------------------------------------------ G0 macro (Part IV)
    # India VIX tiering, calibrated against observed 2026 readings.
    vix_green_max: float = 13.0
    vix_amber_max: float = 16.0
    vix_crisis: float = 21.0
    vix_percentile_window: int = 250
    vix_percentile_block: float = 0.75     # block if VIX in top quartile
    vix_intraday_expand_block: float = 0.10
    index_ema: int = 50
    breadth_min: float = 0.45              # >=45% of universe above own 200-DMA
    breadth_ema: int = 200
    max_names_green: int = 5
    max_names_amber: int = 2

    # 2026 macro-factor overlay (Part 4.3)
    brent_exclude_above: float = 95.0
    usdinr_move_window: int = 20
    usdinr_move_block: float = 0.01
    gsec10y_exclude_above: float = 7.0
    fpi_seller_streak_block: int = 10

    # ------------------------------------------------------------------ G1 events (Part V)
    earnings_min_sessions_ahead: int = 10
    earnings_recent_ok_sessions: int = 3

    # ------------------------------------------------------------------ G2 trend (Part VI)
    hurst_window: int = 100
    hurst_min: float = 0.60
    kalman_em_window: int = 60
    kalman_q_over_r: float = 1e-3          # used only if EM fails to converge
    kalman_slope_confidence: float = 0.95
    ols_window: int = 60
    ols_tstat_min: float = 2.5
    ema_trend: int = 50

    # ------------------------------------------------------------------ G3 jump (Part VII)
    seasonal_window_sessions: int = 60
    bars_per_session: int = 75             # 09:15-15:30 = 375 min / 5
    bns_z_max: float = 2.15
    overnight_gap_sigma_max: float = 1.5

    # ------------------------------------------------------------------ G4 stationarity (Part VIII)
    adf_p_max: float = 0.05

    # ------------------------------------------------------------------ S1-S4 (Part IX)
    garch_window: int = 60
    z_lo: float = -2.20
    z_hi: float = -1.40
    z_lo_amber: float = -2.00
    z_hi_amber: float = -1.50
    ou_episode_lookback: int = 250
    halflife_min: float = 1.0
    halflife_max: float = 3.5
    v_ratio_max: float = 0.60
    vol_sma: int = 20
    avwap_primary_win: tuple = (40, 5)
    avwap_fallback_win: tuple = (30, 5)
    avwap_donchian: int = 20
    avwap_vol_expansion: float = 1.5
    avwap_stale_max: float = 0.04
    confluence_min_points: int = 3         # >= 3 of 4

    # ------------------------------------------------------------------ sizing (Part XI)
    payoff_gross: float = 1.75             # 0.5(1.0R) + 0.5(2.5R)
    slippage_roundtrip: float = 0.0030
    cost_cash_roundtrip: float = 0.002243  # Part 15.1, exact 2026 rates
    cost_futures_roundtrip: float = 0.000581  # Part 15.2
    prob_nu: int = 4                       # Student-t degrees of freedom
    prob_floor: float = 0.43
    kelly_fraction: float = 0.5            # half-Kelly
    margin_rate: float = 0.15              # SPAN + ELM assumption
    risk_cap_pct: float = 0.015            # 1.5% of equity per trade
    min_lots: int = 2                      # even-lot scale-out requirement

    # ------------------------------------------------------------------ execution (Part XII)
    entry_trigger_pct: float = 0.0005      # High_t + 0.05%
    limit_buffer_pct: float = 0.0020       # +0.20% slippage buffer
    gap_cancel_pct: float = 0.012          # > High_t + 1.2%
    gap_cancel_atr_mult: float = 0.75
    stop_atr_mult: float = 0.75            # NOT 0.25 -- see Part 12.3
    stop_avwap_mult: float = 0.992
    max_r_atr_mult: float = 1.25
    atr_window: int = 14

    # ------------------------------------------------------------------ exits (Part XIII)
    target1_ema: int = 20
    target2_r_mult: float = 2.5
    trail_ema: int = 9
    time_stop_mult: float = 2.5
    time_stop_cap_days: int = 8
    breakeven_buffer_pct: float = 0.0005

    # ------------------------------------------------------------------ portfolio (Part XIV)
    max_per_sector: int = 1
    sector_exposure_max: float = 0.25
    heat_max_green: float = 0.075
    heat_max_amber: float = 0.030

    # ------------------------------------------------------------------ portfolio book
    paper_equity: float = 50 * LAKH        # simulated book for paper mode
    paper_unencumbered_cash: float = 50 * LAKH

    # ------------------------------------------------------------------ API
    upstox_base: str = "https://api.upstox.com"
    upstox_api_version: str = "2.0"
    instruments_url: str = "https://assets.upstox.com/market-quote/instruments.json"
    rate_limit_per_sec: float = 5.0        # standard APIs: 50/s but we stay conservative
    rate_limit_per_30min: int = 1900       # hard ceiling is 2000; keep headroom
    request_timeout: int = 20
    max_retries: int = 4

    # ------------------------------------------------------------------ session clock (IST)
    session_eod_scan: str = "15:35"
    session_preopen: str = "09:08"
    session_place: str = "09:15"
    session_cancel: str = "15:20"
    market_open: str = "09:15"
    market_close: str = "15:30"

    def as_dict(self) -> dict:
        return asdict(self)


CFG = Config()
