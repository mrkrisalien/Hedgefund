"""Persist confirmed fills and reconcile closed Dhan trades into memory.json."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from broker import contract_value_multiplier, get_client, get_trade_book

IST = ZoneInfo("Asia/Kolkata")


def _load():
    path = config.MEMORY_FILE
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(rows):
    config.MEMORY_FILE.write_text(
        json.dumps(rows[-800:], indent=2, default=str),
        encoding="utf-8",
    )


def load_trade_memory():
    return _load()


def append_trade_memory(record):
    rows = _load()
    rows.append(record)
    _save(rows)
    return record


def _entry_datetime(row):
    timestamp = row.get("entry_time") or row.get("time")
    try:
        stamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return stamp.astimezone(IST) if stamp.tzinfo is not None else stamp.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


def _live_rows_today(rows=None):
    today = datetime.now(IST).date()
    entries = []
    for row in rows if rows is not None else _load():
        if str(row.get("mode") or "").upper() != "LIVE":
            continue
        stamp = _entry_datetime(row)
        if stamp is None or stamp.date() != today:
            continue
        entries.append(row)
    return entries


_DEAD_ORDER = {"CANCELLED", "REJECTED", "EXPIRED"}
_PENDING_ORDER = {
    "PENDING",
    "TRANSIT",
    "SUBMITTED",
    "PART_TRADED",
    "ACTIVE",
    "OPEN",
    "TRIGGER_PENDING",
}


def live_entries_today():
    """Names that already used a daily slot: filled, confirmed, or still working."""
    occupied = []
    seen = set()
    for row in _live_rows_today():
        status = str(row.get("status") or "").upper()
        order_status = str(row.get("order_status") or "").upper()
        if status in _DEAD_ORDER or order_status in _DEAD_ORDER:
            continue
        if not (
            order_status in {"TRADED", "PART_TRADED"}
            or status in {"CONFIRMED", "CLOSED", "SUBMITTED"}
            or order_status in _PENDING_ORDER
        ):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        occupied.append(row)
    return occupied


def _names_match(left, right):
    a = str(left or "").upper()
    b = str(right or "").upper()
    return bool(a and b and (a == b or a in b or b in a))


def symbol_already_used_today(symbol, rows=None):
    """True if this name already filled, stopped, targeted, or is still working today."""
    name = str(symbol or "").upper()
    if not name:
        return False
    for row in rows if rows is not None else live_entries_today():
        if _names_match(name, row.get("symbol")):
            return True
    return False


def live_pending_orders_today():
    return [
        row
        for row in _live_rows_today()
        if str(row.get("order_status") or row.get("status") or "").upper()
        in _PENDING_ORDER
    ]


def _symbol_has_open_position(symbol):
    name = str(symbol or "").upper()
    if not name:
        return False
    try:
        from execution import get_open_positions

        for pos in get_open_positions() or []:
            pos_name = str(pos.get("symbol") or "").upper()
            if pos_name and (name in pos_name or pos_name in name):
                return True
    except Exception:
        return False
    return False


def sync_live_order_statuses(cancel_stale=True):
    """Refresh Dhan entry states and cancel unfilled entries after the timeout."""
    rows = _load()
    today_rows = _live_rows_today(rows)
    if not today_rows:
        return []
    client = get_client()
    now = datetime.now(IST)
    timeout = max(
        30,
        int(getattr(config, "LIVE_PENDING_ORDER_TIMEOUT_SECONDS", 120) or 120),
    )
    changed = False
    for row in today_rows:
        if str(row.get("status") or "").upper() == "CLOSED":
            continue
        order_id = str(row.get("order") or row.get("order_id") or "")
        if not order_id:
            continue
        response = client.get_order_by_id(order_id)
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            continue
        order_status = str(data.get("orderStatus") or "").upper()
        stamp = _entry_datetime(row)
        if (
            cancel_stale
            and order_status in _PENDING_ORDER
            and stamp is not None
            and (now - stamp).total_seconds() >= timeout
            and not _symbol_has_open_position(row.get("symbol"))
        ):
            cancel = client.cancel_super_order(order_id, "ENTRY_LEG")
            if str((cancel or {}).get("status") or "").lower() in {"success", "ok"}:
                order_status = "CANCELLED"
                row["cancel_reason"] = f"unfilled after {timeout}s"
                row["cancelled_at"] = now.isoformat()
        if row.get("order_status") != order_status:
            row["order_status"] = order_status
            changed = True
        status = str(row.get("status") or "").upper()
        if order_status == "TRADED" and status != "CONFIRMED":
            row["status"] = "CONFIRMED"
            changed = True
        elif _symbol_has_open_position(row.get("symbol")) and status != "CLOSED":
            if order_status != "TRADED":
                row["order_status"] = "TRADED"
                changed = True
            if status != "CONFIRMED":
                row["status"] = "CONFIRMED"
                changed = True
        elif order_status in _PENDING_ORDER:
            if status != "SUBMITTED":
                row["status"] = "SUBMITTED"
                changed = True
        elif order_status in {"CANCELLED", "REJECTED", "EXPIRED"}:
            if status != order_status:
                row["status"] = order_status
                changed = True
    if changed:
        _save(rows)
    return _live_rows_today(rows)


def _outcome(pnl):
    try:
        value = float(pnl)
    except (TypeError, ValueError):
        return "BREAKEVEN"
    if value > 0.5:
        return "WIN"
    if value < -0.5:
        return "LOSS"
    return "BREAKEVEN"


def reconcile_closed_trades():
    """Match CONFIRMED memory rows to Dhan trade-book exit fills."""
    rows = _load()
    try:
        fills = get_trade_book()
    except Exception as error:
        print(f"[SYSTEM] Trade-book reconcile failed: {error}")
        raise

    newly = 0
    for row in rows:
        if str(row.get("status", "")).upper() != "CONFIRMED":
            continue
        if row.get("exit_deal"):
            continue
        order_id = str(row.get("order") or row.get("order_id") or "")
        symbol = str(row.get("symbol") or "")
        entry_side = str(row.get("side") or row.get("signal") or "BUY").upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        matches = []
        for fill in fills:
            fill_order = str(fill.get("orderId") or fill.get("order_id") or "")
            fill_sym = str(
                fill.get("tradingSymbol")
                or fill.get("symbol")
                or fill.get("securityId")
                or ""
            )
            fill_side = str(fill.get("transactionType") or fill.get("trnOver") or "").upper()
            if order_id and fill_order and fill_order != order_id:
                # Same-day square-off often uses a new order id.
                pass
            if symbol and fill_sym and symbol.upper() not in fill_sym.upper() and fill_sym.upper() not in symbol.upper():
                continue
            if exit_side not in fill_side and fill_side not in (exit_side,):
                continue
            matches.append(fill)
        if not matches:
            continue
        match = matches[-1]
        try:
            remaining = float(row.get("volume") or 0)
            matched_qty = 0.0
            matched_value = 0.0
            for fill in matches:
                fill_qty = float(
                    fill.get("tradedQuantity")
                    or fill.get("quantity")
                    or remaining
                    or 0
                )
                fill_px = float(fill.get("tradedPrice") or fill.get("price") or 0)
                take = min(fill_qty, remaining) if remaining > 0 else fill_qty
                if take > 0 and fill_px > 0:
                    matched_qty += take
                    matched_value += fill_px * take
                    remaining = max(0.0, remaining - take)
                if remaining == 0:
                    break
            entry = float(row.get("entry_price") or 0)
            exit_px = matched_value / matched_qty if matched_qty else 0.0
            multiplier = contract_value_multiplier(symbol)
            side = str(row.get("side") or "BUY").upper()
            pnl = (
                (exit_px - entry) * matched_qty * multiplier
                if side == "BUY"
                else (entry - exit_px) * matched_qty * multiplier
            )
        except (TypeError, ValueError):
            exit_px = 0.0
            pnl = 0.0
        row["status"] = "CLOSED"
        row["exit_deal"] = match.get("exchangeTradeId") or match.get("dhanOrderId") or match.get("orderId")
        row["exit_price"] = exit_px or match.get("tradedPrice") or match.get("price")
        row["exit_time"] = match.get("exchangeTime") or datetime.now(IST).isoformat()
        row["realized_pnl"] = pnl
        row["outcome"] = _outcome(pnl)
        newly += 1

    if newly:
        _save(rows)
        print(f"[LEARNING] Reconciled {newly} closed trade(s).")
    return newly


def update_live_memory_row(symbol, **fields):
    name = str(symbol or "").upper()
    if not name:
        return None
    rows = _load()
    updated = None
    for row in reversed(rows):
        if str(row.get("mode") or "").upper() != "LIVE":
            continue
        row_name = str(row.get("symbol") or "").upper()
        if row_name != name and name not in row_name and row_name not in name:
            continue
        if str(row.get("status") or "").upper() == "CLOSED":
            continue
        row.update(fields)
        updated = row
        break
    if updated is not None:
        _save(rows)
    return updated


def realized_pnl_today():
    """Return today's reconciled realized P&L split into cash and MCX."""
    today = datetime.now(IST).date()
    cash = 0.0
    mcx = 0.0
    for row in _load():
        if str(row.get("status", "")).upper() != "CLOSED":
            continue
        timestamp = row.get("exit_time") or row.get("time")
        try:
            stamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(IST)
            if stamp.date() != today:
                continue
        except (TypeError, ValueError):
            continue
        try:
            pnl = float(row.get("realized_pnl") or 0)
        except (TypeError, ValueError):
            continue
        if contract_value_multiplier(row.get("symbol")) > 1:
            mcx += pnl
        else:
            cash += pnl
    return cash, mcx
