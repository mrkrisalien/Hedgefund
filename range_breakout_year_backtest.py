"""One-year NIFTY 5m rolling-range breakout replay using Dhan history.

Signal instrument is the non-tradable NIFTY spot index. Results are normalized
to R and Rs 1,000 risk per trade; they are not an option-premium backtest.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
from broker import fetch_historical_intraday, resolve_symbol_cached, round_to_tick

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "nifty_range_breakout_1y.json"
ENTRY_CUTOFF = time.fromisoformat(
    str(getattr(config, "COMBINED_SCAN_CUTOFF", "15:15"))
)
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def _prepare(frame):
    out = frame.copy()
    stamps = pd.to_datetime(out["time"], utc=True).dt.tz_convert(IST)
    out["ist_stamp"] = stamps
    out["session_date"] = stamps.dt.date
    out["session_time"] = stamps.dt.time
    for column in ("open", "high", "low", "close", "volume"):
        if column in out:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return (
        out.dropna(subset=["open", "high", "low", "close"])
        .drop_duplicates(subset=["ist_stamp"])
        .sort_values("ist_stamp")
        .reset_index(drop=True)
    )


def _exit_trade(session, entry_index, entry, sl, tp):
    for offset in range(entry_index, len(session)):
        bar = session.iloc[offset]
        low = float(bar["low"])
        high = float(bar["high"])
        hit_sl = low <= sl
        hit_tp = high >= tp
        if hit_sl and hit_tp:
            return sl, "SL_BOTH_CONSERVATIVE", offset
        if hit_sl:
            return sl, "SL", offset
        if hit_tp:
            return tp, "TP", offset
    return float(session.iloc[-1]["close"]), "EOD", len(session) - 1


def _volume_ratio(session, index, lookback=20):
    if "volume" not in session or index < lookback:
        return None
    history = session.iloc[index - lookback : index]["volume"].dropna()
    average = float(history.mean()) if not history.empty else 0.0
    current = float(session.iloc[index].get("volume") or 0)
    return (current / average) if average > 0 else None


def replay(frame, start_date, end_date, require_volume=False):
    lookback = int(getattr(config, "RANGE_BREAKOUT_LOOKBACK_BARS", 24))
    rr = float(getattr(config, "REWARD_RATIO", 3.0))
    volume_threshold = float(
        getattr(config, "BREAKOUT_VOLUME_SPIKE_RATIO", 1.5)
    )
    risk_rupees = float(getattr(config, "CATALYST_RISK_RS", 1000.0))
    trades = []
    eligible_days = 0

    for session_date, raw_session in frame.groupby("session_date", sort=True):
        if session_date < start_date or session_date > end_date:
            continue
        session = raw_session[
            (raw_session["session_time"] >= SESSION_OPEN)
            & (raw_session["session_time"] <= SESSION_CLOSE)
        ].reset_index(drop=True)
        if len(session) <= lookback:
            continue
        eligible_days += 1

        # The live manual slot takes at most one NIFTY signal per session.
        for index in range(lookback, len(session)):
            bar = session.iloc[index]
            if bar["session_time"] >= ENTRY_CUTOFF:
                break
            prior = session.iloc[index - lookback : index]
            trigger = float(prior["high"].max())
            previous = session.iloc[index - 1]
            sl = round_to_tick(float(previous["low"]), 0.05)
            if float(bar["high"]) < trigger or sl >= trigger:
                continue
            volume_ratio = _volume_ratio(session, index)
            if require_volume and (
                volume_ratio is None or volume_ratio < volume_threshold
            ):
                continue
            base_risk = trigger - sl
            entry = round_to_tick(max(trigger, float(bar["open"])), 0.05)
            # Match the live 0.5R option chase guard for opening gaps.
            if entry - trigger > 0.5 * base_risk:
                continue
            risk = entry - sl
            if risk <= 0:
                continue
            tp = round_to_tick(entry + rr * risk, 0.05)
            exit_price, reason, exit_index = _exit_trade(
                session, index, entry, sl, tp
            )
            result_r = (exit_price - entry) / risk
            trades.append(
                {
                    "date": str(session_date),
                    "entry_time": bar["ist_stamp"].isoformat(),
                    "exit_time": session.iloc[exit_index]["ist_stamp"].isoformat(),
                    "trigger": round(trigger, 2),
                    "entry": round(entry, 2),
                    "sl": round(sl, 2),
                    "tp": round(tp, 2),
                    "exit": round(exit_price, 2),
                    "exit_reason": reason,
                    "risk_points": round(risk, 2),
                    "result_r": round(result_r, 4),
                    "normalized_pnl_rs": round(result_r * risk_rupees, 2),
                    "volume_ratio": (
                        round(volume_ratio, 3)
                        if volume_ratio is not None
                        else None
                    ),
                }
            )
            break

    return trades, eligible_days


def summarize(trades, eligible_days):
    if not trades:
        return {"trades": 0, "eligible_days": eligible_days}
    results = [float(row["result_r"]) for row in trades]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for result in results:
        equity += result
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    exits = {}
    monthly = {}
    for row in trades:
        exits[row["exit_reason"]] = exits.get(row["exit_reason"], 0) + 1
        month = row["date"][:7]
        monthly[month] = monthly.get(month, 0.0) + float(row["result_r"])
    return {
        "eligible_days": eligible_days,
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "net_r": round(sum(results), 3),
        "normalized_net_pnl_rs": round(
            sum(float(row["normalized_pnl_rs"]) for row in trades), 2
        ),
        "expectancy_r": round(sum(results) / len(results), 4),
        "profit_factor": (
            round(gross_win / gross_loss, 3) if gross_loss > 0 else None
        ),
        "max_drawdown_r": round(max_drawdown, 3),
        "best_trade_r": round(max(results), 3),
        "worst_trade_r": round(min(results), 3),
        "exit_counts": exits,
        "monthly_r": {key: round(value, 3) for key, value in monthly.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args()

    end_date = datetime.now(IST).date()
    start_date = end_date - timedelta(days=max(1, args.days))
    instrument = resolve_symbol_cached(
        {"symbol": "NIFTY", "exchange": "NSE", "instrument": "INDEX"}
    )
    print(f"Loading Dhan NIFTY 5m history {start_date}..{end_date}...")
    raw = fetch_historical_intraday(
        instrument, start_date - timedelta(days=7), end_date, interval=5
    )
    if raw is None or raw.empty:
        raise RuntimeError("Dhan returned no NIFTY 5-minute history.")
    frame = _prepare(raw)
    unfiltered, eligible_days = replay(frame, start_date, end_date, False)
    volume_filtered, _ = replay(frame, start_date, end_date, True)
    payload = {
        "generated_at": datetime.now(IST).isoformat(),
        "source": "Dhan historical intraday API",
        "symbol": instrument,
        "period": {"start": str(start_date), "end": str(end_date)},
        "bars": len(frame),
        "method": {
            "timeframe": "5m",
            "range": "rolling prior 24 completed bars (2 hours)",
            "entry": "range high, or current open after an opening gap",
            "stop": "immediately previous completed 5m candle low",
            "target": f"1:{getattr(config, 'REWARD_RATIO', 3):g}",
            "frequency": "maximum one trade per day, entry before 15:15 IST",
            "same_bar_rule": "SL first when SL and TP are both touched",
            "pnl": "normalized at Rs 1,000 per 1R; excludes option behavior and costs",
        },
        "limitations": [
            "NIFTY spot is a signal instrument and cannot be traded directly.",
            "This is not a historical NIFTY option-premium or continuous-futures backtest.",
            "Historical option OI changes are not available in the candle response.",
            "Dhan NIFTY index volume can be synthetic/cumulative; volume results are experimental.",
        ],
        "unfiltered": {
            "summary": summarize(unfiltered, eligible_days),
            "trades": unfiltered,
        },
        "volume_1_5x_experimental": {
            "summary": summarize(volume_filtered, eligible_days),
            "trades": volume_filtered,
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(
        {
            "unfiltered": payload["unfiltered"]["summary"],
            "volume_1_5x_experimental": payload["volume_1_5x_experimental"]["summary"],
            "output": str(output),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
