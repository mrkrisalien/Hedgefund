"""1-year automated regime strangle backtest on Delta India BTCUSD 1h candles."""
from __future__ import annotations

import json
import math
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import config
from delta_indicators import candles_to_frame, entry_score, snapshot
from delta_strategy import (
    choose_strikes,
    pick_regime,
    realized_iv,
    should_exit,
    size_contracts,
    structure_levels,
    week_loss_halted,
)

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "delta_strangle_year_backtest.json"
CONTRACT_MULT = 0.001
FEE_RATE = 0.0413
EVAL_EVERY = 2
LOOKBACK = 72
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


def fetch_btc(days=365, resolution="1h"):
    end = int(time.time())
    start = end - days * 86400
    url = str(config.DELTA_BASE_URL).rstrip("/") + "/v2/history/candles"
    rows = []
    cursor_end = end
    while cursor_end > start:
        response = requests.get(
            url,
            params={
                "symbol": "BTCUSD",
                "resolution": resolution,
                "start": start,
                "end": cursor_end,
            },
            timeout=30,
        )
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
        oldest = min(times)
        if oldest > 10_000_000_000:
            oldest = oldest / 1000
        if oldest <= start or len(chunk) < 20:
            break
        cursor_end = int(oldest) - 1
        if len(rows) > 25000:
            break
        time.sleep(0.15)
    frame = candles_to_frame(rows)
    stamp = pd.to_numeric(frame["time"], errors="coerce")
    if stamp.median() > 10_000_000_000:
        stamp = stamp / 1000
    frame["ts"] = pd.to_datetime(stamp, unit="s", utc=True).dt.tz_convert(IST)
    return frame.dropna(subset=["ts", "close", "high", "low"]).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def years_to_expiry(ts, expiry=None):
    local = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if getattr(local, "tzinfo", None) is None:
        local = local.replace(tzinfo=IST)
    if expiry is None:
        expiry = local.replace(hour=17, minute=30, second=0, microsecond=0)
        while expiry - local < timedelta(hours=6):
            expiry = expiry + timedelta(days=1)
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
        "reasons": pd.Series([t["reason"] for t in trades]).value_counts().to_dict() if trades else {},
        "by_side": pd.DataFrame(trades).groupby("side").agg(
            n=("pnl", "size"), wr=("pnl", lambda s: round((s > 0).mean() * 100, 1)), pnl=("pnl", "sum")
        ).to_dict() if trades else {},
        "by_month": (
            pd.DataFrame(trades)
            .assign(month=lambda d: pd.to_datetime(d["exit"]).dt.tz_convert(IST).dt.strftime("%Y-%m"))
            .groupby("month")
            .agg(
                n=("pnl", "size"),
                wr=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
                pnl=("pnl", "sum"),
            )
            .round(2)
            .to_dict()
            if trades
            else {}
        ),
    }


