"""Consolidation / trend-break plans. SL from structure, target 1:2, qty from Rs risk."""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import config
from broker import (
    fetch_daily_candles,
    fetch_five_minute_candles,
    fetch_live_session_candles,
    get_quote,
    get_ltp_batch,
    resolve_symbol_cached,
    round_to_tick,
)
from risk import (
    as_five_minute_bars,
    as_resampled_bars,
    as_session_date,
    attach_ist,
    average_true_range,
    opening_range_entry_price,
    session_rows,
)
from watchlist import instrument_requests, load_watchlist

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK = 20
BOX_MAX_WIDTH = 0.15
CHASE_SKIP = {"high": 0.03, "very_high": 0.02, "very high": 0.02}
_OI_BASELINES = {}


def _five_minute_volume_signal(day, completed, current_bucket, now):
    """Estimate current 5m volume pace against the prior 20 completed bars."""
    if "volume" not in day.columns:
        return None, False, "unavailable"
    history = pd.to_numeric(completed["volume"], errors="coerce").dropna().tail(20)
    current = day[day["ist_stamp"] >= current_bucket]
    if history.empty or current.empty:
        return None, False, "unavailable"
    average = float(history.mean())
    current_volume = float(
        pd.to_numeric(current["volume"], errors="coerce").fillna(0).sum()
    )
    if average <= 0:
        return None, False, "unavailable"
    elapsed_minutes = max(
        1.0,
        min(5.0, (now - current_bucket).total_seconds() / 60.0),
    )
    ratio = (current_volume * (5.0 / elapsed_minutes)) / average
    threshold = float(getattr(config, "BREAKOUT_VOLUME_SPIKE_RATIO", 1.5))
    return round(ratio, 2), ratio >= threshold, "spike" if ratio >= threshold else "normal"


def _option_oi_signal(instrument):
    """Compare live option OI with a rolling five-minute baseline."""
    if instrument.get("instrument") not in {"OPTIDX", "OPTSTK", "OPTFUT"}:
        return None, None, False, "unavailable"
    try:
        raw = get_quote(instrument).get("raw") or {}
        oi = float(raw.get("oi") or 0)
    except Exception:
        return None, None, False, "unavailable"
    if oi <= 0:
        return None, None, False, "unavailable"
    key = str(instrument.get("security_id") or instrument.get("trading_symbol"))
    now = datetime.now(IST).timestamp()
    baseline = _OI_BASELINES.get(key)
    if not baseline:
        _OI_BASELINES[key] = {"oi": oi, "at": now}
        return oi, None, False, "baseline"
    base_oi = float(baseline.get("oi") or 0)
    change = ((oi - base_oi) / base_oi * 100.0) if base_oi > 0 else 0.0
    if now - float(baseline.get("at") or 0) >= 300:
        _OI_BASELINES[key] = {"oi": oi, "at": now}
    threshold = float(getattr(config, "BREAKOUT_OI_SPIKE_PCT", 1.0))
    spike = abs(change) >= threshold
    return oi, round(change, 2), spike, "spike" if spike else "normal"


