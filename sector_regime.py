"""Sector tilt: calendar-month lock, 20-day RS, crude/gold.

Cash months are only those whose locked sector was net-green on the
calendar-lock year (FMCG / Realty / Energy). IT, Fin, Metal, and Bank
months are skipped. That cut is in-sample. Fed/news are not in the feed.
"""
from __future__ import annotations

import pandas as pd

import config

# Month (1-12) -> sectors we are allowed to trade that month.
# Month map after dropping sectors that lost on the calendar-lock year.
# IT, Fin, Metal, Bank are skipped. March stays dark. In-sample cut.
CALENDAR_SECTORS = {
    1: ("NIFTY FMCG",),
    2: ("NIFTY REALTY",),
    3: (),
    4: ("NIFTY ENERGY",),
    5: (),
    6: ("NIFTY REALTY",),
    7: (),
    8: (),
    9: ("NIFTY REALTY",),
    10: (),
    11: ("NIFTY REALTY",),
    12: (),
}

# Single name used by the old soft-prior bonus (first name of the month).
CALENDAR_WINNER = {
    month: names[0] for month, names in CALENDAR_SECTORS.items() if names
}

CRUDE_UP_BOOST = ("NIFTY ENERGY", "NIFTY METAL")
CRUDE_DOWN_BOOST = ("NIFTY AUTO", "NIFTY FMCG")
GOLD_UP_BOOST = ("NIFTY METAL",)
GOLD_DOWN_BOOST = ("NIFTY FMCG", "NIFTY FIN SERVICE")


def trailing_return(daily_df, before_date, lookback=20):
    if daily_df is None or daily_df.empty or "close" not in daily_df.columns:
        return None
    frame = daily_df.copy()
    if "ist_date" in frame.columns and before_date is not None:
        frame = frame[frame["ist_date"] < before_date]
    if len(frame) < lookback + 1:
        return None
    closes = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(closes) < lookback + 1:
        return None
    start = float(closes.iloc[-lookback - 1])
    end = float(closes.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start


def _month(session_date):
    return session_date.month if hasattr(session_date, "month") else int(session_date)


def allowed_calendar_sectors(session_date):
    """None = no lock. Tuple (possibly empty) = only these sector names."""
    if not getattr(config, "CALENDAR_SECTOR_LOCK", False):
        return None
    return CALENDAR_SECTORS.get(_month(session_date), ())


def sector_allowed(sector, session_date):
    allowed = allowed_calendar_sectors(session_date)
    if allowed is None:
        return True
    return sector in allowed


def calendar_bonus(sector, session_date):
    if not getattr(config, "USE_CALENDAR_SECTOR_PRIOR", True):
        return 0.0
    month = _month(session_date)
    names = CALENDAR_SECTORS.get(month, ())
    if sector in names:
        return 0.35
    if month == 3:
        return -0.15
    return 0.0


def macro_bonus(sector, crude_ret, gold_ret):
    if not getattr(config, "USE_MACRO_SECTOR_TILT", True):
        return 0.0
    bonus = 0.0
    thresh = float(getattr(config, "MACRO_MOVE_THRESHOLD", 0.03))
    if crude_ret is not None:
        if crude_ret >= thresh and sector in CRUDE_UP_BOOST:
            bonus += 0.4
        if crude_ret <= -thresh and sector in CRUDE_DOWN_BOOST:
            bonus += 0.4
        if crude_ret >= thresh and sector == "NIFTY AUTO":
            bonus -= 0.25
    if gold_ret is not None:
        if gold_ret >= thresh and sector in GOLD_UP_BOOST:
            bonus += 0.35
        if gold_ret <= -thresh and sector in GOLD_DOWN_BOOST:
            bonus += 0.2
    return bonus


def tilt_score(sector, opening_momentum, rs_20d, session_date, crude_ret, gold_ret):
    """Combine same-day open, 20d sector RS, crude/gold, calendar prior."""
    score = float(opening_momentum or 0) * 100.0
    if rs_20d is not None:
        score += 20.0 * float(rs_20d)
        min_rs = float(getattr(config, "SECTOR_RS_MIN", -0.04))
        if rs_20d < min_rs:
            score -= 0.8
    score += calendar_bonus(sector, session_date)
    score += macro_bonus(sector, crude_ret, gold_ret)
    return score


def passes_rs_gate(rs_20d):
    if rs_20d is None:
        return True
    min_rs = float(getattr(config, "SECTOR_RS_MIN", -0.04))
    return rs_20d >= min_rs
