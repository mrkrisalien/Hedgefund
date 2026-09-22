import pandas as pd

import config
from broker import connect, get_equity, load_security_master


WATCHLIST = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "SBIN",
    "AXISBANK",
    "ITC",
]

COMMODITY_ROOTS = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]


def print_rows(frame, limit=40):
    if frame.empty:
        print("  (none)")
        return

    shown = 0
    for _, row in frame.iterrows():
        print(
            f"  {row['SEM_TRADING_SYMBOL']} | {row['SEM_EXM_EXCH_ID']} | "
            f"{row['SEM_INSTRUMENT_NAME']} | security_id={row['SEM_SMST_SECURITY_ID']} | "
            f"{row['SEM_CUSTOM_SYMBOL']}"
        )
        shown += 1
        if shown >= limit:
            print(f"  ...truncated after {limit} rows.")
            break


if str(config.DHAN_CLIENT_ID or "").strip() and str(
    config.DHAN_ACCESS_TOKEN or ""
).strip():
    try:
        _, funds = connect()
        print("\nCONNECTED TO DHAN")
        print("=" * 60)
        print("Client ID:", config.DHAN_CLIENT_ID)
        print("Available balance:", get_equity(funds))
    except Exception as error:
        print("Dhan initialization failed.")
        print("Error:", error)
        print("Continuing with the public instrument master.\n")
else:
    print(
        "Dhan credentials not set. Searching the public instrument master only."
    )
    print(
        "Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in config.py to check the account.\n"
    )


print("\nSEARCHING FOR TRADEABLE SYMBOLS")
print("=" * 60)

master = load_security_master()
print(f"Total instruments available: {len(master)}\n")

nse_equity = master[
    (master["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE")
    & (master["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "EQUITY")
].copy()
if "SEM_SERIES" in nse_equity.columns:
    nse_equity = nse_equity[
        nse_equity["SEM_SERIES"].astype(str).str.upper().isin(["EQ", "BE", "SM", ""])
    ]

print("NSE CASH STOCKS (use these names in config.SYMBOLS)")
print("-" * 60)
nse_equity_symbols = nse_equity["SEM_TRADING_SYMBOL"].astype(str).str.upper()
watch = nse_equity[nse_equity_symbols.isin(WATCHLIST)].sort_values("SEM_TRADING_SYMBOL")
print_rows(watch)

print("\nMCX COMMODITY FUTURES (nearest listed contracts)")
print("-" * 60)
mcx = master[
    (master["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "MCX")
    & (master["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "FUTCOM")
].copy()
mcx_trading = mcx["SEM_TRADING_SYMBOL"].astype(str).str.upper()
commodity_mask = pd.Series(False, index=mcx.index)
for root in COMMODITY_ROOTS:
    commodity_mask = commodity_mask | mcx_trading.str.startswith(root)
print_rows(
    mcx.loc[commodity_mask].sort_values(["SEM_TRADING_SYMBOL", "SEM_EXPIRY_DATE"]),
    limit=30,
)

print("\nNSE INDEX UNDERLYINGS")
print("-" * 60)
index_rows = master[
    (master["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE")
    & (master["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "INDEX")
].copy()
index_symbols = index_rows["SEM_TRADING_SYMBOL"].astype(str).str.upper()
print_rows(
    index_rows[index_symbols.isin(["NIFTY", "BANKNIFTY", "FINNIFTY"])].sort_values(
        "SEM_TRADING_SYMBOL"
    )
)

print("\nDhan instrument search complete.")
print("Copy exact SEM_TRADING_SYMBOL values into config.SYMBOLS.")
print("For MCX, use: {\"symbol\": \"GOLD\", \"exchange\": \"MCX\", \"instrument\": \"FUTCOM\"}")
