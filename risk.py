import pandas as pd
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import config
from broker import round_to_tick

IST = ZoneInfo("Asia/Kolkata")


def true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(daily_df, period=None):
    period = period or config.ATR_PERIOD
    frame = daily_df.copy().reset_index(drop=True)
    if frame is None or len(frame) < 2:
        if frame is None or frame.empty:
            return 0.0
        last = frame.iloc[-1]
        return float(last["high"] - last["low"])

    ranges = []
    for index in range(1, len(frame)):
        prev_close = float(frame.iloc[index - 1]["close"])
        row = frame.iloc[index]
        ranges.append(
            true_range(float(row["high"]), float(row["low"]), prev_close)
        )

    if not ranges:
        last = frame.iloc[-1]
        return float(last["high"] - last["low"])

    window = ranges[-period:]
    return float(sum(window) / len(window))


def stop_target_prices(entry, signal, daily_df, tick_size, hourly_df=None, use_hourly=False):
    if use_hourly and hourly_df is not None and len(hourly_df) >= 3:
        atr = average_true_range(hourly_df, period=config.ATR_PERIOD)
        sl_mult = config.HOURLY_SL_ATR_MULT
        min_frac = 0.0015
    else:
        atr = average_true_range(daily_df)
        sl_mult = config.SL_ATR_MULT
        min_frac = 0.004

    sl_distance = max(atr * sl_mult, entry * min_frac, tick_size * 4)
    tp_distance = sl_distance * config.REWARD_RATIO

    if signal == "BUY":
        sl = round_to_tick(entry - sl_distance, tick_size)
        tp = round_to_tick(entry + tp_distance, tick_size)
        if sl >= entry:
            sl = round_to_tick(entry - tick_size * 4, tick_size)
        if tp <= entry:
            tp = round_to_tick(entry + tick_size * 8, tick_size)
    else:
        sl = round_to_tick(entry + sl_distance, tick_size)
        tp = round_to_tick(entry - tp_distance, tick_size)
        if sl <= entry:
            sl = round_to_tick(entry + tick_size * 4, tick_size)
        if tp >= entry:
            tp = round_to_tick(entry - tick_size * 8, tick_size)

    return sl, tp, atr


def as_session_date(value):
    if value is None:
        return datetime.now(IST).date()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(IST).date()
        return value.date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return datetime.now(IST).date()


def attach_ist(frame):
    out = frame.copy()
    stamps = pd.to_datetime(out["time"], utc=True)
    ist = stamps.dt.tz_convert(IST)
    out["ist_stamp"] = ist
    out["ist_date"] = ist.dt.strftime("%Y-%m-%d")
    out["ist_time"] = ist.dt.time
    return out


def session_rows(minute_df, session_date):
    if minute_df is None or getattr(minute_df, "empty", True):
        return minute_df
    frame = attach_ist(minute_df)
    want = as_session_date(session_date).isoformat()
    return frame[frame["ist_date"] == want].sort_values("ist_stamp")


def opening_range_entry_price(minute_df, session_date):
    if minute_df is None or minute_df.empty:
        return None
    day = session_rows(as_five_minute_bars(minute_df), session_date)
    if day is None or day.empty:
        return None
    opening = day[
        (day["ist_time"] >= dtime(9, 15)) & (day["ist_time"] <= dtime(9, 20))
    ]
    if opening.empty:
        return None
    return float(opening.iloc[-1]["close"])


def atr_entry_plan(minute_df, session_date, signal, tick_size, daily_df, hourly_df=None, use_hourly=False):
    entry = opening_range_entry_price(minute_df, session_date)
    if entry is None:
        return None, "no 9:15-9:20 bar"
    sl, tp, atr = stop_target_prices(
        entry, signal, daily_df, tick_size, hourly_df=hourly_df, use_hourly=use_hourly
    )
    open_px = entry
    frame = as_five_minute_bars(minute_df)
    if frame is not None and not frame.empty:
        day = session_rows(frame, session_date)
        opening = day[
            (day["ist_time"] >= dtime(9, 15)) & (day["ist_time"] <= dtime(9, 20))
        ]
        if not opening.empty:
            open_px = float(opening.iloc[0]["open"])
            entry = round_to_tick(float(opening.iloc[-1]["close"]), tick_size)
            sl, tp, atr = stop_target_prices(
                entry, signal, daily_df, tick_size, hourly_df=hourly_df, use_hourly=use_hourly
            )
    sl_dist = abs(entry - sl)
    mom = abs(entry - open_px)
    mom_atr = mom / atr if atr else 0.0
    sl_atr = sl_dist / atr if atr else 0.0
    min_mom = float(getattr(config, "MOMENTUM_ATR_MIN", 0.15))
    if mom_atr < min_mom:
        return None, f"momentum/ATR {mom_atr:.2f} < {min_mom:.2f}"
    return {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "momentum_atr": round(mom_atr, 3),
        "sl_atr": round(sl_atr, 3),
        "score": mom_atr / max(sl_atr, 0.05),
        "prior_low": sl,
        "sl_source": "daily ATR stop",
    }, "ok"


