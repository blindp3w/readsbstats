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
CREATE INDEX IF NOT EXISTS idx_vdl2_ts     ON vdl2_messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_icao   ON vdl2_messages(icao_hex, ts DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_label  ON vdl2_messages(label, ts DESC);
CREATE INDEX IF NOT EXISTS idx_vdl2_reg    ON vdl2_messages(registration, ts DESC);
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the VDL2 schema if absent and stamp ``user_version``.

    Idempotent and safe to call concurrently from the web process and the
    ingest collector (CREATE ... IF NOT EXISTS + a version guard). Cheap —
    no slow scans — so unlike the core schema it may run in the web path.
    """
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver >= VDL2_SCHEMA_VERSION:
        return
    conn.executescript(_DDL_MESSAGES)
    if fts5_available(conn):
        conn.executescript(_DDL_FTS)
    conn.execute(f"PRAGMA user_version = {VDL2_SCHEMA_VERSION}")
    conn.commit()


def insert_messages(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert normalized records (dicts keyed by COLUMNS). Returns the count."""
    if not records:
        return 0
    placeholders = ", ".join("?" * len(COLUMNS))
    sql = f"INSERT INTO vdl2_messages ({', '.join(COLUMNS)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r.get(c) for c in COLUMNS) for r in records])
    return len(records)


def prune(conn: sqlite3.Connection, retention_days: int, *, now: int | None = None) -> int:
    """Delete messages older than ``retention_days``. 0 = keep forever.
    Returns the number of rows removed. FTS stays in sync via the triggers."""
    if retention_days <= 0:
        return 0
    cutoff = (now if now is not None else int(time.time())) - retention_days * 86400
    cur = conn.execute("DELETE FROM vdl2_messages WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Web-side per-thread reader connection. Mirrors api/_deps.py: tests inject an
# in-memory connection by setting ``_conn`` directly; production opens one
# WAL connection per thread, lazily.
# ---------------------------------------------------------------------------
_conn: sqlite3.Connection | None = None   # test override; None in production
_thread_local = threading.local()


def web_conn() -> sqlite3.Connection:
    if _conn is not None:
        return _conn
    c = getattr(_thread_local, "conn", None)
    if c is None:
        c = connect()
        ensure_schema(c)
        _thread_local.conn = c
    return c
