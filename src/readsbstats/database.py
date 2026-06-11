"""
readsbstats — database initialisation, schema, and connection management.
"""
import logging
import os
import sqlite3
import time
from . import config, geo

SCHEMA_VERSION = 5

# Watchlist input caps — enforced by the HTTP API and the Telegram bot
# command path. Kept here (rather than in each consumer) so they cannot drift.
WATCHLIST_VALUE_MAX = 64    # ICAO=6, reg ≤10, callsign ≤8 — 64 is generous
WATCHLIST_LABEL_MAX = 255

# improvements.md A13-079: single source of truth for tables that were
# previously declared twice — once in the top-of-file DDL block (used by
# fresh DBs via `executescript(DDL)`) and once again inside `_migrate()`
# (so the web server, which only calls `_migrate()`, picks them up on
# existing DBs).  Each constant ends without trailing semicolon so it can
# be passed straight to `conn.execute()`; `DDL` joins them with `;`.
_DDL_WATCHLIST = """
CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL,   -- 'icao' | 'registration' | 'callsign_prefix'
    value      TEXT NOT NULL,   -- stored lowercase
    label      TEXT,
    created_at INTEGER NOT NULL
)
"""

_DDL_ADSBX_OVERRIDES = """
CREATE TABLE IF NOT EXISTS adsbx_overrides (
    icao_hex     TEXT PRIMARY KEY,
    flags        INTEGER DEFAULT 0,   -- dbFlags bitmask (military=1, interesting=2, PIA=4, LADD=8)
    registration TEXT,
    type_code    TEXT,
    type_desc    TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
)
"""

_DDL_TYPE_PHOTOS = """
CREATE TABLE IF NOT EXISTS type_photos (
    type_code     TEXT PRIMARY KEY,
    thumbnail_url TEXT,
    large_url     TEXT,
    link_url      TEXT,
    photographer  TEXT,
    fetched_at    INTEGER NOT NULL
)
"""

_DDL_AIRPORTS = """
CREATE TABLE IF NOT EXISTS airports (
    icao_code   TEXT PRIMARY KEY,
    iata_code   TEXT,
    name        TEXT,
    country     TEXT,
    latitude    REAL,
    longitude   REAL,
    fetched_at  INTEGER NOT NULL
)
"""

_DDL_CALLSIGN_ROUTES = """
CREATE TABLE IF NOT EXISTS callsign_routes (
    callsign    TEXT PRIMARY KEY,
    origin_icao TEXT,
    dest_icao   TEXT,
    fetched_at  INTEGER NOT NULL
)
"""

_DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""

_DDL_GRID_DAILY = """
CREATE TABLE IF NOT EXISTS grid_daily (
    scale INTEGER NOT NULL,
    day   INTEGER NOT NULL,
    lat_b INTEGER NOT NULL,
    lon_b INTEGER NOT NULL,
    w     INTEGER NOT NULL,
    PRIMARY KEY (scale, day, lat_b, lon_b)
) WITHOUT ROWID
"""

_DDL_COVERAGE_DAILY = """
CREATE TABLE IF NOT EXISTS coverage_daily (
    day       INTEGER NOT NULL,
    bearing_b INTEGER NOT NULL,
    max_nm    REAL NOT NULL,
    PRIMARY KEY (day, bearing_b)
) WITHOUT ROWID
"""

