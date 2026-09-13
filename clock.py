"""IST clock and session-phase resolution (spec Part III)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def hhmm(dt: datetime) -> time:
    return dt.time()


# ---------------------------------------------------------------- phases
PHASE_PRE_MARKET = "PRE_MARKET"        # before 09:00 -- may run prior session's EOD scan
PHASE_PRE_OPEN_WAIT = "PRE_OPEN_WAIT"  # 09:00-09:08
PHASE_PRE_OPEN = "PRE_OPEN"            # 09:08-09:15 -- gap-cancel check
PHASE_PLACE = "PLACE"                  # 09:15-09:16 -- arm GFD orders
PHASE_MANAGE = "MANAGE"                # 09:16-15:20 -- run the exit ladder
PHASE_CANCEL = "CANCEL"                # 15:20-15:35 -- hard-cancel unfilled GFD
PHASE_POST_CLOSE = "POST_CLOSE"        # 15:35-16:30 -- EOD scan on the final bar
PHASE_IDLE = "IDLE"                    # evening/night


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def resolve_phase(dt: datetime, cfg=None) -> str:
    """Map a wall-clock instant to the pipeline phase. Deterministic and side-effect free."""
    t = hhmm(dt)
    if cfg is None:
        from .config import CFG as cfg  # noqa: N814 - default to the live config
    if t < _t(cfg.session_preopen):
        # Before 09:08. Between midnight and 09:00 the previous session's EOD scan
        # may still be owed (e.g. the box was off at 15:35 yesterday).
        return PHASE_PRE_MARKET if t < _t("09:00") else PHASE_PRE_OPEN_WAIT
    if t < _t(cfg.session_place):
        return PHASE_PRE_OPEN
    if t < _t("09:16"):
        return PHASE_PLACE
    if t < _t(cfg.session_cancel):
        return PHASE_MANAGE
    if t < _t(cfg.session_eod_scan):
        return PHASE_CANCEL
    if t < _t("16:30"):
        return PHASE_POST_CLOSE
    return PHASE_IDLE


def session_date_for(dt: datetime, cfg=None) -> date:
    """Which trading session an action belongs to.

    Anything before 09:00 belongs to the *previous* session's post-close work, so a
    box that was off at 15:35 yesterday still completes yesterday's scan on restart.
    """
    if hhmm(dt) < _t("09:00"):
        return prev_business_day(dt.date())
    return dt.date()


def prev_business_day(d: date) -> date:
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def seconds_until(dt: datetime, target: str) -> float:
    h, m = target.split(":")
    tgt = dt.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return (tgt - dt).total_seconds()


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")
