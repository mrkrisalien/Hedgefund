"""Walk-forward parameter sweep for the NIFTY Dhan range-breakout study."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from broker import fetch_historical_intraday, resolve_symbol_cached, round_to_tick
from range_breakout_year_backtest import _prepare

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "nifty_range_breakout_optimization.json"
START_DATE = datetime.now(IST).date() - timedelta(days=365)
END_DATE = datetime.now(IST).date()
SPLIT_DATE = END_DATE - timedelta(days=92)


def _exit(session, start, entry, sl, tp):
    for index in range(start, len(session["close"])):
        hit_sl = session["low"][index] <= sl
        hit_tp = session["high"][index] >= tp
        if hit_sl and hit_tp:
            return sl, "SL_BOTH"
        if hit_sl:
            return sl, "SL"
        if hit_tp:
            return tp, "TP"
    return float(session["close"][-1]), "EOD"


def replay(sessions, params, first_date, last_date):
    trades = []
    lookback = params["lookback"]
    start_time = time.fromisoformat(params["start_time"])
    cutoff = time.fromisoformat(params["cutoff"])
    for session_date, session in sessions:
        length = len(session["close"])
        if not (first_date <= session_date <= last_date) or length <= lookback:
            continue
        for index in range(lookback, length):
            bar_time = session["time"][index]
            if bar_time < start_time:
                continue
            if bar_time >= cutoff:
                break
            trigger = float(np.max(session["high"][index - lookback : index]))
            touch = session["high"][index] >= trigger
            close_confirmed = session["close"][index] > trigger
            if params["confirmation"] == "close":
                if not close_confirmed:
                    continue
                entry = round_to_tick(session["close"][index], 0.05)
                exit_start = index + 1
            else:
                if not touch:
                    continue
                entry = round_to_tick(max(trigger, session["open"][index]), 0.05)
                exit_start = index
            if params["trend_filter"]:
                if (
                    index < 5
                    or session["close"][index - 1] <= session["ema20"][index - 1]
                ):
                    continue
                if session["ema20"][index - 1] <= session["ema20"][index - 5]:
                    continue
            sl = round_to_tick(session["low"][index - 1], 0.05)
            risk = entry - sl
            if risk <= 0:
                continue
            max_stop_pct = params["max_stop_pct"]
            if max_stop_pct is not None and risk / entry > max_stop_pct:
                continue
            if entry - trigger > 0.5 * max(trigger - sl, 0.05):
                continue
            tp = round_to_tick(entry + params["rr"] * risk, 0.05)
            if exit_start >= length:
                continue
            exit_price, reason = _exit(session, exit_start, entry, sl, tp)
            trades.append(
                {
                    "date": str(session_date),
                    "result_r": (exit_price - entry) / risk,
                    "reason": reason,
                }
            )
            break
    return trades


def metrics(trades):
    if not trades:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "net_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_r": 0.0,
        }
    values = [float(row["result_r"]) for row in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(values),
        "win_rate_pct": round(len(wins) / len(values) * 100, 2),
        "net_r": round(sum(values), 3),
        "expectancy_r": round(sum(values) / len(values), 4),
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(max_drawdown, 3),
    }


def selection_score(stats):
    if stats["trades"] < 35:
        return -9999.0
    profit_factor = float(stats.get("profit_factor") or 0)
    return (
        stats["expectancy_r"] * 100
        + stats["win_rate_pct"] * 0.10
        + stats["net_r"] * 0.20
        - abs(stats["max_drawdown_r"]) * 0.40
        + (profit_factor - 1.0) * 5
    )


def main():
    instrument = resolve_symbol_cached(
        {"symbol": "NIFTY", "exchange": "NSE", "instrument": "INDEX"}
    )
    raw = fetch_historical_intraday(
        instrument, START_DATE - timedelta(days=7), END_DATE, interval=5
    )
    frame = _prepare(raw)
    sessions = []
    for session_date, raw_session in frame.groupby("session_date", sort=True):
        session = raw_session[
            (raw_session["session_time"] >= time(9, 15))
            & (raw_session["session_time"] <= time(15, 30))
        ].reset_index(drop=True)
        closes = session["close"].astype(float)
        sessions.append(
            (
                session_date,
                {
                    "time": session["session_time"].tolist(),
                    "open": session["open"].astype(float).to_numpy(),
                    "high": session["high"].astype(float).to_numpy(),
                    "low": session["low"].astype(float).to_numpy(),
                    "close": closes.to_numpy(),
                    "ema20": closes.ewm(span=20, adjust=False).mean().to_numpy(),
                },
            )
        )

    grid = itertools.product(
        [12, 18, 24, 30, 36],
        [1.5, 2.0, 2.5, 3.0],
        ["touch", "close"],
        [False, True],
        ["10:15", "11:15"],
        ["14:30", "15:15"],
        [None, 0.001, 0.0015, 0.002],
    )
    results = []
    for (
        lookback,
        rr,
        confirmation,
        trend_filter,
        start_time,
        cutoff,
        max_stop_pct,
    ) in grid:
        params = {
            "lookback": lookback,
            "rr": rr,
            "confirmation": confirmation,
            "trend_filter": trend_filter,
            "start_time": start_time,
            "cutoff": cutoff,
            "max_stop_pct": max_stop_pct,
        }
        train = metrics(
            replay(sessions, params, START_DATE, SPLIT_DATE - timedelta(days=1))
        )
        test = metrics(replay(sessions, params, SPLIT_DATE, END_DATE))
        results.append(
            {
                "params": params,
                "train": train,
                "test": test,
                "selection_score": round(selection_score(train), 4),
            }
        )

    ranked = sorted(results, key=lambda row: row["selection_score"], reverse=True)
    baseline_params = {
        "lookback": 24,
        "rr": 3.0,
        "confirmation": "touch",
        "trend_filter": False,
        "start_time": "11:15",
        "cutoff": "15:15",
        "max_stop_pct": None,
    }
    baseline = {
        "params": baseline_params,
        "train": metrics(
            replay(
                sessions,
                baseline_params,
                START_DATE,
                SPLIT_DATE - timedelta(days=1),
            )
        ),
        "test": metrics(replay(sessions, baseline_params, SPLIT_DATE, END_DATE)),
    }
    robust = [
        row
        for row in ranked
        if row["test"]["trades"] >= 12
        and row["test"]["expectancy_r"] > 0
        and (row["test"]["profit_factor"] or 0) > 1
    ]
    candidate_params = {
        "safer_capped": {
            "lookback": 18,
            "rr": 1.5,
            "confirmation": "close",
            "trend_filter": True,
            "start_time": "11:15",
            "cutoff": "14:30",
            "max_stop_pct": 0.0015,
        },
        "higher_win_rate": {
            "lookback": 30,
            "rr": 1.5,
            "confirmation": "close",
            "trend_filter": False,
            "start_time": "11:15",
            "cutoff": "15:15",
            "max_stop_pct": None,
        },
        "balanced": {
            "lookback": 18,
            "rr": 2.0,
            "confirmation": "close",
            "trend_filter": True,
            "start_time": "10:15",
            "cutoff": "15:15",
            "max_stop_pct": 0.001,
        },
        "higher_reward": {
            "lookback": 18,
            "rr": 3.0,
            "confirmation": "close",
            "trend_filter": True,
            "start_time": "11:15",
            "cutoff": "14:30",
            "max_stop_pct": 0.0015,
        },
    }
    candidates = {
        name: {
            "params": params,
            "full_year": metrics(replay(sessions, params, START_DATE, END_DATE)),
        }
        for name, params in candidate_params.items()
    }
    payload = {
        "generated_at": datetime.now(IST).isoformat(),
        "source": "Dhan NIFTY 5-minute history",
        "period": {"start": str(START_DATE), "end": str(END_DATE)},
        "split": {
            "training_end": str(SPLIT_DATE - timedelta(days=1)),
            "test_start": str(SPLIT_DATE),
        },
        "combinations": len(results),
        "baseline": baseline,
        "retrospective_candidates": candidates,
        "top_by_training_only": ranked[:20],
        "training_selected_that_survived_test": robust[:20],
        "all_results": results,
        "warning": (
            "The final-three-month test is used only for validation, but trying "
            "1,280 combinations still creates multiple-testing risk."
        ),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(
        {
            "baseline": baseline,
            "best_training": ranked[0],
            "best_training_surviving_test": robust[0] if robust else None,
            "retrospective_candidates": candidates,
            "survivors": len(robust),
            "output": str(OUTPUT),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