_DDL_RECEIVER_STATS = """
CREATE TABLE IF NOT EXISTS receiver_stats (
    ts                  INTEGER PRIMARY KEY,
    ac_with_pos         INTEGER,
    ac_without_pos      INTEGER,
    ac_adsb             INTEGER,
    ac_mlat             INTEGER,
    signal              REAL,
    noise               REAL,
    peak_signal         REAL,
    strong_signals      INTEGER,
    local_modes         INTEGER,
    local_bad           INTEGER,
    local_unknown_icao  INTEGER,
    local_accepted_0    INTEGER,
    local_accepted_1    INTEGER,
    samples_dropped     REAL,
    samples_lost        REAL,
    messages            INTEGER,
    positions_total     INTEGER,
    positions_adsb      INTEGER,
    positions_mlat      INTEGER,
    max_distance_m      REAL,
    tracks_new          INTEGER,
    tracks_single       INTEGER,
    cpu_demod           REAL,
    cpu_reader          REAL,
    cpu_background      REAL,
    cpu_aircraft_json   REAL,
    cpu_heatmap         REAL,
    remote_modes        INTEGER,
    remote_bad          INTEGER,
    remote_accepted     INTEGER,
    remote_bytes_in     INTEGER,
    remote_bytes_out    INTEGER,
    cpr_airborne        INTEGER,
    cpr_global_ok       INTEGER,
    cpr_global_bad      INTEGER,
    cpr_global_range    INTEGER,
    cpr_global_speed    INTEGER,
    cpr_global_skipped  INTEGER,
    cpr_local_ok        INTEGER,
    cpr_local_range     INTEGER,
    cpr_local_speed     INTEGER,
    cpr_filtered        INTEGER,
    altitude_suppressed INTEGER
)
"""

DDL = f"""
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = {config.DB_SYNCHRONOUS};
PRAGMA foreign_keys = ON;
PRAGMA cache_size   = -65536;

CREATE TABLE IF NOT EXISTS flights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icao_hex        TEXT    NOT NULL,
    callsign        TEXT,
    registration    TEXT,
    aircraft_type   TEXT,
    squawk          TEXT,
    category        TEXT,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    max_alt_baro    INTEGER,
    max_gs          REAL,
    min_rssi        REAL,
    max_rssi        REAL,
    total_positions INTEGER DEFAULT 0,
    adsb_positions  INTEGER DEFAULT 0,
    mlat_positions  INTEGER DEFAULT 0,
    primary_source  TEXT,           -- "adsb" | "mlat" | "mixed" | "other"
    max_distance_nm REAL,           -- great-circle distance from receiver (nm)
    max_distance_bearing REAL,      -- bearing (deg) at the max-distance point
    lat_min         REAL,
    lat_max         REAL,
    lon_min         REAL,
    lon_max         REAL,
    origin_icao     TEXT,           -- departure airport ICAO (from adsbdb.com)
    dest_icao       TEXT            -- destination airport ICAO (from adsbdb.com)
);

CREATE INDEX IF NOT EXISTS idx_flights_icao         ON flights(icao_hex);
CREATE INDEX IF NOT EXISTS idx_flights_first        ON flights(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_flights_callsign     ON flights(callsign);
-- Audit-13 A13-063: was `idx_flights_reg` historically; renamed to
-- `idx_flights_registration` so fresh installs match the name used by
-- `_migrate()` and the docstring at `_backfill_flights_enrichment`.
-- `_migrate()` drops the old `idx_flights_reg` on existing DBs.
CREATE INDEX IF NOT EXISTS idx_flights_registration ON flights(registration);
CREATE INDEX IF NOT EXISTS idx_flights_type         ON flights(aircraft_type);
-- idx_flights_dist is created in _migrate() after the column is guaranteed to exist

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_id   INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    ts          INTEGER NOT NULL,
    lat         REAL,
    lon         REAL,
    alt_baro    INTEGER,
    alt_geom    INTEGER,
    gs          REAL,
    track       REAL,
    baro_rate   INTEGER,
    rssi        REAL,
    messages    INTEGER,
    source_type TEXT            -- "adsb_icao" | "mlat" | "adsr_icao" | "mode_s" | …
);

CREATE INDEX IF NOT EXISTS idx_positions_ts     ON positions(ts);
-- Audit 2026-05-26: composite for the hot `WHERE flight_id=? ORDER BY ts`
-- pattern used by flight-detail rendering and the purge scripts. Built
-- once per fresh install via DDL; existing DBs get it via
-- run_background_migrations() because the index build is too slow for
-- _migrate() on a millions-row positions table.
CREATE INDEX IF NOT EXISTS idx_positions_flight_ts ON positions(flight_id, ts);

-- Persists currently open flights across collector restarts
CREATE TABLE IF NOT EXISTS active_flights (
    icao_hex    TEXT    PRIMARY KEY,
    flight_id   INTEGER NOT NULL REFERENCES flights(id),
    last_seen   INTEGER NOT NULL
);

-- Aircraft metadata from tar1090-db CSV (updated weekly by db_updater.py)
CREATE TABLE IF NOT EXISTS aircraft_db (
    icao_hex     TEXT PRIMARY KEY,
    registration TEXT,
    type_code    TEXT,   -- ICAO type designator e.g. B738, A320
    type_desc    TEXT,   -- long description e.g. "BOEING 737-800"
    flags        INTEGER DEFAULT 0  -- military=1, interesting=2, PIA=4, LADD=8
);

-- Airline names from OpenFlights (updated weekly by db_updater.py)
CREATE TABLE IF NOT EXISTS airlines (
    icao_code TEXT PRIMARY KEY,  -- 3-letter ICAO airline code (LOT, RYR, DLH…)
    name      TEXT NOT NULL,     -- full name ("LOT Polish Airlines")
    iata_code TEXT,              -- 2-letter IATA code
    country   TEXT,
    active    INTEGER DEFAULT 1
);

-- Airport metadata populated by route_enricher.py via adsbdb.com
{_DDL_AIRPORTS.strip()};

-- Route cache: callsign → origin/dest airport ICAO codes
-- NULL origin_icao + NULL dest_icao means "confirmed unknown, don't retry until fetched_at expires"
-- (DDL inlined from _DDL_CALLSIGN_ROUTES below)
{_DDL_CALLSIGN_ROUTES.strip()};

-- Cached photo URLs from Planespotters.net (keyed by ICAO hex)
CREATE TABLE IF NOT EXISTS photos (
    icao_hex      TEXT PRIMARY KEY,
    thumbnail_url TEXT,
    large_url     TEXT,
    link_url      TEXT,
    photographer  TEXT,
    fetched_at    INTEGER NOT NULL
);

-- Cached representative photo per aircraft type code (type-level fallback)
{_DDL_TYPE_PHOTOS.strip()};

-- User-defined aircraft watchlist (Telegram alerts on new flight)
{_DDL_WATCHLIST.strip()};

-- ADSBexchange-confirmed flags & enrichment (survives weekly tar1090-db refresh)
{_DDL_ADSBX_OVERRIDES.strip()};

-- Receiver metrics time-series (metrics_collector.py)
{_DDL_RECEIVER_STATS.strip()};

-- Generic key/value metadata (first use: rollups_ready flag).
{_DDL_META.strip()};

-- Daily heatmap rollups, maintained incrementally by the collector inside
-- the poll transaction. scale=100 → 0.01° cells (serves 7d window, pruned
-- to RSBS_GRID_FINE_RETENTION_DAYS); scale=10 → 0.1° cells (serves
-- 30d/all, kept forever). Bucket = FLOOR(coord*scale + 0.5), identical to
-- the historical heatmap SQL so cells line up across the migration.
{_DDL_GRID_DAILY.strip()};

-- Daily per-bearing max range (1° buckets; the API rebuckets to 10°).
{_DDL_COVERAGE_DAILY.strip()};

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
"""


