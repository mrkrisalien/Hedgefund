"""Persistent paper ledger, restart-safe session state, and EOD reports."""
from __future__ import annotations

import csv
import json
import os
import threading
from datetime import date, datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")
PAPER_PATH = Path(__file__).resolve().parent / "data" / "paper_trades.json"
REPORT_DIR = Path(__file__).resolve().parent / "data" / "reports"
REQUIRED_SOURCES = ("catalyst", "seasonal")
_LOCK = threading.RLock()


def _load():
    with _LOCK:
        if not PAPER_PATH.exists():
            return {"fills": [], "sessions": {}}
        try:
            doc = json.loads(PAPER_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"fills": [], "sessions": {}}
        if not isinstance(doc, dict):
            return {"fills": [], "sessions": {}}
        doc.setdefault("fills", [])
        doc.setdefault("sessions", {})
        return doc


def _save(doc):
    with _LOCK:
        PAPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PAPER_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(temp_path, PAPER_PATH)


def _date_key(value=None):
    if value is None:
        return str(datetime.now(IST).date())
    if isinstance(value, (date, datetime)):
        return str(value.date() if isinstance(value, datetime) else value)
    return str(value)


def _fills_for(doc, session_date=None):
    key = _date_key(session_date)
    return [row for row in doc.get("fills", []) if row.get("session") == key]


def _derived_sources(rows):
    sources = []
    for row in rows:
        source = str(row.get("source") or "").strip().lower()
        if source and source not in sources:
            sources.append(source)

    # Fills created before source persistence cannot be identified precisely.
    # Assign them to still-empty slots in order; this fails safe by preventing
    # the same daily slots from firing again after a restart.
    legacy_count = sum(1 for row in rows if not row.get("source"))
    for source in REQUIRED_SOURCES:
        if legacy_count <= 0:
            break
        if source not in sources:
            sources.append(source)
            legacy_count -= 1
    return sources


def record_fill(
    symbol,
    signal,
    quantity,
    price,
    sl,
    tp,
    sector="",
    kind="NSE_EQ",
    source="",
    value_multiplier=1.0,
    **kwargs,
):
    now = datetime.now(IST)
    with _LOCK:
        doc = _load()
        fills = doc.setdefault("fills", [])
        row = {
            "id": uuid4().hex,
            "ts": now.isoformat(),
            "session": str(now.date()),
            "symbol": str(symbol),
            "signal": signal,
            "qty": int(quantity),
            "price": float(price) if price is not None else None,
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "sector": sector,
            "source": str(source or "").lower(),
            "kind": kind,
            "value_multiplier": float(value_multiplier or 1),
            "paper": True,
            "status": "OPEN",
            "exit_price": None,
            "exit_ts": None,
            "exit_reason": None,
            "pnl": 0.0,
            "ltp": float(price) if price is not None else None,
            "security_id": str(kwargs.get("security_id") or ""),
            "exchange": kwargs.get("exchange") or "",
            "instrument": kwargs.get("instrument") or "",
        }
        fills.append(row)
        _save(doc)
        return row


def close_fill(ticket, exit_price=None, reason="MANUAL"):
    fill_id = str(ticket or "").replace("PAPER-", "")
    now = datetime.now(IST)
    with _LOCK:
        doc = _load()
        for row in doc.get("fills", []):
            if str(row.get("id")) != fill_id:
                continue
            if row.get("status") == "CLOSED":
                return {"ok": True, "paper": True, "fill": row}
            entry = float(row.get("price") or 0)
            qty = float(row.get("qty") or 0)
            multiplier = float(row.get("value_multiplier") or 1)
            side = str(row.get("signal") or "BUY").upper()
            mark = float(exit_price if exit_price is not None else (row.get("ltp") or entry))
            pnl = (
                (mark - entry) * qty * multiplier
                if side == "BUY"
                else (entry - mark) * qty * multiplier
            )
            row.update(
                {
                    "status": "CLOSED",
                    "exit_price": mark,
                    "exit_ts": now.isoformat(),
                    "exit_reason": reason,
                    "pnl": round(pnl, 2),
                    "ltp": mark,
                }
            )
            _save(doc)
            return {"ok": True, "paper": True, "fill": row}
    raise RuntimeError(f"No paper position matching {ticket}")


def fills_for_day(session_date=None):
    return _fills_for(_load(), session_date)


def fills_today():
    return fills_for_day()