def previous_candle_breakout_plan(
    minute_df,
    session_date,
    signal,
    tick_size,
    entry_override=None,
    max_chase_r=None,
):
    """Buy only after the prior completed 5m high, with its low as SL."""
    if str(signal or "BUY").upper() != "BUY":
        return None, "buy-only"
    if minute_df is None or minute_df.empty:
        return None, "no 5m bars"

    day = session_rows(as_five_minute_bars(minute_df), session_date)
    if day is None or day.empty:
        return None, "no session 5m bars"

    now = pd.Timestamp.now(tz=IST)
    current_bucket = now.floor("5min")
    completed = day[day["ist_stamp"] < current_bucket]
    if completed.empty:
        return None, "WAIT for first completed 5m candle"
    previous = completed.iloc[-1]
    trigger = float(previous["high"])
    sl_raw = float(previous["low"])
    try:
        live = float(entry_override)
    except (TypeError, ValueError):
        live = float(previous["close"])
    if live <= 0:
        return None, "no live price"

    broke = live >= trigger
    base_risk = trigger - sl_raw
    if (
        broke
        and max_chase_r is not None
        and base_risk > 0
        and live - trigger > float(max_chase_r) * base_risk
    ):
        extension_r = (live - trigger) / base_risk
        return None, (
            f"chase skip: price is {extension_r:.2f}R above the fresh breakout"
        )
    entry = round_to_tick(max(live, trigger), tick_size)
    sl = round_to_tick(sl_raw, tick_size)
    if sl >= entry:
        return None, "previous 5m candle has no tradable range after tick rounding"
    risk = entry - sl
    rr = float(getattr(config, "REWARD_RATIO", 3) or 3)
    tp = round_to_tick(entry + rr * risk, tick_size)
    volume_ratio, volume_spike, volume_status = _five_minute_volume_signal(
        day, completed, current_bucket, now
    )
    plan = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": risk,
        "momentum_atr": 1.0,
        "sl_atr": 1.0,
        "score": 100.0,
        "prior_low": sl_raw,
        "prior_high": trigger,
        "prior_time": previous["ist_stamp"].strftime("%H:%M"),
        "sl_source": "previous completed 5m candle low",
        "setup_kind": "previous_candle_breakout",
        "strategy": "breakout",
        "trigger": trigger,
        "broke": broke,
        "alert_state": "BREAKOUT" if broke else "WAIT",
        "breakout_time": int(now.floor("min").timestamp()) if broke else None,
        "volume_ratio_5m": volume_ratio,
        "volume_spike": volume_spike,
        "volume_status": volume_status,
        "oi_change_pct": None,
        "oi_spike": False,
        "oi_status": "unavailable",
        "reward_ratio": rr,
        "risk_rs": float(getattr(config, "CATALYST_RISK_RS", 2000)),
    }
    if not broke:
        return plan, f"WAIT break previous 5m high {trigger:.2f}"
    return plan, "ok"


