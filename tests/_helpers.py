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