def build_entry_plan(minute_df, session_date, signal, tick_size, daily_df, hourly_df=None, use_hourly=False):
    if getattr(config, "USE_STRUCTURE_SETUP", False):
        from catalyst_setup import structure_entry_plan

        return structure_entry_plan(
            minute_df,
            session_date,
            signal,
            tick_size,
            daily_df,
            hourly_df=hourly_df,
            use_hourly=use_hourly,
        )
    if getattr(config, "USE_FIVE_MINUTE_STOP", False):
        return five_minute_entry_plan(
            minute_df, session_date, signal, tick_size, daily_df=daily_df
        )
    return atr_entry_plan(
        minute_df,
        session_date,
        signal,
        tick_size,
        daily_df,
        hourly_df=hourly_df,
        use_hourly=use_hourly,
    )


def daily_sma(daily_df, period=10):
    closes = daily_df["close"].astype(float)
    window = closes.tail(period)
    return float(window.mean())


def trend_allows(signal, daily_df):
    close = float(daily_df.iloc[-1]["close"])
    sma = daily_sma(daily_df)
    if signal == "BUY":
        return close >= sma
    if signal == "SELL":
        return close <= sma
    return False


def signal_gate(signal, confidence, daily_df):
    signal = str(signal).strip().upper()
    if signal not in ("BUY", "SELL"):
        return False, "HOLD"

    if getattr(config, "USE_CATALYST_WATCHLIST", False) and getattr(
        config, "USE_STRUCTURE_SETUP", False
    ):
        if signal != "BUY":
            return False, "buy-only"
        if confidence < config.MIN_CONFIDENCE:
            return False, f"confidence {confidence:.0f} < {config.MIN_CONFIDENCE}"
        return True, "ok"

    if confidence < config.MIN_CONFIDENCE:
        return False, f"confidence {confidence:.0f} < {config.MIN_CONFIDENCE}"

    if not trend_allows(signal, daily_df):
        return False, "against daily SMA trend"

    return True, "ok"


