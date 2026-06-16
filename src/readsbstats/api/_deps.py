"""Shared dependencies for the api/* routers.

Holds the DB connection seam (``_db``/``_thread_local``/``db()`` — tests
inject by monkeypatching this module), the SQL fragments + allowlists,
the request-shape validators, and small utilities that don't belong to
any single domain endpoint.

Settings helpers live in ``api/settings.py`` (settings-endpoint internal),
photo helpers live in ``api/_photos.py`` (have their own async per-type
locks), and cache state lives in ``cache`` (shared mutable runtime).
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from .. import config, database, icao_ranges


# ---------------------------------------------------------------------------
# DB connection — per-thread.  Python's sqlite3 module holds a per-connection
# mutex, so sharing a single connection across uvicorn's threadpool would
# serialize every request — destroying WAL's reader concurrency.  Each thread
# gets its own connection, opened lazily on first use.
#
# Tests inject an in-memory connection by setting ``_db`` directly via
# ``monkeypatch.setattr(_deps, "_db", conn)``; when set, every thread sees
# that connection (in-memory DBs cannot be reopened).
# ---------------------------------------------------------------------------
_db: sqlite3.Connection | None = None  # test override; None in production
_thread_local = threading.local()


def db() -> sqlite3.Connection:
    if _db is not None:
        return _db
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        # uri=True so _maybe_attach_vdl2 can attach vdl2.db read-only (file:?mode=ro).
        conn = database.connect(uri=True)
        _maybe_attach_vdl2(conn)
        _thread_local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Optional VDL2 read-time join. When RSBS_VDL2_ENABLED, the separate vdl2.db is
# ATTACHed READ-ONLY (file:...?mode=ro — enforced, not by convention) so the
# flights query can compute a `has_acars` flag and filter on it. Best-effort: a
# missing/table-less/unattachable vdl2.db leaves it not-attached, and callers
# gate on `vdl2_attached(conn)` so the core query degrades to "no badge" instead
# of erroring. Never touches history.db; no-op when the feature is disabled.
#
# mode=ro on a WAL DB works on SQLite >= 3.22 when the -shm/-wal exist or the
# directory is writable (both true here — the collector is the live writer and
# /mnt/ext is writable). On a read-only mount the attach fails and the feature
# degrades, surfaced honestly via the `vdl2.attach_available` bit in /api/health.
# ---------------------------------------------------------------------------
def _vdl2_ro_uri() -> str:
    return f"file:{urllib.parse.quote(os.path.abspath(config.VDL2_DB_PATH))}?mode=ro"


def _maybe_attach_vdl2(conn: sqlite3.Connection) -> None:
    # Attach state is read back via PRAGMA database_list (sqlite3.Connection has
    # no __dict__ to memoise a flag on).
    if not config.VDL2_ENABLED or vdl2_attached(conn):
        return
    # mode=ro won't create a missing file, but skip early to avoid a noisy error.
    if not os.path.exists(config.VDL2_DB_PATH):
        return
    try:
        conn.execute("ATTACH DATABASE ? AS vdl2db", (_vdl2_ro_uri(),))
        has_table = conn.execute(
            "SELECT 1 FROM vdl2db.sqlite_master WHERE type='table' AND name='vdl2_messages'"
        ).fetchone()
        if not has_table:
            conn.execute("DETACH DATABASE vdl2db")
    except sqlite3.Error:
        try:
            conn.execute("DETACH DATABASE vdl2db")
        except sqlite3.Error:
            pass


def vdl2_attached(conn: sqlite3.Connection) -> bool:
    """True when vdl2.db is attached on this connection — the gate for emitting
    the `has_acars` column/filter (so a missing vdl2.db never 500s /api/flights).
    Stateless: reads the live attach list (cheap, ~3 rows)."""
    try:
        return any(r["name"] == "vdl2db" for r in conn.execute("PRAGMA database_list"))
    except sqlite3.Error:
        return False


# Computed badge column for flight rows — only safe when vdl2db is attached.
# Boolean EXISTS over the flight's time window; uses idx_vdl2_icao (icao_hex, ts).
_HAS_ACARS_EXPR = """
    CASE WHEN EXISTS(
        SELECT 1 FROM vdl2db.vdl2_messages v
        WHERE v.icao_hex = f.icao_hex
          AND v.ts >= f.first_seen AND v.ts <= f.last_seen
    ) THEN 1 ELSE 0 END AS has_acars
