"""
mkttime.py - IST market-time helpers (Asia/Kolkata).

Convention: every bar_time / date string stored in the DB is IST and has the
format  'YYYY-MM-DD HH:MM:SS' (bar start) or 'YYYY-MM-DD'. Because every value
uses the same zone and format, plain lexicographic comparison is chronological.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo(config.IST_TZ)


def ist_now() -> datetime:
    return datetime.now(IST)


def parse_t(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def fmt_t(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def epoch_to_ist_str(epoch: int) -> str:
    return fmt_t(datetime.fromtimestamp(int(epoch), IST))


def ist_str_to_epoch(s: str) -> int:
    return int(parse_t(s).timestamp())


def today_str(now: datetime | None = None) -> str:
    return (now or ist_now()).strftime("%Y-%m-%d")


def hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def bar_times(date_str: str) -> list[datetime]:
    """All 75 five-minute bar start times of a trading day (09:15 .. 15:25)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    t = d.replace(hour=9, minute=15, second=0, microsecond=0)
    end = d.replace(hour=15, minute=25, second=0, microsecond=0)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=config.BAR_MINUTES)
    return out


def day_start_epoch(date_str: str) -> int:
    return ist_str_to_epoch(date_str + " 09:15:00")


def day_end_epoch(date_str: str) -> int:
    return ist_str_to_epoch(date_str + " 15:30:00")


def session_phase(now: datetime | None = None) -> str:
    """
    Phase of the trading session for the given (IST) time:
      PRE_OPEN   08:45 - 09:14   build daily context
      OPEN       09:15 - 15:29   scan / select / enter / exit
      POST_CLOSE 15:30 - 16:29   persist final data, EOD report
      CLOSED     everything else - NO data capture, NO writes (DB stays clean)
    """
    now = now or ist_now()
    if now.weekday() >= 5:
        return "CLOSED"
    h = hm(now)
    if config.CONTEXT_READY_AT <= h < config.SESSION_OPEN_AT:
        return "PRE_OPEN"
    if config.SESSION_OPEN_AT <= h < config.SESSION_CLOSE_AT:
        return "OPEN"
    if config.SESSION_CLOSE_AT <= h < config.POST_CLOSE_UNTIL:
        return "POST_CLOSE"
    return "CLOSED"


def current_bar_start(now: datetime) -> datetime:
    """Start of the 5-minute bar that is in progress at `now`."""
    d = now.replace(second=0, microsecond=0)
    m = d.minute - (d.minute % config.BAR_MINUTES)
    return d.replace(minute=m)


def completed_bar_until(now: datetime) -> datetime | None:
    """
    Start time of the last 5-min bar that is FULLY complete at `now`
    (i.e. bar_end <= now - BOUNDARY_LAG_SEC). None if no bar is complete yet.
    """
    adj = now - timedelta(seconds=config.BOUNDARY_LAG_SEC)
    h = hm(adj)
    if h < config.SESSION_OPEN_AT:
        return None
    if h >= config.SESSION_CLOSE_AT:
        return bar_times(adj.strftime("%Y-%m-%d"))[-1]
    prev = current_bar_start(adj) - timedelta(minutes=config.BAR_MINUTES)
    if hm(prev) < config.SESSION_OPEN_AT:
        return None
    return prev


def business_days_before(d: date, n: int) -> list[date]:
    """The n business days (Mon-Fri) ending the day before `d`, ascending."""
    out: list[date] = []
    x = d - timedelta(days=1)
    while len(out) < n:
        if x.weekday() < 5:
            out.append(x)
        x -= timedelta(days=1)
    out.reverse()
    return out
