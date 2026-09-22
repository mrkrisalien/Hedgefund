from datetime import datetime, time as clock_time, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock
import time

import pandas as pd
from dhanhq import DhanContext, dhanhq

import config


EXCHANGE_SEGMENT_MAP = {
    ("NSE", "EQUITY"): dhanhq.NSE,
    ("BSE", "EQUITY"): dhanhq.BSE,
    ("NSE", "INDEX"): dhanhq.INDEX,
    ("BSE", "INDEX"): dhanhq.INDEX,
    ("NSE", "FUTSTK"): dhanhq.NSE_FNO,
    ("NSE", "FUTIDX"): dhanhq.NSE_FNO,
    ("NSE", "OPTSTK"): dhanhq.NSE_FNO,
    ("NSE", "OPTIDX"): dhanhq.NSE_FNO,
    ("BSE", "FUTSTK"): dhanhq.BSE_FNO,
    ("BSE", "FUTIDX"): dhanhq.BSE_FNO,
    ("BSE", "OPTSTK"): dhanhq.BSE_FNO,
    ("BSE", "OPTIDX"): dhanhq.BSE_FNO,
    ("MCX", "FUTCOM"): dhanhq.MCX,
    ("MCX", "OPTFUT"): dhanhq.MCX,
    ("NSE", "FUTCUR"): dhanhq.CUR,
    ("NSE", "OPTCUR"): dhanhq.CUR,
}

# Dhan's compact security master reports MCX order lot units as 1, while P&L
# is multiplied by the commodity contract value shown on live positions.
MCX_VALUE_MULTIPLIERS = {
    "CRUDEOILM": 10,
    "CRUDEOIL": 100,
    "GOLDM": 10,
    "GOLD": 100,
    "SILVERM": 5,
    "SILVER": 30,
    "NATURALGAS": 1250,
}


def contract_value_multiplier(symbol):
    root = str(symbol or "").upper().split("-")[0].replace(" ", "")
    return float(MCX_VALUE_MULTIPLIERS.get(root, 1))

_client = None
_security_master = None
_MARKET_DATA_LOCK = Lock()
_LTP_CACHE = {}


def _require_credentials():
    client_id = str(config.DHAN_CLIENT_ID or "").strip()
    access_token = str(config.DHAN_ACCESS_TOKEN or "").strip()

    if not client_id or not access_token:
        raise RuntimeError(
            "Dhan credentials are missing. Set DHAN_CLIENT_ID and "
            "DHAN_ACCESS_TOKEN in config.py (or as environment variables). "
            "Generate an access token from the Dhan web/app: "
            "My Profile -> DhanHQ Trading APIs."
        )

    return client_id, access_token


def get_client():
    global _client

    if _client is None:
        client_id, access_token = _require_credentials()
        _client = dhanhq(DhanContext(client_id, access_token))

    return _client


def connect():
    client = get_client()
    funds = require_success(client.get_fund_limits(), "Dhan fund-limit check")
    return client, funds


def check_trading_ip():
    """Ask Dhan which public IP this process is using for order APIs."""
    payload = get_client().dhan_http.get("/ip/getIP")
    if isinstance(payload, dict) and payload.get("data") not in (None, ""):
        inner = payload["data"]
        if isinstance(inner, dict):
            payload = inner
    if not isinstance(payload, dict):
        raise RuntimeError(f"IP check failed: {payload}")
    if str(payload.get("status", "")).lower() in ("failure", "error"):
        raise RuntimeError(payload.get("remarks") or payload)
    return payload


def format_ip_status(payload):
    if not isinstance(payload, dict):
        return f"Dhan IP payload: {payload}"

    def pick(*names):
        for name in names:
            if payload.get(name) not in (None, "", "NA"):
                return payload.get(name)
            remarks = payload.get("remarks")
            if isinstance(remarks, dict) and remarks.get(name) not in (None, "", "NA"):
                return remarks.get(name)
        return None

    detected = pick("detectedIP", "detected_ip") or "?"
    primary = pick("primaryIP", "primary_ip") or "not set"
    secondary = pick("secondaryIP", "secondary_ip") or "not set"
    match = pick("ipMatchStatus", "ip_match_status") or "?"
    allowed = pick("ordersAllowed", "orders_allowed")
    return (
        f"Dhan sees this PC as {detected}. "
        f"Whitelist primary={primary}, secondary={secondary}. "
        f"Match={match}, ordersAllowed={allowed}."
    )


