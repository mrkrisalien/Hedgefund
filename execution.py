import config
from broker import (
    check_trading_ip,
    close_position_market,
    fetch_daily_candles,
    fetch_five_minute_candles,
    format_ip_status,
    get_client,
    get_equity,
    get_positions,
    get_quote,
    require_success,
    resolve_symbol_cached,
    round_to_tick,
)
from risk import book_loss_hit, build_entry_plan, signal_gate, size_position


from datetime import datetime
from zoneinfo import ZoneInfo


def evaluate_entry_plan(symbol, signal="BUY", chase_risk="medium", strategy="breakout"):
    instrument = resolve_symbol_cached(symbol)
    five = fetch_five_minute_candles(instrument, bars=80)
    if five is None or five.empty:
        from broker import fetch_live_session_candles

        five = fetch_live_session_candles(instrument, interval=5)
    daily = fetch_daily_candles(instrument, bars=40)
    session = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    is_commodity = instrument["exchange"] == "MCX"
    is_option = instrument.get("instrument") in {"OPTIDX", "OPTSTK", "OPTFUT"}
    hourly = None
    if is_commodity and config.COMMODITY_USE_HOURLY_ATR:
        from broker import fetch_hourly_candles

        hourly = fetch_hourly_candles(instrument, bars=24)
    strategy = str(strategy or "breakout").strip().lower()
    live_orb = (
        bool(getattr(config, "LIVE_FIXED_PROTECTION_ENABLED", False))
        and instrument.get("exchange") == "NSE"
        and instrument.get("instrument") == "EQUITY"
        and bool(getattr(config, "REQUIRE_FIRST_5M_RETRACE", True))
    )
    if live_orb or (
        strategy in {"breakout", "range_breakout"} and (not is_commodity or is_option)
    ):
        from catalyst_setup import (
            first_five_minute_retrace_plan,
            previous_candle_breakout_plan,
            range_breakout_plan,
        )

        quote = get_quote(instrument)
        live_entry = quote["ask"] or quote["last_price"]
        planner = first_five_minute_retrace_plan
        one_minute = None
        if live_orb:
            from broker import fetch_live_session_candles

            one_minute = fetch_live_session_candles(instrument, interval=1)
        if not live_orb:
            planner = (
                range_breakout_plan
                if strategy == "range_breakout"
                else previous_candle_breakout_plan
            )
        plan, reason = planner(
            five,
            session,
            signal,
            instrument["tick_size"],
            entry_override=live_entry,
            max_chase_r=0.5 if is_option else None,
            **({"three_minute_df": one_minute} if live_orb else {}),
        )
    elif not is_commodity and strategy in {
        "consolidation",
        "trend_break",
        "auto_structure",
    }:
        from catalyst_setup import structure_entry_plan

        quote = get_quote(instrument)
        live_entry = quote["ask"] or quote["last_price"]
        plan, reason = structure_entry_plan(
            five,
            session,
            signal,
            instrument["tick_size"],
            daily,
            chase_risk=chase_risk,
            entry_override=live_entry,
            strategy=strategy,
        )
    else:
        plan, reason = build_entry_plan(
            five,
            session,
            signal,
            instrument["tick_size"],
            daily,
            hourly_df=hourly,
            use_hourly=is_commodity and config.COMMODITY_USE_HOURLY_ATR,
        )
    if plan and plan.get("broke") is False:
        return None, reason, instrument, daily
    return plan, reason, instrument, daily


def calculate_position_size(symbol, stop_loss_price, risk_percent, equity):
    instrument = resolve_symbol_cached(symbol)
    quote = get_quote(instrument)
    entry = quote["ask"] or quote["last_price"]
    book = "MCX" if instrument["exchange"] == "MCX" else "NSE_EQ"
    if risk_percent is not None and book != "MCX":
        config.CASH_RISK_PCT = float(risk_percent) / 100.0
    qty, reason = size_position(
        equity,
        entry,
        stop_loss_price,
        instrument["lot_size"],
        book=book,
        value_multiplier=instrument.get("value_multiplier", 1),
    )
    return qty, reason


