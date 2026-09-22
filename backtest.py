import argparse
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
from ai_brain import get_ai_decision
from broker import (
    connect,
    fetch_historical_daily,
    fetch_historical_intraday,
    get_equity,
    resolve_symbol_cached,
    round_to_tick,
)
from morning_scan import OPEN_END, OPEN_START, opening_momentum_from_frame, resolve_index
from risk import (
    book_loss_hit,
    build_entry_plan,
    daily_sma,
    intraday_costs,
    pick_best_candidates,
    session_loss_hit,
    signal_gate,
    size_position,
)
from trade_memory import TradeMemory
from sector_regime import allowed_calendar_sectors, passes_rs_gate, sector_allowed, tilt_score, trailing_return
from sectors import SECTORS, mcx_symbols


IST = ZoneInfo("Asia/Kolkata")
RESULTS_PATH = Path(__file__).resolve().parent / "backtest_results.json"
ACTIVE_SEGMENT = "both"


def segment_results_path(segment):
    if segment == "cash":
        return Path(__file__).resolve().parent / "backtest_results_cash.json"
    if segment == "mcx":
        return Path(__file__).resolve().parent / "backtest_results_mcx.json"
    return RESULTS_PATH
CACHE = {}


def to_ist_date(series):
    return pd.to_datetime(series, utc=True).dt.tz_convert(IST).dt.date


def annotate(frame):
    if frame is None or frame.empty:
        return frame
    out = frame.copy()
    stamps = pd.to_datetime(out["time"], utc=True).dt.tz_convert(IST)
    out["ist_date"] = stamps.dt.date
    out["ist_time"] = stamps.dt.time
    return out


def load_history(symbol_spec, history_start, today, label, interval=1):
    key = f"{symbol_spec}|{interval}"
    if key in CACHE:
        return CACHE[key]

    if history_start is None or today is None:
        payload = {"ok": False, "error": "history not prefetched"}
        CACHE[key] = payload
        return payload

    print(f"  Loading {label} ({interval}m)...")
    try:
        instrument = (
            resolve_index(symbol_spec)
            if isinstance(symbol_spec, list)
            else resolve_symbol_cached(symbol_spec)
        )
        daily = annotate(
            fetch_historical_daily(instrument, history_start, today)
        )
        minute = annotate(
            fetch_historical_intraday(
                instrument, history_start, today, interval=interval
            )
        )
        payload = {
            "instrument": instrument,
            "daily": daily,
            "minute": minute,
            "ok": True,
            "interval": interval,
        }
        print(
            f"    {label}: {0 if daily is None else len(daily)} daily / "
            f"{0 if minute is None else len(minute)} intraday bars"
        )
    except Exception as error:
        print(f"  Failed {label}: {error}")
        payload = {"ok": False, "error": str(error)}

    CACHE[key] = payload
    return payload


def session_minutes(minute_df, session_date, after_open_range=True):
    if minute_df is None or minute_df.empty:
        return minute_df
    rows = minute_df[minute_df["ist_date"] == session_date]
    if after_open_range:
        rows = rows[rows["ist_time"] > OPEN_END]
    return rows.reset_index(drop=True)


def hourly_from_minutes(minute_df, before_date, bars=24):
    if minute_df is None or minute_df.empty:
        return pd.DataFrame()
    prior = minute_df[minute_df["ist_date"] < before_date]
    if prior.empty:
        return pd.DataFrame()
    indexed = prior.set_index(pd.to_datetime(prior["time"], utc=True))
    aggregations = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in indexed.columns:
        aggregations["volume"] = "sum"
    hourly = indexed.resample("1h").agg(aggregations).dropna().tail(bars).reset_index()
    if hourly.columns[0] != "time":
        hourly = hourly.rename(columns={hourly.columns[0]: "time"})
    return hourly


def simulate_intraday(signal, sl, tp, session):
    if session is None or session.empty:
        return None, "no session bars", None

    for _, bar in session.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        bar_time = pd.Timestamp(bar["time"]).isoformat()
        if signal == "BUY":
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp

        if hit_sl and hit_tp:
            return sl, "SL (both levels in bar, conservative)", bar_time
        if hit_sl:
            return sl, "SL", bar_time
        if hit_tp:
            return tp, "TP", bar_time

    last = session.iloc[-1]
    return float(last["close"]), "EOD square-off", pd.Timestamp(last["time"]).isoformat()


