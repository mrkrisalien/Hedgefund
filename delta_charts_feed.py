"""Cached multi-asset charts for the Delta desk."""
from __future__ import annotations

import time
from threading import Lock

from delta_client import DeltaClient
from delta_indicators import candles_to_frame
from delta_universe import load_delta_universe

CANDLE_CACHE_SECONDS = 60
RESPONSE_CACHE_SECONDS = 4
_LOCK = Lock()
_CANDLES = {}
_RESPONSE = {"at": 0.0, "charts": None}


def invalidate_delta_chart_cache():
    with _LOCK:
        _CANDLES.clear()
        _RESPONSE["at"] = 0.0
        _RESPONSE["charts"] = None


def _bars(rows):
    frame = candles_to_frame(rows)
    if frame.empty:
        return []
    out = []
    for _, row in frame.iterrows():
        try:
            stamp = int(float(row["time"]))
            if stamp > 10_000_000_000:
                stamp //= 1000
            out.append(
                {
                    "time": stamp,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda item: item["time"])
    return out


def delta_charts(limit=120):
    with _LOCK:
        now = time.monotonic()
        if (
            _RESPONSE["charts"] is not None
            and now - _RESPONSE["at"] < RESPONSE_CACHE_SECONDS
        ):
            return _RESPONSE["charts"]
        charts = _build_charts(limit)
        _RESPONSE["at"] = time.monotonic()
        _RESPONSE["charts"] = charts
        return charts


def _build_charts(limit):
    client = DeltaClient()
    charts = []
    now = time.monotonic()
    for item in load_delta_universe()["instruments"]:
        symbol = item["symbol"]
        cached = _CANDLES.get(symbol)
        candles = list(cached["candles"]) if cached else []
        error = None
        if not cached or now - cached["at"] >= CANDLE_CACHE_SECONDS:
            try:
                candles = _bars(client.candles(symbol, "5m", limit))[-int(limit) :]
                if candles:
                    _CANDLES[symbol] = {"at": now, "candles": candles}
            except Exception as exc:
                error = str(exc)
        last = candles[-1]["close"] if candles else None
        try:
            ticker = client.ticker(symbol)
            if isinstance(ticker, dict):
                last = float(
                    ticker.get("mark_price")
                    or ticker.get("close")
                    or ticker.get("spot_price")
                    or last
                )
        except Exception as exc:
            error = error or str(exc)
        charts.append(
            {
                **item,
                "name": symbol,
                "ok": bool(candles),
                "last": last,
                "live": last is not None,
                "error": error,
                "candles": candles,
            }
        )
    return charts
