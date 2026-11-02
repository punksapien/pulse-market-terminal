"""ADR-0030: unchanged legacy keys, windows, limits and constants."""
UI_COMBINATIONS = (("60", "1H"), ("240", "4H"), ("1D", "24H"))
# The product exposes exactly one horizon per timeframe. Keeping the API
# whitelist identical prevents accidental cache/report cross-wiring.
ALLOWED_COMBINATIONS = set(UI_COMBINATIONS)
REPORT_SCHEMA_VERSION = "2"
MODEL_VERSION = "volatility-lightweight-v2"
GENERATION_KEY_PREFIX = "vol:v2:gen"
ACTIVE_GENERATION_KEY = "vol:v2:active"
PREVIOUS_GENERATION_KEY = "vol:v2:previous"
LAST_GOOD_GENERATION_KEY = "vol:v2:last_good"
LAST_REFRESH_STATUS_KEY = "vol:v2:refresh:status"
REFRESH_BACKOFF_KEY = "vol:v2:refresh:backoff"
SYNC_LOCK_KEY = "vol:v2:lock:sync"
SYNC_LOCK_TTL = 600
GENERATION_TTL = 7 * 24 * 60 * 60
SIGNAL_STATE_KEY_PREFIX = "vol:v2:signal-state"
SIGNAL_STATE_TTL = 30 * 24 * 60 * 60
TRACKED_TRADE_SIGNALS = frozenset({
    "MomentumLong",
    "MomentumShort",
    "ReversalLong",
    "ReversalShort",
    "BreakoutWatch",
    "Range",
    "Avoid",
})
DISPLAYABLE_SIGNAL_STATES = frozenset({
    "MomentumLong",
    "MomentumShort",
    "BreakoutWatch",
    "Range",
})

# Production freshness contract. The watchdog checks every 15 minutes, rebuilds
# after 50 minutes, and retries at the next tick if Vercel or an upstream fails.
# A report older than 75 minutes triggers request-side healing. Reports older
# than three hours are never returned as successful production data.
GENERATION_REFRESH_AFTER = 50 * 60
REPORT_REFRESH_AFTER = 75 * 60
REPORT_MAX_STALE = 3 * 60 * 60
REFRESH_FAILURE_BACKOFF = 5 * 60
MIN_GENERATED_ROWS = 100
MAX_UNEXPECTED_ERROR_RATIO = 0.10
MIN_PREVIOUS_ROW_RATIO = 0.70

CACHE_TTL = 7200  # 2 hours
OI_CACHE_TTL = 300  # 5 minutes
LOCK_KEY_PREFIX = "vol_lock_"
LOCK_TTL = 180  # 3 minutes
