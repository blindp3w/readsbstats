"""VDL2 message store — a SEPARATE SQLite database (``RSBS_VDL2_DB_PATH``).

Deliberately independent of ``database.py`` (the core ``history.db`` and its
SCHEMA_VERSION machinery). This module owns its own connection helper, schema,
and tiny migration via ``PRAGMA user_version`` so the two databases can never
collide or drift. The core schema is never touched by this feature.

Full-text search uses an FTS5 external-content table over the message body.
FTS5 ships in standard Debian/Ubuntu SQLite, but the Pi runs 3.45 and the dev
box 3.50 (see memory/project_sqlite_version_skew.md), so ``fts5_available()``
probes at schema-build time and the schema/API degrade to ``LIKE`` if absent.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from .. import config

VDL2_SCHEMA_VERSION = 1

# Insert column order — single source of truth shared by the collector's
# insert path and the tests. ``id`` (rowid alias) is auto-assigned.
COLUMNS = (
    "ts", "icao_hex", "registration", "flight", "label", "mode",
    "block_id", "ack", "msgno", "freq", "station_id", "toaddr", "dsta",
    "lat", "lon", "alt", "epu", "app_name", "app_ver", "body", "raw", "decoder",
)

_DDL_MESSAGES = """
CREATE TABLE IF NOT EXISTS vdl2_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,          -- epoch seconds (UTC)
    icao_hex     TEXT,                      -- 24-bit Mode-S address, lowercase; joins core flights.icao_hex
    registration TEXT,
    flight       TEXT,                      -- callsign / flight id
    label        TEXT,                      -- 2-char ACARS message label
    mode         TEXT,
    block_id     TEXT,
    ack          TEXT,
    msgno        TEXT,
    freq         REAL,                      -- MHz
    station_id   TEXT,
    toaddr       TEXT,
    dsta         TEXT,                      -- destination (ICAO/IATA as decoded)
    lat          REAL,
    lon          REAL,
    alt          INTEGER,
    epu          REAL,
    app_name     TEXT,
    app_ver      TEXT,
    body         TEXT,                      -- decoded message text (capped at ingest)
    raw          TEXT,                      -- full decoder JSON, verbatim (fidelity / re-parse)
    decoder      TEXT                       -- which decoder produced it
);
"""

# Indexes are created by migrate() (idempotent, run on every open) so additions
# reach already-created DBs without a user_version bump. The feed orders by
# `id DESC` with label/hex/reg filters → `(col, id DESC)` indexes; the
# flight-panel per-aircraft query filters icao + ts window → `(icao_hex, ts)`.
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_vdl2_ts       ON vdl2_messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_icao     ON vdl2_messages(icao_hex, ts DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_label_id ON vdl2_messages(label, id DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_icao_id  ON vdl2_messages(icao_hex, id DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_reg_id   ON vdl2_messages(registration COLLATE NOCASE, id DESC);
-- Partial index for the map positions overlay (/api/vdl2/positions): only the
-- sparse structured-position rows are indexed, so the lat/lon + ts filter is
-- served without scanning the (mostly position-less) table.
CREATE INDEX IF NOT EXISTS idx_vdl2_pos      ON vdl2_messages(ts DESC) WHERE lat IS NOT NULL AND lon IS NOT NULL;
"""