def size_position(
    equity,
    entry,
    sl,
    lot_size=1,
    book="NSE_EQ",
    value_multiplier=1.0,
):
    lot_size = max(1, int(lot_size or 1))
    distance = abs(float(entry) - float(sl))
    value_multiplier = max(1.0, float(value_multiplier or 1.0))
    risk_per_unit = distance * value_multiplier
    if distance <= 0 or equity <= 0:
        return 0, "invalid stop or equity"

    rupee = float(
        getattr(config, "LIVE_TRADE_RISK_RS", 0)
        or getattr(config, "CATALYST_RISK_RS", 0)
        or 0
    )
    if rupee > 0 and getattr(config, "USE_CATALYST_WATCHLIST", False):
        quantity = int(rupee // risk_per_unit)
        quantity = (quantity // lot_size) * lot_size
        if quantity < lot_size:
            return 0, (
                f"skip: Rs {rupee:.0f} risk cannot cover 1 lot; "
                f"stop risk is Rs {risk_per_unit * lot_size:.2f}"
            )
        slots = max(1, int(getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10) or 10))
        slot_notional = float(equity) / slots
        notional = quantity * float(entry) * value_multiplier
        if slot_notional > 0 and notional > slot_notional:
            quantity = int(slot_notional // (float(entry) * value_multiplier))
            quantity = (quantity // lot_size) * lot_size
            if quantity < lot_size:
                return 0, (
                    f"skip: 1 lot notional Rs {float(entry) * lot_size * value_multiplier:.0f} "
                    f"exceeds 1/{slots} capital Rs {slot_notional:.0f}"
                )
        return quantity, (
            f"ok Rs {min(rupee, quantity * risk_per_unit):.0f} risk -> {quantity} units"
        )

    if str(book).upper() == "MCX":
        lots = max(1, int(getattr(config, "MCX_LOTS", 1)))
        return lots * lot_size, "mcx 1-lot book (independent of cash sizing)"

    risk_budget = equity * config.CASH_RISK_PCT
    max_lot_risk = equity * config.CASH_MAX_SINGLE_LOT_RISK_PCT
    risk_one_lot = risk_per_unit * lot_size
    if risk_one_lot > max_lot_risk:
        return 0, (
            f"skip cash: 1 lot risk Rs {risk_one_lot:.0f} exceeds "
            f"{config.CASH_MAX_SINGLE_LOT_RISK_PCT:.2%} cash cap"
        )

    quantity = int(risk_budget // risk_per_unit)
    quantity = (quantity // lot_size) * lot_size
    if quantity < lot_size:
        return 0, "skip cash: cannot fit 1 lot inside cash risk budget"
    return quantity, "ok"


def book_day_loss_pct(book):
    if str(book).upper() == "MCX":
        return config.MCX_MAX_DAY_LOSS_PCT
    return config.CASH_MAX_DAY_LOSS_PCT


def book_loss_hit(day_start_equity, book_pnl, book="NSE_EQ"):
    if day_start_equity <= 0:
        return False
    return -float(book_pnl) >= day_start_equity * book_day_loss_pct(book)


def session_loss_hit(day_pnl, cap_rs=None):
    """True when combined session P&L has hit the rupee stop."""
    cap = float(cap_rs if cap_rs is not None else getattr(config, "MAX_DAY_LOSS_RS", 0) or 0)
    if cap <= 0:
        return False
    return -float(day_pnl) >= cap


def session_profit_hit(day_pnl, cap_rs=None):
    """True when combined session P&L has hit the rupee take-profit square-off."""
    cap = float(cap_rs if cap_rs is not None else getattr(config, "MAX_DAY_PROFIT_RS", 0) or 0)
    if cap <= 0:
        return False
    return float(day_pnl) >= cap


def session_square_off_reason(day_pnl):
    """'loss', 'profit', or None. Same flatten-all behaviour for both."""
    if session_loss_hit(day_pnl):
        return "loss"
    if session_profit_hit(day_pnl):
        return "profit"
    return None


def day_loss_hit(day_start_equity, equity, book="NSE_EQ", book_pnl=None):
    if book_pnl is not None:
        return book_loss_hit(day_start_equity, book_pnl, book)
    if day_start_equity <= 0:
        return False
    loss = day_start_equity - equity
    return loss >= day_start_equity * book_day_loss_pct(book)


def intraday_costs(entry, exit_price, quantity):
    """Dhan-style equity intraday estimate: brokerage cap + STT + other charges."""
    buy_value = abs(entry * quantity)
    sell_value = abs(exit_price * quantity)
    brokerage = min(config.BROKERAGE_CAP, config.BROKERAGE_RATE * buy_value) + min(
        config.BROKERAGE_CAP,
        config.BROKERAGE_RATE * sell_value,
    )
    stt = config.STT_SELL_RATE * sell_value
    other = config.OTHER_CHARGE_RATE * (buy_value + sell_value)
    return brokerage + stt + other


def as_resampled_bars(minute_df, minutes=5):
    if minute_df is None or minute_df.empty:
        return minute_df
    minutes = max(1, int(minutes or 1))
    frame = minute_df.copy()
    stamps = pd.to_datetime(frame["time"], utc=True)
    if minutes == 1:
        out = frame.copy()
        out["time"] = stamps
        return out
    if len(frame) >= 2:
        delta = stamps.sort_values().diff().median()
        if delta is None or delta <= pd.Timedelta(minutes=minutes):
            indexed = frame.set_index(stamps)
            aggregations = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
            }
            if "volume" in indexed.columns:
                aggregations["volume"] = "sum"
            out = (
                indexed.resample(f"{minutes}min", origin="start_day")
                .agg(aggregations)
                .dropna()
                .reset_index()
            )
            return out.rename(columns={out.columns[0]: "time"})
    return frame


def as_five_minute_bars(minute_df):
    return as_resampled_bars(minute_df, 5)


def five_minute_entry_plan(minute_df, session_date, signal, tick_size, daily_df=None):
    """BUY stop at previous 5m low; target = REWARD_RATIO x that distance. ATR quality gate."""
    signal = str(signal or "BUY").upper()
    if minute_df is None or minute_df.empty:
        return None, "no 5m bars"

    open_start = dtime(9, 15)
    open_end = dtime(9, 20)
    day = session_rows(as_five_minute_bars(minute_df), session_date)
    if day is None or day.empty:
        return None, "no session 5m bars"

    opening = day[(day["ist_time"] >= open_start) & (day["ist_time"] <= open_end)]
    if opening.empty:
        return None, "no 9:15-9:20 5m bar"
    entry_bar = opening.iloc[-1]
    entry = float(entry_bar["close"])
    prior = day[day["time"] < entry_bar["time"]]
    if prior.empty:
        prev = entry_bar
        sl_source = "opening 5m low (no earlier same-session bar)"
    else:
        prev = prior.iloc[-1]
        sl_source = "previous same-session 5m low"

    tick_size = float(tick_size or 0.05)

    if signal == "BUY":
        sl_raw = float(prev["low"])
        if sl_raw >= entry:
            sl_raw = float(entry_bar["low"])
            sl_source = "opening 5m low (prior low was above entry)"
        if sl_raw >= entry:
            return None, "5m low is not below entry"
        sl_dist = entry - sl_raw
        tp_raw = entry + sl_dist * float(config.REWARD_RATIO)
    else:
        sl_raw = float(prev["high"])
        if sl_raw <= entry:
            sl_raw = float(entry_bar["high"])
            sl_source = "opening 5m high (prior high was below entry)"
        if sl_raw <= entry:
            return None, "5m high is not above entry"
        sl_dist = sl_raw - entry
        tp_raw = entry - sl_dist * float(config.REWARD_RATIO)

    daily_atr = 0.0
    if daily_df is not None and len(daily_df) >= 3:
        daily_atr = average_true_range(daily_df, period=config.ATR_PERIOD)
    hist = session_rows(as_five_minute_bars(minute_df), session_date)
    hist = hist[hist["time"] < entry_bar["time"]].tail(30) if hist is not None else hist
    atr_5m = average_true_range(hist, period=config.ATR_PERIOD) if len(hist) >= 5 else 0.0
    atr = daily_atr or atr_5m or sl_dist

    open_px = float(opening.iloc[0]["open"])
    momentum_pts = abs(entry - open_px)
    mom_atr = momentum_pts / atr if atr else 0.0
    sl_atr = sl_dist / atr if atr else 0.0
    min_mom = float(getattr(config, "MOMENTUM_ATR_MIN", 0.25))
    sl_min = float(getattr(config, "SL_ATR_MIN", 0.2))
    sl_max = float(getattr(config, "SL_ATR_MAX", 1.5))
    if mom_atr < min_mom:
        return None, f"momentum/ATR {mom_atr:.2f} < {min_mom:.2f}"
    if sl_atr < sl_min:
        return None, f"SL/ATR {sl_atr:.2f} below {sl_min:.2f} (too tight)"
    if sl_atr > sl_max:
        return None, f"SL/ATR {sl_atr:.2f} above {sl_max:.2f} (too wide)"

    sl = round_to_tick(sl_raw, tick_size)
    tp = round_to_tick(tp_raw, tick_size)
    entry = round_to_tick(entry, tick_size)
    if signal == "BUY" and not (sl < entry < tp):
        return None, "invalid 1:4 geometry after tick round"
    if signal == "SELL" and not (tp < entry < sl):
        return None, "invalid 1:4 geometry after tick round"

    return {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "momentum_atr": round(mom_atr, 3),
        "sl_atr": round(sl_atr, 3),
        "score": mom_atr / max(sl_atr, 0.05),
        "prior_low": float(prev["low"]),
        "prior_high": float(prev["high"]),
        "prior_time": str(prev.get("ist_time", "")),
        "sl_source": sl_source,
        "daily_atr": daily_atr,
        "atr_5m": atr_5m,
    }, "ok"


def pick_best_candidates(
    candidates,
    cash_n=None,
    mcx_n=None,
    limit=None,
    occupied_sectors=None,
):
    cash_n = int(cash_n if cash_n is not None else getattr(config, "BEST_CASH_ENTRIES", 10))
    mcx_n = int(mcx_n if mcx_n is not None else getattr(config, "BEST_MCX_ENTRIES", 1))
    limit = int(
        limit
        if limit is not None
        else getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10)
    )
    taken_sectors = {
        str(name).upper()
        for name in (occupied_sectors or [])
        if name
    }
    one_per_sector = bool(getattr(config, "ONE_STOCK_PER_SECTOR", True))
    if getattr(config, "USE_CATALYST_WATCHLIST", False):
        ranked = list(candidates or [])
        ranked.sort(
            key=lambda row: (
                not bool((row.get("plan") or {}).get("broke", True)),
                -float((row.get("plan") or {}).get("score") or 0),
                int(row.get("rank") or 99),
            )
        )
        picked = []
        seen = set()
        for row in ranked:
            symbol = str(
                row.get("display")
                or (
                    row["symbol"]["symbol"]
                    if isinstance(row.get("symbol"), dict)
                    else row.get("symbol")
                )
                or ""
            ).upper()
            if not symbol or symbol in seen:
                continue
            sector = str(row.get("sector") or "").upper()
            if one_per_sector and sector and sector in taken_sectors:
                continue
            seen.add(symbol)
            if one_per_sector and sector:
                taken_sectors.add(sector)
            picked.append(row)
            if len(picked) >= max(0, limit):
                break
        return picked
    cash = [row for row in candidates if row.get("kind") != "MCX"]
    mcx = [row for row in candidates if row.get("kind") == "MCX"]
    cash.sort(key=lambda row: float((row.get("plan") or {}).get("score") or 0), reverse=True)
    mcx.sort(key=lambda row: float((row.get("plan") or {}).get("score") or 0), reverse=True)
    return cash[: max(0, cash_n)] + mcx[: max(0, mcx_n)]
