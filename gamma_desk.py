"""Expiry calendar, ATM delta/gamma, and CE/PE pair for the Gamma desk page."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from credentials import dhan_ready
from index_option_picks import build_picks

IST = ZoneInfo("Asia/Kolkata")
TUE = 1
THU = 3
GOLD_MONTHS = {2, 4, 6, 8, 10, 12}
SILVER_MONTHS = {3, 5, 7, 9, 12}
WINDOW_START = time(13, 45)
WINDOW_END = time(15, 0)


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        day = date(year, 12, 31)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def prev_weekday_if_weekend(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def week_mon_fri(today: date) -> list[date]:
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=offset) for offset in range(5)]


def _mcx_on(year: int, month: int, day_num: int, months: set[int] | None) -> date | None:
    if months is not None and month not in months:
        return None
    try:
        return prev_weekday_if_weekend(date(year, month, day_num))
    except ValueError:
        return None


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def events_for(day: date) -> list[dict]:
    tags: list[dict] = []
    if day.weekday() == TUE:
        last_tue = last_weekday_of_month(day.year, day.month, TUE)
        if day == last_tue:
            tags.append(
                {
                    "kind": "nse",
                    "label": "NSE monthly",
                    "detail": "Nifty · BankNifty · FinNifty · Midcap · stock F&O",
                }
            )
        else:
            tags.append(
                {
                    "kind": "nse",
                    "label": "Nifty weekly",
                    "detail": "Tuesday weekly",
                }
            )
    if day.weekday() == THU:
        last_thu = last_weekday_of_month(day.year, day.month, THU)
        if day == last_thu:
            tags.append(
                {
                    "kind": "bse",
                    "label": "BSE monthly",
                    "detail": "Sensex monthly",
                }
            )
        else:
            tags.append(
                {
                    "kind": "bse",
                    "label": "Sensex weekly",
                    "detail": "Thursday weekly",
                }
            )
    gold = _mcx_on(day.year, day.month, 5, GOLD_MONTHS)
    if gold == day:
        tags.append({"kind": "mcx", "label": "Gold ~5th", "detail": "MCX gold"})
    silver = _mcx_on(day.year, day.month, 5, SILVER_MONTHS)
    if silver == day:
        tags.append({"kind": "mcx", "label": "Silver ~5th", "detail": "MCX silver"})
    crude = _mcx_on(day.year, day.month, 19, None)
    if crude == day:
        tags.append({"kind": "mcx", "label": "Crude ~19th", "detail": "MCX crude"})
    ng = prev_weekday_if_weekend(_month_end(day.year, day.month))
    if ng == day:
        tags.append({"kind": "mcx", "label": "NG month-end", "detail": "MCX natural gas"})
    return tags


def _window(today_events: list[dict], now: datetime) -> dict:
    index_expiry = any(item.get("kind") in {"nse", "bse"} for item in today_events)
    clock = now.time()
    if not index_expiry:
        return {
            "active": False,
            "label": "Not an index expiry day",
            "detail": "Gamma blast is for weekly or monthly expiry. Cash ORB still applies.",
        }
    if clock < time(9, 15):
        return {
            "active": False,
            "label": "Pre-open",
            "detail": "Bias only. Do not buy options into the open gap.",
        }
    if WINDOW_START <= clock <= WINDOW_END:
        return {
            "active": True,
            "label": "Expiry window 13:45–15:00 IST",
            "detail": "ATM gamma is highest. One of five slots. ₹500 risk. Hedge never takes the breakout.",
        }
    if clock < WINDOW_START:
        return {
            "active": False,
            "label": "Expiry day - wait for 13:45 IST",
            "detail": "Morning is not the blast. Same 09:25 cash rules until the window.",
        }
    return {
        "active": False,
        "label": "Window closed",
        "detail": "Do not chase the last 15 minutes.",
    }


def _phase(leg: dict | None, today: date) -> str:
    if not leg:
        return "quiet"
    expiry = str(leg.get("expiry") or "")[:10]
    if expiry != today.isoformat():
        return "quiet"
    delta = abs(_num(leg.get("delta")))
    gamma = _num(leg.get("gamma"))
    if delta >= 0.42 and gamma > 0:
        return "blast"
    if delta >= 0.35:
        return "building"
    return "quiet"


def _leg_view(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "contract": row.get("contract"),
        "role": row.get("role"),
        "role_label": row.get("role_label") or row.get("role"),
        "option_type": row.get("option_type"),
        "strike": row.get("strike"),
        "expiry": row.get("expiry"),
        "exchange": row.get("exchange"),
        "instrument": row.get("instrument") or "OPTIDX",
        "security_id": str(row.get("security_id") or ""),
        "ltp": row.get("ltp"),
        "delta": row.get("delta"),
        "gamma": row.get("gamma"),
        "theta": row.get("theta"),
        "vega": row.get("vega"),
        "iv": row.get("iv"),
        "oi": row.get("oi"),
        "volume": row.get("volume"),
        "why": row.get("why"),
        "hedge_for": row.get("hedge_for") or "",
        "spot": row.get("spot"),
        "atm": row.get("atm"),
        "selected": bool(row.get("selected")),
    }


def _pairs_from_rows(rows: list, today: date) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        underlying = str(row.get("underlying") or "").upper()
        if not underlying:
            continue
        bucket = buckets.setdefault(
            underlying,
            {"directional": None, "hedge": None, "fallback_dir": None, "fallback_hedge": None},
        )
        if row.get("role") == "directional":
            if row.get("selected") and not bucket["directional"]:
                bucket["directional"] = row
            if not bucket["fallback_dir"]:
                bucket["fallback_dir"] = row
        elif row.get("role") == "hedge":
            if row.get("selected") and not bucket["hedge"]:
                bucket["hedge"] = row
            if not bucket["fallback_hedge"]:
                bucket["fallback_hedge"] = row
    pairs = []
    for underlying, bucket in buckets.items():
        buy = bucket["directional"] or bucket["fallback_dir"]
        hedge = bucket["hedge"] or bucket["fallback_hedge"]
        if not buy:
            continue
        phase = _phase(buy, today)
        pairs.append(
            {
                "underlying": underlying,
                "exchange": buy.get("exchange") or "NSE",
                "expiry": str(buy.get("expiry") or "")[:10],
                "spot": buy.get("spot"),
                "atm": buy.get("atm"),
                "phase": phase,
                "buy": _leg_view(buy),
                "hedge": _leg_view(hedge),
            }
        )
    order = {"NIFTY": 0, "BANKNIFTY": 1, "SENSEX": 2}
    pairs.sort(key=lambda item: order.get(item["underlying"], 9))
    return pairs


def _month_mcx(today: date) -> list[dict]:
    year, month = today.year, today.month
    items = []
    mapping = (
        (_mcx_on(year, month, 5, GOLD_MONTHS), "Gold ~5th"),
        (_mcx_on(year, month, 5, SILVER_MONTHS), "Silver ~5th"),
        (_mcx_on(year, month, 19, None), "Crude ~19th"),
        (prev_weekday_if_weekend(_month_end(year, month)), "NG month-end"),
    )
    for day, label in mapping:
        if not day:
            continue
        items.append(
            {
                "date": day.isoformat(),
                "label": label,
                "when": "today" if day == today else ("past" if day < today else "ahead"),
            }
        )
    return items


def build_calendar(today: date | None = None) -> dict:
    today = today or datetime.now(IST).date()
    now = datetime.now(IST)
    days = []
    today_events = events_for(today)
    for day in week_mon_fri(today):
        events = events_for(day)
        days.append(
            {
                "date": day.isoformat(),
                "day": day.day,
                "weekday": day.strftime("%a"),
                "is_today": day == today,
                "events": events,
            }
        )
    return {
        "today": today.isoformat(),
        "today_label": today.strftime("%a %d %b %Y"),
        "today_events": today_events,
        "window": _window(today_events, now),
        "week": days,
        "mcx_month": _month_mcx(today),
        "clock": now.strftime("%H:%M:%S"),
        "generated_at": now.isoformat(),
    }


def build_gamma_desk(force: bool = False) -> dict:
    calendar = build_calendar()
    today = date.fromisoformat(calendar["today"])
    picks = {
        "ok": True,
        "rows": [],
        "side": "flat",
        "title": "",
        "vix": None,
        "errors": [],
        "rule": "",
    }
    try:
        picks = build_picks(force) or picks
    except Exception as error:
        picks = {
            "ok": False,
            "error": str(error),
            "rows": [],
            "side": "flat",
            "title": "",
            "vix": None,
            "errors": [str(error)],
            "rule": "",
        }
    pairs = _pairs_from_rows(picks.get("rows") or [], today)
    keys_ready = dhan_ready()
    note = picks.get("note") or picks.get("error") or ""
    if not keys_ready and not note:
        note = "Dhan keys missing. Open Settings, save Client ID and Access Token, then refresh."
    errors = []
    seen = {note}
    for item in picks.get("errors") or []:
        text = str(item)
        if not text or text in seen or (note and text.endswith(note)):
            continue
        seen.add(text)
        errors.append(text)
    return {
        "ok": True,
        "picks_ok": bool(picks.get("ok")),
        "error": picks.get("error") or "",
        "note": note,
        "errors": errors,
        "side": picks.get("side") or "flat",
        "title": picks.get("title") or "",
        "vix": picks.get("vix"),
        "vix_volatile": bool(picks.get("vix_volatile")),
        "rule": picks.get("rule") or "",
        "keys_ready": keys_ready,
        "pairs": pairs,
        "safety": {
            "slots": "1 of 5",
            "risk": "₹500",
            "kill": "−₹3,000",
            "square": "+₹5,000",
            "hedge": "Hedge never takes its own breakout.",
            "engine": "This page does not send engine orders. Add to Execute book only.",
        },
        **calendar,
    }
