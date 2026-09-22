"""One-year BTC options proxy matrix for every Delta dashboard strategy.

Delta does not expose archived option-chain marks in this project. This replay
therefore uses real BTCUSD 15-minute candles and synthetic Black-Scholes option
marks derived from rolling realized volatility. Results are model estimates,
not historical option fills.
"""
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
OUT = ROOT / "data" / "delta_strategy_matrix_1y.json"
CACHE = ROOT / "data" / "delta_btcusd_15m_1y.json"
STRATEGIES = (
    "auto_both",
    "buy_straddle",
    "buy_strangle",
    "sell_straddle",
    "sell_strangle",
)
SCENARIOS = {
    "baseline": 0.0,
    "conservative_2pct_slippage": 0.02,
}
CONTRACT_MULT = 0.001
FEE_RATE = 0.0413
LOOKBACK = 96
EVAL_EVERY = 2
STRIKE_STEP = 200
STARTING_EQUITY = float(getattr(config, "DELTA_PAPER_EQUITY", 10000))


def _norm_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def bs_premium(spot, strike, years, iv, is_call):
    years = max(float(years), 15.0 / (365 * 24 * 60))
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


def years_to_expiry(ts, expiry=None):
    local = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if getattr(local, "tzinfo", None) is None:
        local = local.replace(tzinfo=IST)
    if expiry is None:
        expiry = local.replace(hour=17, minute=30, second=0, microsecond=0)
        while expiry - local < timedelta(hours=6):
            expiry += timedelta(days=1)
    seconds = (expiry - local).total_seconds()
    years = max(seconds / (365 * 24 * 3600), 15.0 / (365 * 24 * 60))
    return years, expiry


def fee(qty, premium):
    return abs(float(qty) * float(premium) * CONTRACT_MULT) * FEE_RATE


def fetch_btc(days=365, use_cache=True):
    if use_cache and CACHE.exists():
        try:
            rows = json.loads(CACHE.read_text(encoding="utf-8"))
            frame = pd.DataFrame(rows)
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(IST)
            if len(frame) >= 30000:
                return frame
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass

    end = int(time.time())
    start = end - int(days) * 86400
    url = str(config.DELTA_BASE_URL).rstrip("/") + "/v2/history/candles"
    rows = []
    cursor_end = end
    while cursor_end > start:
        response = requests.get(
            url,
            params={
                "symbol": "BTCUSD",
                "resolution": "15m",
                "start": start,
                "end": cursor_end,
            },
            timeout=45,
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
            oldest //= 1000
        if oldest <= start or len(chunk) < 20:
            break
        cursor_end = int(oldest) - 1
        if len(rows) > 60000:
            break
        time.sleep(0.15)

    frame = candles_to_frame(rows)
    stamp = pd.to_numeric(frame["time"], errors="coerce")
    if stamp.median() > 10_000_000_000:
        stamp /= 1000
    frame["ts"] = pd.to_datetime(stamp, unit="s", utc=True).dt.tz_convert(IST)
    frame = (
        frame.dropna(subset=["ts", "close", "high", "low"])
        .sort_values("ts")
        .drop_duplicates("ts")
        .reset_index(drop=True)
    )
    serial = frame.copy()
    serial["ts"] = serial["ts"].map(lambda value: value.isoformat())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps(serial[["time", "open", "high", "low", "close", "volume", "ts"]].to_dict("records")),
        encoding="utf-8",
    )
    return frame


def _entry_side(strategy, snap, score, iv):
    original = bool(getattr(config, "DELTA_ALLOW_SHORT", False))
    try:
        config.DELTA_ALLOW_SHORT = strategy.startswith("sell_")
        side, reason = pick_regime(snap, score, iv)
    finally:
        config.DELTA_ALLOW_SHORT = original
    required = (
        "long"
        if strategy.startswith("buy_")
        else "short"
        if strategy.startswith("sell_")
        else None
    )
    if required and side != required:
        return None, f"{strategy}: current regime {side or 'flat'}"
    return side, reason


def _strikes(strategy, spot, atr_value):
    if strategy.endswith("_straddle"):
        atm = int(round(float(spot) / STRIKE_STEP) * STRIKE_STEP)
        return atm, atm, 0.0
    available = list(
        range(
            int(spot * 0.9 / STRIKE_STEP) * STRIKE_STEP,
            int(spot * 1.1 / STRIKE_STEP) * STRIKE_STEP + STRIKE_STEP,
            STRIKE_STEP,
        )
    )
    return choose_strikes(spot, atr_value, available, available)


def _adverse_entry(mark, side, slippage):
    return mark * (1 + slippage if side == "long" else 1 - slippage)


def _adverse_exit(mark, side, slippage):
    return mark * (1 - slippage if side == "long" else 1 + slippage)