def pnl_for_trade(signal, entry, exit_price, quantity):
    if signal == "BUY":
        gross = (exit_price - entry) * quantity
    else:
        gross = (entry - exit_price) * quantity
    costs = intraday_costs(entry, exit_price, quantity)
    return gross - costs, costs


def session_open_momentum(data, session_date):
    mom = opening_momentum_from_frame(data.get("minute"), session_date)
    if mom is not None:
        return mom
    daily = data.get("daily")
    if daily is None or daily.empty:
        return None
    today_rows = daily[daily["ist_date"] == session_date]
    prior = daily[daily["ist_date"] < session_date]
    if today_rows.empty or prior.empty:
        return None
    prev_close = float(prior.iloc[-1]["close"])
    open_px = float(today_rows.iloc[0]["open"])
    if prev_close <= 0:
        return None
    return (open_px - prev_close) / prev_close


def unique_equity_symbols():
    names = []
    seen = set()
    for spec in SECTORS.values():
        for symbol in spec["stocks"]:
            if symbol not in seen:
                seen.add(symbol)
                names.append(symbol)
    return names


def rules_replay_decision(display, daily_window, momentum):
    """Deterministic stand-in for DeepSeek on long windows (same BUY-only gates)."""
    mom = float(momentum or 0)
    close = float(daily_window.iloc[-1]["close"])
    sma = daily_sma(daily_window, 10)
    if mom <= 0:
        return {
            "signal": "HOLD",
            "confidence_score": 0,
            "logic": "rules replay: opening range not positive",
        }
    if close < sma:
        return {
            "signal": "HOLD",
            "confidence_score": 40,
            "logic": "rules replay: close below SMA10",
        }
    confidence = int(max(65, min(92, 65 + mom * 2500)))
    return {
        "signal": "BUY",
        "confidence_score": confidence,
        "logic": (
            f"rules replay {display}: +{mom:.2%} open, close {close:.2f} > SMA10 {sma:.2f}"
        ),
    }


def _macro_from_cache(session_date):
    crude_ret = gold_ret = None
    lookback = int(getattr(config, "SECTOR_RS_LOOKBACK", 20))
    mapping = {
        "CRUDEOILM": "crude",
        "GOLDM": "gold",
        "CRUDEOIL": "crude",
        "GOLD": "gold",
    }
    for payload in CACHE.values():
        if not payload.get("ok"):
            continue
        inst = payload.get("instrument") or {}
        name = str(inst.get("symbol") or inst.get("trading_symbol") or "")
        key = None
        for needle, bucket in mapping.items():
            if needle in name.upper():
                key = bucket
                break
        if not key:
            continue
        value = trailing_return(payload.get("daily"), session_date, lookback)
        if key == "crude" and crude_ret is None:
            crude_ret = value
        if key == "gold" and gold_ret is None:
            gold_ret = value
    return crude_ret, gold_ret


