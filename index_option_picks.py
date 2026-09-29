"""Index option strikes from today's side: OI, volume, greeks, VIX. Read-only picks."""
from __future__ import annotations

from datetime import datetime
from threading import Lock
import time
from zoneinfo import ZoneInfo

import pandas as pd

from broker import load_security_master, option_chain, option_expiry_list
from credentials import dhan_ready
from preopen import build_briefing

IST = ZoneInfo("Asia/Kolkata")
CACHE_SECONDS = 90
_LOCK = Lock()
_CACHE = {"at": 0.0, "payload": None}

INDEXES = (
    {"symbol": "NIFTY", "scrip": 13, "segments": ("IDX_I",), "exchange": "NSE"},
    {"symbol": "BANKNIFTY", "scrip": 25, "segments": ("IDX_I",), "exchange": "NSE"},
    {"symbol": "SENSEX", "scrip": 51, "segments": ("IDX_I", "BSE_FNO"), "exchange": "BSE"},
)


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _contract(symbol, expiry, strike, option_type):
    stamp = datetime.strptime(str(expiry)[:10], "%Y-%m-%d")
    strike_i = int(round(float(strike)))
    return f"{symbol} {stamp.day:02d} {stamp.strftime('%b').upper()} {strike_i} {option_type}"


def _nearest_expiry(dates):
    today = datetime.now(IST).date().isoformat()
    live = sorted(str(item)[:10] for item in (dates or []) if str(item)[:10] >= today)
    return live[0] if live else None


def _leg(raw, side):
    if not isinstance(raw, dict):
        return None
    greeks = raw.get("greeks") or {}
    oi = _num(raw.get("oi"))
    prev_oi = _num(raw.get("previous_oi"))
    last = _num(raw.get("last_price"))
    bid = _num(raw.get("top_bid_price"))
    ask = _num(raw.get("top_ask_price"))
    mid = (bid + ask) / 2.0 if bid and ask else last
    spread = ((ask - bid) / mid * 100.0) if mid else 99.0
    oi_chg = ((oi - prev_oi) / prev_oi * 100.0) if prev_oi else 0.0
    return {
        "side": side,
        "ltp": round(last, 2) if last else None,
        "bid": round(bid, 2) if bid else None,
        "ask": round(ask, 2) if ask else None,
        "spread_pct": round(spread, 2),
        "iv": round(_num(raw.get("implied_volatility")), 2),
        "delta": round(_num(greeks.get("delta")), 4),
        "gamma": round(_num(greeks.get("gamma")), 5),
        "theta": round(_num(greeks.get("theta")), 3),
        "vega": round(_num(greeks.get("vega")), 3),
        "oi": int(oi),
        "previous_oi": int(prev_oi),
        "oi_change_pct": round(oi_chg, 1),
        "volume": int(_num(raw.get("volume"))),
        "security_id": str(
            raw.get("security_id")
            or raw.get("securityId")
            or raw.get("SecurityId")
            or raw.get("sid")
            or ""
        ),
    }


def _score(leg, role, vix_volatile):
    delta = abs(_num(leg.get("delta")))
    target = 0.18 if role == "hedge" else (0.34 if vix_volatile else 0.42)
    delta_fit = max(0.0, 1.0 - abs(delta - target) / 0.28)
    oi_chg = _num(leg.get("oi_change_pct"))
    build = max(0.0, min(1.0, (oi_chg + 8.0) / 30.0))
    volume = _num(leg.get("volume"))
    vol_fit = max(0.0, min(1.0, volume / 800000.0))
    oi_fit = max(0.0, min(1.0, _num(leg.get("oi")) / 2500000.0))
    spread_pen = 1.0 if _num(leg.get("spread_pct")) <= 6 else 0.45
    vega = _num(leg.get("vega"))
    vega_pen = 0.75 if vix_volatile and vega > 15 else 1.0
    return round(
        (0.32 * delta_fit + 0.26 * oi_fit + 0.22 * vol_fit + 0.20 * build)
        * spread_pen
        * vega_pen
        * 100.0,
        1,
    )


def _passes(leg, role, vix_volatile, loose=False):
    if not leg or not leg.get("ltp"):
        return False
    if leg["oi"] <= 0:
        return False
    if not loose and leg["volume"] <= 0:
        return False
    if not loose and leg["spread_pct"] > 10:
        return False
    if loose and leg["spread_pct"] > 18:
        return False
    delta = abs(leg["delta"])
    if role == "directional":
        low, high = (0.28, 0.52) if vix_volatile else (0.30, 0.58)
        return low <= delta <= high
    if loose:
        return 0.08 <= delta <= 0.36
    return 0.10 <= delta <= 0.28


