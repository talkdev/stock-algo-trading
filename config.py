"""
================================================================================
 STMR - SHORT-TERM EQUITY MEAN REVERSION (intraday, NSE cash, long-only)
 Central configuration. Every tunable in the system lives in this file.

 How the strategy works in one paragraph
 ---------------------------------------
 For each of the ~90 Nifty-100 names we compute, on 5-minute bars,
     z = (close - SMA20) / rolling_std20
 A "dip" is recorded when z falls to <= -DIP_Z (and RSI is soft) within the
 last DIP_LOOKBACK bars. When the bar closes GREEN and above the previous
 close while z is still below the mean (but recovering), we BUY. The stop is
 1.5*ATR(14) (clamped), the target is the 20-bar SMA (the mean itself), so we
 are paid when the price reverts. Positions are flattened by 15:20 IST.
--------------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Paths / storage
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
for _d in (DATA_DIR, LOGS_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# SQLite database - the single source of truth for ALL state (candles,
# positions, trades, cash, signals, context). Survives restarts.
DB_PATH = Path(os.environ.get("STMR_DB", str(DATA_DIR / "trading.db")))

UNIVERSE_FILE = BASE_DIR / "stock_universe.json"
INSTRUMENT_KEYS_FILE = DATA_DIR / "instrument_keys.json"   # optional manual override
TOKEN_FILE = DATA_DIR / "upstox_token.json"                # OAuth tokens (secret - gitignored)
FM_TOKEN_FILE = DATA_DIR / "upstox_fm_token.json"          # cached fm_token (secret)

# ---------------------------------------------------------------------------
# Upstox API (v2). Credentials via environment variables - never hardcode.
#   set UPSTOX_CLIENT_ID=xxxx   (and optionally UPSTOX_REDIRECT_URI)
# ---------------------------------------------------------------------------
UPSTOX_API_BASE = os.environ.get("UPSTOX_API_BASE", "https://api.upstox.com/v2")
UPSTOX_FM_BASE = os.environ.get("UPSTOX_FM_BASE", "https://fm-upstox.upstox.com/v1")
UPSTOX_CLIENT_ID = os.environ.get("UPSTOX_CLIENT_ID", "")
UPSTOX_REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:9876/upstox")
# NSE equity instrument master (Upstox API has no symbol->instrument_key
# lookup, so keys are sourced from NSE and cached in the DB - see
# upstox_client.py, limitation #5).
NSE_INSTRUMENT_CSV = "https://archives.nseindia.com/content/equities/indices/equityinstrument.csv"

# ---------------------------------------------------------------------------
# Market session (all times IST = Asia/Kolkata)
# ---------------------------------------------------------------------------
IST_TZ = "Asia/Kolkata"
CONTEXT_READY_AT = "08:45"   # pre-open: daily context is built from here
SESSION_OPEN_AT = "09:15"
SESSION_CLOSE_AT = "15:30"
POST_CLOSE_UNTIL = "16:30"   # final bar + daily bar persistence, EOD report
EOD_FLAT_AT = "15:20"        # force-close all positions from this time
LAST_ENTRY_AT = "14:50"      # no new entries after this time
BOUNDARY_LAG_SEC = 20        # wait this long after a 5-min boundary before the bar is "final"

# ---------------------------------------------------------------------------
# Strategy - short-term mean reversion on 5-minute bars
# ---------------------------------------------------------------------------
BAR_MINUTES = 5
WARMUP_BARS = 30              # minimum 5-min bars before any signal is allowed
SMOOTH_N = 20                 # SMA / rolling-std window for the z-score
DIP_Z = 1.2                   # a "dip" = z fell to <= -DIP_Z ...
DIP_LOOKBACK = 6              # ... within this many bars (30 minutes)
DIP_RSI = 35                  # and RSI(14) was <= this at some point in the dip
ENTRY_Z_MIN = -1.5            # at entry, z must be >= this (not still falling off a cliff)
ENTRY_Z_MAX = -0.4            # at entry, z must be <= this (still below the mean)
ENTRY_RSI_MAX = 55            # at entry, RSI must be <= this (not already fully recovered)
EXIT_Z = -0.10                # exit when a bar close reverts to z >= this (at/above the mean)
SL_ATR_MULT = 1.5             # stop = entry - 1.5 * ATR(14)
SL_MIN_PCT = 0.30             # stop at least this % below entry
SL_MAX_PCT = 1.50             # stop at most this % below entry
TRAIL_ATR_MULT = 1.0          # after crossing the mean: stop trails close - 1.0*ATR
RSI_N = 14
ATR_N = 14

# --- entry-quality refinements (all tunable; 0 / False disables) ---
DIP_MIN_DEPTH_ATR = 0.5       # dip must be at least 0.5x ATR(14) deep (real pullback, not drift)
USE_VWAP_FILTER = True        # only fade dips that are still below day-VWAP (classic intraday MR)
TIME_STOP_BARS = 8            # exit if still below the mean after 8 bars (40 min) of holding
MIN_ATR_PCT = 0.08            # skip names whose 5-min ATR is too quiet to cover costs

# ---------------------------------------------------------------------------
# Selection filters
# ---------------------------------------------------------------------------
TREND_FILTER = True           # long only when daily close > SMA20 AND SMA20 > SMA50
MIN_DAILY_AVG_VOLUME = 50_000 # 20-day average daily volume floor (liquidity)

# ---------------------------------------------------------------------------
# Portfolio / risk
# ---------------------------------------------------------------------------
MAX_POSITIONS = 5             # max concurrent open positions
RISK_PER_TRADE_PCT = 0.5      # % of equity risked between entry and stop
MAX_POS_VALUE_PCT = 20.0      # max single-position exposure, % of equity
MIN_QTY = 1                   # NSE cash segment trades in single shares
PAPER_START_EQUITY = 1_000_000.0
REAL_CAPITAL = float(os.environ.get("STMR_REAL_CAPITAL", "100000"))  # sizing base in real mode

# ---------------------------------------------------------------------------
# Paper-trade economics (intraday MIS charges - approximations, see README)
# ---------------------------------------------------------------------------
BROKERAGE_FLAT = 20.0         # flat per order (typical broker)
BROKERAGE_CAP_PCT = 0.03      # ... capped at 0.03% of value
STT_BPS = 2.5                 # 0.025% on BOTH buy and sell (intraday equity MIS)
EXCHANGE_BPS = 1.9            # NSE transaction charge
SEBI_BPS = 0.01               # SEBI fees
GST_PCT = 18.0                # GST on (brokerage + exchange + sebi)
STAMP_BPS_BUY = 1.5           # stamp duty on buys (conservative for MIS)
SLIPPAGE_BPS = 5.0            # per side

# ---------------------------------------------------------------------------
# Engine loop / API behaviour
# ---------------------------------------------------------------------------
POLL_FAST_SEC = 8             # mid-bar poll cadence for positions + dip watchlist
API_MIN_INTERVAL_SEC = 0.25   # throttle between Upstox REST calls (rate-limit safety)
API_TIMEOUT_SEC = 20
API_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Tuned-parameter overrides (written by tune.py, auto-loaded by engine/backtest)
# ---------------------------------------------------------------------------
PARAMS_FILE = DATA_DIR / "best_params.json"

# Every parameter the strategy engine reacts to (used by make_cfg / tune.py)
TUNABLES = [
    "WARMUP_BARS", "SMOOTH_N",
    "DIP_Z", "DIP_LOOKBACK", "DIP_RSI", "DIP_MIN_DEPTH_ATR",
    "ENTRY_Z_MIN", "ENTRY_Z_MAX", "ENTRY_RSI_MAX", "USE_VWAP_FILTER",
    "MIN_ATR_PCT", "TIME_STOP_BARS",
    "EXIT_Z", "SL_ATR_MULT", "SL_MIN_PCT", "SL_MAX_PCT", "TRAIL_ATR_MULT",
    "RSI_N", "ATR_N",
    "TREND_FILTER", "MIN_DAILY_AVG_VOLUME",
    "MAX_POSITIONS", "RISK_PER_TRADE_PCT", "MAX_POS_VALUE_PCT", "MIN_QTY",
    "LAST_ENTRY_AT", "EOD_FLAT_AT", "BAR_MINUTES",
]


def make_cfg(overrides: dict | None = None):
    """A plain namespace holding every tunable. `overrides` (e.g. from
    best_params.json or the tuner) replaces defaults. Unknown keys are ignored."""
    from types import SimpleNamespace
    ns = SimpleNamespace()
    for name in TUNABLES:
        setattr(ns, name, globals()[name])
    for k, v in (overrides or {}).items():
        if k in TUNABLES:
            setattr(ns, k, v)
    return ns


def load_params() -> dict:
    """Tuned parameter overrides from PARAMS_FILE (empty dict if absent).
    Accepts both a plain {param: value} mapping and the tune.py payload
    (which nests the overrides under 'overrides')."""
    if PARAMS_FILE.exists():
        try:
            d = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if "overrides" in d and isinstance(d["overrides"], dict):
                    return d["overrides"]
                return d
        except Exception as e:
            print(f"[warn] could not read {PARAMS_FILE}: {e}")
    return {}


def load_universe() -> list[str]:
    """Load the trading universe (Nifty-100 names) from stock_universe.json."""
    raw = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("symbols", [])
    return [str(s).strip().upper() for s in raw if str(s).strip()]