def first_five_minute_retrace_plan(
    minute_df,
    session_date,
    signal,
    tick_size,
    entry_override=None,
    max_chase_r=None,
    three_minute_df=None,
):
    """First 5m break and retrace, then a completed 3m close above today's high."""
    if str(signal or "BUY").upper() != "BUY":
        return None, "buy-only"
    if minute_df is None or minute_df.empty:
        return None, "no 5m bars"

    day = session_rows(as_five_minute_bars(minute_df), session_date)
    if day is None or day.empty:
        return None, "no session 5m bars"

    first = None
    for _, row in day.iterrows():
        stamp = pd.Timestamp(row["ist_stamp"])
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(IST)
        if stamp.hour > 9 or (stamp.hour == 9 and stamp.minute >= 15):
            first = row
            break
    if first is None:
        return None, "WAIT for the 09:15 5m candle"
    first_open = float(first["open"])
    or_high = float(first["high"])
    or_low = float(first["low"])
    first_close = float(first["close"])
    if first_close <= first_open or or_high <= or_low:
        return None, "WAIT first 5m candle is not bullish"

    now = pd.Timestamp.now(tz=IST)
    current_bucket = now.floor("5min")
    completed = day[day["ist_stamp"] < current_bucket]
    later = completed[completed["ist_stamp"] > first["ist_stamp"]]
    try:
        live = float(entry_override)
    except (TypeError, ValueError):
        live = float(later.iloc[-1]["close"]) if len(later) else first_close
    if live <= 0:
        return None, "no live price"
    if live < or_low:
        return None, f"first 5m low {or_low:.2f} already broken"

    broke = False
    retraced = False
    retrace_stamp = None
    for _, bar in later.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        if not broke and high >= or_high:
            broke = True
            if low <= or_high:
                retraced = True
                retrace_stamp = bar["ist_stamp"]
            continue
        if broke and low <= or_high and low >= or_low:
            retraced = True
            retrace_stamp = bar["ist_stamp"]
    if live >= or_high:
        broke = True
    if broke and or_low <= live <= or_high:
        retraced = True
        retrace_stamp = retrace_stamp or now

    confirm_close = None
    day_high = or_high
    three_src = three_minute_df if three_minute_df is not None and not getattr(three_minute_df, "empty", True) else minute_df
    if (
        retraced
        and bool(getattr(config, "REQUIRE_3M_CLOSE_ABOVE_DAY_HIGH", True))
        and three_src is not None
        and not getattr(three_src, "empty", True)
    ):
        three = as_resampled_bars(three_src, 3)
        three_day = session_rows(three, session_date)
        three_done = three_day[three_day["ist_stamp"] < now.floor("3min")]
        for _, bar in three_done.iterrows():
            prior_high = day_high
            bar_high = float(bar["high"])
            bar_close = float(bar["close"])
            if (
                retrace_stamp is not None
                and bar["ist_stamp"] >= retrace_stamp
                and bar_close > prior_high
            ):
                confirm_close = bar_close
                day_high = prior_high
                break
            day_high = max(day_high, bar_high)
    elif retraced:
        day_high = max(
            or_high,
            float(pd.to_numeric(later["high"], errors="coerce").max()) if len(later) else or_high,
        )

    signal_price = confirm_close if confirm_close is not None else max(live, or_high)
    entry = round_to_tick(signal_price, tick_size)
    sl = round_to_tick(or_low, tick_size)
    if sl >= entry:
        return None, "first 5m candle has no tradable range after tick rounding"
    risk = entry - sl
    rr = float(getattr(config, "REWARD_RATIO", 2) or 2)
    tp = round_to_tick(entry + rr * risk, tick_size)
    volume_ratio, volume_spike, volume_status = _five_minute_volume_signal(
        day, completed, current_bucket, now
    )
    body = first_close - first_open
    rng = or_high - or_low
    score = 50.0 + 50.0 * (body / rng if rng > 0 else 0.0)
    ready = bool(broke and retraced and confirm_close is not None)
    if (
        ready
        and max_chase_r is not None
        and risk > 0
        and live - entry > float(max_chase_r) * risk
    ):
        return None, (
            f"chase skip: price is {(live - entry) / risk:.2f}R above the 3m confirm"
        )
    plan = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": risk,
        "momentum_atr": 1.0,
        "sl_atr": 1.0,
        "score": score,
        "prior_low": or_low,
        "prior_high": or_high,
        "prior_time": first["ist_stamp"].strftime("%H:%M"),
        "sl_source": "first 5m candle low",
        "setup_kind": "first_5m_retrace_3m_close",
        "strategy": "breakout",
        "trigger": day_high,
        "broke": ready,
        "retraced": retraced,
        "alert_state": "BREAKOUT" if ready else "WAIT",
        "breakout_time": int(now.floor("min").timestamp()) if ready else None,
        "volume_ratio_5m": volume_ratio,
        "volume_spike": volume_spike,
        "volume_status": volume_status,
        "oi_change_pct": None,
        "oi_spike": False,
        "oi_status": "unavailable",
        "reward_ratio": rr,
        "risk_rs": float(getattr(config, "CATALYST_RISK_RS", 500)),
    }
    if not broke:
        return plan, f"WAIT break first 5m high {or_high:.2f}"
    if not retraced:
        return plan, f"WAIT retracement to first 5m high {or_high:.2f}"
    if confirm_close is None:
        return plan, f"WAIT 3m close above today's high {day_high:.2f}"
    return plan, "ok"


