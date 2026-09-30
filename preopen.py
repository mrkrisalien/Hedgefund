"""09:00–09:15 IST pre-open briefing: US, VIX, NSE breadth, GIFT/SGX Nifty."""
from __future__ import annotations

import json
import os
from datetime import datetime, time as clock_time
from threading import Lock
import re
from urllib.error import URLError
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
CACHE_SECONDS = 60
PATH = os.path.join(os.path.dirname(__file__), "data", "preopen.json")
VIX_STABLE = 15.0
_LOCK = Lock()
_CACHE = {"at": 0.0, "payload": None}

YAHOO_SYMBOLS = {
    "dow": "^DJI",
    "nasdaq": "^IXIC",
    "dow_fut": "YM=F",
    "nasdaq_fut": "NQ=F",
    "india_vix": "^INDIAVIX",
    "nifty": "^NSEI",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _now():
    return datetime.now(IST)


def _get_json(url, timeout=8):
    request = Request(url, headers=HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _yahoo_chart(symbol):
    encoded = urlquote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=5d"
    payload = _get_json(url)
    result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = meta.get("regularMarketChangePercent")
    if change is None and last not in (None, 0) and prev not in (None, 0):
        try:
            change = (float(last) - float(prev)) / float(prev) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            change = None
    return {
        "symbol": meta.get("symbol") or symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "last": last,
        "previous": prev,
        "change_pct": round(float(change), 2) if change is not None else None,
        "ok": last is not None,
    }


def _yahoo_quotes(symbols):
    out = {}
    for symbol in symbols:
        try:
            out[symbol] = _yahoo_chart(symbol)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, KeyError):
            continue
    return out


def _quote(quotes, *keys):
    for key in keys:
        yahoo = YAHOO_SYMBOLS.get(key, key)
        if yahoo in quotes:
            return quotes[yahoo]
    return {"ok": False, "last": None, "change_pct": None, "error": "unavailable"}


def _tone(change_pct):
    if change_pct is None:
        return "wait"
    return "green" if float(change_pct) >= 0 else "red"


def _mc_nifty():
    """Nifty 50 last + advance/decline from Moneycontrol (works when NSE blocks bots)."""
    try:
        payload = _get_json(
            "https://priceapi.moneycontrol.com/pricefeed/notapplicable/inidicesindia/in%3BNSX"
        )
        data = payload.get("data") or {}
        last = data.get("pricecurrent")
        change = data.get("pricepercentchange")
        advances = data.get("adv")
        declines = data.get("decl")
        quote = {
            "ok": last is not None,
            "symbol": "NIFTY50",
            "name": data.get("company") or "Nifty 50",
            "last": float(last) if last is not None else None,
            "previous": data.get("priceprevclose"),
            "change_pct": round(float(change), 2) if change is not None else None,
        }
        breadth = _parse_breadth(advances, declines, data.get("unchg"))
        if not breadth:
            breadth = {"ok": False, "error": "Moneycontrol did not publish advances"}
        return quote, breadth
    except Exception as error:
        return {"ok": False, "error": str(error)}, {"ok": False, "error": str(error)}


def _gift_groww():
    """GIFT/SGX Nifty from Groww's public page (formerly Singapore Nifty)."""
    try:
        request = Request(
            "https://groww.in/indices/global-indices/sgx-nifty",
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        )
        with urlopen(request, timeout=12) as response:
            html = response.read().decode("utf-8", "replace")
        match = re.search(r'"priceData":(\{[^}]+\})', html)
        if not match:
            return {"ok": False, "error": "GIFT block missing on Groww"}
        data = json.loads(match.group(1))
        last = data.get("value")
        change = data.get("dayChangePerc")
        return {
            "ok": last is not None,
            "symbol": "GIFTNIFTY",
            "name": "GIFT Nifty",
            "last": float(last) if last is not None else None,
            "previous": data.get("close"),
            "change_pct": round(float(change), 2) if change is not None else None,
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}


def _nse_headers():
    home = Request("https://www.nseindia.com", headers=HEADERS)
    with urlopen(home, timeout=6) as response:
        cookie = response.headers.get("Set-Cookie") or ""
    return {**HEADERS, "Cookie": cookie, "Referer": "https://www.nseindia.com"}


def _nse_json(path, headers):
    api = Request(f"https://www.nseindia.com{path}", headers=headers)
    with urlopen(api, timeout=8) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _parse_breadth(advances, declines, unchanged=None):
    if advances is None:
        return None
    advances = int(advances)
    declines = int(declines or 0)
    return {
        "ok": True,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "bias": "bullish" if advances > declines else ("bearish" if declines > advances else "flat"),
    }


def _nse_bundle():
    """Best-effort GIFT Nifty + Nifty 50 advances/declines. NSE often blocks bots."""
    breadth = {"ok": False, "error": "NSE breadth unavailable"}
    gift = {"ok": False, "error": "GIFT Nifty unavailable"}
    try:
        headers = _nse_headers()
    except Exception as error:
        return {"ok": False, "error": str(error)}, breadth, gift
    try:
        status = _nse_json("/api/marketStatus", headers)
        raw = status.get("giftnifty") or {}
        last = raw.get("LASTPRICE") or raw.get("lastPrice")
        change = raw.get("PERCHANGE") or raw.get("percentChange")
        day_change = raw.get("DAYCHANGE") or raw.get("change")
        if change is None and last not in (None, 0) and day_change not in (None, 0):
            try:
                prev = float(last) - float(day_change)
                change = (float(day_change) / prev) * 100.0 if prev else None
            except (TypeError, ValueError, ZeroDivisionError):
                change = None
        if last is not None:
            gift = {
                "ok": True,
                "symbol": "GIFTNIFTY",
                "name": "GIFT Nifty",
                "last": float(last),
                "previous": None,
                "change_pct": round(float(change), 2) if change is not None else None,
            }
    except Exception as error:
        gift = {"ok": False, "error": str(error)}
    try:
        payload = _nse_json("/api/equity-stockIndices?index=NIFTY%2050", headers)
        advance = payload.get("advance") or {}
        parsed = _parse_breadth(
            advance.get("advances") or advance.get("advance"),
            advance.get("declines") or advance.get("decline"),
            advance.get("unchanged"),
        )
        if parsed:
            breadth = parsed
        else:
            rows = payload.get("data") or []
            up = sum(1 for row in rows if float(row.get("pChange") or 0) > 0)
            down = sum(1 for row in rows if float(row.get("pChange") or 0) < 0)
            if rows:
                breadth = _parse_breadth(up, down, len(rows) - up - down)
            else:
                breadth = {"ok": False, "error": "NSE did not publish advances"}
    except Exception as error:
        breadth = {"ok": False, "error": str(error)}
    return {"ok": True}, breadth, gift


def _check(name, status, detail, score=0):
    return {"name": name, "status": status, "detail": detail, "score": score}


# Frozen 2y Yahoo study (2024-10-01 to 2026-09-28). Not recomputed at 09:00.
STUDY_WINDOW = "2024-10-01 to 2026-09-28"
DAILY_BUCKETS = {
    "us_red_vix_stable": {
        "id": "us_red_vix_stable",
        "side": "short",
        "title": "Short bias today",
        "label": "US red, VIX under 15",
        "days": 120,
        "short_win_pct": 57.5,
        "long_win_pct": 42.5,
        "return_pct": 6.5,
        "max_dd_pct": -4.11,
        "gap_follow_pct": 70.8,
        "body": (
            "No cash longs and no CE. If you take risk, Nifty short after a bearish "
            "first 5m and the 09:25 confirm. Do not start the stock 5-point engine."
        ),
    },
    "us_red_vix_volatile": {
        "id": "us_red_vix_volatile",
        "side": "long",
        "title": "Do not short the open — bounce bucket",
        "label": "US red, VIX 15 or higher",
        "days": 47,
        "short_win_pct": 40.4,
        "long_win_pct": 59.6,
        "return_pct": None,
        "max_dd_pct": None,
        "gap_follow_pct": 59.6,
        "body": (
            "Red overnight with high VIX often gapped down then recovered into the close. "
            "Do not chase puts at 09:15. Wait for the 09:25 3m close before any long."
        ),
    },
    "us_green_vix_stable": {
        "id": "us_green_vix_stable",
        "side": "fade",
        "title": "Fade the green open — do not buy the gap",
        "label": "US green, VIX under 15",
        "days": 141,
        "short_win_pct": 58.9,
        "long_win_pct": 41.1,
        "return_pct": None,
        "max_dd_pct": None,
        "gap_follow_pct": 71.6,
        "body": (
            "US green usually gaps Nifty up, then open-to-close faded more often than not. "
            "Do not buy cash into the gap. Flat is valid; shorts only after a failed 5m."
        ),
    },
    "us_green_vix_volatile": {
        "id": "us_green_vix_volatile",
        "side": "flat",
        "title": "No 2-year edge — stay flat",
        "label": "US green, VIX 15 or higher",
        "days": None,
        "short_win_pct": None,
        "long_win_pct": None,
        "return_pct": None,
        "max_dd_pct": None,
        "gap_follow_pct": None,
        "body": (
            "This mix was too thin in the 2-year sample to call a side. "
            "Stand down until the first 5m is complete. Do not start the engine on the gap."
        ),
    },
    "mixed": {
        "id": "mixed",
        "side": "flat",
        "title": "Stay flat — US colour is mixed",
        "label": "Dow and Nasdaq disagree",
        "days": 124,
        "short_win_pct": 50.8,
        "long_win_pct": 49.2,
        "return_pct": None,
        "max_dd_pct": None,
        "gap_follow_pct": None,
        "body": (
            "Open-to-close was a coin flip on mixed US days. No cash longs, no calls, no shorts from the 09:00 board."
        ),
    },
    "wait": {
        "id": "wait",
        "side": "flat",
        "title": "No tape yet — wait",
        "label": "US or VIX missing",
        "days": None,
        "short_win_pct": None,
        "long_win_pct": None,
        "return_pct": None,
        "max_dd_pct": None,
        "gap_follow_pct": None,
        "body": "Daily side needs Dow, Nasdaq and India VIX. Refresh Command after 09:00.",
    },
}


def _daily_side(us_status, vix_last, vix_volatile):
    if us_status not in {"green", "red", "mixed"} or vix_last is None:
        bucket = dict(DAILY_BUCKETS["wait"])
    elif us_status == "mixed":
        bucket = dict(DAILY_BUCKETS["mixed"])
    elif us_status == "red" and not vix_volatile:
        bucket = dict(DAILY_BUCKETS["us_red_vix_stable"])
    elif us_status == "red" and vix_volatile:
        bucket = dict(DAILY_BUCKETS["us_red_vix_volatile"])
    elif us_status == "green" and not vix_volatile:
        bucket = dict(DAILY_BUCKETS["us_green_vix_stable"])
    else:
        bucket = dict(DAILY_BUCKETS["us_green_vix_volatile"])
    bucket["expires"] = "15:30 IST"
    bucket["window"] = STUDY_WINDOW
    bucket["engine"] = "leave_off"
    bucket["note"] = (
        "Today only. Built from frozen 2-year open-to-close stats, not from GIFT or A/D. "
        "A/D before 09:15 is yesterday's close."
    )
    return bucket


def _recommendation(us_tone, vix, vix_volatile, breadth, gift_tone, aligned):
    if aligned <= -2:
        return {
            "instrument": "cash_stand_down",
            "title": "Stand down on cash longs",
            "body": (
                "Overnight tape is red. Do not buy cash or call options into the 09:15 gap. "
                "Wait for a completed 5-point short/skip. Protective puts only after a bearish "
                "3m close, never as a pre-open guess."
            ),
        }
    if aligned >= 2 and not vix_volatile:
        return {
            "instrument": "cash",
            "title": "Cash first, after 09:25",
            "body": (
                "Bias is green and VIX is under 15, so the book should stay in cash equities: "
                "₹500 risk, 1 name per bullish sector, first-5m break, retrace, then 3m close. "
                "Skip options — you do not need leverage on a stable open."
            ),
        }
    if aligned >= 2 and vix_volatile:
        return {
            "instrument": "cash_reduced",
            "title": "Cash half-size; options only after confirm",
            "body": (
                "Bias is green but India VIX is above 15. Prefer cash at half the usual size "
                "and only after the 09:25 3m close. Do not chase ATM calls at the open — "
                "premium is expensive on a volatile gap. Defined-risk options wait for the same "
                "cash trigger, then one lot."
            ),
        }
    return {
        "instrument": "wait",
        "title": "No edge yet — wait for structure",
        "body": (
            "US, GIFT/SGX, VIX and breadth disagree. This checklist is bias only. "
            "Do not pick cash or options before 09:15. If the first 5m is a doji, skip the day."
        ),
    }


def build_briefing(force=False):
    import time as clock

    with _LOCK:
        now = clock.monotonic()
        if (
            not force
            and _CACHE["payload"] is not None
            and now - _CACHE["at"] < CACHE_SECONDS
        ):
            return _CACHE["payload"]

        stamps = _now()
        quotes = {}
        yahoo_error = ""
        try:
            quotes = _yahoo_quotes(list(YAHOO_SYMBOLS.values()))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as error:
            yahoo_error = str(error)

        dow = _quote(quotes, "dow_fut") if stamps.time() < clock_time(9, 15) else _quote(quotes, "dow")
        nasdaq = _quote(quotes, "nasdaq_fut") if stamps.time() < clock_time(9, 15) else _quote(quotes, "nasdaq")
        if not dow.get("ok"):
            dow = _quote(quotes, "dow", "dow_fut")
        if not nasdaq.get("ok"):
            nasdaq = _quote(quotes, "nasdaq", "nasdaq_fut")
        vix = _quote(quotes, "india_vix")
        nifty = _quote(quotes, "nifty")
        _, nse_breadth, nse_gift = _nse_bundle()
        mc_nifty, mc_breadth = _mc_nifty()
        if not nifty.get("ok") and mc_nifty.get("ok"):
            nifty = mc_nifty
        gift = nse_gift if nse_gift.get("ok") else _gift_groww()
        if not gift.get("ok"):
            gift = nifty
        breadth = nse_breadth if nse_breadth.get("ok") else mc_breadth

        us_tone = _tone(dow.get("change_pct"))
        nasdaq_tone = _tone(nasdaq.get("change_pct"))
        if us_tone == nasdaq_tone and us_tone in {"green", "red"}:
            us_status, us_score = us_tone, (1 if us_tone == "green" else -1)
            us_detail = (
                f"Dow {dow.get('change_pct'):+.2f}% · Nasdaq {nasdaq.get('change_pct'):+.2f}%. "
                f"India is likely to open {us_tone}."
            ) if dow.get("change_pct") is not None and nasdaq.get("change_pct") is not None else "US quotes missing."
        else:
            us_status, us_score = "mixed", 0
            us_detail = "Dow and Nasdaq disagree — treat the open as mixed, not a gift."

        vix_last = vix.get("last")
        vix_volatile = vix_last is not None and float(vix_last) >= VIX_STABLE
        if vix_last is None:
            vix_status, vix_detail = "wait", "India VIX unavailable. Save Dhan keys as backup."
        elif vix_volatile:
            vix_status, vix_detail = "volatile", f"India VIX {float(vix_last):.2f} is above 15 — expect a wide, noisy session."
        else:
            vix_status, vix_detail = "stable", f"India VIX {float(vix_last):.2f} is below 15 — a stable cash tape is more likely."

        if breadth.get("ok"):
            if breadth["bias"] == "bullish":
                br_status, br_score = "green", 1
                br_detail = f"Advances {breadth['advances']} vs declines {breadth['declines']} — breadth is bullish."
            elif breadth["bias"] == "bearish":
                br_status, br_score = "red", -1
                br_detail = f"Advances {breadth['advances']} vs declines {breadth['declines']} — breadth is bearish."
            else:
                br_status, br_score = "mixed", 0
                br_detail = "Advances and declines are even."
        else:
            br_status, br_score = "wait", 0
            br_detail = (
                "NSE breadth is blocked from this machine. Open nseindia.com Advance/Decline "
                f"manually. ({breadth.get('error') or 'no data'})"
            )

        gift_tone = _tone(gift.get("change_pct"))
        if gift.get("ok") and gift.get("change_pct") is not None:
            gift_score = 1 if gift_tone == "green" else -1
            gift_detail = (
                f"GIFT/SGX Nifty {gift['change_pct']:+.2f}%. Cash Nifty usually opens near this colour."
                if gift.get("symbol") == "GIFTNIFTY" or gift.get("name") == "GIFT Nifty"
                else f"Cash Nifty {gift['change_pct']:+.2f}% (GIFT quote missing — use this colour as proxy)."
            )
        else:
            gift_tone, gift_score = "wait", 0
            gift_detail = "GIFT/SGX quote missing. Use the US tape and VIX until it prints."

        aligned = us_score + br_score + gift_score
        window = clock_time(9, 0) <= stamps.time() <= clock_time(9, 25)
        rec = _recommendation(us_tone, vix_last, vix_volatile, breadth, gift_tone, aligned)
        daily = _daily_side(us_status, vix_last, vix_volatile)
        payload = {
            "ok": True,
            "generated_at": stamps.isoformat(),
            "session": str(stamps.date()),
            "window": "09:00-09:15 IST",
            "in_window": window,
            "yahoo_error": yahoo_error,
            "checks": [
                _check("Dow + Nasdaq", us_status, us_detail, us_score),
                _check("India VIX", vix_status, vix_detail, 0),
                _check("NSE advances / declines", br_status, br_detail, br_score),
                _check("GIFT / SGX Nifty", gift_tone, gift_detail, gift_score),
            ],
            "quotes": {
                "dow": dow,
                "nasdaq": nasdaq,
                "india_vix": vix,
                "nifty": nifty,
                "gift": gift,
            },
            "breadth": breadth,
            "aligned": aligned,
            "vix_volatile": vix_volatile,
            "recommendation": rec,
            "daily": daily,
            "rule": (
                "This is a 15-minute bias, not an order. Cash still needs the 09:15 5m bar, "
                "retrace, and 09:25 3m close. Options never lead the open."
            ),
        }
        try:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            temp = PATH + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temp, PATH)
        except OSError:
            pass
        _CACHE["at"] = clock.monotonic()
        _CACHE["payload"] = payload
        return payload


def load_saved():
    try:
        with open(PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
