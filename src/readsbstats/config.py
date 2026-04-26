"""
readsbstats — central configuration.
All tunables can be overridden via environment variables.
"""
import json
import os
import sys


def _clamp_int(name: str, value: int, minimum: int, default: int) -> int:
    """Return *value* if >= *minimum*, else warn and return *default*."""
    if value < minimum:
        print(f"ERROR: {name}={value} is below minimum {minimum}, using default {default}", file=sys.stderr)
        return default
    return value


def _clamp_float(name: str, value: float, minimum: float, default: float) -> float:
    """Return *value* if >= *minimum*, else warn and return *default*."""
    if value < minimum:
        print(f"ERROR: {name}={value} is below minimum {minimum}, using default {default}", file=sys.stderr)
        return default
    return value


def _int(name: str, default: str) -> int:
    val = os.getenv(name, default)
    try:
        return int(val)
    except ValueError:
        print(f"ERROR: {name}={val!r} is not a valid integer, using default {default}", file=sys.stderr)
        return int(default)


def _float(name: str, default: str) -> float:
    val = os.getenv(name, default)
    try:
        return float(val)
    except ValueError:
        print(f"ERROR: {name}={val!r} is not a valid number, using default {default}", file=sys.stderr)
        return float(default)


# ---------------------------------------------------------------------------
# Aircraft flag bitmask constants (shared across all modules)
# ---------------------------------------------------------------------------
FLAG_MILITARY    = 1
FLAG_INTERESTING = 2
FLAG_PIA         = 4
FLAG_LADD        = 8

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
AIRCRAFT_JSON = os.getenv("RSBS_AIRCRAFT_JSON", "/run/readsb/aircraft.json")

# ---------------------------------------------------------------------------
# Collector behaviour
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC  = _int("RSBS_POLL_INTERVAL", "5")
FLIGHT_GAP_SEC     = _int("RSBS_FLIGHT_GAP",    "1800")   # 30 min gap = new flight
MIN_POSITIONS_KEEP = _int("RSBS_MIN_POSITIONS",  "2")      # discard ghost tracks
MAX_SEEN_POS_SEC   = _int("RSBS_MAX_SEEN_POS",   "60")     # skip stale positions
MAX_SPEED_KTS      = _int("RSBS_MAX_SPEED_KTS",  "2000")   # ghost-position filter
MAX_GS_CIVIL_KTS    = _int("RSBS_MAX_GS_CIVIL",      "750")  # null gs above this for civil aircraft
MAX_GS_MILITARY_KTS = _int("RSBS_MAX_GS_MILITARY",  "1800") # null gs above this for military/unknown
MAX_GS_DEVIATION_KTS = _int("RSBS_MAX_GS_DEVIATION", "100")  # null gs when it disagrees with position-derived speed by more than this
MAX_GS_ACCEL_KTS_S   = _float("RSBS_MAX_GS_ACCEL",     "8.0")  # null MLAT gs when acceleration exceeds this (kts/s)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH            = os.getenv("RSBS_DB_PATH",         "/mnt/ext/readsbstats/history.db")
RETENTION_DAYS     = _int("RSBS_RETENTION_DAYS", "0")      # 0 = keep forever
PURGE_INTERVAL_SEC = _int("RSBS_PURGE_INTERVAL", "3600")

# ---------------------------------------------------------------------------
# Receiver location
# ---------------------------------------------------------------------------
RECEIVER_LAT       = _float("RSBS_LAT",       "52.24199")
RECEIVER_LON       = _float("RSBS_LON",       "21.02872")
RECEIVER_MAX_RANGE = _int("RSBS_MAX_RANGE",    "450")      # nmi

