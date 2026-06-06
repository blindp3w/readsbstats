"""
readsbstats — central configuration.
All tunables can be overridden via environment variables.
"""
import json
import os
import sys

from . import cleaners


def _min_or_default_int(name: str, value: int, minimum: int, default: int) -> int:
    """Return *value* unchanged when >= *minimum*; else warn and return *default*.

    Audit-12 #196 renamed this from `_clamp_int` because "clamp" implied a
    two-sided range. It only enforces the lower bound — values larger than
    *default* pass through untouched.
    """
    if value < minimum:
        print(f"ERROR: {name}={value} is below minimum {minimum}, using default {default}", file=sys.stderr)
        return default
    return value


def _min_or_default_float(name: str, value: float, minimum: float, default: float) -> float:
    """Float twin of :func:`_min_or_default_int`. See its docstring for the
    "min-or-default" semantics — *value* is returned unchanged when above
    the lower bound regardless of how it compares to *default*."""
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


# Falsy values for a boolean env var. Empty string is included so that
# `RSBS_FOO=` (assigned but blank) means "off", matching most operators'
# mental model that unsetting and blanking are equivalent.
_BOOL_FALSY = frozenset({"", "0", "false", "no", "off"})


def _bool(name: str, default: bool) -> bool:
    """Parse a boolean env var. Returns `default` when the var is unset
    and the operator hasn't provided any value at all.

    Audit-12 #197 — replaces five copies of
    `os.getenv(...).lower() not in ("0", "false", "no", "")` that had
    drifted in their tuple ordering and empty-string handling.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _BOOL_FALSY


# ---------------------------------------------------------------------------
# Settings metadata registry — drift defence for /api/settings
# ---------------------------------------------------------------------------
# `_register(payload_key, env_var, default, config_attr)` records the
# env-var name and default at the same call site that reads the env var,
# so the frontend can render the env-var name and a "(default)" indicator
# without ever maintaining its own table that could drift. Returns
# (env_var, default) as a tuple so it can be splatted into _int / _float /
# _bool / os.getenv:
#
#   RECEIVER_LAT = _float(*_register("lat", "RSBS_LAT", "52.24199", "RECEIVER_LAT"))
#
# Removing the `_register(...)` removes the env-var name passed to the
# parser, so the failure mode is "the line stops compiling" rather than
# "the metadata silently drifts". Only settings shipped by /api/settings
# need registration; purely internal tunables (DuckDB, MLAT outlier
# filter, etc.) are left untouched.
_META_REGISTRY: dict[str, dict] = {}


def _register(payload_key: str, env_var: str, default, config_attr: str,
              *, secret: bool = False):
    """Record `payload_key → (env_var, default, config_attr, secret)` and
    return (env_var, default) for splatting into the parser call.

    `secret=True` marks keys whose default is a filesystem path or other
    sensitive string. The metadata builder masks the default for these so
    the path doesn't leak through /api/settings — matches the existing
    masking applied to the payload value itself.

    Raises if the same payload key is registered twice — a load-time check
    that catches copy-paste mistakes immediately.
    """
    if payload_key in _META_REGISTRY:
        raise RuntimeError(
            f"settings registry collision: {payload_key!r} registered twice"
        )
    _META_REGISTRY[payload_key] = {
        "env_var":     env_var,
        "default":     default,
        "config_attr": config_attr,
        "secret":      secret,
    }
    return env_var, default


# ---------------------------------------------------------------------------
# Aircraft flag bitmask constants (shared across all modules)
# ---------------------------------------------------------------------------
FLAG_MILITARY    = 1
FLAG_INTERESTING = 2
FLAG_PIA         = 4
FLAG_LADD        = 8
FLAG_ANONYMOUS   = 16    # icao_hex falls outside every ICAO state-allocated block

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
AIRCRAFT_JSON = os.getenv("RSBS_AIRCRAFT_JSON", "/run/readsb/aircraft.json")

# ---------------------------------------------------------------------------
# Collector behaviour
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC  = _int(*_register("poll_interval", "RSBS_POLL_INTERVAL", "5",    "POLL_INTERVAL_SEC"))
FLIGHT_GAP_SEC     = _int(*_register("flight_gap",    "RSBS_FLIGHT_GAP",    "1800", "FLIGHT_GAP_SEC"))   # 30 min gap = new flight
MIN_POSITIONS_KEEP = _int(*_register("min_positions", "RSBS_MIN_POSITIONS", "2",    "MIN_POSITIONS_KEEP"))  # discard ghost tracks
MAX_SEEN_POS_SEC   = _int(*_register("max_seen_pos",  "RSBS_MAX_SEEN_POS",  "60",   "MAX_SEEN_POS_SEC"))    # skip stale positions
MAX_SPEED_KTS      = _int(*_register("max_speed_kts", "RSBS_MAX_SPEED_KTS", "2000", "MAX_SPEED_KTS"))   # ghost-position filter
MAX_GS_CIVIL_KTS    = _int("RSBS_MAX_GS_CIVIL",      "750")  # null gs above this for civil aircraft
MAX_GS_MILITARY_KTS = _int("RSBS_MAX_GS_MILITARY",  "1800") # null gs above this for military/unknown
MAX_GS_DEVIATION_KTS = _int("RSBS_MAX_GS_DEVIATION", "100")  # null gs when it disagrees with position-derived speed by more than this
MAX_GS_ACCEL_KTS_S       = _float("RSBS_MAX_GS_ACCEL",          "8.0")   # null MLAT gs when acceleration exceeds this (kts/s)
MLAT_OUTLIER_FACTOR      = _float("RSBS_MLAT_OUTLIER_FACTOR",   "5.0")   # null MLAT gs > this × p75 of flight's gs values
MLAT_OUTLIER_MIN_READINGS = _int("RSBS_MLAT_OUTLIER_MIN",       "10")    # minimum MLAT gs readings required to apply outlier filter

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH            = os.getenv(*_register("db_path",        "RSBS_DB_PATH",         "/mnt/ext/readsbstats/history.db", "DB_PATH", secret=True))
RETENTION_DAYS     = _int(*_register("retention_days", "RSBS_RETENTION_DAYS",  "0",    "RETENTION_DAYS"))      # 0 = keep forever
PURGE_INTERVAL_SEC = _int(*_register("purge_interval", "RSBS_PURGE_INTERVAL",  "3600", "PURGE_INTERVAL_SEC"))

# ---------------------------------------------------------------------------
# Receiver location
# ---------------------------------------------------------------------------
RECEIVER_LAT       = _float(*_register("lat",       "RSBS_LAT",       "52.24199", "RECEIVER_LAT"))
RECEIVER_LON       = _float(*_register("lon",       "RSBS_LON",       "21.02872", "RECEIVER_LON"))
RECEIVER_MAX_RANGE = _int(*_register("max_range",   "RSBS_MAX_RANGE", "450",      "RECEIVER_MAX_RANGE"))      # nmi

# ---------------------------------------------------------------------------
# Enrichment / photo cache
# ---------------------------------------------------------------------------
PHOTO_CACHE_DAYS      = _int(*_register("photo_cache_days", "RSBS_PHOTO_CACHE_DAYS",    "30",  "PHOTO_CACHE_DAYS"))
WIKIPEDIA_PHOTO       = _bool("RSBS_WIKIPEDIA_PHOTO", default=True)   # type-photo fallback via Wikipedia REST API
PHOTO_HOST_ENFORCE    = _bool("RSBS_PHOTO_HOST_ENFORCE", default=False)  # BE-17: drop provider photo URLs off the CDN allowlist (default log-only)
AIRSPACE_GEOJSON      = os.getenv(*_register("airspace_geojson", "RSBS_AIRSPACE_GEOJSON", "", "AIRSPACE_GEOJSON"))      # empty = use bundled poland.geojson
ROUTE_CACHE_DAYS      = _int(*_register("route_cache_days", "RSBS_ROUTE_CACHE_DAYS",    "30",  "ROUTE_CACHE_DAYS"))
ROUTE_ENRICH_INTERVAL = _int(*_register("route_interval",   "RSBS_ROUTE_INTERVAL",      "60",  "ROUTE_ENRICH_INTERVAL"))    # seconds between batch runs
ROUTE_BATCH_SIZE      = _int(*_register("route_batch",      "RSBS_ROUTE_BATCH",         "20",  "ROUTE_BATCH_SIZE"))    # callsigns per batch
ROUTE_RATE_LIMIT_SEC  = _float(*_register("route_rate_limit", "RSBS_ROUTE_RATE_LIMIT",  "1.0", "ROUTE_RATE_LIMIT_SEC"))  # seconds between API calls

# ---------------------------------------------------------------------------
# External ADS-B enrichment (airplanes.live — free, no auth)
# ---------------------------------------------------------------------------
ADSBX_ENABLED       = _bool(*_register("adsbx_enabled",  "RSBS_ADSBX_ENABLED", True, "ADSBX_ENABLED"))
ADSBX_POLL_INTERVAL = _int(*_register("adsbx_interval",  "RSBS_ADSBX_INTERVAL", "60",  "ADSBX_POLL_INTERVAL"))       # seconds between area polls
ADSBX_RANGE_NM      = _int(*_register("adsbx_range",     "RSBS_ADSBX_RANGE",    "250", "ADSBX_RANGE_NM"))      # radius in nautical miles
ADSBX_API_URL       = os.getenv(*_register("adsbx_url",  "RSBS_ADSBX_URL",
                                "https://api.airplanes.live/v2", "ADSBX_API_URL"))

# ---------------------------------------------------------------------------
# Receiver metrics (metrics_collector) — disabled by default
# ---------------------------------------------------------------------------
METRICS_ENABLED  = _bool(*_register("metrics_enabled",  "RSBS_METRICS_ENABLED", False, "METRICS_ENABLED"))
METRICS_INTERVAL = _int(*_register("metrics_interval",  "RSBS_METRICS_INTERVAL", "60", "METRICS_INTERVAL"))
STATS_JSON       = os.getenv(*_register("stats_json",   "RSBS_STATS_JSON", "/run/readsb/stats.json", "STATS_JSON", secret=True))

# ---------------------------------------------------------------------------
# DuckDB analytical accelerator (web process only) — disabled by default
# ---------------------------------------------------------------------------
USE_DUCKDB        = _bool("RSBS_USE_DUCKDB", default=False)
DUCKDB_MEMORY_MB  = _min_or_default_int("RSBS_DUCKDB_MEMORY_MB",
                               _int("RSBS_DUCKDB_MEMORY_MB", "256"), 64, 256)
DUCKDB_THREADS    = _min_or_default_int("RSBS_DUCKDB_THREADS",
                               _int("RSBS_DUCKDB_THREADS", "2"), 1, 2)
DUCKDB_TEMP_DIR   = os.getenv("RSBS_DUCKDB_TEMP_DIR",
                              "/mnt/ext/readsbstats/duckdb-tmp")
# `readsbstats` is a system user with no /home — DuckDB needs an explicit
# home for its extension cache. Lives next to the DB on /mnt/ext (already
# writable for this user, survives across deploys, doesn't clutter /opt).
DUCKDB_HOME_DIR   = os.getenv("RSBS_DUCKDB_HOME_DIR",
                              "/mnt/ext/readsbstats/duckdb-home")

# ---------------------------------------------------------------------------
# VDL2 / ACARS (opt-in, SEPARATE DB) — disabled by default
# ---------------------------------------------------------------------------
# Optional feature: ingest VDL Mode 2 / ACARS messages decoded by an external
# decoder (vdlm2dec, consume-only) into a SEPARATE SQLite DB. The core app is
# fully unaffected when RSBS_VDL2_ENABLED is false — no router, no nav item,
# no ingest, no schema creation. history.db is never touched.
# See internal_docs/features/vdl2-research.md + vdl2-ui-integration.md.
VDL2_ENABLED        = _bool(*_register("vdl2_enabled",  "RSBS_VDL2_ENABLED", False, "VDL2_ENABLED"))
VDL2_DB_PATH        = os.getenv(*_register("vdl2_db_path", "RSBS_VDL2_DB_PATH",
                                "/mnt/ext/readsbstats/vdl2.db", "VDL2_DB_PATH", secret=True))
VDL2_RETENTION_DAYS = _int(*_register("vdl2_retention", "RSBS_VDL2_RETENTION_DAYS", "90", "VDL2_RETENTION_DAYS"))  # 0 = keep forever
# Internal tunables (not surfaced via /api/settings). Range-validated per the
# project rule (parse via _int, floor via _min_or_default_int).
VDL2_UDP_HOST       = os.getenv("RSBS_VDL2_UDP_HOST", "127.0.0.1")   # bind localhost; decoder feeds line-delimited JSON here
VDL2_UDP_PORT       = _min_or_default_int("RSBS_VDL2_UDP_PORT", _int("RSBS_VDL2_UDP_PORT", "5556"), 1, 5556)
# Decoder whose JSON dialect the ingest normalizer expects. vdlm2dec is the
# working decoder for the Airspy Mini; dumpvdl2 is the documented future swap.
_VDL2_DECODERS      = ("vdlm2dec", "dumpvdl2")
_VDL2_DECODER_RAW   = os.getenv("RSBS_VDL2_DECODER", "vdlm2dec").strip().lower()
if _VDL2_DECODER_RAW not in _VDL2_DECODERS:
    print(f"ERROR: RSBS_VDL2_DECODER={_VDL2_DECODER_RAW!r} not in {_VDL2_DECODERS}, using 'vdlm2dec'", file=sys.stderr)
VDL2_DECODER        = _VDL2_DECODER_RAW if _VDL2_DECODER_RAW in _VDL2_DECODERS else "vdlm2dec"
VDL2_PURGE_INTERVAL_SEC = _min_or_default_int("RSBS_VDL2_PURGE_INTERVAL", _int("RSBS_VDL2_PURGE_INTERVAL", "3600"), 60, 3600)
# Floor at 256 so a typo'd 0/negative can't silently store empty bodies (which
# would make every message body blank and FTS search useless).
VDL2_BODY_MAX       = _min_or_default_int("RSBS_VDL2_BODY_MAX", _int("RSBS_VDL2_BODY_MAX", "4096"), 256, 4096)
# Cap on the stored verbatim `raw` decoder JSON. Bounded by the UDP datagram size
# already, but caps per-row growth against a hostile/misconfigured local sender.
VDL2_RAW_MAX        = _min_or_default_int("RSBS_VDL2_RAW_MAX", _int("RSBS_VDL2_RAW_MAX", "8192"), 256, 8192)

# Audit 2026-05-26: minimum fraction of the previous aircraft_db row count
# that a freshly-imported tar1090-db CSV must contain before the swap is
# allowed. Protects against truncated upstream downloads (a successful 200
# OK with a half-streamed body) silently wiping most of the local cache.
# 0.8 = refuse a swap that loses >20% of rows compared to last successful
# import. First-ever imports (prev_count == 0) bypass the check.
AIRCRAFT_DB_MIN_RATIO = _min_or_default_float(
    "RSBS_AIRCRAFT_DB_MIN_RATIO",
    _float("RSBS_AIRCRAFT_DB_MIN_RATIO", "0.8"),
    0.0, 0.8,
)
# PY-7 (Audit 2026-05-31): same min-ratio guard for the airlines updater.
# Default 0.8 mirrors aircraft_db. OpenFlights is more volatile than
# tar1090-db (airlines disappear, new entries added) so the threshold
# could conceivably be loosened via env var, but the floor here is
# truncation protection — a sudden 50%-row drop is upstream corruption
# regardless of OpenFlights churn.
AIRLINES_DB_MIN_RATIO = _min_or_default_float(
    "RSBS_AIRLINES_DB_MIN_RATIO",
    _float("RSBS_AIRLINES_DB_MIN_RATIO", "0.8"),
    0.0, 0.8,
)
# Code-review follow-up: maximum age (days) before adsbx_overrides rows are
# eligible for deletion by the weekly db_updater. The UPSERT clause in
# adsbx_enricher._upsert_overrides preserves confirmed metadata across
# transient upstream gaps; this purge clears genuinely-stale rows so an
# airframe whose registration has actually been removed doesn't keep
# surfacing the old value forever. 0 disables the purge. Default 365
# days = 1 year of un-seen-ness, generous enough for airframes that pass
# overhead only occasionally.
ADSBX_OVERRIDES_TTL_DAYS = _int(
    *_register("adsbx_overrides_ttl_days", "RSBS_ADSBX_OVERRIDES_TTL_DAYS",
               "365", "ADSBX_OVERRIDES_TTL_DAYS")
)
# Background prewarmer for map heatmap/coverage caches. On when DuckDB is
# on; harmless to leave on with DuckDB off (the prewarmer self-disables if
# the analytics engine isn't available — running the heavy SQLite query
# unsolicited would hammer the collector).
PREWARM_MAP_CACHE = _bool("RSBS_PREWARM_MAP_CACHE", default=True)

# ---------------------------------------------------------------------------
# Receiver health dashboard (rule-based checks over receiver_stats)
# ---------------------------------------------------------------------------
HEALTH_HEARTBEAT_CRIT_S = _int(*_register("health_heartbeat_crit_s",   "RSBS_HEALTH_HEARTBEAT_CRIT_S", "300", "HEALTH_HEARTBEAT_CRIT_S"))  # no metrics row in 5 min
HEALTH_HEARTBEAT_WARN_S = _int(*_register("health_heartbeat_warn_s",   "RSBS_HEALTH_HEARTBEAT_WARN_S", "120", "HEALTH_HEARTBEAT_WARN_S"))  # last row 2+ min old
HEALTH_AIRCRAFT_GAP_S   = _int(*_register("health_aircraft_gap_s",     "RSBS_HEALTH_AIRCRAFT_GAP_S",   "600", "HEALTH_AIRCRAFT_GAP_S"))  # 0 aircraft for 10 min => critical
HEALTH_NOISE_CRIT_DB    = _float(*_register("health_noise_crit_db",    "RSBS_HEALTH_NOISE_CRIT_DB",    "-25", "HEALTH_NOISE_CRIT_DB"))  # dBFS, higher is worse
HEALTH_NOISE_WARN_DB    = _float(*_register("health_noise_warn_db",    "RSBS_HEALTH_NOISE_WARN_DB",    "-28", "HEALTH_NOISE_WARN_DB"))
HEALTH_CPU_CRIT_PCT     = _float(*_register("health_cpu_crit_pct",     "RSBS_HEALTH_CPU_CRIT_PCT",     "90",  "HEALTH_CPU_CRIT_PCT"))   # demod % of one core
HEALTH_CPU_WARN_PCT     = _float(*_register("health_cpu_warn_pct",     "RSBS_HEALTH_CPU_WARN_PCT",     "80",  "HEALTH_CPU_WARN_PCT"))
# Phase 2 — baseline-aware checks (same hour-of-week, prior weeks)
HEALTH_BASELINE_WEEKS       = _int(*_register("health_baseline_weeks",        "RSBS_HEALTH_BASELINE_WEEKS",       "4",  "HEALTH_BASELINE_WEEKS"))
HEALTH_BASELINE_MIN_SAMPLES = _int(*_register("health_baseline_min_samples",  "RSBS_HEALTH_BASELINE_MIN_SAMPLES", "3",  "HEALTH_BASELINE_MIN_SAMPLES"))
HEALTH_MSG_DROP_PCT         = _float(*_register("health_msg_drop_pct",        "RSBS_HEALTH_MSG_DROP_PCT",         "50", "HEALTH_MSG_DROP_PCT"))  # warn below this % of baseline
HEALTH_AIRCRAFT_DROP_PCT    = _float(*_register("health_aircraft_drop_pct",   "RSBS_HEALTH_AIRCRAFT_DROP_PCT",    "25", "HEALTH_AIRCRAFT_DROP_PCT"))
HEALTH_SIGNAL_DROP_DB       = _float(*_register("health_signal_drop_db",      "RSBS_HEALTH_SIGNAL_DROP_DB",       "3",  "HEALTH_SIGNAL_DROP_DB"))   # warn if signal drops this many dB below baseline
# Phase 3 — gain hints
HEALTH_GAIN_STRONG_PCT  = _float(*_register("health_gain_strong_pct",   "RSBS_HEALTH_GAIN_STRONG_PCT",  "5",    "HEALTH_GAIN_STRONG_PCT"))   # warn above this % strong-signals/messages
HEALTH_RANGE_SHORT_DAYS = _int(*_register("health_range_short_days",    "RSBS_HEALTH_RANGE_SHORT_DAYS", "7",    "HEALTH_RANGE_SHORT_DAYS"))
HEALTH_RANGE_LONG_DAYS  = _int(*_register("health_range_long_days",     "RSBS_HEALTH_RANGE_LONG_DAYS",  "30",   "HEALTH_RANGE_LONG_DAYS"))
HEALTH_RANGE_RATIO      = _float(*_register("health_range_ratio",       "RSBS_HEALTH_RANGE_RATIO",      "0.85", "HEALTH_RANGE_RATIO")) # info if 7d max < 30d max × this

# ---------------------------------------------------------------------------
# Map / historical replay
# ---------------------------------------------------------------------------
MAP_HISTORY_HOURS  = _int(*_register("map_history_hours", "RSBS_MAP_HISTORY_HOURS", "24", "MAP_HISTORY_HOURS"))    # slider reach (hours)
# PY-11 (Audit 2026-05-31): time-window bound on the map trail CTE.
# Without it, ROW_NUMBER() ranks every historical position for each
# selected flight_id up to `at`. On a long flight with 10k+ positions,
# SQLite materialises the whole partition just to return the first
# `trail_count` (50) — gratuitous full-table scan.
# Default 3600s = 1h trail, vs the live-view _MAP_WINDOW_SEC=600s
# activity bound (6× headroom). For replay of a historical `at`, the
# trail just shows the last hour of motion up to that point.
MAP_TRAIL_WINDOW_SECONDS = _int(
    *_register("map_trail_window_seconds", "RSBS_MAP_TRAIL_WINDOW_SECONDS",
               "3600", "MAP_TRAIL_WINDOW_SECONDS")
)

# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
WEB_HOST           = os.getenv("RSBS_WEB_HOST",  "0.0.0.0")
WEB_PORT           = _int("RSBS_WEB_PORT", "8080")
ROOT_PATH          = os.getenv(*_register("root_path", "RSBS_ROOT_PATH", "/stats", "ROOT_PATH"))          # reverse-proxy subpath

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE  = _int(*_register("page_size",     "RSBS_PAGE_SIZE",     "100", "DEFAULT_PAGE_SIZE"))
MAX_PAGE_SIZE      = _int(*_register("max_page_size", "RSBS_MAX_PAGE_SIZE", "500", "MAX_PAGE_SIZE"))
MAX_EXPORT_ROWS    = _int("RSBS_MAX_EXPORT",    "50000")
_TIME_FORMAT_RAW   = os.getenv(*_register("time_format", "RSBS_TIME_FORMAT", "24h", "TIME_FORMAT")).strip().lower()
TIME_FORMAT        = _TIME_FORMAT_RAW if _TIME_FORMAT_RAW in ("24h", "12h") else "24h"

# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN        = os.getenv(*_register("telegram_token",        "RSBS_TELEGRAM_TOKEN",    "",        "TELEGRAM_TOKEN",   secret=True))
TELEGRAM_CHAT_ID      = os.getenv(*_register("telegram_chat_id",      "RSBS_TELEGRAM_CHAT_ID",  "",        "TELEGRAM_CHAT_ID", secret=True))
TELEGRAM_SUMMARY_TIME = os.getenv(*_register("telegram_summary_time", "RSBS_SUMMARY_TIME",      "21:00",   "TELEGRAM_SUMMARY_TIME"))  # local HH:MM; "" or "off" to disable
TELEGRAM_UNITS        = os.getenv(*_register("telegram_units",        "RSBS_TELEGRAM_UNITS",    "metric",  "TELEGRAM_UNITS")) # metric|imperial|aeronautical
TELEGRAM_PHOTOS       = _int("RSBS_TELEGRAM_PHOTOS",         "1")     # 0 to disable photo enrichment
TELEGRAM_ANONYMOUS_ALERT = _int("RSBS_TELEGRAM_ANONYMOUS_ALERT", "1")  # 0 to mute first-sighting alerts for non-ICAO hex addresses
TELEGRAM_BASE_URL     = os.getenv(*_register("base_url",              "RSBS_TELEGRAM_BASE_URL", "http://homepi.local/stats", "TELEGRAM_BASE_URL"))

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

# BE-18: each /api/feeders call fans out one subprocess batch per feeder, so an
# oversized (or hostile) RSBS_FEEDERS array would multiply the per-request work.
# Cap the parsed list — the default set is 9 entries, so 64 leaves ample room.
_MAX_FEEDERS = 64


def _parse_feeders(raw: str) -> list[dict]:
    if not raw.strip():
        return _DEFAULT_FEEDERS
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("RSBS_FEEDERS must be a JSON array")
        # Cap BEFORE validating: a malformed entry past _MAX_FEEDERS lives in
        # the discarded tail, so it must not trigger a full fallback that loses
        # the valid leading feeders. Only the kept slice is validated below.
        if len(parsed) > _MAX_FEEDERS:
            print(
                f"WARNING: RSBS_FEEDERS has {len(parsed)} entries, "
                f"truncating to {_MAX_FEEDERS}",
                file=sys.stderr,
            )
            parsed = parsed[:_MAX_FEEDERS]
        # Audit 2026-05-25: validate item *and* field types so a malformed
        # deployment value cannot crash config import (`"name" in item` raises
        # TypeError when item is null/int) or crash /api/feeders later
        # (`_check_port` blows up if port is a string).
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"feeder #{i} must be a JSON object")
            for key in ("name", "unit"):
                val = item.get(key)
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(
                        f"feeder #{i} '{key}' must be a non-empty string"
                    )
            if "port" in item:
                port = item["port"]
                # Reject bool explicitly: bool is a subclass of int in Python
                # so `isinstance(True, int)` is True and we'd silently accept
                # `"port": true` as port 1.
                if (not isinstance(port, int) or isinstance(port, bool)
                        or not (1 <= port <= 65535)):
                    raise ValueError(
                        f"feeder #{i} 'port' must be an int in 1..65535"
                    )
            for key in ("status_type", "status_path", "status_url"):
                if key in item and not isinstance(item[key], str):
                    raise ValueError(f"feeder #{i} '{key}' must be a string")
        return parsed
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"ERROR: RSBS_FEEDERS: {exc}, using defaults", file=sys.stderr)
        return _DEFAULT_FEEDERS


FEEDERS = _parse_feeders(os.getenv("RSBS_FEEDERS", ""))

# improvements.md #136: filesystem root the feeder status-path guard accepts.
# Always `/run` in production; tests override via monkeypatch.setattr so they
# don't need a writable /run.
FEEDER_STATUS_ROOT = os.getenv("RSBS_FEEDER_STATUS_ROOT", "/run").rstrip("/") or "/"

# ---------------------------------------------------------------------------
# Range validation — clamp values that would cause busy loops, data loss,
# or broken filtering.  Warn to stderr and fall back to defaults.
# ---------------------------------------------------------------------------

# Intervals used in sleep() — zero causes infinite busy loops
POLL_INTERVAL_SEC    = _min_or_default_int("RSBS_POLL_INTERVAL",  POLL_INTERVAL_SEC,    1, 5)
FLIGHT_GAP_SEC       = _min_or_default_int("RSBS_FLIGHT_GAP",     FLIGHT_GAP_SEC,       1, 1800)
PURGE_INTERVAL_SEC   = _min_or_default_int("RSBS_PURGE_INTERVAL", PURGE_INTERVAL_SEC,   1, 3600)
ROUTE_ENRICH_INTERVAL = _min_or_default_int("RSBS_ROUTE_INTERVAL", ROUTE_ENRICH_INTERVAL, 1, 60)
ADSBX_POLL_INTERVAL  = _min_or_default_int("RSBS_ADSBX_INTERVAL", ADSBX_POLL_INTERVAL,  1, 60)
METRICS_INTERVAL     = _min_or_default_int("RSBS_METRICS_INTERVAL", METRICS_INTERVAL, 10, 60)
HEALTH_HEARTBEAT_CRIT_S = _min_or_default_int("RSBS_HEALTH_HEARTBEAT_CRIT_S", HEALTH_HEARTBEAT_CRIT_S, 30, 300)
HEALTH_HEARTBEAT_WARN_S = _min_or_default_int("RSBS_HEALTH_HEARTBEAT_WARN_S", HEALTH_HEARTBEAT_WARN_S, 30, 120)
HEALTH_AIRCRAFT_GAP_S   = _min_or_default_int("RSBS_HEALTH_AIRCRAFT_GAP_S",   HEALTH_AIRCRAFT_GAP_S,   60, 600)
HEALTH_CPU_CRIT_PCT     = _min_or_default_float("RSBS_HEALTH_CPU_CRIT_PCT",   HEALTH_CPU_CRIT_PCT,    1.0, 90.0)
HEALTH_CPU_WARN_PCT     = _min_or_default_float("RSBS_HEALTH_CPU_WARN_PCT",   HEALTH_CPU_WARN_PCT,    1.0, 80.0)
HEALTH_BASELINE_WEEKS       = _min_or_default_int("RSBS_HEALTH_BASELINE_WEEKS",       HEALTH_BASELINE_WEEKS,       1, 4)
HEALTH_BASELINE_MIN_SAMPLES = _min_or_default_int("RSBS_HEALTH_BASELINE_MIN_SAMPLES", HEALTH_BASELINE_MIN_SAMPLES, 1, 3)
HEALTH_MSG_DROP_PCT         = _min_or_default_float("RSBS_HEALTH_MSG_DROP_PCT",      HEALTH_MSG_DROP_PCT,      1.0, 50.0)
HEALTH_AIRCRAFT_DROP_PCT    = _min_or_default_float("RSBS_HEALTH_AIRCRAFT_DROP_PCT", HEALTH_AIRCRAFT_DROP_PCT, 1.0, 25.0)
HEALTH_SIGNAL_DROP_DB       = _min_or_default_float("RSBS_HEALTH_SIGNAL_DROP_DB",    HEALTH_SIGNAL_DROP_DB,    0.1, 3.0)
HEALTH_GAIN_STRONG_PCT      = _min_or_default_float("RSBS_HEALTH_GAIN_STRONG_PCT",   HEALTH_GAIN_STRONG_PCT,   0.1, 5.0)
HEALTH_RANGE_SHORT_DAYS     = _min_or_default_int("RSBS_HEALTH_RANGE_SHORT_DAYS",    HEALTH_RANGE_SHORT_DAYS,  1, 7)
HEALTH_RANGE_LONG_DAYS      = _min_or_default_int("RSBS_HEALTH_RANGE_LONG_DAYS",     HEALTH_RANGE_LONG_DAYS,   1, 30)
HEALTH_RANGE_RATIO          = _min_or_default_float("RSBS_HEALTH_RANGE_RATIO",       HEALTH_RANGE_RATIO,       0.1, 0.85)

# BUG-6: receiver coordinates must be in-range — an out-of-range RSBS_LAT/LON
# (e.g. a swapped/garbled value) skews every distance/bearing computation and
# the polar range plot. valid_lat/valid_lon reject NaN/inf/out-of-range; fall
# back to the documented Warsaw-area defaults and warn.
_RECEIVER_LAT_DEFAULT = 52.24199
_RECEIVER_LON_DEFAULT = 21.02872
if cleaners.valid_lat(RECEIVER_LAT) is None:
    print(f"ERROR: RSBS_LAT={RECEIVER_LAT} is out of range [-90, 90], "
          f"using default {_RECEIVER_LAT_DEFAULT}", file=sys.stderr)
    RECEIVER_LAT = _RECEIVER_LAT_DEFAULT
if cleaners.valid_lon(RECEIVER_LON) is None:
    print(f"ERROR: RSBS_LON={RECEIVER_LON} is out of range [-180, 180], "
          f"using default {_RECEIVER_LON_DEFAULT}", file=sys.stderr)
    RECEIVER_LON = _RECEIVER_LON_DEFAULT

# Thresholds — zero would reject all positions or delete valid flights
MIN_POSITIONS_KEEP   = _min_or_default_int("RSBS_MIN_POSITIONS",  MIN_POSITIONS_KEEP,   1, 2)
MAX_SEEN_POS_SEC     = _min_or_default_int("RSBS_MAX_SEEN_POS",   MAX_SEEN_POS_SEC,     1, 60)
RECEIVER_MAX_RANGE   = _min_or_default_int("RSBS_MAX_RANGE",      RECEIVER_MAX_RANGE,   1, 450)
MAX_SPEED_KTS        = _min_or_default_int("RSBS_MAX_SPEED_KTS",  MAX_SPEED_KTS,        1, 2000)
MAX_GS_CIVIL_KTS     = _min_or_default_int("RSBS_MAX_GS_CIVIL",   MAX_GS_CIVIL_KTS,    1, 750)
MAX_GS_MILITARY_KTS  = _min_or_default_int("RSBS_MAX_GS_MILITARY", MAX_GS_MILITARY_KTS, 1, 1800)
MAX_GS_DEVIATION_KTS = _min_or_default_int("RSBS_MAX_GS_DEVIATION", MAX_GS_DEVIATION_KTS, 1, 100)
MAX_GS_ACCEL_KTS_S        = _min_or_default_float("RSBS_MAX_GS_ACCEL",          MAX_GS_ACCEL_KTS_S,        0.1, 8.0)
MLAT_OUTLIER_FACTOR       = _min_or_default_float("RSBS_MLAT_OUTLIER_FACTOR",   MLAT_OUTLIER_FACTOR,       2.0, 5.0)
MLAT_OUTLIER_MIN_READINGS = _min_or_default_int(  "RSBS_MLAT_OUTLIER_MIN",      MLAT_OUTLIER_MIN_READINGS, 3,   50)
ADSBX_RANGE_NM       = _min_or_default_int("RSBS_ADSBX_RANGE",   ADSBX_RANGE_NM,      1, 250)
ROUTE_BATCH_SIZE     = _min_or_default_int("RSBS_ROUTE_BATCH",   ROUTE_BATCH_SIZE,     1, 20)
# STY-1: floor at 0.0 (0 = no inter-call delay / disabled); a negative value is
# nonsensical and falls back to the 1.0s default. No upper bound.
ROUTE_RATE_LIMIT_SEC = _min_or_default_float("RSBS_ROUTE_RATE_LIMIT", ROUTE_RATE_LIMIT_SEC, 0.0, 1.0)

# Map history — zero would make the slider useless
MAP_HISTORY_HOURS    = _min_or_default_int("RSBS_MAP_HISTORY_HOURS", MAP_HISTORY_HOURS, 1, 24)
# PY-11: trail window — zero would degenerate to no trail at all; floor 60s.
MAP_TRAIL_WINDOW_SECONDS = _min_or_default_int(
    "RSBS_MAP_TRAIL_WINDOW_SECONDS", MAP_TRAIL_WINDOW_SECONDS, 60, 3600)

# Pagination — zero causes FastAPI validation conflicts (ge=1, le=0)
MAX_PAGE_SIZE        = _min_or_default_int("RSBS_MAX_PAGE_SIZE",  MAX_PAGE_SIZE,        1, 500)
DEFAULT_PAGE_SIZE    = _min_or_default_int("RSBS_PAGE_SIZE",      DEFAULT_PAGE_SIZE,    1, 100)
MAX_EXPORT_ROWS      = _min_or_default_int("RSBS_MAX_EXPORT",    MAX_EXPORT_ROWS,      1, 50000)

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
ROOT_PATH         = ROOT_PATH.rstrip("/")         if ROOT_PATH         else ROOT_PATH
TELEGRAM_BASE_URL = TELEGRAM_BASE_URL.rstrip("/") if TELEGRAM_BASE_URL else TELEGRAM_BASE_URL
ADSBX_API_URL     = ADSBX_API_URL.rstrip("/")     if ADSBX_API_URL     else ADSBX_API_URL

# DB_PATH — empty string causes sqlite3.connect("") which is an in-memory DB
# that silently loses all data on restart
_DB_PATH_DEFAULT = "/mnt/ext/readsbstats/history.db"
if not DB_PATH.strip():
    print(f"ERROR: RSBS_DB_PATH is empty, using default {_DB_PATH_DEFAULT}",
          file=sys.stderr)
    DB_PATH = _DB_PATH_DEFAULT
