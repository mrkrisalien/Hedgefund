import config

SECTORS = {
    "NIFTY BANK": {
        "index_names": ["NIFTY BANK", "BANKNIFTY", "NIFTYBANK"],
        "stocks": [
            "HDFCBANK",
            "ICICIBANK",
            "SBIN",
            "KOTAKBANK",
            "AXISBANK",
            "INDUSINDBK",
            "BANKBARODA",
            "PNB",
            "FEDERALBNK",
            "IDFCFIRSTB",
        ],
    },
    "NIFTY IT": {
        "index_names": ["NIFTY IT", "NIFTYIT", "CNXIT"],
        "stocks": [
            "TCS",
            "INFY",
            "HCLTECH",
            "WIPRO",
            "TECHM",
            "LTIM",
            "PERSISTENT",
            "COFORGE",
            "MPHASIS",
            "LTTS",
        ],
    },
    "NIFTY AUTO": {
        "index_names": ["NIFTY AUTO", "NIFTYAUTO"],
        "stocks": [
            "MARUTI",
            "M&M",
            "TATAMOTORS",
            "BAJAJ-AUTO",
            "HEROMOTOCO",
            "EICHERMOT",
            "TVSMOTOR",
            "MOTHERSON",
            "BOSCHLTD",
            "BHARATFORG",
        ],
    },
    "NIFTY PHARMA": {
        "index_names": ["NIFTY PHARMA", "NIFTYPHARMA"],
        "stocks": [
            "SUNPHARMA",
            "DRREDDY",
            "CIPLA",
            "DIVISLAB",
            "LUPIN",
            "AUROPHARMA",
            "TORNTPHARM",
            "ALKEM",
            "GLENMARK",
            "BIOCON",
        ],
    },
    "NIFTY FMCG": {
        "index_names": ["NIFTY FMCG", "NIFTYFMCG"],
        "stocks": [
            "HINDUNILVR",
            "ITC",
            "NESTLEIND",
            "BRITANNIA",
            "TATACONSUM",
            "GODREJCP",
            "DABUR",
            "MARICO",
            "COLPAL",
            "VBL",
        ],
    },
    "NIFTY METAL": {
        "index_names": ["NIFTY METAL", "NIFTYMETAL"],
        "stocks": [
            "TATASTEEL",
            "JSWSTEEL",
            "HINDALCO",
            "VEDL",
            "COALINDIA",
            "JINDALSTEL",
            "NMDC",
            "HINDZINC",
            "NATIONALUM",
            "SAIL",
        ],
    },
    "NIFTY ENERGY": {
        "index_names": ["NIFTY ENERGY", "NIFTYENERGY"],
        "stocks": [
            "RELIANCE",
            "ONGC",
            "NTPC",
            "POWERGRID",
            "IOC",
            "BPCL",
            "GAIL",
            "TATAPOWER",
            "ADANIGREEN",
            "ADANIPOWER",
        ],
    },
    "NIFTY REALTY": {
        "index_names": ["NIFTY REALTY", "NIFTYREALTY"],
        "stocks": [
            "DLF",
            "GODREJPROP",
            "OBEROIRLTY",
            "PHOENIXLTD",
            "PRESTIGE",
            "BRIGADE",
        ],
    },
    "NIFTY FIN SERVICE": {
        "index_names": ["NIFTY FIN SERVICE", "NIFTYFIN", "FINNIFTY"],
        "stocks": [
            "BAJFINANCE",
            "BAJAJFINSV",
            "HDFCLIFE",
            "SBILIFE",
            "ICICIPRULI",
            "PFC",
            "RECLTD",
            "CHOLAFIN",
            "MUTHOOTFIN",
            "HDFCAMC",
        ],
    },
}


MCX_FULL = [
    {"symbol": "GOLD", "exchange": "MCX", "instrument": "FUTCOM"},
    {"symbol": "SILVER", "exchange": "MCX", "instrument": "FUTCOM"},
    {"symbol": "CRUDEOIL", "exchange": "MCX", "instrument": "FUTCOM"},
]

MCX_MINI = [
    {"symbol": "GOLDM", "exchange": "MCX", "instrument": "FUTCOM"},
    {"symbol": "SILVERM", "exchange": "MCX", "instrument": "FUTCOM"},
    {"symbol": "CRUDEOILM", "exchange": "MCX", "instrument": "FUTCOM"},
]


def mcx_symbols():
    return MCX_MINI if config.MCX_USE_MINI else MCX_FULL


def sector_for_symbol(symbol):
    name = str(symbol or "").upper().split()[0]
    if not name:
        return ""
    for sector_name, spec in SECTORS.items():
        for stock in spec.get("stocks") or []:
            if str(stock).upper() == name:
                return sector_name
    return ""
