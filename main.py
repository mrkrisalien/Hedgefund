import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from ai_brain import get_ai_decision, load_learned_rules
from auditor import load_rules_document, run_audit
from catalyst_setup import analyze_watchlist
from broker import (
    check_trading_ip,
    connect,
    format_ip_status,
    get_equity,
    resolve_symbol_cached,
)
from data_engine import fetch_correlated_asset_prices, fetch_multi_timeframe_data
from delta_engine import delta_loop, delta_state, evaluate_once
from delta_charts_feed import delta_charts, invalidate_delta_chart_cache
from delta_universe import (
    MARKETS as DELTA_MARKETS,
    STRATEGIES as DELTA_STRATEGIES,
    load_delta_universe,
    save_delta_universe,
)
from execution import close_position, evaluate_entry_plan, execute_trade, get_open_positions
from sectors import sector_for_symbol
from memory_store import (
    append_trade_memory,
    live_entries_today,
    live_pending_orders_today,
    realized_pnl_today,
    reconcile_closed_trades,
    symbol_already_used_today,
    sync_live_order_statuses,
)
from rung_trail import manage_rung_trails
from market_pulse import market_pulse
from morning_scan import morning_universe
from paper_book import (
    get_eod_report as paper_eod_report,
    open_positions as paper_open_positions,
    realized_pnl_today as paper_realized_pnl_today,
    refresh_positions as refresh_paper_positions,
    save_session as save_paper_session,
    session_state as paper_session_state,
    trade_history as paper_trade_history,
    write_due_eod_reports,
)
from charts_feed import invalidate_chart_cache, watchlist_charts
from risk import book_loss_hit, pick_best_candidates, session_square_off_reason
from trade_memory import LIVE_MEMORY
from watchlist import (
    infer_option_market,
    load_watchlist,
    normalize_option_contract,
    save_watchlist,
)


