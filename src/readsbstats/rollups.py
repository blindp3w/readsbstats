"""Incremental daily rollups for heatmap and coverage.

The collector accumulates per-poll counts in memory and flushes them inside
the same transaction as the position inserts, so rollups can never drift
from committed positions. Heatmap/coverage API windows ≥7d read these
tables (thousands of rows) instead of scanning the multi-million-row
positions table — and the rollups survive raw-position retention, so
all-time analytics outlive any purge horizon.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import Counter

from . import config

log = logging.getLogger(__name__)

FINE_SCALE = 100    # 0.01° ≈ 1 km cells — 24h/7d display precision
COARSE_SCALE = 10   # 0.1°  ≈ 11 km cells — 30d/all display precision
SCALES = (FINE_SCALE, COARSE_SCALE)  # iterated by accumulator; reuse for backfill


def bucket(value: float, scale: int) -> int:
    """Half-up grid bucket; must stay identical to the SQL expression
    FLOOR(value*scale + 0.5).

    Two SQL twins exist:
    - Raw-float live path: ``CAST(FLOOR(lat * scale + 0.5) AS INTEGER)``
      (24h backfill, used directly on the float from readsb).
    - Quantized decode path: ``CAST(FLOOR(lat_enc / 100000.0 * scale + 0.5) AS INTEGER)``
      where lat_enc is the v6 INTEGER column (= posenc.enc5(lat)).
    Values within 5e-6° of a cell edge may differ by one fine cell between the
    two paths due to integer quantisation; this is accepted.
    """
    return math.floor(value * scale + 0.5)


class RollupAccumulator:
    """Per-poll in-memory aggregation; one instance per _poll() cycle."""

    def __init__(self) -> None:
        self.grid: Counter[tuple[int, int, int, int]] = Counter()
        self.cov: dict[tuple[int, int], float] = {}

    def add(self, ts: int, lat: float, lon: float,
            dist_nm: float, bearing_deg: float) -> None:
        """Accumulate one position sample into the in-memory counters.

        SQL twins:
        - ``day = ts // 86400`` mirrors SQL ``ts / 86400`` on INTEGER ts
          (SQLite integer division truncates toward zero, matching Python ``//``
          for positive Unix timestamps).
        - Bucket values match SQL ``CAST(FLOOR(lat*scale + 0.5) AS INTEGER)``
          via :func:`bucket`.

        Precondition: ``bearing_deg`` must be in [0, 360) as returned by
        :func:`geo.bearing`.
        """
        day = ts // 86400
        for scale in SCALES:
            self.grid[(scale, day, bucket(lat, scale), bucket(lon, scale))] += 1
        key = (day, int(bearing_deg) % 360)
        if dist_nm > self.cov.get(key, 0.0):
            self.cov[key] = dist_nm


def flush(conn: sqlite3.Connection, acc: RollupAccumulator) -> None:
    """Upsert the accumulated counts. Caller owns the transaction (the
    collector calls this inside its per-poll `with conn:` block)."""
    if acc.grid:
        conn.executemany(
            "INSERT INTO grid_daily(scale, day, lat_b, lon_b, w) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(scale, day, lat_b, lon_b) "
            "DO UPDATE SET w = w + excluded.w",
            [(s, d, lb, lob, w) for (s, d, lb, lob), w in acc.grid.items()],
        )
    if acc.cov:
        conn.executemany(
            "INSERT INTO coverage_daily(day, bearing_b, max_nm) "
            "VALUES (?,?,?) "
            "ON CONFLICT(day, bearing_b) "
            "DO UPDATE SET max_nm = MAX(max_nm, excluded.max_nm)",
            [(d, b, nm) for (d, b), nm in acc.cov.items()],
        )
    acc.grid.clear()
    acc.cov.clear()


def prune_fine(conn: sqlite3.Connection, now: int) -> None:
    """Drop fine-scale (0.01°) rollup days beyond GRID_FINE_RETENTION_DAYS.
    Coarse rows are permanent. Caller owns the transaction."""
    cutoff_day = now // 86400 - config.GRID_FINE_RETENTION_DAYS
    conn.execute(
        "DELETE FROM grid_daily WHERE scale = ? AND day < ?",
        (FINE_SCALE, cutoff_day),
    )


def ready(conn: sqlite3.Connection) -> bool:
    """True once backfill_and_finalize() completed on this DB."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'rollups_ready'"
    ).fetchone()
    return bool(row and row[0] == "1")