def session_state(session_date=None):
    key = _date_key(session_date)
    doc = _load()
    saved = dict((doc.get("sessions") or {}).get(key) or {})
    sources = list(saved.get("filled_sources") or [])
    for source in _derived_sources(_fills_for(doc, key)):
        if source not in sources:
            sources.append(source)
    saved.update(
        {
            "session_date": key,
            "filled_sources": sources,
            "orders_placed": bool(saved.get("orders_placed"))
            or set(REQUIRED_SOURCES).issubset(sources),
            "day_start_equity": float(saved.get("day_start_equity") or 0),
            "is_running": bool(saved.get("is_running", False)),
            "eod_report_written": bool(saved.get("eod_report_written", False)),
        }
    )
    return saved


def save_session(session_date=None, **updates):
    key = _date_key(session_date)
    with _LOCK:
        doc = _load()
        sessions = doc.setdefault("sessions", {})
        state = dict(sessions.get(key) or {})
        allowed = {
            "filled_sources",
            "orders_placed",
            "day_start_equity",
            "is_running",
            "interval",
            "risk_percent",
            "eod_report_written",
        }
        state.update({name: value for name, value in updates.items() if name in allowed})
        state["updated_at"] = datetime.now(IST).isoformat()
        sessions[key] = state
        _save(doc)
    return session_state(key)


def _notional(row):
    price = float(row.get("price") or row.get("fill") or 0)
    qty = float(row.get("qty") or row.get("volume") or 0)
    multiplier = float(row.get("value_multiplier") or 1)
    return abs(price * qty * multiplier)


def _mark_pnl(row, ltp=None):
    entry = float(row.get("price") or 0)
    qty = float(row.get("qty") or 0)
    multiplier = float(row.get("value_multiplier") or 1)
    side = str(row.get("signal") or "BUY").upper()
    mark = float(ltp if ltp is not None else (row.get("ltp") if row.get("ltp") not in (None, "") else entry) or 0)
    if side == "BUY":
        pnl = (mark - entry) * qty * multiplier
    else:
        pnl = (entry - mark) * qty * multiplier
    return round(pnl, 2), mark


def _pnl_pct(pnl, notional):
    if not notional:
        return None
    return round(float(pnl or 0) / notional * 100.0, 2)


def available_dates():
    days = {
        str(row.get("session") or "")
        for row in (_load().get("fills") or [])
        if row.get("session")
    }
    days.update((_load().get("sessions") or {}).keys())
    return sorted(day for day in days if day)


def day_pnl_stats(session_date=None, start_equity=0.0):
    fills = fills_for_day(session_date)
    closed = [row for row in fills if str(row.get("status") or "").upper() == "CLOSED"]
    open_rows = [row for row in fills if str(row.get("status") or "").upper() != "CLOSED"]
    realized = round(sum(float(row.get("pnl") or 0) for row in closed), 2)
    open_pnl = 0.0
    for row in open_rows:
        ltp = float(row.get("ltp") or row.get("price") or 0)
        entry = float(row.get("price") or 0)
        qty = float(row.get("qty") or 0)
        multiplier = float(row.get("value_multiplier") or 1)
        side = str(row.get("signal") or "BUY").upper()
        if side == "BUY":
            open_pnl += (ltp - entry) * qty * multiplier
        else:
            open_pnl += (entry - ltp) * qty * multiplier
    open_pnl = round(open_pnl, 2)
    start = float(start_equity or 0)
    if start <= 0:
        start = round(sum(_notional(row) for row in fills), 2)
    pct = round(((realized + open_pnl) / start) * 100.0, 2) if start else 0.0
    realized_pct = round((realized / start) * 100.0, 2) if start else 0.0
    return {
        "realized_pnl": realized,
        "open_pnl": open_pnl,
        "today_pnl": round(realized + open_pnl, 2),
        "day_start_equity": start or None,
        "today_pnl_pct": pct,
        "realized_pnl_pct": realized_pct,
        "trade_count": len(fills),
        "closed_count": len(closed),
        "session": _date_key(session_date),
        "book": "paper",
    }


def today_pnl_stats(start_equity=0.0):
    return day_pnl_stats(None, start_equity)