# ---------------------------------------------------------------------------
# Enrichment / photo cache
# ---------------------------------------------------------------------------
PHOTO_CACHE_DAYS      = _int("RSBS_PHOTO_CACHE_DAYS",    "30")
AIRSPACE_GEOJSON      = os.getenv("RSBS_AIRSPACE_GEOJSON",        "")      # empty = use bundled poland.geojson
ROUTE_CACHE_DAYS      = _int("RSBS_ROUTE_CACHE_DAYS",    "30")
ROUTE_ENRICH_INTERVAL = _int("RSBS_ROUTE_INTERVAL",      "60")    # seconds between batch runs
ROUTE_BATCH_SIZE      = _int("RSBS_ROUTE_BATCH",         "20")    # callsigns per batch
ROUTE_RATE_LIMIT_SEC  = _float("RSBS_ROUTE_RATE_LIMIT",  "1.0")  # seconds between API calls

# ---------------------------------------------------------------------------
# External ADS-B enrichment (airplanes.live — free, no auth)
# ---------------------------------------------------------------------------
ADSBX_ENABLED       = os.getenv("RSBS_ADSBX_ENABLED", "1") not in ("0", "false", "no", "")
ADSBX_POLL_INTERVAL = _int("RSBS_ADSBX_INTERVAL", "60")       # seconds between area polls
ADSBX_RANGE_NM      = _int("RSBS_ADSBX_RANGE",    "250")      # radius in nautical miles
ADSBX_API_URL       = os.getenv("RSBS_ADSBX_URL",
                                "https://api.airplanes.live/v2")

# ---------------------------------------------------------------------------
# Receiver metrics (metrics_collector) — disabled by default
# ---------------------------------------------------------------------------
METRICS_ENABLED  = os.getenv("RSBS_METRICS_ENABLED", "") not in ("", "0", "false", "no")
METRICS_INTERVAL = _int("RSBS_METRICS_INTERVAL", "60")
STATS_JSON       = os.getenv("RSBS_STATS_JSON", "/run/readsb/stats.json")

# ---------------------------------------------------------------------------
# Receiver health dashboard (rule-based checks over receiver_stats)
# ---------------------------------------------------------------------------
HEALTH_HEARTBEAT_CRIT_S = _int("RSBS_HEALTH_HEARTBEAT_CRIT_S", "300")  # no metrics row in 5 min
HEALTH_HEARTBEAT_WARN_S = _int("RSBS_HEALTH_HEARTBEAT_WARN_S", "120")  # last row 2+ min old
HEALTH_AIRCRAFT_GAP_S   = _int("RSBS_HEALTH_AIRCRAFT_GAP_S",   "600")  # 0 aircraft for 10 min => critical
HEALTH_NOISE_CRIT_DB    = _float("RSBS_HEALTH_NOISE_CRIT_DB",  "-25")  # dBFS, higher is worse
HEALTH_NOISE_WARN_DB    = _float("RSBS_HEALTH_NOISE_WARN_DB",  "-28")
HEALTH_CPU_CRIT_PCT     = _float("RSBS_HEALTH_CPU_CRIT_PCT",   "90")   # demod % of one core
HEALTH_CPU_WARN_PCT     = _float("RSBS_HEALTH_CPU_WARN_PCT",   "80")
# Phase 2 — baseline-aware checks (same hour-of-week, prior weeks)
HEALTH_BASELINE_WEEKS       = _int("RSBS_HEALTH_BASELINE_WEEKS",       "4")
HEALTH_BASELINE_MIN_SAMPLES = _int("RSBS_HEALTH_BASELINE_MIN_SAMPLES", "3")
HEALTH_MSG_DROP_PCT         = _float("RSBS_HEALTH_MSG_DROP_PCT",      "50")  # warn below this % of baseline
HEALTH_AIRCRAFT_DROP_PCT    = _float("RSBS_HEALTH_AIRCRAFT_DROP_PCT", "25")
HEALTH_SIGNAL_DROP_DB       = _float("RSBS_HEALTH_SIGNAL_DROP_DB",    "3")   # warn if signal drops this many dB below baseline
# Phase 3 — gain hints
HEALTH_GAIN_STRONG_PCT  = _float("RSBS_HEALTH_GAIN_STRONG_PCT",  "5")   # warn above this % strong-signals/messages
HEALTH_RANGE_SHORT_DAYS = _int("RSBS_HEALTH_RANGE_SHORT_DAYS",   "7")
HEALTH_RANGE_LONG_DAYS  = _int("RSBS_HEALTH_RANGE_LONG_DAYS",   "30")
HEALTH_RANGE_RATIO      = _float("RSBS_HEALTH_RANGE_RATIO",     "0.85") # info if 7d max < 30d max × this

# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
WEB_HOST           = os.getenv("RSBS_WEB_HOST",  "0.0.0.0")
WEB_PORT           = _int("RSBS_WEB_PORT", "8080")
ROOT_PATH          = os.getenv("RSBS_ROOT_PATH", "/stats")          # reverse-proxy subpath

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE  = _int("RSBS_PAGE_SIZE",     "100")
MAX_PAGE_SIZE      = _int("RSBS_MAX_PAGE_SIZE", "500")
MAX_EXPORT_ROWS    = _int("RSBS_MAX_EXPORT",    "50000")

# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN        = os.getenv("RSBS_TELEGRAM_TOKEN",    "")
TELEGRAM_CHAT_ID      = os.getenv("RSBS_TELEGRAM_CHAT_ID",  "")
TELEGRAM_SUMMARY_TIME = os.getenv("RSBS_SUMMARY_TIME",      "21:00")  # local HH:MM; "" or "off" to disable
TELEGRAM_UNITS        = os.getenv("RSBS_TELEGRAM_UNITS",    "metric") # metric|imperial|aeronautical
BASE_URL              = os.getenv("RSBS_BASE_URL", "http://homepi.local/stats")

# ---------------------------------------------------------------------------
# Feeders health monitoring
# ---------------------------------------------------------------------------
_DEFAULT_FEEDERS = [
    {"name": "readsb",                "unit": "readsb.service",                  "port": 30005,
     "status_type": "readsb", "status_path": "/run/readsb"},
    {"name": "FR24 feeder",           "unit": "fr24feed.service",                "port": 8754,
     "status_type": "fr24",   "status_url": "http://127.0.0.1:8754/monitor.json"},
    {"name": "PiAware",               "unit": "piaware.service",
     "status_type": "piaware", "status_path": "/run/piaware/status.json"},
    {"name": "ADSBexchange feed",     "unit": "adsbexchange-feed.service",
     "status_type": "readsb", "status_path": "/run/adsbexchange-feed"},
    {"name": "ADSBexchange MLAT",    "unit": "adsbexchange-mlat.service",
     "status_type": "mlat"},
    {"name": "airplanes.live feed",   "unit": "airplanes-feed.service",
     "status_type": "readsb", "status_path": "/run/airplanes-feed"},
    {"name": "airplanes.live MLAT",   "unit": "airplanes-mlat.service",
     "status_type": "mlat"},
    {"name": "readsbstats collector", "unit": "readsbstats-collector.service"},
    {"name": "readsbstats web",       "unit": "readsbstats-web.service",         "port": 8080},
]


def _parse_feeders(raw: str) -> list[dict]:
    if not raw.strip():
        return _DEFAULT_FEEDERS
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("RSBS_FEEDERS must be a JSON array")
        for item in parsed:
            if "name" not in item or "unit" not in item:
                raise ValueError(f"Each feeder needs 'name' and 'unit': {item}")
        return parsed
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: RSBS_FEEDERS: {exc}, using defaults", file=sys.stderr)
        return _DEFAULT_FEEDERS


FEEDERS = _parse_feeders(os.getenv("RSBS_FEEDERS", ""))

