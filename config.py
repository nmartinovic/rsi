import os

# Pushover (from environment / GitHub Secrets)
PUSHOVER_APP_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")

# RSI parameters
RSI_PERIOD = 14             # Standard 14-period RSI
RSI_THRESHOLD = 30          # Alert when Confirmed RSI below this
RSI_LOOKBACK = "6mo"        # Daily data lookback (gives ~25 rolling weeks)
ROLLING_WEEK_DAYS = 5       # Trading days per rolling weekly candle

# LEAP filter
MIN_DTE_DAYS = 360          # Minimum days to expiration for LEAP qualification

# Rate limiting
RSI_BATCH_SIZE = 50                 # Tickers per yf.download() bulk call
RSI_BATCH_DELAY = 5                 # Seconds between bulk download batches
LEAP_REQUEST_DELAY = 2.5            # Seconds between .options calls
BACKOFF_INITIAL = 30                # First backoff wait on 429 (seconds)
BACKOFF_MULTIPLIER = 2              # Double wait each consecutive 429
BACKOFF_MAX_RETRIES = 5             # Max consecutive 429s before exiting

# Paths
SP1500_UNIVERSE_PATH = "data/sp1500_universe.json"
RSI_VALUES_PATH = "data/rsi_values.json"
OVERSOLD_PATH = "data/oversold.json"
ALERT_STATE_PATH = "data/alert_state.json"
SYSTEM_LOG_PATH = "data/system_log.json"
DASHBOARD_DATA_PATH = "docs/data/dashboard.json"
