"""Counterfactual exits on the year trade log using cached 5-minute bars."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest import load_history, session_minutes, unique_equity_symbols
from broker import connect
from sectors import mcx_symbols

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
YEAR = ROOT / "backtest_results_year.json"
OUT = ROOT / "data" / "profit_lever_analysis.json"


def summarize(rows, starting_equity):
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "return_pct": 0.0,
            "avg_r": 0.0,
            "max_dd": 0.0,
            "profit_factor": 0.0,
        }
    pnls = [float(r["pnl"]) for r in rows]
    rs = [float(r["r"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = 100.0 * len(wins) / len(pnls)
    pnl = sum(pnls)
    gross_win = sum(wins) or 0.0
    gross_loss = abs(sum(losses)) or 0.0
    peak = starting_equity
    running = starting_equity
    max_dd = 0.0
    by_day = defaultdict(float)
    for row in rows:
        by_day[row["entry_date"]] += float(row["pnl"])
    for day in sorted(by_day):
        running += by_day[day]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "pnl": round(pnl, 2),
        "return_pct": round(100.0 * pnl / starting_equity, 3),
        "avg_r": round(sum(rs) / len(rs), 3),
        "max_dd": round(max_dd, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else 99.0,
    }


def walk(session, entry, sl, targets):
    risk = entry - sl
    if risk <= 0 or session is None or session.empty:
        return None
    mfe = 0.0
    mae = 0.0
    for _, bar in session.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        mfe = max(mfe, high - entry)
        mae = max(mae, entry - low)
        hit_sl = low <= sl
        hit_tps = [(name, px) for name, px in targets if high >= px]
        if hit_sl:
            return {
                "exit": sl,
                "reason": "SL",
                "mfe_r": mfe / risk,
                "mae_r": mae / risk,
            }
        if hit_tps:
            name, px = min(hit_tps, key=lambda item: item[1])
            return {
                "exit": px,
                "reason": name,
                "mfe_r": mfe / risk,
                "mae_r": mae / risk,
            }
    last = session.iloc[-1]
    return {
        "exit": float(last["close"]),
        "reason": "EOD",
        "mfe_r": mfe / risk,
        "mae_r": mae / risk,
    }


def apply_day_cap(rows, cap=2000.0):
    kept = []
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["entry_date"]].append(row)
    for day in sorted(by_day):
        day_pnl = 0.0
        halted = False
        for row in by_day[day]:
            if halted:
                continue
            kept.append(row)
            day_pnl += float(row["pnl"])
            if -day_pnl >= cap:
                halted = True
    return kept


def main():
    payload = json.loads(YEAR.read_text(encoding="utf-8"))
    starting = float(payload["starting_equity"])
    trades = [t for t in payload["trade_log"] if t.get("kind") == "NSE_EQ"]
    connect()
    today = datetime.now(IST).date()
    history_start = datetime(2025, 8, 1).date()
    print("Prefetching cached 5m bars...")
    for symbol in unique_equity_symbols():
        load_history(symbol, history_start, today, symbol, interval=5)
    for spec in mcx_symbols():
        load_history(spec, history_start, today, spec["symbol"], interval=5)

    enriched = []
    missed = 0
    for trade in trades:
        data = load_history(trade["symbol"], None, None, trade["symbol"], 5)
        if not data.get("ok"):
            missed += 1
            continue
        session_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
        rest = session_minutes(data["minute"], session_date, after_open_range=True)
        entry = float(trade["entry"])
        sl = float(trade["sl"])
        risk = entry - sl
        if risk <= 0:
            missed += 1
            continue
        qty = float(trade["qty"])
        costs = float(trade["costs"])
        half = walk(rest, entry, sl, [("0.5R", entry + 0.5 * risk)])
        one = walk(rest, entry, sl, [("1.0R", entry + 1.0 * risk)])
        one_five = walk(rest, entry, sl, [("1.5R", entry + 1.5 * risk)])
        short = rest.head(12) if rest is not None and not rest.empty else rest
        hour = walk(short, entry, sl, [("0.5R", entry + 0.5 * risk)])
        if not half or not one or not one_five or not hour:
            missed += 1
            continue

        def as_row(result):
            exit_px = result["exit"]
            pnl = (exit_px - entry) * qty - costs
            return {
                "entry_date": trade["entry_date"],
                "symbol": trade["symbol"],
                "sector": trade["sector"],
                "momentum": float(trade["momentum"]),
                "reason": result["reason"],
                "mfe_r": round(result["mfe_r"], 3),
                "mae_r": round(result["mae_r"], 3),
                "r": (exit_px - entry) / risk,
                "pnl": pnl,
            }

        enriched.append(
            {
                "base": as_row(one_five),
                "half": as_row(half),
                "one": as_row(one),
                "hour": as_row(hour),
                "mfe_r": one_five["mfe_r"],
                "mae_r": one_five["mae_r"],
                "trade": trade,
            }
        )

    def take(key, predicate=None, cap=True):
        rows = []
        for item in enriched:
            if predicate and not predicate(item["trade"], item):
                continue
            rows.append(item[key])
        if cap:
            rows = apply_day_cap(rows, 2000)
        return summarize(rows, starting)

    variants = {
        "cash_1.5R": take("base"),
        "take_half_target_0.5R": take("half"),
        "take_1R": take("one"),
        "exit_in_first_hour_0.5R": take("hour"),
        "0.5R_FIN_FMCG_only": take(
            "half",
            lambda src, _item: src["sector"] in ("NIFTY FIN SERVICE", "NIFTY FMCG"),
        ),
        "0.5R_momentum_ge_1pct": take("half", lambda src, _item: src["momentum"] >= 1.0),
        "0.5R_momATR_ge_0.5": take(
            "half", lambda src, _item: float(src.get("momentum_atr") or 0) >= 0.5
        ),
        "0.5R_skip_pharma": take(
            "half", lambda src, _item: src["sector"] != "NIFTY PHARMA"
        ),
        "1R_FIN_FMCG_only": take(
            "one",
            lambda src, _item: src["sector"] in ("NIFTY FIN SERVICE", "NIFTY FMCG"),
        ),
        "0.5R_no_day_cap": take("half", cap=False),
    }

    n = max(len(enriched), 1)
    touched_05 = sum(1 for item in enriched if item["mfe_r"] >= 0.5)
    touched_10 = sum(1 for item in enriched if item["mfe_r"] >= 1.0)
    touched_15 = sum(1 for item in enriched if item["mfe_r"] >= 1.5)
    never_05 = sum(1 for item in enriched if item["mfe_r"] < 0.5)
    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "cash_trades_analyzed": len(enriched),
        "missed": missed,
        "starting_equity": starting,
        "mfe_share": {
            "touched_0.5R": round(100.0 * touched_05 / n, 1),
            "touched_1.0R": round(100.0 * touched_10 / n, 1),
            "touched_1.5R": round(100.0 * touched_15 / n, 1),
            "never_reached_0.5R": round(100.0 * never_05 / n, 1),
        },
        "variants": variants,
        "note": (
            "Cash only. Same 9:20 BUY fills as the ATR 1.5R year. "
            "Exits replayed on cached 5-minute bars after 9:20. "
            "Same-bar SL+target counts as SL. Rs 2000 day cap unless noted."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"mfe": report["mfe_share"], "variants": variants}, indent=2))
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