def backfill_and_finalize(path: str = config.DB_PATH) -> None:
    """One-time build of grid_daily/coverage_daily from historical positions,
    then drop the legacy ts-composite indexes and set the rollups_ready flag.

    Runs in the collector's background-migrations thread. Day-batched: one
    transaction per UTC day so the writer lock is released between days and
    the poll loop interleaves. Only FULL past days are backfilled — today's
    counts come from the live accumulator. Deploy-day positions recorded before
    the collector restart are not backfilled; the loss is bounded to part of
    one day and only affects ≥7d window counts marginally.

    Re-runnable: a watermark stored in ``meta`` (key ``rollups_backfill_done_through``)
    tracks the last completed day so the loop resumes correctly after a restart.
    The watermark is written atomically inside each day's transaction, so a crash
    mid-day leaves no partial state. The ready flag short-circuits the whole call
    once backfill is complete.

    The grid upsert (ON CONFLICT … DO UPDATE SET w = w + excluded.w) handles the
    one-poll-overlap case: a collector poll straddling midnight may have already
    flushed a row for a day the backfill is now processing; the merge double-counts
    at most that one fix. The watermark (not a ``have`` set) governs resume, so a
    live-flushed row can never cause an entire day's historical rows to be skipped.
    """
    from . import database, geo  # lazy: database.run_background_migrations calls us

    conn = database.connect(path)
    try:
        if ready(conn):
            return
        log.info("Rollup backfill starting …")
        row = conn.execute("SELECT MIN(ts), MAX(ts) FROM positions").fetchone()
        if row[0] is not None:
            today = int(time.time()) // 86400
            wm_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'rollups_backfill_done_through'"
            ).fetchone()
            # v6 positions: lat/lon are scaled INTEGERs (×1e5) — decode in SQL.
            # (Safe to assume the v6 layout: this thread only starts after
            # init_db()/_migrate() succeeded, and _migrate() fails closed on a
            # legacy positions table — on any pre-v6 DB either the rebuild has
            # already happened or the service refused to start, so this
            # backfill never sees v5 rows.)
            bearing_expr = geo.bearing_sql("lat / 100000.0", "lon / 100000.0", ":rlat", ":rlon")
            dist_expr = geo.haversine_sql("lat / 100000.0", "lon / 100000.0", ":rlat", ":rlon")
            first_day = row[0] // 86400
            last_day = min(row[1] // 86400, today - 1)
            done_through = int(wm_row[0]) if wm_row else first_day - 1
            done = 0
            for day in range(max(first_day, done_through + 1), last_day + 1):
                lo, hi = day * 86400, (day + 1) * 86400
                with conn:
                    for scale in SCALES:
                        # Upsert (not plain INSERT): the watermark governs
                        # resume; this upsert handles only the one-poll-overlap
                        # case where the live accumulator already flushed a row
                        # for this day (e.g. a midnight-straddle fix). The merge
                        # double-counts at most that one poll's worth of fixes.
                        conn.execute(
                            """
                            INSERT INTO grid_daily(scale, day, lat_b, lon_b, w)
                            SELECT :scale, :day,
                                   CAST(FLOOR(lat / 100000.0 * :scale + 0.5) AS INTEGER),
                                   CAST(FLOOR(lon / 100000.0 * :scale + 0.5) AS INTEGER),
                                   COUNT(*)
                            FROM positions
                            WHERE ts >= :lo AND ts < :hi
                              AND lat IS NOT NULL AND lon IS NOT NULL
                            GROUP BY 3, 4
                            ON CONFLICT(scale, day, lat_b, lon_b)
                            DO UPDATE SET w = w + excluded.w
                            """,
                            {"scale": scale, "day": day, "lo": lo, "hi": hi},
                        )
                    # Inner GROUP BY, outer NULL guard: the trig expressions
                    # can yield NULL on float-domain edge cases (Audit 17 saw
                    # NULL coverage buckets), and coverage_daily's PK/NOT NULL
                    # would reject such rows. The outer WHERE also satisfies
                    # SQLite's upsert-after-SELECT parsing rule.
                    conn.execute(
                        f"""
                        INSERT INTO coverage_daily(day, bearing_b, max_nm)
                        SELECT :day, b, mx FROM (
                            SELECT CAST({bearing_expr} AS INTEGER) % 360 AS b,
                                   MAX({dist_expr}) AS mx
                            FROM positions
                            WHERE ts >= :lo AND ts < :hi
                              AND lat IS NOT NULL AND lon IS NOT NULL
                            GROUP BY b
                        )
                        WHERE b IS NOT NULL AND mx IS NOT NULL
                        ON CONFLICT(day, bearing_b)
                        DO UPDATE SET max_nm = MAX(max_nm, excluded.max_nm)
                        """,
                        {"day": day, "lo": lo, "hi": hi,
                         "rlat": config.RECEIVER_LAT, "rlon": config.RECEIVER_LON},
                    )
                    # Watermark commits atomically with this day's data: if the
                    # process is killed mid-backfill, this day reruns on restart.
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) "
                        "VALUES('rollups_backfill_done_through', ?)",
                        (str(day),),
                    )
                done += 1
                # Yield the write lock between days so the collector's 5 s poll
                # loop can grab it; SQLite's busy-handler makes immediate
                # re-acquisition by the backfill loop likely without this pause.
                time.sleep(0.25)
                if done % 10 == 0:
                    log.info("  … backfilled %d days", done)
        with conn:
            # These two only served the heatmap/coverage scans now answered
            # by the rollups. (~320 MB on the production DB.)
            conn.execute("DROP INDEX IF EXISTS idx_positions_ts_flight")
            conn.execute("DROP INDEX IF EXISTS idx_positions_ts_lat_lon")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('rollups_ready', '1')"
            )
        log.info("Rollup backfill complete.")
    except Exception:
        log.exception("rollup backfill failed")
    finally:
        conn.close()