def connect(path: str = config.DB_PATH, *, uri: bool = False) -> sqlite3.Connection:
    """Return a connection with WAL mode and row_factory set.

    ``uri=True`` enables SQLite URI filenames so the caller can later ATTACH a
    database read-only via ``file:...?mode=ro`` (URI processing is a per-connection
    flag). Only the web reader (``api/_deps.db``, which ATTACHes vdl2.db) needs it;
    the collector/writer and everything else keep the default so a path containing
    ``?`` or a leading ``file:`` is never reinterpreted. Behavior-neutral for plain
    paths and ``:memory:`` even when uri=True."""
    conn = sqlite3.connect(path, check_same_thread=False, uri=uri)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "PRAGMA journal_mode = WAL;"
        f"PRAGMA synchronous  = {config.DB_SYNCHRONOUS};"
        "PRAGMA foreign_keys = ON;"
        "PRAGMA cache_size   = -65536;"
        "PRAGMA mmap_size    = 268435456;"
        "PRAGMA busy_timeout = 30000;"
        "PRAGMA wal_autocheckpoint = 1000;"
    )
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema changes to an existing database."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
    new_cols = {
        "max_distance_nm":   "REAL",
        # adsb_positions / mlat_positions / primary_source were added in v1 DDL,
        # but add them here as a safety net for very early installs
        "adsb_positions":    "INTEGER DEFAULT 0",
        "mlat_positions":    "INTEGER DEFAULT 0",
        "primary_source":    "TEXT",
        "origin_icao":       "TEXT",
        "dest_icao":         "TEXT",
        "max_distance_bearing": "REAL",
    }
    for col, defn in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE flights ADD COLUMN {col} {defn}")

    # Create this index here (not in DDL) so it always runs after the column
    # is guaranteed to exist — whether via CREATE TABLE or ALTER TABLE above.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_dist ON flights(max_distance_nm DESC)"
    )
    # Composite indexes for common sort patterns (icao/callsign lookups with time sort)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_icao_first "
        "ON flights(icao_hex, first_seen DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_callsign_first "
        "ON flights(callsign, first_seen DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_origin ON flights(origin_icao)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_dest ON flights(dest_icao)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_max_gs ON flights(max_gs DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_max_alt "
        "ON flights(max_alt_baro DESC)"
    )
    # F08/SQL-1 (Audit 18, measure-first): flights.last_seen is intentionally
    # NOT indexed. It's UPDATE-ed on every poll for each active flight (index
    # write amplification on the hot path), and its only reader — the default-
    # off retention purge — doesn't justify that cost. Revisit only with an
    # EXPLAIN QUERY PLAN + a Pi write-throughput check if retention is enabled
    # on a large DB.
    # NOTE: backfill of NULL primary_source on closed flights moved to
    # run_background_migrations() — it's a full-table UPDATE that would block
    # web startup on the SQLite write lock. See audit-12 #139.

    # Audit-13 A13-063: existing DBs (pre-rename) carry an orphan
    # `idx_flights_reg` on `flights(registration)` alongside the
    # newer `idx_flights_registration`. Two indexes on the same column
    # waste write cost and disk; drop the older name. `IF EXISTS` keeps
    # this a no-op on fresh installs (which now create the canonical
    # name from DDL).
    conn.execute("DROP INDEX IF EXISTS idx_flights_reg")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_registration ON flights(registration)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_type ON flights(aircraft_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flights_source ON flights(primary_source)"
    )

    # New tables added after initial schema — created here so the web server
    # (which only calls _migrate, not the full DDL) picks them up on existing DBs.
    # improvements.md A13-079: each `_DDL_*` constant is the single source of
    # truth, used here and inlined into `DDL` above for fresh installs.
    conn.execute(_DDL_WATCHLIST)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_type_value "
        "ON watchlist(match_type, value)"
    )

    conn.execute(_DDL_ADSBX_OVERRIDES)

    # Type-level photo cache (fallback when no specific aircraft photo exists)
    conn.execute(_DDL_TYPE_PHOTOS)

    # Airport metadata (populated by route_enricher via adsbdb.com)
    conn.execute(_DDL_AIRPORTS)

    # Route cache: callsign → origin/dest airport ICAO codes
    conn.execute(_DDL_CALLSIGN_ROUTES)

    # Index for type-based photo lookups (photos JOIN aircraft_db WHERE type_code = ?)
    # Guard: aircraft_db may not exist in very old test/minimal schemas
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "aircraft_db" in tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aircraft_db_type_code ON aircraft_db(type_code)"
        )

    # Receiver metrics time-series (metrics_collector.py)
    conn.execute(_DDL_RECEIVER_STATS)

    # Rollup tables: generic metadata store + daily heatmap/coverage rollups.
    # Added in Phase 2 (2026-06); must be in _migrate() so the web server
    # picks them up on existing DBs without a collector restart.
    conn.execute(_DDL_META)
    conn.execute(_DDL_GRID_DAILY)
    conn.execute(_DDL_COVERAGE_DAILY)

    conn.commit()


