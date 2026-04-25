"""
readsbstats — database initialisation, schema, and connection management.
"""
import logging
import sqlite3
from . import config

SCHEMA_VERSION = 5

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
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

CREATE INDEX IF NOT EXISTS idx_flights_icao     ON flights(icao_hex);
CREATE INDEX IF NOT EXISTS idx_flights_first    ON flights(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_flights_callsign ON flights(callsign);
CREATE INDEX IF NOT EXISTS idx_flights_reg      ON flights(registration);
CREATE INDEX IF NOT EXISTS idx_flights_type     ON flights(aircraft_type);
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

CREATE INDEX IF NOT EXISTS idx_positions_flight ON positions(flight_id);
CREATE INDEX IF NOT EXISTS idx_positions_ts     ON positions(ts);

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
CREATE TABLE IF NOT EXISTS airports (
    icao_code   TEXT PRIMARY KEY,
    iata_code   TEXT,
    name        TEXT,
    country     TEXT,
    latitude    REAL,
    longitude   REAL,
    fetched_at  INTEGER NOT NULL
);

-- Route cache: callsign → origin/dest airport ICAO codes
-- NULL origin_icao + NULL dest_icao means "confirmed unknown, don't retry until fetched_at expires"
CREATE TABLE IF NOT EXISTS callsign_routes (
    callsign    TEXT PRIMARY KEY,
    origin_icao TEXT,
    dest_icao   TEXT,
    fetched_at  INTEGER NOT NULL
);

-- Cached photo URLs from Planespotters.net (keyed by ICAO hex)
CREATE TABLE IF NOT EXISTS photos (
    icao_hex      TEXT PRIMARY KEY,
    thumbnail_url TEXT,
    large_url     TEXT,
    link_url      TEXT,
    photographer  TEXT,
    fetched_at    INTEGER NOT NULL
);

-- User-defined aircraft watchlist (Telegram alerts on new flight)
CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL,   -- 'icao' | 'registration' | 'callsign_prefix'
    value      TEXT NOT NULL,   -- stored lowercase
    label      TEXT,
    created_at INTEGER NOT NULL
);

-- ADSBexchange-confirmed flags & enrichment (survives weekly tar1090-db refresh)
CREATE TABLE IF NOT EXISTS adsbx_overrides (
    icao_hex     TEXT PRIMARY KEY,
    flags        INTEGER DEFAULT 0,   -- dbFlags bitmask (military=1, interesting=2, PIA=4, LADD=8)
    registration TEXT,
    type_code    TEXT,
    type_desc    TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);

-- Receiver metrics time-series (metrics_collector.py)
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
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
"""


def connect(path: str = config.DB_PATH) -> sqlite3.Connection:
    """Return a connection with WAL mode and row_factory set."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "PRAGMA journal_mode = WAL;"
        "PRAGMA synchronous  = NORMAL;"
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
        "watchlist_alerted": "INTEGER DEFAULT 0",
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
    # Backfill NULL primary_source on closed flights (crash recovery)
    conn.execute(
        "UPDATE flights SET primary_source = 'other' "
        "WHERE primary_source IS NULL "
        "AND id NOT IN (SELECT flight_id FROM active_flights)"
    )

    # Composite index for /api/live: latest position per active flight
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_flight_id_desc "
        "ON positions(flight_id, id DESC)"
    )

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            match_type TEXT NOT NULL,
            value      TEXT NOT NULL,
            label      TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_type_value "
        "ON watchlist(match_type, value)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adsbx_overrides (
            icao_hex     TEXT PRIMARY KEY,
            flags        INTEGER DEFAULT 0,
            registration TEXT,
            type_code    TEXT,
            type_desc    TEXT,
            first_seen   INTEGER NOT NULL,
            last_seen    INTEGER NOT NULL
        )
        """
    )

    # Receiver metrics time-series (metrics_collector.py)
    conn.execute(
        """
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
    )

    conn.commit()

    # One-time backfill: compute max_distance_bearing for flights that have
    # max_distance_nm but no bearing yet.  Processes in batches of 500 to
    # avoid holding the WAL write lock for too long (collector writes
    # concurrently).
    rlat, rlon = config.RECEIVER_LAT, config.RECEIVER_LON
    need_backfill = conn.execute(
        "SELECT COUNT(*) FROM flights "
        "WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NULL"
    ).fetchone()[0]
    if need_backfill:
        log = logging.getLogger(__name__)
        log.info("Backfilling max_distance_bearing for %d flights …", need_backfill)
        conn.execute("PRAGMA busy_timeout = 10000")
        batch_size = 500
        done = 0
        while done < need_backfill:
            conn.execute(
                f"""
                UPDATE flights SET max_distance_bearing = (
                    SELECT (degrees(atan2(
                        sin(radians(p.lon - {rlon})) * cos(radians(p.lat)),
                        cos(radians({rlat})) * sin(radians(p.lat))
                            - sin(radians({rlat})) * cos(radians(p.lat))
                              * cos(radians(p.lon - {rlon}))
                    )) + 360) % 360
                    FROM positions p
                    WHERE p.flight_id = flights.id
                      AND p.lat IS NOT NULL AND p.lon IS NOT NULL
                    ORDER BY (
                        sin(radians((p.lat - {rlat}) / 2)) * sin(radians((p.lat - {rlat}) / 2))
                      + cos(radians({rlat})) * cos(radians(p.lat))
                      * sin(radians((p.lon - {rlon}) / 2)) * sin(radians((p.lon - {rlon}) / 2))
                    ) DESC
                    LIMIT 1
                )
                WHERE id IN (
                    SELECT id FROM flights
                    WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NULL
                    LIMIT {batch_size}
                )
                """
            )
            conn.commit()
            done += batch_size
            if done < need_backfill:
                log.info("  … %d / %d", min(done, need_backfill), need_backfill)
        log.info("Backfill complete.")


def init_db(path: str = config.DB_PATH) -> None:
    """Create tables/indexes if absent, run migrations, record schema version."""
    conn = connect(path)
    try:
        conn.executescript(DDL)
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s','now'))",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
