"""Live OHLC packs for the dashboard chart grid (NSE session, IST)."""
from __future__ import annotations

from datetime import datetime, time as clock_time
from threading import Lock
import time
from zoneinfo import ZoneInfo

import pandas as pd

from broker import fetch_live_session_candles, get_ltp_batch, resolve_symbol_cached
from watchlist import instrument_requests, load_watchlist

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = clock_time(9, 15)
NSE_CLOSE = clock_time(15, 30)
MCX_OPEN = clock_time(9, 0)
MCX_CLOSE = clock_time(23, 30)
CANDLE_CACHE_SECONDS = 120
RESPONSE_CACHE_SECONDS = 4
HISTORY_SPACING_SECONDS = 0.75

_CACHE_LOCK = Lock()
_CANDLE_CACHE = {}
_RESPONSE_CACHE = {"at": 0.0, "charts": None}


def invalidate_chart_cache():
    """Drop removed symbols and force the next dashboard request to rebuild."""
    with _CACHE_LOCK:
        _CANDLE_CACHE.clear()
        _RESPONSE_CACHE["at"] = 0.0
        _RESPONSE_CACHE["charts"] = None


def _to_ist(ts):
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert(IST)


def _unix_utc(ts):
    return int(_to_ist(ts).timestamp())


def _session_hours(exchange):
    if str(exchange or "").upper() == "MCX":
        return MCX_OPEN, MCX_CLOSE
    return NSE_OPEN, NSE_CLOSE


def _in_exchange_hours(ist_stamp, exchange):
    t = ist_stamp.time()
    session_open, session_close = _session_hours(exchange)
    return session_open <= t <= session_close


def _exchange_bar_unix(exchange, now=None):
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    session_open, session_close = _session_hours(exchange)
    if now.time() < session_open:
        now = now.replace(
            hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0
        )
    elif now.time() > session_close:
        now = now.replace(
            hour=session_close.hour,
            minute=session_close.minute,
            second=0,
            microsecond=0,
        )
    else:
        now = now.replace(second=0, microsecond=0)
    return int(now.timestamp())


def _bars(frame, exchange):
    if frame is None or frame.empty:
        return []
    out = []
    seen = set()
    for _, row in frame.iterrows():
        ist = _to_ist(row["time"])
        if not _in_exchange_hours(ist, exchange):
            continue
        t = int(ist.replace(second=0, microsecond=0).timestamp())
        if t in seen:
            continue
        seen.add(t)
        try:
            out.append(
                {
                    "time": t,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda item: item["time"])
    return out


def _stitch_ltp(candles, ltp, exchange="NSE"):
    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        return candles
    if ltp <= 0:
        return candles
    now = datetime.now(IST)
    session_open, session_close = _session_hours(exchange)
    market_open = (
        now.weekday() < 5
        and session_open <= now.time() <= session_close
    )
    # Never manufacture a future opening bar before the exchange opens, or a
    # new bar after it closes. The card may still show the broker's LTP, while
    # the chart remains anchored to actual completed candles.
    if not market_open and candles:
        return candles
    bar = _exchange_bar_unix(exchange)
    if not candles:
        return [{"time": bar, "open": ltp, "high": ltp, "low": ltp, "close": ltp}]
    last = dict(candles[-1])
    if last["time"] == bar:
        last["close"] = ltp
        last["high"] = max(last["high"], ltp)
        last["low"] = min(last["low"], ltp)
        return candles[:-1] + [last]
    prev_close = last["close"]
    return candles + [
        {
            "time": bar,
            "open": prev_close,
            "high": max(prev_close, ltp),
            "low": min(prev_close, ltp),
            "close": ltp,
        }
    ]


def _cached_session_bars(instrument, bars):
    symbol = instrument.get("trading_symbol") or instrument.get("symbol")
    today = str(datetime.now(IST).date())
    now = time.monotonic()
    cached = _CANDLE_CACHE.get(symbol)
    if (
        cached
        and cached.get("date") == today
        and now - cached.get("at", 0) < CANDLE_CACHE_SECONDS
    ):
        return list(cached["candles"])

    try:
        frame = fetch_live_session_candles(instrument, interval=1)
        # Historical candles are a low-rate endpoint. Space the eight
        # watchlist requests even though they now run in one single flight.
        time.sleep(HISTORY_SPACING_SECONDS)
        candles = _bars(frame, instrument.get("exchange"))[-int(bars or 120) :]
        if candles:
            _CANDLE_CACHE[symbol] = {
                "date": today,
                "at": now,
                "candles": candles,
            }
            return list(candles)
    except Exception:
        if cached and cached.get("date") == today:
            return list(cached["candles"])
        raise

    if cached and cached.get("date") == today:
        return list(cached["candles"])
    return []


def watchlist_charts(bars=120):
    # Multiple dashboard tabs poll concurrently. Only one request may refresh
    # Dhan; all others reuse the shared response/candle caches.
    with _CACHE_LOCK:
        now = time.monotonic()
        if (
            _RESPONSE_CACHE["charts"] is not None
            and now - _RESPONSE_CACHE["at"] < RESPONSE_CACHE_SECONDS
        ):
            return _RESPONSE_CACHE["charts"]

        charts = _build_watchlist_charts(bars)
        _RESPONSE_CACHE["at"] = time.monotonic()
        _RESPONSE_CACHE["charts"] = charts
        return charts


def _build_watchlist_charts(bars=120):
    names = load_watchlist().get("names") or []
    resolved_rows = []
    instruments = []
    for item in sorted(names, key=lambda row: int(row.get("rank") or 99)):
        name = item.get("name") or item.get("symbol")
        resolved = None
        error = None
        for request in instrument_requests(item):
            try:
                resolved = resolve_symbol_cached(request)
                break
            except Exception as exc:
                error = str(exc)
        resolved_rows.append((item, name, resolved, error))
        if resolved:
            instruments.append(resolved)

    prices = {}
    if instruments:
        try:
            prices = get_ltp_batch(instruments) or {}
        except Exception:
            prices = {}

    charts = []
    for item, name, resolved, error in resolved_rows:
        if not resolved:
            charts.append(
                {
                    "rank": item.get("rank"),
                    "name": name,
                    "symbol": item.get("symbol") or "",
                    "ok": False,
                    "error": error or "unresolved",
                    "candles": [],
                    "live": False,
                }
            )
            continue
        symbol = resolved.get("trading_symbol") or item.get("symbol")
        ltp = prices.get(symbol) or prices.get(resolved.get("symbol"))
        try:
            candles = _cached_session_bars(resolved, bars)
            candles = _stitch_ltp(candles, ltp, resolved.get("exchange"))
            last = ltp if ltp not in (None, 0) else (
                candles[-1]["close"] if candles else None
            )
            charts.append(
                {
                    "rank": item.get("rank"),
                    "name": name,
                    "symbol": symbol,
                    "exchange": resolved.get("exchange"),
                    "instrument": resolved.get("instrument"),
                    "ok": True,
                    "error": None,
                    "last": last,
                    "live": True,
                    "candles": candles,
                }
            )
        except Exception as exc:
            candles = _stitch_ltp([], ltp, resolved.get("exchange"))
            charts.append(
                {
                    "rank": item.get("rank"),
                    "name": name,
                    "symbol": symbol,
                    "exchange": resolved.get("exchange"),
                    "instrument": resolved.get("instrument"),
                    "ok": bool(candles),
                    "error": str(exc),
                    "last": ltp,
                    "live": bool(candles),
                    "candles": candles,
                }
            )
    return charts