def backfill_bearing(path: str = config.DB_PATH) -> None:
    """Backfill max_distance_bearing for flights that have max_distance_nm but
    no bearing yet.  Runs in batches to avoid long WAL write locks.  Called
    from collector.py in a background thread after READY=1 so it never blocks
    systemd startup.

    Uses a ``WHERE id > last_id`` cursor (audit-12 #147) so each row is
    examined exactly once. The previous LIMIT-subquery pattern re-scanned
    the flights table from the top on every iteration; on a Pi 4 with
    200k+ flights that would have turned a ~30s job into hours of work.
    """
    _log = logging.getLogger(__name__)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        rlat, rlon = config.RECEIVER_LAT, config.RECEIVER_LON
        need_backfill = conn.execute(
            "SELECT COUNT(*) FROM flights "
            "WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NULL"
        ).fetchone()[0]
        if not need_backfill:
            return
        _log.info("Backfilling max_distance_bearing for %d flights …", need_backfill)
        conn.execute("PRAGMA busy_timeout = 10000")
        batch_size = 500
        last_id = 0
        done = 0
        while True:
            # Pull the next batch of IDs past the cursor. The PK index on
            # `id` makes this an O(batch_size) seek, regardless of how many
            # rows we've already processed.
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM flights "
                    "WHERE id > ? "
                    "  AND max_distance_nm IS NOT NULL "
                    "  AND max_distance_bearing IS NULL "
                    "ORDER BY id LIMIT ?",
                    (last_id, batch_size),
                ).fetchall()
            ]
            if not ids:
                break
            placeholders = ",".join("?" * len(ids))
            # Audit-13 A13-020: parameterise receiver lat/lon (previously
            # f-stringed). Both are numeric today but going through bound
            # params lets SQLite reuse the prepared statement across
            # batches and removes a latent injection trap.
            # Audit 17: route through the canonical geo SQL helpers instead of
            # hand-rolling the great-circle formulas (the A13-076 consolidation
            # missed this backfill). Bind order is identical to the previous
            # inline SQL: bearing_sql emits [rlon, rlat, rlat, rlon] and
            # haversine_sql emits [rlat, rlat, rlat, rlon, rlon]. Ordering by
            # the full distance is monotonic with the old inner `a` term.
            bearing_expr = geo.bearing_sql("p.lat", "p.lon", "?", "?")
            dist_expr = geo.haversine_sql("p.lat", "p.lon", "?", "?")
            conn.execute(
                f"""
                UPDATE flights SET max_distance_bearing = (
                    SELECT {bearing_expr}
                    FROM positions p
                    WHERE p.flight_id = flights.id
                      AND p.lat IS NOT NULL AND p.lon IS NOT NULL
                    ORDER BY {dist_expr} DESC
                    LIMIT 1
                )
                WHERE id IN ({placeholders})
                """,
                # Order must match each '?' placeholder above
                [rlon, rlat, rlat, rlon, rlat, rlat, rlat, rlon, rlon, *ids],
            )
            conn.commit()
            last_id = ids[-1]
            done += len(ids)
            if done < need_backfill:
                _log.info("  … %d / %d", done, need_backfill)
        _log.info("Backfill complete.")
    except Exception:
        _log.exception("backfill_bearing failed")
    finally:
        if conn is not None:
            conn.close()


