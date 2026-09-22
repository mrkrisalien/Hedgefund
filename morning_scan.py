from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config
from broker import (
    fetch_historical_daily,
    fetch_historical_intraday,
    resolve_symbol_cached,
)
from sector_regime import allowed_calendar_sectors, passes_rs_gate, sector_allowed, tilt_score, trailing_return
from sectors import SECTORS, mcx_symbols


IST = ZoneInfo("Asia/Kolkata")
OPEN_START = time(9, 15)
OPEN_END = time(9, 20)


def _ist_times(frame):
    stamps = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(IST)
    out = frame.copy()
    out["ist_time"] = stamps.dt.time
    out["ist_date"] = stamps.dt.date
    return out


def opening_momentum_from_frame(candles, session_date):
    if candles is None or candles.empty:
        return None

    window = _ist_times(candles)
    window = window[
        (window["ist_date"] == session_date)
        & (window["ist_time"] >= OPEN_START)
        & (window["ist_time"] <= OPEN_END)
    ]
    if window.empty:
        return None

    start = float(window.iloc[0]["open"])
    end = float(window.iloc[-1]["close"])
    if start <= 0:
        return None

    return (end - start) / start


def opening_momentum(instrument, session_date=None):
    session_date = session_date or datetime.now(IST).date()
    candles = fetch_historical_intraday(
        instrument,
        session_date,
        session_date,
        interval=1,
    )
    return opening_momentum_from_frame(candles, session_date)


def resolve_index(index_names):
    last_error = None
    for name in index_names:
        try:
            return resolve_symbol_cached(
                {
                    "symbol": name,
                    "exchange": "NSE",
                    "instrument": "INDEX",
                }
            )
        except Exception as error:
            last_error = error
            try:
                return resolve_symbol_cached(name)
            except Exception as inner:
                last_error = inner
    raise RuntimeError(f"Could not resolve sector index {index_names}: {last_error}")


def _macro_returns(session_date):
    crude_ret = gold_ret = None
    if not getattr(config, "USE_MACRO_SECTOR_TILT", True):
        return None, None
    lookback = int(getattr(config, "SECTOR_RS_LOOKBACK", 20))
    for spec, bucket in (
        ({"symbol": "CRUDEOILM", "exchange": "MCX", "instrument": "FUTCOM"}, "crude"),
        ({"symbol": "GOLDM", "exchange": "MCX", "instrument": "FUTCOM"}, "gold"),
    ):
        try:
            instrument = resolve_symbol_cached(spec)
            daily = fetch_historical_daily(
                instrument,
                session_date - timedelta(days=lookback * 3),
                session_date,
            )
            if daily is None or daily.empty:
                continue
            stamps = pd.to_datetime(daily["time"], utc=True).dt.tz_convert(IST)
            daily = daily.copy()
            daily["ist_date"] = stamps.dt.date
            value = trailing_return(daily, session_date, lookback)
            if bucket == "crude":
                crude_ret = value
            else:
                gold_ret = value
        except Exception as error:
            print(f"Macro skip {bucket}: {error}")
    return crude_ret, gold_ret


