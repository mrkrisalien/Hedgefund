import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env")

MEMORY_FILE = BASE_DIR / "memory.json"
RULES_FILE = BASE_DIR / "new_rules.json"

# Secrets are entered on the dashboard Settings page and held in the browser
# plus process memory. Do not hardcode them here or in .env.
DHAN_CLIENT_ID = ""
DHAN_ACCESS_TOKEN = ""
GROQ_API_KEY = ""

# Quantity in shares (or lots for F&O/MCX after resolution uses lot size
# only as a minimum). Keep this small while testing.
TRADE_QUANTITY = 1

# Cash names are chosen each morning from the top 2 sectors.
# GOLD / SILVER / CRUDEOIL are always included via morning_scan.MCX_SYMBOLS.
_symbols_env = os.getenv("SYMBOLS", "RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK")
SYMBOLS = [s.strip() for s in _symbols_env.split(",") if s.strip()]

# Live Dhan order submission. Requires PAPER_TRADE = False.
TRADING_ENABLED = True

# Equal-risk live desk. Five mixed slots across cash / F&O / commodity.
LIVE_FIXED_PROTECTION_ENABLED = True
LIVE_MAX_ENTRIES_PER_DAY = 5
LIVE_TRADE_RISK_RS = 500.0
LIVE_PENDING_ORDER_TIMEOUT_SECONDS = 120

# Legacy percent stops are unused when ATR stops are on.
SL_PERCENT = 0.002
TP_PERCENT = 0.004

# Opening-range long: ATR stop (not the 9:15 wick) and a target the tape can actually hit.
USE_FIVE_MINUTE_STOP = False
ATR_PERIOD = 14
SL_ATR_MULT = 0.75
TP_ATR_MULT = 2.25
USE_CATALYST_WATCHLIST = True
USE_STRUCTURE_SETUP = True
# Five equal-risk slots. Each stop is capped at Rs 500; capital is split 5 ways.
CATALYST_RISK_RS = 500.0
BEST_CATALYST_ENTRIES = 7
BEST_SECTOR_ENTRIES = 3
COMBINED_SCAN_CUTOFF = "15:15"
# New cash entries only after the 09:15 bar is complete and one more 5m has printed.
LIVE_ENTRY_START = "09:25"
ONE_STOCK_PER_SECTOR = True
BLOCK_REPEAT_SYMBOL_TODAY = True
REQUIRE_FIRST_5M_RETRACE = True
REQUIRE_3M_CLOSE_ABOVE_DAY_HIGH = True
REWARD_RATIO = 2.0
# Virtual 1:2 rungs. Dhan keeps a far emergency target so the first 1:2 does not exit.
RUNG_TRAIL_ENABLED = True
TRAIL_MAX_EXTRA_RUNGS = 2
TRAIL_EMERGENCY_RR = 8.0
TRAIL_NO_NEW_RUNG_AFTER = "14:30"
BREAKOUT_ALERTS_ENABLED = True
BREAKOUT_VOLUME_SPIKE_RATIO = 1.5
BREAKOUT_OI_SPIKE_PCT = 1.0
# Twenty-four completed five-minute candles form the rolling two-hour range.
RANGE_BREAKOUT_LOOKBACK_BARS = 24
MOMENTUM_ATR_MIN = 0.15
SL_ATR_MIN = 0.20
SL_ATR_MAX = 2.00
BEST_CASH_ENTRIES = 5
BEST_MCX_ENTRIES = 2
CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", "65"))
MIN_CONFIDENCE = CONFIDENCE_THRESHOLD
DEFAULT_RISK_PERCENT = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
EMA_PERIOD = 200
RSI_PERIOD = 14
VOL_MA_PERIOD = 20
AUDIT_TRADE_THRESHOLD = int(os.getenv("AUDIT_TRADE_THRESHOLD", "10"))
AUDIT_COOLDOWN_HOURS = int(os.getenv("AUDIT_COOLDOWN_HOURS", "12"))
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
USE_SECTOR_TILT = True
# Seasonal sector selection runs alongside the catalyst watchlist.
CALENDAR_SECTOR_LOCK = False
USE_CALENDAR_SECTOR_PRIOR = True
USE_MACRO_SECTOR_TILT = True
MACRO_MOVE_THRESHOLD = 0.03
SECTOR_RS_LOOKBACK = 20
# With calendar lock, do not drop the month's sector on a weak 20d RS.
SECTOR_RS_MIN = -0.04
TOP_SECTORS = 8
TOP_STOCKS_PER_SECTOR = 1
# False routes accepted setups to Dhan because TRADING_ENABLED is True.
PAPER_TRADE = False
# Full GOLD/SILVER/CRUDEOIL lots need large margin. Mini contracts keep 1-lot size
# usable on a typical cash account. Set False to use the full contracts.
MCX_USE_MINI = True