def _metrics(trades, curve):
    pnls = [float(row["pnl"]) for row in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    peak = STARTING_EQUITY
    max_dd = 0.0
    for row in curve:
        value = float(row["equity"])
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    trade_series = pd.Series(pnls, dtype=float)
    months = {}
    if trades:
        trade_frame = pd.DataFrame(trades)
        trade_frame["month"] = pd.to_datetime(trade_frame["exit"]).dt.strftime("%Y-%m")
        for month, group in trade_frame.groupby("month"):
            months[month] = {
                "trades": int(len(group)),
                "pnl": round(float(group["pnl"].sum()), 4),
                "win_rate_pct": round(float((group["pnl"] > 0).mean() * 100), 1),
            }
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "net_pnl": round(sum(pnls), 4),
        "return_pct": round(sum(pnls) / STARTING_EQUITY * 100, 3),
        "end_equity": round(STARTING_EQUITY + sum(pnls), 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd / STARTING_EQUITY * 100, 3),
        "avg_trade": round(float(trade_series.mean()), 4) if pnls else 0.0,
        "sharpe_trade": round(float(trade_series.mean() / trade_series.std()), 3)
        if len(pnls) > 1 and trade_series.std()
        else None,
        "avg_hold_min": round(
            sum(float(row["hold_min"]) for row in trades) / len(trades), 1
        )
        if trades
        else 0.0,
        "reasons": pd.Series(
            [row["reason"] for row in trades], dtype=str
        ).value_counts().to_dict(),
        "by_month": months,
    }


def simulate(frame, strategy, slippage=0.0):
    equity = STARTING_EQUITY
    session = None
    day_pnl = 0.0
    position = None
    trades = []
    curve = []
    realized = []
    last_eval = -10

    for index in range(LOOKBACK, len(frame)):
        row = frame.iloc[index]
        window = frame.iloc[index - LOOKBACK : index + 1]
        ts = row["ts"]
        day = str(ts.date())
        if day != session:
            session = day
            day_pnl = 0.0
        snap = snapshot(window, float(row["close"]))
        iv = realized_iv(window)
        tte, expiry = years_to_expiry(
            ts, position.get("expiry") if position else None
        )

        if position:
            call_mark = bs_premium(
                snap["spot"], position["call"], tte, iv, True
            )
            put_mark = bs_premium(
                snap["spot"], position["put"], tte, iv, False
            )
            raw_exit = call_mark + put_mark
            exit_sum = _adverse_exit(raw_exit, position["side"], slippage)
            done, reason, pnl_pct = should_exit(
                position,
                call_mark,
                put_mark,
                snap,
                now=ts.to_pydatetime(),
                bar_close=float(row["close"]),
                minutes_to_expiry=tte * 365 * 24 * 60,
            )
            if done:
                if position["side"] == "long":
                    gross = position["qty"] * (
                        exit_sum - position["entry_sum"]
                    ) * CONTRACT_MULT
                else:
                    gross = position["qty"] * (
                        position["entry_sum"] - exit_sum
                    ) * CONTRACT_MULT
                pnl = gross - position["open_fee"] - fee(
                    position["qty"], exit_sum
                )
                equity += pnl
                day_pnl += pnl
                hold = (ts - position["opened_at"]).total_seconds() / 60
                trades.append(
                    {
                        "strategy": strategy,
                        "side": position["side"],
                        "structure": position["structure"],
                        "entry": position["opened_at"].isoformat(),
                        "exit": ts.isoformat(),
                        "spot_in": round(position["spot"], 2),
                        "spot_out": round(float(row["close"]), 2),
                        "call": position["call"],
                        "put": position["put"],
                        "qty": position["qty"],
                        "entry_sum": round(position["entry_sum"], 4),
                        "exit_sum": round(exit_sum, 4),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "hold_min": round(hold, 1),
                        "reason": reason,
                    }
                )
                realized.append({"time": ts.to_pydatetime(), "pnl": pnl})
                position = None
                last_eval = index
            curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue

        if day_pnl <= -STARTING_EQUITY * config.DELTA_MAX_DAILY_LOSS_PCT:
            curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        halted, _ = week_loss_halted(
            realized, ts.to_pydatetime(), max(equity, 1)
        )
        if halted or index - last_eval < EVAL_EVERY:
            curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        last_eval = index
        score = entry_score(snap, hour=ts.hour)
        side, reason = _entry_side(strategy, snap, score, iv)
        if side not in ("long", "short"):
            curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        levels = structure_levels(window, snap["atr"])
        if levels["stop_low"] is None or levels["stop_high"] is None:
            curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})
            continue
        call_k, put_k, width = _strikes(
            strategy, snap["spot"], snap["atr"]
        )
        call_p = bs_premium(snap["spot"], call_k, tte, iv, True)
        put_p = bs_premium(snap["spot"], put_k, tte, iv, False)
        raw_entry = call_p + put_p
        entry_sum = _adverse_entry(raw_entry, side, slippage)
        qty = max(
            1,
            min(
                size_contracts(
                    equity,
                    call_p * CONTRACT_MULT,
                    put_p * CONTRACT_MULT,
                ),
                int(config.DELTA_MAX_CONTRACTS),
            ),
        )
        position = {
            "opened_at": ts,
            "spot": snap["spot"],
            "side": side,
            "strategy": strategy,
            "structure": "straddle"
            if strategy.endswith("_straddle")
            else "strangle",
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
            "expiry": expiry,
            "stop_low": levels["stop_low"],
            "stop_high": levels["stop_high"],
            "reason": reason,
        }
        curve.append({"time": ts.isoformat(), "equity": round(equity, 4)})

    if position:
        row = frame.iloc[-1]
        window = frame.iloc[-LOOKBACK:]
        snap = snapshot(window, float(row["close"]))
        iv = realized_iv(window)
        tte, _ = years_to_expiry(row["ts"], position["expiry"])
        raw_exit = bs_premium(
            snap["spot"], position["call"], tte, iv, True
        ) + bs_premium(snap["spot"], position["put"], tte, iv, False)
        exit_sum = _adverse_exit(raw_exit, position["side"], slippage)
        gross = (
            position["qty"] * (exit_sum - position["entry_sum"]) * CONTRACT_MULT
            if position["side"] == "long"
            else position["qty"]
            * (position["entry_sum"] - exit_sum)
            * CONTRACT_MULT
        )
        pnl = gross - position["open_fee"] - fee(position["qty"], exit_sum)
        equity += pnl
        trades.append(
            {
                "strategy": strategy,
                "side": position["side"],
                "structure": position["structure"],
                "entry": position["opened_at"].isoformat(),
                "exit": row["ts"].isoformat(),
                "spot_in": round(position["spot"], 2),
                "spot_out": round(float(row["close"]), 2),
                "call": position["call"],
                "put": position["put"],
                "qty": position["qty"],
                "entry_sum": round(position["entry_sum"], 4),
                "exit_sum": round(exit_sum, 4),
                "pnl": round(pnl, 4),
                "pnl_pct": 0.0,
                "hold_min": round(
                    (row["ts"] - position["opened_at"]).total_seconds() / 60,
                    1,
                ),
                "reason": "final_flatten",
            }
        )
        curve.append(
            {"time": row["ts"].isoformat(), "equity": round(equity, 4)}
        )
    return {
        "metrics": _metrics(trades, curve),
        "trades": trades,
    }


