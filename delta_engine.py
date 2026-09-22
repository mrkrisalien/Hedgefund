"""Independent Delta BTC desk. Does not share Dhan order paths or day books."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from delta_client import DeltaClient
from delta_indicators import candles_to_frame
from delta_strategy import evaluate, should_exit, week_loss_halted
from delta_universe import approved_delta_trade

IST = ZoneInfo("Asia/Kolkata")
LOG_PATH = Path(__file__).resolve().parent / "data" / "delta_decisions.jsonl"

delta_state = {
    "is_running": False,
    "interval": int(config.DELTA_EVAL_SECONDS),
    "broker": "Delta Exchange",
    "mode": "PAPER" if config.DELTA_PAPER or not config.DELTA_TRADING_ENABLED else "LIVE",
    "trading_enabled": bool(config.DELTA_TRADING_ENABLED) and not config.DELTA_PAPER,
    "kill_switch": False,
    "state": "IDLE",
    "equity": float(getattr(config, "DELTA_PAPER_EQUITY", 10000)),
    "spot": 0.0,
    "last_logic": "Delta desk idle. Automated regime strangle (paper).",
    "last_confidence": 0,
    "last_score": {},
    "position": None,
    "perp": None,
    "day_pnl": 0.0,
    "realized": [],
    "session_date": "",
    "keys_configured": bool(str(config.DELTA_API_KEY or "").strip()),
    "trade_history": [],
    "last_eval": "",
    "approval": None,
}


def _log(event):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"time": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    history = delta_state["trade_history"]
    action = str(event.get("action") or "")
    if action not in ("SKIP",):
        history.append(
            {
                "time": row["time"],
                "asset": event.get("asset", "BTC-STRANGLE"),
                "signal": event.get("signal", event.get("action", "HOLD")),
                "logic": event.get("reason", ""),
                "confidence": event.get("confidence", delta_state.get("last_confidence", 0)),
            }
        )
        delta_state["trade_history"] = history[-100:]


def _spot_from_ticker(ticker):
    if not ticker:
        return 0.0
    for key in ("mark_price", "spot_price", "close", "price"):
        value = ticker.get(key) if isinstance(ticker, dict) else None
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _premium(product):
    if not product:
        return 0.0
    for key in ("mark_price", "close", "last_price"):
        value = product.get(key)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                continue
    return 0.0


def _expiry_tag(now=None):
    now = now or datetime.now(IST)
    return now.strftime("%d%m%y")


def _match_option(products, kind, strike, same_day=True):
    prefix = "C-BTC-" if kind == "CALL" else "P-BTC-"
    tag = _expiry_tag()
    candidates = []
    for item in products or []:
        symbol = str(item.get("symbol") or "")
        if not symbol.startswith(prefix):
            continue
        parts = symbol.split("-")
        if len(parts) < 4:
            continue
        try:
            item_strike = int(parts[2])
        except ValueError:
            continue
        if item_strike != int(strike):
            continue
        if same_day and parts[3] != tag:
            continue
        candidates.append(item)
    if candidates:
        return candidates[0]
    # Fall back to nearest expiry of that strike.
    for item in products or []:
        symbol = str(item.get("symbol") or "")
        if symbol.startswith(f"{prefix}{int(strike)}-"):
            return item
    return None


def _atm_common_strike(products, spot):
    calls = set()
    puts = set()
    for item in products or []:
        symbol = str(item.get("symbol") or "")
        if not symbol.startswith(("C-BTC-", "P-BTC-")):
            continue
        parts = symbol.split("-")
        if len(parts) < 4:
            continue
        try:
            strike = int(parts[2])
        except ValueError:
            continue
        (calls if symbol.startswith("C-") else puts).add(strike)
    common = calls & puts
    return min(common, key=lambda value: abs(value - float(spot))) if common else None


def _paper_equity():
    return float(delta_state.get("equity") or config.DELTA_PAPER_EQUITY)


def _minutes_to_expiry(now=None):
    now = now or datetime.now(IST)
    expiry = now.replace(hour=17, minute=30, second=0, microsecond=0)
    if now >= expiry:
        expiry = expiry + timedelta(days=1)
    return max((expiry - now).total_seconds() / 60, 0)


def evaluate_once(client=None):
    if delta_state.get("kill_switch"):
        delta_state["state"] = "IDLE"
        delta_state["last_logic"] = "Kill switch is on. No Delta orders."
        return delta_state

    today = str(datetime.now(IST).date())
    if delta_state.get("session_date") != today:
        delta_state["session_date"] = today
        delta_state["day_pnl"] = 0.0

    max_loss = _paper_equity() * config.DELTA_MAX_DAILY_LOSS_PCT
    if delta_state["day_pnl"] <= -max_loss:
        delta_state["state"] = "COOLDOWN"
        delta_state["last_logic"] = "Delta daily loss cap reached."
        return delta_state

    halted, week_pnl = week_loss_halted(
        delta_state.get("realized") or [],
        datetime.now(IST),
        _paper_equity(),
    )
    if halted and not delta_state.get("position"):
        delta_state["state"] = "COOLDOWN"
        delta_state["last_logic"] = (
            f"Weekly loss pause. Last 7d P&L {week_pnl:.2f} hit "
            f"{config.DELTA_WEEKLY_LOSS_PCT:.0%} cap."
        )
        return delta_state

    delta_state["state"] = "ANALYSING"
    client = client or DeltaClient()
    ticker = client.ticker("BTCUSD")
    spot = _spot_from_ticker(ticker)
    raw_candles = client.candles("BTCUSD", "15m", 200)
    frame = candles_to_frame(raw_candles)
    products = client.products()
    now = datetime.now(IST)
    plan = evaluate(
        frame,
        spot,
        products,
        _paper_equity(),
        delta_state.get("position"),
        hour=now.hour,
    )
    snap = plan["snapshot"]
    delta_state["spot"] = snap.get("spot") or spot
    delta_state["last_score"] = plan["score"]
    delta_state["last_confidence"] = min(100, plan["score"]["total"])
    delta_state["last_eval"] = datetime.now(IST).isoformat()
    position = delta_state.get("position")

    if position:
        delta_state["state"] = "MONITORING"
        call_prod = _match_option(products, "CALL", position["call_strike"])
        put_prod = _match_option(products, "PUT", position["put_strike"])
        mark_call = _premium(call_prod) or position["call_prem"]
        mark_put = _premium(put_prod) or position["put_prem"]
        exit_now, why, pnl_pct = should_exit(
            position,
            mark_call,
            mark_put,
            snap,
            now=now,
            bar_close=snap.get("spot"),
            minutes_to_expiry=_minutes_to_expiry(now),
        )
        if exit_now:
            delta_state["state"] = "EXIT_PENDING"
            entry_value = position["qty"] * (position["call_prem"] + position["put_prem"])
            mark_value = position["qty"] * (mark_call + mark_put)
            if position.get("side") == "short":
                pnl = entry_value - mark_value
                close_side = "buy"
            else:
                pnl = mark_value - entry_value
                close_side = "sell"
            call_prod = _match_option(products, "CALL", position["call_strike"])
            put_prod = _match_option(products, "PUT", position["put_strike"])
            client.place_order(call_prod.get("id") if call_prod else 0, position["qty"], close_side)
            client.place_order(put_prod.get("id") if put_prod else 0, position["qty"], close_side)
            delta_state["day_pnl"] += pnl
            delta_state["equity"] = _paper_equity() + pnl
            log = list(delta_state.get("realized") or [])
            log.append({"time": now, "pnl": pnl})
            delta_state["realized"] = log[-400:]
            _log(
                {
                    "action": "EXIT",
                    "signal": "SELL",
                    "asset": f"C{position['call_strike']}/P{position['put_strike']}",
                    "reason": (
                        f"{why}: pnl {pnl_pct:.1f}%. "
                        f"Held since {position['opened_at']}. Paper PnL {pnl:.4f}."
                    ),
                    "confidence": delta_state["last_confidence"],
                    "paper": True,
                }
            )
            delta_state["position"] = None
            delta_state["state"] = "IDLE"
            delta_state["last_logic"] = (
                f"Auto exit ({why}) {position.get('side')} "
                f"C{position['call_strike']}/P{position['put_strike']} pnl {pnl_pct:.1f}%."
            )
            return delta_state
        else:
            delta_state["last_logic"] = (
                f"HOLD {position.get('side')} "
                f"C{position['call_strike']}/P{position['put_strike']} "
                f"| pnl {pnl_pct:.1f}% | SL "
                f"{position.get('stop_low') or '--'}/"
                f"{position.get('stop_high') or '--'} on close | TP "
                f"{config.DELTA_PROFIT_TARGET_PCT:.0f}%"
            )
            return delta_state

    approval = approved_delta_trade("BTCUSD")
    delta_state["approval"] = approval
    if plan["enter"] and not approval:
        delta_state["state"] = "AWAITING_CONFIRMATION"
        delta_state["last_logic"] = (
            "Delta signal found, but BTCUSD is not confirmed for trading today. "
            "Charts continue to update."
        )
        return delta_state

    if not plan["enter"]:
        delta_state["state"] = "IDLE"
        delta_state["last_logic"] = (
            f"No entry. {plan['reason']}. "
            f"Trend {snap.get('trend')} | {snap.get('breakout')} | "
            f"vol_ratio {snap.get('vol_ratio', 0):.2f}."
        )
        _log(
            {
                "action": "SKIP",
                "signal": "HOLD",
                "reason": delta_state["last_logic"],
                "confidence": delta_state["last_confidence"],
            }
        )
        return delta_state

    strategy = approval.get("strategy") if approval else "auto_both"
    required_side = (
        "long" if strategy.startswith("buy_")
        else "short" if strategy.startswith("sell_")
        else None
    )
    if required_side == "short" and not getattr(config, "DELTA_ALLOW_SHORT", False):
        delta_state["state"] = "IDLE"
        delta_state["last_logic"] = (
            f"{strategy} is confirmed, but DELTA_ALLOW_SHORT is disabled."
        )
        return delta_state
    if required_side and plan.get("side") != required_side:
        delta_state["state"] = "IDLE"
        delta_state["last_logic"] = (
            f"{strategy} confirmed; current regime is {plan.get('side') or 'flat'}, "
            f"so no {required_side} entry."
        )
        return delta_state
    structure_name = "strangle"
    if strategy.endswith("_straddle"):
        atm = _atm_common_strike(products, snap["spot"])
        if atm is None:
            delta_state["state"] = "IDLE"
            delta_state["last_logic"] = "No common live ATM call/put strike for straddle."
            return delta_state
        plan["call_strike"] = atm
        plan["put_strike"] = atm
        plan["width"] = 0
        structure_name = "straddle"

    fingerprint = (
        f"{strategy}-{plan['call_strike']}-{plan['put_strike']}-{plan['qty']}"
    )
    if delta_state.get("last_fingerprint") == fingerprint and delta_state.get("position"):
        delta_state["last_logic"] = "Duplicate structure blocked."
        return delta_state

    delta_state["state"] = "ENTRY_PENDING"
    call_prod = _match_option(products, "CALL", plan["call_strike"], config.DELTA_SAME_DAY_EXPIRY)
    put_prod = _match_option(products, "PUT", plan["put_strike"], config.DELTA_SAME_DAY_EXPIRY)
    call_prem = _premium(call_prod) or max(snap["atr"] * 0.02, 5)
    put_prem = _premium(put_prod) or max(snap["atr"] * 0.02, 5)
    qty = plan["qty"]
    open_side = "sell" if plan["side"] == "short" else "buy"
    call_order = client.place_order(call_prod.get("id") if call_prod else 0, qty, open_side)
    put_order = client.place_order(put_prod.get("id") if put_prod else 0, qty, open_side)
    structure = plan.get("structure") or {}
    opened = {
        "opened_at": datetime.now(IST),
        "spot": snap["spot"],
        "side": plan["side"],
        "strategy": strategy,
        "structure_name": structure_name,
        "breakout": snap.get("breakout"),
        "call_strike": plan["call_strike"],
        "put_strike": plan["put_strike"],
        "call_prem": call_prem,
        "put_prem": put_prem,
        "qty": qty,
        "call_symbol": (call_prod or {}).get("symbol"),
        "put_symbol": (put_prod or {}).get("symbol"),
        "width": plan["width"],
        "score": plan["score"],
        "stop_low": structure.get("stop_low"),
        "stop_high": structure.get("stop_high"),
        "lower_low": structure.get("lower_low"),
        "higher_high": structure.get("higher_high"),
        "orders": {"call": call_order, "put": put_order},
    }
    delta_state["position"] = opened
    delta_state["last_fingerprint"] = fingerprint
    delta_state["state"] = "POSITION_OPEN"
    reason = (
        f"{delta_state['mode']} {plan['side']} {structure_name}: {open_side} {qty}x "
        f"{opened['call_symbol'] or plan['call_strike']} and "
        f"{qty}x {opened['put_symbol'] or plan['put_strike']}. "
        f"Spot {snap['spot']:.0f}. Structure SL on CLOSE below "
        f"{opened['stop_low']} / above {opened['stop_high']} "
        f"(LL {opened['lower_low']} HH {opened['higher_high']} + ATR buffer). "
        f"Hold until TP {config.DELTA_PROFIT_TARGET_PCT:.0f}% or structure stop. "
        f"{plan['reason']}."
    )
    delta_state["last_logic"] = reason
    _log(
        {
            "action": "ENTRY",
            "signal": "SELL" if plan["side"] == "short" else "BUY",
            "asset": f"C{plan['call_strike']}/P{plan['put_strike']}",
            "reason": reason,
            "confidence": delta_state["last_confidence"],
            "paper": True,
        }
    )
    return delta_state


async def delta_loop():
    client = DeltaClient()
    while True:
        if not delta_state["is_running"] or delta_state.get("kill_switch"):
            if delta_state.get("state") not in ("POSITION_OPEN", "MONITORING"):
                delta_state["state"] = "IDLE"
            await asyncio.sleep(1)
            continue
        try:
            await asyncio.to_thread(evaluate_once, client)
        except Exception as error:
            delta_state["state"] = "IDLE"
            delta_state["last_logic"] = f"Delta eval failed: {error}"
            _log({"action": "ERROR", "signal": "HOLD", "reason": str(error)})
        wait = max(30, int(delta_state.get("interval") or config.DELTA_EVAL_SECONDS))
        await asyncio.sleep(wait)