IST = ZoneInfo("Asia/Kolkata")
SCAN_LOCK = time.fromisoformat(
    str(getattr(config, "LIVE_ENTRY_START", "09:25"))
)
SCAN_CUTOFF = time.fromisoformat(
    str(getattr(config, "COMBINED_SCAN_CUTOFF", "15:15"))
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
INDEX_FILE = TEMPLATES_DIR / "index.html"


bot_state = {
    "is_running": False,
    "interval": int(getattr(config, "SCAN_INTERVAL_SECONDS", 30)),
    "risk_percent": float(getattr(config, "DEFAULT_RISK_PERCENT", 1.0)),
    "equity": 0.0,
    "trading_enabled": bool(config.TRADING_ENABLED),
    "paper_trade": bool(getattr(config, "PAPER_TRADE", False)),
    "broker_authenticated": False,
    "broker_error": "",
    "last_logic": "",
    "last_confidence": 0,
    "last_signal": "HOLD",
    "last_entry_price": None,
    "last_stop_loss": None,
    "last_take_profit": None,
    "trade_history": [],
    "open_positions": [],
    "pending_orders": [],
    "learned_rules": [],
    "last_audit_at": None,
    "broker": "Dhan",
    "session_date": "",
    "top_sectors": [],
    "candidates": [],
    "orders_placed": False,
    "filled_sources": [],
    "day_start_equity": 0.0,
    "cash_pnl_today": 0.0,
    "mcx_pnl_today": 0.0,
    "watchlist": [],
    "breakout_alerts": [],
    "confirmed_fills": 0,
}


class ControlRequest(BaseModel):
    is_running: bool | None = None
    action: str | None = None
    interval: int | None = None
    risk_percent: float | None = None
    kill_switch: bool | None = None


class WatchlistName(BaseModel):
    rank: int = 99
    name: str
    symbol: str | None = None
    exchange: str | None = None
    instrument: str | None = None
    strategy: str = "breakout"
    catalyst: str = ""
    chase_risk: str = "medium"


class WatchlistRequest(BaseModel):
    date: str = ""
    names: list[WatchlistName]


class DeltaUniverseItem(BaseModel):
    rank: int = 99
    symbol: str
    market: str = "futures"
    strategy: str = "auto_both"
    confirmed: bool = False
    confirmed_date: str = ""


class DeltaUniverseRequest(BaseModel):
    instruments: list[DeltaUniverseItem]


class CloseRequest(BaseModel):
    ticket: str


class AuditRequest(BaseModel):
    force: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(config, "PAPER_TRADE", False):
        _restore_paper_session()
    try:
        _, funds = connect()
        bot_state["equity"] = get_equity(funds)
        if (
            getattr(config, "PAPER_TRADE", False)
            and not bot_state.get("day_start_equity")
        ):
            bot_state["day_start_equity"] = bot_state["equity"]
            _save_paper_session()
        bot_state["broker_authenticated"] = True
        bot_state["broker_error"] = ""
        print("[SYSTEM] Dhan initialized successfully.")
        print(f"[SYSTEM] Available balance: {bot_state['equity']}")
        print("[SYSTEM] Open the dashboard at http://127.0.0.1:8000  (not http://0.0.0.0:8000)")
        try:
            print("[SYSTEM] " + format_ip_status(check_trading_ip()))
        except Exception as ip_error:
            print(f"[SYSTEM] Dhan IP check failed: {ip_error}")
        try:
            n = reconcile_closed_trades()
            print(f"[LEARNING] Startup reconcile: {n}")
        except Exception as error:
            print(f"[SYSTEM] Startup reconcile failed: {error}")
        doc = load_rules_document()
        bot_state["learned_rules"] = doc.get("rules") or []
        bot_state["last_audit_at"] = doc.get("last_audit_at")
        if (doc.get("trades_analyzed") or 0) >= config.AUDIT_TRADE_THRESHOLD:
            try:
                run_audit(force=False)
            except Exception as error:
                print(f"[AUDITOR] Startup audit skipped: {error}")
    except Exception as error:
        bot_state["broker_authenticated"] = False
        bot_state["broker_error"] = str(error)
        bot_state["is_running"] = False
        print("[SYSTEM] Dhan initialization failed.")
        print(f"[SYSTEM] Dhan error: {error}")

    dhan_task = asyncio.create_task(trading_loop())
    alert_task = asyncio.create_task(breakout_alert_loop())
    delta_task = asyncio.create_task(delta_loop())
    paper_task = asyncio.create_task(paper_maintenance_loop())
    yield
    _save_paper_session()
    dhan_task.cancel()
    alert_task.cancel()
    delta_task.cancel()
    paper_task.cancel()
    for task in (dhan_task, alert_task, delta_task, paper_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("[SYSTEM] Dhan and Delta desks closed.")


app = FastAPI(title="Autonomous AI Hedge Fund", lifespan=lifespan)
DESIGN_ASSET_DIR = (
    Path.home() / ".cursor" / "projects" / "e-Python-Hedgefund" / "assets"
)
if DESIGN_ASSET_DIR.exists():
    app.mount(
        "/design-assets",
        StaticFiles(directory=str(DESIGN_ASSET_DIR)),
        name="design-assets",
    )


@app.get("/", response_class=HTMLResponse)
async def home():
    if INDEX_FILE.exists():
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=500)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/market/pulse")
async def get_market_pulse():
    try:
        payload = await asyncio.to_thread(market_pulse)
        return {"ok": True, **payload}
    except Exception as error:
        return {"ok": False, "error": str(error)}


@app.post("/api/control")
async def control_bot(request: ControlRequest):
    action = (request.action or "").lower()
    requested_start = action == "start" or request.is_running is True
    if requested_start:
        cash_pnl, mcx_pnl = realized_pnl_today()
        if getattr(config, "PAPER_TRADE", False):
            cash_pnl += paper_realized_pnl_today()
        bot_state["cash_pnl_today"] = cash_pnl
        bot_state["mcx_pnl_today"] = mcx_pnl
        realized = cash_pnl + mcx_pnl
        cap_reason = session_square_off_reason(realized)
        if cap_reason:
            bot_state["is_running"] = False
            limit = (
                config.MAX_DAY_LOSS_RS
                if cap_reason == "loss"
                else config.MAX_DAY_PROFIT_RS
            )
            kind = "loss" if cap_reason == "loss" else "profit"
            return {
                "success": False,
                "error": (
                    f"Start blocked: today's realized {kind} is Rs {abs(realized):.2f}; "
                    f"the session {kind} square-off is Rs {limit:.0f}."
                ),
                "bot_state": bot_state,
            }
        if not config.TRADING_ENABLED and not getattr(config, "PAPER_TRADE", False):
            bot_state["is_running"] = False
            return {
                "success": False,
                "error": "Start blocked: live Dhan trading is disabled in config.py.",
                "bot_state": bot_state,
            }
        if not bot_state.get("broker_authenticated"):
            return {
                "success": False,
                "error": "Dhan authentication failed. Generate a fresh access token and restart.",
                "bot_state": bot_state,
            }
        bot_state["is_running"] = True
    elif action == "stop":
        bot_state["is_running"] = False
    elif request.is_running is not None:
        bot_state["is_running"] = request.is_running
    if request.interval is not None:
        if request.interval < 1:
            return {"success": False, "error": "Interval must be at least 1 second."}
        bot_state["interval"] = request.interval
    if request.risk_percent is not None and request.risk_percent > 0:
        bot_state["risk_percent"] = float(request.risk_percent)
        config.DEFAULT_RISK_PERCENT = float(request.risk_percent)
        config.CASH_RISK_PCT = float(request.risk_percent) / 100.0
    _save_paper_session()
    return {"success": True, "bot_state": bot_state}


@app.get("/api/status")
async def get_status():
    try:
        bot_state["equity"] = get_equity()
        bot_state["broker_authenticated"] = True
        bot_state["broker_error"] = ""
        bot_state["open_positions"] = get_open_positions()
        if getattr(config, "PAPER_TRADE", False):
            bot_state["open_positions"] += paper_open_positions()
            bot_state["trade_history"] = paper_trade_history()
            bot_state["cash_pnl_today"] = (
                sum(realized_pnl_today())
                + paper_realized_pnl_today()
            )
        else:
            bot_state["pending_orders"] = live_pending_orders_today()
    except Exception as error:
        bot_state["broker_authenticated"] = False
        bot_state["broker_error"] = str(error)
        bot_state["is_running"] = False
        bot_state["open_positions"] = []
    doc = load_rules_document()
    bot_state["learned_rules"] = doc.get("rules") or []
    bot_state["last_audit_at"] = doc.get("last_audit_at")
    bot_state["confirmed_fills"] = len(bot_state.get("trade_history") or [])
    bot_state["paper_trade"] = bool(getattr(config, "PAPER_TRADE", False))
    bot_state["trading_enabled"] = bool(config.TRADING_ENABLED)
    bot_state["catalyst_mode"] = bool(getattr(config, "USE_CATALYST_WATCHLIST", False))
    payload = dict(bot_state)
    payload["watchlist"] = _public_watchlist()
    if getattr(config, "PAPER_TRADE", False):
        payload["paper_session"] = paper_session_state()
        payload["paper_eod_report"] = paper_eod_report()
    return payload


@app.get("/api/paper/eod-report")
async def get_paper_eod_report(date: str = ""):
    report = await asyncio.to_thread(paper_eod_report, date or None)
    if report is None:
        return {
            "ok": False,
            "error": f"No paper EOD report saved for {date or datetime.now(IST).date()}.",
        }
    return {"ok": True, "report": report}


def _public_watchlist():
    rows = bot_state.get("watchlist") or []
    if rows and isinstance(rows[0], dict) and "ok" in rows[0]:
        out = []
        for row in rows:
            plan = row.get("plan") or {}
            out.append(
                {
                    "rank": row.get("rank"),
                    "name": row.get("name"),
                    "symbol": row.get("symbol"),
                    "input_symbol": row.get("input_symbol"),
                    "exchange": row.get("exchange"),
                    "instrument": row.get("instrument"),
                    "strategy": row.get("strategy") or "breakout",
                    "catalyst": row.get("catalyst"),
                    "chase_risk": row.get("chase_risk"),
                    "ok": row.get("ok"),
                    "reason": row.get("reason"),
                    "entry": plan.get("entry"),
                    "sl": plan.get("sl"),
                    "tp": plan.get("tp"),
                    "qty": row.get("qty"),
                    "setup": plan.get("setup_kind"),
                    "risk_rs": row.get("risk_rs"),
                    "trigger": plan.get("trigger"),
                    "prior_low": plan.get("prior_low"),
                    "prior_high": plan.get("prior_high"),
                    "prior_time": plan.get("prior_time"),
                    "broke": plan.get("broke"),
                    "alert_state": plan.get("alert_state"),
                    "breakout_time": plan.get("breakout_time"),
                    "volume_ratio_5m": plan.get("volume_ratio_5m"),
                    "volume_spike": plan.get("volume_spike"),
                    "volume_status": plan.get("volume_status"),
                    "oi": plan.get("oi"),
                    "oi_change_pct": plan.get("oi_change_pct"),
                    "oi_spike": plan.get("oi_spike"),
                    "oi_status": plan.get("oi_status"),
                    "breakout_markers": [
                        marker
                        for marker in bot_state.get("breakout_alerts", [])
                        if marker.get("symbol") == row.get("symbol")
                    ],
                }
            )
        return out
    return load_watchlist().get("names") or []


@app.post("/api/watchlist")
async def api_watchlist(request: WatchlistRequest):
    names = []
    seen = set()
    for item in request.names:
        row = item.model_dump()
        row["name"] = str(row.get("name") or row.get("symbol") or "").strip()
        row["symbol"] = str(row.get("symbol") or "").strip().upper() or None
        contract = normalize_option_contract(row["symbol"] or row["name"])
        requested_instrument = str(row.get("instrument") or "").strip().upper()
        if requested_instrument in {"OPTIDX", "OPTFUT"} and not contract:
            return {
                "ok": False,
                "error": (
                    "Option format must include expiry and strike, for example "
                    "'NIFTY 15 SEP 25000 CE' or "
                    "'CRUDEOILM 17 SEP 9900 CALL'."
                ),
            }
        if contract:
            inferred_exchange, inferred_instrument = infer_option_market(contract)
            row["name"] = contract
            row["symbol"] = contract
            row["exchange"] = str(row.get("exchange") or inferred_exchange).upper()
            row["instrument"] = str(
                row.get("instrument") or inferred_instrument
            ).upper()
            try:
                resolved = resolve_symbol_cached(
                    {
                        "symbol": contract,
                        "exchange": row["exchange"],
                        "instrument": row["instrument"],
                    }
                )
            except Exception as error:
                return {"ok": False, "error": f"Contract not found on Dhan: {error}"}
            row["name"] = resolved.get("display_symbol") or contract
            row["exchange"] = resolved.get("exchange") or row["exchange"]
            row["instrument"] = resolved.get("instrument") or row["instrument"]
        else:
            row["exchange"] = str(row.get("exchange") or "NSE").upper()
            row["instrument"] = str(row.get("instrument") or "EQUITY").upper()
        row["strategy"] = str(row.get("strategy") or "breakout").strip().lower()
        if row["strategy"] not in {
            "breakout",
            "range_breakout",
            "consolidation",
            "trend_break",
        }:
            return {"ok": False, "error": f"Unsupported strategy: {row['strategy']}"}
        key = (
            row["exchange"],
            row["instrument"],
            row["symbol"] or row["name"].upper(),
        )
        if not key or key in seen:
            continue
        seen.add(key)
        row["rank"] = len(names) + 1
        row["chase_risk"] = str(row.get("chase_risk") or "medium").lower()
        names.append(row)
    doc = {
        "date": request.date or str(datetime.now(IST).date()),
        "names": names,
    }
    save_watchlist(doc)
    bot_state["watchlist"] = doc["names"]
    await asyncio.to_thread(invalidate_chart_cache)
    if datetime.now(IST).time() < SCAN_CUTOFF:
        bot_state["orders_placed"] = len(bot_state.get("filled_sources") or []) >= int(
            getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10)
        )
        bot_state["last_logic"] = (
            f"Watchlist updated with {len(names)} instrument(s); "
            f"{len(bot_state.get('filled_sources') or [])}/"
            f"{int(getattr(config, 'LIVE_MAX_ENTRIES_PER_DAY', 10))} live slots filled."
        )
    return {"ok": True, "count": len(doc["names"]), "document": doc}


