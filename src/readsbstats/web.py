"""
readsbstats — FastAPI web server.

Serves the flight history web UI and JSON API.
Run via uvicorn (see systemd/readsbstats-web.service).
"""

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import OrderedDict as _OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
import urllib.parse
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, config, database, enrichment, geo, health, icao_ranges, photo_sources, route_enricher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Geometry helpers (used by polar endpoint)
# ---------------------------------------------------------------------------

_haversine_nm = geo.haversine_nm
_bearing = geo.bearing

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
    log.info("Starting web server — DB: %s", config.DB_PATH)
    # Audit-13 A13-067: `_migrate` may run a handful of ALTER TABLEs and
    # `CREATE INDEX` statements on cold disk — hundreds of ms on a Pi 4.
    # Wrap in a thread so the event loop stays free during startup.
    # Open and close an explicit connection rather than using db() so the
    # worker thread does not leave a thread-local connection open for its
    # lifetime (fixing A14-004 startup connection leak).
    # When a test has injected _db, use it directly (in-memory DBs can't be
    # reopened) and skip the close — the test owns the connection.
    def _startup_migrate() -> None:
        if _db is not None:
            database._migrate(_db)
            return
        conn = database.connect()
        try:
            database._migrate(conn)
        finally:
            conn.close()
    await asyncio.to_thread(_startup_migrate)
    # Background migrations (positions indexes, bearing backfill) are owned by
    # the collector so two processes don't fight on the SQLite write lock.
    route_enricher.start_background_enricher()
    # Eager-init DuckDB so the first user hit doesn't pay extension+ATTACH
    # cost (~1–2 s). If the engine is up, also kick off the prewarmer so
    # users land on warm cache instead of triggering the cold-scan path.
    if analytics.is_available():
        log.info("analytics: DuckDB engine ready")
        if config.PREWARM_MAP_CACHE:
            log.info("starting map-cache prewarmer (8 targets, half-TTL refresh)")
            _start_prewarmer()
    yield
    _stop_prewarmer()
    analytics.close()
    log.info("Web server stopped")


