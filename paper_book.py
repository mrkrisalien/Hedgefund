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
        }
        fills.append(row)
        _save(doc)
        return row


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


def trade_history(session_date=None):
    rows = []
    for row in fills_for_day(session_date):
        status = str(row.get("status") or "OPEN").upper()
        result = "Paper position open"
        if status == "CLOSED":
            result = (
                f"Paper exit {row.get('exit_reason') or ''} @ {row.get('exit_price')} "
                f"| P&L Rs {float(row.get('pnl') or 0):.2f}"
            )
        rows.append(
            {
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
                "sl": row.get("sl"),
                "tp": row.get("tp"),
                "volume": row.get("qty"),
                "deal": "PAPER",
                "status": status,
                "exit_price": row.get("exit_price"),
                "exit_reason": row.get("exit_reason"),
                "pnl": float(row.get("pnl") or 0),
            }
        )
    return rows


def refresh_positions():
    """Mark paper SL/TP exits from live Dhan quotes; never submits an order."""
    from broker import get_quote, resolve_symbol_cached

    with _LOCK:
        doc = _load()
        changed = 0
        now = datetime.now(IST)
        for row in doc.get("fills", []):
            if row.get("status") != "OPEN":
                continue
            stale = str(row.get("session") or "") < str(now.date())
            try:
                entry = float(row["price"])
                sl = float(row["sl"])
                tp = float(row["tp"])
                qty = float(row["qty"])
                if stale and row.get("ltp") is not None:
                    ltp = float(row["ltp"])
                else:
                    instrument = resolve_symbol_cached(row["symbol"])
                    quote = get_quote(instrument)
                    ltp = float(quote.get("last_price") or 0)
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
            if exit_price is None:
                row["ltp"] = ltp
                continue
            multiplier = float(row.get("value_multiplier") or 1)
            pnl = (
                (exit_price - entry) * qty * multiplier
                if side == "BUY"
                else (entry - exit_price) * qty * multiplier
            )
            row.update(
                {
                    "status": "CLOSED",
                    "exit_price": exit_price,
                    "exit_ts": now.isoformat(),
                    "exit_reason": reason,
                    "pnl": round(pnl, 2),
                    "ltp": ltp,
                }
            )
            changed += 1
        _save(doc)
        return changed


def open_positions():
    positions = []
    for row in fills_today():
        if row.get("status") != "OPEN":
            continue
        ltp = float(row.get("ltp") or row.get("price") or 0)
        entry = float(row.get("price") or 0)
        qty = float(row.get("qty") or 0)
        multiplier = float(row.get("value_multiplier") or 1)
        side = str(row.get("signal") or "BUY").upper()
        pnl = (
            (ltp - entry) * qty * multiplier
            if side == "BUY"
            else (entry - ltp) * qty * multiplier
        )
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