def get_open_positions(symbol=None):
    rows = []
    try:
        raw = get_positions()
    except Exception as error:
        print(f"[SYSTEM] Positions fetch failed: {error}")
        return []
    needle = str(symbol).upper() if symbol else None
    for item in raw:
        if not isinstance(item, dict):
            continue
        net = float(item.get("netQty") or item.get("net_qty") or 0)
        if net == 0:
            continue
        name = str(
            item.get("tradingSymbol")
            or item.get("symbol")
            or item.get("securityId")
            or ""
        )
        if needle and needle not in name.upper() and name.upper() not in needle:
            continue
        side = str(item.get("positionType") or "LONG").upper()
        rows.append(
            {
                "ticket": item.get("securityId") or item.get("security_id"),
                "symbol": name,
                "side": "BUY" if side in ("LONG", "BUY") else "SELL",
                "volume": abs(net),
                "price": item.get("averagePrice") or item.get("costPrice"),
                "sl": item.get("stopLossPrice") or "",
                "tp": item.get("targetPrice") or "",
                "pnl": item.get("unrealizedProfit") or item.get("unrealized_profit") or 0,
                "raw": item,
            }
        )
    return rows


def close_position(ticket_or_pos):
    target = (
        ticket_or_pos.get("ticket")
        if isinstance(ticket_or_pos, dict)
        else ticket_or_pos
    )
    if str(target or "").startswith("PAPER-") or (
        isinstance(ticket_or_pos, dict) and ticket_or_pos.get("paper")
    ):
        from paper_book import close_fill

        return close_fill(target, reason="MANUAL")
    if isinstance(ticket_or_pos, dict) and ticket_or_pos.get("raw"):
        data = close_position_market(ticket_or_pos["raw"])
        return {"ok": True, "data": data}
    positions = get_open_positions()
    target = str(ticket_or_pos)
    for pos in positions:
        if str(pos.get("ticket")) == target or str(pos.get("symbol")) == target:
            data = close_position_market(pos["raw"])
            return {"ok": True, "data": data}
    raise RuntimeError(f"No open position matching {ticket_or_pos}")