def _build_positions_indexes(path: str = config.DB_PATH) -> None:
    """Ensure idx_positions_flight_ts exists on the positions table and drop
    indexes that are redundant or harmful to query-plan choices.  Separated
    from _migrate() so the collector can run them in a background thread
    after READY=1 rather than blocking startup.

    Phase 1 drops (2026-06, production-dump analysis on 6.18M-row DB):
    - idx_positions_flight        — left-prefix duplicate of idx_positions_flight_ts
    - idx_positions_ts_coords     — 0% NULL coords in practice; planner picked
                                    it for heatmap-all and did a per-row table
                                    lookup, measured 28% slower than a plain scan
    - idx_positions_flight_id_desc — freed by Task 2 (latest-fix queries now use
                                    ORDER BY ts via idx_positions_flight_ts)
    Saves ~280 MB and cuts per-insert B-tree updates from 8 to 5.
    ORDERING: idx_positions_flight_ts is created BEFORE the drops so the
    latest-fix query never lacks a usable index on a stale DB.

    Phase 2 (rollups): idx_positions_ts_flight / idx_positions_ts_lat_lon are
    no longer created here — they only served the heatmap/coverage scans now
    answered by grid_daily/coverage_daily, and rollups.backfill_and_finalize()
    (called right after this in run_background_migrations) drops them from
    existing DBs (~320 MB on production). idx_positions_ts (plain, from DDL)
    remains for windowed raw scans.
    """
    _log = logging.getLogger(__name__)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 30000")
        _log.info("Building positions indexes …")
        # Audit 2026-05-26: composite covering `WHERE flight_id=? ORDER BY ts`
        # used by /api/flights/{id} positions, purge_ghosts, purge_bad_gs.
        # NOTE (Audit 17): this index is ALSO declared in the top-level DDL
        # (search `idx_positions_flight_ts`) so a fresh install gets it while
        # `positions` is still empty (cheap). Keep BOTH definitions byte-for-byte
        # identical — they are the single logical source for this index.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_flight_ts "
            "ON positions(flight_id, ts)"
        )
        # Phase 1 (2026-06, production-dump analysis): drop indexes that are
        # redundant (left-prefix duplicates) or actively harmful (the planner
        # chose idx_positions_ts_coords for heatmap-all and did a per-row
        # table lookup — measured 28% slower than a plain scan). Saves
        # ~280 MB on a 6M-row DB and cuts per-insert B-tree updates 8 → 5.
        # Ordering: idx_positions_flight_ts is created above BEFORE these
        # drops so the latest-fix query never lacks a usable index.
        conn.execute("DROP INDEX IF EXISTS idx_positions_flight")
        conn.execute("DROP INDEX IF EXISTS idx_positions_ts_coords")
        conn.execute("DROP INDEX IF EXISTS idx_positions_flight_id_desc")
        # First-ever statistics for the query planner (production DB had no
        # sqlite_stat1 at all). analysis_limit bounds the row sample so this
        # stays cheap even on millions of rows. Re-runs on every collector
        # start; ~0.1 s at this limit.
        conn.execute("PRAGMA analysis_limit = 1000")
        conn.execute("ANALYZE")
        conn.commit()
        _log.info("Positions indexes ready.")
    except Exception:
        _log.exception("_build_positions_indexes failed")
    finally:
        if conn is not None:
            conn.close()