"""


# ---------------------------------------------------------------------------
# CSRF dependency for mutating endpoints
# ---------------------------------------------------------------------------

# SH-1 (Audit 2026-05-31): optional bearer-token auth for mutating
# endpoints. No-op when RSBS_API_TOKEN is unset (default trusted-LAN
# posture — see README "Security" section). When set, every mutating
# call must carry `Authorization: Bearer <token>` and the value is
# compared with hmac.compare_digest.
#
# Read directly from os.getenv rather than via config.py to keep the
# secret out of the parsed-config surface (which `/api/settings`
# returns). Read at request time, not import time — so tests using
# monkeypatch.setenv work and operators can SIGHUP-rotate the env var
# without a full module reload. Production cost is negligible
# (os.environ is a dict lookup).
def _get_api_token() -> str:
    return os.getenv("RSBS_API_TOKEN", "")


# Module-level binding kept for back-compat with existing tests that
# do `monkeypatch.setattr(_deps, "_API_TOKEN", "secret")`. When the
# attribute is present and non-empty it overrides the env var; otherwise
# _auth_check falls back to os.getenv.
_API_TOKEN: str | None = None


def _auth_check(authorization: str | None = Header(default=None)) -> None:
    """Optional bearer-token gate. Apply alongside _csrf_check on every
    mutating endpoint. No-op when no token is configured."""
    token = _API_TOKEN if _API_TOKEN else _get_api_token()
    if not token:
        return
    expected = f"Bearer {token}"
    if not hmac.compare_digest(authorization or "", expected):
        raise HTTPException(401, "Unauthorized")


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


# ---------------------------------------------------------------------------
# Timestamp formatter — used by CSV export
# ---------------------------------------------------------------------------

def _fmt_ts(epoch: int | None) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM' UTC. Used by the CSV
    export endpoint; empty string for None."""
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Flag SQL — OR-merged bitmask of (aircraft_db.flags | adsbx_overrides.flags
# | anonymous-bit CASE expression). Three aliased variants because the
# anonymous CASE evaluates the source icao_hex column, which lives under
# different aliases (`f`, `sub`, `af`) in different queries.
# ---------------------------------------------------------------------------
_ANON_SQL_F   = icao_ranges.anonymous_flag_sql("f.icao_hex",   config.FLAG_ANONYMOUS)
_ANON_SQL_SUB = icao_ranges.anonymous_flag_sql("sub.icao_hex", config.FLAG_ANONYMOUS)
_ANON_SQL_AF  = icao_ranges.anonymous_flag_sql("af.icao_hex",  config.FLAG_ANONYMOUS)
_FLAGS_EXPR_F   = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_F})"
_FLAGS_EXPR_SUB = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_SUB})"
_FLAGS_EXPR_AF  = f"(COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0) | {_ANON_SQL_AF})"

# Shared aircraft-metadata enrichment: flight row first, then aircraft_db,
# then airplanes.live-confirmed adsbx_overrides — every surface that shows
# registration/type/type_desc must use these or it'll disagree with the
# flight list. `_ENRICH_JOIN` provides the two LEFT JOINs these depend on;
# alias `f` must be in scope.
_ENRICH_REG  = "COALESCE(f.registration,  adb.registration, axo.registration)"
_ENRICH_TYPE = "COALESCE(f.aircraft_type, adb.type_code,    axo.type_code)"
_ENRICH_DESC = "COALESCE(adb.type_desc,   axo.type_desc,    '')"
_ENRICH_JOIN = """
    LEFT JOIN aircraft_db     adb ON adb.icao_hex = f.icao_hex
    LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
"""