def rank_day(session_date, interval=1):
    ranked_sectors = []
    crude_ret, gold_ret = _macro_from_cache(session_date)
    lookback = int(getattr(config, "SECTOR_RS_LOOKBACK", 20))
    if ACTIVE_SEGMENT in ("both", "cash") and config.ENABLE_CASH_SEGMENT:
        allowed = allowed_calendar_sectors(session_date)
        if allowed is not None and not allowed:
            ranked_sectors = []
        for sector_name, spec in SECTORS.items():
            if not sector_allowed(sector_name, session_date):
                continue
            data = load_history(spec["index_names"], None, None, sector_name, interval)
            if not data.get("ok"):
                continue
            momentum = session_open_momentum(data, session_date)
            if momentum is None:
                continue
            rs = trailing_return(data.get("daily"), session_date, lookback)
            lock = getattr(config, "CALENDAR_SECTOR_LOCK", False)
            if getattr(config, "USE_SECTOR_TILT", True) and not lock and not passes_rs_gate(rs):
                continue
            score = (
                tilt_score(sector_name, momentum, rs, session_date, crude_ret, gold_ret)
                if getattr(config, "USE_SECTOR_TILT", True)
                else float(momentum)
            )
            ranked_sectors.append(
                {
                    "sector": sector_name,
                    "momentum": momentum,
                    "rs_20d": rs,
                    "tilt_score": score,
                    "stocks": spec["stocks"],
                }
            )
        key = "tilt_score" if getattr(config, "USE_SECTOR_TILT", True) else "momentum"
        ranked_sectors.sort(key=lambda row: row.get(key) or 0, reverse=True)
    take = (
        len(ranked_sectors)
        if getattr(config, "CALENDAR_SECTOR_LOCK", False)
        else int(getattr(config, "TOP_SECTORS", 2))
    )
    top = ranked_sectors[:take]

    equities = []
    if ACTIVE_SEGMENT in ("both", "cash") and config.ENABLE_CASH_SEGMENT:
        for sector in top:
            scored = []
            for symbol in sector["stocks"]:
                data = load_history(symbol, None, None, symbol, interval)
                if not data.get("ok"):
                    continue
                momentum = session_open_momentum(data, session_date)
                if momentum is None or momentum <= 0:
                    continue
                scored.append(
                    {
                        "symbol": symbol,
                        "display": data["instrument"]["trading_symbol"],
                        "sector": sector["sector"],
                        "momentum": momentum,
                        "kind": "NSE_EQ",
                        "history": data,
                    }
                )
            scored.sort(key=lambda row: row["momentum"], reverse=True)
            equities.extend(scored[: config.TOP_STOCKS_PER_SECTOR])

    commodities = []
    if ACTIVE_SEGMENT in ("both", "mcx") and config.ENABLE_MCX_SEGMENT:
        for spec in mcx_symbols():
            label = spec["symbol"]
            data = load_history(spec, None, None, label, interval)
            if not data.get("ok"):
                continue
            momentum = session_open_momentum(data, session_date)
            if momentum is None:
                continue
            if config.MCX_REQUIRE_POSITIVE_MOMENTUM and momentum <= 0:
                continue
            commodities.append(
                {
                    "symbol": spec,
                    "display": data["instrument"]["trading_symbol"],
                    "sector": "MCX",
                    "momentum": momentum,
                    "kind": "MCX",
                    "history": data,
                }
            )
        commodities.sort(key=lambda row: row["momentum"], reverse=True)
        if config.ONE_METAL_AT_A_TIME:
            commodities = commodities[:1]

    return top, equities + commodities


