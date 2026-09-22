import json
import time
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from broker import connect, fetch_historical_intraday, resolve_symbol_cached
from morning_scan import OPEN_END
from risk import intraday_costs


IST = ZoneInfo("Asia/Kolkata")
BASE = Path(__file__).resolve().parent
TRADES_PATH = BASE / "backtest_results.json"
OUT_PATH = BASE / "rr_compare_results.json"


def parse_spec(name):
    if name.startswith("GOLDM"):
        return {"symbol": "GOLDM", "exchange": "MCX", "instrument": "FUTCOM"}
    if name.startswith("SILVERM"):
        return {"symbol": "SILVERM", "exchange": "MCX", "instrument": "FUTCOM"}
    if name.startswith("CRUDEOILM"):
        return {"symbol": "CRUDEOILM", "exchange": "MCX", "instrument": "FUTCOM"}
    return name.split("-")[0]


def annotate(frame):
    out = frame.copy()
    stamps = pd.to_datetime(out["time"], utc=True).dt.tz_convert(IST)
    out["ist_date"] = stamps.dt.date
    out["ist_time"] = stamps.dt.time
    return out


def fetch_minutes(instrument, start, end):
    for delay in (0, 2, 5, 10, 20):
        if delay:
            time.sleep(delay)
        try:
            return annotate(fetch_historical_intraday(instrument, start, end, interval=1))
        except Exception as error:
            message = str(error)
            if "DH-904" in message or "Rate_Limit" in message:
                print(f"  rate limit, retry in {delay or 2}s: {instrument['trading_symbol']}")
                continue
            raise
    raise RuntimeError(f"Could not fetch 1-min for {instrument['trading_symbol']}")


def simulate(entry, sl, tp, bars):
    if bars is None or bars.empty:
        return None, "no bars", None, 0.0

    mfe = 0.0
    for _, bar in bars.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        mfe = max(mfe, high - entry)
        hit_sl = low <= sl
        hit_tp = high >= tp
        when = pd.Timestamp(bar["time"]).isoformat()
        if hit_sl and hit_tp:
            return sl, "SL (both in bar)", when, mfe
        if hit_sl:
            return sl, "SL", when, mfe
        if hit_tp:
            return tp, "TP", when, mfe

    last = bars.iloc[-1]
    return float(last["close"]), "EOD square-off", pd.Timestamp(last["time"]).isoformat(), mfe


def net_pnl(entry, exit_price, qty):
    gross = (exit_price - entry) * qty
    costs = intraday_costs(entry, exit_price, qty)
    return gross - costs, costs


def summarize(rows):
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "exits": {},
        }
    frame = pd.DataFrame(rows)
    wins = int((frame["pnl"] > 0).sum())
    losses = int((frame["pnl"] < 0).sum())
    return {
        "trades": len(frame),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(frame) * 100, 2),
        "pnl": round(float(frame["pnl"].sum()), 2),
        "exits": {str(k): int(v) for k, v in frame["reason"].value_counts().to_dict().items()},
    }


def main():
    connect()
    source = json.loads(TRADES_PATH.read_text(encoding="utf-8"))
    trades = source["trade_log"]
    start = date.fromisoformat(source["window_start"])
    end = date.fromisoformat(source["window_end"])

    cache = {}
    results_3 = []
    results_5 = []
    touch = {"touched_3r": 0, "touched_5r": 0, "stopped_before_3r": 0}

    for trade in trades:
        spec = parse_spec(trade["symbol"])
        key = str(spec)
        if key not in cache:
            print(f"Loading 1-min {trade['symbol']}...")
            time.sleep(0.8)
            instrument = resolve_symbol_cached(spec)
            cache[key] = {
                "instrument": instrument,
                "minute": fetch_minutes(instrument, start, end),
            }

        session = date.fromisoformat(trade["entry_date"])
        minute = cache[key]["minute"]
        bars = minute[
            (minute["ist_date"] == session) & (minute["ist_time"] > OPEN_END)
        ].reset_index(drop=True)

        entry = float(trade["entry"])
        sl = float(trade["sl"])
        qty = int(trade["qty"])
        risk = entry - sl
        if risk <= 0:
            print(f"  skip {trade['symbol']} {session}: invalid stop")
            continue

        tp3 = entry + 3 * risk
        tp5 = entry + 5 * risk

        exit3, reason3, _, mfe = simulate(entry, sl, tp3, bars)
        exit5, reason5, _, _ = simulate(entry, sl, tp5, bars)
        if exit3 is None or exit5 is None:
            continue

        pnl3, _ = net_pnl(entry, exit3, qty)
        pnl5, _ = net_pnl(entry, exit5, qty)
        results_3.append({**trade, "reason": reason3, "exit": round(exit3, 2), "pnl": round(pnl3, 2), "tp": round(tp3, 2)})
        results_5.append({**trade, "reason": reason5, "exit": round(exit5, 2), "pnl": round(pnl5, 2), "tp": round(tp5, 2)})

        if mfe >= 3 * risk:
            touch["touched_3r"] += 1
        if mfe >= 5 * risk:
            touch["touched_5r"] += 1
        if mfe < 3 * risk and reason3 == "SL":
            touch["stopped_before_3r"] += 1

        print(
            f"  {session} {trade['symbol']}: 1:3 {reason3} {pnl3:.2f} | "
            f"1:5 {reason5} {pnl5:.2f} | MFE {mfe/risk:.2f}R"
        )

    n = max(len(results_3), 1)
    report = {
        "window_start": source["window_start"],
        "window_end": source["window_end"],
        "starting_equity": source["starting_equity"],
        "same_entries": True,
        "ratio_1_to_3": summarize(results_3),
        "ratio_1_to_5": summarize(results_5),
        "touch_stats": {
            "trades": len(results_3),
            "pct_price_reached_3R": round(touch["touched_3r"] / n * 100, 2),
            "pct_price_reached_5R": round(touch["touched_5r"] / n * 100, 2),
            "count_reached_3R": touch["touched_3r"],
            "count_reached_5R": touch["touched_5r"],
        },
        "trades_1_to_3": results_3,
        "trades_1_to_5": results_5,
    }
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\nCOMPARE COMPLETE")
    print(json.dumps({k: report[k] for k in ["ratio_1_to_3", "ratio_1_to_5", "touch_stats"]}, indent=2))
    print(f"Saved {OUT_PATH}")


if __name__ == "__main__":
    main()
