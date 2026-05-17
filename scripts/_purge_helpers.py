"""Shared helpers for the one-shot purge scripts.

Audit-12 #199 — `new_max_gs` was duplicated identically across
`purge_bad_gs.py` and `purge_mlat_gs_spikes.py`. A fix in one half would
silently drift from the other (see the empty-list `IN ()` guard that
landed in Phase 1). Centralising here keeps them in sync.
"""

from __future__ import annotations

import sqlite3


def new_max_gs(
    conn: sqlite3.Connection,
    flight_id: int,
    bad_ids: list[int],
) -> float | None:
    """Return max gs from positions excluding the bad ones.

    SQLite accepts `NOT IN ()` but the standard SQL grammar forbids it —
    use a plain WHERE when there are no exclusions (also clearer to read).
    """
    if bad_ids:
        placeholders = ",".join("?" * len(bad_ids))
        row = conn.execute(
            f"SELECT MAX(gs) FROM positions "
            f"WHERE flight_id = ? AND id NOT IN ({placeholders}) AND gs IS NOT NULL",
            [flight_id] + bad_ids,
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(gs) FROM positions "
            "WHERE flight_id = ? AND gs IS NOT NULL",
            (flight_id,),
        ).fetchone()
    return row[0] if row else None