def execute_trade(
    symbol,
    signal,
    quantity=None,
    confidence=100,
    daily_df=None,
    day_start_equity=None,
    book_pnl=None,
    stop_loss=None,
    take_profit=None,
    chase_risk="medium",
    strategy="breakout",
    source="",
    sector="",
):
    signal = str(signal).strip().upper()
    empty = {
        "ok": False,
        "message": "HOLD: No trade executed.",
        "order": "",
        "price": None,
        "sl": None,
        "tp": None,
        "volume": 0,
    }
    if signal == "HOLD":
        return {**empty, "message": "HOLD: No trade executed."}
    if signal not in ("BUY", "SELL"):
        return {**empty, "message": f"ERROR: Invalid signal '{signal}'."}

    instrument = resolve_symbol_cached(symbol)
    if getattr(config, "LIVE_CASH_EQUITIES_ONLY", False) and (
        instrument.get("exchange") != "NSE"
        or instrument.get("instrument") != "EQUITY"
    ):
        return {
            **empty,
            "message": (
                "BLOCKED: restricted live mode permits NSE cash equities only; "
                "options and MCX are alert-only."
            ),
        }
    if daily_df is None:
        daily_df = fetch_daily_candles(instrument, bars=20)

    allowed, reason = signal_gate(signal, confidence, daily_df)
    if not allowed:
        return {**empty, "message": f"SKIPPED: {reason}."}

    equity = get_equity()
    is_commodity = instrument["exchange"] == "MCX"
    book = "MCX" if is_commodity else "NSE_EQ"
    if (
        day_start_equity is not None
        and book_pnl is not None
        and book_loss_hit(day_start_equity, book_pnl, book)
    ):
        return {
            **empty,
            "message": f"SKIPPED: {book} daily loss cap reached (other book still allowed).",
        }

    quote = get_quote(instrument)
    price = (quote["ask"] or quote["last_price"]) if signal == "BUY" else (
        quote["bid"] or quote["last_price"]
    )
    if price <= 0:
        return {
            **empty,
            "message": f"ERROR: Unable to retrieve current price for '{instrument['symbol']}'.",
        }

    tick_size = instrument["tick_size"]
    price = round_to_tick(price, tick_size)
    plan, plan_reason, _, _ = evaluate_entry_plan(
        symbol, signal, chase_risk, strategy
    )
    if not plan:
        return {**empty, "message": f"SKIPPED: {plan_reason}."}
    sl = round_to_tick(plan["sl"], tick_size)
    tp = round_to_tick(plan["tp"], tick_size)
    virtual_tp = tp
    emergency_tp = tp
    atr = plan["atr"]
    price = round_to_tick(plan["entry"], tick_size)
    trailing_jump = 0.0

    live_fixed = bool(getattr(config, "LIVE_FIXED_PROTECTION_ENABLED", False))
    if live_fixed:
        if stop_loss is not None:
            sl = round_to_tick(float(stop_loss), tick_size)
        rr = float(getattr(config, "REWARD_RATIO", 2) or 2)
        sl_dist = abs(price - sl)
        if sl_dist <= 0:
            return {**empty, "message": "BLOCKED: stop distance is zero after tick rounding."}
        if signal == "BUY":
            tp = round_to_tick(price + rr * sl_dist, tick_size)
        else:
            tp = round_to_tick(price - rr * sl_dist, tick_size)
        trailing_jump = 0.0
        virtual_tp = tp
        emergency_tp = tp
        if getattr(config, "RUNG_TRAIL_ENABLED", True):
            from rung_trail import emergency_target

            emergency_tp = emergency_target(price, sl, tick_size, signal) or tp
            tp = emergency_tp
    else:
        if stop_loss is not None:
            sl = round_to_tick(float(stop_loss), tick_size)
        if take_profit is not None:
            tp = round_to_tick(float(take_profit), tick_size)

    valid_geometry = sl < price < tp if signal == "BUY" else tp < price < sl
    if not valid_geometry:
        return {
            **empty,
            "message": (
                f"BLOCKED: invalid protected-order geometry "
                f"(entry {price}, SL {sl}, target {tp})."
            ),
        }

    if live_fixed or quantity is None:
        quantity, size_reason = size_position(
            equity,
            price,
            sl,
            instrument["lot_size"],
            book=book,
            value_multiplier=instrument.get("value_multiplier", 1),
        )
        if quantity < 1:
            return {**empty, "message": f"SKIPPED: {size_reason}."}
        order_quantity = int(quantity)
        max_risk = float(getattr(config, "LIVE_TRADE_RISK_RS", 500) or 500)
        actual_risk = abs(price - sl) * order_quantity * float(
            instrument.get("value_multiplier") or 1
        )
        if live_fixed and actual_risk > max_risk + 0.5:
            return {
                **empty,
                "message": (
                    f"BLOCKED: sized risk Rs {actual_risk:.0f} exceeds "
                    f"Rs {max_risk:.0f} stop budget."
                ),
            }
    else:
        order_quantity = int(quantity)
    if getattr(config, "PAPER_TRADE", False):
        from paper_book import record_fill

        paper_fill = record_fill(
            instrument.get("trading_symbol") or instrument.get("symbol"),
            signal,
            order_quantity,
            price,
            sl,
            tp,
            sector=sector,
            kind=book,
            source=source,
            value_multiplier=instrument.get("value_multiplier", 1),
        )
        message = (
            f"PAPER: {signal} {instrument['trading_symbol']} qty {order_quantity} "
            f"@ {price} SL {sl} TP {tp} (no Dhan order)"
        )
        print(f"[PAPER] {message}")
        return {
            "ok": True,
            "paper": True,
            "message": message,
            "order": "PAPER",
            "deal": "PAPER",
            "price": price,
            "sl": sl,
            "tp": tp,
            "volume": order_quantity,
            "atr": atr,
            "paper_fill_id": paper_fill.get("id"),
            "data": {"paper": True, "fill": paper_fill},
        }

    if not config.TRADING_ENABLED:
        return {
            **empty,
            "message": "BLOCKED: Trading is disabled in config.TRADING_ENABLED.",
        }

    if live_fixed:
        from memory_store import (
            live_entries_today,
            live_pending_orders_today,
            symbol_already_used_today,
            sync_live_order_statuses,
        )

        sync_live_order_statuses(cancel_stale=True)
        entry_limit = int(getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10) or 10)
        entries_today = live_entries_today()
        pending_symbols = {
            str(row.get("symbol") or "").upper()
            for row in live_pending_orders_today()
            if row.get("symbol")
        }
        filled_symbols = {
            str(row.get("symbol") or "").upper()
            for row in entries_today
            if row.get("symbol")
        }
        position_symbols = {
            str(pos.get("symbol") or "").upper()
            for pos in get_open_positions()
            if pos.get("symbol")
        }
        occupied = filled_symbols | pending_symbols | position_symbols
        trading_symbol = str(
            instrument.get("trading_symbol") or instrument.get("symbol") or ""
        ).upper()
        if trading_symbol and trading_symbol in pending_symbols:
            return {
                **empty,
                "message": f"BLOCKED: {trading_symbol} already has a pending entry order.",
            }
        if getattr(config, "BLOCK_REPEAT_SYMBOL_TODAY", True) and symbol_already_used_today(
            trading_symbol, entries_today
        ):
            return {
                **empty,
                "message": (
                    f"BLOCKED: {trading_symbol} already traded today "
                    "(stop, target, or still open). No repeat."
                ),
            }
        if len(occupied) >= entry_limit:
            return {
                **empty,
                "message": (
                    f"BLOCKED: {entry_limit} equal-risk slots are already filled "
                    f"or pending."
                ),
            }

    client = get_client()
    try:
        response = client.place_super_order(
            security_id=instrument["security_id"],
            exchange_segment=instrument["exchange_segment"],
            transaction_type=signal,
            quantity=order_quantity,
            order_type=client.LIMIT,
            product_type=client.INTRA,
            price=price,
            targetPrice=tp,
            stopLossPrice=sl,
            trailingJump=trailing_jump,
            tag="AI-HEDGE",
        )
        data = require_success(response, f"{signal} {instrument['symbol']}")
    except Exception as error:
        text = str(error)
        if "DH-905" in text and "Invalid IP" in text:
            try:
                ip_note = format_ip_status(check_trading_ip())
            except Exception as ip_error:
                ip_note = str(ip_error)
            return {
                **empty,
                "message": (
                    f"ERROR: Dhan rejected the order (DH-905 Invalid IP). "
                    f"{ip_note}"
                ),
            }
        return {
            **empty,
            "message": f"ERROR: Order failed for {instrument['symbol']}: {error}",
        }

    order_id = ""
    broker_status = "SUBMITTED"
    if isinstance(data, dict):
        order_id = data.get("orderId") or data.get("order_id") or ""
        broker_status = str(
            data.get("orderStatus") or data.get("order_status") or "SUBMITTED"
        ).upper()
    filled = broker_status == "TRADED"
    message = (
        f"{'FILLED' if filled else 'SUBMITTED'}: {signal} {instrument['trading_symbol']} "
        f"({instrument['security_id']}) submitted. "
        f"Qty: {order_quantity}, Price: {price}, SL {sl}, "
        f"virtual 1:2 {virtual_tp}, Dhan emergency TP {emergency_tp}, "
        f"Status: {broker_status}"
        + (f", Order ID: {order_id}" if order_id else "")
    )
    print(f"[TRADE] {message}")
    return {
        "ok": True,
        "message": message,
        "order": order_id,
        "deal": order_id,
        "price": price,
        "sl": sl,
        "tp": virtual_tp,
        "emergency_tp": emergency_tp,
        "virtual_tp": virtual_tp,
        "trail_rungs": 0,
        "risk_r": abs(price - sl),
        "volume": order_quantity,
        "atr": atr,
        "filled": filled,
        "order_status": broker_status,
        "data": data,
    }