def _backfill_primary_source(path: str = config.DB_PATH) -> None:
    """Set primary_source='other' on closed flights where it's NULL.

    Crash-recovery backfill: a collector that died mid-flight without writing
    _close_flight leaves the row open with NULL primary_source. Runs in a
    background thread (after READY=1) — a full-table UPDATE here historically
    lived in _migrate() and blocked web startup on the SQLite write lock."""
    _log = logging.getLogger(__name__)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute(
            "UPDATE flights SET primary_source = 'other' "
            "WHERE primary_source IS NULL "
            "AND id NOT IN (SELECT flight_id FROM active_flights)"
        )
        conn.commit()
    except Exception:
        _log.exception("_backfill_primary_source failed")
    finally:
        if conn is not None:
            conn.close()


def _drop_dead_watchlist_alerted_column(path: str = config.DB_PATH) -> None:
    """Drop the legacy watchlist_alerted column from existing DBs.

    The column was added by an earlier _migrate() but never read or written —
    watchlist dedup is handled by ``is_new_flight`` in the collector. Lives in
    background migrations because SQLite rewrites the entire flights table on
    DROP COLUMN, which is too slow for _migrate()."""
    _log = logging.getLogger(__name__)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        if "watchlist_alerted" not in cols:
            return
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("ALTER TABLE flights DROP COLUMN watchlist_alerted")
        conn.commit()
    except Exception:
        _log.exception("_drop_dead_watchlist_alerted_column failed")
    finally:
        if conn is not None:
            conn.close()