def weekly_equity(path):
    if not path:
        return []
    frame = pd.DataFrame(path)
    stamp = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(IST)
    frame["week"] = stamp.dt.strftime("%Y-%m-%d")
    weekly = frame.groupby(stamp.dt.to_period("W"))["equity"].last()
    rows = []
    for period, value in weekly.items():
        rows.append({"week": str(period.start_time.date()), "equity": round(float(value), 2)})
    if len(rows) > 56:
        rows = rows[:: max(1, len(rows) // 52)]
    return rows


def simulate(frame):
    equity = STARTING_EQUITY
    day_pnl = 0.0
    session = None
    position = None
    trades = []
    equity_path = []
    last_eval = -10
    realized = []

    for index in range(LOOKBACK, len(frame)):
        row = frame.iloc[index]
        window = frame.iloc[index - LOOKBACK : index + 1]
        snap = snapshot(window, float(row["close"]))
        ts = row["ts"]
        day = str(ts.date())
        if day != session:
            session = day
            day_pnl = 0.0
        iv = realized_iv(window)
        tte, expiry_guess = years_to_expiry(ts, None if not position else position.get("expiry"))
        minutes_left = tte * 365 * 24 * 60

        if position:
            call_p = bs_premium(snap["spot"], position["call"], tte, iv, True)
            put_p = bs_premium(snap["spot"], position["put"], tte, iv, False)
            mark = call_p + put_p
            side = position["side"]
            if side == "long":
                gross = position["qty"] * (mark - position["entry_sum"]) * CONTRACT_MULT
            else:
                gross = position["qty"] * (position["entry_sum"] - mark) * CONTRACT_MULT
            pos_view = {
                **position,
                "call_prem": position["call_prem"],
                "put_prem": position["put_prem"],
            }
            done, why, pnl_pct = should_exit(
                pos_view,
                call_p,
                put_p,
                snap,
                now=ts.to_pydatetime(),
                bar_close=float(row["close"]),
                minutes_to_expiry=minutes_left,
            )
            if done:
                pnl = gross - position["open_fee"] - fee(position["qty"], mark)
                equity += pnl
                day_pnl += pnl
                trades.append(
                    {
                        "side": side,
                        "entry": position["opened_at"].isoformat(),
                        "exit": ts.isoformat(),
                        "spot_in": round(position["spot"], 2),
                        "spot_out": round(float(row["close"]), 2),
                        "call": position["call"],
                        "put": position["put"],
                        "qty": position["qty"],
                        "stop_low": position.get("stop_low"),
                        "stop_high": position.get("stop_high"),
                        "hold_min": round((ts - position["opened_at"]).total_seconds() / 60, 1),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": why,
                        "iv": round(position["iv"], 3),
                    }
                )
                realized.append({"time": ts.to_pydatetime(), "pnl": pnl})
                position = None
                last_eval = index
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue

        if day_pnl <= -STARTING_EQUITY * config.DELTA_MAX_DAILY_LOSS_PCT:
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        halted, week_pnl = week_loss_halted(realized, ts.to_pydatetime(), equity)
        if halted:
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        if index - last_eval < EVAL_EVERY:
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        last_eval = index
        score = entry_score(snap, hour=ts.hour)
        side, reason = pick_regime(snap, score, iv)
        if side not in ("long", "short"):
            equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        levels = structure_levels(window, snap["atr"])
        if levels["stop_low"] is None or levels["stop_high"] is None:
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
        qty = max(1, min(size_contracts(equity, call_p * CONTRACT_MULT, put_p * CONTRACT_MULT), int(config.DELTA_MAX_CONTRACTS)))
        entry_sum = call_p + put_p
        position = {
            "opened_at": ts.to_pydatetime(),
            "spot": snap["spot"],
            "side": side,
            "breakout": snap.get("breakout"),
            "call": call_k,
            "put": put_k,
            "qty": qty,
            "entry_sum": entry_sum,
            "call_prem": call_p,
            "put_prem": put_p,
            "open_fee": fee(qty, entry_sum),
            "iv": iv,
            "width": width,
            "expiry": expiry_guess,
            "stop_low": levels["stop_low"],
            "stop_high": levels["stop_high"],
            "reason": reason,
        }
        equity_path.append({"time": ts.isoformat(), "equity": round(equity, 4)})

    if position:
        row = frame.iloc[-1]
        window = frame.iloc[-LOOKBACK:]
        snap = snapshot(window, float(row["close"]))
        iv = realized_iv(window)
        tte, _ = years_to_expiry(row["ts"], position.get("expiry"))
        call_p = bs_premium(snap["spot"], position["call"], tte, iv, True)
        put_p = bs_premium(snap["spot"], position["put"], tte, iv, False)
        mark = call_p + put_p
        if position["side"] == "long":
            gross = position["qty"] * (mark - position["entry_sum"]) * CONTRACT_MULT
        else:
            gross = position["qty"] * (position["entry_sum"] - mark) * CONTRACT_MULT
        pnl = gross - position["open_fee"] - fee(position["qty"], mark)
        equity += pnl
        trades.append(
            {
                "side": position["side"],
                "entry": position["opened_at"].isoformat(),
                "exit": row["ts"].isoformat(),
                "spot_in": round(position["spot"], 2),
                "spot_out": round(float(row["close"]), 2),
                "call": position["call"],
                "put": position["put"],
                "qty": position["qty"],
                "hold_min": round((row["ts"] - position["opened_at"]).total_seconds() / 60, 1),
                "pnl": round(pnl, 4),
                "pnl_pct": 0,
                "reason": "eod_flatten",
                "iv": round(position["iv"], 3),
            }
        )
        realized.append({"time": row["ts"].to_pydatetime(), "pnl": pnl})
        equity_path.append({"time": row["ts"].isoformat(), "equity": round(equity, 4)})
    return trades, equity_path, metrics(trades, equity_path, STARTING_EQUITY)


def run():
    frame = fetch_btc(365, "1h")
    start = frame["ts"].iloc[LOOKBACK]
    end = frame["ts"].iloc[-1]
    trades, eq, stats = simulate(frame)
    payload = {
        "rules": {
            "entry": "Long-only expansion/breakout. Flat if IV>=50%. No shorts. Pause new entries if last 7 days P&L <= -2% equity.",
            "hold": "Hold to +40% target. If still red at 6h, flatten. Hard cap 12h. Also -35% premium stop or 1h structure close. Weekly -2% pause. Fully automated.",
            "stop": "1h CLOSE beyond last confirmed lower-low / higher-high plus 0.5 ATR. Wicks ignored.",
            "safety": "Flatten 60m before expiry. Daily 3% book halt still applies.",
        },
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bars": int(len(frame)),
            "symbol": "BTCUSD",
            "resolution": "1h",
            "source": str(config.DELTA_BASE_URL),
        },
        "starting_equity": STARTING_EQUITY,
        "metrics": stats,
        "weekly_equity": weekly_equity(eq),
        "trades": trades,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = run()
    print(json.dumps({"range": result["range"], "metrics": result["metrics"]}, indent=2, default=str))
    print("saved", OUT)