def range_breakout_plan(
    minute_df,
    session_date,
    signal,
    tick_size,
    entry_override=None,
    max_chase_r=None,
):
    """Break the rolling 5m range; SL is the immediately prior candle low."""
    if str(signal or "BUY").upper() != "BUY":
        return None, "buy-only"
    if minute_df is None or minute_df.empty:
        return None, "no 5m bars"
    day = session_rows(as_five_minute_bars(minute_df), session_date)
    if day is None or day.empty:
        return None, "no session 5m bars"
    now = pd.Timestamp.now(tz=IST)
    current_bucket = now.floor("5min")
    completed = day[day["ist_stamp"] < current_bucket]
    lookback = int(getattr(config, "RANGE_BREAKOUT_LOOKBACK_BARS", 24))
    if len(completed) < min(6, lookback):
        return None, "WAIT for enough completed 5m range bars"
    window = completed.tail(lookback)
    previous = completed.iloc[-1]
    trigger = float(pd.to_numeric(window["high"], errors="coerce").max())
    sl_raw = float(previous["low"])
    try:
        live = float(entry_override)
    except (TypeError, ValueError):
        live = float(previous["close"])
    if live <= 0:
        return None, "no live price"
    broke = live >= trigger
    base_risk = trigger - sl_raw
    if base_risk <= 0:
        return None, "previous candle low is not below range trigger"
    if (
        broke
        and max_chase_r is not None
        and live - trigger > float(max_chase_r) * base_risk
    ):
        return None, f"chase skip: price is {(live - trigger) / base_risk:.2f}R above range"
    entry = round_to_tick(max(live, trigger), tick_size)
    sl = round_to_tick(sl_raw, tick_size)
    if sl >= entry:
        return None, "previous 5m candle has no tradable range after tick rounding"
    risk = entry - sl
    rr = float(getattr(config, "REWARD_RATIO", 3) or 3)
    tp = round_to_tick(entry + rr * risk, tick_size)
    volume_ratio, volume_spike, volume_status = _five_minute_volume_signal(
        day, completed, current_bucket, now
    )
    plan = {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": risk,
        "momentum_atr": 1.0,
        "sl_atr": 1.0,
        "score": 100.0,
        "prior_low": sl_raw,
        "prior_high": float(previous["high"]),
        "prior_time": previous["ist_stamp"].strftime("%H:%M"),
        "sl_source": "previous completed 5m candle low",
        "setup_kind": "range_breakout",
        "strategy": "range_breakout",
        "trigger": trigger,
        "range_bars": len(window),
        "broke": broke,
        "alert_state": "BREAKOUT" if broke else "WAIT",
        "breakout_time": int(now.floor("min").timestamp()) if broke else None,
        "volume_ratio_5m": volume_ratio,
        "volume_spike": volume_spike,
        "volume_status": volume_status,
        "oi_change_pct": None,
        "oi_spike": False,
        "oi_status": "unavailable",
        "reward_ratio": rr,
        "risk_rs": float(getattr(config, "CATALYST_RISK_RS", 1000)),
    }
    if not broke:
        return plan, f"WAIT break {len(window)}-bar range high {trigger:.2f}"
    return plan, "ok"


def _daily_prior(daily_df, session_date):
    if daily_df is None or daily_df.empty:
        return None
    frame = attach_ist(daily_df) if "time" in daily_df.columns else daily_df.copy()
    want = as_session_date(session_date).isoformat()
    if "ist_date" in frame.columns:
        frame = frame.copy()
        frame["ist_date"] = frame["ist_date"].map(lambda value: str(value)[:10])
        prior = frame[frame["ist_date"] < want] if session_date else frame
    else:
        prior = frame
    if len(prior) < 12:
        prior = frame.iloc[:-1] if len(frame) > 12 else frame
    return prior.tail(LOOKBACK + 5)


def classify_setup(daily_df, session_date, last_price):
    prior = _daily_prior(daily_df, session_date)
    if prior is None or len(prior) < 10:
        return None, "not enough daily bars"
    highs = pd.to_numeric(prior["high"], errors="coerce")
    lows = pd.to_numeric(prior["low"], errors="coerce")
    closes = pd.to_numeric(prior["close"], errors="coerce")
    box_high = float(highs.tail(LOOKBACK).max())
    box_low = float(lows.tail(LOOKBACK).min())
    last_close = float(closes.iloc[-1])
    sma = float(closes.tail(20).mean()) if len(closes) >= 10 else last_close
    width = (box_high - box_low) / box_low if box_low > 0 else 9
    price = float(last_price or last_close)
    atr = average_true_range(prior)
    swing_low = float(lows.tail(10).min())

    consolidating = width <= BOX_MAX_WIDTH
    ten_high = float(highs.tail(10).max())
    trigger = box_high if consolidating else ten_high
    broke = price >= trigger * 0.999
    kind = "consolidation_break" if consolidating else "trend_break"
    atr_stop = price - 2.0 * atr if atr and atr > 0 else swing_low
    sl = max(swing_low, atr_stop)
    if sl >= price:
        sl = price - max((atr * 0.75) if atr else price * 0.008, price * 0.004)
    payload = {
        "kind": kind,
        "box_high": box_high,
        "box_low": box_low,
        "width_pct": width,
        "sma": sma,
        "atr": atr,
        "sl_raw": sl,
        "trigger": trigger,
        "broke": broke,
        "consolidating": consolidating,
    }
    if not broke:
        return payload, (
            f"WAIT break {trigger:.2f} ({kind.replace('_', ' ')}, "
            f"box {width:.1%})"
        )
    return payload, "ok"


