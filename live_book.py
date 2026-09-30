"""Dated live Dhan book, stored separately from paper fills."""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LIVE_PATH = Path(__file__).resolve().parent / "data" / "live_trades.json"
_LOCK = threading.RLock()


def _load():
    with _LOCK:
        if not LIVE_PATH.exists():
            return {"fills": [], "sessions": {}}
        try:
            doc = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"fills": [], "sessions": {}}
        if not isinstance(doc, dict):
            return {"fills": [], "sessions": {}}
        doc.setdefault("fills", [])
        doc.setdefault("sessions", {})
        return doc


def _save(doc):
    with _LOCK:
        LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = LIVE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        os.replace(temp, LIVE_PATH)


def _date_key(value=None):
    if value is None:
        return str(datetime.now(IST).date())
    if isinstance(value, (date, datetime)):
        return str(value.date() if isinstance(value, datetime) else value)
    text = str(value or "").strip()
    return text[:10] if text else str(datetime.now(IST).date())


def _stamp_date(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(IST)
        return str(stamp.date())
    except (TypeError, ValueError):
        text = str(value)
        return text[:10] if len(text) >= 10 and text[4] == "-" else None


def _notional(row):
    price = float(row.get("price") or row.get("fill") or 0)
    qty = float(row.get("qty") or row.get("volume") or 0)
    multiplier = float(row.get("value_multiplier") or 1)
    return abs(price * qty * multiplier)


def _pnl_pct(pnl, notional):
    if not notional:
        return None
    return round(float(pnl or 0) / notional * 100.0, 2)


def _memory_key(record):
    order = str(record.get("order") or record.get("ticket") or record.get("id") or "")
    if order:
        return f"order:{order}"
    symbol = str(record.get("symbol") or "").upper()
    entry = str(record.get("entry_time") or record.get("ts") or "")
    return f"{symbol}:{entry}" if symbol and entry else ""


def _history_row(row):
    status = str(row.get("status") or "OPEN").upper()
    pnl = float(row.get("pnl") or row.get("realized_pnl") or 0)
    notional = _notional(row)
    result = "Live position open"
    if status == "CLOSED":
        result = (
            f"Live exit {row.get('exit_reason') or ''} @ {row.get('exit_price')} "
            f"| P&L Rs {pnl:.2f}"
        )
    return {
        "id": row.get("id"),
        "time": row.get("ts") or row.get("entry_time"),
        "asset": row.get("symbol"),
        "symbol": row.get("symbol"),
        "sector": row.get("sector") or "",
        "source": row.get("source") or "",
        "signal": row.get("signal") or row.get("side"),
        "side": row.get("side") or row.get("signal"),
        "result": result,
        "fill": row.get("price"),
        "price": row.get("price"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "volume": row.get("qty"),
        "deal": "LIVE",
        "status": status,
        "exit_price": row.get("exit_price"),
        "exit_reason": row.get("exit_reason"),
        "pnl": pnl,
        "pnl_pct": _pnl_pct(pnl, notional),
        "paper": False,
        "live": True,
    }


def fills_for_day(session_date=None):
    key = _date_key(session_date)
    return [row for row in _load().get("fills", []) if row.get("session") == key]


def available_dates():
    days = {
        str(row.get("session") or "")
        for row in (_load().get("fills") or [])
        if row.get("session")
    }
    return sorted(day for day in days if day)


def trade_history(session_date=None):
    return [_history_row(row) for row in fills_for_day(session_date)]


def latest_levels_by_symbol(session_date=None):
    levels = {}
    for row in trade_history(session_date):
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            levels[symbol] = row
    return levels


def day_pnl_stats(session_date=None, start_equity=0.0):
    fills = fills_for_day(session_date)
    closed = [row for row in fills if str(row.get("status") or "").upper() == "CLOSED"]
    open_rows = [row for row in fills if str(row.get("status") or "").upper() != "CLOSED"]
    realized = round(sum(float(row.get("pnl") or row.get("realized_pnl") or 0) for row in closed), 2)
    open_pnl = round(sum(float(row.get("pnl") or 0) for row in open_rows), 2)
    start = float(start_equity or 0)
    if start <= 0:
        start = round(sum(_notional(row) for row in fills), 2)
    total = round(realized + open_pnl, 2)
    pct = round((total / start) * 100.0, 2) if start else 0.0
    return {
        "realized_pnl": realized,
        "open_pnl": open_pnl,
        "today_pnl": total,
        "day_start_equity": start or None,
        "today_pnl_pct": pct,
        "realized_pnl_pct": round((realized / start) * 100.0, 2) if start else 0.0,
        "trade_count": len(fills),
        "closed_count": len(closed),
        "session": _date_key(session_date),
        "book": "live",
    }


def upsert_from_memory(record):
    if str((record or {}).get("mode") or "").upper() != "LIVE":
        return None
    key = _memory_key(record)
    session = (
        _stamp_date(record.get("entry_time") or record.get("ts") or record.get("time"))
        or _date_key()
    )
    status = str(record.get("status") or "OPEN").upper()
    if status in {"CONFIRMED", "SUBMITTED"}:
        status = "OPEN" if not record.get("exit_price") else "CLOSED"
    row = {
        "id": record.get("live_id") or uuid4().hex,
        "memory_key": key,
        "ts": record.get("entry_time") or record.get("time") or datetime.now(IST).isoformat(),
        "session": session,
        "symbol": str(record.get("symbol") or ""),
        "signal": record.get("signal") or record.get("side") or "BUY",
        "side": record.get("side") or record.get("signal") or "BUY",
        "qty": int(record.get("volume") or record.get("qty") or 0),
        "price": record.get("entry_price") or record.get("price") or record.get("fill"),
        "sl": record.get("sl"),
        "tp": record.get("tp") or record.get("virtual_tp"),
        "sector": record.get("sector") or "",
        "source": str(record.get("source") or "").lower(),
        "kind": record.get("kind") or "NSE_EQ",
        "value_multiplier": float(record.get("value_multiplier") or 1),
        "paper": False,
        "live": True,
        "order": record.get("order") or record.get("ticket"),
        "status": status,
        "exit_price": record.get("exit_price"),
        "exit_ts": record.get("exit_time"),
        "exit_reason": record.get("exit_reason") or record.get("outcome"),
        "pnl": float(record.get("realized_pnl") or record.get("pnl") or 0),
    }
    with _LOCK:
        doc = _load()
        fills = doc.setdefault("fills", [])
        matched = None
        for existing in fills:
            if key and existing.get("memory_key") == key:
                matched = existing
                break
            if (
                not key
                and str(existing.get("symbol") or "").upper() == row["symbol"].upper()
                and existing.get("session") == session
                and str(existing.get("status") or "").upper() != "CLOSED"
            ):
                matched = existing
                break
        if matched:
            matched.update({k: v for k, v in row.items() if v is not None or k in {"exit_price", "pnl"}})
            matched["id"] = matched.get("id") or row["id"]
            saved = matched
        else:
            fills.append(row)
            saved = row
        _save(doc)
        return saved


def import_from_memory():
    from memory_store import load_trade_memory

    count = 0
    for record in load_trade_memory():
        if upsert_from_memory(record):
            count += 1
    return count
