"""Run cash-only then MCX-only month backtests in separate processes."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
IST = ZoneInfo("Asia/Kolkata")


def run_segment(segment):
    log_path = ROOT / f"backtest_run_{segment}.log"
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        result = subprocess.run(
            [str(PYTHON), "-u", str(ROOT / "backtest.py"), "--segment", segment],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise SystemExit(f"{segment} backtest failed with code {result.returncode}. See {log_path}")
    return ROOT / f"backtest_results_{segment}.json"


def pick(summary):
    return {
        "segment": summary.get("segment"),
        "starting_equity": summary.get("starting_equity"),
        "ending_equity": summary.get("ending_equity"),
        "total_pnl": summary.get("total_pnl"),
        "return_pct": summary.get("return_pct"),
        "trades": summary.get("trades"),
        "wins": summary.get("wins"),
        "losses": summary.get("losses"),
        "win_rate": summary.get("win_rate"),
        "max_drawdown": summary.get("max_drawdown"),
        "exit_reasons": summary.get("exit_reasons"),
        "by_symbol": summary.get("by_symbol"),
        "by_kind": summary.get("by_kind"),
        "go_live_recommendation": summary.get("go_live_recommendation"),
        "trade_log": summary.get("trade_log"),
    }


def main():
    print("Running independent cash backtest...")
    cash_path = run_segment("cash")
    print("Running independent MCX backtest...")
    mcx_path = run_segment("mcx")
    cash = json.loads(cash_path.read_text(encoding="utf-8"))
    mcx = json.loads(mcx_path.read_text(encoding="utf-8"))
    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "note": (
            "Cash and MCX segments are both enabled. These two runs isolate "
            "each book so P&L is not mixed and AI memory is not shared."
        ),
        "live_trading_enabled": False,
        "cash": pick(cash),
        "mcx": pick(mcx),
        "sum_pnl": round(float(cash.get("total_pnl") or 0) + float(mcx.get("total_pnl") or 0), 2),
    }
    out = ROOT / "independent_segment_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    compact = {
        "cash": {k: v for k, v in report["cash"].items() if k != "trade_log"},
        "mcx": {k: v for k, v in report["mcx"].items() if k != "trade_log"},
        "sum_pnl": report["sum_pnl"],
    }
    print(json.dumps(compact, indent=2))
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