def _history_row(row):
    status = str(row.get("status") or "OPEN").upper()
    if status == "OPEN":
        pnl, _ = _mark_pnl(row)
    else:
        pnl = float(row.get("pnl") or 0)
    notional = _notional(row)
    result = "Paper position open"
    if status == "CLOSED":
        result = (
            f"Paper exit {row.get('exit_reason') or ''} @ {row.get('exit_price')} "
            f"| P&L Rs {pnl:.2f}"
        )
    return {
        "id": row.get("id"),
        "time": row.get("ts"),
        "asset": row.get("symbol"),
        "symbol": row.get("symbol"),
        "sector": row.get("sector") or "",
        "source": row.get("source") or "",
        "signal": row.get("signal"),
        "side": row.get("signal"),
        "result": result,
        "fill": row.get("price"),
        "price": row.get("price"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "volume": row.get("qty"),
        "deal": "PAPER",
        "status": status,
        "exit_price": row.get("exit_price"),
        "exit_reason": row.get("exit_reason"),
        "pnl": pnl,
        "pnl_pct": _pnl_pct(pnl, notional),
        "paper": True,
        "ticket": f"PAPER-{row.get('id') or row.get('symbol')}",
        "instrument": row.get("instrument") or "",
        "exchange": row.get("exchange") or "",
        "security_id": str(row.get("security_id") or ""),
        "book": _position_book(row),
    }


def trade_history(session_date=None):
    return [_history_row(row) for row in fills_for_day(session_date)]


def trade_history_recent(limit=40):
    fills = list(_load().get("fills") or [])
    return [_history_row(row) for row in fills[-max(1, int(limit or 40)) :]]


def latest_levels_by_symbol(limit=40, session_date=None):
    levels = {}
    rows = (
        trade_history(session_date)
        if session_date
        else trade_history_recent(limit)
    )
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            levels[symbol] = row
    return levels


_LAST_LTP_REFRESH = 0.0


def refresh_positions():
    """Mark paper SL/TP exits from live Dhan quotes; never submits an order."""
    import time as clock
    from broker import get_ltp_batch, resolve_symbol_cached

    global _LAST_LTP_REFRESH
    now_mono = clock.monotonic()
    if now_mono - _LAST_LTP_REFRESH < 8:
        return 0
    _LAST_LTP_REFRESH = now_mono

    with _LOCK:
        doc = _load()
        changed = 0
        now = datetime.now(IST)
        open_rows = [
            row
            for row in doc.get("fills", [])
            if row.get("status") == "OPEN"
        ]
        resolved_map = {}
        instruments = []
        for row in open_rows:
            if str(row.get("session") or "") < str(now.date()):
                continue
            request = {"symbol": row.get("symbol")}
            if row.get("security_id"):
                request["security_id"] = row["security_id"]
            if row.get("exchange"):
                request["exchange"] = row["exchange"]
            if row.get("instrument"):
                request["instrument"] = row["instrument"]
            try:
                resolved = resolve_symbol_cached(request)
            except Exception:
                continue
            resolved_map[id(row)] = resolved
            instruments.append(resolved)
        prices = {}
        if instruments:
            try:
                prices = get_ltp_batch(instruments) or {}
            except Exception:
                prices = {}

        for row in open_rows:
            stale = str(row.get("session") or "") < str(now.date())
            try:
                entry = float(row["price"])
                sl = float(row["sl"])
                tp = float(row["tp"])
                qty = float(row["qty"])
                if stale and row.get("ltp") is not None:
                    ltp = float(row["ltp"])
                else:
                    resolved = resolved_map.get(id(row))
                    ltp = 0.0
                    if resolved:
                        ltp = float(
                            prices.get(resolved.get("trading_symbol"))
                            or prices.get(resolved.get("symbol"))
                            or prices.get(resolved.get("display_symbol"))
                            or prices.get(str(resolved.get("security_id") or ""))
                            or 0
                        )
                    if ltp <= 0:
                        ltp = float(row.get("ltp") or 0)
                if ltp <= 0:
                    continue
            except Exception:
                continue
            side = str(row.get("signal") or "BUY").upper()
            exit_price = None
            reason = None
            if stale:
                exit_price, reason = ltp, "EOD_RECOVERY"
            elif side == "BUY" and ltp <= sl:
                exit_price, reason = sl, "SL"
            elif side == "BUY" and ltp >= tp:
                exit_price, reason = tp, "TP"
            elif side == "SELL" and ltp >= sl:
                exit_price, reason = sl, "SL"
            elif side == "SELL" and ltp <= tp:
                exit_price, reason = tp, "TP"
            elif now.time() >= time(15, 25):
                exit_price, reason = ltp, "EOD"
            pnl, _ = _mark_pnl(row, ltp)
            row["ltp"] = ltp
            if exit_price is None:
                row["pnl"] = pnl
                changed += 1
                continue
            row.update(
                {
                    "status": "CLOSED",
                    "exit_price": exit_price,
                    "exit_ts": now.isoformat(),
                    "exit_reason": reason,
                    "pnl": _mark_pnl(row, exit_price)[0],
                    "ltp": ltp,
                }
            )
            changed += 1
        if changed:
            _save(doc)
        return changed


def _position_book(row):
    inst = str(row.get("instrument") or "").upper()
    exch = str(row.get("exchange") or "").upper()
    if exch == "MCX" or inst in {"OPTFUT", "FUTCOM"}:
        return "mcx"
    if inst in {"OPTIDX", "OPTSTK", "INDEX", "FUTIDX"}:
        return "index"
    return "cash"


def open_positions(session_date=None):
    rows = fills_for_day(session_date) if session_date else _load().get("fills", [])
    positions = []
    for row in rows:
        if row.get("status") != "OPEN":
            continue
        ltp = float(row.get("ltp") or row.get("price") or 0)
        entry = float(row.get("price") or 0)
        qty = float(row.get("qty") or 0)
        side = str(row.get("signal") or "BUY").upper()
        pnl, _ = _mark_pnl(row, ltp)
        positions.append(
            {
                "ticket": f"PAPER-{row.get('id') or row.get('symbol')}",
                "symbol": row.get("symbol"),
                "side": side,
                "volume": qty,
                "price": entry,
                "sl": row.get("sl"),
                "tp": row.get("tp"),
                "pnl": round(pnl, 2),
                "paper": True,
                "instrument": row.get("instrument") or "",
                "exchange": row.get("exchange") or "",
                "security_id": str(row.get("security_id") or ""),
                "book": _position_book(row),
            }
        )
    return positions


def realized_pnl_today():
    return sum(
        float(row.get("pnl") or 0)
        for row in fills_today()
        if row.get("status") == "CLOSED"
    )


def daily_summary(session_date=None):
    key = _date_key(session_date)
    fills = fills_for_day(key)
    state = session_state(key)
    closed = [row for row in fills if row.get("status") == "CLOSED"]
    open_rows = [row for row in fills if row.get("status") == "OPEN"]
    realized = round(sum(float(row.get("pnl") or 0) for row in closed), 2)
    wins = sum(1 for row in closed if float(row.get("pnl") or 0) > 0)
    losses = sum(1 for row in closed if float(row.get("pnl") or 0) < 0)
    start_equity = float(state.get("day_start_equity") or 0)
    return {
        "date": key,
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "PAPER",
        "day_start_equity": start_equity or None,
        "ending_equity": round(start_equity + realized, 2) if start_equity else None,
        "trade_count": len(fills),
        "closed_count": len(closed),
        "open_count": len(open_rows),
        "wins": wins,
        "losses": losses,
        "breakeven": len(closed) - wins - losses,
        "win_rate_pct": round((wins / len(closed) * 100) if closed else 0, 2),
        "gross_profit": round(
            sum(max(0, float(row.get("pnl") or 0)) for row in closed), 2
        ),
        "gross_loss": round(
            sum(min(0, float(row.get("pnl") or 0)) for row in closed), 2
        ),
        "realized_pnl": realized,
        "filled_sources": state.get("filled_sources") or [],
        "trades": fills,
    }


def _report_paths(session_date):
    key = _date_key(session_date)
    return (
        REPORT_DIR / f"paper_eod_{key}.json",
        REPORT_DIR / f"paper_eod_{key}.csv",
    )


def write_eod_report(session_date=None, force=False):
    key = _date_key(session_date)
    json_path, csv_path = _report_paths(key)
    with _LOCK:
        if json_path.exists() and not force:
            try:
                return json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        report = daily_summary(key)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = json_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(temp_path, json_path)
        fields = [
            "ts", "symbol", "source", "sector", "signal", "qty", "price",
            "sl", "tp", "status", "exit_ts", "exit_price", "exit_reason", "pnl",
        ]
        csv_temp = csv_path.with_suffix(".tmp")
        with csv_temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in report["trades"]:
                writer.writerow({field: row.get(field) for field in fields})
        os.replace(csv_temp, csv_path)
        save_session(key, eod_report_written=True)
        report["json_path"] = str(json_path)
        report["csv_path"] = str(csv_path)
        return report


def write_due_eod_reports(now=None):
    now = now or datetime.now(IST)
    doc = _load()
    days = sorted(
        {
            str(row.get("session"))
            for row in doc.get("fills", [])
            if row.get("session")
        }
        | set((doc.get("sessions") or {}).keys())
    )
    written = []
    for key in days:
        due = key < str(now.date()) or (
            key == str(now.date()) and now.time() >= time(15, 30)
        )
        json_path, _ = _report_paths(key)
        if due and not json_path.exists():
            written.append(write_eod_report(key))
    return written


def get_eod_report(session_date=None):
    key = _date_key(session_date)
    json_path, _ = _report_paths(key)
    if not json_path.exists():
        return None
    try:
        report = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    report["json_path"] = str(json_path)
    report["csv_path"] = str(_report_paths(key)[1])
    return report
