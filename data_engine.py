"""Multi-timeframe market snapshots for the Dhan desk."""
from __future__ import annotations

import pandas as pd

from broker import (
    fetch_daily_candles,
    fetch_hourly_candles,
    get_equity,
    get_ltp_batch,
    get_quote,
    resolve_symbol_cached,
)
import config


def _rsi(closes, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def calculate_indicators(df):
    if df is None or df.empty:
        return {}
    frame = df.copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    volume = pd.to_numeric(
        frame["volume"] if "volume" in frame.columns else frame.get("tick_volume", 0),
        errors="coerce",
    ).fillna(0)
    ema = close.ewm(span=config.EMA_PERIOD, adjust=False).mean()
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(config.ATR_PERIOD).mean()
    vol_ma = volume.rolling(config.VOL_MA_PERIOD).mean()
    rel_vol = volume / vol_ma.replace(0, pd.NA)
    rsi = _rsi(close, config.RSI_PERIOD)
    last = frame.iloc[-1]
    recent = close.tail(10).astype(float).tolist()
    trend = "up" if len(recent) >= 2 and recent[-1] >= recent[0] else "down"
    return {
        "ema200": float(ema.iloc[-1]) if pd.notna(ema.iloc[-1]) else None,
        "rsi14": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
        "atr14": float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
        "rel_volume": float(rel_vol.iloc[-1]) if pd.notna(rel_vol.iloc[-1]) else None,
        "tick_volume": float(volume.iloc[-1]),
        "close": float(last["close"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "trend_10": trend,
        "structure": recent,
    }


def fetch_correlated_asset_prices(current_symbol, all_symbols=None):
    names = list(all_symbols or config.SYMBOLS)
    current = str(current_symbol).upper()
    others = []
    for name in names:
        if str(name).upper() == current:
            continue
        try:
            others.append(resolve_symbol_cached(name))
        except Exception:
            continue
    if not others:
        return {}
    try:
        prices = get_ltp_batch(others)
    except Exception as error:
        print(f"[SYSTEM] Correlated quotes failed: {error}")
        return {}
    out = {}
    for key, last in prices.items():
        out[key] = {"bid": last, "ask": last}
    return out


def fetch_multi_timeframe_data(symbol):
    instrument = resolve_symbol_cached(symbol)
    daily_df = fetch_daily_candles(instrument, bars=max(250, config.EMA_PERIOD + 20))
    hourly_df = fetch_hourly_candles(instrument, bars=250)
    quote = get_quote(instrument)

    if daily_df.empty:
        raise RuntimeError(f"Failed to fetch Daily data for {instrument['symbol']}")
    if hourly_df.empty:
        raise RuntimeError(f"Failed to fetch Hourly data for {instrument['symbol']}")

    ask_price = quote["ask"] or quote["last_price"]
    bid_price = quote["bid"] or quote["last_price"]
    if ask_price <= 0:
        raise RuntimeError(f"Failed to retrieve current price for {instrument['symbol']}")

    daily = calculate_indicators(daily_df)
    hourly = calculate_indicators(hourly_df)
    equity = get_equity()
    return {
        "symbol": instrument.get("trading_symbol") or instrument["symbol"],
        "daily_csv": daily_df.tail(30).to_csv(index=False),
        "hourly_csv": hourly_df.tail(30).to_csv(index=False),
        "equity": equity,
        "balance": equity,
        "ask_price": ask_price,
        "bid": bid_price,
        "ask": ask_price,
        "spread": abs(ask_price - bid_price),
        "daily_df": daily_df,
        "hourly_df": hourly_df,
        "instrument": instrument,
        "h1_data": hourly,
        "daily_data": daily,
    }