def _selection(results):
    eligible = []
    for strategy, scenarios in results.items():
        stats = scenarios["conservative_2pct_slippage"]["metrics"]
        pf = stats.get("profit_factor") or 0
        if (
            stats["net_pnl"] > 0
            and pf > 1.0
            and stats["trades"] >= 100
            and stats["max_drawdown"] < 0
        ):
            score = stats["return_pct"] / abs(stats["max_drawdown_pct"])
            eligible.append((score, strategy, stats))
    if not eligible:
        return {
            "strategy": "no_trade",
            "reason": (
                "No strategy remained profitable with profit factor above 1 "
                "after the 2% per-side premium slippage stress."
            ),
        }
    eligible.sort(reverse=True)
    score, strategy, stats = eligible[0]
    return {
        "strategy": strategy,
        "score_return_over_drawdown": round(score, 3),
        "reason": (
            "Highest conservative return-to-drawdown score among strategies "
            "with positive P&L, PF > 1, and at least 100 trades."
        ),
        "metrics": stats,
    }


def run():
    frame = fetch_btc(365)
    if len(frame) <= LOOKBACK:
        raise RuntimeError(f"Insufficient BTCUSD history: {len(frame)} bars")
    results = {}
    for strategy in STRATEGIES:
        results[strategy] = {}
        for scenario, slippage in SCENARIOS.items():
            print(f"[BACKTEST] {strategy} | {scenario}")
            results[strategy][scenario] = simulate(
                frame, strategy, slippage=slippage
            )
    payload = {
        "methodology": {
            "data": (
                "Real Delta BTCUSD 15-minute spot candles; synthetic "
                "Black-Scholes option premiums from rolling realized IV."
            ),
            "warning": (
                "Delta historical option-chain marks are unavailable. This is "
                "a model backtest, not an option-fill replay."
            ),
            "fees": f"{FEE_RATE:.4f} of option notional on entry and exit.",
            "scenarios": {
                "baseline": "Fees, no slippage.",
                "conservative_2pct_slippage": (
                    "Fees plus 2% adverse premium movement on entry and exit."
                ),
            },
            "entry": (
                "Live engine regime and score gates. Buy strategies accept "
                "long regimes; sell strategies accept compression/short regimes."
            ),
            "exit": "Shared live should_exit rules, structure stops, and caps.",
        },
        "range": {
            "start": frame["ts"].iloc[LOOKBACK].isoformat(),
            "end": frame["ts"].iloc[-1].isoformat(),
            "bars": int(len(frame)),
            "symbol": "BTCUSD",
            "resolution": "15m",
            "source": str(config.DELTA_BASE_URL),
        },
        "starting_equity": STARTING_EQUITY,
        "results": results,
    }
    payload["recommendation"] = _selection(results)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    report = run()
    compact = {
        strategy: {
            scenario: value["metrics"]
            for scenario, value in scenarios.items()
        }
        for strategy, scenarios in report["results"].items()
    }
    print(
        json.dumps(
            {
                "range": report["range"],
                "results": compact,
                "recommendation": report["recommendation"],
            },
            indent=2,
        )
    )
    print("saved", OUT)