# External-content FTS5 over body. Triggers keep it in sync; the delete/update
# triggers use the special 'delete' command form required for external-content
# tables so retention DELETEs don't leave orphaned FTS rows.
_DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS vdl2_fts
    USING fts5(body, content='vdl2_messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS vdl2_ai AFTER INSERT ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS vdl2_ad AFTER DELETE ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(vdl2_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS vdl2_au AFTER UPDATE ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(vdl2_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO vdl2_fts(rowid, body) VALUES (new.id, new.body);
END;
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a WAL connection to the VDL2 DB. Mirrors the core pragma set
    (database.connect) minus foreign_keys (this schema has none)."""
    conn = sqlite3.connect(path or config.VDL2_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "PRAGMA journal_mode = WAL;"
        # synchronous=NORMAL (not core's FULL): VDL2 is lossy best-effort SDR data
        # — on power loss WAL+NORMAL can drop only the last few committed messages
        # (never corruption), an acceptable trade for avoiding an fsync per commit
        # on the Pi. Core history.db keeps FULL (see docs/configuration.md).
        "PRAGMA synchronous  = NORMAL;"
        "PRAGMA busy_timeout = 30000;"
        "PRAGMA cache_size   = -16384;"
        "PRAGMA wal_autocheckpoint = 1000;"
    )
    return conn


def fts5_available(conn: sqlite3.Connection) -> bool:
    """True if this SQLite build has FTS5 (probe a throwaway temp table)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__vdl2_fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__vdl2_fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def has_fts(conn: sqlite3.Connection) -> bool:
    """True if the FTS index exists in this DB (built when FTS5 is available)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vdl2_fts'"
    ).fetchone()
    return row is not None


def ensure_schema(conn: sqlite3.Connection, *, build_fts: bool = True) -> None:
    """Create the VDL2 base table (version-gated) then run the idempotent
    `migrate()`. Safe to call concurrently from web + ingest (IF NOT EXISTS +
    a version guard). ``build_fts=False`` (web path) skips the only potentially
    slow step — a populated FTS rebuild — leaving that to the collector."""
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < VDL2_SCHEMA_VERSION:
        conn.executescript(_DDL_MESSAGES)
        conn.execute(f"PRAGMA user_version = {VDL2_SCHEMA_VERSION}")
        conn.commit()
    migrate(conn, build_fts=build_fts)


def migrate(conn: sqlite3.Connection, *, build_fts: bool = True) -> None:
    """Idempotent upgrades NOT gated by user_version, so they reach already-created
    DBs: create any missing indexes, and create FTS if FTS5 is available but the
    index is absent.

    The only slow case is the FTS *rebuild* that populates the index from existing
    rows (the "DB created on a no-FTS build, reopened with FTS5" skew). That scan
    takes a write lock, so it's gated on ``build_fts`` — the collector (writer)
    runs it; the web path passes ``build_fts=False`` and, when rows already exist,
    leaves FTS absent so search falls back to LIKE instead of MATCHing a
    half-populated index (which would silently miss old rows)."""
    conn.executescript(_DDL_INDEXES)
    if fts5_available(conn) and not has_fts(conn):
        has_rows = conn.execute("SELECT 1 FROM vdl2_messages LIMIT 1").fetchone() is not None
        if build_fts or not has_rows:
            conn.executescript(_DDL_FTS)
            if has_rows:
                conn.execute("INSERT INTO vdl2_fts(vdl2_fts) VALUES('rebuild')")
    conn.commit()


def insert_messages(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert normalized records (dicts keyed by COLUMNS). Returns the count."""
    if not records:
        return 0
    placeholders = ", ".join("?" * len(COLUMNS))
    sql = f"INSERT INTO vdl2_messages ({', '.join(COLUMNS)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r.get(c) for c in COLUMNS) for r in records])
    return len(records)


def prune(
    conn: sqlite3.Connection, retention_days: int, *, now: int | None = None, batch: int = 5000
) -> int:
    """Delete messages older than ``retention_days`` in bounded batches. 0 = keep
    forever. Returns the total rows removed. FTS stays in sync via the triggers.

    Batched (commit per batch) so the first big retention run on a large DB can't
    hold the write lock long enough to starve the ingest writer — the per-row FTS
    delete trigger amplifies the work, so one unbounded DELETE could block for
    seconds. (`DELETE ... LIMIT` isn't available on stock SQLite, hence the
    `id IN (SELECT ... LIMIT)` form.)"""
    if retention_days <= 0:
        return 0
    cutoff = (now if now is not None else int(time.time())) - retention_days * 86400
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM vdl2_messages WHERE id IN "
            "(SELECT id FROM vdl2_messages WHERE ts < ? ORDER BY ts LIMIT ?)",
            (cutoff, batch),
        )
        conn.commit()
        total += cur.rowcount
        if cur.rowcount < batch:
            return total


# ---------------------------------------------------------------------------
# Web-side per-thread reader connection. Mirrors api/_deps.py: tests inject an
# in-memory connection by setting ``_conn`` directly; production opens one
# WAL connection per thread, lazily.
# ---------------------------------------------------------------------------
_conn: sqlite3.Connection | None = None   # test override; None in production
_thread_local = threading.local()
# Registry of per-thread web connections so the web lifespan can close them all
# on shutdown (a thread can't reach another thread's thread-local). Guarded by a
# lock since connections are created on uvicorn threadpool threads.
_web_conns: list[sqlite3.Connection] = []
_web_conns_lock = threading.Lock()


def web_conn() -> sqlite3.Connection:
    if _conn is not None:
        return _conn
    c = getattr(_thread_local, "conn", None)
    if c is None:
        c = connect()
        ensure_schema(c, build_fts=False)   # never run a slow FTS rebuild on a web thread
        _thread_local.conn = c
        with _web_conns_lock:
            _web_conns.append(c)
    return c


def close_all_web_conns() -> None:
    """Close every web reader connection (called from the web lifespan shutdown)
    so connections are released cleanly and attach state can't persist stale
    across a runtime DB change. Best-effort; ignores already-closed conns."""
    with _web_conns_lock:
        conns, _web_conns[:] = list(_web_conns), []
    for c in conns:
        try:
            c.close()
        except sqlite3.Error:
            pass