def run_backtest(segment="both", days=30, skip_ai=False, interval=None, output=None):
    global ACTIVE_SEGMENT
    segment = str(segment or "both").strip().lower()
    if segment not in ("both", "cash", "mcx"):
        raise ValueError(f"Unknown segment '{segment}'")
    ACTIVE_SEGMENT = segment
    CACHE.clear()

    days = max(5, int(days))
    if interval is None:
        interval = 5 if days > 40 else 1
    interval = int(interval)
    if skip_ai is None:
        skip_ai = days >= 90

    _, funds = connect()
    starting_equity = get_equity(funds) or 100000.0
    equity = starting_equity

    today = datetime.now(IST).date()
    test_start = today - timedelta(days=days)
    history_start = test_start - timedelta(days=40)
    if output:
        out_path = Path(output)
        if not out_path.is_absolute():
            out_path = Path(__file__).resolve().parent / out_path
    elif days >= 300:
        out_path = Path(__file__).resolve().parent / "backtest_results_year.json"
    else:
        out_path = segment_results_path(segment)
    print(f"Starting equity: {starting_equity:.2f}")
    print(f"Window: {test_start} to {today} (IST)  days={days} interval={interval}m")
    print(f"Segment: {segment}")
    print(f"Decision engine: {'rules replay (no DeepSeek)' if skip_ai else 'DeepSeek'}")
    print("Live orders remain disabled.")

    if segment in ("both", "cash"):
        print("\nPrefetching sector indices...")
        for sector_name, spec in SECTORS.items():
            load_history(spec["index_names"], history_start, today, sector_name, interval)

        print("Prefetching equity constituents...")
        for symbol in unique_equity_symbols():
            load_history(symbol, history_start, today, symbol, interval)

    if segment in ("both", "mcx") or getattr(config, "USE_MACRO_SECTOR_TILT", False):
        print("Prefetching MCX contracts (execution and/or crude-gold tilt)...")
        for spec in mcx_symbols():
            load_history(spec, history_start, today, spec["symbol"], interval)

    session_days = set()
    for payload in CACHE.values():
        if payload.get("ok") and payload.get("daily") is not None and not payload["daily"].empty:
            session_days.update(payload["daily"]["ist_date"].tolist())
    session_days = sorted(d for d in session_days if test_start <= d <= today)

    memory = TradeMemory(persist=False)
    trades = []
    scan_log = []
    equity_curve = [{"date": str(test_start), "equity": round(starting_equity, 2)}]

    for session_date in session_days:
        top, candidates = rank_day(session_date, interval)
        scan_log.append(
            {
                "date": str(session_date),
                "sectors": [
                    {
                        "sector": row["sector"],
                        "momentum": round(row["momentum"] * 100, 3),
                    }
                    for row in top
                ],
                "candidates": [
                    {
                        "symbol": row["display"],
                        "sector": row["sector"],
                        "momentum": round(row["momentum"] * 100, 3),
                    }
                    for row in candidates
                ],
            }
        )
        print(
            f"\n{session_date} sectors: "
            + ", ".join(
                f"{row['sector']} {row['momentum']*100:.2f}%" for row in top
            )
        )

        cash_pnl_today = 0.0
        mcx_pnl_today = 0.0
        day_start_equity = equity

        planned = []
        for candidate in candidates:
            data = candidate["history"]
            instrument = data["instrument"]
            daily = data["daily"]
            daily_window = daily[daily["ist_date"] < session_date].tail(20)
            if len(daily_window) < 8:
                continue
            hourly_window = hourly_from_minutes(data["minute"], session_date, bars=24)
            plan, plan_reason = build_entry_plan(
                data["minute"],
                session_date,
                "BUY",
                instrument["tick_size"],
                daily_window,
                hourly_df=hourly_window,
                use_hourly=candidate["kind"] == "MCX" and config.COMMODITY_USE_HOURLY_ATR,
            )
            if not plan:
                print(f"  {candidate['display']}: skip ({plan_reason})")
                continue
            row = dict(candidate)
            row["plan"] = plan
            row["daily_window"] = daily_window
            planned.append(row)

        selected = pick_best_candidates(planned)
        if planned and not selected:
            selected = []
        if selected:
            print(
                "  Best setups: "
                + ", ".join(
                    f"{row['display']} score {row['plan']['score']:.2f} "
                    f"mom/ATR {row['plan']['momentum_atr']}"
                    for row in selected
                )
            )

        for candidate in selected:
            session_pnl = cash_pnl_today + mcx_pnl_today
            if session_loss_hit(session_pnl):
                print(
                    f"  [SYSTEM] Session loss cap Rs {config.MAX_DAY_LOSS_RS:.0f} "
                    f"hit ({session_pnl:.2f}). No more trades today."
                )
                break
            is_commodity = candidate["kind"] == "MCX"
            book = "MCX" if is_commodity else "NSE_EQ"
            book_pnl = mcx_pnl_today if is_commodity else cash_pnl_today
            if book_loss_hit(day_start_equity, book_pnl, book):
                print(f"  {book} daily loss cap reached. Other book still open.")
                continue
            data = candidate["history"]
            instrument = data["instrument"]
            display = candidate["display"]
            daily_window = candidate.get("daily_window")
            if daily_window is None or len(daily_window) < 8:
                continue
            plan = candidate["plan"]
            extra = (
                f"Opening-range sector: {candidate['sector']}. "
                f"9:15-9:20 momentum: {candidate['momentum']:.4%}. "
                f"mom/ATR={plan['momentum_atr']} SL/ATR={plan['sl_atr']}. "
                f"Previous 5m low SL, 1:{int(config.REWARD_RATIO)} target."
            )

            try:
                if skip_ai:
                    decision = rules_replay_decision(
                        display, daily_window, candidate["momentum"]
                    )
                else:
                    market_data = {
                        "daily_csv": daily_window.drop(
                            columns=["ist_date", "ist_time"], errors="ignore"
                        ).to_csv(index=False),
                        "equity": equity,
                        "ask_price": plan["entry"],
                    }
                    decision = get_ai_decision(
                        market_data, display, extra, book=book, memory=memory
                    )
            except Exception as error:
                print(f"  {display}: AI error ({error})")
                continue

            signal = str(decision.get("signal", "HOLD")).strip().upper()
            logic = str(decision.get("logic", ""))
            try:
                confidence = float(decision.get("confidence_score", 0))
            except (TypeError, ValueError):
                confidence = 0

            allowed, gate_reason = signal_gate(signal, confidence, daily_window)
            print(
                f"  {display} ({candidate['sector']}) {candidate['momentum']*100:.2f}% "
                f"| {signal} {confidence:.0f}% | {gate_reason}"
            )
            if not allowed:
                continue

            entry = plan["entry"]
            sl = plan["sl"]
            tp = plan["tp"]
            atr = plan["atr"]
            quantity, size_reason = size_position(
                equity, entry, sl, instrument["lot_size"], book=book
            )
            if quantity < 1:
                print(f"  {display}: {size_reason}")
                continue
            rest = session_minutes(data["minute"], session_date, after_open_range=True)
            exit_price, reason, exit_time = simulate_intraday("BUY", sl, tp, rest)
            if exit_price is None:
                continue

            net, costs = pnl_for_trade("BUY", entry, exit_price, quantity)
            equity += net
            if is_commodity:
                mcx_pnl_today += net
            else:
                cash_pnl_today += net
            trade_row = {
                "entry_date": str(session_date),
                "symbol": display,
                "sector": candidate["sector"],
                "kind": candidate["kind"],
                "momentum": round(candidate["momentum"] * 100, 3),
                "signal": "BUY",
                "confidence": round(confidence, 1),
                "entry": round(entry, 2),
                "sl": round(sl, 2),
                "tp": round(tp, 2),
                "exit": round(exit_price, 2),
                "qty": quantity,
                "pnl": round(net, 2),
                "costs": round(costs, 2),
                "reason": reason,
                "atr": round(atr, 2),
                "momentum_atr": plan.get("momentum_atr"),
                "sl_atr": plan.get("sl_atr"),
                "score": round(float(plan.get("score") or 0), 3),
                "prior_5m_low": plan.get("prior_low"),
                "gate": gate_reason,
                "logic": logic,
                "exit_time": exit_time,
                "equity_after": round(equity, 2),
            }
            trades.append(trade_row)
            memory.record(trade_row)
            equity_curve.append(
                {"date": str(session_date), "equity": round(equity, 2)}
            )

    closed = pd.DataFrame(trades)
    wins = int((closed["pnl"] > 0).sum()) if not closed.empty else 0
    losses = int((closed["pnl"] < 0).sum()) if not closed.empty else 0
    flats = int((closed["pnl"] == 0).sum()) if not closed.empty else 0
    total_pnl = float(closed["pnl"].sum()) if not closed.empty else 0.0
    win_rate = (wins / len(closed) * 100) if len(closed) else 0.0
    max_dd = 0.0
    peak = starting_equity
    running = starting_equity
    daily_pnl = {}
    if not closed.empty:
        daily_pnl = closed.groupby("entry_date")["pnl"].sum().to_dict()
    curve = [{"date": str(test_start), "equity": round(starting_equity, 2)}]
    for day in session_days:
        running += float(daily_pnl.get(str(day), 0.0))
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
        curve.append({"date": str(day), "equity": round(running, 2)})

    by_symbol = []
    by_sector = []
    by_kind = []
    if not closed.empty:
        for symbol, row in closed.groupby("symbol")["pnl"].agg(["count", "sum"]).iterrows():
            by_symbol.append(
                {
                    "symbol": symbol,
                    "trades": int(row["count"]),
                    "pnl": round(float(row["sum"]), 2),
                }
            )
        for sector, row in closed.groupby("sector")["pnl"].agg(["count", "sum"]).iterrows():
            by_sector.append(
                {
                    "sector": sector,
                    "trades": int(row["count"]),
                    "pnl": round(float(row["sum"]), 2),
                }
            )
        for kind, row in closed.groupby("kind")["pnl"].agg(["count", "sum"]).iterrows():
            by_kind.append(
                {
                    "book": kind,
                    "trades": int(row["count"]),
                    "pnl": round(float(row["sum"]), 2),
                }
            )

    reasons = {}
    if not closed.empty:
        reasons = {
            str(key): int(value)
            for key, value in closed["reason"].value_counts().to_dict().items()
        }

    summary = {
        "generated_at": datetime.now(IST).isoformat(),
        "segment": segment,
        "cash_segment_enabled": config.ENABLE_CASH_SEGMENT,
        "mcx_segment_enabled": config.ENABLE_MCX_SEGMENT,
        "strategy": (
            f"{'5m-low stop' if config.USE_FIVE_MINUTE_STOP else 'ATR stop'}, "
            f"1:{config.REWARD_RATIO:g} target, momentum/ATR filter, "
            f"best {config.BEST_CASH_ENTRIES} cash + {config.BEST_MCX_ENTRIES} MCX, "
            f"session cap Rs {config.MAX_DAY_LOSS_RS:.0f}"
        ),
        "window_start": str(test_start),
        "window_end": str(today),
        "days": days,
        "bar_interval_minutes": interval,
        "decision_engine": "rules_replay" if skip_ai else "deepseek",
        "starting_equity": round(starting_equity, 2),
        "ending_equity": round(equity, 2),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round((equity / starting_equity - 1) * 100, 3),
        "trades": len(closed),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": round(win_rate, 2),
        "max_drawdown": round(max_dd, 2),
        "reward_ratio": config.REWARD_RATIO,
        "min_confidence": config.MIN_CONFIDENCE,
        "cash_risk_pct": config.CASH_RISK_PCT,
        "mcx_lots": config.MCX_LOTS,
        "one_metal": config.ONE_METAL_AT_A_TIME,
        "hourly_mcx_atr": config.COMMODITY_USE_HOURLY_ATR,
        "mcx_mini": config.MCX_USE_MINI,
        "calendar_sector_lock": bool(getattr(config, "CALENDAR_SECTOR_LOCK", False)),
        "paper_trade": bool(getattr(config, "PAPER_TRADE", False)),
        "max_day_loss_rs": config.MAX_DAY_LOSS_RS,
        "live_trading_enabled": config.TRADING_ENABLED,
        "by_symbol": by_symbol,
        "by_sector": by_sector,
        "by_kind": by_kind,
        "exit_reasons": reasons,
        "equity_curve": curve,
        "scan_log": scan_log,
        "trade_log": trades,
        "go_live_recommendation": (
            "Do not enable live or paper"
            if total_pnl <= 0
            else "In-sample green: paper only. Do not send live Dhan orders."
        ),
    }
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\nBACKTEST COMPLETE")
    print("=" * 60)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in [
                    "segment",
                    "window_start",
                    "window_end",
                    "starting_equity",
                    "ending_equity",
                    "total_pnl",
                    "return_pct",
                    "trades",
                    "wins",
                    "losses",
                    "win_rate",
                    "max_drawdown",
                    "exit_reasons",
                    "go_live_recommendation",
                ]
            },
            indent=2,
        )
    )
    print(f"\nSaved {out_path}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--segment",
        choices=("both", "cash", "mcx"),
        default="both",
        help="Run cash only, MCX only, or both books in one pass.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback calendar days for the test window.",
    )
    parser.add_argument(
        "--skip-ai",
        action="store_true",
        help="Use rules replay instead of DeepSeek (recommended for year runs).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Intraday bar minutes. 0 = auto (1m under 40 days, else 5m).",
    )
    parser.add_argument(
        "--output",
        default="",
        help="JSON results path (relative to project or absolute).",
    )
    args = parser.parse_args()
    skip_ai = bool(args.skip_ai) or args.days >= 90
    interval = None if int(args.interval or 0) <= 0 else int(args.interval)
    run_backtest(
        segment=args.segment,
        days=args.days,
        skip_ai=skip_ai,
        interval=interval,
        output=args.output or None,
    )
