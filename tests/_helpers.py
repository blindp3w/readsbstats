"""Shared test helpers — extracted from per-test duplicates.

Audit-13 A13-085 / A13-086 created this module to deduplicate code
that was repeated identically across many test files:

- `make_db()` was defined in 13 test files (one connection, full DDL,
  `_migrate()`). Centralising it removes the drift surface — when
  schema rules change, only this file needs to know.
- `CountingConn` was defined in three purge test files. Same drift
  argument.

Anything added here should be either thread-of-truth (single source
for behaviour repeated everywhere) or a thin fixture-style wrapper.
Avoid adding per-test logic that only one file consumes.
"""

from __future__ import annotations

import sqlite3

from readsbstats import database


def make_db() -> sqlite3.Connection:
    """Fresh in-memory SQLite with the full DDL + migrations applied.

    Matches the production startup path: `database.connect()` (which sets
    WAL + busy_timeout + the project's pragmas), then `executescript(DDL)`
    for tables/indexes, then `_migrate()` for any column additions or
    background-migration-relevant schema bumps. Always returns the same
    shape; if a test needs a real-file DB, it should use a tempfile path
    and call `database.connect(path)` directly.
    """
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def make_vdl2_db() -> sqlite3.Connection:
    """Fresh in-memory VDL2 SQLite with its schema applied.

    Parallels :func:`make_db` but for the separate ``vdl2.db`` store. Uses
    the feature's own connect()/ensure_schema() so FTS5 + triggers are built
    exactly as in production (when this SQLite build has FTS5)."""
    from readsbstats.vdl2 import db as vdl2_db

    conn = vdl2_db.connect(":memory:")
    vdl2_db.ensure_schema(conn)
    return conn


def insert_position(conn, flight_id, ts, lat=None, lon=None, alt_baro=None,
                    alt_geom=None, gs=None, track=None, baro_rate=None,
                    rssi=None, source_type="adsb_icao"):
    """Insert one v6 positions row from HUMAN units (degrees, knots, dB).
    Tests must use this instead of hand-written INSERT INTO positions —
    it owns the posenc encoding so schema changes touch one place.
    Returns the new row id."""
    from readsbstats import posenc
    cur = conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, alt_baro, alt_geom,"
        " gs, track, baro_rate, rssi, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (flight_id, ts, posenc.enc5(lat), posenc.enc5(lon), alt_baro, alt_geom,
         posenc.enc1(gs), posenc.enc1(track), baro_rate, posenc.enc1(rssi),
         posenc.encode_source(source_type)),
    )
    return cur.lastrowid


class CountingConn:
    """Sqlite3 connection wrapper that counts `.commit()` calls.

    Used by `tests/test_purge_*.py` to assert that batched purges commit
    once per `_BATCH_SIZE` flights — not per flight, not once at the end.
    Forwards every other attribute access to the wrapped connection.
    """

    def __init__(self, c: sqlite3.Connection):
        self._c = c
        self.commits = 0

    def __getattr__(self, name: str):  # pragma: no cover — passthrough
        return getattr(self._c, name)

    def commit(self) -> None:
        self.commits += 1
        self._c.commit()

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            # `with conn:` commits on clean exit — count it like .commit()
            self.commits += 1
        return self._c.__exit__(exc_type, exc, tb)