# Joined SELECT fragment used in flight list and detail queries
_FLIGHT_COLS = f"""
    f.id,
    f.icao_hex,
    f.callsign                                            AS callsign,
    {_ENRICH_REG}  AS registration,
    {_ENRICH_TYPE}     AS aircraft_type,
    {_ENRICH_DESC}                AS type_desc,
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


# ---------------------------------------------------------------------------
# `_top1()` column allowlist — guards the records endpoint's SQL f-string
# against future caller-controlled order columns. Hoisted to module scope so
# the guard is unit-testable; otherwise the closure-local frozenset was
# unreachable from tests, and a future refactor that loosened the check would
# silently land.
# ---------------------------------------------------------------------------
_TOP1_ALLOWLIST: frozenset[str] = frozenset({"max_distance_nm", "max_gs", "max_alt_baro"})


def _assert_top1_column(order_col: str) -> None:
    """Raise ``ValueError`` if *order_col* is not in ``_TOP1_ALLOWLIST``."""
    if order_col not in _TOP1_ALLOWLIST:
        raise ValueError(f"unsupported order column: {order_col!r}")


# ---------------------------------------------------------------------------
# Sort allowlists — never interpolate raw URL params into ORDER BY.
#
# CONTRACT: every consumer of _SORT_COLS must build its ORDER BY inside a
# query that includes `_FLIGHT_JOIN` (or at minimum `_ENRICH_JOIN`). The
# `registration` and `aircraft_type` entries are `_ENRICH_REG` /
# `_ENRICH_TYPE`, which reference the `adb` AND `axo` aliases. A SELECT
# without those joins will fail at runtime with "no such column:
# axo.registration". A new lightweight handler that wants to skip the
# adsbx_overrides join must either pass its own ORDER BY (not consult
# _SORT_COLS) or extend _SORT_COLS with a sibling table-free dict.
#
# Current consumers (all join _FLIGHT_JOIN): api/flights.py (list +
# export.csv), api/aircraft.py (drilldown list).
# ---------------------------------------------------------------------------
_SORT_COLS: dict[str, str] = {
    "first_seen":     "f.first_seen",
    "icao_hex":       "f.icao_hex",
    "callsign":       "f.callsign",
    "registration":   _ENRICH_REG,
    "aircraft_type":  _ENRICH_TYPE,
    "primary_source": "f.primary_source",
    "duration_sec":   "(f.last_seen - f.first_seen)",
    "max_alt_baro":   "f.max_alt_baro",
    "max_gs":         "f.max_gs",
    "max_distance_nm":"f.max_distance_nm",
    "total_positions":"f.total_positions",
    "origin_icao":    "f.origin_icao",
    "dest_icao":      "f.dest_icao",
}

# Audit-13 A13-077: sibling allowlist for /api/aircraft/flagged. Keys resolve
# to columns from the GROUP-BY aggregate SELECT in that handler (not the
# per-flight `f.*` columns above), so we keep this as a separate dict rather
# than rolling everything into ``_SORT_COLS`` and leaking aggregate names into
# the /api/flights surface.
_FLAGGED_SORT_COLS: dict[str, str] = {
    "last_seen":     "last_seen",
    "first_seen":    "first_seen",
    "flight_count":  "flight_count",
    "registration":  "registration",
    "aircraft_type": "aircraft_type",
}


# ---------------------------------------------------------------------------
# Receiver metrics — column allowlist + aggregation types
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
    """Return the SQL aggregate function for a metrics column.

    Raises ``ValueError`` for any column outside ``_METRICS_COLS``. The caller
    (``api/health.py``) already rejects off-allowlist names, but mirroring
    ``_assert_top1_column`` keeps the membership check travelling with the SQL
    so a future caller that forgets to validate can't interpolate an arbitrary
    column into the GROUP BY projection.
    """
    if col not in _METRICS_COLS:
        raise ValueError(f"unsupported metrics column: {col!r}")
    if col in _METRICS_MAX:
        return f"MAX({col})"
    if col in _METRICS_AVG:
        return f"AVG({col})"
    return f"SUM({col})"


# ---------------------------------------------------------------------------
# CSV export columns
# ---------------------------------------------------------------------------
_CSV_COLS = [
    "first_seen", "last_seen", "duration_sec",
    "icao_hex", "callsign", "registration", "aircraft_type", "type_desc",
    "squawk", "category", "primary_source",
    "max_alt_baro", "max_gs", "max_distance_nm",
    "total_positions", "adsb_positions", "mlat_positions",
    "origin_icao", "dest_icao",
]


# ---------------------------------------------------------------------------
# Pagination + window constants
# ---------------------------------------------------------------------------
_POSITIONS_DEFAULT_LIMIT = 1000
_POSITIONS_MAX_LIMIT = 2000
_CHART_DEFAULT_TARGET = 500
_CHART_MAX_TARGET = 2000

# Historical map snapshot — flight must have a position within this window of `at`
_MAP_WINDOW_SEC = 600


# ---------------------------------------------------------------------------
# Heatmap windows + grid precision (used by api/map.py and the prewarmer)
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

# Coverage polygon bearing buckets
_BUCKET_DEG = 10
_NUM_BUCKETS = 360 // _BUCKET_DEG


# ---------------------------------------------------------------------------
# ICAO path validator — bounds external side-effect surface (photo fetches,
# cache writes) so an arbitrary path segment can't drive them.
# ---------------------------------------------------------------------------
_ICAO_PATH_RE = re.compile(r"^~?[0-9a-fA-F]{6}$")


def _parse_icao_path(raw: str) -> str:
    """Validate + normalise an ICAO hex taken from a URL path (BE-11).

    Accepts an optional single leading ``~`` (anonymous / TIS-B addresses)
    followed by exactly 6 hex digits; returns the lowercase hex with ``~``
    stripped. Anything else → 404. DB queries are parameterised so this is not
    about SQLi — it bounds external side effects so an arbitrary path segment
    can't drive them.
    """
    if not _ICAO_PATH_RE.match(raw or ""):
        raise HTTPException(404)
    return raw.lower().lstrip("~")


# ---------------------------------------------------------------------------
# Shared flight-filter builders (used by /api/flights, /api/flights/export.csv,
# /api/stats — half-open [from, to) semantics so adjacent windows never overlap)
# ---------------------------------------------------------------------------

def _build_date_filter(
    from_ts: int | None,
    to_ts: int | None,
    *,
    col: str = "first_seen",
) -> tuple[list[str], list]:
    """Half-open ``[from_ts, to_ts)`` range filter on ``col`` (BE-16).

    Returns ``(conditions, params)`` — a list of SQL fragments already
    qualified with ``col`` (``>= ?`` / ``< ?``) and the matching bind values;
    either bound may be ``None``.  Half-open semantics match the flight
    history/export filter so adjacent day windows never overlap and a flight
    exactly at ``to`` is never double-counted across buckets.
    """
    conditions: list[str] = []
    params: list = []
    if from_ts is not None:
        conditions.append(f"{col} >= ?")
        params.append(from_ts)
    if to_ts is not None:
        conditions.append(f"{col} < ?")
        params.append(to_ts)
    return conditions, params


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
    has_acars: bool | None = None,
) -> tuple[str, list]:
    """Return (WHERE clause, params list) for the shared flight filter params.

    Date filtering supports either:
      - ``date=YYYY-MM-DD``           — single calendar day (receiver local time)
      - ``from``/``to`` epoch seconds — browser-local midnight boundaries (preferred)
      - ``date_from=YYYY-MM-DD`` and/or ``date_to=YYYY-MM-DD`` — receiver local time
        (kept for backward compat; epoch params take priority when both are sent)

    If ``date`` is set, the range params are ignored — single-day takes priority
    because that's what the old single-``date`` UI sent, and we don't want to
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
        dc, dp = _build_date_filter(from_ts, to_ts, col="f.first_seen")
        conditions += dc
        params += dp
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
        # PY-2 (Audit 2026-05-31): include adsbx_overrides in the match so
        # a flight whose registration is known only via adsbx still appears
        # in `?registration=` filters. All callers join _FLIGHT_JOIN so `axo`
        # is in scope.
        conditions.append(f"{_ENRICH_REG} LIKE ?")
        params.append(registration.upper().strip() + "%")

    if aircraft_type:
        # PY-2: same as above for aircraft_type.
        conditions.append(f"{_ENRICH_TYPE} = ?")
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

    # Only emitted when the handler has confirmed vdl2db is attached (it passes
    # has_acars=None otherwise), so the vdl2db reference is always resolvable.
    if has_acars:
        conditions.append(
            "EXISTS(SELECT 1 FROM vdl2db.vdl2_messages v "
            "WHERE v.icao_hex = f.icao_hex "
            "AND v.ts >= f.first_seen AND v.ts <= f.last_seen)"
        )

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params
