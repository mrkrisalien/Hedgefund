"""Persistent Delta chart universe and date-scoped trade approvals."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UNIVERSE_PATH = Path(__file__).resolve().parent / "data" / "delta_universe.json"
STRATEGIES = {
    "buy_straddle",
    "buy_strangle",
    "sell_straddle",
    "sell_strangle",
    "auto_both",
}
MARKETS = {"options", "futures"}

DEFAULT_UNIVERSE = [
    {"symbol": "BTCUSD", "market": "options", "strategy": "auto_both"},
    {"symbol": "ETHUSD", "market": "options", "strategy": "buy_strangle"},
    {"symbol": "SOLUSD", "market": "futures", "strategy": "auto_both"},
    {"symbol": "XRPUSD", "market": "futures", "strategy": "auto_both"},
    {"symbol": "BNBUSD", "market": "futures", "strategy": "auto_both"},
    {"symbol": "DOGEUSD", "market": "futures", "strategy": "auto_both"},
    {"symbol": "ADAUSD", "market": "futures", "strategy": "auto_both"},
    {"symbol": "AVAXUSD", "market": "futures", "strategy": "auto_both"},
]


def _today():
    return str(datetime.now(IST).date())


def _normalized_item(item, rank):
    symbol = str(item.get("symbol") or "").strip().upper()
    market = str(item.get("market") or "futures").strip().lower()
    strategy = str(item.get("strategy") or "auto_both").strip().lower()
    confirmed_date = str(item.get("confirmed_date") or "")
    confirmed = bool(item.get("confirmed")) and confirmed_date == _today()
    return {
        "rank": rank,
        "symbol": symbol,
        "market": market if market in MARKETS else "futures",
        "strategy": strategy if strategy in STRATEGIES else "auto_both",
        "confirmed": confirmed,
        "confirmed_date": _today() if confirmed else "",
        # The current execution engine is BTC-options only. Other rows are
        # chart-capable and remain deliberately ineligible for orders.
        "tradeable": symbol == "BTCUSD" and market == "options",
    }


def load_delta_universe():
    if UNIVERSE_PATH.exists():
        try:
            payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
            rows = payload.get("instruments") if isinstance(payload, dict) else payload
        except (OSError, json.JSONDecodeError):
            rows = None
    else:
        rows = None
    source = rows if isinstance(rows, list) else DEFAULT_UNIVERSE
    instruments = []
    seen = set()
    for item in source:
        row = _normalized_item(item, len(instruments) + 1)
        if not row["symbol"] or row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        instruments.append(row)
    return {"date": _today(), "instruments": instruments}


def save_delta_universe(items):
    rows = []
    seen = set()
    for item in items:
        candidate = dict(item)
        if candidate.get("confirmed"):
            candidate["confirmed_date"] = _today()
        else:
            candidate["confirmed_date"] = ""
        row = _normalized_item(candidate, len(rows) + 1)
        if not row["symbol"] or row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        rows.append(row)
    doc = {"date": _today(), "instruments": rows}
    UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def approved_delta_trade(symbol="BTCUSD"):
    needle = str(symbol).upper()
    for item in load_delta_universe()["instruments"]:
        if item["symbol"] == needle and item["confirmed"] and item["tradeable"]:
            return item
    return None