def structure_entry_plan(
    minute_df,
    session_date,
    signal,
    tick_size,
    daily_df,
    hourly_df=None,
    use_hourly=False,
    chase_risk="medium",
    entry_override=None,
    strategy="auto_structure",
):
    signal = str(signal or "BUY").upper()
    if signal != "BUY":
        return None, "buy-only"
    try:
        entry = float(entry_override) if entry_override is not None else None
    except (TypeError, ValueError):
        entry = None
    if entry is None or entry <= 0:
        entry = opening_range_entry_price(minute_df, session_date)
    if entry is None and daily_df is not None and not daily_df.empty:
        entry = float(daily_df.iloc[-1]["close"])
    if entry is None or entry <= 0:
        return None, "no entry price"
    setup, reason = classify_setup(daily_df, session_date, entry)
    if not setup:
        return None, reason
    strategy = str(strategy or "auto_structure").strip().lower()
    if strategy == "consolidation" and not setup.get("consolidating"):
        return None, "WAIT: stock is not in a consolidation setup"
    if strategy == "trend_break" and setup.get("consolidating"):
        return None, "WAIT: consolidation detected, not a trend-break setup"
    trigger = float(setup.get("trigger") or setup["box_high"])
    live_entry = max(entry, trigger) if not setup.get("broke") else entry
    chase = str(chase_risk or "medium").lower().replace(" ", "_")
    cap = CHASE_SKIP.get(chase)
    if cap is not None and trigger > 0 and setup.get("broke"):
        extension = (entry - trigger) / trigger
        if extension > cap:
            return None, f"chase skip: already {extension:.1%} through break ({chase} risk)"

    rr = float(getattr(config, "REWARD_RATIO", 3) or 3)
    sl = round_to_tick(setup["sl_raw"], tick_size)
    if sl >= live_entry:
        sl = round_to_tick(live_entry * 0.992, tick_size)
    risk = live_entry - sl
    tp = round_to_tick(live_entry + rr * risk, tick_size)
    atr = float(setup["atr"] or 0)
    plan = {
        "entry": round_to_tick(live_entry, tick_size),
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "momentum_atr": 1.0,
        "sl_atr": (risk / atr) if atr else 0.0,
        "score": 100 - int(row_rank_penalty(chase)),
        "prior_low": sl,
        "sl_source": setup["kind"],
        "setup_kind": setup["kind"],
        "strategy": strategy,
        "box_high": setup["box_high"],
        "box_low": setup["box_low"],
        "trigger": trigger,
        "broke": bool(setup.get("broke")),
        "reward_ratio": rr,
        "risk_rs": float(getattr(config, "CATALYST_RISK_RS", 2000)),
    }
    if not setup.get("broke"):
        return plan, reason
    return plan, "ok"


def row_rank_penalty(chase):
    return {"low": 0, "medium": 5, "high": 15, "very_high": 25, "very high": 25}.get(chase, 5)


