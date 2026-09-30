"""Persist Execute desk levels so a refresh or restart does not wipe them."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PATH = Path(__file__).resolve().parent / "data" / "desk_snapshot.json"
_LOCK = threading.RLock()
_LAST_WRITE = 0.0

KEEP_KEYS = (
    "session_date",
    "last_logic",
    "last_confidence",
    "last_signal",
    "last_entry_price",
    "last_stop_loss",
    "last_take_profit",
    "day_start_equity",
    "cash_pnl_today",
    "mcx_pnl_today",
    "orders_placed",
    "filled_sources",
    "interval",
    "risk_percent",
    "confirmed_fills",
)


def today_ist():
    return str(datetime.now(IST).date())


def load_snapshot():
    with _LOCK:
        if not PATH.exists():
            return {}
        try:
            doc = json.loads(PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return doc if isinstance(doc, dict) else {}


def restore_snapshot(bot_state):
    doc = load_snapshot()
    if not doc:
        return False
    same_day = str(doc.get("session_date") or "") == today_ist()
    if same_day:
        for key in KEEP_KEYS:
            if key in doc and doc[key] not in (None, ""):
                bot_state[key] = doc[key]
        watch = doc.get("watchlist") or []
        if watch:
            bot_state["watchlist"] = watch
        alerts = doc.get("breakout_alerts") or []
        if alerts:
            bot_state["breakout_alerts"] = alerts
    return True


def save_snapshot(bot_state, watchlist=None, min_interval=2.0):
    import time

    global _LAST_WRITE
    now = time.time()
    with _LOCK:
        if now - _LAST_WRITE < float(min_interval):
            return False
        payload = {key: bot_state.get(key) for key in KEEP_KEYS}
        payload["session_date"] = bot_state.get("session_date") or today_ist()
        payload["updated_at"] = datetime.now(IST).isoformat()
        payload["watchlist"] = list(
            watchlist
            if watchlist is not None
            else (bot_state.get("watchlist") or [])
        )
        payload["breakout_alerts"] = list(bot_state.get("breakout_alerts") or [])[-100:]
        PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(temp, PATH)
        _LAST_WRITE = now
    return True