def require_success(response, action):
    if not isinstance(response, dict):
        raise RuntimeError(f"{action} failed: {response}")

    status = str(response.get("status", "")).strip().lower()
    if status and status not in ("success", "ok"):
        remarks = response.get("remarks") or response.get("message") or response
        raise RuntimeError(f"{action} failed: {remarks}")

    if "data" in response and response["data"] not in (None, ""):
        return response["data"]

    return response


def _rate_limited_history(call, action, retries=6):
    last_error = None
    for attempt in range(retries):
        try:
            payload = require_success(call(), action)
            time.sleep(0.35)
            return payload
        except Exception as error:
            last_error = error
            text = str(error)
            if "DH-904" in text or "rate" in text.lower() or "too many" in text.lower():
                wait = 1.5 * (attempt + 1)
                print(f"  {action}: rate limited, retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
    raise last_error


def get_equity(funds=None):
    if funds is None:
        funds = require_success(get_client().get_fund_limits(), "Dhan fund-limit check")

    for key in (
        "availabelBalance",
        "availableBalance",
        "sodLimit",
        "withdrawableBalance",
    ):
        value = funds.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

    return 0.0


def load_security_master(force_refresh=False):
    global _security_master

    if _security_master is None or force_refresh:
        master_path = Path(__file__).resolve().parent / "security_id_list.csv"
        _security_master = dhanhq.fetch_security_list(
            "compact",
            filename=str(master_path),
        )

        if not isinstance(_security_master, pd.DataFrame) or _security_master.empty:
            raise RuntimeError("Failed to download the Dhan instrument master.")

        _security_master = pd.read_csv(master_path, low_memory=False)

        _security_master.columns = [
            str(column).strip() for column in _security_master.columns
        ]

        if "SEM_CUSTOM_SYMBOL" not in _security_master.columns:
            _security_master["SEM_CUSTOM_SYMBOL"] = _security_master.get(
                "SEM_TRADING_SYMBOL", ""
            )

    return _security_master


def _exchange_segment(exchange, instrument):
    segment = EXCHANGE_SEGMENT_MAP.get(
        (str(exchange).upper(), str(instrument).upper())
    )

    if not segment:
        raise RuntimeError(
            f"Unsupported Dhan instrument: {exchange} {instrument}."
        )

    return segment


def _parse_expiry(value):
    if pd.isna(value) or value in ("", 0, "0", "-1"):
        return None

    return pd.to_datetime(value, errors="coerce", utc=True)


def _normalize_symbol_request(symbol):
    if isinstance(symbol, dict):
        trading_symbol = str(symbol.get("symbol", "")).strip().upper()
        preferred_exchange = str(symbol.get("exchange", "NSE")).strip().upper()
        preferred_instrument = str(
            symbol.get("instrument", "EQUITY")
        ).strip().upper()
    else:
        trading_symbol = str(symbol).strip().upper()
        preferred_exchange = "NSE"
        preferred_instrument = "EQUITY"

    return trading_symbol, preferred_exchange, preferred_instrument


def resolve_symbol(symbol):
    trading_symbol, preferred_exchange, preferred_instrument = (
        _normalize_symbol_request(symbol)
    )

    if not trading_symbol:
        raise RuntimeError("Empty trading symbol.")

    master = load_security_master()
    trading_col = master["SEM_TRADING_SYMBOL"].astype(str).str.upper()
    custom_col = master["SEM_CUSTOM_SYMBOL"].astype(str).str.upper()

    matches = master[
        (trading_col == trading_symbol) | (custom_col == trading_symbol)
    ].copy()

    if matches.empty:
        matches = master[
            trading_col.str.startswith(trading_symbol + "-")
            | custom_col.str.contains(trading_symbol, na=False)
        ].copy()

    if matches.empty:
        raise RuntimeError(f"Symbol not found on Dhan: {trading_symbol}")

    matches["_expiry"] = matches["SEM_EXPIRY_DATE"].apply(_parse_expiry)
    now = pd.Timestamp.now(tz="UTC")

    live = matches[
        matches["_expiry"].isna() | (matches["_expiry"] >= now)
    ]
    if not live.empty:
        matches = live

    preferred = matches[
        (matches["SEM_EXM_EXCH_ID"].astype(str).str.upper() == preferred_exchange)
        & (matches["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == preferred_instrument)
    ]
    if not preferred.empty:
        matches = preferred

    if matches["_expiry"].notna().any():
        matches = matches.sort_values("_expiry", ascending=True)

    row = matches.iloc[0]
    exchange = str(row["SEM_EXM_EXCH_ID"]).upper()
    instrument = str(row["SEM_INSTRUMENT_NAME"]).upper()
    resolved_trading_symbol = str(
        row.get("SEM_TRADING_SYMBOL") or trading_symbol
    )
    underlying = resolved_trading_symbol.upper().split("-")[0]
    value_multiplier = (
        contract_value_multiplier(underlying) if exchange == "MCX" else 1.0
    )

    lot_size = 1
    try:
        lot_size = max(1, int(float(row.get("SEM_LOT_UNITS", 1) or 1)))
    except (TypeError, ValueError):
        lot_size = 1

    tick_size = 0.05
    try:
        # Dhan's compact security master stores tick sizes in paise. Examples:
        # NSE cash 5 -> Rs 0.05, MCX CRUDEOIL 100 -> Rs 1.00.
        tick_size = float(row.get("SEM_TICK_SIZE", 5) or 5) / 100.0
    except (TypeError, ValueError):
        tick_size = 0.05

    return {
        "symbol": trading_symbol,
        "security_id": str(int(float(row["SEM_SMST_SECURITY_ID"]))),
        "exchange": exchange,
        "instrument": instrument,
        "exchange_segment": _exchange_segment(exchange, instrument),
        "lot_size": lot_size,
        "value_multiplier": value_multiplier,
        "tick_size": tick_size,
        "display_symbol": str(row.get("SEM_CUSTOM_SYMBOL") or trading_symbol),
        "trading_symbol": resolved_trading_symbol,
    }


@lru_cache(maxsize=64)
def _resolve_symbol_cached(trading_symbol, preferred_exchange, preferred_instrument):
    return resolve_symbol(
        {
            "symbol": trading_symbol,
            "exchange": preferred_exchange,
            "instrument": preferred_instrument,
        }
    )


def resolve_symbol_cached(symbol):
    return _resolve_symbol_cached(*_normalize_symbol_request(symbol))


def round_to_tick(price, tick_size):
    tick_size = float(tick_size or 0.05)
    if tick_size <= 0:
        return round(float(price), 2)

    ticks = round(float(price) / tick_size)
    rounded = ticks * tick_size
    decimals = 0 if tick_size >= 1 else len(str(tick_size).rstrip("0").split(".")[-1])
    return round(rounded, max(decimals, 2))


def _candles_to_dataframe(payload):
    if isinstance(payload, pd.DataFrame):
        frame = payload.copy()
    elif isinstance(payload, dict):
        lengths = [
            len(payload[key])
            for key in ("open", "high", "low", "close", "timestamp")
            if key in payload and isinstance(payload[key], list)
        ]
        if not lengths or min(lengths) == 0:
            return pd.DataFrame()

        frame = pd.DataFrame(
            {
                "time": payload.get("timestamp", []),
                "open": payload.get("open", []),
                "high": payload.get("high", []),
                "low": payload.get("low", []),
                "close": payload.get("close", []),
                "volume": payload.get("volume", []),
            }
        )
    elif isinstance(payload, list):
        frame = pd.DataFrame(payload)
    else:
        raise RuntimeError(f"Unexpected candle payload: {payload}")

    if "time" not in frame.columns:
        for candidate in ("timestamp", "start_Time", "start_time"):
            if candidate in frame.columns:
                frame = frame.rename(columns={candidate: "time"})
                break

    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True, errors="coerce")
    if frame["time"].isna().all():
        frame["time"] = pd.to_datetime(frame["time"], utc=True, errors="coerce")

    return frame.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def _as_date(value):
    if hasattr(value, "year") and hasattr(value, "month") and not hasattr(value, "hour"):
        return value
    return pd.Timestamp(value).date()


def _date_chunks(from_date, to_date, max_days):
    start = _as_date(from_date)
    end = _as_date(to_date)
    cursor = start
    span = max(1, int(max_days))
    while cursor <= end:
        stop = min(cursor + timedelta(days=span - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def _merge_ohlc(frames):
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    out = pd.concat(valid, ignore_index=True)
    return out.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)


OHLC_CACHE_DIR = Path(__file__).resolve().parent / "data" / "ohlc_cache"


def _ohlc_cache_path(instrument, kind, interval, from_date, to_date):
    OHLC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sid = instrument.get("security_id") or instrument.get("symbol")
    return OHLC_CACHE_DIR / f"{sid}_{kind}_{interval}_{from_date}_{to_date}.pkl"


def _load_ohlc_cache(path):
    if not path.exists():
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _save_ohlc_cache(path, frame):
    try:
        frame.to_pickle(path)
    except Exception:
        pass


def fetch_historical_daily(instrument, from_date, to_date):
    from_date = _as_date(from_date)
    to_date = _as_date(to_date)
    cache_path = _ohlc_cache_path(instrument, "d", 1440, from_date, to_date)
    cached = _load_ohlc_cache(cache_path)
    if cached is not None and not cached.empty:
        return cached

    client = get_client()
    frames = []
    for start, stop in _date_chunks(from_date, to_date, 360):
        try:
            payload = _rate_limited_history(
                lambda s=start, e=stop: client.historical_daily_data(
                    security_id=instrument["security_id"],
                    exchange_segment=instrument["exchange_segment"],
                    instrument_type=instrument["instrument"],
                    from_date=str(s),
                    to_date=str(e),
                ),
                f"Daily candles for {instrument['symbol']} {start}..{stop}",
            )
            frames.append(_candles_to_dataframe(payload))
        except Exception as error:
            print(f"  Daily {instrument['symbol']} {start}..{stop} failed: {error}")
    frame = _merge_ohlc(frames)
    if not frame.empty:
        _save_ohlc_cache(cache_path, frame)
    return frame


def _intraday_once(instrument, from_date, to_date, interval):
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime.now(ist)
    start_d = _as_date(from_date)
    end_d = _as_date(to_date)
    start = datetime.combine(start_d, clock_time(9, 15), tzinfo=ist)
    if end_d >= now.date():
        stop = now
    else:
        stop = datetime.combine(end_d, clock_time(15, 30), tzinfo=ist)
    if stop < start:
        stop = start
    client = get_client()
    payload = _rate_limited_history(
        lambda: client.intraday_minute_data(
            security_id=instrument["security_id"],
            exchange_segment=instrument["exchange_segment"],
            instrument_type=instrument["instrument"],
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=stop.strftime("%Y-%m-%d %H:%M:%S"),
            interval=interval,
        ),
        f"{interval}-min candles for {instrument['symbol']} {start_d}..{end_d}",
    )
    return _candles_to_dataframe(payload)


def fetch_historical_intraday(instrument, from_date, to_date, interval=60, skip_cache=False):
    from_date = _as_date(from_date)
    to_date = _as_date(to_date)
    interval = int(interval or 60)
    cache_path = _ohlc_cache_path(instrument, "i", interval, from_date, to_date)
    if not skip_cache:
        cached = _load_ohlc_cache(cache_path)
        if cached is not None and not cached.empty:
            return cached

    if interval <= 1:
        chunk_days = 5
    elif interval <= 5:
        chunk_days = 30
    else:
        chunk_days = 90

    frames = []
    for start, stop in _date_chunks(from_date, to_date, chunk_days):
        try:
            frames.append(_intraday_once(instrument, start, stop, interval))
        except Exception as error:
            text = str(error).lower()
            print(f"  {interval}m {instrument['symbol']} {start}..{stop} failed: {error}")
            retryable = "too long" in text or "invalid" in text or "range" in text or "dh-904" in text
            if retryable and chunk_days > 5:
                for inner_start, inner_stop in _date_chunks(start, stop, 5):
                    try:
                        frames.append(
                            _intraday_once(instrument, inner_start, inner_stop, interval)
                        )
                    except Exception as inner_error:
                        print(
                            f"  {interval}m {instrument['symbol']} "
                            f"{inner_start}..{inner_stop} failed: {inner_error}"
                        )
    frame = _merge_ohlc(frames)
    if not frame.empty and not skip_cache:
        _save_ohlc_cache(cache_path, frame)
    return frame


def fetch_historical_hourly(instrument, from_date, to_date):
    return fetch_historical_intraday(instrument, from_date, to_date, interval=60)


def fetch_daily_candles(instrument, bars=10):
    to_date = datetime.now().date()
    from_date = to_date - timedelta(days=max(30, bars * 3))
    return fetch_historical_daily(instrument, from_date, to_date).tail(bars)


def fetch_hourly_candles(instrument, bars=24):
    to_date = datetime.now().date()
    days = 14 if bars <= 40 else max(45, int(bars / 5) + 5)
    from_date = to_date - timedelta(days=days)
    return fetch_historical_hourly(instrument, from_date, to_date).tail(bars)


def fetch_five_minute_candles(instrument, bars=80):
    to_date = datetime.now().date()
    from_date = to_date - timedelta(days=10)
    return fetch_historical_intraday(
        instrument, from_date, to_date, interval=5
    ).tail(bars)


def fetch_live_session_candles(instrument, interval=1):
    """Current IST session, falling back to the latest session in seven days."""
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    today = datetime.now(ist).date()
    interval = int(interval or 1)
    frame = fetch_historical_intraday(
        instrument, today, today, interval=interval, skip_cache=True
    )
    if frame is None or frame.empty:
        frame = fetch_historical_intraday(
            instrument,
            today - timedelta(days=7),
            today,
            interval=interval,
            skip_cache=True,
        )
    if frame is None or frame.empty:
        return pd.DataFrame()
    stamps = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(ist)
    today_rows = frame.loc[stamps.dt.date == today]
    if not today_rows.empty:
        return today_rows.reset_index(drop=True)
    latest_session = max(stamps.dt.date)
    return frame.loc[stamps.dt.date == latest_session].reset_index(drop=True)


def _unwrap_market_payload(payload):
    current = payload
    for _ in range(3):
        if not isinstance(current, dict):
            return current
        inner = current.get("data")
        if not isinstance(inner, dict):
            return current
        if inner.get("last_price") is not None or inner.get("LTP") is not None:
            return inner
        current = inner
    return current


def _find_quote_node(payload, security_id):
    if not isinstance(payload, dict):
        return None

    sid = str(int(float(security_id)))
    if payload.get("last_price") is not None or payload.get("LTP") is not None:
        return payload

    for key in (sid, security_id):
        node = payload.get(key)
        if isinstance(node, dict) and (
            node.get("last_price") is not None
            or node.get("LTP") is not None
            or "depth" in node
        ):
            return node

    for value in payload.values():
        if isinstance(value, dict):
            found = _find_quote_node(value, sid)
            if found:
                return found
    return None


def get_quote(instrument):
    client = get_client()
    security_id = instrument["security_id"]
    segment = instrument["exchange_segment"]
    last_error = None

    for attempt in range(4):
        try:
            with _MARKET_DATA_LOCK:
                raw = client.quote_data({segment: [int(security_id)]})
            payload = require_success(raw, f"Quote for {instrument['symbol']}")
            payload = _unwrap_market_payload(payload)
            if isinstance(payload, dict) and payload.get("status") == "success":
                payload = _unwrap_market_payload(payload)

            quote = _find_quote_node(payload, security_id)
            if not isinstance(quote, dict):
                raise RuntimeError(
                    f"No quote returned for {instrument['symbol']}: {raw}"
                )

            last_price = float(quote.get("last_price") or quote.get("LTP") or 0)
            depth = quote.get("depth") or {}
            sell_depth = depth.get("sell") or []
            buy_depth = depth.get("buy") or []

            ask = last_price
            bid = last_price
            if sell_depth:
                ask = float(sell_depth[0].get("price") or last_price)
            if buy_depth:
                bid = float(buy_depth[0].get("price") or last_price)
            if ask <= 0:
                ask = last_price
            if bid <= 0:
                bid = last_price
            if last_price <= 0 and ask <= 0:
                raise RuntimeError(f"Zero price for {instrument['symbol']}")

            return {
                "last_price": last_price or ask,
                "ask": ask or last_price,
                "bid": bid or last_price,
                "raw": quote,
            }
        except Exception as error:
            last_error = error
            time.sleep(0.6 * (attempt + 1))

    raise RuntimeError(last_error)


def get_ltp_batch(instruments):
    grouped = {}
    lookup = {}
    for instrument in instruments:
        segment = instrument["exchange_segment"]
        security_id = int(instrument["security_id"])
        grouped.setdefault(segment, [])
        if security_id not in grouped[segment]:
            grouped[segment].append(security_id)
        lookup[(segment, str(security_id))] = instrument

    last_error = None
    for attempt in range(3):
        try:
            with _MARKET_DATA_LOCK:
                raw = get_client().ticker_data(grouped)
            payload = require_success(raw, "Batch LTP")
            payload = _unwrap_market_payload(payload)
            prices = {}
            now = time.monotonic()
            for segment, ids in grouped.items():
                segment_data = payload.get(segment, {}) if isinstance(payload, dict) else {}
                if not isinstance(segment_data, dict):
                    continue
                for security_id in ids:
                    quote = (
                        segment_data.get(security_id)
                        or segment_data.get(str(security_id))
                        or {}
                    )
                    last_price = 0.0
                    if isinstance(quote, dict):
                        last_price = float(
                            quote.get("last_price") or quote.get("LTP") or 0
                        )
                    elif isinstance(quote, (int, float)):
                        last_price = float(quote)
                    instrument = lookup.get((segment, str(security_id)))
                    if instrument and last_price > 0:
                        symbol = instrument["trading_symbol"]
                        prices[symbol] = last_price
                        _LTP_CACHE[(segment, str(security_id))] = (now, last_price)
            for key, instrument in lookup.items():
                symbol = instrument["trading_symbol"]
                item = _LTP_CACHE.get(key)
                if symbol not in prices and item and now - item[0] <= 20:
                    prices[symbol] = item[1]
            if prices:
                return prices
            raise RuntimeError(f"Batch LTP returned no prices: {raw}")
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.75 * (attempt + 1))

    now = time.monotonic()
    cached = {}
    for key, instrument in lookup.items():
        item = _LTP_CACHE.get(key)
        if item and now - item[0] <= 20:
            cached[instrument["trading_symbol"]] = item[1]
    if cached:
        return cached
    raise RuntimeError(last_error)


def get_positions():
    payload = require_success(get_client().get_positions(), "Dhan positions")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("positions") or []
        if isinstance(data, list):
            return data
    return []


def get_trade_book(order_id=None):
    client = get_client()
    payload = require_success(
        client.get_trade_book(order_id) if order_id else client.get_trade_book(),
        "Dhan trade book",
    )
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("trades") or []
        if isinstance(data, list):
            return data
    return []


def close_position_market(position):
    """Square off an open Dhan INTRA/CNC position with a market order."""
    client = get_client()
    qty = int(float(position.get("netQty") or position.get("quantity") or 0))
    if qty == 0:
        qty = abs(int(float(position.get("buyQty") or 0) - float(position.get("sellQty") or 0)))
    qty = abs(qty)
    if qty < 1:
        raise RuntimeError("Position quantity is zero.")
    side = str(position.get("positionType") or position.get("transactionType") or "LONG").upper()
    close_side = "SELL" if side in ("LONG", "BUY") else "BUY"
    security_id = str(position.get("securityId") or position.get("security_id"))
    segment = position.get("exchangeSegment") or position.get("exchange_segment")
    product = position.get("productType") or position.get("product_type") or client.INTRA
    response = client.place_order(
        security_id=security_id,
        exchange_segment=segment,
        transaction_type=close_side,
        quantity=qty,
        order_type=client.MARKET,
        product_type=product,
        price=0,
        tag="AI-HEDGE-CLOSE",
    )
    return require_success(response, f"Close {security_id}")