@app.get("/api/watchlist")
async def api_watchlist_get():
    return load_watchlist()


@app.get("/api/charts")
async def api_charts():
    try:
        charts = await asyncio.to_thread(watchlist_charts, 120)
        return {"ok": True, "interval": "1m-live", "charts": charts}
    except Exception as error:
        return {"ok": False, "error": str(error), "charts": []}


@app.post("/api/close")
async def api_close(request: CloseRequest):
    try:
        result = close_position(request.ticket)
        print(f"[REVERSAL] Closed {request.ticket}: {result}")
        return {"success": True, "result": result}
    except Exception as error:
        return {"success": False, "error": str(error)}


@app.post("/api/audit")
async def api_audit(request: AuditRequest):
    try:
        result = await asyncio.to_thread(run_audit, bool(request.force))
    except Exception as error:
        result = {"ok": False, "reason": "error", "message": str(error), "document": load_rules_document()}
    if not isinstance(result, dict):
        result = {"ok": False, "reason": "error", "message": str(result)}
    result.setdefault("message", result.get("reason") or "done")
    doc = result.get("document") or load_rules_document()
    bot_state["learned_rules"] = doc.get("rules") or []
    bot_state["last_audit_at"] = doc.get("last_audit_at")
    return result


@app.get("/api/rules")
async def api_rules():
    return load_rules_document()


@app.post("/api/delta/control")
async def control_delta(request: ControlRequest):
    if request.kill_switch is not None:
        delta_state["kill_switch"] = bool(request.kill_switch)
        if delta_state["kill_switch"]:
            delta_state["is_running"] = False
            delta_state["state"] = "IDLE"
            delta_state["last_logic"] = "Kill switch engaged from dashboard."
    if request.is_running is not None and not delta_state.get("kill_switch"):
        delta_state["is_running"] = request.is_running
    if request.action == "start" and not delta_state.get("kill_switch"):
        delta_state["is_running"] = True
    if request.action == "stop":
        delta_state["is_running"] = False
    if request.interval is not None:
        if request.interval < 30:
            return {"success": False, "error": "Delta interval must be at least 30 seconds."}
        delta_state["interval"] = request.interval
    return {"success": True, "bot_state": delta_state}