app = FastAPI(root_path=config.ROOT_PATH, docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
# `/static` still serves /api/airspace's bundled GeoJSON and the favicon
# fallback. The old Jinja JS/CSS subtrees were removed at v2.0.0 cutover.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# ---------------------------------------------------------------------------
# React SPA mount.  Serves the Vite build from frontend/dist/ at the root of
# the nginx prefix (/stats/ externally; / internally because of root_path).
# Gated by presence of the built artefacts so a missing dist (e.g. fresh
# clone, mid-rsync) doesn't crash the worker — the API surface keeps working
# but every UI path returns 404.
#
# index.html is served per-request (not cached at import) so atomic-swap
# deploys take effect without restart; assets are mounted via StaticFiles and
# can be long-cached because their URLs are content-hashed.
#
# /v2/* paths from the v2.0.0-rc.1 era 301-redirect to / so RC bookmarks
# keep working.
# ---------------------------------------------------------------------------
SPA_DIR = BASE_DIR / "frontend" / "dist"
SPA_ASSETS = SPA_DIR / "assets"
SPA_INDEX = SPA_DIR / "index.html"

_SPA_AVAILABLE = SPA_INDEX.is_file() and SPA_ASSETS.is_dir()

if _SPA_AVAILABLE:
    app.mount("/assets", StaticFiles(directory=SPA_ASSETS), name="spa-assets")

    # Top-level static files emitted by Vite from `frontend/public/` — they
    # land at the root of `dist/`, NOT under `dist/assets/`, so the /assets
    # mount above doesn't catch them. Add explicit routes for each one
    # (rather than a StaticFiles mount at "/" which would shadow /api/*).
    from fastapi.responses import FileResponse  # local import to keep top of file tidy

    @app.get("/favicon.svg", include_in_schema=False)
    def _favicon_svg() -> FileResponse:
        return FileResponse(
            SPA_DIR / "favicon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


# Audit-13 A13-005: /v2 compat redirect lives OUTSIDE the SPA-availability
# gate. During a mid-rsync deploy the SPA dist may be briefly absent;
# /v2 bookmarks should still rewrite their URL bar to the canonical
# scheme even though the target path itself will 404 until the deploy
# completes. Old `if _SPA_AVAILABLE:`-gated registration meant /v2/...
# 404'd outright during the same window.
@app.get("/v2", include_in_schema=False)
@app.get("/v2/{rest:path}", include_in_schema=False)
def _v2_compat(request: Request, rest: str = "") -> RedirectResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    sanitized = _sanitize_v2_rest(rest)
    target = f"{root}/{sanitized}" if sanitized else f"{root}/"
    # CodeQL #29 — explicit recognized sanitizer pattern. `_sanitize_v2_rest`
    # already strips the leading `/` and `\` that produce scheme-relative
    # URLs, but CodeQL's data-flow analysis doesn't know our custom helper
    # is safe (CWE-601 / `py/url-redirection`). This urlparse() check is
    # the pattern CodeQL's own documentation recommends, and serves as a
    # defence-in-depth catch: a safe redirect target has neither scheme
    # nor netloc. If anything slips through the sanitizer, fall back to
    # the SPA root rather than honour the redirect.
    parsed_target = urllib.parse.urlparse(target)
    if parsed_target.scheme or parsed_target.netloc:
        return RedirectResponse(url=f"{root}/", status_code=301)
    return RedirectResponse(url=target, status_code=301)


def _sanitize_v2_rest(rest: str) -> str:
    """Return a safe path suffix for the /v2 → / redirect.

    Hardening against:
      * Open-redirect (CodeQL #28): a crafted `/v2//evil.com` would otherwise
        produce a Location starting with `//` (browsers treat that as
        scheme-relative and follow off-site). We strip leading `/` and `\\`
        characters — some browsers treat the latter as the former in URLs.
      * Response splitting (audit-12 #149): Starlette rejects raw CR/LF in
        path parameters today, but if a future ASGI server change weakens
        that we don't want CR/LF reaching the Location header.
      * Header validity (audit-12 P8 follow-up): percent-encode the remaining
        path so spaces / quotes / other URL-special characters can't
        produce a malformed Location. The original ``_sanitize_v2_rest``
        landed only the strip; the quote step was always part of the
        audit's recommended fix.
    """
    rest = rest.lstrip("/\\")
    rest = rest.replace("\r", "").replace("\n", "")
    return urllib.parse.quote(rest, safe="/")

# Note: the SPA root catch-all (`@app.get("/{spa_path:path}")`) is registered
# at the END of this module — see the bottom of the file. It has to come
# after every literal /api/* and named route so it doesn't shadow them.

# ---------------------------------------------------------------------------
# DB connection — per-thread.  Python's sqlite3 module holds a per-connection
# mutex, so sharing a single connection across uvicorn's threadpool would
# serialize every request — destroying WAL's reader concurrency.  Each thread
# gets its own connection, opened lazily on first use.
#
# Tests inject an in-memory connection by setting `_db` directly; when set,
# every thread sees that connection (in-memory DBs cannot be reopened).
# ---------------------------------------------------------------------------
_db: sqlite3.Connection | None = None  # test override; None in production
_thread_local = threading.local()


def db() -> sqlite3.Connection:
    if _db is not None:
        return _db
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = database.connect()
        _thread_local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Response cache — bounded TTL store, keyed by endpoint name
# ---------------------------------------------------------------------------
# Audit 2026-05-25: filtered /api/stats keys are caller-controlled
# (`stats:{from}:{to}`), so the cache must cap total entries and evict the
# oldest on overflow. OrderedDict gives us insertion-order eviction without
# a dependency.
_cache: "_OrderedDict[str, tuple[float, object]]" = _OrderedDict()
_CACHE_MAX_ENTRIES = 256
_CACHE_TTLS: dict[str, int] = {
    "stats":        120,   # seconds — aggregate data, no need to recompute often
    "polar":        300,   # seconds — max range rarely shifts
    "records":      300,   # seconds — all-time bests, very stable
    "health":        60,   # seconds — matches metrics_collector poll cycle
    "dates":        600,   # seconds — calendar of flight days; only ticks daily
    "heatmap:24h":   300,   # 5 min — recent data changes frequently
    "heatmap:7d":   1800,   # 30 min
    "heatmap:30d":  7200,   # 2 h — large query, cache aggressively
    "heatmap:all":  21600,  # 6 h — full-history scan, very stable
    "coverage:24h":  300,
    "coverage:7d":  1800,
    "coverage:30d": 7200,
    "coverage:all": 21600,
}
_DEFAULT_TTL  = 30    # seconds
_AIRSPACE_TTL = 3600  # seconds — airspace data rarely changes


def _ttl_for(key: str) -> int:
    # Filtered stats keys arrive as ``stats:{from}:{to}``; the base prefix
    # ``stats`` already has an entry in _CACHE_TTLS, so look that up first
    # before falling back to the default.
    if key in _CACHE_TTLS:
        return _CACHE_TTLS[key]
    base = key.split(":", 1)[0]
    return _CACHE_TTLS.get(base, _DEFAULT_TTL)


def _get_cache(key: str) -> object | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry[0] < _ttl_for(key):
        return entry[1]
    # Lazy eviction so an expired key doesn't keep occupying the cap.
    del _cache[key]
    return None


def _set_cache(key: str, value: object) -> None:
    now = time.time()
    # Refreshing an existing key keeps insertion order useful only when
    # we move it to the end; otherwise the same key would be evicted
    # before never-touched-since keys.
    if key in _cache:
        _cache.move_to_end(key)
    _cache[key] = (now, value)
    if len(_cache) > _CACHE_MAX_ENTRIES:
        # Drop any expired entries first; if still over cap, evict in
        # insertion order (oldest first).
        for k in list(_cache.keys()):
            ts, _ = _cache[k]
            if now - ts >= _ttl_for(k):
                del _cache[k]
            if len(_cache) <= _CACHE_MAX_ENTRIES:
                break
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def _fmt_ts(epoch: int | None) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM' UTC. Used by the CSV
    export endpoint; empty string for None."""
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Enrichment helper — resolves reg/type via aircraft_db when NULL in flights
# ---------------------------------------------------------------------------

# OR-merged flag bitmask: aircraft_db.flags | adsbx_overrides.flags | computed
# anonymous bit.  The anon CASE evaluates the source icao_hex against the ICAO
# state-allocation table at query time, so non-state addresses (e.g. dd85cb)
# surface as FLAG_ANONYMOUS=16 retroactively without any DB column or backfill.
# Use the variant matching the alias of the source column in scope.
_ANON_SQL_F   = icao_ranges.anonymous_flag_sql("f.icao_hex",   config.FLAG_ANONYMOUS)
_ANON_SQL_SUB = icao_ranges.anonymous_flag_sql("sub.icao_hex", config.FLAG_ANONYMOUS)
_ANON_SQL_AF  = icao_ranges.anonymous_flag_sql("af.icao_hex",  config.FLAG_ANONYMOUS)
_FLAGS_EXPR_F   = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_F})"
_FLAGS_EXPR_SUB = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_SUB})"
_FLAGS_EXPR_AF  = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_AF})"

# Joined SELECT fragment used in flight list and detail queries
_FLIGHT_COLS = f"""
    f.id,
    f.icao_hex,
    f.callsign                                            AS callsign,
    COALESCE(f.registration,  adb.registration, axo.registration)  AS registration,
    COALESCE(f.aircraft_type, adb.type_code,    axo.type_code)     AS aircraft_type,
    COALESCE(adb.type_desc,   axo.type_desc,    '')                AS type_desc,
    {_FLAGS_EXPR_F}                                                AS flags,
    f.squawk,
    f.category,
    f.primary_source,
    f.first_seen,
    f.last_seen,
    (f.last_seen - f.first_seen)                         AS duration_sec,
    f.max_alt_baro,
    f.max_gs,
    f.max_distance_nm,
    f.total_positions,
    f.adsb_positions,
    f.mlat_positions,
    f.lat_min, f.lat_max, f.lon_min, f.lon_max,
    f.origin_icao,
    f.dest_icao,
    ap_orig.name     AS origin_name,
    ap_orig.country  AS origin_country,
    ap_dest.name     AS dest_name,
    ap_dest.country  AS dest_country
"""

_FLIGHT_JOIN = """
    LEFT JOIN aircraft_db      adb  ON adb.icao_hex     = f.icao_hex
    LEFT JOIN adsbx_overrides  axo  ON axo.icao_hex     = f.icao_hex
    LEFT JOIN airports         ap_orig ON ap_orig.icao_code = f.origin_icao
    LEFT JOIN airports         ap_dest ON ap_dest.icao_code = f.dest_icao
"""

# Whitelist of sortable columns for /api/flights
_SORT_COLS: dict[str, str] = {
    "first_seen":     "f.first_seen",
    "icao_hex":       "f.icao_hex",
    "callsign":       "f.callsign",
    "registration":   "COALESCE(f.registration, adb.registration)",
    "aircraft_type":  "COALESCE(f.aircraft_type, adb.type_code)",
    "primary_source": "f.primary_source",
    "duration_sec":   "(f.last_seen - f.first_seen)",
    "max_alt_baro":   "f.max_alt_baro",
    "max_gs":         "f.max_gs",
    "max_distance_nm":"f.max_distance_nm",
    "total_positions":"f.total_positions",
    "origin_icao":    "f.origin_icao",
    "dest_icao":      "f.dest_icao",
}

# Audit-13 A13-077: sibling allowlist for /api/aircraft/flagged. Keys
# resolve to columns from the GROUP-BY aggregate SELECT in that handler
# (not the per-flight `f.*` columns above), so we keep this as a
# separate dict rather than rolling everything into `_SORT_COLS` and
# leaking aggregate names into the /api/flights surface.
_FLAGGED_SORT_COLS: dict[str, str] = {
    "last_seen":     "last_seen",
    "first_seen":    "first_seen",
    "flight_count":  "flight_count",
    "registration":  "registration",
    "aircraft_type": "aircraft_type",
}


# ---------------------------------------------------------------------------
# Receiver metrics — column allowlist and aggregation types
# ---------------------------------------------------------------------------

_METRICS_COLS = frozenset({
    "ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat",
    "signal", "noise", "peak_signal", "strong_signals",
    "local_modes", "local_bad", "local_unknown_icao",
    "local_accepted_0", "local_accepted_1",
    "samples_dropped", "samples_lost",
    "messages", "positions_total", "positions_adsb", "positions_mlat",
    "max_distance_m", "tracks_new", "tracks_single",
    "cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap",
    "remote_modes", "remote_bad", "remote_accepted", "remote_bytes_in", "remote_bytes_out",
    "cpr_airborne", "cpr_global_ok", "cpr_global_bad", "cpr_global_range",
    "cpr_global_speed", "cpr_global_skipped", "cpr_local_ok",
    "cpr_local_range", "cpr_local_speed", "cpr_filtered",
    "altitude_suppressed",
})

# Columns where MAX is the correct aggregation (peaks / extremes)
_METRICS_MAX = frozenset({"peak_signal", "strong_signals", "max_distance_m"})
# Columns where AVG is the correct aggregation (continuous measurements)
_METRICS_AVG = frozenset({
    "ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat",
    "signal", "noise",
    "cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap",
    "samples_dropped", "samples_lost",
})
# Everything else uses SUM (counters per interval)


def _metrics_agg(col: str) -> str:
    """Return the SQL aggregate function for a metrics column."""
    if col in _METRICS_MAX:
        return f"MAX({col})"
    if col in _METRICS_AVG:
        return f"AVG({col})"
    return f"SUM({col})"


# ---------------------------------------------------------------------------
# The Jinja UI is gone and the SPA owns the root URL space — /flight/{id},
# /aircraft/{icao} et al. are served by the SPA catch-all at the bottom of
# this module. /live is the one exception: it's not a real SPA page, just a
# historical alias for /map, so we keep a server-side 302.
# ---------------------------------------------------------------------------


@app.get("/live", include_in_schema=False)
def redirect_live(request: Request) -> RedirectResponse:
    # Audit-13 A13-049: defence in depth against a hostile reverse-proxy
    # injecting an absolute root_path. Use the same urlparse() check
    # _v2_compat uses (which CodeQL recognises as a recognised sanitiser).
    root = request.scope.get("root_path", "").rstrip("/")
    target = f"{root}/map"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc:
        return RedirectResponse(url="/map", status_code=302)
    return RedirectResponse(url=target, status_code=302)


def _settings_receiver() -> dict:
    return {
        "lat":       config.RECEIVER_LAT,
        "lon":       config.RECEIVER_LON,
        "max_range": config.RECEIVER_MAX_RANGE,
    }


def _settings_collector() -> dict:
    return {
        "poll_interval": config.POLL_INTERVAL_SEC,
        "flight_gap":    config.FLIGHT_GAP_SEC,
        "min_positions": config.MIN_POSITIONS_KEEP,
        "max_seen_pos":  config.MAX_SEEN_POS_SEC,
        "max_speed_kts": config.MAX_SPEED_KTS,
    }


def _settings_database() -> dict:
    # Mask filesystem paths — operator just needs to know whether a custom
    # value was set, not the actual path on disk (#H8 + audit-12 #171).
    return {
        "db_path":        os.path.basename(config.DB_PATH) or "(default)",
        "retention_days": config.RETENTION_DAYS,
        "purge_interval": config.PURGE_INTERVAL_SEC,
    }


def _settings_enrichment() -> dict:
    airspace_label = "(set)" if config.AIRSPACE_GEOJSON else "(bundled poland.geojson)"
    return {
        "photo_cache_days": config.PHOTO_CACHE_DAYS,
        "airspace_geojson": airspace_label,
        "route_cache_days": config.ROUTE_CACHE_DAYS,
        "route_interval":   config.ROUTE_ENRICH_INTERVAL,
        "route_batch":      config.ROUTE_BATCH_SIZE,
        "route_rate_limit": config.ROUTE_RATE_LIMIT_SEC,
        # External ADS-B enrichment
        "adsbx_enabled":  config.ADSBX_ENABLED,
        "adsbx_interval": config.ADSBX_POLL_INTERVAL,
        "adsbx_range":    config.ADSBX_RANGE_NM,
        "adsbx_url":      config.ADSBX_API_URL,
    }


def _settings_metrics() -> dict:
    # Audit-12 P8: stats_json previously compared against a literal
    # `/run/readsb/stats.json`. Just report "(configured)" for any non-empty
    # value — avoids duplicating the default and avoids leaking the one bit
    # of "was it customised".
    return {
        "metrics_enabled":  config.METRICS_ENABLED,
        "metrics_interval": config.METRICS_INTERVAL,
        "stats_json":       "(configured)" if config.STATS_JSON else "(not set)",
    }


def _settings_health() -> dict:
    return {
        "health_heartbeat_warn_s":     config.HEALTH_HEARTBEAT_WARN_S,
        "health_heartbeat_crit_s":     config.HEALTH_HEARTBEAT_CRIT_S,
        "health_aircraft_gap_s":       config.HEALTH_AIRCRAFT_GAP_S,
        "health_noise_warn_db":        config.HEALTH_NOISE_WARN_DB,
        "health_noise_crit_db":        config.HEALTH_NOISE_CRIT_DB,
        "health_cpu_warn_pct":         config.HEALTH_CPU_WARN_PCT,
        "health_cpu_crit_pct":         config.HEALTH_CPU_CRIT_PCT,
        "health_baseline_weeks":       config.HEALTH_BASELINE_WEEKS,
        "health_baseline_min_samples": config.HEALTH_BASELINE_MIN_SAMPLES,
        "health_msg_drop_pct":         config.HEALTH_MSG_DROP_PCT,
        "health_aircraft_drop_pct":    config.HEALTH_AIRCRAFT_DROP_PCT,
        "health_signal_drop_db":       config.HEALTH_SIGNAL_DROP_DB,
        "health_gain_strong_pct":      config.HEALTH_GAIN_STRONG_PCT,
        "health_range_short_days":     config.HEALTH_RANGE_SHORT_DAYS,
        "health_range_long_days":      config.HEALTH_RANGE_LONG_DAYS,
        "health_range_ratio":          config.HEALTH_RANGE_RATIO,
    }


def _settings_ui() -> dict:
    # web_host / web_port intentionally omitted (audit-12 #171): the client
    # is already at that URL, and on a reverse-proxied deploy the bind host
    # (0.0.0.0) would be misleading anyway.
    return {
        "root_path":         config.ROOT_PATH,
        "page_size":         config.DEFAULT_PAGE_SIZE,
        "max_page_size":     config.MAX_PAGE_SIZE,
        "time_format":       config.TIME_FORMAT,
        "map_history_hours": config.MAP_HISTORY_HOURS,
    }


def _settings_telegram() -> dict:
    # Mask the token + chat id (#H8 + audit-12 #171) — consumer never sees
    # raw secrets, just whether they're configured.
    return {
        "telegram_token":        "configured" if config.TELEGRAM_TOKEN else "not set",
        "telegram_chat_id":      "configured" if config.TELEGRAM_CHAT_ID else "not set",
        "telegram_summary_time": config.TELEGRAM_SUMMARY_TIME,
        "telegram_units":        config.TELEGRAM_UNITS,
        "base_url":              config.TELEGRAM_BASE_URL,
    }


def _settings_payload() -> dict:
    """Return the runtime-settings dict shown on both the Jinja /settings page
    and the React /v2/settings page.  Single source of truth — keep this in
    sync with templates/settings.html.

    improvements.md A13-083: decomposed into per-domain helpers so each group
    of settings lives next to its own masking / formatting rules.  The
    payload shape (one flat dict of every key) is unchanged.
    """
    return {
        **_settings_receiver(),
        **_settings_collector(),
        **_settings_database(),
        **_settings_enrichment(),
        **_settings_metrics(),
        **_settings_health(),
        **_settings_ui(),
        **_settings_telegram(),
    }


# Falsy bool strings — kept in sync with `config._BOOL_FALSY`. Duplicated
# rather than imported to avoid widening config.py's public API surface.
_SETTINGS_BOOL_FALSY = frozenset({"", "0", "false", "no", "off"})


def _settings_default_as_parsed(default, current):
    """Cast `default` (as recorded in `_META_REGISTRY`) to the type of
    `current` (the parsed config attribute). Mirrors what the
    `_int/_float/_bool/os.getenv` parsers do at module load."""
    if isinstance(current, bool):
        if isinstance(default, bool):
            return default
        return str(default).strip().lower() not in _SETTINGS_BOOL_FALSY
    if isinstance(current, int):
        return int(default)
    if isinstance(current, float):
        return float(default)
    return str(default) if default is not None else ""


def _settings_metadata(config_namespace, payload_keys) -> dict:
    """Build the `_metadata` block keyed by payload_key. Pure function so
    tests can pass a `SimpleNamespace` stub.

    For each payload key it emits ``{env_var, default, customized}``.
    `customized` compares the **raw** config attribute against the
    registered default, not the masked display value — otherwise
    `telegram_token` would always read as customized regardless of
    whether the operator actually set it.
    """
    registry = getattr(config, "_META_REGISTRY", {})
    out: dict[str, dict] = {}
    for key in payload_keys:
        reg = registry.get(key)
        if reg is None:
            continue
        attr = reg["config_attr"]
        raw_value = getattr(config_namespace, attr, None)
        parsed_default = _settings_default_as_parsed(reg["default"], raw_value)
        customized = raw_value != parsed_default
        # Float tolerance: parsed defaults can re-roundtrip to a slightly
        # different float (e.g. "-25" → -25.0). Treat exact-equal-after-
        # round as not-customized.
        if isinstance(raw_value, float) and isinstance(parsed_default, float):
            customized = abs(raw_value - parsed_default) > 1e-9
        # Filesystem-path / secret defaults are masked: shipping the raw
        # default would leak install paths through /api/settings. The
        # `customized` flag is still meaningful, and the displayed payload
        # value is already masked by the corresponding `_settings_*` helper.
        if reg.get("secret"):
            out_default = None
        else:
            out_default = parsed_default
        out[key] = {
            "env_var":    reg["env_var"],
            "default":    out_default,
            "customized": bool(customized),
        }
    return out


@app.get("/api/settings")
def api_settings() -> dict:
    payload = _settings_payload()
    payload["_metadata"] = _settings_metadata(
        config, [k for k in payload if k != "_metadata"]
    )
    return payload


# ---------------------------------------------------------------------------
# API — watchlist
# ---------------------------------------------------------------------------

_VALID_MATCH_TYPES = {"icao", "registration", "callsign_prefix"}


def _csrf_check(x_requested_with: str | None = Header(None)) -> None:
    # Browsers cannot set custom headers cross-origin without a CORS preflight,
    # which this app rejects (no CORS allowlist). Requiring X-Requested-With
    # with the canonical `XMLHttpRequest` value blocks simple-form CSRF
    # without needing tokens. Audit-13 (A13-001) tightened the check from
    # "any non-empty value" to the literal canonical value to remove a class
    # of accidental-bypass mistakes.
    #
    # CRITICAL: this protection assumes there is **no** CORS middleware that
    # whitelists `X-Requested-With` (or `*`) in `allow_headers`.  Adding one
    # would silently disable CSRF protection for every mutating endpoint that
    # uses this dependency.  If you ever introduce `CORSMiddleware`, audit
    # `allow_headers` first and add a token-based CSRF scheme before
    # weakening it.
    if not x_requested_with or x_requested_with.strip().lower() != "xmlhttprequest":
        raise HTTPException(403, "X-Requested-With: XMLHttpRequest header is required")


class _WatchlistEntry(BaseModel):
    match_type: str = Field(max_length=32)
    value: str = Field(max_length=database.WATCHLIST_VALUE_MAX)
    label: str | None = Field(default=None, max_length=database.WATCHLIST_LABEL_MAX)


@app.get("/api/watchlist")
def api_watchlist_list() -> dict:
    rows = db().execute(
        """
        SELECT w.id, w.match_type, w.value, w.label, w.created_at,
               CASE WHEN w.match_type = 'icao' AND af.icao_hex IS NOT NULL
                    THEN 1 ELSE 0 END AS airborne
        FROM watchlist w
        LEFT JOIN active_flights af ON w.match_type = 'icao' AND af.icao_hex = w.value
        ORDER BY w.created_at
        """
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.post("/api/watchlist", status_code=201, dependencies=[Depends(_csrf_check)])
def api_watchlist_add(body: _WatchlistEntry) -> dict:
    if body.match_type not in _VALID_MATCH_TYPES:
        raise HTTPException(422, "match_type must be icao, registration, or callsign_prefix")
    value = body.value.strip().lower()
    if not value:
        raise HTTPException(422, "value is required")
    label = body.label.strip() if body.label else None
    try:
        with db():
            cur = db().execute(
                "INSERT INTO watchlist (match_type, value, label, created_at) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                (body.match_type, value, label),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Entry already exists")
    return {"id": cur.lastrowid, "match_type": body.match_type, "value": value, "label": label}


@app.delete("/api/watchlist/{entry_id}", status_code=204,
            dependencies=[Depends(_csrf_check)])
def api_watchlist_delete(entry_id: int) -> Response:
    row = db().execute("SELECT id FROM watchlist WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    with db():
        db().execute("DELETE FROM watchlist WHERE id = ?", (entry_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# API — flight list
# ---------------------------------------------------------------------------

def _build_flight_filter(
    date: str | None,
    icao: str | None,
    callsign: str | None,
    registration: str | None,
    aircraft_type: str | None,
    source: str | None,
    flags: str | None,
    squawk: str | None = None,
    date_from: str | None = None,
    date_to:   str | None = None,
    from_ts: int | None = None,
    to_ts:   int | None = None,
) -> tuple[str, list]:
    """Return (WHERE clause, params list) for the shared flight filter params.

    Date filtering supports either:
      - `date=YYYY-MM-DD`           — single calendar day (receiver local time)
      - `from`/`to` epoch seconds   — browser-local midnight boundaries (preferred)
      - `date_from=YYYY-MM-DD` and/or `date_to=YYYY-MM-DD` — receiver local time
        (kept for backward compat; epoch params take priority when both are sent)

    If `date` is set, the range params are ignored — single-day takes priority
    because that's what the old single-`date` UI sent, and we don't want to
    break bookmarked URLs.
    """
    conditions: list[str] = []
    params: list = []

    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        day_start = int(day.timestamp())
        day_end = day_start + 86400
        conditions.append("f.first_seen >= ? AND f.first_seen < ?")
        params += [day_start, day_end]
    elif from_ts is not None or to_ts is not None:
        if from_ts is not None:
            conditions.append("f.first_seen >= ?")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("f.first_seen < ?")
            params.append(to_ts)
    elif date_from or date_to:
        if date_from:
            try:
                lo_day = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "date_from must be YYYY-MM-DD")
            conditions.append("f.first_seen >= ?")
            params.append(int(lo_day.timestamp()))
        if date_to:
            try:
                hi_day = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "date_to must be YYYY-MM-DD")
            # End of day inclusive: + 86400 so date_to=YYYY-MM-DD captures
            # everything up to (but not including) the next midnight.
            conditions.append("f.first_seen < ?")
            params.append(int(hi_day.timestamp()) + 86400)

    if icao:
        conditions.append("f.icao_hex = ?")
        params.append(icao.lower().lstrip("~"))

    if callsign:
        conditions.append("f.callsign LIKE ?")
        params.append(callsign.upper().strip() + "%")

    if registration:
        conditions.append("COALESCE(f.registration, adb.registration) LIKE ?")
        params.append(registration.upper().strip() + "%")

    if aircraft_type:
        conditions.append("COALESCE(f.aircraft_type, adb.type_code) = ?")
        params.append(aircraft_type.upper().strip())

    if source:
        conditions.append("f.primary_source = ?")
        params.append(source.lower())

    if flags == "military":
        conditions.append(f"({_FLAGS_EXPR_F} & 1) = 1")
    elif flags == "interesting":
        conditions.append(
            f"({_FLAGS_EXPR_F} & 2) = 2 AND ({_FLAGS_EXPR_F} & 1) = 0"
        )
    elif flags == "anonymous":
        # Show "anonymous-only" contacts — military/interesting take precedence
        # and surface under their own filter (mirrors the interesting/military split).
        conditions.append(
            f"({_FLAGS_EXPR_F} & 16) = 16 AND ({_FLAGS_EXPR_F} & 3) = 0"
        )

    if squawk:
        conditions.append("f.squawk = ?")
        params.append(squawk.strip())

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


@app.get("/api/flights")
def api_flights(
    date: str | None = Query(None, description="YYYY-MM-DD (receiver local time)"),
    date_from: str | None = Query(None, description="YYYY-MM-DD inclusive range start (receiver local time)"),
    date_to:   str | None = Query(None, description="YYYY-MM-DD inclusive range end (receiver local time)"),
    from_ts: int | None = Query(None, alias="from", description="Unix timestamp range start (browser-local midnight)"),
    to_ts:   int | None = Query(None, alias="to",   description="Unix timestamp range end (browser-local midnight of next day)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    squawk: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    where, params = _build_flight_filter(
        date, icao, callsign, registration, aircraft_type, source, flags, squawk,
        date_from=date_from, date_to=date_to, from_ts=from_ts, to_ts=to_ts,
    )
    conn = db()

    # Only JOIN extra tables for COUNT when filters need them
    needs_join = registration or aircraft_type or flags
    count_join = _FLIGHT_JOIN if needs_join else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM flights f {count_join} {where}", params
    ).fetchone()
    total = total_row["n"] if total_row else 0

    sort_col = _SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        {where}
        ORDER BY {sort_col} {sort_order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "flights": [dict(r) for r in rows],
    }


_CSV_COLS = [
    "first_seen", "last_seen", "duration_sec",
    "icao_hex", "callsign", "registration", "aircraft_type", "type_desc",
    "squawk", "category", "primary_source",
    "max_alt_baro", "max_gs", "max_distance_nm",
    "total_positions", "adsb_positions", "mlat_positions",
    "origin_icao", "dest_icao",
]


@app.get("/api/flights/export.csv")
def api_flights_export(
    date: str | None = Query(None, description="YYYY-MM-DD (receiver local time)"),
    date_from: str | None = Query(None, description="YYYY-MM-DD inclusive range start (receiver local time)"),
    date_to:   str | None = Query(None, description="YYYY-MM-DD inclusive range end (receiver local time)"),
    icao: str | None = Query(None),
    callsign: str | None = Query(None),
    registration: str | None = Query(None),
    aircraft_type: str | None = Query(None),
    source: str | None = Query(None, description="adsb | mlat | mixed | other"),
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    squawk: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> Response:
    where, params = _build_flight_filter(
        date, icao, callsign, registration, aircraft_type, source, flags, squawk,
        date_from=date_from, date_to=date_to,
    )
    sort_col = _SORT_COLS.get(sort_by or "", "f.first_seen")
    sort_order = "ASC" if sort_dir == "asc" else "DESC"

    # Audit-13 A13-055: stream rows instead of materialising the entire
    # CSV in memory. On a Pi 4 a 50k-row export previously buffered a
    # multi-MB StringIO before the first byte hit the wire.
    sql = f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        {where}
        ORDER BY {sort_col} {sort_order}
        LIMIT ?
        """
    bind = params + [config.MAX_EXPORT_ROWS]
    conn = db()

    def _iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        cursor = conn.execute(sql, bind)
        while True:
            chunk = cursor.fetchmany(1000)
            if not chunk:
                break
            for r in chunk:
                writer.writerow([
                    _fmt_ts(r["first_seen"]), _fmt_ts(r["last_seen"]), r["duration_sec"],
                    r["icao_hex"], r["callsign"] or "", r["registration"] or "",
                    r["aircraft_type"] or "", r["type_desc"] or "",
                    r["squawk"] or "", r["category"] or "", r["primary_source"] or "",
                    r["max_alt_baro"], r["max_gs"],
                    round(r["max_distance_nm"], 1) if r["max_distance_nm"] is not None else "",
                    r["total_positions"], r["adsb_positions"], r["mlat_positions"],
                    r["origin_icao"] or "", r["dest_icao"] or "",
                ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    filename = f"flights_{date}.csv" if date else "flights.csv"
    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# API — single flight detail
# ---------------------------------------------------------------------------

@app.get("/api/flights/{flight_id}")
def api_flight_detail(flight_id: int) -> dict:
    conn = db()
    flight = conn.execute(
        f"SELECT {_FLIGHT_COLS} FROM flights f {_FLIGHT_JOIN} WHERE f.id = ?",
        (flight_id,),
    ).fetchone()
    if flight is None:
        raise HTTPException(404, "Flight not found")

    positions = conn.execute(
        """
        SELECT ts, lat, lon, alt_baro, alt_geom, gs, track,
               baro_rate, rssi, source_type
        FROM positions
        WHERE flight_id = ?
        ORDER BY ts
        """,
        (flight_id,),
    ).fetchall()

    # Enrich callsign → airline name
    flight_dict = dict(flight)
    flight_dict["airline_name"] = enrichment.lookup_airline(conn, flight_dict.get("callsign"))

    other_flights = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.icao_hex = ? AND f.id != ?
        ORDER BY f.first_seen DESC
        LIMIT 10
        """,
        (flight["icao_hex"], flight_id),
    ).fetchall()

    return {
        "flight": flight_dict,
        "positions": [dict(p) for p in positions],
        "other_flights": [dict(f) for f in other_flights],
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
    }


# ---------------------------------------------------------------------------
# API — aircraft photo (Planespotters → airport-data.com → hexdb.io, cached)
# ---------------------------------------------------------------------------

# Per-type asyncio locks — prevent concurrent duplicate fetches for the same type.
# Audit-12 #150 — LRU-capped so the dict can't grow without bound across the
# worker's lifetime. ICAO type designators are ~3k distinct in practice; 1024
# is comfortable headroom for hot types while still capping memory.
_TYPE_LOCKS_MAX = 1024
_type_fetch_locks: "_OrderedDict[str, asyncio.Lock]" = _OrderedDict()


def _type_lock(type_code: str) -> asyncio.Lock:
    existing = _type_fetch_locks.get(type_code)
    if existing is not None:
        _type_fetch_locks.move_to_end(type_code)
        return existing
    lock = asyncio.Lock()
    _type_fetch_locks[type_code] = lock
    # Audit-13 A13-004: skip eviction of locks that are currently held.
    # Previously, `popitem(last=False)` could remove a held lock; the
    # next caller for the same type_code would then get a fresh lock
    # object and race the in-progress fetch.
    while len(_type_fetch_locks) > _TYPE_LOCKS_MAX:
        oldest_key = next(iter(_type_fetch_locks))
        oldest_lock = _type_fetch_locks[oldest_key]
        if oldest_lock.locked():
            # Rotate to end so we don't pick it next iteration.
            _type_fetch_locks.move_to_end(oldest_key)
            # Safety net: if every lock is held (impossible in practice
            # with ICAO type designators <~3k), break to avoid infinite
            # rotation.
            if all(lk.locked() for lk in _type_fetch_locks.values()):
                break
            continue
        _type_fetch_locks.pop(oldest_key)
    return lock


async def _fetch_photo(icao_hex: str) -> dict | None:
    """Return the cached or freshly-fetched specific-ICAO photo dict (or None).

    Delegates to :func:`photo_sources.fetch_photo` (full source chain), and
    persists the result — including a negative cache row when all sources
    fail — into the ``photos`` table.  Does NOT cascade to a type-level photo;
    callers do that via :func:`_fetch_type_photo`.
    """
    conn = db()
    cache_seconds = config.PHOTO_CACHE_DAYS * 86400

    cached = conn.execute(
        "SELECT * FROM photos WHERE icao_hex = ? AND fetched_at > ?",
        (icao_hex, int(time.time()) - cache_seconds),
    ).fetchone()
    if cached:
        return dict(cached) if cached["thumbnail_url"] else None

    pr = await asyncio.get_running_loop().run_in_executor(
        None, photo_sources.fetch_photo, icao_hex,
    )
    now = int(time.time())
    if pr:
        result = {
            "icao_hex":      icao_hex,
            "thumbnail_url": pr.thumbnail_url,
            "large_url":     pr.large_url,
            "link_url":      pr.link_url,
            "photographer":  pr.photographer,
            "fetched_at":    now,
        }
        conn.execute(
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?)",
            (icao_hex, pr.thumbnail_url, pr.large_url, pr.link_url, pr.photographer, now),
        )
    else:
        result = None
        # Audit-13 A13-014: don't blow away a previously-resolved positive
        # row on a transient fetch failure. If a stale-but-positive row is
        # within the grace window (cache TTL + 7 days), leave it untouched
        # so the cached URL keeps serving requests; the next successful
        # fetch will refresh it normally. Outside the window, the negative
        # row signals "confirmed unknown" to subsequent lookups.
        grace_seconds = 7 * 86400
        existing = conn.execute(
            "SELECT thumbnail_url, fetched_at FROM photos WHERE icao_hex = ?",
            (icao_hex,),
        ).fetchone()
        if existing and existing["thumbnail_url"] and existing["fetched_at"] > now - cache_seconds - grace_seconds:
            pass  # keep stale positive row
        else:
            conn.execute(
                "INSERT OR REPLACE INTO photos "
                "(icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
                "VALUES (?,NULL,NULL,NULL,NULL,?)",
                (icao_hex, now),
            )
    conn.commit()
    return result


async def _fetch_type_photo(type_code: str | None) -> dict | None:
    """Return a cached or freshly-resolved type-level photo dict (or None).

    Delegates the full ladder (type-cache → photos JOIN aircraft_db → probe one
    ICAO → Wikipedia type lookup) to :func:`photo_sources.resolve_photo` via the
    threadpool.  A per-type asyncio.Lock serialises concurrent gallery requests.
    """
    if not type_code:
        return None

    conn = db()
    cutoff = int(time.time()) - config.PHOTO_CACHE_DAYS * 86400

    # Fast path — cache hit avoids the executor hop entirely.
    cached = conn.execute(
        "SELECT * FROM type_photos WHERE type_code = ? AND fetched_at > ?",
        (type_code, cutoff),
    ).fetchone()
    if cached is not None:
        return dict(cached) if cached["thumbnail_url"] else None

    async with _type_lock(type_code):
        cached = conn.execute(
            "SELECT * FROM type_photos WHERE type_code = ? AND fetched_at > ?",
            (type_code, cutoff),
        ).fetchone()
        if cached is not None:
            return dict(cached) if cached["thumbnail_url"] else None

        def _resolve() -> dict | None:
            # icao_hex="" is the documented type-only mode: resolve_photo skips
            # the specific-aircraft cache check (step 1) and the specific fetch
            # (step 4) so we don't pollute the ``photos`` table with an
            # empty-key row.  ``conn`` is shared across the event-loop thread
            # and this executor worker; Python's sqlite3 per-connection mutex
            # serialises calls but contention is microseconds (no cursor is
            # held across HTTP).  ``database.connect`` uses
            # ``check_same_thread=False`` so cross-thread use is permitted.
            result, _is_type = photo_sources.resolve_photo(
                conn, "", type_code,
                cache_seconds=config.PHOTO_CACHE_DAYS * 86400,
            )
            return result

        return await asyncio.get_running_loop().run_in_executor(None, _resolve)


def _annotate_photo(result: dict | None, *,
                    is_type: bool = False,
                    type_code: str | None = None,
                    type_desc: str | None = None) -> dict | None:
    """Attach is_type_photo / type_code / type_desc fields to a photo result dict."""
    if result is None:
        return None
    return {
        **result,
        "is_type_photo": is_type,
        "type_code":     type_code if is_type else None,
        "type_desc":     type_desc if is_type else None,
    }


@app.get("/api/flights/{flight_id}/photo")
async def api_flight_photo(flight_id: int) -> dict | None:
    row = db().execute(
        """
        SELECT f.icao_hex,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code) AS type_code,
               COALESCE(adb.type_desc, axo.type_desc, f.aircraft_type) AS type_desc
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        WHERE f.id = ?
        """,
        (flight_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404)
    specific = await _fetch_photo(row["icao_hex"])
    if specific:
        return _annotate_photo(specific, is_type=False)
    type_photo = await _fetch_type_photo(row["type_code"])
    return _annotate_photo(type_photo, is_type=True,
                           type_code=row["type_code"], type_desc=row["type_desc"])


# ---------------------------------------------------------------------------
# API — aircraft history
# ---------------------------------------------------------------------------

@app.get("/api/aircraft/{icao_hex}/flights")
def api_aircraft_flights(
    icao_hex: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = db()
    icao = icao_hex.lower().lstrip("~")

    # Single query for count + aggregates (avoids two full scans)
    aggs_row = conn.execute(
        """
        SELECT COUNT(*)                    AS total_flights,
               MIN(first_seen)             AS first_seen,
               MAX(last_seen)              AS last_seen,
               SUM(last_seen - first_seen) AS total_duration_sec
        FROM flights WHERE icao_hex = ?
        """,
        (icao,),
    ).fetchone()
    total = aggs_row["total_flights"] if aggs_row else 0

    order_col = _SORT_COLS.get(sort_by or "first_seen", "f.first_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.icao_hex = ?
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
        """,
        (icao, limit, offset),
    ).fetchall()

    adb_row = conn.execute(
        "SELECT registration, type_code, type_desc, flags FROM aircraft_db WHERE icao_hex = ?",
        (icao,),
    ).fetchone()
    aircraft_info = {**(dict(adb_row) if adb_row else {}), **(dict(aggs_row) if aggs_row else {})}
    aircraft_info["country"] = icao_ranges.icao_to_country(icao)

    return {"total": total, "icao_hex": icao, "aircraft_info": aircraft_info,
            "flights": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# API — flagged aircraft gallery (military + interesting)
# ---------------------------------------------------------------------------

@app.get("/api/aircraft/flagged")
def api_aircraft_flagged(
    flags: str | None = Query(None, description="military | interesting | anonymous"),
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query(None, description="asc | desc"),
) -> dict:
    conn = db()

    flag_expr = _FLAGS_EXPR_F
    if flags == "military":
        flag_filter = f"({flag_expr} & 1) = 1"
    elif flags == "interesting":
        flag_filter = f"({flag_expr} & 2) = 2 AND ({flag_expr} & 1) = 0"
    elif flags == "anonymous":
        # Same precedence rule as the history filter — surface anon-only here.
        flag_filter = f"({flag_expr} & 16) = 16 AND ({flag_expr} & 3) = 0"
    else:
        # Default "all" tab: military | interesting | anonymous
        flag_filter = (
            f"({flag_expr} & {config.FLAG_MILITARY | config.FLAG_INTERESTING | config.FLAG_ANONYMOUS}) != 0"
        )

    # Audit-13 A13-077: shared allowlist at module top so this endpoint
    # follows the same pattern as /api/flights — no inline ad-hoc maps.
    order_col = _FLAGGED_SORT_COLS.get(sort_by or "last_seen", "last_seen")
    order_dir = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    base_joins = """
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
    """

    total = conn.execute(
        f"SELECT COUNT(DISTINCT f.icao_hex) AS cnt {base_joins} WHERE {flag_filter}"
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT
            f.icao_hex,
            COALESCE(f.registration, adb.registration, axo.registration)    AS registration,
            COALESCE(f.aircraft_type, adb.type_code, axo.type_code)         AS aircraft_type,
            COALESCE(adb.type_desc, axo.type_desc, '')                      AS type_desc,
            {flag_expr}                                                     AS flags,
            COUNT(*)                                                        AS flight_count,
            MIN(f.first_seen)                                               AS first_seen,
            MAX(f.last_seen)                                                AS last_seen,
            COALESCE(p.thumbnail_url, tp.thumbnail_url)                     AS thumbnail_url,
            COALESCE(p.large_url,     tp.large_url)                         AS large_url,
            COALESCE(p.link_url,      tp.link_url)                          AS link_url,
            COALESCE(p.photographer,  tp.photographer)                      AS photographer,
            CASE WHEN p.thumbnail_url IS NULL AND tp.thumbnail_url IS NOT NULL
                 THEN 1 ELSE 0 END                                          AS is_type_photo
        {base_joins}
        LEFT JOIN photos p     ON p.icao_hex  = f.icao_hex
        LEFT JOIN type_photos tp
               ON tp.type_code = COALESCE(adb.type_code, axo.type_code, f.aircraft_type)
        WHERE {flag_filter}
        GROUP BY f.icao_hex
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    aircraft = []
    for r in rows:
        d = dict(r)
        d["is_type_photo"] = bool(d["is_type_photo"])
        d["country"] = icao_ranges.icao_to_country(d["icao_hex"])
        aircraft.append(d)

    return {"total": total, "aircraft": aircraft}


@app.get("/api/aircraft/{icao_hex}/photo")
async def api_aircraft_photo(icao_hex: str) -> dict | None:
    icao = icao_hex.lower().lstrip("~")
    row = db().execute(
        """
        SELECT COALESCE(adb.type_code, axo.type_code) AS type_code,
               COALESCE(adb.type_desc, axo.type_desc) AS type_desc
        FROM (SELECT ? AS h) base
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = base.h
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = base.h
        """,
        (icao,),
    ).fetchone()
    type_code = row["type_code"] if row else None
    type_desc = row["type_desc"] if row else None
    specific = await _fetch_photo(icao)
    if specific:
        return _annotate_photo(specific, is_type=False)
    type_photo = await _fetch_type_photo(type_code)
    return _annotate_photo(type_photo, is_type=True, type_code=type_code, type_desc=type_desc)


# ---------------------------------------------------------------------------
# API — statistics
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats(
    from_ts: int | None = Query(None, alias="from"),
    to_ts:   int | None = Query(None, alias="to"),
) -> dict:
    filtered = from_ts is not None or to_ts is not None
    cache_key = "stats" if not filtered else f"stats:{from_ts}:{to_ts}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    conn = db()
    now = int(time.time())
    cutoff_24h      = now - 86400
    cutoff_7d       = now - 7 * 86400
    cutoff_30d      = now - 30 * 86400
    cutoff_prev_24h = now - 2 * 86400
    cutoff_prev_7d  = now - 14 * 86400

    # WHERE fragments injected into all range-aware queries
    if filtered:
        ts_lo = from_ts if from_ts is not None else 0
        ts_hi = to_ts   if to_ts   is not None else now
        _fw   = "WHERE first_seen >= ? AND first_seen <= ?"    # standalone
        _fwa  = "AND first_seen >= ? AND first_seen <= ?"      # appended to existing WHERE
        _fjwa = "AND f.first_seen >= ? AND f.first_seen <= ?"  # appended, aliased table
        _fp   = (ts_lo, ts_hi)
    else:
        _fw = _fwa = _fjwa = ""
        _fp = ()

    # --- Main aggregation ---
    if not filtered:
        # Single pass including relative-window CASE WHENs
        agg = conn.execute(
            """
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                  AS flights_24h,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                  AS flights_7d,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_7d,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct,
                SUM(CASE WHEN max_alt_baro >= 0     AND max_alt_baro < 1000  THEN 1 ELSE 0 END) AS alt_0_1k,
                SUM(CASE WHEN max_alt_baro >= 1000  AND max_alt_baro < 5000  THEN 1 ELSE 0 END) AS alt_1k_5k,
                SUM(CASE WHEN max_alt_baro >= 5000  AND max_alt_baro < 10000 THEN 1 ELSE 0 END) AS alt_5k_10k,
                SUM(CASE WHEN max_alt_baro >= 10000 AND max_alt_baro < 20000 THEN 1 ELSE 0 END) AS alt_10k_20k,
                SUM(CASE WHEN max_alt_baro >= 20000 AND max_alt_baro < 30000 THEN 1 ELSE 0 END) AS alt_20k_30k,
                SUM(CASE WHEN max_alt_baro >= 30000 AND max_alt_baro < 40000 THEN 1 ELSE 0 END) AS alt_30k_40k,
                SUM(CASE WHEN max_alt_baro >= 40000 THEN 1 ELSE 0 END)           AS alt_40k_plus,
                SUM(CASE WHEN squawk = '7700' THEN 1 ELSE 0 END)                 AS squawk_7700,
                SUM(CASE WHEN squawk = '7600' THEN 1 ELSE 0 END)                 AS squawk_7600,
                SUM(CASE WHEN squawk = '7500' THEN 1 ELSE 0 END)                 AS squawk_7500
            FROM flights
            """,
            (cutoff_24h, cutoff_7d, cutoff_prev_24h, cutoff_24h, cutoff_prev_7d, cutoff_7d),
        ).fetchone()
        flights_24h      = agg["flights_24h"]
        flights_7d       = agg["flights_7d"]
        flights_prev_24h = agg["flights_prev_24h"] or 0
        flights_prev_7d  = agg["flights_prev_7d"]  or 0
    else:
        # Filtered: agg over selected range; live window stats from separate query
        agg = conn.execute(
            f"""
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct,
                SUM(CASE WHEN max_alt_baro >= 0     AND max_alt_baro < 1000  THEN 1 ELSE 0 END) AS alt_0_1k,
                SUM(CASE WHEN max_alt_baro >= 1000  AND max_alt_baro < 5000  THEN 1 ELSE 0 END) AS alt_1k_5k,
                SUM(CASE WHEN max_alt_baro >= 5000  AND max_alt_baro < 10000 THEN 1 ELSE 0 END) AS alt_5k_10k,
                SUM(CASE WHEN max_alt_baro >= 10000 AND max_alt_baro < 20000 THEN 1 ELSE 0 END) AS alt_10k_20k,
                SUM(CASE WHEN max_alt_baro >= 20000 AND max_alt_baro < 30000 THEN 1 ELSE 0 END) AS alt_20k_30k,
                SUM(CASE WHEN max_alt_baro >= 30000 AND max_alt_baro < 40000 THEN 1 ELSE 0 END) AS alt_30k_40k,
                SUM(CASE WHEN max_alt_baro >= 40000 THEN 1 ELSE 0 END)           AS alt_40k_plus,
                SUM(CASE WHEN squawk = '7700' THEN 1 ELSE 0 END)                 AS squawk_7700,
                SUM(CASE WHEN squawk = '7600' THEN 1 ELSE 0 END)                 AS squawk_7600,
                SUM(CASE WHEN squawk = '7500' THEN 1 ELSE 0 END)                 AS squawk_7500
            FROM flights {_fw}
            """,
            _fp,
        ).fetchone()
        live = conn.execute(
            """
            SELECT
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                     AS flights_24h,
                SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END)                     AS flights_7d,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_24h,
                SUM(CASE WHEN first_seen > ? AND first_seen <= ? THEN 1 ELSE 0 END) AS flights_prev_7d
            FROM flights
            """,
            (cutoff_24h, cutoff_7d, cutoff_prev_24h, cutoff_24h, cutoff_prev_7d, cutoff_7d),
        ).fetchone()
        flights_24h      = live["flights_24h"]
        flights_7d       = live["flights_7d"]
        flights_prev_24h = live["flights_prev_24h"] or 0
        flights_prev_7d  = live["flights_prev_7d"]  or 0

    adsb_pct  = agg["adsb_pct"]  or 0
    mlat_pct  = agg["mlat_pct"]  or 0
    other_pct = round(100.0 - adsb_pct - mlat_pct, 1)

    # Previous-window deltas. Frontend KPI cards have a `prev` slot that
    # was previously only fed by `trends.flights_*_prev` (24h/7d only),
    # leaving every other range with empty em-dashes. Compute totals over
    # `[ts_lo - D, ts_lo]` where D = current window length so Flights /
    # Unique aircraft / Position fixes all show a real comparison. Skipped
    # for unfiltered (all-time) since there's no equivalent prior window.
    previous_window: dict | None = None
    if filtered:
        window_seconds = ts_hi - ts_lo
        if window_seconds > 0:
            prev_lo = ts_lo - window_seconds
            prev_hi = ts_lo
            # Upper bound is exclusive (`< ts_lo`) so a flight whose
            # first_seen falls on the boundary second is not counted in
            # both windows. The current-window aggregation uses the
            # inclusive `<= ts_hi` form, so this is the only place where
            # the half-open / closed split matters.
            prev_agg = conn.execute(
                """
                SELECT
                    COUNT(*)                  AS total_flights,
                    SUM(total_positions)      AS total_positions,
                    COUNT(DISTINCT icao_hex)  AS unique_aircraft
                FROM flights
                WHERE first_seen >= ? AND first_seen < ?
                """,
                (prev_lo, prev_hi),
            ).fetchone()
            previous_window = {
                "from_ts":         prev_lo,
                "to_ts":           prev_hi,
                "total_flights":   prev_agg["total_flights"] or 0,
                "total_positions": prev_agg["total_positions"] or 0,
                "unique_aircraft": prev_agg["unique_aircraft"] or 0,
            }

    try:
        db_size = os.path.getsize(config.DB_PATH)
    except OSError:
        db_size = None

    # Lifetime block — receiver-wide totals that are NOT scoped to the
    # selected window. The Statistics page's "About this receiver" footer
    # reads from this so it stays stable when the user changes the range
    # picker. When `filtered=False` the main `agg` query is ALREADY over
    # all flights, so we reuse those values; when filtered, a small
    # extra aggregation runs.
    if not filtered:
        lifetime_total_flights    = agg["total_flights"]
        # COALESCE NULL → 0: SUM() returns NULL on an empty `flights`
        # table; the StatsResponse TS interface declares this as `number`
        # (not nullable), so coerce here rather than lie about the type.
        lifetime_total_positions  = agg["total_positions"] or 0
        lifetime_unique_aircraft  = agg["unique_aircraft"]
        lifetime_unique_airlines  = agg["unique_airlines"]
        lifetime_oldest_flight    = agg["oldest_flight"]
        lifetime_adsb_pct         = adsb_pct
        lifetime_mlat_pct         = mlat_pct
    else:
        life = conn.execute(
            """
            SELECT
                COUNT(*)                                                          AS total_flights,
                SUM(total_positions)                                              AS total_positions,
                COUNT(DISTINCT icao_hex)                                          AS unique_aircraft,
                COUNT(DISTINCT CASE
                    WHEN callsign IS NOT NULL AND callsign != '' AND length(callsign) >= 3
                    THEN substr(callsign,1,3) END)                                AS unique_airlines,
                MIN(first_seen)                                                   AS oldest_flight,
                ROUND(100.0 * SUM(adsb_positions) / NULLIF(SUM(total_positions),0), 1) AS adsb_pct,
                ROUND(100.0 * SUM(mlat_positions) / NULLIF(SUM(total_positions),0), 1) AS mlat_pct
            FROM flights
            """,
        ).fetchone()
        lifetime_total_flights    = life["total_flights"]
        # See note in the unfiltered branch — SUM() can return NULL.
        lifetime_total_positions  = life["total_positions"] or 0
        lifetime_unique_aircraft  = life["unique_aircraft"]
        lifetime_unique_airlines  = life["unique_airlines"]
        lifetime_oldest_flight    = life["oldest_flight"]
        lifetime_adsb_pct         = life["adsb_pct"] or 0
        lifetime_mlat_pct         = life["mlat_pct"] or 0
    lifetime_other_pct = round(100.0 - lifetime_adsb_pct - lifetime_mlat_pct, 1)

    # Military + interesting — one JOIN pass (OR-merge tar1090-db + ADSBx flags)
    flags_row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN ({_FLAGS_EXPR_F} & 1) = 1 THEN 1 ELSE 0 END) AS military,
            SUM(CASE WHEN ({_FLAGS_EXPR_F} & 2) = 2
                      AND ({_FLAGS_EXPR_F} & 1) = 0 THEN 1 ELSE 0 END) AS interesting,
            SUM(CASE WHEN ({_FLAGS_EXPR_F} & 16) = 16
                      AND ({_FLAGS_EXPR_F} & 3)  = 0 THEN 1 ELSE 0 END) AS anonymous
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {"WHERE f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        """,
        _fp,
    ).fetchone()

    # Top airlines
    top_airlines = conn.execute(
        f"""
        SELECT
            substr(f.callsign,1,3)      AS airline,
            al.name                     AS airline_name,
            COUNT(*)                    AS flights,
            COUNT(DISTINCT f.icao_hex)  AS unique_aircraft
        FROM flights f
        LEFT JOIN airlines al ON al.icao_code = substr(f.callsign,1,3)
        WHERE f.callsign IS NOT NULL AND length(f.callsign) >= 3 {_fjwa}
        GROUP BY airline
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top aircraft types
    top_types = conn.execute(
        f"""
        SELECT
            COALESCE(f.aircraft_type, adb.type_code)  AS type,
            COALESCE(adb.type_desc, '')                AS type_desc,
            COUNT(*)                                   AS flights,
            COUNT(DISTINCT f.icao_hex)                 AS unique_aircraft
        FROM flights f
        LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex
        WHERE COALESCE(f.aircraft_type, adb.type_code) IS NOT NULL {_fjwa}
        GROUP BY type
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top routes (origin → destination, by flight count)
    top_routes = conn.execute(
        f"""
        SELECT cr.origin_icao,
               cr.dest_icao,
               ap_o.name   AS origin_name,
               ap_d.name   AS dest_name,
               COUNT(*)    AS flights
        FROM flights f
        JOIN callsign_routes cr ON cr.callsign = f.callsign
        LEFT JOIN airports ap_o ON ap_o.icao_code = cr.origin_icao
        LEFT JOIN airports ap_d ON ap_d.icao_code = cr.dest_icao
        WHERE cr.origin_icao IS NOT NULL AND cr.dest_icao IS NOT NULL {_fjwa}
        GROUP BY cr.origin_icao, cr.dest_icao
        ORDER BY flights DESC
        LIMIT 20
        """,
        _fp,
    ).fetchall()

    # Top airports (combined origin + destination appearances)
    top_airports = conn.execute(
        f"""
        SELECT icao_code, name, country, SUM(cnt) AS appearances
        FROM (
            SELECT ap_o.icao_code, ap_o.name, ap_o.country, COUNT(*) AS cnt
            FROM flights f
            JOIN callsign_routes cr ON cr.callsign = f.callsign
            JOIN airports ap_o ON ap_o.icao_code = cr.origin_icao
            WHERE cr.origin_icao IS NOT NULL {_fjwa}
            GROUP BY ap_o.icao_code
            UNION ALL
            SELECT ap_d.icao_code, ap_d.name, ap_d.country, COUNT(*) AS cnt
            FROM flights f
            JOIN callsign_routes cr ON cr.callsign = f.callsign
            JOIN airports ap_d ON ap_d.icao_code = cr.dest_icao
            WHERE cr.dest_icao IS NOT NULL {_fjwa}
            GROUP BY ap_d.icao_code
        )
        GROUP BY icao_code
        ORDER BY appearances DESC
        LIMIT 20
        """,
        _fp + _fp,
    ).fetchall()

    # Hourly distribution
    hourly = conn.execute(
        f"""
        SELECT CAST(strftime('%H', first_seen, 'unixepoch') AS INTEGER) AS hour,
               COUNT(*) AS count
        FROM flights {_fw}
        GROUP BY hour
        ORDER BY hour
        """,
        _fp,
    ).fetchall()
    hourly_map = {r["hour"]: r["count"] for r in hourly}
    hourly_dist = [{"hour": h, "count": hourly_map.get(h, 0)} for h in range(24)]

    # Daily unique aircraft
    if not filtered:
        daily = conn.execute(
            """
            SELECT date(first_seen, 'unixepoch') AS day,
                   COUNT(DISTINCT icao_hex) AS unique_aircraft,
                   COUNT(*) AS flights
            FROM flights
            WHERE first_seen > ?
            GROUP BY day
            ORDER BY day ASC
            LIMIT 31
            """,
            # LIMIT 31 not 30: cutoff_30d is `now - 30*86400` (an instant,
            # not a day boundary), so the WHERE typically straddles 31
            # distinct UTC date strings (partial start day + 30 full days
            # ending today). With ASC ordering, LIMIT 30 would truncate
            # today's bar — the most user-relevant one.
            (cutoff_30d,),
        ).fetchall()
    else:
        daily = conn.execute(
            """
            SELECT date(first_seen, 'unixepoch') AS day,
                   COUNT(DISTINCT icao_hex) AS unique_aircraft,
                   COUNT(*) AS flights
            FROM flights
            WHERE first_seen >= ? AND first_seen <= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (ts_lo, ts_hi),
        ).fetchall()

    # New aircraft — first seen within the window
    if not filtered:
        new_having   = "HAVING MIN(f.first_seen) > ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) > ?)"
        new_params   = (cutoff_24h,)
    else:
        new_having   = "HAVING MIN(f.first_seen) >= ? AND MIN(f.first_seen) <= ?"
        new_cnt_sql  = "SELECT COUNT(*) FROM (SELECT icao_hex FROM flights GROUP BY icao_hex HAVING MIN(first_seen) >= ? AND MIN(first_seen) <= ?)"
        new_params   = (ts_lo, ts_hi)

    new_aircraft_rows = conn.execute(
        f"""
        SELECT sub.icao_hex,
               COALESCE(f2.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f2.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               COALESCE(adb.type_desc, axo.type_desc, '')                   AS type_desc,
               {_FLAGS_EXPR_SUB}                                            AS flags,
               sub.first_seen_ever
        FROM (
            SELECT f.icao_hex, MIN(f.first_seen) AS first_seen_ever
            FROM flights f
            GROUP BY f.icao_hex
            {new_having}
            ORDER BY MIN(f.first_seen) DESC
            LIMIT 10
        ) sub
        LEFT JOIN flights         f2  ON f2.icao_hex = sub.icao_hex AND f2.first_seen = sub.first_seen_ever
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = sub.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = sub.icao_hex
        ORDER BY sub.first_seen_ever DESC
        """,
        new_params,
    ).fetchall()
    new_aircraft_total = conn.execute(new_cnt_sql, new_params).fetchone()[0] or 0

    # Most frequent aircraft
    frequent_aircraft = conn.execute(
        f"""
        SELECT f.icao_hex,
               COALESCE(f.registration, adb.registration, axo.registration)  AS registration,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code)       AS aircraft_type,
               COALESCE(adb.type_desc, axo.type_desc, '')                    AS type_desc,
               {_FLAGS_EXPR_F}                                              AS flags,
               COUNT(*)                                    AS flights,
               MAX(f.last_seen)                            AS last_seen
        FROM flights f
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        {"WHERE f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        GROUP BY f.icao_hex
        ORDER BY flights DESC
        LIMIT 15
        """,
        _fp,
    ).fetchall()

    # Country breakdown — aggregated entirely in SQL using pre-built CASE expression
    top_countries = [dict(r) for r in conn.execute(
        f"""
        SELECT {icao_ranges.COUNTRY_SQL_CASE} AS country,
               COUNT(*)                        AS flights,
               COUNT(DISTINCT icao_hex)        AS unique_aircraft
        FROM flights {_fw}
        GROUP BY country
        ORDER BY flights DESC
        LIMIT 15
        """,
        _fp,
    ).fetchall()]

    # Activity heatmap
    heatmap_rows = conn.execute(
        f"""
        SELECT CAST(strftime('%w', first_seen, 'unixepoch') AS INT) AS dow,
               CAST(strftime('%H', first_seen, 'unixepoch') AS INT) AS hour,
               COUNT(*) AS count
        FROM flights {_fw}
        GROUP BY dow, hour
        ORDER BY dow, hour
        """,
        _fp,
    ).fetchall()

    # Furthest detected aircraft. Sprint 1 #4: surface the record-set
    # timestamp under the explicit `record_set_at` key so the frontend
    # MaxRangeCard sublabel can render `{callsign} · set {date}`. This is
    # the `first_seen` of the flight that holds the max-distance record
    # (the flight could span hours; `first_seen` is when it started).
    furthest_row = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.max_distance_nm IS NOT NULL {"AND f.first_seen >= ? AND f.first_seen <= ?" if filtered else ""}
        ORDER BY f.max_distance_nm DESC
        LIMIT 1
        """,
        _fp,
    ).fetchone()
    if furthest_row:
        furthest = dict(furthest_row)
        # Rename, don't duplicate: keep a single timestamp key in the
        # response so a future cleanup of `first_seen` from the projection
        # doesn't leave a stale alias behind. No external consumer reads
        # the original `first_seen` from `furthest_aircraft` — the
        # Personal Records section uses `/api/stats/records` instead.
        furthest["record_set_at"] = furthest.pop("first_seen", None)
    else:
        furthest = None

    result = {
        "total_flights":           agg["total_flights"],
        "total_positions":         agg["total_positions"],
        "unique_aircraft":         agg["unique_aircraft"],
        "unique_airlines":         agg["unique_airlines"],
        "db_size_bytes":           db_size,
        "oldest_flight":           agg["oldest_flight"],
        "flights_last_24h":        flights_24h,
        "flights_last_7d":         flights_7d,
        "source_breakdown":        {"adsb": adsb_pct, "mlat": mlat_pct, "other": other_pct},
        "top_airlines":            [dict(r) for r in top_airlines],
        "top_aircraft_types":      [dict(r) for r in top_types],
        "hourly_distribution":     hourly_dist,
        "daily_unique_aircraft":   [dict(r) for r in daily],
        "altitude_distribution":   [
            {"band": "Ground / <1k", "count": agg["alt_0_1k"]   or 0},
            {"band": "1k–5k",        "count": agg["alt_1k_5k"]  or 0},
            {"band": "5k–10k",       "count": agg["alt_5k_10k"] or 0},
            {"band": "10k–20k",      "count": agg["alt_10k_20k"] or 0},
            {"band": "20k–30k",      "count": agg["alt_20k_30k"] or 0},
            {"band": "30k–40k",      "count": agg["alt_30k_40k"] or 0},
            {"band": "40k+",         "count": agg["alt_40k_plus"] or 0},
        ],
        "military_flights":        flags_row["military"]     or 0,
        "interesting_flights":     flags_row["interesting"]  or 0,
        "anonymous_flights":       flags_row["anonymous"]    or 0,
        "squawk_counts":           {
            "7700": agg["squawk_7700"] or 0,
            "7600": agg["squawk_7600"] or 0,
            "7500": agg["squawk_7500"] or 0,
        },
        "new_aircraft":            {"total": new_aircraft_total, "items": [dict(r) for r in new_aircraft_rows]},
        "furthest_aircraft":       furthest,
        "receiver_lat":            config.RECEIVER_LAT,
        "receiver_lon":            config.RECEIVER_LON,
        "trends": {
            "flights_24h_prev": flights_prev_24h,
            "flights_7d_prev":  flights_prev_7d,
        },
        "previous_window": previous_window,
        "lifetime": {
            "total_flights":    lifetime_total_flights,
            "total_positions":  lifetime_total_positions,
            "unique_aircraft":  lifetime_unique_aircraft,
            "unique_airlines":  lifetime_unique_airlines,
            "oldest_flight":    lifetime_oldest_flight,
            "db_size_bytes":    db_size,
            "source_breakdown": {
                "adsb":  lifetime_adsb_pct,
                "mlat":  lifetime_mlat_pct,
                "other": lifetime_other_pct,
            },
        },
        "heatmap": [dict(r) for r in heatmap_rows],
        "top_countries": top_countries,
        "frequent_aircraft": [dict(r) for r in frequent_aircraft],
        "top_routes":   [dict(r) for r in top_routes],
        "top_airports": [dict(r) for r in top_airports],
        "range":  {"from": from_ts, "to": to_ts} if filtered else None,
    }
    _set_cache(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# API — all-time personal records
# ---------------------------------------------------------------------------

@app.get("/api/stats/records")
def api_stats_records() -> dict:
    """All-time personal records: furthest / fastest / highest / longest flight."""
    cached = _get_cache("records")
    if cached is not None:
        return cached

    conn = db()

    # Audit-13 A13-040: previously accepted any string as `order_col` and
    # f-stringed it into SQL — latent SQLi if a future caller forwarded a
    # query param. Explicit allowlist enforced at function entry.
    _TOP1_ALLOWLIST = frozenset({"max_distance_nm", "max_gs", "max_alt_baro"})

    def _top1(order_col: str, extra_where: str = "", extra_params: tuple = ()) -> dict | None:
        if order_col not in _TOP1_ALLOWLIST:
            raise ValueError(f"unsupported order column: {order_col!r}")
        row = conn.execute(
            f"""
            SELECT {_FLIGHT_COLS}
            FROM flights f {_FLIGHT_JOIN}
            WHERE f.{order_col} IS NOT NULL {extra_where}
            ORDER BY f.{order_col} DESC
            LIMIT 1
            """,
            extra_params,
        ).fetchone()
        return dict(row) if row else None

    furthest = _top1("max_distance_nm")
    # Audit-13 A13-050: parameterise MAX_GS_* numerics (previously f-stringed).
    fastest  = _top1(
        "max_gs",
        "AND f.max_gs <= CASE WHEN (COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1 != 0"
        " THEN ? ELSE ? END",
        (config.MAX_GS_MILITARY_KTS, config.MAX_GS_CIVIL_KTS),
    )
    highest  = _top1("max_alt_baro")

    longest_row = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}, (f.last_seen - f.first_seen) AS duration_s
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.last_seen > f.first_seen
        ORDER BY duration_s DESC
        LIMIT 1
        """
    ).fetchone()
    longest = dict(longest_row) if longest_row else None

    result = {
        "furthest": furthest,
        "fastest":  fastest,
        "highest":  highest,
        "longest":  longest,
    }
    _set_cache("records", result)
    return result


# ---------------------------------------------------------------------------
# API — airspace GeoJSON overlay
# ---------------------------------------------------------------------------

@app.get("/api/airspace")
def api_airspace() -> dict:
    """Serve the configured airspace GeoJSON (default: bundled poland.geojson)."""
    cached = _cache.get("airspace")
    if cached and time.time() - cached[0] < _AIRSPACE_TTL:
        return cached[1]

    path = config.AIRSPACE_GEOJSON or str(BASE_DIR / "static" / "airspace" / "poland.geojson")
    # improvements.md #73: env-set paths must resolve to a regular file so a
    # misconfiguration can't make us read /dev/random or follow a symlink to
    # an unintended target.  A13-041 size cap stays in place below.
    _AIRSPACE_MAX_BYTES = 10 * 1024 * 1024
    data = {"type": "FeatureCollection", "features": []}
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        log.warning("Airspace path %r could not be resolved (%s); serving empty", path, exc)
    else:
        if not resolved.is_file():
            log.warning("Airspace path %s is not a regular file; serving empty", resolved)
        else:
            try:
                size = resolved.stat().st_size
                if size > _AIRSPACE_MAX_BYTES:
                    log.warning("Airspace file %s is %d bytes — over %d-byte limit; serving empty",
                                resolved, size, _AIRSPACE_MAX_BYTES)
                else:
                    with open(resolved) as fh:
                        data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Failed to load airspace from %s: %s", resolved, exc)

    _cache["airspace"] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------
# API — polar range plot
# ---------------------------------------------------------------------------

@app.get("/api/stats/polar")
def api_stats_polar() -> dict:
    """Max detection range per 10° azimuth bucket (36 buckets, 0 = North)."""
    cached = _get_cache("polar")
    if cached is not None:
        return cached

    # BUCKET_DEG controls angular resolution of the polar plot.
    # 10° (36 buckets) is a good default.
    BUCKET_DEG = 10
    n_buckets = 360 // BUCKET_DEG

    # Use precomputed max_distance_bearing from flights table (much faster
    # than scanning millions of position rows with trig math).
    sql_rows = db().execute(
        f"""
        SELECT
            CAST(max_distance_bearing / {BUCKET_DEG} AS INT) AS bucket,
            MAX(max_distance_nm) AS max_dist_nm
        FROM flights
        WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NOT NULL
        GROUP BY bucket
        """
    ).fetchall()

    buckets: list[float] = [0.0] * n_buckets
    for r in sql_rows:
        if r["bucket"] is not None:
            buckets[int(r["bucket"]) % n_buckets] = r["max_dist_nm"] or 0.0

    result = {
        "buckets": [
            {"bearing": i * BUCKET_DEG, "max_dist_nm": round(buckets[i], 1)}
            for i in range(n_buckets)
        ]
    }
    _set_cache("polar", result)
    return result


# ---------------------------------------------------------------------------
# API — position density heatmap
# ---------------------------------------------------------------------------

_HEATMAP_WINDOWS: dict[str, int | None] = {
    "24h": 86_400,
    "7d":  7 * 86_400,
    "30d": 30 * 86_400,
    "all": None,
}
# 30d/all scan millions of rows — use coarser grid (0.1° ≈ 11 km) to keep
# GROUP BY small enough for a Pi 4.  24h/7d use fine grid (0.01° ≈ 1 km).
_HEATMAP_PRECISION: dict[str, int] = {
    "24h": 2,
    "7d":  2,
    "30d": 1,
    "all": 1,
}


def _compute_heatmap_sync(window: str) -> dict:
    """Run the heavy aggregation query — call via run_in_executor to avoid blocking the event loop.

    Tries the DuckDB engine first (gated by `analytics.is_available()`);
    falls through to a SQLite query on unavailable or per-query failure.
    Both paths feed into shared post-processing so the response shape is
    identical."""
    precision = _HEATMAP_PRECISION[window]
    secs = _HEATMAP_WINDOWS[window]
    cutoff = (int(time.time()) - secs) if secs is not None else None

    try:
        rows: list[tuple[float, float, int]] | None = analytics.heatmap(cutoff, precision)
    except Exception:  # noqa: BLE001 — belt-and-suspenders: SQLite path must still answer
        log.warning("analytics.heatmap raised; falling back to SQLite", exc_info=True)
        rows = None
    if rows is None:
        params: list = []
        extra = ""
        if cutoff is not None:
            extra = "AND ts > ?"
            params.append(cutoff)
        # improvements.md A13-019: GROUP BY integer bucket (FLOOR(x*10^p + 0.5))
        # and divide in Python so this path agrees bucket-for-bucket with the
        # DuckDB heatmap.  Raw `round()` differs across engines (SQLite is
        # half-away-from-zero, DuckDB is banker's) and even an explicit
        # `FLOOR/scale` on the SQL side picks up a per-engine float drift on
        # the divide step.
        scale = 10 ** precision
        sqlite_rows = db().execute(
            f"""
            SELECT CAST(FLOOR(lat * {scale} + 0.5) AS INTEGER) AS lat_bucket,
                   CAST(FLOOR(lon * {scale} + 0.5) AS INTEGER) AS lon_bucket,
                   COUNT(*) AS w
            FROM positions
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              {extra}
            GROUP BY lat_bucket, lon_bucket
            """,
            params,
        ).fetchall()
        rows = [(r["lat_bucket"] / scale, r["lon_bucket"] / scale, r["w"]) for r in sqlite_rows]

    if not rows:
        return {"points": [], "window": window, "count": 0}

    max_w = max(r[2] for r in rows)
    return {
        "points": [[r[0], r[1], r[2] / max_w] for r in rows],
        "window": window,
        "count": sum(r[2] for r in rows),
    }


_heatmap_locks: dict[str, asyncio.Lock] = {}


def _heatmap_lock(window: str) -> asyncio.Lock:
    if window not in _heatmap_locks:
        _heatmap_locks[window] = asyncio.Lock()
    return _heatmap_locks[window]


@app.get("/api/map/heatmap")
async def api_map_heatmap(window: str = Query("7d")) -> dict:
    """Return position density grid for Leaflet.heat overlay.

    Intensities are normalised so the densest cell = 1.0.
    Fine grid (0.01°) for 24h/7d; coarse grid (0.1°) for 30d/all.
    """
    if window not in _HEATMAP_WINDOWS:
        raise HTTPException(
            400, f"window must be one of: {', '.join(_HEATMAP_WINDOWS)}"
        )

    cache_key = f"heatmap:{window}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    async with _heatmap_lock(window):
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached
        result = await asyncio.get_running_loop().run_in_executor(
            None, _compute_heatmap_sync, window
        )
        _set_cache(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# API — coverage range outline
# ---------------------------------------------------------------------------

_BUCKET_DEG = 10
_NUM_BUCKETS = 360 // _BUCKET_DEG


def _compute_coverage_sync(window: str) -> dict:
    """Compute per-bearing max-range polygon from raw positions — call via run_in_executor.

    Bearing and haversine distance are computed per-position in SQL so each 10° bucket
    reflects the actual farthest position recorded in that direction, not just the single
    furthest-point bearing stored on the flight row.  DuckDB engine first (when available);
    SQLite fallback on unavailable or per-query failure.
    """
    secs = _HEATMAP_WINDOWS[window]
    cutoff = (int(time.time()) - secs) if secs is not None else None

    try:
        by_bucket = analytics.coverage(cutoff, config.RECEIVER_LAT, config.RECEIVER_LON, _BUCKET_DEG)
    except Exception:  # noqa: BLE001
        log.warning("analytics.coverage raised; falling back to SQLite", exc_info=True)
        by_bucket = None
    if by_bucket is None:
        params: dict = {"rlat": config.RECEIVER_LAT, "rlon": config.RECEIVER_LON}
        extra = ""
        if cutoff is not None:
            extra = "AND ts > :cutoff"
            params["cutoff"] = cutoff

        # Audit-13 A13-076: shared SQL helpers — single source of truth.
        bearing_expr = geo.bearing_sql("lat", "lon", ":rlat", ":rlon")
        dist_expr    = geo.haversine_sql("lat", "lon", ":rlat", ":rlon")
        rows = db().execute(
            f"""
            WITH pos_bearing AS (
                SELECT
                    {bearing_expr} AS bearing_deg,
                    {dist_expr}    AS dist_nm
                FROM positions
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  {extra}
            )
            SELECT
                CAST(bearing_deg / {_BUCKET_DEG}.0 AS INT) % {_NUM_BUCKETS} AS bucket,
                MAX(dist_nm) AS max_dist
            FROM pos_bearing
            GROUP BY bucket
            """,
            params,
        ).fetchall()
        by_bucket = {r["bucket"]: r["max_dist"] for r in rows}

    polygon: list[list[float]] = []
    for i in range(_NUM_BUCKETS):
        dist = by_bucket.get(i, 0.0)
        if dist > 0:
            lat, lon = geo.destination_point(
                config.RECEIVER_LAT, config.RECEIVER_LON, float(i * _BUCKET_DEG), dist
            )
        else:
            lat, lon = config.RECEIVER_LAT, config.RECEIVER_LON
        polygon.append([lat, lon])

    max_range = max(by_bucket.values(), default=0.0)
    return {"polygon": polygon, "max_range_nm": max_range, "window": window}


_coverage_locks: dict[str, asyncio.Lock] = {}


def _coverage_lock(window: str) -> asyncio.Lock:
    if window not in _coverage_locks:
        _coverage_locks[window] = asyncio.Lock()
    return _coverage_locks[window]


@app.get("/api/map/coverage")
async def api_map_coverage(window: str = Query("7d")) -> dict:
    """Return receiver coverage polygon for Leaflet overlay.

    Each of 36 bearing buckets (10° each) contains the max detection range
    in that direction, projected to a lat/lon point.  Buckets with no data
    collapse to the receiver location, pulling the polygon inward.
    """
    if window not in _HEATMAP_WINDOWS:
        raise HTTPException(
            400, f"window must be one of: {', '.join(_HEATMAP_WINDOWS)}"
        )

    cache_key = f"coverage:{window}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    async with _coverage_lock(window):
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached
        result = await asyncio.get_running_loop().run_in_executor(
            None, _compute_coverage_sync, window
        )
        _set_cache(cache_key, result)
        return result


# ---------------------------------------------------------------------------
# Background prewarmer — keep heatmap+coverage caches hot so users never pay
# the cold-scan latency. Each entry is refreshed at half its TTL so the
# cache is renewed well before users could see an expiry.
# ---------------------------------------------------------------------------

_PREWARM_TARGETS: list[tuple[str, str]] = [
    ("heatmap", "24h"), ("heatmap", "7d"),  ("heatmap", "30d"),  ("heatmap", "all"),
    ("coverage", "24h"), ("coverage", "7d"), ("coverage", "30d"), ("coverage", "all"),
]

# Stagger gap between the first refresh of consecutive targets. With 8
# targets and a 15s gap, the initial burst is spread across ~105s instead
# of all 8 contending immediately at process startup (audit-12 #185).
_PREWARM_INITIAL_STAGGER_S = 15

# TTL-priority order for the initial schedule: shortest-TTL windows are
# the ones a user is most likely to hit first, so they run first. The
# longest-TTL ("all") windows are slowest to compute and rarely hit cold —
# they can wait.
_PREWARM_TTL_PRIORITY = {"24h": 0, "7d": 1, "30d": 2, "all": 3}


def _initial_prewarm_schedule(
    targets: list[tuple[str, str]],
    *,
    now: float,
) -> dict[tuple[str, str], float]:
    """Return ``{(kind, window): epoch_seconds}`` mapping each target to its
    desired *first* refresh time. Earliest first is ``now``; subsequent
    targets are spaced by ``_PREWARM_INITIAL_STAGGER_S``. Targets are
    ordered by TTL ascending (shortest-window = most-user-hit = first),
    with kind as a tiebreaker so heatmap runs before coverage at each TTL.

    Pulled out so we can unit-test the ordering without spinning up the
    thread or sleeping for real time.
    """
    ordered = sorted(
        targets,
        key=lambda kw: (_PREWARM_TTL_PRIORITY.get(kw[1], 99), 0 if kw[0] == "heatmap" else 1),
    )
    return {kw: now + i * _PREWARM_INITIAL_STAGGER_S for i, kw in enumerate(ordered)}


_prewarmer_stop = threading.Event()
_prewarmer_thread: threading.Thread | None = None


def _prewarm_one(kind: str, window: str) -> None:
    """Run the heavy compute for one (kind, window) and populate the cache.
    Cheap to call from any thread — the compute helpers open per-thread DB
    connections via `db()` and the cache dict is set-only (no race risk
    beyond a last-writer-wins value swap)."""
    result = _compute_heatmap_sync(window) if kind == "heatmap" else _compute_coverage_sync(window)
    _set_cache(f"{kind}:{window}", result)


def _prewarm_loop() -> None:
    """Refresh one target per pass with a cool-off between heavy queries.

    The cool-off prevents 8 back-to-back full-table scans from saturating
    the web service for 60+ s on startup — the collector and incoming user
    requests both need a slice of CPU. Steady-state refreshes are sparse
    (half-TTL: 150 s for 24h, 10800 s for `all`) so the thread spends most
    of its life sleeping.
    """
    # Staggered initial schedule — see _initial_prewarm_schedule().
    next_at: dict[tuple[str, str], float] = _initial_prewarm_schedule(
        _PREWARM_TARGETS, now=time.time(),
    )
    if _prewarmer_stop.wait(5):
        return

    while not _prewarmer_stop.is_set():
        target = min(_PREWARM_TARGETS, key=lambda kw: next_at[kw])
        kind, window = target
        wait_for = next_at[target] - time.time()
        if wait_for > 0:
            if _prewarmer_stop.wait(min(wait_for, 60)):
                return
            continue

        try:
            _prewarm_one(kind, window)
            ttl = _CACHE_TTLS.get(f"{kind}:{window}", _DEFAULT_TTL)
            next_at[target] = time.time() + max(ttl // 2, 60)
            log.debug("prewarm: refreshed %s:%s (next in %ds)",
                      kind, window, max(ttl // 2, 60))
        except Exception:  # noqa: BLE001 — must not kill the thread
            log.warning("prewarm: %s:%s failed; retry in 5 min",
                        kind, window, exc_info=True)
            next_at[target] = time.time() + 300

        if _prewarmer_stop.wait(10):
            return


def _start_prewarmer() -> None:
    global _prewarmer_thread
    if _prewarmer_thread is not None and _prewarmer_thread.is_alive():
        return
    _prewarmer_stop.clear()
    _prewarmer_thread = threading.Thread(
        target=_prewarm_loop, name="map-prewarm", daemon=True
    )
    _prewarmer_thread.start()


def _stop_prewarmer() -> None:
    global _prewarmer_thread
    _prewarmer_stop.set()
    _prewarmer_thread = None


# ---------------------------------------------------------------------------
# API — live (currently tracked aircraft)
# ---------------------------------------------------------------------------

@app.get("/api/live")
def api_live() -> dict:
    """
    Audit-13 A13-069: single query (was two — fetch IDs, then bind into an
    IN-clause). The correlated subquery uses idx_positions_flight_id_desc
    on (flight_id, id DESC), so each per-flight position lookup is
    O(log n) without materialising a Python list of active IDs.
    """
    conn = db()
    rows = conn.execute(
        f"""
        SELECT af.icao_hex, af.flight_id, af.last_seen,
               f.callsign,
               COALESCE(f.registration, adb.registration, axo.registration) AS registration,
               COALESCE(f.aircraft_type, adb.type_code, axo.type_code)     AS aircraft_type,
               {_FLAGS_EXPR_AF}                                            AS flags,
               f.primary_source,
               cr.origin_icao,
               cr.dest_icao,
               p.lat,
               p.lon
        FROM active_flights af
        JOIN flights f ON f.id = af.flight_id
        LEFT JOIN aircraft_db      adb ON adb.icao_hex  = af.icao_hex
        LEFT JOIN adsbx_overrides  axo ON axo.icao_hex  = af.icao_hex
        LEFT JOIN callsign_routes  cr  ON cr.callsign   = f.callsign
        LEFT JOIN positions p ON p.id = (
            SELECT id FROM positions
            WHERE flight_id = af.flight_id
              AND lat IS NOT NULL
              AND lon IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
        )
        ORDER BY af.last_seen DESC
        """
    ).fetchall()
    now = int(time.time())
    aircraft = []
    for r in rows:
        d = dict(r)
        d["seconds_ago"] = now - r["last_seen"]
        aircraft.append(d)
    return {
        "now": now,
        "count": len(rows),
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
        "aircraft": aircraft,
    }


# ---------------------------------------------------------------------------
# API — historical map snapshot
# ---------------------------------------------------------------------------

_MAP_WINDOW_SEC = 600  # flight must have a position within this window of `at`


@app.get("/api/map/snapshot")
def api_map_snapshot(
    at: int | None = Query(None, description="Unix timestamp (default: now → live mode)"),
    trail: int = Query(10, ge=0, description="Trail positions per aircraft (capped at 50)"),
) -> dict:
    now = int(time.time())

    if at is None:
        at = now
        is_live = True
    else:
        if at > now + 60:
            raise HTTPException(400, "at timestamp cannot be in the future")
        oldest = now - config.MAP_HISTORY_HOURS * 3600
        if at < oldest:
            raise HTTPException(
                400,
                f"at timestamp exceeds history limit ({config.MAP_HISTORY_HOURS}h)",
            )
        is_live = abs(at - now) <= 30

    trail_count = min(trail, 50)
    conn = db()

    rows = conn.execute(
        f"""
        WITH af AS (
            SELECT flight_id, MAX(id) AS lid
            FROM positions
            WHERE ts BETWEEN ? AND ?
              AND lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY flight_id
        )
        SELECT p.flight_id, p.ts, p.lat, p.lon, p.alt_baro, p.gs, p.track,
               p.source_type,
               f.icao_hex, f.callsign, f.registration, f.aircraft_type,
               f.category, f.primary_source,
               {_FLAGS_EXPR_F} AS flags,
               cr.origin_icao, cr.dest_icao
        FROM af
        JOIN positions p ON p.id = af.lid
        JOIN flights f ON f.id = p.flight_id
        LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
        LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
        LEFT JOIN callsign_routes cr  ON cr.callsign  = f.callsign
        """,
        (at - _MAP_WINDOW_SEC, at),
    ).fetchall()

    aircraft = []
    for r in rows:
        d = dict(r)
        d["seconds_ago"] = at - r["ts"]
        aircraft.append(d)

    if trail_count > 0 and aircraft:
        flight_ids = [r["flight_id"] for r in aircraft]
        placeholders = ",".join("?" * len(flight_ids))
        trail_rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT flight_id, ts, lat, lon,
                       ROW_NUMBER() OVER (PARTITION BY flight_id ORDER BY ts DESC) AS rn
                FROM positions
                WHERE flight_id IN ({placeholders})
                  AND ts <= ?
                  AND lat IS NOT NULL AND lon IS NOT NULL
            )
            SELECT flight_id, ts, lat, lon FROM ranked WHERE rn <= ?
            ORDER BY flight_id, ts
            """,
            [*flight_ids, at, trail_count],
        ).fetchall()

        trail_by_flight: dict[int, list] = {}
        for tr in trail_rows:
            fid = tr["flight_id"]
            if fid not in trail_by_flight:
                trail_by_flight[fid] = []
            trail_by_flight[fid].append([tr["lat"], tr["lon"], tr["ts"]])

        for ac in aircraft:
            ac["trail"] = trail_by_flight.get(ac["flight_id"], [])
    else:
        for ac in aircraft:
            ac["trail"] = []

    return {
        "at":           at,
        "is_live":      is_live,
        "receiver_lat": config.RECEIVER_LAT,
        "receiver_lon": config.RECEIVER_LON,
        "aircraft":     aircraft,
    }


# ---------------------------------------------------------------------------
# API — date index
# ---------------------------------------------------------------------------

@app.get("/api/dates")
def api_dates() -> dict:
    cached = _get_cache("dates")
    if cached is not None:
        return cached
    conn = db()
    rows = conn.execute(
        """
        SELECT date(first_seen, 'unixepoch', 'localtime') AS date,
               COUNT(*) AS flight_count
        FROM flights
        GROUP BY date
        ORDER BY date DESC
        LIMIT 365
        """
    ).fetchall()
    result = {"dates": [dict(r) for r in rows]}
    _set_cache("dates", result)
    return result


# ---------------------------------------------------------------------------
# API — airline and type drill-downs
# ---------------------------------------------------------------------------

@app.get("/api/airlines/{prefix}/flights")
def api_airline_flights(
    prefix: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = db()
    p = prefix.upper()[:3]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f WHERE f.callsign LIKE ?",
        (p + "%",),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE f.callsign LIKE ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (p + "%", limit, offset),
    ).fetchall()
    return {"total": total, "airline": p, "flights": [dict(r) for r in rows]}


@app.get("/api/types/{aircraft_type}/flights")
def api_type_flights(
    aircraft_type: str,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=500_000),
) -> dict:
    conn = db()
    t = aircraft_type.upper()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM flights f "
        "LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex "
        "WHERE COALESCE(f.aircraft_type, adb.type_code) = ?",
        (t,),
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_FLIGHT_COLS}
        FROM flights f {_FLIGHT_JOIN}
        WHERE COALESCE(f.aircraft_type, adb.type_code) = ?
        ORDER BY f.first_seen DESC
        LIMIT ? OFFSET ?
        """,
        (t, limit, offset),
    ).fetchall()
    return {"total": total, "aircraft_type": t, "flights": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# API — receiver metrics time-series
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
def api_metrics(
    from_ts: int | None = Query(None, alias="from"),
    to_ts:   int | None = Query(None, alias="to"),
    metrics: str = "signal,noise",
) -> dict:
    """
    Return receiver metrics as columnar arrays (uPlot-native format).

    Query params:
        from   — start epoch (default: 24 h ago)
        to     — end epoch (default: now)
        metrics — comma-separated column names from _METRICS_COLS

    Non-integer `from` / `to` are rejected at the FastAPI layer with HTTP 422
    rather than the 500 the old `int(request.query_params.get(...))` path
    produced.  See improvements.md #115.
    """
    now = int(time.time())
    if from_ts is None:
        from_ts = now - 86400
    if to_ts is None:
        to_ts = now

    # Validate requested columns against allowlist
    requested = [c.strip() for c in metrics.split(",") if c.strip()]
    invalid = [c for c in requested if c not in _METRICS_COLS]
    if invalid:
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown metrics: {', '.join(invalid)}"},
        )
    if not requested:
        return {"bucket_seconds": 0, "metrics": [], "data": []}

    # Auto-downsampling based on time range span
    span = to_ts - from_ts
    if span <= 86_400:          # <= 24 h: raw
        bucket = 0
    elif span <= 604_800:       # <= 7 d: 5-min buckets
        bucket = 300
    elif span <= 2_592_000:     # <= 30 d: 15-min buckets
        bucket = 900
    elif span <= 7_776_000:     # <= 90 d: 1-hour buckets
        bucket = 3600
    else:                       # > 90 d: 4-hour buckets
        bucket = 14400

    conn = db()
    if bucket == 0:
        cols_sql = ", ".join(requested)
        sql = (
            f"SELECT ts, {cols_sql} FROM receiver_stats "
            f"WHERE ts BETWEEN ? AND ? ORDER BY ts"
        )
        rows = conn.execute(sql, (from_ts, to_ts)).fetchall()
    else:
        agg_cols = ", ".join(_metrics_agg(c) for c in requested)
        sql = (
            f"SELECT (ts / {bucket}) * {bucket} AS bucket_ts, {agg_cols} "
            f"FROM receiver_stats WHERE ts BETWEEN ? AND ? "
            f"GROUP BY bucket_ts ORDER BY bucket_ts"
        )
        rows = conn.execute(sql, (from_ts, to_ts)).fetchall()

    # Build columnar arrays: [[ts, ...], [metric1, ...], [metric2, ...]]
    n_cols = 1 + len(requested)
    data: list[list] = [[] for _ in range(n_cols)]
    for row in rows:
        for i in range(n_cols):
            data[i].append(row[i])

    return {
        "bucket_seconds": bucket,
        "metrics": requested,
        "data": data,
    }


# ---------------------------------------------------------------------------
# API — service liveness (used by uptime probes)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health() -> dict:
    try:
        db().execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded"}


# ---------------------------------------------------------------------------
# API — receiver health (rule-based checks over receiver_stats)
# ---------------------------------------------------------------------------

@app.get("/api/metrics/health")
def api_metrics_health() -> dict:
    cached = _get_cache("health")
    if cached is not None:
        return cached
    report = health.compute_health(db()).to_dict()
    _set_cache("health", report)
    return report


# ---------------------------------------------------------------------------
# Feeders health page
# ---------------------------------------------------------------------------

async def _check_systemd_unit(unit: str) -> dict:
    """Run ``systemctl is-active <unit>`` and return the status string.

    Audit-13 A13-042: reject unit names that start with ``-`` (would be
    interpreted as systemctl flags) and pass ``--`` between args and the
    unit so even a name containing ``--foo`` is treated as positional.
    """
    if unit.startswith("-"):
        return {"systemd": "invalid-unit-name"}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--", unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return {"systemd": stdout.decode().strip() or "unknown"}
    except FileNotFoundError:
        return {"systemd": "unavailable"}
    except asyncio.TimeoutError:
        # Don't leak the child process — kill it and reap (audit-12 #152).
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return {"systemd": "timeout"}
    except Exception as exc:
        return {"systemd": f"error: {exc}"}


async def _check_port(port: int, host: str = "127.0.0.1") -> dict:
    """Try to open a TCP connection to *host*:*port*."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3.0,
        )
        writer.close()
        await writer.wait_closed()
        return {"port": port, "port_status": "open"}
    except (ConnectionRefusedError, OSError):
        return {"port": port, "port_status": "closed"}
    except asyncio.TimeoutError:
        return {"port": port, "port_status": "timeout"}


def _read_json_file(path: str) -> dict | None:
    """Read and parse a JSON file, returning None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _feeder_details_readsb(status_path: str) -> list[tuple[str, str]]:
    """Extract key stats from a readsb JSON directory."""
    details: list[tuple[str, str]] = []
    ac = _read_json_file(f"{status_path}/aircraft.json")
    if ac:
        count = len(ac.get("aircraft", []))
        details.append(("Aircraft tracked", str(count)))
    stats = _read_json_file(f"{status_path}/stats.json")
    if stats:
        last = stats.get("last1min", {})
        msgs = last.get("messages", 0)
        if msgs:
            start = last.get("start", 0)
            end = last.get("end", 0)
            dur = end - start if end > start else 60
            details.append(("Messages/s", f"{msgs / dur:.0f}"))
        local = last.get("local", {})
        if "signal" in local:
            details.append(("Signal", f"{local['signal']:.1f} dBFS"))
        if "noise" in local:
            details.append(("Noise", f"{local['noise']:.1f} dBFS"))
        max_dist = last.get("max_distance")
        if max_dist:
            details.append(("Max range", f"{max_dist / 1852:.1f}"))
    return details


async def _feeder_details_fr24(status_url: str) -> list[tuple[str, str]]:
    """Fetch FR24 monitor.json and extract key fields."""
    details: list[tuple[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(status_url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return details
    if data.get("build_version"):
        details.append(("Version", data["build_version"]))
    fs = data.get("feed_status")
    if fs:
        details.append(("FR24 link", fs))
    alias = data.get("feed_alias")
    if alias:
        details.append(("Radar code", alias))
    ac = data.get("feed_num_ac_tracked")
    if ac is not None:
        details.append(("Aircraft tracked", str(ac)))
    rx = data.get("rx_connected")
    if rx is not None:
        details.append(("Receiver", "connected" if str(rx) == "1" else "disconnected"))
    mlat_ok = data.get("mlat-ok")
    if mlat_ok is not None:
        details.append(("MLAT", "ok" if str(mlat_ok) == "1" else "not ok"))
    return details


def _feeder_details_piaware(status_path: str) -> list[tuple[str, str]]:
    """Read PiAware status.json and extract component statuses."""
    details: list[tuple[str, str]] = []
    data = _read_json_file(status_path)
    if not data:
        return details
    ver = data.get("piaware_version")
    if ver:
        details.append(("Version", f"PiAware {ver}"))
    for key in ("piaware", "adept", "radio", "mlat"):
        comp = data.get(key)
        if comp and isinstance(comp, dict):
            msg = comp.get("message", comp.get("status", ""))
            if msg:
                details.append((key.capitalize(), msg))
    cpu = data.get("cpu_temp_celcius")
    if cpu is not None:
        details.append(("CPU temp", f"{cpu:.0f} C"))
    return details


async def _feeder_details_mlat(unit: str) -> list[tuple[str, str]]:
    """Parse recent journald output for mlat-client stats.

    Audit-13 A13-042: reject unit names that start with ``-`` (would be
    misread as a journalctl flag) before invoking the subprocess.
    """
    details: list[tuple[str, str]] = []
    if unit.startswith("-"):
        return details
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", unit, "--no-pager", "-n", "30", "-o", "cat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        lines = stdout.decode(errors="replace").splitlines()
    except asyncio.TimeoutError:
        # Don't leak the child — kill + reap (audit-12 #152).
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return details
    except Exception:
        return details
    for line in reversed(lines):
        if not details or len(details) < 4:
            m = re.search(r"Results:\s+([\d.]+)\s+positions/minute", line)
            if m and not any(k == "Positions/min" for k, _ in details):
                details.append(("Positions/min", m.group(1)))
            m = re.search(r"Aircraft:\s+(.+)", line)
            if m and not any(k == "Aircraft" for k, _ in details):
                details.append(("Aircraft", m.group(1)))
            m = re.search(r"peer_count:\s+(\d+)", line)
            if m and not any(k == "Peers" for k, _ in details):
                details.append(("Peers", m.group(1)))
            m = re.search(r"Server:\s+(\S+)", line)
            if m and not any(k == "Server" for k, _ in details):
                details.append(("Server", m.group(1)))
    return details


_FEEDER_STATUS_URL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_safe_status_path(path: str) -> bool:
    """A feeder status_path comes from RSBS_FEEDERS (env-controlled). Only allow
    paths that resolve under ``config.FEEDER_STATUS_ROOT`` (default ``/run``)
    — defence-in-depth against path traversal if the env is ever attacker-
    controlled.  The root is read at call time so tests can monkeypatch.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    root = config.FEEDER_STATUS_ROOT
    return resolved == root or resolved.startswith(root + "/")


def _is_safe_status_url(url: str) -> bool:
    """A feeder status_url comes from RSBS_FEEDERS (env-controlled). Only allow
    plain http on a loopback host — defence-in-depth against SSRF if the env
    is ever attacker-controlled."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in _FEEDER_STATUS_URL_HOSTS


async def _fetch_feeder_details(feeder: dict) -> list[tuple[str, str]]:
    """Dispatch to the appropriate detail fetcher based on status_type."""
    st = feeder.get("status_type")
    try:
        if st == "readsb" and feeder.get("status_path"):
            if not _is_safe_status_path(feeder["status_path"]):
                log.warning("feeder %r: rejecting status_path %r (must be under %s/)",
                            feeder.get("name"), feeder["status_path"], config.FEEDER_STATUS_ROOT)
                return []
            return _feeder_details_readsb(feeder["status_path"])
        if st == "fr24" and feeder.get("status_url"):
            if not _is_safe_status_url(feeder["status_url"]):
                log.warning("feeder %r: rejecting status_url %r (must be http on loopback)",
                            feeder.get("name"), feeder["status_url"])
                return []
            return await _feeder_details_fr24(feeder["status_url"])
        if st == "piaware" and feeder.get("status_path"):
            if not _is_safe_status_path(feeder["status_path"]):
                log.warning("feeder %r: rejecting status_path %r (must be under %s/)",
                            feeder.get("name"), feeder["status_path"], config.FEEDER_STATUS_ROOT)
                return []
            return _feeder_details_piaware(feeder["status_path"])
        if st == "mlat":
            return await _feeder_details_mlat(feeder["unit"])
    except Exception:
        # audit-12 #151 — surface real failures to the operator instead of
        # silently returning []. A misconfigured feeder or a corrupted
        # status file would otherwise be invisible.
        log.warning(
            "feeder %r: details fetch failed (status_type=%r)",
            feeder.get("name"), st, exc_info=True,
        )
    return []


async def _check_single_feeder(feeder: dict) -> dict:
    result = {"name": feeder["name"], "unit": feeder["unit"]}
    coros: list = [_check_systemd_unit(feeder["unit"])]
    if feeder.get("port"):
        coros.append(_check_port(feeder["port"]))
    checks = await asyncio.gather(*coros)
    for check in checks:
        result.update(check)
    systemd_ok = result.get("systemd") == "active"
    port_ok = result.get("port_status", "open") == "open"
    if result.get("systemd") == "unavailable":
        result["overall"] = "unknown"
    elif systemd_ok and port_ok:
        result["overall"] = "ok"
    else:
        result["overall"] = "error"
    result["details"] = await _fetch_feeder_details(feeder)
    return result


async def _check_all_feeders() -> list[dict]:
    return await asyncio.gather(*[_check_single_feeder(f) for f in config.FEEDERS])


@app.get("/api/feeders")
async def api_feeders() -> dict:
    """Same shape as the Jinja /feeders template uses — list of feeder
    status dicts plus a has_feeders flag for the empty-state notice.
    """
    feeders = list(await _check_all_feeders()) if config.FEEDERS else []
    return {"feeders": feeders, "has_feeders": bool(config.FEEDERS)}


# ---------------------------------------------------------------------------
# SPA root catch-all — MUST be registered last so it doesn't shadow literal
# /api/* routes, the compat redirects, or /static. FastAPI's router tries
# routes in registration order; this `path:path` parameter matches anything,
# so it's the final fallback.
# ---------------------------------------------------------------------------

if _SPA_AVAILABLE:
    _SPA_ASSET_EXTS = {
        ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".svg",
        ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf",
        ".map", ".json", ".txt",
    }

    @app.get("/", include_in_schema=False)
    @app.get("/{spa_path:path}", include_in_schema=False)
    def _spa(spa_path: str = "") -> Response:
        # Surface missing-asset 404s instead of returning the SPA shell —
        # masking them as HTML hides deploy mistakes (blank page in browser
        # tries to execute HTML as JS/CSS).
        last = spa_path.rsplit("/", 1)[-1]
        if "." in last:
            ext = "." + last.rsplit(".", 1)[-1].lower()
            if ext in _SPA_ASSET_EXTS:
                raise HTTPException(status_code=404)
        try:
            body = SPA_INDEX.read_bytes()
        except FileNotFoundError:  # pragma: no cover — disappears mid-flight
            raise HTTPException(status_code=503, detail="SPA dist missing")
        return Response(
            content=body,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


