"""Automated BTC strangle desk: regime entry, hold-to-target, structure stops."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

import config
from delta_indicators import entry_score, snapshot

IST = None  # set by callers if needed; evaluate uses naive/aware datetimes as given


def nearest_strike(spot, target, available):
    if not available:
        step = 200 if spot > 10000 else 50
        return int(round(target / step) * step)
    return min(available, key=lambda strike: abs(strike - target))


def choose_strikes(spot, atr_value, call_strikes, put_strikes):
    atr_value = max(float(atr_value or 0), spot * config.DELTA_MIN_STRIKE_PCT)
    width = max(
        spot * config.DELTA_MIN_STRIKE_PCT,
        min(spot * config.DELTA_MAX_STRIKE_PCT, config.DELTA_ATR_MULTIPLIER * atr_value),
    )
    call_target = spot + width
    put_target = spot - width
    call = nearest_strike(spot, call_target, call_strikes)
    put = nearest_strike(spot, put_target, put_strikes)
    if call <= spot:
        call = nearest_strike(spot, spot + width, [s for s in call_strikes if s > spot] or [call])
    if put >= spot:
        put = nearest_strike(spot, spot - width, [s for s in put_strikes if s < spot] or [put])
    return int(call), int(put), width


def size_contracts(equity, call_prem, put_prem):
    total = max(float(call_prem) + float(put_prem), 1e-6)
    risk_budget = max(float(equity), 1) * config.DELTA_RISK_PCT
    qty = int(risk_budget / total)
    return max(1, min(qty, int(config.DELTA_MAX_CONTRACTS)))


def realized_iv(frame):
    if frame is None or frame.empty or "close" not in frame.columns:
        return 0.5
    closes = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(closes) < 20:
        return 0.5
    periods = 24.0
    if "time" in frame.columns or "ts" in frame.columns:
        series = frame["ts"] if "ts" in frame.columns else frame["time"]
        try:
            stamp = pd.to_datetime(series, utc=True, errors="coerce").dropna()
            if len(stamp) >= 3:
                delta_sec = stamp.diff().dt.total_seconds().median()
                if delta_sec and delta_sec > 0:
                    periods = 86400.0 / float(delta_sec)
        except Exception:
            periods = 24.0
    vol = closes.pct_change().dropna().std()
    if not vol or math.isnan(float(vol)):
        return 0.5
    return float(max(0.12, min(1.8, float(vol) * math.sqrt(365 * periods))))


def _fractal_pivots(values, left, right, find_min=True):
    pivots = []
    values = list(values)
    n = len(values)
    for index in range(left, n - right):
        window = values[index - left : index + right + 1]
        if not window:
            continue
        if find_min and values[index] <= min(window):
            pivots.append((index, float(values[index])))
        if not find_min and values[index] >= max(window):
            pivots.append((index, float(values[index])))
    return pivots


def last_lower_low(lows, left=None, right=None):
    left = int(left or config.DELTA_SWING_LEFT)
    right = int(right or config.DELTA_SWING_RIGHT)
    pivots = _fractal_pivots(lows, left, right, find_min=True)
    if len(pivots) < 2:
        return float(min(lows)) if len(lows) else None
    confirmed = None
    for prev, curr in zip(pivots, pivots[1:]):
        if curr[1] < prev[1]:
            confirmed = curr[1]
    return confirmed if confirmed is not None else pivots[-1][1]


def last_higher_high(highs, left=None, right=None):
    left = int(left or config.DELTA_SWING_LEFT)
    right = int(right or config.DELTA_SWING_RIGHT)
    pivots = _fractal_pivots(highs, left, right, find_min=False)
    if len(pivots) < 2:
        return float(max(highs)) if len(highs) else None
    confirmed = None
    for prev, curr in zip(pivots, pivots[1:]):
        if curr[1] > prev[1]:
            confirmed = curr[1]
    return confirmed if confirmed is not None else pivots[-1][1]


def structure_levels(frame, atr_value):
    if frame is None or frame.empty:
        return {"stop_low": None, "stop_high": None, "lower_low": None, "higher_high": None}
    lows = pd.to_numeric(frame["low"], errors="coerce").dropna().tolist()
    highs = pd.to_numeric(frame["high"], errors="coerce").dropna().tolist()
    lower_low = last_lower_low(lows)
    higher_high = last_higher_high(highs)
    buffer = max(float(atr_value or 0) * float(config.DELTA_STOP_ATR_BUFFER), 0.0)
    stop_low = (lower_low - buffer) if lower_low is not None else None
    stop_high = (higher_high + buffer) if higher_high is not None else None
    return {
        "lower_low": lower_low,
        "higher_high": higher_high,
        "stop_low": stop_low,
        "stop_high": stop_high,
        "buffer": buffer,
    }


def week_loss_halted(realized, now, equity):
    """True if closed P&L over the last 7 days is at or below the weekly cap."""
    cap = max(float(equity), 1.0) * float(config.DELTA_WEEKLY_LOSS_PCT)
    if not realized:
        return False, 0.0
    cutoff = now - timedelta(days=7)
    total = 0.0
    for row in realized:
        stamp = row.get("time")
        pnl = float(row.get("pnl") or 0)
        if stamp is None:
            continue
        if hasattr(stamp, "to_pydatetime"):
            stamp = stamp.to_pydatetime()
        try:
            if stamp >= cutoff:
                total += pnl
        except TypeError:
            continue
    return total <= -cap, total


def pick_regime(snap, score, iv):
    if score["total"] < config.DELTA_ENTRY_SCORE_THRESHOLD:
        return None, f"score {score['total']} < {config.DELTA_ENTRY_SCORE_THRESHOLD}"
    if iv >= config.DELTA_HIGH_IV_FLAT:
        return None, f"iv {iv:.2f} already expanded — stay flat"
    compressing = snap.get("breakout") == "COMPRESSING_WITHIN_A_RANGE"
    breaking = snap.get("breakout") in ("BREAKING_UP", "BREAKING_DOWN")
    expanding = snap.get("vol_ratio", 1) > 1.02
    if breaking or expanding:
        return "long", "expansion/breakout long strangle"
    if (
        getattr(config, "DELTA_ALLOW_SHORT", True)
        and compressing
        and iv <= config.DELTA_LOW_IV
    ):
        return "short", "compressed low-IV short strangle"
    return None, "no regime (need compression for short or expansion for long)"


def pnl_pct_for_side(side, qty, entry_call, entry_put, mark_call, mark_put):
    entry_value = qty * (entry_call + entry_put)
    mark_value = qty * (mark_call + mark_put)
    if not entry_value:
        return 0.0
    if side == "short":
        return (entry_value - mark_value) / entry_value * 100
    return (mark_value - entry_value) / entry_value * 100


def structure_stop_hit(side, breakout, bar_close, stop_low, stop_high):
    if bar_close is None:
        return False, ""
    close = float(bar_close)
    if side == "short":
        if stop_low is not None and close <= float(stop_low):
            return True, "structure_stop_low"
        if stop_high is not None and close >= float(stop_high):
            return True, "structure_stop_high"
        return False, ""
    if breakout == "BREAKING_DOWN":
        if stop_high is not None and close >= float(stop_high):
            return True, "structure_stop_high"
        return False, ""
    if stop_low is not None and close <= float(stop_low):
        return True, "structure_stop_low"
    return False, ""


def should_exit(position, mark_call, mark_put, snap, now=None, bar_close=None, minutes_to_expiry=None):
    now = now or datetime.now()
    side = position.get("side") or "long"
    pnl_pct = pnl_pct_for_side(
        side,
        position["qty"],
        position["call_prem"],
        position["put_prem"],
        mark_call,
        mark_put,
    )
    if pnl_pct >= config.DELTA_PROFIT_TARGET_PCT:
        return True, "profit_target", pnl_pct
    if pnl_pct <= -config.DELTA_EMERGENCY_PREMIUM_STOP_PCT:
        return True, "premium_stop", pnl_pct
    hit, why = structure_stop_hit(
        side,
        position.get("breakout"),
        bar_close if bar_close is not None else snap.get("spot"),
        position.get("stop_low"),
        position.get("stop_high"),
    )
    if hit:
        return True, why, pnl_pct
    if minutes_to_expiry is not None and minutes_to_expiry <= config.DELTA_FLATTEN_MINUTES_BEFORE_EXPIRY:
        return True, "expiry_flatten", pnl_pct
    opened = position.get("opened_at")
    if opened is not None:
        if getattr(opened, "tzinfo", None) and getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=opened.tzinfo)
        try:
            held = (now - opened).total_seconds() / 60
        except TypeError:
            held = 0
        red_hold = int(getattr(config, "DELTA_TIME_EXIT_IF_RED_MINUTES", 360))
        if held >= red_hold and pnl_pct < 5:
            return True, "time_exit_red", pnl_pct
        if held >= config.DELTA_MAX_HOLDING_MINUTES:
            return True, "time_exit", pnl_pct
    return False, "hold", pnl_pct


def perp_signal(snap):
    if not config.DELTA_ENABLE_PERP:
        return "FLAT", "perp module off"
    expanding = snap["vol_ratio"] > 1.05 and snap["volume_ratio"] > 1.1
    if snap["ema9"] > snap["ema21"] and snap["breakout"] == "BREAKING_UP" and expanding:
        return "LONG", "ema stack + breakout up"
    if snap["ema9"] < snap["ema21"] and snap["breakout"] == "BREAKING_DOWN" and expanding:
        return "SHORT", "ema stack + breakout down"
    return "FLAT", "no directional edge"


def evaluate(candles, spot, products, equity, open_position, hour=None, iv=None):
    snap = snapshot(candles, spot)
    score = entry_score(snap, hour=hour)
    iv = realized_iv(candles) if iv is None else iv
    structure = structure_levels(candles, snap.get("atr"))
    btc_opts = [
        item
        for item in (products or [])
        if str(item.get("symbol", "")).startswith(("C-BTC-", "P-BTC-"))
        and str(item.get("state", "live")).lower() in ("live", "active", "")
    ]
    calls = []
    puts = []
    for item in btc_opts:
        try:
            strike = int(str(item["symbol"]).split("-")[2])
        except (IndexError, ValueError, KeyError):
            continue
        if str(item["symbol"]).startswith("C-"):
            calls.append(strike)
        else:
            puts.append(strike)
    call_k, put_k, width = choose_strikes(snap["spot"], snap["atr"], calls, puts)
    if open_position:
        side, reason = None, "position already open"
    else:
        side, reason = pick_regime(snap, score, iv)
    enter = side in ("long", "short")
    return {
        "snapshot": snap,
        "score": score,
        "iv": iv,
        "structure": structure,
        "enter": enter,
        "side": side,
        "reason": reason,
        "call_strike": call_k,
        "put_strike": put_k,
        "width": width,
        "qty": size_contracts(equity, max(snap["atr"] * 0.02, 5), max(snap["atr"] * 0.02, 5)),
        "perp": perp_signal(snap),
    }