def analyze_watchlist(session_date=None):
    session_date = as_session_date(session_date)
    doc = load_watchlist()
    source_rows = sorted(
        doc.get("names") or [], key=lambda row: int(row.get("rank") or 99)
    )
    resolved_rows = []
    instruments = []
    for item in source_rows:
        name = item.get("name") or item.get("symbol")
        resolved = None
        last_error = None
        for request in instrument_requests(item):
            try:
                resolved = resolve_symbol_cached(request)
                break
            except Exception as error:
                last_error = error
        resolved_rows.append((item, name, resolved, last_error))
        if resolved:
            instruments.append(resolved)

    try:
        live_prices = get_ltp_batch(instruments) if instruments else {}
    except Exception as error:
        print(f"[CATALYST] Batch LTP failed: {error}")
        live_prices = {}

    rows = []
    for item, name, resolved, last_error in resolved_rows:
        chase = item.get("chase_risk") or "medium"
        strategy = str(item.get("strategy") or "breakout").strip().lower()
        if not resolved:
            rows.append(
                {
                    "rank": item.get("rank"),
                    "name": name,
                    "symbol": item.get("symbol"),
                    "catalyst": item.get("catalyst"),
                    "ok": False,
                    "reason": f"symbol not on Dhan: {last_error}",
                }
            )
            continue
        symbol = resolved["trading_symbol"]
        live_price = (
            live_prices.get(symbol)
            or live_prices.get(resolved.get("symbol"))
            or live_prices.get(resolved.get("display_symbol"))
            or live_prices.get(str(resolved.get("security_id") or ""))
        )
        if not live_price:
            rows.append(
                {
                    "rank": item.get("rank"),
                    "name": name,
                    "symbol": symbol,
                    "input_symbol": item.get("symbol") or name,
                    "exchange": resolved.get("exchange"),
                    "instrument": resolved.get("instrument"),
                    "catalyst": item.get("catalyst"),
                    "chase_risk": chase,
                    "strategy": strategy,
                    "ok": False,
                    "reason": "WAIT: live price temporarily unavailable from Dhan",
                    "qty": 0,
                    "risk_rs": None,
                    "plan": None,
                }
            )
            continue
        five = fetch_five_minute_candles(resolved, bars=80)
        if five is None or five.empty:
            five = fetch_live_session_candles(resolved, interval=5)
        daily = (
            fetch_daily_candles(resolved, bars=40)
            if strategy not in {"breakout", "range_breakout"}
            else None
        )
        live_orb = (
            bool(getattr(config, "LIVE_FIXED_PROTECTION_ENABLED", False))
            and resolved.get("exchange") == "NSE"
            and resolved.get("instrument") == "EQUITY"
            and bool(getattr(config, "REQUIRE_FIRST_5M_RETRACE", True))
        )
        if live_orb or strategy in {"breakout", "range_breakout"}:
            is_option = resolved.get("instrument") in {"OPTIDX", "OPTSTK", "OPTFUT"}
            planner = first_five_minute_retrace_plan
            if not live_orb:
                planner = (
                    range_breakout_plan
                    if strategy == "range_breakout"
                    else previous_candle_breakout_plan
                )
            one_minute = None
            if live_orb:
                from broker import fetch_live_session_candles

                one_minute = fetch_live_session_candles(resolved, interval=1)
            plan, reason = planner(
                five,
                session_date,
                "BUY",
                resolved["tick_size"],
                entry_override=live_price,
                max_chase_r=0.5 if is_option else None,
                **({"three_minute_df": one_minute} if live_orb else {}),
            )
        else:
            plan, reason = structure_entry_plan(
                five,
                session_date,
                "BUY",
                resolved["tick_size"],
                daily,
                chase_risk=chase,
                entry_override=live_price,
                strategy=strategy,
            )
        momentum = opening_range_entry_price(five, session_date)
        mom_pct = None
        if five is not None and not five.empty and momentum:
            try:
                from morning_scan import opening_momentum_from_frame

                mom_pct = opening_momentum_from_frame(five, session_date)
            except Exception:
                mom_pct = None
        risk_rs = float(getattr(config, "CATALYST_RISK_RS", 2000))
        qty = 0
        if plan:
            live_equal_risk = bool(
                getattr(config, "LIVE_FIXED_PROTECTION_ENABLED", False)
            )
            if live_equal_risk and plan.get("entry") is not None and plan.get("sl") is not None:
                entry = float(plan["entry"])
                tick_size = float(resolved["tick_size"])
                sl_dist = abs(entry - float(plan["sl"]))
                rr = float(getattr(config, "REWARD_RATIO", 2) or 2)
                if sl_dist > 0:
                    plan["tp"] = round_to_tick(entry + rr * sl_dist, tick_size)
                plan["trailing_jump"] = 0.0
                plan["reward_ratio"] = rr
            if resolved.get("instrument") == "INDEX":
                # Cash-index volume is synthetic/cumulative in Dhan history.
                plan["volume_ratio_5m"] = None
                plan["volume_spike"] = False
                plan["volume_status"] = "unavailable"
            oi, oi_change, oi_spike, oi_status = _option_oi_signal(resolved)
            plan["oi"] = oi
            plan["oi_change_pct"] = oi_change
            plan["oi_spike"] = oi_spike
            plan["oi_status"] = oi_status
            dist = plan["entry"] - plan["sl"]
            if dist > 0:
                if live_equal_risk:
                    from risk import size_position

                    equity_hint = 0.0
                    try:
                        from broker import get_equity as _get_equity

                        equity_hint = float(_get_equity() or 0)
                    except Exception:
                        equity_hint = 0.0
                    qty, size_reason = size_position(
                        equity_hint,
                        plan["entry"],
                        plan["sl"],
                        resolved.get("lot_size") or 1,
                        book="NSE_EQ",
                        value_multiplier=resolved.get("value_multiplier", 1),
                    )
                    risk_rs = dist * max(qty, 0) if qty else float(
                        getattr(config, "LIVE_TRADE_RISK_RS", config.CATALYST_RISK_RS)
                    )
                    if qty < 1:
                        reason = size_reason
                else:
                    multiplier = max(
                        1.0, float(resolved.get("value_multiplier") or 1.0)
                    )
                    raw_qty = int(risk_rs // (dist * multiplier))
                    lot_size = max(1, int(resolved.get("lot_size") or 1))
                    qty = (raw_qty // lot_size) * lot_size
                    if qty < lot_size:
                        reason = (
                            f"WAIT one lot risks Rs {dist * lot_size * multiplier:.0f}, "
                            f"above Rs {risk_rs:.0f} budget"
                        )
        rows.append(
            {
                "rank": item.get("rank"),
                "name": name,
                "symbol": symbol,
                "input_symbol": item.get("symbol") or name,
                "exchange": resolved.get("exchange"),
                "instrument": resolved.get("instrument"),
                "security_id": resolved.get("security_id"),
                "catalyst": item.get("catalyst"),
                "chase_risk": chase,
                "strategy": strategy,
                "ok": bool(plan) and reason == "ok" and qty > 0,
                "reason": reason,
                "momentum": mom_pct,
                "live_price": live_price,
                "qty": qty,
                "risk_rs": risk_rs if plan else None,
                "plan": plan,
            }
        )
    return rows


def catalyst_candidates(session_date=None):
    from sectors import sector_for_symbol

    analyses = analyze_watchlist(session_date)
    picks = []
    for row in analyses:
        if not row.get("ok") or not row.get("plan"):
            continue
        # Cash indices provide alert context but are not directly orderable.
        if row.get("instrument") == "INDEX":
            continue
        mapped_sector = sector_for_symbol(row.get("symbol") or row.get("name"))
        picks.append(
            {
                "symbol": {
                    "symbol": row["symbol"],
                    "exchange": row.get("exchange") or "NSE",
                    "instrument": row.get("instrument") or "EQUITY",
                    "security_id": row.get("security_id"),
                },
                "display": row["symbol"],
                "sector": mapped_sector or f"WATCH:{row.get('symbol')}",
                "momentum": float(row.get("momentum") or 0.001),
                "kind": "MCX" if row.get("exchange") == "MCX" else (
                    "NSE_FNO"
                    if row.get("instrument") in {"OPTIDX", "OPTSTK"}
                    else "NSE_EQ"
                ),
                "catalyst": True,
                "source": "catalyst",
                "rank": int(row.get("rank") or 99),
                "chase_risk": row.get("chase_risk") or "medium",
                "strategy": row.get("strategy") or "breakout",
                "setup_note": (
                    f"{row.get('name')}: {row['plan'].get('setup_kind')} "
                    f"({row.get('catalyst')}). SL Rs {row.get('risk_rs'):.0f} "
                    f"qty {row.get('qty')} 1:{config.REWARD_RATIO:g}"
                ),
                "analysis": row,
            }
        )
    picks.sort(key=lambda r: r.get("rank", 99))
    return analyses, picks