# ---------------------------------------------------------------------------
# Range validation — clamp values that would cause busy loops, data loss,
# or broken filtering.  Warn to stderr and fall back to defaults.
# ---------------------------------------------------------------------------

# Intervals used in sleep() — zero causes infinite busy loops
POLL_INTERVAL_SEC    = _clamp_int("RSBS_POLL_INTERVAL",  POLL_INTERVAL_SEC,    1, 5)
FLIGHT_GAP_SEC       = _clamp_int("RSBS_FLIGHT_GAP",     FLIGHT_GAP_SEC,       1, 1800)
PURGE_INTERVAL_SEC   = _clamp_int("RSBS_PURGE_INTERVAL", PURGE_INTERVAL_SEC,   1, 3600)
ROUTE_ENRICH_INTERVAL = _clamp_int("RSBS_ROUTE_INTERVAL", ROUTE_ENRICH_INTERVAL, 1, 60)
ADSBX_POLL_INTERVAL  = _clamp_int("RSBS_ADSBX_INTERVAL", ADSBX_POLL_INTERVAL,  1, 60)
METRICS_INTERVAL     = _clamp_int("RSBS_METRICS_INTERVAL", METRICS_INTERVAL, 10, 60)
HEALTH_HEARTBEAT_CRIT_S = _clamp_int("RSBS_HEALTH_HEARTBEAT_CRIT_S", HEALTH_HEARTBEAT_CRIT_S, 30, 300)
HEALTH_HEARTBEAT_WARN_S = _clamp_int("RSBS_HEALTH_HEARTBEAT_WARN_S", HEALTH_HEARTBEAT_WARN_S, 30, 120)
HEALTH_AIRCRAFT_GAP_S   = _clamp_int("RSBS_HEALTH_AIRCRAFT_GAP_S",   HEALTH_AIRCRAFT_GAP_S,   60, 600)
HEALTH_CPU_CRIT_PCT     = _clamp_float("RSBS_HEALTH_CPU_CRIT_PCT",   HEALTH_CPU_CRIT_PCT,    1.0, 90.0)
HEALTH_CPU_WARN_PCT     = _clamp_float("RSBS_HEALTH_CPU_WARN_PCT",   HEALTH_CPU_WARN_PCT,    1.0, 80.0)
HEALTH_BASELINE_WEEKS       = _clamp_int("RSBS_HEALTH_BASELINE_WEEKS",       HEALTH_BASELINE_WEEKS,       1, 4)
HEALTH_BASELINE_MIN_SAMPLES = _clamp_int("RSBS_HEALTH_BASELINE_MIN_SAMPLES", HEALTH_BASELINE_MIN_SAMPLES, 1, 3)
HEALTH_MSG_DROP_PCT         = _clamp_float("RSBS_HEALTH_MSG_DROP_PCT",      HEALTH_MSG_DROP_PCT,      1.0, 50.0)
HEALTH_AIRCRAFT_DROP_PCT    = _clamp_float("RSBS_HEALTH_AIRCRAFT_DROP_PCT", HEALTH_AIRCRAFT_DROP_PCT, 1.0, 25.0)
HEALTH_SIGNAL_DROP_DB       = _clamp_float("RSBS_HEALTH_SIGNAL_DROP_DB",    HEALTH_SIGNAL_DROP_DB,    0.1, 3.0)
HEALTH_GAIN_STRONG_PCT      = _clamp_float("RSBS_HEALTH_GAIN_STRONG_PCT",   HEALTH_GAIN_STRONG_PCT,   0.1, 5.0)
HEALTH_RANGE_SHORT_DAYS     = _clamp_int("RSBS_HEALTH_RANGE_SHORT_DAYS",    HEALTH_RANGE_SHORT_DAYS,  1, 7)
HEALTH_RANGE_LONG_DAYS      = _clamp_int("RSBS_HEALTH_RANGE_LONG_DAYS",     HEALTH_RANGE_LONG_DAYS,   1, 30)
HEALTH_RANGE_RATIO          = _clamp_float("RSBS_HEALTH_RANGE_RATIO",       HEALTH_RANGE_RATIO,       0.1, 0.85)

