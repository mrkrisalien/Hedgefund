"""Morning catalyst names you paste in. Edit this file or POST /api/watchlist."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import config

WATCHLIST_PATH = Path(__file__).resolve().parent / "data" / "morning_watchlist.json"

# NSE aliases tried in order when the headline name is not the ticker.
NAME_ALIASES = {
    "VODAFONE IDEA": ["IDEA"],
    "IDEA": ["IDEA"],
    "ADANI ENTERPRISES": ["ADANIENT"],
    "ADANIENT": ["ADANIENT"],
    "L&T": ["LT"],
    "LARSEN": ["LT"],
    "LARSEN & TOUBRO": ["LT"],
    "ENVIRO INFRA": ["EIEL", "ENVIROINFRA", "ENVIRO"],
    "GRAPHITE INDIA": ["GRAPHITE"],
    "GRAPHITE": ["GRAPHITE"],
    "POWER MECH": ["POWERMECH"],
    "POWERMECH": ["POWERMECH"],
    "SHAKTI PUMPS": ["SHAKTIPUMP"],
    "SHAKTIPUMP": ["SHAKTIPUMP"],
    "IDFC FIRST BANK": ["IDFCFIRSTB"],
    "IDFCFIRSTB": ["IDFCFIRSTB"],
}

OPTION_CONTRACT_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9]+)\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Z]{3,9})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s+"
    r"(?P<option_type>CE|PE|CALL|PUT)$",
    re.IGNORECASE,
)
MCX_OPTION_UNDERLYINGS = {
    "CRUDEOIL",
    "CRUDEOILM",
    "GOLD",
    "GOLDM",
    "SILVER",
    "SILVERM",
    "NATURALGAS",
}
BSE_OPTION_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


def _key(name):
    return str(name or "").upper().replace(".", "").strip()


def aliases_for(name, symbol=None):
    out = []
    if symbol:
        out.append(str(symbol).upper())
    key = _key(name)
    out.extend(NAME_ALIASES.get(key, []))
    if name and str(name).isalpha() and len(str(name)) <= 20:
        out.append(str(name).upper().replace(" ", ""))
    seen = set()
    ordered = []
    for item in out:
        item = str(item).upper().strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def normalize_option_contract(value):
    """Return Dhan's custom-symbol form, or None for a non-option value."""
    text = " ".join(str(value or "").upper().replace("-", " ").split())
    match = OPTION_CONTRACT_RE.fullmatch(text)
    if not match:
        return None
    parts = match.groupdict()
    option_type = {"CE": "CALL", "PE": "PUT"}.get(
        parts["option_type"].upper(), parts["option_type"].upper()
    )
    strike = float(parts["strike"])
    strike_text = str(int(strike)) if strike.is_integer() else str(strike)
    return (
        f"{parts['underlying'].upper()} {int(parts['day']):02d} "
        f"{parts['month'][:3].upper()} {strike_text} {option_type}"
    )


def option_lookup_names(value, expiry=None):
    """All Dhan custom / trading-symbol spellings for one option contract."""
    text = " ".join(str(value or "").upper().replace("-", " ").split())
    names = []
    if text:
        names.append(text)
    match = OPTION_CONTRACT_RE.fullmatch(text)
    if not match:
        return list(dict.fromkeys(names))
    parts = match.groupdict()
    underlying = parts["underlying"].upper()
    day = int(parts["day"])
    month = parts["month"][:3].upper()
    strike = float(parts["strike"])
    strike_text = str(int(strike)) if strike.is_integer() else str(strike)
    raw_right = parts["option_type"].upper()
    if raw_right in {"CE", "CALL"}:
        rights = ["CALL", "CE"]
    elif raw_right in {"PE", "PUT"}:
        rights = ["PUT", "PE"]
    else:
        rights = [raw_right]
    for day_text in (f"{day:02d}", str(day)):
        for right in rights:
            names.append(f"{underlying} {day_text} {month} {strike_text} {right}")
    stamp = None
    if expiry:
        try:
            stamp = datetime.strptime(str(expiry)[:10], "%Y-%m-%d")
        except ValueError:
            stamp = None
    if stamp is None:
        try:
            stamp = datetime.strptime(f"{day} {month}", "%d %b").replace(
                year=datetime.now().year
            )
        except ValueError:
            stamp = None
    if stamp is not None:
        pe_ce = "CE" if raw_right in {"CE", "CALL"} else "PE"
        names.append(f"{underlying}-{stamp.strftime('%b%Y')}-{strike_text}-{pe_ce}")
    return list(dict.fromkeys(names))


def infer_option_market(contract):
    normalized = normalize_option_contract(contract)
    if not normalized:
        return None
    underlying = normalized.split()[0]
    if underlying in MCX_OPTION_UNDERLYINGS:
        return "MCX", "OPTFUT"
    if underlying in BSE_OPTION_UNDERLYINGS:
        return "BSE", "OPTIDX"
    return "NSE", "OPTIDX"


def instrument_requests(item):
    """Build exact broker requests for an equity name or option contract."""
    name = item.get("name") or item.get("symbol")
    symbol = item.get("symbol")
    contract = normalize_option_contract(symbol or name)
    exchange = str(item.get("exchange") or "").strip().upper()
    instrument = str(item.get("instrument") or "").strip().upper()

    if contract:
        inferred_exchange, inferred_instrument = infer_option_market(contract)
        request = {
            "symbol": contract,
            "exchange": exchange or inferred_exchange,
            "instrument": instrument or inferred_instrument,
        }
        if item.get("security_id"):
            request["security_id"] = str(item.get("security_id"))
        if item.get("expiry"):
            request["expiry"] = str(item.get("expiry"))
        return [request]

    if exchange and instrument:
        return [
            {"symbol": symbol or name, "exchange": exchange, "instrument": instrument}
        ] + aliases_for(name, symbol)

    return aliases_for(name, symbol)


def load_watchlist():
    if not WATCHLIST_PATH.exists():
        return {"date": "", "names": []}
    try:
        payload = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"date": "", "names": []}
    if isinstance(payload, list):
        return {"date": "", "names": payload}
    payload.setdefault("names", [])
    return payload


def save_watchlist(doc):
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def watchlist_active():
    if not getattr(config, "USE_CATALYST_WATCHLIST", False):
        return False
    names = load_watchlist().get("names") or []
    return bool(names)