@app.get("/api/delta/status")
async def get_delta_status():
    delta_state["keys_configured"] = bool(str(config.DELTA_API_KEY or "").strip())
    delta_state["mode"] = (
        "PAPER" if config.DELTA_PAPER or not config.DELTA_TRADING_ENABLED else "LIVE"
    )
    delta_state["universe"] = load_delta_universe()["instruments"]
    delta_state["rules"] = [
        {"rule": "Daily confirmation", "value": "Required before entry", "status": "active"},
        {"rule": "Execution universe", "value": "BTC options only", "status": "active"},
        {
            "rule": "Daily loss stop",
            "value": f"{config.DELTA_MAX_DAILY_LOSS_PCT:.1%}",
            "status": "active",
        },
        {
            "rule": "Weekly loss pause",
            "value": f"{config.DELTA_WEEKLY_LOSS_PCT:.1%}",
            "status": "active",
        },
        {
            "rule": "Premium stop",
            "value": f"{config.DELTA_EMERGENCY_PREMIUM_STOP_PCT:.0f}%",
            "status": "active",
        },
        {
            "rule": "Profit target",
            "value": f"{config.DELTA_PROFIT_TARGET_PCT:.0f}%",
            "status": "active",
        },
    ]
    cycles_path = BASE_DIR / "data" / "delta_hypothesis_compare.json"
    if cycles_path.exists():
        try:
            payload = json.loads(cycles_path.read_text(encoding="utf-8"))
            delta_state["reconstruction"] = payload.get("summary", {})
            delta_state["reconstructed_cycles"] = (payload.get("cycles") or [])[:12]
        except Exception:
            pass
    return delta_state


@app.get("/api/delta/universe")
async def get_delta_universe():
    return load_delta_universe()


@app.post("/api/delta/universe")
async def update_delta_universe(request: DeltaUniverseRequest):
    rows = [item.model_dump() for item in request.instruments]
    for row in rows:
        row["market"] = str(row.get("market") or "").lower()
        row["strategy"] = str(row.get("strategy") or "").lower()
        if row["market"] not in DELTA_MARKETS:
            return {"ok": False, "error": f"Unsupported market: {row['market']}"}
        if row["strategy"] not in DELTA_STRATEGIES:
            return {"ok": False, "error": f"Unsupported strategy: {row['strategy']}"}
    doc = await asyncio.to_thread(save_delta_universe, rows)
    await asyncio.to_thread(invalidate_delta_chart_cache)
    return {"ok": True, "document": doc}


@app.get("/api/delta/charts")
async def get_delta_charts():
    try:
        charts = await asyncio.to_thread(delta_charts, 120)
        return {"ok": True, "interval": "5m-live", "charts": charts}
    except Exception as error:
        return {"ok": False, "error": str(error), "charts": []}


@app.post("/api/delta/evaluate")
async def delta_evaluate_now():
    if delta_state.get("kill_switch"):
        return {"success": False, "error": "Kill switch is on.", "bot_state": delta_state}
    await asyncio.to_thread(evaluate_once)
    return {"success": True, "bot_state": delta_state}


def _save_paper_session():
    if not getattr(config, "PAPER_TRADE", False):
        return
    session_date = bot_state.get("session_date") or str(datetime.now(IST).date())
    save_paper_session(
        session_date,
        filled_sources=list(bot_state.get("filled_sources") or []),
        orders_placed=bool(bot_state.get("orders_placed")),
        day_start_equity=float(bot_state.get("day_start_equity") or 0),
        is_running=bool(bot_state.get("is_running")),
        interval=int(bot_state.get("interval") or 30),
        risk_percent=float(bot_state.get("risk_percent") or 1),
    )


def _restore_paper_session():
    state = paper_session_state()
    bot_state["session_date"] = state["session_date"]
    bot_state["filled_sources"] = list(state.get("filled_sources") or [])
    bot_state["orders_placed"] = bool(state.get("orders_placed"))
    bot_state["day_start_equity"] = float(state.get("day_start_equity") or 0)
    bot_state["is_running"] = bool(state.get("is_running"))
    bot_state["interval"] = int(state.get("interval") or bot_state["interval"])
    bot_state["risk_percent"] = float(
        state.get("risk_percent") or bot_state["risk_percent"]
    )
    bot_state["trade_history"] = paper_trade_history()
    _save_paper_session()
    if bot_state["trade_history"]:
        print(
            f"[PAPER] Restored {len(bot_state['trade_history'])} fills and "
            f"slots {bot_state['filled_sources']} for {state['session_date']}."
        )


def reset_session(today):
    bot_state["session_date"] = str(today)
    bot_state["top_sectors"] = []
    bot_state["candidates"] = []
    bot_state["orders_placed"] = False
    bot_state["filled_sources"] = []
    bot_state["day_start_equity"] = 0.0
    bot_state["cash_pnl_today"] = 0.0
    bot_state["mcx_pnl_today"] = 0.0
    bot_state["breakout_alerts"] = []
    bot_state["trade_history"] = (
        paper_trade_history(today)
        if getattr(config, "PAPER_TRADE", False)
        else []
    )
    if not getattr(config, "PAPER_TRADE", False):
        sync_live_order_statuses(cancel_stale=True)
        entries = live_entries_today()
        bot_state["filled_sources"] = list(
            dict.fromkeys(
                str(row.get("symbol") or row.get("source") or "live")
                for row in entries
            )
        )
        bot_state["orders_placed"] = len(entries) >= int(
            getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10)
        )
    _save_paper_session()


def _position_side_for_symbol(display):
    positions = get_open_positions()
    if getattr(config, "PAPER_TRADE", False):
        positions += paper_open_positions()
    for pos in positions:
        name = str(pos.get("symbol") or "").upper()
        if display.upper() in name or name in display.upper():
            return str(pos.get("side") or "").upper(), pos
    return None, None


