"""Compare reconstructed cycles against the hypothesized strangle rules."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import config
from delta_reconstruct import OUT_JSON, run as reconstruct

COMPARE_JSON = Path(__file__).resolve().parent / "data" / "delta_hypothesis_compare.json"


def compare(payload=None):
    payload = payload or reconstruct()
    cycles = payload.get("cycles") or []
    rows = []
    for index, cycle in enumerate(cycles, start=1):
        implied = float(cycle.get("implied_spot") or 0)
        actual_wing = float(cycle.get("wing_pct") or 0)
        hypothesized_width_pct = (
            config.DELTA_ATR_MULTIPLIER * config.DELTA_MIN_STRIKE_PCT * 100 * 2
        )
        # Mid-strike proxy: half-wing on each side vs ATR-style min distance.
        hypothesized_half = config.DELTA_ATR_MULTIPLIER * 1.2
        actual_half = actual_wing / 2 if actual_wing else 0
        hold = (
            float(cycle.get("hold_minutes_call") or 0)
            + float(cycle.get("hold_minutes_put") or 0)
        ) / 2
        scheduled = hold % 30 < 8 or hold % 60 < 8
        qty_fit = bool(cycle.get("qty_equal"))
        simultaneous = float(cycle.get("entry_gap_seconds") or 99) <= 30
        score = (
            (25 if simultaneous else 5)
            + (20 if qty_fit else 8)
            + (20 if 0.5 <= actual_half <= 8 else 6)
            + (20 if scheduled else 10)
            + (15 if cycle.get("pnl") is not None else 0)
        )
        rows.append(
            {
                "TRADE_ID": f"STRANGLE-{index:03d}",
                "ENTRY_TIME": cycle["call"]["entry_time"],
                "EXIT_TIME": cycle["call"]["exit_time"],
                "BTC_PRICE_AT_ENTRY": round(implied, 2),
                "CALL_STRIKE": cycle["call_strike"],
                "PUT_STRIKE": cycle["put_strike"],
                "CALL_PREMIUM": cycle["call"]["entry_price"],
                "PUT_PREMIUM": cycle["put"]["entry_price"],
                "QUANTITY": cycle["qty_call"],
                "STRIKE_DISTANCE_PERCENTAGE": actual_wing,
                "HOLDING_TIME": round(hold, 1),
                "REALIZED_PNL": cycle["pnl"],
                "RECONSTRUCTED_SIGNAL_SCORE": score,
                "entry_gap_seconds": cycle["entry_gap_seconds"],
                "qty_equal": qty_fit,
                "hypothesized_wing_pct_proxy": round(hypothesized_width_pct, 3),
                "scheduled_hold_hint": scheduled,
            }
        )
    frame = pd.DataFrame(rows)
    summary = payload["summary"]
    if not frame.empty:
        summary["mean_reconstructed_score"] = float(frame["RECONSTRUCTED_SIGNAL_SCORE"].mean())
        summary["pct_simultaneous_legs"] = float(
            (frame["entry_gap_seconds"] <= 30).mean() * 100
        )
        summary["pct_equal_qty"] = float(frame["qty_equal"].mean() * 100)
        summary["median_hold_minutes_cycles"] = float(frame["HOLDING_TIME"].median())
    COMPARE_JSON.write_text(
        json.dumps({"summary": summary, "cycles": rows}, indent=2, default=str),
        encoding="utf-8",
    )
    return {"summary": summary, "cycles": rows}


if __name__ == "__main__":
    result = compare()
    print(json.dumps(result["summary"], indent=2))
    print("saved", COMPARE_JSON)
