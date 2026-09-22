"""Cached real-market overview for the Market Command dashboard."""
from __future__ import annotations

from datetime import datetime, time as clock_time
from threading import Lock
import time
from zoneinfo import ZoneInfo

from broker import get_ltp_batch, resolve_symbol_cached
from charts_feed import _cached_session_bars, _stitch_ltp
from delta_charts_feed import delta_charts

IST = ZoneInfo("Asia/Kolkata")
CACHE_SECONDS = 120
_LOCK = Lock()
_CACHE = {"at": 0.0, "payload": None}

DHAN_MARKETS = (
    {
        "key": "nifty",
        "name": "Nifty 50",
        "short": "NIFTY",
        "symbol": "NIFTY",
        "exchange": "NSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "sensex",
        "name": "Sensex",
        "short": "SENSEX",
        "symbol": "SENSEX",
        "exchange": "BSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "banknifty",
        "name": "Bank Nifty",
        "short": "BANKNIFTY",
        "symbol": "BANKNIFTY",
        "exchange": "NSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "finnifty",
        "name": "FinNifty",
        "short": "FINNIFTY",
        "symbol": "FINNIFTY",
        "exchange": "NSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "bankex",
        "name": "Bankex",
        "short": "BANKEX",
        "symbol": "BANKEX",
        "exchange": "BSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "indiavix",
        "name": "India VIX",
        "short": "INDIA VIX",
        "symbol": "INDIA VIX",
        "exchange": "NSE",
        "instrument": "INDEX",
        "group": "indices",
    },
    {
        "key": "gold",
        "name": "AU Gold",
        "short": "GOLD",
        "symbol": "GOLDM",
        "exchange": "MCX",
        "instrument": "FUTCOM",
        "group": "mcx",
    },
    {
        "key": "silver",
        "name": "AG Silver",
        "short": "SILVER",
        "symbol": "SILVERM",
        "exchange": "MCX",
        "instrument": "FUTCOM",
        "group": "mcx",
    },
    {
        "key": "crude",
        "name": "Crude Oil",
        "short": "CRUDE",
        "symbol": "CRUDEOILM",
        "exchange": "MCX",
        "instrument": "FUTCOM",
        "group": "mcx",
    },
)


def _change(candles, last):
    if not candles or last in (None, 0):
        return None
    try:
        base = float(candles[0]["open"])
        return round((float(last) - base) / base * 100, 2) if base else None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _error_market(item, error):
    return {
        **item,
        "ok": False,
        "last": None,
        "change_pct": None,
        "candles": [],
        "error": str(error),
    }


def _market_open(exchange):
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if str(exchange).upper() == "MCX":
        return clock_time(9, 0) <= now.time() <= clock_time(23, 30)
    return clock_time(9, 15) <= now.time() <= clock_time(15, 30)


def _dhan_markets():
    resolved = []
    instruments = []
    for item in DHAN_MARKETS:
        try:
            instrument = resolve_symbol_cached(
                {
                    "symbol": item["symbol"],
                    "exchange": item["exchange"],
                    "instrument": item["instrument"],
                }
            )
            resolved.append((item, instrument, None))
            instruments.append(instrument)
        except Exception as error:
            resolved.append((item, None, error))

    try:
        prices = get_ltp_batch(instruments) if instruments else {}
    except Exception:
        prices = {}

    rows = []
    for item, instrument, resolution_error in resolved:
        if not instrument:
            rows.append(_error_market(item, resolution_error or "unresolved"))
            continue
        trading_symbol = instrument.get("trading_symbol") or instrument.get("symbol")
        ltp = prices.get(trading_symbol) or prices.get(instrument.get("symbol"))
        try:
            candles = _cached_session_bars(instrument, 90)
            candles = _stitch_ltp(candles, ltp, item["exchange"])
            last = ltp if ltp not in (None, 0) else (
                candles[-1]["close"] if candles else None
            )
            market_open = _market_open(item["exchange"])
            rows.append(
                {
                    **item,
                    "resolved_symbol": trading_symbol,
                    "ok": last is not None,
                    "last": last,
                    "change_pct": _change(candles, last),
                    "candles": candles,
                    "market_open": market_open,
                    "price_label": "Live" if market_open else "Last traded",
                    "error": None,
                }
            )
        except Exception as error:
            rows.append(_error_market(item, error))
    return rows


def _crypto_markets():
    try:
        source = delta_charts(120)
    except Exception as error:
        return [
            {
                "key": "bitcoin",
                "name": "Bitcoin",
                "short": "BTC",
                "symbol": "BTCUSD",
                "group": "crypto",
                "ok": False,
                "last": None,
                "change_pct": None,
                "candles": [],
                "error": str(error),
            }
        ]
    rows = []
    for item in source:
        symbol = str(item.get("symbol") or "")
        short = symbol.replace("USD", "") or symbol
        candles = item.get("candles") or []
        last = item.get("last")
        rows.append(
            {
                "key": "bitcoin" if short == "BTC" else short.lower(),
                "name": "Bitcoin" if short == "BTC" else short,
                "short": short,
                "symbol": symbol,
                "group": "crypto",
                "ok": bool(item.get("live")),
                "last": last,
                "change_pct": _change(candles, last),
                "candles": candles,
                "market_open": True,
                "price_label": "Live",
                "error": item.get("error"),
            }
        )
    return rows


def market_pulse(force=False):
    with _LOCK:
        now = time.monotonic()
        if (
            not force
            and _CACHE["payload"] is not None
            and now - _CACHE["at"] < CACHE_SECONDS
        ):
            return _CACHE["payload"]

        dhan = _dhan_markets()
        crypto = _crypto_markets()
        by_key = {row["key"]: row for row in dhan + crypto}
        payload = {
            "generated_at": datetime.now(IST).isoformat(),
            "primary": [
                by_key.get("bitcoin"),
                by_key.get("nifty"),
                by_key.get("gold"),
            ],
            "indices": [row for row in dhan if row["group"] == "indices"],
            "mcx": [row for row in dhan if row["group"] == "mcx"],
            "crypto": crypto,
            "global_markets": {
                "connected": False,
                "message": "Global-market feed not connected",
                "markets": [],
            },
            "what_matters": {
                "connected": False,
                "message": "Economic-calendar feed not connected",
                "items": [],
            },
            "news": {
                "connected": False,
                "message": "Live-news feed not connected",
                "items": [],
            },
            "sentiment": {
                "connected": False,
                "message": "Sentiment and impact feeds not connected",
            },
        }
        _CACHE["at"] = time.monotonic()
        _CACHE["payload"] = payload
        return payload