def _strike_windows(strikes, atm_index, spot, buy_type):
    if len(strikes) >= 2:
        step = min(strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1))
    else:
        step = 50.0
    step = max(float(step or 50), 1.0)
    dir_n = max(4, int(round(spot * 0.012 / step)))
    hedge_skip = max(2, int(round(spot * 0.012 / step)))
    hedge_n = max(8, int(round(spot * 0.045 / step)))
    if buy_type == "PE":
        window = strikes[max(0, atm_index - dir_n) : atm_index + 2]
        start = min(len(strikes), atm_index + hedge_skip)
        hedge_window = strikes[start : start + hedge_n]
    else:
        window = strikes[atm_index : min(len(strikes), atm_index + dir_n + 1)]
        end = max(0, atm_index - hedge_skip)
        hedge_window = strikes[max(0, end - hedge_n) : end]
    return window, hedge_window


def _master_expiries(symbol):
    master = load_security_master()
    trading = master["SEM_TRADING_SYMBOL"].astype(str).str.upper()
    inst = master["SEM_INSTRUMENT_NAME"].astype(str).str.upper()
    rows = master[(inst == "OPTIDX") & trading.str.startswith(str(symbol).upper() + "-")]
    if rows.empty:
        return []
    stamps = pd.to_datetime(rows["SEM_EXPIRY_DATE"], errors="coerce")
    today = datetime.now(IST).date()
    return sorted(
        {
            value.date().isoformat()
            for value in stamps.dropna()
            if hasattr(value, "date") and value.date() >= today
        }
    )


def _normalize_expiries(raw):
    if isinstance(raw, dict):
        raw = (
            raw.get("data")
            or raw.get("expiry")
            or raw.get("expiryList")
            or raw.get("expiry_list")
            or []
        )
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("expiry") or item.get("date") or item.get("Expiry")
        text = str(item or "").strip()
        if len(text) >= 10 and text[4] == "-":
            text = text[:10]
        if text:
            out.append(text)
    return out


def _extract_chain(payload):
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            return {}, 0.0
        oc = current.get("oc")
        if isinstance(oc, dict) and oc:
            return oc, _num(current.get("last_price"))
        inner = current.get("data")
        if inner is None or inner is current:
            break
        current = inner
    return {}, 0.0


def _fetch_index(meta):
    segments = list(meta.get("segments") or ("IDX_I",))
    last_error = None
    expiries = []
    segment = segments[0]
    for candidate in segments:
        try:
            expiries = _normalize_expiries(option_expiry_list(meta["scrip"], candidate))
            if expiries:
                segment = candidate
                break
        except Exception as error:
            last_error = error
    if not expiries:
        expiries = _master_expiries(meta["symbol"])
        if not expiries:
            hint = str(last_error or "no expiry")
            if "None" in hint or "empty error" in hint.lower():
                hint = "Dhan Sensex chain is not on this token; Nifty/Bank still run"
            return {"symbol": meta["symbol"], "error": hint}
    live = [_nearest_expiry(expiries)]
    live.extend(item for item in expiries if item not in live)
    live = [item for item in live if item][:3]
    if not live:
        return {"symbol": meta["symbol"], "error": "No live expiry"}
    time.sleep(3.1)
    chain_error = None
    chain = {}
    expiry = live[0]
    used_segment = segment
    first = True
    for expiry in live:
        for candidate in (segment, *[item for item in segments if item != segment]):
            if not first:
                time.sleep(3.1)
            first = False
            try:
                payload = option_chain(meta["scrip"], expiry, candidate) or {}
                oc, spot = _extract_chain(payload)
                if oc:
                    chain = {"oc": oc, "last_price": spot or _num(payload.get("last_price"))}
                    used_segment = candidate
                    chain_error = None
                    break
                chain_error = "Empty option chain"
            except Exception as error:
                chain_error = error
        if chain.get("oc"):
            break
    if chain_error or not chain.get("oc"):
        keys = sorted((chain or {}).keys())[:8] if isinstance(chain, dict) else type(chain).__name__
        return {
            "symbol": meta["symbol"],
            "error": f"{chain_error or 'Empty option chain'} ({expiry} {used_segment})",
            "debug_keys": keys,
        }
    spot = _num(chain.get("last_price"))
    if not spot:
        try:
            spot = float(min(chain["oc"], key=lambda value: abs(float(value))))
        except (TypeError, ValueError):
            spot = 0.0
    return {
        "symbol": meta["symbol"],
        "exchange": meta["exchange"],
        "expiry": expiry,
        "spot": spot,
        "oc": chain.get("oc") or {},
        "segment": used_segment,
    }


def _direction(side):
    if side in {"short", "fade"}:
        return "PE", "CE", "Buy PE (short/fade)", "Hedge CE"
    if side == "long":
        return "CE", "PE", "Buy CE (long)", "Hedge PE"
    return None, None, None, None