def rank_sectors(session_date=None):
    session_date = session_date or datetime.now(IST).date()
    lookback = int(getattr(config, "SECTOR_RS_LOOKBACK", 20))
    crude_ret, gold_ret = _macro_returns(session_date)
    ranked = []
    allowed = allowed_calendar_sectors(session_date)
    if allowed is not None and not allowed:
        print(f"Calendar lock: no cash sector for {session_date} (month {session_date.month})")
        return []
    for sector_name, spec in SECTORS.items():
        if not sector_allowed(sector_name, session_date):
            continue
        try:
            index = resolve_index(spec["index_names"])
            momentum = opening_momentum(index, session_date)
            daily = fetch_historical_daily(
                index,
                session_date - timedelta(days=lookback * 3),
                session_date,
            )
            if daily is not None and not daily.empty:
                stamps = pd.to_datetime(daily["time"], utc=True).dt.tz_convert(IST)
                daily = daily.copy()
                daily["ist_date"] = stamps.dt.date
            rs = trailing_return(daily, session_date, lookback)
        except Exception as error:
            print(f"Sector skip {sector_name}: {error}")
            continue

        if momentum is None:
            continue
        if getattr(config, "REQUIRE_BULLISH_SECTOR", True) and momentum <= 0:
            print(
                f"Sector skip {sector_name}: not bullish "
                f"({momentum:.2%} opening range)"
            )
            continue
        lock = getattr(config, "CALENDAR_SECTOR_LOCK", False)
        if (
            getattr(config, "USE_SECTOR_TILT", True)
            and not lock
            and not passes_rs_gate(rs)
        ):
            print(f"Sector skip {sector_name}: 20d RS {rs:.2%} below floor")
            continue

        score = (
            tilt_score(sector_name, momentum, rs, session_date, crude_ret, gold_ret)
            if getattr(config, "USE_SECTOR_TILT", True)
            else float(momentum)
        )
        ranked.append(
            {
                "sector": sector_name,
                "momentum": momentum,
                "rs_20d": rs,
                "tilt_score": score,
                "stocks": spec["stocks"],
                "index": index["trading_symbol"],
            }
        )

    key = "tilt_score" if getattr(config, "USE_SECTOR_TILT", True) else "momentum"
    ranked.sort(key=lambda row: row.get(key) or 0, reverse=True)
    if getattr(config, "ONE_STOCK_PER_SECTOR", True):
        return ranked
    take = len(ranked) if getattr(config, "CALENDAR_SECTOR_LOCK", False) else int(
        getattr(config, "TOP_SECTORS", 2)
    )
    return ranked[:take]


def rank_sector_stocks(sector, session_date=None):
    ranked = []
    for symbol in sector["stocks"]:
        try:
            instrument = resolve_symbol_cached(symbol)
            momentum = opening_momentum(instrument, session_date)
        except Exception as error:
            print(f"Stock skip {symbol}: {error}")
            continue

        if momentum is None or momentum <= 0:
            continue

        ranked.append(
            {
                "symbol": symbol,
                "sector": sector["sector"],
                "momentum": momentum,
                "kind": "NSE_EQ",
            }
        )

    ranked.sort(key=lambda row: row["momentum"], reverse=True)
    return ranked[: config.TOP_STOCKS_PER_SECTOR]


def mcx_candidates(session_date=None):
    picks = []
    for spec in mcx_symbols():
        try:
            instrument = resolve_symbol_cached(spec)
            momentum = opening_momentum(instrument, session_date)
        except Exception as error:
            print(f"MCX skip {spec}: {error}")
            continue

        if momentum is None:
            print(f"MCX skip {instrument['trading_symbol']}: no 9:15-9:20 bars")
            continue
        if config.MCX_REQUIRE_POSITIVE_MOMENTUM and momentum <= 0:
            print(
                f"MCX skip {instrument['trading_symbol']}: "
                f"open momentum {momentum:.4%} is not positive"
            )
            continue

        picks.append(
            {
                "symbol": spec,
                "display": instrument["trading_symbol"],
                "sector": "MCX",
                "momentum": momentum,
                "kind": "MCX",
            }
        )
    return picks


def morning_universe(session_date=None):
    from watchlist import watchlist_active

    analyses = []
    catalyst_picks = []
    if watchlist_active():
        from catalyst_setup import catalyst_candidates

        analyses, catalyst_picks = catalyst_candidates(session_date)

    sectors = rank_sectors(session_date) if config.ENABLE_CASH_SEGMENT else []
    sector_picks = []
    if config.ENABLE_CASH_SEGMENT:
        for sector in sectors:
            rows = rank_sector_stocks(sector, session_date)
            for row in rows:
                row["source"] = "seasonal"
                row["strategy"] = "breakout"
            sector_picks.extend(rows)

    commodities = []
    if config.ENABLE_MCX_SEGMENT:
        commodities = mcx_candidates(session_date)
        for row in commodities:
            # MCX shares the five mixed slots with cash and F&O.
            row["source"] = "seasonal"
        commodities.sort(key=lambda row: row["momentum"], reverse=True)
        if config.ONE_METAL_AT_A_TIME:
            commodities = commodities[:1]
    equity_picks = catalyst_picks + sector_picks
    return {
        "sectors": sectors,
        "equities": equity_picks,
        "commodities": commodities,
        "candidates": equity_picks + commodities,
        "watchlist": analyses,
    }
