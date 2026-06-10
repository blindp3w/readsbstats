"""Incremental daily rollups for heatmap and coverage.

The collector accumulates per-poll counts in memory and flushes them inside
the same transaction as the position inserts, so rollups can never drift
from committed positions. Heatmap/coverage API windows ≥7d read these
tables (thousands of rows) instead of scanning the multi-million-row
positions table — and the rollups survive raw-position retention, so
all-time analytics outlive any purge horizon.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter

FINE_SCALE = 100    # 0.01° ≈ 1 km cells — 24h/7d display precision
COARSE_SCALE = 10   # 0.1°  ≈ 11 km cells — 30d/all display precision


def bucket(value: float, scale: int) -> int:
    """Half-up grid bucket; must stay identical to the SQL expression
    FLOOR(value*scale + 0.5) used by the backfill and the 24h raw path."""
    return math.floor(value * scale + 0.5)


class RollupAccumulator:
    """Per-poll in-memory aggregation; one instance per _poll() cycle."""

    def __init__(self) -> None:
        self.grid: Counter = Counter()
        self.cov: dict[tuple[int, int], float] = {}

    def add(self, ts: int, lat: float, lon: float,
            dist_nm: float, bearing_deg: float) -> None:
        day = ts // 86400
        for scale in (FINE_SCALE, COARSE_SCALE):
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


def ready(conn: sqlite3.Connection) -> bool:
    """True once backfill_and_finalize() completed on this DB."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'rollups_ready'"
    ).fetchone()
    return bool(row and row[0] == "1")
