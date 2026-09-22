"""1-month long vs short BTC strangle backtest on Delta India BTCUSD candles.

Option marks are Black-Scholes from spot + 24h realized vol, not historical
option trades. Long and short see the same marks; exits are side-specific so
the books are not exact mirrors.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import config
from delta_indicators import candles_to_frame, entry_score, snapshot
from delta_strategy import choose_strikes, size_contracts

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "delta_strangle_month_backtest.json"
CONTRACT_MULT = 0.001
FEE_RATE = 0.0413
EVAL_EVERY = 2
LOOKBACK = 96
STRIKE_STEP = 200
STARTING_EQUITY = float(getattr(config, "DELTA_PAPER_EQUITY", 10000))


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_premium(spot, strike, years, iv, is_call):
    years = max(float(years), 1.0 / (365 * 24))
    iv = max(float(iv), 0.08)
    spot = max(float(spot), 1.0)
    strike = max(float(strike), 1.0)
    vol_sqrt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vol_sqrt
    d2 = d1 - vol_sqrt
    if is_call:
        price = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    else:
        price = strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(price, 0.05)


def fetch_btc_15m(days=31):
    end = int(time.time())
    start = end - days * 86400
    url = str(config.DELTA_BASE_URL).rstrip("/") + "/v2/history/candles"
    rows = []
    cursor_end = end
    while cursor_end > start:
        params = {
            "symbol": "BTCUSD",
            "resolution": "15m",
            "start": start,
            "end": cursor_end,
        }
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        chunk = payload.get("result") if isinstance(payload, dict) else payload
        if not chunk:
            break
        rows.extend(chunk)
        times = []
        for item in chunk:
            if isinstance(item, dict):
                times.append(int(float(item.get("time") or item.get("timestamp") or 0)))
            elif isinstance(item, (list, tuple)):
                times.append(int(float(item[0])))
        if not times:
            break
        oldest = min(t if t > 10_000_000_000 else t for t in times)
        if oldest > 10_000_000_000:
            oldest = oldest / 1000
        if oldest <= start or len(chunk) < 50:
            break
        cursor_end = int(oldest) - 1
        if len(rows) > 8000:
            break
    frame = candles_to_frame(rows)
    if "time" not in frame.columns:
        raise RuntimeError("Candle payload has no time column.")
    stamp = pd.to_numeric(frame["time"], errors="coerce")
    if stamp.median() > 10_000_000_000:
        stamp = stamp / 1000
    frame["ts"] = pd.to_datetime(stamp, unit="s", utc=True).dt.tz_convert(IST)
    frame = frame.dropna(subset=["ts", "close"]).sort_values("ts").drop_duplicates("ts")
    return frame.reset_index(drop=True)


def realized_iv(closes):
    if len(closes) < 20:
        return 0.55
    rets = pd.Series(closes).pct_change().dropna()
    if rets.empty:
        return 0.55
    return float(max(0.15, min(1.8, rets.std() * math.sqrt(365 * 96))))


def years_to_expiry(ts, expiry=None):
    if hasattr(ts, "to_pydatetime"):
        local = ts.to_pydatetime()
    else:
        local = ts
    if getattr(local, "tzinfo", None) is None:
        local = local.replace(tzinfo=IST)
    if expiry is None:
        expiry = local.replace(hour=17, minute=30, second=0, microsecond=0)
        while expiry - local < timedelta(hours=6):
            expiry = expiry + timedelta(days=1)
    else:
        if hasattr(expiry, "to_pydatetime"):
            expiry = expiry.to_pydatetime()
        if getattr(expiry, "tzinfo", None) is None:
            expiry = expiry.replace(tzinfo=IST)
    seconds = (expiry - local).total_seconds()
    return max(seconds / (365 * 24 * 3600), 15.0 / (365 * 24 * 60)), expiry


def fee(qty, prem):
    return abs(qty * prem * CONTRACT_MULT) * FEE_RATE


def metrics(trades, equity_path, start_eq):
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    curve = [p["equity"] for p in equity_path]
    peak = curve[0] if curve else start_eq
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    std = float(pd.Series(pnls).std()) if len(pnls) > 1 else 0.0
    mean = float(pd.Series(pnls).mean()) if pnls else 0.0
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 1) if pnls else 0.0,
        "net_pnl": round(sum(pnls), 4) if pnls else 0.0,
        "end_equity": round(curve[-1] if curve else start_eq, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown": round(max_dd, 4),
        "max_win": round(max(pnls), 4) if pnls else 0.0,
        "max_loss": round(min(pnls), 4) if pnls else 0.0,
        "sharpe_trade": round(mean / std, 3) if std else None,
        "reasons": pd.Series([t["reason"] for t in trades]).value_counts().to_dict()
        if trades
        else {},
    }


def exit_rules(side, pnl_pct, held, snap, entry_spot):
    if side == "long":
        if pnl_pct >= config.DELTA_PROFIT_TARGET_PCT:
            return True, "profit_target"
        if pnl_pct <= -config.DELTA_STOP_LOSS_PCT:
            return True, "stop_loss"
        if snap["vol_ratio"] < 0.85 and held >= 30:
            return True, "volatility_failure"
    else:
        if pnl_pct >= config.DELTA_PROFIT_TARGET_PCT:
            return True, "profit_target"
        if pnl_pct <= -config.DELTA_STOP_LOSS_PCT:
            return True, "stop_loss"
        if snap["vol_ratio"] > 1.35 and held >= 15:
            return True, "volatility_spike"
    if held >= config.DELTA_MAX_HOLDING_MINUTES:
        return True, "time_exit"
    move = abs(snap["spot"] - entry_spot) / max(entry_spot, 1)
    if move >= 0.02:
        return True, "reposition"
    return False, "hold"


def simulate(frame, side, exit_mode="engine"):
    equity = STARTING_EQUITY
    day_pnl = 0.0
    session = None
    position = None
    trades = []
    equity_path = []
    last_eval = -10

    for index in range(LOOKBACK, len(frame)):
        row = frame.iloc[index]
        window = frame.iloc[index - LOOKBACK : index + 1]
        snap = snapshot(window, float(row["close"]))
        ts = row["ts"]
        day = str(ts.date())
        if day != session:
            session = day
            day_pnl = 0.0
        if day_pnl <= -STARTING_EQUITY * config.DELTA_MAX_DAILY_LOSS_PCT:
            if position is None:
                equity_path.append({"time": ts.isoformat(), "equity": equity})
                continue

        iv = realized_iv(window["close"].tolist()[-96:])
        tte, expiry_guess = years_to_expiry(ts, None if not position else position.get("expiry"))

        if position:
            call_p = bs_premium(snap["spot"], position["call"], tte, iv, True)
            put_p = bs_premium(snap["spot"], position["put"], tte, iv, False)
            held = (ts - position["opened_at"]).total_seconds() / 60
            mark = call_p + put_p
            if side == "long":
                gross = position["qty"] * (mark - position["entry_sum"]) * CONTRACT_MULT
            else:
                gross = position["qty"] * (position["entry_sum"] - mark) * CONTRACT_MULT
            entry_cost = position["qty"] * position["entry_sum"] * CONTRACT_MULT
            pnl_pct = (gross / entry_cost * 100) if entry_cost else 0
            done, why = exit_rules(side, pnl_pct, held, snap, position["spot"])
            if exit_mode == "time60":
                done, why = (True, "time_exit") if held >= 60 else (False, "hold")
            if done:
                round_trip_fee = position["open_fee"] + fee(position["qty"], mark)
                pnl = gross - round_trip_fee
                equity += pnl
                day_pnl += pnl
                trades.append(
                    {
                        "side": side,
                        "entry": position["opened_at"].isoformat(),
                        "exit": ts.isoformat(),
                        "spot_in": round(position["spot"], 2),
                        "spot_out": round(snap["spot"], 2),
                        "call": position["call"],
                        "put": position["put"],
                        "qty": position["qty"],
                        "entry_sum": round(position["entry_sum"], 4),
                        "exit_sum": round(mark, 4),
                        "hold_min": round(held, 1),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": why,
                        "iv": round(position["iv"], 3),
                    }
                )
                position = None
                last_eval = index
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue

        if index - last_eval < EVAL_EVERY:
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        last_eval = index
        score = entry_score(snap, hour=ts.hour)
        if score["total"] < config.DELTA_ENTRY_SCORE_THRESHOLD:
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        strikes = list(
            range(
                int(snap["spot"] * 0.9 / STRIKE_STEP) * STRIKE_STEP,
                int(snap["spot"] * 1.1 / STRIKE_STEP) * STRIKE_STEP + STRIKE_STEP,
                STRIKE_STEP,
            )
        )
        call_k, put_k, width = choose_strikes(snap["spot"], snap["atr"], strikes, strikes)
        call_p = bs_premium(snap["spot"], call_k, tte, iv, True)
        put_p = bs_premium(snap["spot"], put_k, tte, iv, False)
        qty = size_contracts(equity, call_p * CONTRACT_MULT, put_p * CONTRACT_MULT)
        qty = max(1, min(qty, int(config.DELTA_MAX_CONTRACTS)))
        entry_sum = call_p + put_p
        position = {
            "opened_at": ts.to_pydatetime(),
            "spot": snap["spot"],
            "call": call_k,
            "put": put_k,
            "qty": qty,
            "entry_sum": entry_sum,
            "open_fee": fee(qty, entry_sum),
            "iv": iv,
            "width": width,
            "expiry": expiry_guess,
        }
        equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})

    if position:
        row = frame.iloc[-1]
        window = frame.iloc[-LOOKBACK:]
        snap = snapshot(window, float(row["close"]))
        iv = realized_iv(window["close"].tolist())
        tte, _ = years_to_expiry(row["ts"], position.get("expiry"))
        call_p = bs_premium(snap["spot"], position["call"], tte, iv, True)
        put_p = bs_premium(snap["spot"], position["put"], tte, iv, False)
        mark = call_p + put_p
        if side == "long":
            gross = position["qty"] * (mark - position["entry_sum"]) * CONTRACT_MULT
        else:
            gross = position["qty"] * (position["entry_sum"] - mark) * CONTRACT_MULT
        pnl = gross - position["open_fee"] - fee(position["qty"], mark)
        equity += pnl
        trades.append(
            {
                "side": side,
                "entry": position["opened_at"].isoformat(),
                "exit": row["ts"].isoformat(),
                "spot_in": round(position["spot"], 2),
                "spot_out": round(snap["spot"], 2),
                "call": position["call"],
                "put": position["put"],
                "qty": position["qty"],
                "entry_sum": round(position["entry_sum"], 4),
                "exit_sum": round(mark, 4),
                "hold_min": round((row["ts"] - position["opened_at"]).total_seconds() / 60, 1),
                "pnl": round(pnl, 4),
                "pnl_pct": 0,
                "reason": "eod_flatten",
                "iv": round(position["iv"], 3),
            }
        )
        equity_path.append({"time": row["ts"].isoformat(), "equity": round(equity, 4)})

    return trades, equity_path, metrics(trades, equity_path, STARTING_EQUITY)


def daily_equity(path, limit=32):
    if not path:
        return []
    frame = pd.DataFrame(path)
    frame["day"] = pd.to_datetime(frame["time"]).dt.tz_convert(IST).dt.strftime("%m-%d")
    last = frame.groupby("day", sort=True)["equity"].last()
    if len(last) > limit:
        last = last.iloc[:: max(1, len(last) // limit)]
    return [{"day": k, "equity": round(float(v), 2)} for k, v in last.items()]


def run():
    frame = fetch_btc_15m(31)
    start = frame["ts"].iloc[LOOKBACK]
    end = frame["ts"].iloc[-1]
    long_trades, long_eq, long_m = simulate(frame, "long")
    short_trades, short_eq, short_m = simulate(frame, "short")
    long_t, long_teq, long_tm = simulate(frame, "long", "time60")
    short_t, short_teq, short_tm = simulate(frame, "short", "time60")
    payload = {
        "hypothesis": (
            "Same 15m BTCUSD path, same entry score and ATR strikes. "
            "Long buys the strangle; short sells it. Marks are Black-Scholes "
            "from 24h realized vol, not Delta option prints."
        ),
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bars": int(len(frame)),
            "symbol": "BTCUSD",
            "resolution": "15m",
            "source": str(config.DELTA_BASE_URL),
        },
        "starting_equity": STARTING_EQUITY,
        "long": {"metrics": long_m, "trades": long_trades, "daily_equity": daily_equity(long_eq)},
        "short": {"metrics": short_m, "trades": short_trades, "daily_equity": daily_equity(short_eq)},
        "long_time60": {"metrics": long_tm, "daily_equity": daily_equity(long_teq), "trades": long_t},
        "short_time60": {"metrics": short_tm, "daily_equity": daily_equity(short_teq), "trades": short_t},
    }
    winner = "short" if short_m["net_pnl"] > long_m["net_pnl"] else "long"
    if short_m["net_pnl"] == long_m["net_pnl"]:
        winner = "tie"
    payload["winner_by_net_pnl"] = winner
    payload["winner_time60"] = (
        "short" if short_tm["net_pnl"] > long_tm["net_pnl"] else "long"
    )
    dd_winner = "short" if short_m["max_drawdown"] > long_m["max_drawdown"] else "long"
    payload["shallower_drawdown"] = dd_winner
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = run()
    print(json.dumps(
        {
            "range": result["range"],
            "long": result["long"]["metrics"],
            "short": result["short"]["metrics"],
            "long_time60": result["long_time60"]["metrics"],
            "short_time60": result["short_time60"]["metrics"],
            "winner_by_net_pnl": result["winner_by_net_pnl"],
            "shallower_drawdown": result["shallower_drawdown"],
        },
        indent=2,
    ))
    print("saved", OUT)
