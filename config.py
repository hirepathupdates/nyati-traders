# =============================================================================
# config.py  —  Nyati Traders | Angel One SmartAPI Configuration
# =============================================================================
# Fill in your Angel One credentials before running the application.
# Never commit this file to a public repository.
# =============================================================================

# ---------------------------------------------------------------------------
# Angel One API Credentials
# ---------------------------------------------------------------------------
API_KEY     = "OAcMk5E4"       # Developer Portal → My Apps
CLIENT_ID   = "AACG264474"     # e.g. A123456
PASSWORD    = "3998"            # Account login password / PIN
TOTP_SECRET = "4DBYUF3XSSCMK76CLZHZDKWKXM"  # Base32 secret shown during 2-FA setup

# ---------------------------------------------------------------------------
# Instrument Master  (Angel One publishes this publicly)
# ---------------------------------------------------------------------------
INSTRUMENT_URL        = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)
INSTRUMENT_CACHE_FILE = "instruments_cache.json"   # local disk cache

# ---------------------------------------------------------------------------
# Data / Feed Settings
# ---------------------------------------------------------------------------
EXCHANGE          = "NSE"           # Exchange identifier
CANDLE_INTERVAL   = "ONE_MINUTE"    # Angel One interval string
CANDLE_LIMIT      = 100             # Max candles to display on chart
REFRESH_INTERVAL  = 5               # Seconds between live data refreshes

# ---------------------------------------------------------------------------
# Strategy Parameters
# ---------------------------------------------------------------------------
SWING_LOOKBACK    = 2               # Candles on each side for swing detection
SR_CANDLE_WINDOW  = 50              # Look-back window (candles) for S/R
SR_MAX_LEVELS     = 5               # Maximum S/R lines to draw per side
PROXIMITY_PCT     = 0.005           # ±0.5 % proximity → triggers a signal

# ---------------------------------------------------------------------------
# Trading / Risk Parameters
# ---------------------------------------------------------------------------
# Intraday margin is estimated at 25 % of the stock price (NRML/MIS approx).
# The system uses only 90 % of available funds as a safety buffer.
INTRADAY_MARGIN_PCT = 0.25          # estimated MIS margin as % of stock price
FUNDS_USAGE_PCT     = 0.90          # use 90 % of available cash (safety buffer)
STOP_LOSS_PCT       = 0.010         # 1.0 % stop-loss below entry
TARGET_PCT          = 0.015         # 1.5 % profit target above entry

# ---------------------------------------------------------------------------
# Funds / Margin Refresh
# ---------------------------------------------------------------------------
FUNDS_INTERVAL    = 12              # Seconds between account-funds refreshes