def _backfill_flights_enrichment(path: str = config.DB_PATH) -> None:
    """Populate flights.registration / flights.aircraft_type from
    aircraft_db where flights have NULL values.

    Audit 2026-05-26: groundwork for dropping the
    `COALESCE(f.registration, adb.registration)` filter in
    `_build_flight_filter()`. Once every existing row has a stored
    effective registration/type, the WHERE clause can become a direct
    column predicate and hit the existing `idx_flights_registration` /
    `idx_flights_type` indexes. The COALESCE drop is deferred to a
    follow-up commit gated on confirmation that this backfill has
    completed in production.

    Uses correlated subqueries (not ``UPDATE ... FROM ...``) to avoid
    the SQLite "ambiguous column name" error documented in CLAUDE.md.
    """
    _log = logging.getLogger(__name__)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute(
            """
            UPDATE flights SET registration = (
                SELECT registration FROM aircraft_db
                WHERE icao_hex = flights.icao_hex
            )
            WHERE registration IS NULL
              AND EXISTS (
                SELECT 1 FROM aircraft_db
                WHERE icao_hex = flights.icao_hex
              )
            """
        )
        conn.execute(
            """
            UPDATE flights SET aircraft_type = (
                SELECT type_code FROM aircraft_db
                WHERE icao_hex = flights.icao_hex
            )
            WHERE aircraft_type IS NULL
              AND EXISTS (
                SELECT 1 FROM aircraft_db
                WHERE icao_hex = flights.icao_hex
              )
            """
        )
        conn.commit()
    except Exception:
        _log.exception("_backfill_flights_enrichment failed")
    finally:
        if conn is not None:
            conn.close()


def run_background_migrations(path: str = config.DB_PATH) -> None:
    """Run slow one-time migrations in a background thread (after READY=1).
    Builds positions indexes, backfills the heatmap/coverage rollups (then
    drops the legacy ts-composite indexes and sets rollups_ready), backfills
    max_distance_bearing, and sets primary_source='other' on closed flights
    crashed mid-write."""
    _build_positions_indexes(path)
    from . import rollups  # lazy: rollups.backfill_and_finalize imports us back
    rollups.backfill_and_finalize(path)
    backfill_bearing(path)
    _backfill_primary_source(path)
    _backfill_flights_enrichment(path)
    _drop_dead_watchlist_alerted_column(path)


def snapshot_db(src_path: str, dest_path: str | None = None) -> str:
    """Atomic snapshot of `src_path` via `VACUUM INTO`.  When `dest_path` is
    omitted, writes to a sibling `<src>.backup-<ts>.db` file.  Returns the
    snapshot path.  Raises FileExistsError if the destination already exists
    (refuses to clobber an earlier backup)."""
    if dest_path is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest_path = f"{src_path}.backup-{ts}.db"
    if os.path.exists(dest_path):
        raise FileExistsError(dest_path)
    # Audit-13 A13-060: previously `sqlite3.connect(src_path)` opened a
    # connection with `busy_timeout=0`; on a busy receiver the VACUUM
    # INTO would fail immediately under collector contention. Use the
    # shared connect() so the snapshot waits up to 30 s for the writer
    # lock to free.
    conn = connect(src_path)
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()
    return dest_path


def recover_aircraft_db_swap(conn: sqlite3.Connection) -> None:
    """Detect and recover from an aircraft_db staging swap interrupted between
    the rename steps (see ADR 0010 and db_updater.update_aircraft_db).

    Three table-name presences are possible after an interrupted run:
      * ``aircraft_db_new`` only → build phase crashed; drop the stale staging
        table.
      * ``aircraft_db_old`` only (no ``aircraft_db``) → first RENAME succeeded
        but the second didn't. Rename old back to canonical.
      * ``aircraft_db`` + ``aircraft_db_old`` → second RENAME succeeded but the
        final DROP didn't. Drop the leftover old copy.

    BE-3 (Audit 2026-05-31): this used to live only in
    ``db_updater._recover_aborted_swap`` and ran on the weekly updater path.
    Moved here so the web server and collector recover an interrupted swap on
    startup, without waiting for the next ``update_aircraft_db()`` run. The
    updater keeps a thin delegate for back-compat.

    Must run BEFORE any DDL that would re-create an empty ``aircraft_db`` —
    otherwise the 'canonical present + _old leftover' branch would drop the
    only surviving copy of the data.
    """
    _recover_swap(conn, "aircraft_db")


def recover_airlines_db_swap(conn: sqlite3.Connection) -> None:
    """Detect and recover from an airlines staging swap interrupted between
    the rename steps (mirrors ``recover_aircraft_db_swap``).

    Audit 2026-06-01 W: ``update_airlines_db`` does the identical
    rename swap as ``update_aircraft_db`` but had no symmetric recovery, so
    a crash left an orphan ``airlines_old``/``airlines_new`` indefinitely.
    Three table-name presences are possible after an interrupted run:
      * ``airlines_new`` only → build phase crashed; drop the stale staging
        table.
      * ``airlines_old`` only (no ``airlines``) → first RENAME succeeded but
        the second didn't. Rename old back to canonical.
      * ``airlines`` + ``airlines_old`` → second RENAME succeeded but the
        final DROP didn't. Drop the leftover old copy.
    """
    _recover_swap(conn, "airlines")


