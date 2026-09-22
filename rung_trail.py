"""3-minute close rung trail: lock SL at the old 1:2, lift the next target 2R."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import config
from broker import fetch_live_session_candles, get_client, resolve_symbol_cached, round_to_tick
from memory_store import live_entries_today, update_live_memory_row
from risk import as_resampled_bars

IST = ZoneInfo("Asia/Kolkata")


def _cutoff():
    raw = str(getattr(config, "TRAIL_NO_NEW_RUNG_AFTER", "14:30") or "14:30")
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return time(14, 30)


def emergency_target(entry, sl, tick_size, side="BUY"):
    risk = abs(float(entry) - float(sl))
    rr = float(getattr(config, "TRAIL_EMERGENCY_RR", 8) or 8)
    if risk <= 0:
        return None
    if str(side).upper() == "SELL":
        return round_to_tick(float(entry) - rr * risk, tick_size)
    return round_to_tick(float(entry) + rr * risk, tick_size)


def virtual_target(entry, sl, rungs, tick_size, side="BUY"):
    risk = abs(float(entry) - float(sl))
    step = float(getattr(config, "REWARD_RATIO", 2) or 2)
    if risk <= 0:
        return None
    multiple = step * (int(rungs) + 1)
    if str(side).upper() == "SELL":
        return round_to_tick(float(entry) - multiple * risk, tick_size)
    return round_to_tick(float(entry) + multiple * risk, tick_size)


def _last_completed_3m_close(instrument):
    frame = fetch_live_session_candles(instrument, interval=1)
    if frame is None or frame.empty:
        return None
    three = as_resampled_bars(frame, 3)
    if three is None or three.empty:
        return None
    stamps = three["time"]
    now = datetime.now(IST)
    bucket = now.astimezone(IST).replace(second=0, microsecond=0)
    minute = bucket.minute - (bucket.minute % 3)
    cutoff = bucket.replace(minute=minute)
    closes = []
    for _, row in three.iterrows():
        stamp = row["time"]
        try:
            when = stamp.tz_convert(IST) if getattr(stamp, "tzinfo", None) else stamp
        except Exception:
            when = stamp
        try:
            if when >= cutoff:
                continue
        except TypeError:
            continue
        try:
            closes.append(float(row["close"]))
        except (TypeError, ValueError):
            continue
    return closes[-1] if closes else None


def _ensure_state(row):
    try:
        entry = float(row.get("entry_price"))
        sl = float(row.get("sl"))
    except (TypeError, ValueError):
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tick = 0.05
    try:
        instrument = resolve_symbol_cached(row.get("symbol"))
        tick = float(instrument.get("tick_size") or 0.05)
    except Exception:
        instrument = None
    rungs = int(row.get("trail_rungs") or 0)
    virtual = row.get("virtual_tp")
    if virtual is None:
        virtual = virtual_target(entry, sl, rungs, tick, row.get("side") or "BUY")
        update_live_memory_row(
            row.get("symbol"),
            trail_rungs=rungs,
            virtual_tp=virtual,
            risk_r=risk,
            emergency_tp=emergency_target(entry, sl, tick, row.get("side") or "BUY"),
        )
        row["virtual_tp"] = virtual
        row["trail_rungs"] = rungs
        row["risk_r"] = risk
    return {
        "entry": entry,
        "sl": sl,
        "risk": risk,
        "tick": tick,
        "rungs": rungs,
        "virtual_tp": float(virtual),
        "instrument": instrument,
        "side": str(row.get("side") or "BUY").upper(),
        "order": str(row.get("order") or row.get("ticket") or ""),
    }


def _push_emergency_target(order_id, price, side="BUY"):
    if not order_id:
        return False
    client = get_client()
    try:
        response = client.modify_super_order(
            order_id,
            getattr(client, "LIMIT", "LIMIT"),
            "TARGET_LEG",
            targetPrice=float(price),
        )
        status = str((response or {}).get("status") or "").lower()
        return status in {"success", "ok"}
    except Exception as error:
        print(f"[TRAIL] Emergency target modify failed for {order_id}: {error}")
        return False


def _modify_stop(order_id, stop):
    if not order_id:
        return False
    client = get_client()
    try:
        response = client.modify_super_order(
            order_id,
            getattr(client, "LIMIT", "LIMIT"),
            "STOP_LOSS_LEG",
            stopLossPrice=float(stop),
            trailingJump=0.0,
        )
        status = str((response or {}).get("status") or "").lower()
        return status in {"success", "ok"}
    except Exception as error:
        print(f"[TRAIL] Stop modify failed for {order_id}: {error}")
        return False


def manage_rung_trails(positions=None):
    """Advance virtual 1:2 rungs after a completed 3m close. Does not flatten."""
    if not getattr(config, "RUNG_TRAIL_ENABLED", True):
        return 0
    if getattr(config, "PAPER_TRADE", False):
        return 0
    now = datetime.now(IST)
    if now.time() >= _cutoff():
        return 0
    max_extra = int(getattr(config, "TRAIL_MAX_EXTRA_RUNGS", 2) or 2)
    if positions is not None:
        open_names = {
            str(pos.get("symbol") or "").upper()
            for pos in positions
            if pos.get("symbol")
        }
        if not open_names:
            return 0
    else:
        open_names = set()
    moved = 0
    for row in live_entries_today():
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        if open_names and not any(symbol in name or name in symbol for name in open_names):
            continue
        if str(row.get("status") or "").upper() == "CLOSED":
            continue
        state = _ensure_state(row)
        if not state:
            continue
        if state["rungs"] >= max_extra:
            continue
        instrument = state["instrument"]
        if instrument is None:
            try:
                instrument = resolve_symbol_cached(symbol)
            except Exception as error:
                print(f"[TRAIL] Skip {symbol}: {error}")
                continue
        if not row.get("emergency_pushed"):
            emergency = emergency_target(
                state["entry"], state["sl"], state["tick"], state["side"]
            )
            if emergency and _push_emergency_target(state["order"], emergency, state["side"]):
                update_live_memory_row(symbol, emergency_tp=emergency, emergency_pushed=True)
                print(
                    f"[TRAIL] {symbol}: Dhan target moved to emergency "
                    f"{emergency:.2f} so the first 1:2 stays virtual."
                )
        close = _last_completed_3m_close(instrument)
        if close is None:
            continue
        virtual = state["virtual_tp"]
        hit = close > virtual if state["side"] == "BUY" else close < virtual
        if not hit:
            continue
        new_rungs = state["rungs"] + 1
        locked_sl = (
            round_to_tick(virtual - state["tick"], state["tick"])
            if state["side"] == "BUY"
            else round_to_tick(virtual + state["tick"], state["tick"])
        )
        next_tp = virtual_target(
            state["entry"], state["sl"], new_rungs, state["tick"], state["side"]
        )
        if not _modify_stop(state["order"], locked_sl):
            print(f"[TRAIL] {symbol}: 3m close {close:.2f} tagged {virtual:.2f}; SL modify failed.")
            continue
        fields = {
            "trail_rungs": new_rungs,
            "virtual_tp": next_tp,
            "sl": locked_sl,
            "tp": next_tp,
        }
        if new_rungs >= max_extra and next_tp:
            if _push_emergency_target(state["order"], next_tp, state["side"]):
                fields["emergency_tp"] = next_tp
                fields["emergency_pushed"] = True
        update_live_memory_row(symbol, **fields)
        moved += 1
        print(
            f"[TRAIL] {symbol}: 3m close {close:.2f} > rung {virtual:.2f}. "
            f"SL locked {locked_sl:.2f}, next 1:{int((new_rungs + 1) * 2)} {next_tp:.2f} "
            f"(rung {new_rungs}/{max_extra})."
        )
    return moved