# Thresholds — zero would reject all positions or delete valid flights
MIN_POSITIONS_KEEP   = _clamp_int("RSBS_MIN_POSITIONS",  MIN_POSITIONS_KEEP,   1, 2)
MAX_SEEN_POS_SEC     = _clamp_int("RSBS_MAX_SEEN_POS",   MAX_SEEN_POS_SEC,     1, 60)
RECEIVER_MAX_RANGE   = _clamp_int("RSBS_MAX_RANGE",      RECEIVER_MAX_RANGE,   1, 450)
MAX_SPEED_KTS        = _clamp_int("RSBS_MAX_SPEED_KTS",  MAX_SPEED_KTS,        1, 2000)
MAX_GS_CIVIL_KTS     = _clamp_int("RSBS_MAX_GS_CIVIL",   MAX_GS_CIVIL_KTS,    1, 750)
MAX_GS_MILITARY_KTS  = _clamp_int("RSBS_MAX_GS_MILITARY", MAX_GS_MILITARY_KTS, 1, 1800)
MAX_GS_DEVIATION_KTS = _clamp_int("RSBS_MAX_GS_DEVIATION", MAX_GS_DEVIATION_KTS, 1, 100)
MAX_GS_ACCEL_KTS_S   = _clamp_float("RSBS_MAX_GS_ACCEL", MAX_GS_ACCEL_KTS_S,  0.1, 8.0)
ADSBX_RANGE_NM       = _clamp_int("RSBS_ADSBX_RANGE",   ADSBX_RANGE_NM,      1, 250)
ROUTE_BATCH_SIZE     = _clamp_int("RSBS_ROUTE_BATCH",   ROUTE_BATCH_SIZE,     1, 20)

# Pagination — zero causes FastAPI validation conflicts (ge=1, le=0)
MAX_PAGE_SIZE        = _clamp_int("RSBS_MAX_PAGE_SIZE",  MAX_PAGE_SIZE,        1, 500)
DEFAULT_PAGE_SIZE    = _clamp_int("RSBS_PAGE_SIZE",      DEFAULT_PAGE_SIZE,    1, 100)
MAX_EXPORT_ROWS      = _clamp_int("RSBS_MAX_EXPORT",    MAX_EXPORT_ROWS,      1, 50000)

# DEFAULT_PAGE_SIZE must not exceed MAX_PAGE_SIZE
if DEFAULT_PAGE_SIZE > MAX_PAGE_SIZE:
    print(
        f"ERROR: RSBS_PAGE_SIZE={DEFAULT_PAGE_SIZE} exceeds "
        f"RSBS_MAX_PAGE_SIZE={MAX_PAGE_SIZE}, clamping to {MAX_PAGE_SIZE}",
        file=sys.stderr,
    )
    DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE

# ---------------------------------------------------------------------------
# String normalization — strip trailing slashes to prevent double-slash URLs
# ---------------------------------------------------------------------------
ROOT_PATH     = ROOT_PATH.rstrip("/")    if ROOT_PATH     else ROOT_PATH
BASE_URL      = BASE_URL.rstrip("/")     if BASE_URL      else BASE_URL
ADSBX_API_URL = ADSBX_API_URL.rstrip("/") if ADSBX_API_URL else ADSBX_API_URL

# DB_PATH — empty string causes sqlite3.connect("") which is an in-memory DB
# that silently loses all data on restart
_DB_PATH_DEFAULT = "/mnt/ext/readsbstats/history.db"
if not DB_PATH.strip():
    print(f"ERROR: RSBS_DB_PATH is empty, using default {_DB_PATH_DEFAULT}",
          file=sys.stderr)
    DB_PATH = _DB_PATH_DEFAULT