def _pick_from_chain(index, side, vix_volatile):
    buy_type, hedge_type, buy_label, hedge_label = _direction(side)
    if not buy_type:
        return []
    oc = index.get("oc") or {}
    spot = index.get("spot") or 0
    expiry = index.get("expiry")
    symbol = index["symbol"]
    exchange = index["exchange"]
    cells = {}
    for raw_strike, cell in oc.items():
        try:
            cells[float(raw_strike)] = cell or {}
        except (TypeError, ValueError):
            continue
    strikes = sorted(cells)
    if not strikes or not spot:
        return []
    atm = min(strikes, key=lambda value: abs(value - spot))
    atm_index = strikes.index(atm)
    window, hedge_window = _strike_windows(strikes, atm_index, spot, buy_type)

    def rows_for(strike_list, option_type, role, label, loose=False):
        out = []
        for strike in strike_list:
            cell = cells.get(strike) or {}
            raw_leg = (
                cell.get(option_type.lower())
                or cell.get(option_type)
                or cell.get(option_type.upper())
                or {}
            )
            leg = _leg(raw_leg, option_type)
            if leg and not leg.get("security_id"):
                leg["security_id"] = str(
                    cell.get("security_id")
                    or cell.get("securityId")
                    or raw_leg.get("security_id")
                    or ""
                )
            if not _passes(leg, role, vix_volatile, loose=loose):
                continue
            score = _score(leg, role, vix_volatile)
            contract = _contract(symbol, expiry, strike, option_type)
            out.append(
                {
                    "id": f"{symbol}-{int(strike)}-{option_type}",
                    "underlying": symbol,
                    "exchange": exchange,
                    "instrument": "OPTIDX",
                    "expiry": expiry,
                    "strike": int(round(strike)),
                    "option_type": option_type,
                    "role": role,
                    "role_label": label,
                    "contract": contract,
                    "spot": round(spot, 2),
                    "atm": int(round(atm)),
                    "score": score,
                    "selected": False,
                    "strategy": "breakout",
                    **leg,
                    "why": (
                        f"{'OTM' if (option_type == 'PE' and strike < atm) or (option_type == 'CE' and strike > atm) else 'ATM'} "
                        f"Δ{abs(leg['delta']):.2f} · OI {leg['oi_change_pct']:+.1f}% · vol {leg['volume']:,}"
                    ),
                }
            )
        out.sort(key=lambda row: row["score"], reverse=True)
        return out

    directional = rows_for(window, buy_type, "directional", buy_label)[:3]
    hedges = rows_for(hedge_window, hedge_type, "hedge", hedge_label)[:2]
    if directional and not hedges:
        hedges = rows_for(hedge_window, hedge_type, "hedge", hedge_label, loose=True)[:1]
    if directional:
        directional[0]["selected"] = True
    if hedges:
        hedges[0]["selected"] = True
        pair = directional[0]["contract"] if directional else ""
        for hedge in hedges:
            hedge["hedge_for"] = pair
    return directional + hedges


def build_picks(force=False):
    with _LOCK:
        now = time.monotonic()
        if (
            not force
            and _CACHE["payload"] is not None
            and now - _CACHE["at"] < CACHE_SECONDS
        ):
            return _CACHE["payload"]

        briefing = build_briefing()
        daily = briefing.get("daily") or {}
        side = daily.get("side") or "flat"
        vix = ((briefing.get("quotes") or {}).get("india_vix") or {}).get("last")
        vix_volatile = bool(briefing.get("vix_volatile"))
        payload = {
            "ok": True,
            "generated_at": datetime.now(IST).isoformat(),
            "side": side,
            "title": daily.get("title") or "No daily side",
            "vix": vix,
            "vix_volatile": vix_volatile,
            "rule": (
                "Only liquid ATM/near-OTM strikes. Buy the signal side, hedge with the "
                "other side. Default strategy is breakout. Engine stays off until you add "
                "and start it. ₹500 risk still applies — index lots can exceed that."
            ),
            "rows": [],
            "errors": [],
        }
        if side == "flat":
            payload["ok"] = True
            payload["rows"] = []
            payload["note"] = "Flat day — no CE/PE buy list. Stay in cash or skip."
            _CACHE["at"] = time.monotonic()
            _CACHE["payload"] = payload
            return payload

        if not dhan_ready():
            missing = (
                "Dhan credentials are missing or expired. Open Settings, "
                "paste Client ID and Access Token, and save."
            )
            payload["ok"] = False
            payload["error"] = missing
            payload["errors"] = [f"{meta['symbol']}: {missing}" for meta in INDEXES]
            return payload

        results = []
        errors = []
        for meta in INDEXES:
            try:
                if results or errors:
                    time.sleep(3.1)
                results.append(_fetch_index(meta))
            except Exception as inner:
                errors.append(f"{meta['symbol']}: {inner}")

        rows = []
        for index in results:
            if index.get("error"):
                errors.append(f"{index.get('symbol')}: {index['error']}")
                continue
            try:
                rows.extend(_pick_from_chain(index, side, vix_volatile))
            except Exception as error:
                errors.append(f"{index.get('symbol')}: {error}")
        payload["rows"] = rows
        payload["errors"] = errors
        payload["count"] = len(rows)
        cred_fail = errors and all("credentials" in str(item).lower() for item in errors)
        empty_fail = (not rows) and errors
        if not cred_fail and not empty_fail:
            _CACHE["at"] = time.monotonic()
            _CACHE["payload"] = payload
        return payload