async def process_candidate(candidate):
    symbol = candidate["symbol"]
    display = candidate.get("display") or (
        symbol["symbol"] if isinstance(symbol, dict) else symbol
    )
    sector = candidate.get("sector", "")
    momentum = candidate.get("momentum", 0)

    market_data = await asyncio.to_thread(fetch_multi_timeframe_data, symbol)
    if not market_data:
        print(f"[SYSTEM] No market data received for {display}.")
        return

    bot_state["equity"] = market_data.get("equity", bot_state["equity"])
    book = candidate.get("kind") or "NSE_EQ"
    extra = (
        f"Bullish sector {sector}: first 5m break, retrace, then 3m close above today's high. "
        f"9:15-9:20 momentum: {momentum:.4%}. "
        f"This name is on the {book} book. Long-only 1:{config.REWARD_RATIO:g} equal-risk setup."
    )
    correlated = {}
    if candidate.get("plan") and candidate.get("source") in ("catalyst", "seasonal"):
        plan = candidate.get("plan") or {}
        source = candidate.get("source")
        logic = candidate.get("setup_note") or (
            f"{source.title()} structure breakout; "
            f"approximately Rs {config.CATALYST_RISK_RS:.0f} risk, "
            f"1:{config.REWARD_RATIO:g} target."
        )
        decision = {
            "signal": "BUY",
            "confidence_score": 80,
            "logic": logic,
            "stop_loss": plan.get("sl"),
            "take_profit": plan.get("tp"),
        }
    else:
        try:
            correlated = await asyncio.to_thread(
                fetch_correlated_asset_prices, display, config.SYMBOLS
            )
            extra += f" Correlated LTPs: {correlated}"
            market_data.setdefault("h1_data", {})["correlated"] = correlated
        except Exception:
            correlated = {}

        decision = await asyncio.to_thread(
            get_ai_decision, market_data, display, extra, book, LIVE_MEMORY
        )
    if not isinstance(decision, dict):
        print(f"[AI] Invalid AI decision for {display}: {decision}")
        return

    signal = str(decision.get("signal", "HOLD")).strip().upper()
    logic = str(decision.get("logic", ""))
    try:
        confidence = float(decision.get("confidence_score", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    bot_state["last_logic"] = logic
    bot_state["last_confidence"] = confidence
    bot_state["last_signal"] = signal
    bot_state["last_stop_loss"] = decision.get("stop_loss")
    bot_state["last_take_profit"] = decision.get("take_profit")

    if signal != "BUY":
        print(f"[AI] {display} | {signal} | Confidence: {confidence}%")
        return

    existing_side, existing_pos = await asyncio.to_thread(_position_side_for_symbol, display)
    if existing_side == "BUY":
        print(f"[DUPLICATE PREVENTED] {display} already long.")
        return
    if existing_side == "SELL" and existing_pos:
        print(f"[REVERSAL] Closing opposite {display} before BUY.")
        try:
            await asyncio.to_thread(close_position, existing_pos)
        except Exception as error:
            print(f"[REVERSAL] Close failed: {error}")
            return

    book_pnl = (
        bot_state.get("mcx_pnl_today", 0.0)
        if book == "MCX"
        else bot_state.get("cash_pnl_today", 0.0)
    )
    result = await asyncio.to_thread(
        execute_trade,
        symbol,
        "BUY",
        None,
        confidence,
        market_data.get("daily_df"),
        bot_state.get("day_start_equity") or bot_state.get("equity"),
        book_pnl,
        decision.get("stop_loss"),
        decision.get("take_profit"),
        candidate.get("chase_risk") or "medium",
        candidate.get("strategy") or "breakout",
        candidate.get("source") or "",
        sector,
    )
    if not isinstance(result, dict):
        result = {"message": str(result), "ok": False}

    bot_state["last_entry_price"] = result.get("price")
    fill = {
        "time": datetime.now(timezone.utc).isoformat(),
        "asset": display,
        "symbol": display,
        "sector": sector,
        "momentum": round(momentum * 100, 3),
        "signal": "BUY",
        "side": "BUY",
        "logic": logic,
        "confidence": confidence,
        "result": result.get("message"),
        "fill": result.get("price"),
        "sl": result.get("sl"),
        "tp": result.get("tp"),
        "volume": result.get("volume"),
        "risk_percent": bot_state.get("risk_percent"),
        "deal": result.get("deal") or result.get("order"),
        "status": (
            "CONFIRMED"
            if result.get("filled")
            else ("SUBMITTED" if result.get("ok") else "BLOCKED")
        ),
    }
    bot_state["trade_history"].append(fill)
    if len(bot_state["trade_history"]) > 100:
        bot_state["trade_history"] = bot_state["trade_history"][-100:]

    if result.get("ok"):
        h1 = market_data.get("h1_data") or {}
        d1 = market_data.get("daily_data") or {}
        append_trade_memory(
            {
                "mode": "LIVE",
                "entry_time": datetime.now(IST).isoformat(),
                "status": "CONFIRMED" if result.get("filled") else "SUBMITTED",
                "order_status": result.get("order_status") or "SUBMITTED",
                "symbol": display,
                "source": candidate.get("source") or "",
                "side": "BUY",
                "signal": "BUY",
                "entry_price": result.get("price"),
                "sl": result.get("sl"),
                "tp": result.get("tp"),
                "virtual_tp": result.get("virtual_tp") or result.get("tp"),
                "emergency_tp": result.get("emergency_tp"),
                "trail_rungs": int(result.get("trail_rungs") or 0),
                "risk_r": result.get("risk_r"),
                "volume": result.get("volume"),
                "order": result.get("order"),
                "ticket": result.get("order"),
                "market_context": {
                    "h1_atr": h1.get("atr14"),
                    "d1_atr": d1.get("atr14"),
                    "h1_rel_volume": h1.get("rel_volume"),
                    "d1_rel_volume": d1.get("rel_volume"),
                    "tick_volume": h1.get("tick_volume"),
                    "correlated": correlated,
                },
            }
        )
    print(f"[AI] {display} | BUY | {sector} | Confidence: {confidence}% | {result.get('message')}")
    return result


def _record_breakout_alerts(rows):
    alerts = bot_state.setdefault("breakout_alerts", [])
    seen = {
        (item.get("symbol"), item.get("trigger"), item.get("prior_time"))
        for item in alerts
    }
    for row in rows or []:
        plan = row.get("plan") or {}
        key = (row.get("symbol"), plan.get("trigger"), plan.get("prior_time"))
        if plan.get("alert_state") != "BREAKOUT" or key in seen:
            continue
        alerts.append(
            {
                "symbol": row.get("symbol"),
                "time": plan.get("breakout_time"),
                "trigger": plan.get("trigger"),
                "entry": plan.get("entry"),
                "sl": plan.get("sl"),
                "tp": plan.get("tp"),
                "prior_time": plan.get("prior_time"),
                "volume_ratio_5m": plan.get("volume_ratio_5m"),
                "volume_spike": plan.get("volume_spike"),
                "oi_change_pct": plan.get("oi_change_pct"),
                "oi_spike": plan.get("oi_spike"),
            }
        )
        seen.add(key)
    bot_state["breakout_alerts"] = alerts[-100:]


def _publish_watchlist_plan(rows):
    """Keep the decision card populated from the best current watchlist plan."""
    planned = [
        row
        for row in (rows or [])
        if all((row.get("plan") or {}).get(key) is not None for key in ("entry", "sl", "tp"))
    ]
    if not planned:
        return
    planned.sort(
        key=lambda row: (
            not bool(row.get("ok")),
            int(row.get("rank") or 99),
        )
    )
    row = planned[0]
    plan = row["plan"]
    symbol = row.get("symbol") or row.get("name") or "instrument"
    bot_state["last_entry_price"] = plan.get("entry")
    bot_state["last_stop_loss"] = plan.get("sl")
    bot_state["last_take_profit"] = plan.get("tp")
    bot_state["last_signal"] = "HOLD"
    bot_state["last_confidence"] = 0
    if bot_state.get("orders_placed"):
        bot_state["last_logic"] = (
            f"Monitoring {symbol}: entry {plan.get('entry')}, SL {plan.get('sl')}, "
            f"target {plan.get('tp')}. Today's manual and seasonal paper slots "
            "are already complete, so no duplicate daily order will be placed."
        )
    else:
        bot_state["last_logic"] = (
            f"{symbol}: {row.get('reason') or 'monitoring breakout'}."
        )


async def breakout_alert_loop():
    """Refresh visual/audio breakout alerts even while execution is stopped."""
    while True:
        now = datetime.now(IST)
        active_hours = time(9, 0) <= now.time() <= time(23, 30)
        if (
            getattr(config, "BREAKOUT_ALERTS_ENABLED", True)
            and bot_state.get("broker_authenticated")
            and (
                not bot_state.get("is_running")
                or bot_state.get("orders_placed")
                or now.time() < SCAN_LOCK
            )
            and now.weekday() < 5
            and active_hours
        ):
            try:
                rows = await asyncio.to_thread(
                    analyze_watchlist, now.date()
                )
                bot_state["watchlist"] = rows
                _record_breakout_alerts(rows)
                _publish_watchlist_plan(rows)
            except Exception as error:
                print(f"[ALERT] Breakout refresh failed: {error}")
        await asyncio.sleep(max(15, int(bot_state.get("interval") or 30)))


async def paper_maintenance_loop():
    """Maintain paper exits and persist due EOD reports even if scanning is stopped."""
    while True:
        if getattr(config, "PAPER_TRADE", False):
            now = datetime.now(IST)
            try:
                if (
                    bot_state.get("broker_authenticated")
                    and now.weekday() < 5
                    and time(9, 15) <= now.time() <= time(15, 35)
                ):
                    await asyncio.to_thread(refresh_paper_positions)
                reports = await asyncio.to_thread(write_due_eod_reports, now)
                for report in reports:
                    print(
                        f"[PAPER] EOD report saved for {report['date']}: "
                        f"{report['trade_count']} trades, "
                        f"P&L Rs {report['realized_pnl']:.2f}."
                    )
                bot_state["trade_history"] = await asyncio.to_thread(
                    paper_trade_history
                )
            except Exception as error:
                print(f"[PAPER] Maintenance failed: {error}")
        await asyncio.sleep(max(15, int(bot_state.get("interval") or 30)))


async def trading_loop():
    while True:
        if not bot_state["is_running"]:
            await asyncio.sleep(1)
            continue

        now = datetime.now(IST)
        today = now.date()
        if str(today) != bot_state["session_date"]:
            reset_session(today)

        if now.weekday() >= 5:
            await asyncio.sleep(60)
            continue

        try:
            if not getattr(config, "PAPER_TRADE", False):
                await asyncio.to_thread(sync_live_order_statuses, True)
                filled_entries = await asyncio.to_thread(live_entries_today)
                bot_state["filled_sources"] = list(
                    dict.fromkeys(
                        str(row.get("symbol") or row.get("source") or "live")
                        for row in filled_entries
                    )
                )
                bot_state["pending_orders"] = await asyncio.to_thread(
                    live_pending_orders_today
                )
            await asyncio.to_thread(reconcile_closed_trades)
            try:
                positions = await asyncio.to_thread(get_open_positions)
                bot_state["open_positions"] = positions
            except Exception as pos_error:
                print(f"[SYSTEM] Open-position fetch failed: {pos_error}")
                raise
            try:
                trailed = await asyncio.to_thread(manage_rung_trails, positions)
                if trailed:
                    print(f"[TRAIL] Advanced {trailed} rung(s).")
            except Exception as trail_error:
                print(f"[TRAIL] Rung manager skipped: {trail_error}")
            cash_pnl, mcx_pnl = await asyncio.to_thread(realized_pnl_today)
            if getattr(config, "PAPER_TRADE", False):
                await asyncio.to_thread(refresh_paper_positions)
                cash_pnl += await asyncio.to_thread(paper_realized_pnl_today)
            bot_state["cash_pnl_today"] = cash_pnl
            bot_state["mcx_pnl_today"] = mcx_pnl
            session_pnl = cash_pnl + mcx_pnl
            positions = bot_state.get("open_positions") or []
            floating = 0.0
            for pos in positions:
                try:
                    floating += float(pos.get("pnl") or 0)
                except (TypeError, ValueError):
                    pass
            combined_pnl = session_pnl + floating
            cap_reason = session_square_off_reason(combined_pnl)
            if cap_reason:
                limit = (
                    config.MAX_DAY_LOSS_RS
                    if cap_reason == "loss"
                    else config.MAX_DAY_PROFIT_RS
                )
                print(
                    f"[SYSTEM] Kill switch: session {cap_reason} "
                    f"Rs {combined_pnl:.2f} hits Rs {limit:.0f} "
                    f"(realized {session_pnl:.2f} + floating {floating:.2f}). Flattening all."
                )
                for pos in positions:
                    try:
                        await asyncio.to_thread(close_position, pos)
                    except Exception as error:
                        print(f"[SYSTEM] Flatten failed for {pos.get('ticket')}: {error}")
                bot_state["is_running"] = False
                bot_state["orders_placed"] = True
                _save_paper_session()
                continue
        except Exception as error:
            # Fail closed: do not submit a new order if realized loss cannot be verified.
            bot_state["is_running"] = False
            _save_paper_session()
            print(f"[SYSTEM] Realized-P&L verification failed; engine halted: {error}")
            continue

        if bot_state["orders_placed"]:
            if getattr(config, "PAPER_TRADE", False) and not config.TRADING_ENABLED:
                await asyncio.sleep(bot_state["interval"])
                continue
            try:
                n = await asyncio.to_thread(reconcile_closed_trades)
                if n:
                    audit = await asyncio.to_thread(run_audit, True)
                    doc = (audit or {}).get("document") or load_rules_document()
                    bot_state["learned_rules"] = doc.get("rules") or []
                    bot_state["last_audit_at"] = doc.get("last_audit_at")
            except Exception as error:
                print(f"[LEARNING] Post-scan reconcile failed: {error}")
            try:
                positions = await asyncio.to_thread(get_open_positions)
                floating = 0.0
                for pos in positions:
                    try:
                        floating += float(pos.get("pnl") or 0)
                    except (TypeError, ValueError):
                        pass
                realized = float(bot_state.get("cash_pnl_today") or 0) + float(
                    bot_state.get("mcx_pnl_today") or 0
                )
                if session_square_off_reason(realized + floating):
                    cap_reason = session_square_off_reason(realized + floating)
                    limit = (
                        config.MAX_DAY_LOSS_RS
                        if cap_reason == "loss"
                        else config.MAX_DAY_PROFIT_RS
                    )
                    print(
                        f"[SYSTEM] Session {cap_reason} cap Rs {limit:.0f} "
                        f"hit (realized {realized:.2f} + floating {floating:.2f}). Flattening."
                    )
                    for pos in positions:
                        try:
                            await asyncio.to_thread(close_position, pos)
                        except Exception as error:
                            print(f"[SYSTEM] Flatten failed for {pos.get('ticket')}: {error}")
            except Exception as error:
                print(f"[SYSTEM] Session loss check failed: {error}")
            await asyncio.sleep(bot_state["interval"])
            continue

        if now.time() < SCAN_LOCK:
            print(
                f"[SYSTEM] Orders start after {SCAN_LOCK.strftime('%H:%M')} IST "
                f"(first 5m + retrace). Now {now.strftime('%H:%M:%S')} IST."
            )
            await asyncio.sleep(15)
            continue

        if bot_state.get("filled_sources"):
            try:
                positions = await asyncio.to_thread(get_open_positions)
                floating = sum(float(pos.get("pnl") or 0) for pos in positions)
                realized = float(bot_state.get("cash_pnl_today") or 0) + float(
                    bot_state.get("mcx_pnl_today") or 0
                )
                if session_square_off_reason(realized + floating):
                    cap_reason = session_square_off_reason(realized + floating)
                    limit = (
                        config.MAX_DAY_LOSS_RS
                        if cap_reason == "loss"
                        else config.MAX_DAY_PROFIT_RS
                    )
                    print(
                        f"[SYSTEM] Shared Rs {limit:.0f} {cap_reason} cap hit. "
                        "Flattening all positions."
                    )
                    for pos in positions:
                        try:
                            await asyncio.to_thread(close_position, pos)
                        except Exception as error:
                            print(f"[SYSTEM] Flatten failed for {pos.get('ticket')}: {error}")
                    bot_state["orders_placed"] = True
                    continue
            except Exception as error:
                print(f"[SYSTEM] Combined-book loss check failed: {error}")

        if now.time() >= SCAN_CUTOFF:
            bot_state["orders_placed"] = True
            _save_paper_session()
            bot_state["last_logic"] = (
                f"Combined scan closed at {SCAN_CUTOFF.strftime('%H:%M')} IST."
            )
            print(f"[SYSTEM] Combined scan cutoff {SCAN_CUTOFF.strftime('%H:%M')} IST reached.")
            continue

        try:
            mode = "PAPER" if getattr(config, "PAPER_TRADE", False) else "SCAN"
            print(
                f"[SYSTEM] {mode}: 1 stock/bullish sector, first-5m break + retrace, "
                f"then 3m close above today's high after {SCAN_LOCK.strftime('%H:%M')} IST, "
                f"1:{config.REWARD_RATIO:g}, Rs {config.CATALYST_RISK_RS:.0f} per slot..."
            )
            scan = await asyncio.to_thread(morning_universe, today)
            bot_state["watchlist"] = scan.get("watchlist") or []
            _record_breakout_alerts(bot_state["watchlist"])
            _publish_watchlist_plan(bot_state["watchlist"])
            bot_state["top_sectors"] = [
                {"sector": row["sector"], "momentum": round(row["momentum"] * 100, 3)}
                for row in scan["sectors"]
            ]
            bot_state["candidates"] = [
                {
                    "symbol": item.get("display")
                    or (
                        item["symbol"]["symbol"]
                        if isinstance(item["symbol"], dict)
                        else item["symbol"]
                    ),
                    "sector": item["sector"],
                    "momentum": round(item["momentum"] * 100, 3),
                    "kind": item.get("kind", "NSE_EQ"),
                    "source": item.get("source", ""),
                    "strategy": item.get("strategy") or "breakout",
                }
                for item in scan["candidates"]
            ]
            if not bot_state.get("day_start_equity"):
                try:
                    bot_state["day_start_equity"] = get_equity()
                except Exception:
                    bot_state["day_start_equity"] = bot_state.get("equity") or 0.0
                _save_paper_session()

            try:
                bot_state["open_positions"] = await asyncio.to_thread(get_open_positions)
            except Exception:
                bot_state["open_positions"] = bot_state.get("open_positions") or []
            filled_symbols = {
                str(item).upper() for item in (bot_state.get("filled_sources") or [])
            }
            pending_symbols = {
                str(row.get("symbol") or "").upper()
                for row in (bot_state.get("pending_orders") or [])
                if row.get("symbol")
            }
            position_symbols = {
                str(pos.get("symbol") or "").upper()
                for pos in (bot_state.get("open_positions") or [])
                if pos.get("symbol")
            }
            occupied_symbols = filled_symbols | pending_symbols | position_symbols
            occupied_sectors = {
                str(sector_for_symbol(name) or "").upper()
                for name in occupied_symbols
                if sector_for_symbol(name)
            }
            bullish_sectors = {
                str(row.get("sector") or "").upper()
                for row in (scan.get("sectors") or [])
                if (row.get("momentum") or 0) > 0
            }
            remaining_slots = max(
                0,
                int(getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10))
                - len(occupied_symbols),
            )
            if remaining_slots == 0:
                bot_state["orders_placed"] = len(filled_symbols) >= int(
                    getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10)
                )
                _save_paper_session()
                print(
                    f"[SYSTEM] {len(occupied_symbols)} equal-risk slots already "
                    f"filled or pending."
                )
                await asyncio.sleep(bot_state["interval"])
                continue
            planned = []
            for candidate in scan["candidates"]:
                if not bot_state["is_running"]:
                    break
                display_name = str(
                    candidate.get("display")
                    or (
                        candidate["symbol"]["symbol"]
                        if isinstance(candidate.get("symbol"), dict)
                        else candidate.get("symbol")
                    )
                    or ""
                ).upper()
                if display_name in occupied_symbols or (
                    getattr(config, "BLOCK_REPEAT_SYMBOL_TODAY", True)
                    and symbol_already_used_today(display_name)
                ):
                    print(
                        f"[SYSTEM] Skip {display_name}: already traded today "
                        "(stop, target, or still open)."
                    )
                    continue
                candidate_sector = str(
                    candidate.get("sector")
                    or sector_for_symbol(display_name)
                    or ""
                ).upper()
                if (
                    getattr(config, "ONE_STOCK_PER_SECTOR", True)
                    and candidate_sector
                    and candidate_sector in occupied_sectors
                ):
                    print(
                        f"[SYSTEM] Skip {display_name}: sector {candidate_sector} "
                        "already has a live slot."
                    )
                    continue
                if (
                    getattr(config, "REQUIRE_BULLISH_SECTOR", True)
                    and candidate_sector.startswith("NIFTY")
                    and bullish_sectors
                    and candidate_sector not in bullish_sectors
                ):
                    print(
                        f"[SYSTEM] Skip {display_name}: sector {candidate_sector} "
                        "is not bullish."
                    )
                    continue
                try:
                    plan, plan_reason, _, _ = await asyncio.to_thread(
                        evaluate_entry_plan,
                        candidate.get("symbol") or candidate,
                        "BUY",
                        candidate.get("chase_risk") or "medium",
                        candidate.get("strategy") or "breakout",
                    )
                    if not plan:
                        print(
                            f"[SYSTEM] Skip {candidate.get('display') or candidate}: {plan_reason}"
                        )
                        continue
                    row = dict(candidate)
                    row["plan"] = plan
                    planned.append(row)
                except Exception as error:
                    print(f"[SYSTEM] Plan failed for {candidate}: {error}")
            selected = pick_best_candidates(
                planned,
                limit=remaining_slots,
                occupied_sectors=occupied_sectors,
            )
            if selected:
                print(
                    "[SYSTEM] Best setups: "
                    + ", ".join(
                        f"{row.get('display') or row.get('symbol')} "
                        f"score {row['plan']['score']:.2f}"
                        for row in selected
                    )
                )

            for candidate in selected:
                if not bot_state["is_running"]:
                    break
                try:
                    session_pnl = float(bot_state.get("cash_pnl_today") or 0) + float(
                        bot_state.get("mcx_pnl_today") or 0
                    )
                    cap_reason = session_square_off_reason(session_pnl)
                    if cap_reason:
                        limit = (
                            config.MAX_DAY_LOSS_RS
                            if cap_reason == "loss"
                            else config.MAX_DAY_PROFIT_RS
                        )
                        print(
                            f"[SYSTEM] Session {cap_reason} cap Rs {limit:.0f} "
                            "hit. No more trades today."
                        )
                        break
                    book = candidate.get("kind") or "NSE_EQ"
                    book_pnl = (
                        bot_state.get("mcx_pnl_today", 0.0)
                        if book == "MCX"
                        else bot_state.get("cash_pnl_today", 0.0)
                    )
                    start = bot_state.get("day_start_equity") or bot_state.get("equity") or 0.0
                    if book_loss_hit(start, book_pnl, book):
                        print(f"[SYSTEM] {book} daily loss cap reached. Other book can still take entries.")
                        continue
                    result = await process_candidate(candidate)
                    if (
                        isinstance(result, dict)
                        and result.get("ok")
                        and result.get("filled")
                    ):
                        filled_name = str(
                            candidate.get("display")
                            or result.get("symbol")
                            or candidate.get("source")
                            or "live"
                        )
                        if filled_name not in bot_state["filled_sources"]:
                            bot_state["filled_sources"].append(filled_name)
                            _save_paper_session()
                    await asyncio.sleep(0.8)
                except Exception as error:
                    print(f"[SYSTEM] Error processing {candidate}: {error}")

            try:
                n = await asyncio.to_thread(reconcile_closed_trades)
                if n:
                    await asyncio.to_thread(run_audit, True)
            except Exception as error:
                print(f"[AUDITOR] Scan audit failed: {error}")

            filled_count = len(bot_state.get("filled_sources") or [])
            daily_limit = int(getattr(config, "LIVE_MAX_ENTRIES_PER_DAY", 10))
            bot_state["orders_placed"] = filled_count >= daily_limit
            _save_paper_session()
            if bot_state["orders_placed"]:
                print(
                    f"[SYSTEM] {daily_limit} equal-risk slots filled. "
                    "Super-order SL/TP manage the rest of the day."
                )
            else:
                bot_state["last_logic"] = (
                    f"Filled {filled_count}/{daily_limit} equal-risk slots "
                    f"(Rs {getattr(config, 'LIVE_TRADE_RISK_RS', config.CATALYST_RISK_RS):.0f} "
                    f"stop, 1:{config.REWARD_RATIO:g} target). "
                    f"Rescan every {bot_state['interval']}s until "
                    f"{SCAN_CUTOFF.strftime('%H:%M')} IST."
                )
                bot_state["last_confidence"] = 0
                bot_state["last_signal"] = "HOLD"
                print(
                    f"[SYSTEM] {filled_count}/{daily_limit} slots filled; rescanning."
                )
        except Exception as error:
            print(f"[SYSTEM] Morning scan failed: {error}")
            await asyncio.sleep(30)
            continue

        await asyncio.sleep(bot_state["interval"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