# Restricted live recovery mode scans cash only.
# Independent backtests can still pass --segment cash|mcx explicitly.
ENABLE_CASH_SEGMENT = True
ENABLE_MCX_SEGMENT = True
# Mixed live desk: cash, F&O, and commodity share the five Rs 500 slots.
LIVE_CASH_EQUITIES_ONLY = False

# Cash (NSE) and MCX are separate books. A cash skip, size fail, or cash
# day-loss cap must not block a metal. MCX does not consume the cash risk budget.
RISK_PCT = 0.004
MAX_SINGLE_LOT_RISK_PCT = 0.005
MAX_DAY_LOSS_PCT = 0.02
# Combined desk: flatten everything at this loss (kill switch), or at this profit.
MAX_DAY_LOSS_RS = float(os.getenv("MAX_DAY_LOSS_RS", "3000"))
MAX_DAY_PROFIT_RS = float(os.getenv("MAX_DAY_PROFIT_RS", "5000"))
KILL_SWITCH_RS = MAX_DAY_LOSS_RS
CASH_RISK_PCT = RISK_PCT
CASH_MAX_SINGLE_LOT_RISK_PCT = MAX_SINGLE_LOT_RISK_PCT
CASH_MAX_DAY_LOSS_PCT = MAX_DAY_LOSS_PCT
# Mini metals are 1 lot when the signal/gate passes. Do not apply the cash
# 0.5% one-lot cap to MCX or a cash loss will not be what stops gold/silver.
MCX_LOTS = 1
MCX_MAX_DAY_LOSS_PCT = 0.02
ONE_METAL_AT_A_TIME = True
MCX_REQUIRE_POSITIVE_MOMENTUM = True
COMMODITY_USE_HOURLY_ATR = True
HOURLY_SL_ATR_MULT = 1.25
TRADE_MEMORY_TRADES = 12

# Equity intraday cost model (estimate, not a broker contract).
# Brokerage 0.03% per side, capped at Rs 20. STT on sell. Other = exchange/GST.
BROKERAGE_RATE = 0.0003
BROKERAGE_CAP = 20.0
STT_SELL_RATE = 0.00025
OTHER_CHARGE_RATE = 0.0001

DEEPSEEK_API_KEY = ""

# --- Delta Exchange (BTC options / perp). Separate desk from Dhan. ---
DELTA_API_KEY = ""
DELTA_API_SECRET = ""
DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
DELTA_PAPER = True
DELTA_TRADING_ENABLED = False
DELTA_PAPER_EQUITY = 10000.0
DELTA_EVAL_SECONDS = 1800
DELTA_ENTRY_SCORE_THRESHOLD = 55
DELTA_ATR_MULTIPLIER = 1.5
DELTA_MIN_STRIKE_PCT = 0.008
DELTA_MAX_STRIKE_PCT = 0.08
DELTA_RISK_PCT = 0.01
DELTA_PROFIT_TARGET_PCT = 40.0
DELTA_STOP_LOSS_PCT = 35.0
DELTA_EMERGENCY_PREMIUM_STOP_PCT = 35.0
DELTA_MAX_HOLDING_MINUTES = 720
DELTA_TIME_EXIT_IF_RED_MINUTES = 360
DELTA_FLATTEN_MINUTES_BEFORE_EXPIRY = 60
DELTA_WEEKLY_LOSS_PCT = 0.02
DELTA_MAX_DAILY_LOSS_PCT = 0.03
DELTA_MAX_CONTRACTS = 80
DELTA_SAME_DAY_EXPIRY = True
DELTA_ENABLE_PERP = False
DELTA_ALLOW_SHORT = False
DELTA_LOW_IV = 0.40
DELTA_HIGH_IV_FLAT = 0.50
DELTA_SWING_LEFT = 3
DELTA_SWING_RIGHT = 3
DELTA_STOP_ATR_BUFFER = 0.5
DELTA_STOP_ON_CLOSE = True
DELTA_PERP_STOP_ATR = 1.5
DELTA_PERP_TP_ATR = 2.0
