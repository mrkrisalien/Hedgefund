"""BTC indicators used by the reconstructed Delta strategy."""
from __future__ import annotations

import math

import pandas as pd


def candles_to_frame(rows):
    if not rows:
        return pd.DataFrame()
    if isinstance(rows, dict):
        rows = rows.get("result") or rows.get("candles") or []
    if rows and isinstance(rows[0], (list, tuple)) and len(rows[0]) >= 5:
        rows = [
            {
                "time": item[0],
                "open": item[1],
                "high": item[2],
                "low": item[3],
                "close": item[4],
                "volume": item[5] if len(item) > 5 else 0,
            }
            for item in rows
        ]
    frame = pd.DataFrame(rows)
    rename = {}
    for column in frame.columns:
        lower = str(column).lower()
        if lower in ("time", "timestamp", "t"):
            rename[column] = "time"
        elif lower in ("open", "o"):
            rename[column] = "open"
        elif lower in ("high", "h"):
            rename[column] = "high"
        elif lower in ("low", "l"):
            rename[column] = "low"
        elif lower in ("close", "c"):
            rename[column] = "close"
        elif lower in ("volume", "v"):
            rename[column] = "volume"
    frame = frame.rename(columns=rename)
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["close"]).reset_index(drop=True)


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, math.nan)
    return 100 - (100 / (1 + rs))


def atr(frame, period=14):
    high = frame["high"]
    low = frame["low"]
    close = frame["close"]
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    return float(tr.tail(period).mean())


def snapshot(frame, spot):
    if frame is None or frame.empty or len(frame) < 30:
        return {
            "spot": spot,
            "atr": spot * 0.012 if spot else 0,
            "avg_atr": spot * 0.012 if spot else 0,
            "vol_ratio": 1.0,
            "ema9": spot,
            "ema21": spot,
            "ema50": spot,
            "rsi": 50,
            "volume_ratio": 1.0,
            "range_high": spot,
            "range_low": spot,
            "trend": "NEUTRAL",
            "breakout": "COMPRESSING_WITHIN_A_RANGE",
        }

    close = frame["close"]
    ema9 = float(ema(close, 9).iloc[-1])
    ema21 = float(ema(close, 21).iloc[-1])
    ema50 = float(ema(close, 50).iloc[-1]) if len(frame) >= 50 else ema21
    current_atr = atr(frame)
    avg_atr = atr(frame.tail(80), period=40) if len(frame) > 40 else current_atr
    vol_ratio = (current_atr / avg_atr) if avg_atr else 1.0
    volume = frame["volume"] if "volume" in frame.columns else pd.Series([1] * len(frame))
    volume_ratio = float(volume.iloc[-1] / max(volume.tail(20).mean(), 1e-9))
    hour = frame.tail(12)
    range_high = float(hour["high"].max())
    range_low = float(hour["low"].min())
    last = float(close.iloc[-1])
    if ema9 > ema21 > ema50 and last > ema9:
        trend = "STRONG_UPTREND"
    elif ema9 > ema21:
        trend = "UPTREND"
    elif ema9 < ema21 < ema50 and last < ema9:
        trend = "STRONG_DOWNTREND"
    elif ema9 < ema21:
        trend = "DOWNTREND"
    else:
        trend = "NEUTRAL"
    if last >= range_high:
        breakout = "BREAKING_UP"
    elif last <= range_low:
        breakout = "BREAKING_DOWN"
    else:
        breakout = "COMPRESSING_WITHIN_A_RANGE"
    return {
        "spot": last,
        "atr": current_atr,
        "avg_atr": avg_atr,
        "vol_ratio": vol_ratio,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "rsi": float(rsi(close).iloc[-1] or 50),
        "volume_ratio": volume_ratio,
        "range_high": range_high,
        "range_low": range_low,
        "trend": trend,
        "breakout": breakout,
    }


def entry_score(snap, hour=None):
    vol = min(40, max(0, (snap["vol_ratio"] - 0.8) * 40))
    if snap["breakout"] == "COMPRESSING_WITHIN_A_RANGE" and snap["vol_ratio"] > 1:
        brk = 28
    elif snap["breakout"] in ("BREAKING_UP", "BREAKING_DOWN"):
        brk = 22
    else:
        brk = 10
    trend = 8 if snap["trend"] == "NEUTRAL" else 12
    volume = min(15, snap["volume_ratio"] * 8)
    clock = 10
    if hour in (0, 9, 13, 14, 15, 18, 19, 22):
        clock = 18
    return {
        "volatility": round(vol, 1),
        "breakout": brk,
        "trend": trend,
        "volume": round(volume, 1),
        "time_of_day": clock,
        "total": round(vol + brk + trend + volume + clock, 1),
    }