def _recover_swap(conn: sqlite3.Connection, base: str) -> None:
    """Shared staging-swap recovery for ``recover_aircraft_db_swap`` /
    ``recover_airlines_db_swap`` (Audit 17 — the two were byte-identical apart
    from the table name). ``base`` is the canonical table; ``{base}_new`` /
    ``{base}_old`` are the staging copies. ``base`` is an internal literal
    (never user input), so interpolating it into the DDL is safe.
    """
    _log = logging.getLogger(__name__)
    new, old = f"{base}_new", f"{base}_old"
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN (?, ?, ?)",
            (base, new, old),
        ).fetchall()
    }
    if base not in names and old in names:
        _log.warning("%s recovery: restoring %s after interrupted swap", base, old)
        conn.execute(f"ALTER TABLE {old} RENAME TO {base}")
    elif old in names:
        _log.info("%s recovery: dropping leftover %s", base, old)
        conn.execute(f"DROP TABLE {old}")
    if new in names:
        _log.info("%s recovery: dropping leftover %s", base, new)
        conn.execute(f"DROP TABLE {new}")
    # F02 follow-up (Audit 18): the 'canonical present + _old leftover' branch
    # above drops aircraft_db_old — and the type_code index that followed the
    # first RENAME lives on that old table, so dropping it leaves the new
    # canonical aircraft_db unindexed (update_aircraft_db's post-swap CREATE
    # never ran in the interrupted case). photo_sources.py JOINs
    # aircraft_db.type_code on every type-fallback lookup, so re-create the
    # index. Safe across all three branches: it only runs once both staging
    # tables (and their copy of the index) are gone, so the schema-global index
    # name is free, and IF NOT EXISTS makes it a no-op when the index survived.
    if base == "aircraft_db" and base in {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (base,)
        ).fetchall()
    }:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aircraft_db_type_code "
            "ON aircraft_db(type_code)"
        )
    conn.commit()


def ensure_base_schema(path: str = config.DB_PATH) -> None:
    """Web-server startup bootstrap (BE-3, Audit 2026-05-31).

    Brings a database to a queryable baseline without ever running slow
    operations synchronously (no full ``positions`` scans, no large composite
    index builds — those stay collector-owned in ``run_background_migrations``).
    Steps:
      1. Recover an interrupted aircraft_db swap (before any DDL, so a real
         ``aircraft_db_old`` is never discarded).
      2. Run the full DDL only when base tables (``flights`` / ``positions``)
         are missing — i.e. a genuinely fresh DB. Existing DBs skip the DDL.
      3. Record the schema version on a fresh DB.
      4. Run ``_migrate()`` for incremental ALTER/CREATE-IF-NOT-EXISTS upgrades.

    Replaces the bare ``_migrate()`` the lifespan used to call, which raised
    ``no such table`` against an empty ``RSBS_DB_PATH``.
    """
    conn = connect(path)
    try:
        recover_aircraft_db_swap(conn)
        recover_airlines_db_swap(conn)
        base = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('flights', 'positions')"
            ).fetchall()
        }
        if not {"flights", "positions"} <= base:
            conn.executescript(DDL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "VALUES (?, strftime('%s','now'))",
                (SCHEMA_VERSION,),
            )
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def init_db(path: str = config.DB_PATH) -> None:
    """Create tables/indexes if absent, run migrations, record schema version."""
    conn = connect(path)
    try:
        # BE-3: recover an interrupted aircraft_db swap BEFORE the DDL runs.
        # executescript(DDL) re-creates an empty aircraft_db; if a real
        # aircraft_db_old were the only surviving copy, the recovery's
        # 'canonical present + _old leftover' branch would then drop it.
        recover_aircraft_db_swap(conn)
        recover_airlines_db_swap(conn)
        conn.executescript(DDL)
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s','now'))",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
