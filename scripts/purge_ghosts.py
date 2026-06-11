#!/usr/bin/env python3
"""
purge_ghosts.py — one-shot cleanup of ghost positions from the positions table.

Ghost positions are ADS-B outliers where the implied speed from the preceding
good position in the same flight exceeds MAX_SPEED_KTS.  The same logic as the
collector's real-time filter: a rejected position does NOT advance the reference
point, so a single ghost does not cascade and mark the next real position bad.

After removing ghosts, max_distance_nm in the flights table is recomputed from
the surviving positions.

Usage:
    python purge_ghosts.py [--db PATH] [--max-speed N] [--apply]

Dry-run (default) — prints a report without touching the DB.
Pass --apply to commit the changes.
"""

import argparse
import itertools
import sqlite3

from readsbstats import config, database, geo

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

haversine_nm = geo.haversine_nm


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _velocity_pass(
    positions: list,
    max_speed_kts: int,
    reverse: bool = False,
) -> tuple[list[int], int]:
    """
    Single-direction velocity pass over a position list.

    Returns (ghost_ids, survivor_count).  When reverse=True the list is
    iterated right-to-left (catches opening ghosts from the far end).
    Rejected positions are NOT used as the reference for the next comparison.
    """
    seq = reversed(positions) if reverse else iter(positions)
    prev = None
    ghost_ids: list[int] = []
    survivors = 0

    for pos in seq:
        # Skip rows without coordinates — haversine_nm would crash on None.
        if pos["lat"] is None or pos["lon"] is None:
            continue
        if prev is not None:
            dt = abs(pos["ts"] - prev["ts"])
            if dt > 0:
                dist = haversine_nm(prev["lat"], prev["lon"], pos["lat"], pos["lon"])
                implied_kts = dist / (dt / 3600.0)
                if implied_kts > max_speed_kts:
                    ghost_ids.append(pos["id"])
                    continue
        prev = pos
        survivors += 1

    return ghost_ids, survivors


def find_ghost_ids(
    conn: sqlite3.Connection,
    max_speed_kts: int,
) -> dict[int, list[int]]:
    """
    Scan every flight and return {flight_id: [ghost_position_ids]}.

    Uses a forward velocity pass.  If only one position survives (meaning the
    very first position was a bad anchor that poisoned all comparisons), falls
    back to a backward pass which correctly identifies the opening ghost.

    improvements.md #126: streams one ordered query through ``itertools.groupby``
    instead of issuing ``SELECT DISTINCT flight_id`` followed by one
    per-flight SELECT.  On a 35 k-flight DB that's ~35 k round trips
    eliminated.
    """
    # v6 positions: lat/lon are scaled INTEGERs (×1e5) — decode in SQL.
    cursor = conn.execute(
        "SELECT flight_id, id, ts, lat / 100000.0 AS lat, lon / 100000.0 AS lon "
        "FROM positions "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY flight_id, ts"
    )

    ghosts: dict[int, list[int]] = {}

    for fid, group in itertools.groupby(cursor, key=lambda r: r["flight_id"]):
        positions = list(group)

        ghost_ids, survivors = _velocity_pass(positions, max_speed_kts, reverse=False)

        if survivors * 2 < len(positions):
            # More than half the positions were flagged — the first position was
            # likely a ghost anchor that poisoned all comparisons.  The backward
            # pass starts from the real track end and correctly identifies it.
            ghost_ids, _ = _velocity_pass(positions, max_speed_kts, reverse=True)

        if ghost_ids:
            ghosts[fid] = ghost_ids

    return ghosts


def max_distance_after_purge(
    conn: sqlite3.Connection,
    flight_id: int,
    ghost_ids: list[int],
    rlat: float,
    rlon: float,
) -> float | None:
    """Compute the new max_distance_nm excluding the ghost positions."""
    # SQLite accepts `NOT IN ()` but the standard SQL grammar forbids it —
    # use a plain WHERE when there are no exclusions (also clearer to read).
    if ghost_ids:
        placeholders = ",".join("?" * len(ghost_ids))
        rows = conn.execute(
            f"SELECT lat / 100000.0 AS lat, lon / 100000.0 AS lon "
            f"FROM positions WHERE flight_id = ? AND id NOT IN ({placeholders})",
            [flight_id] + ghost_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT lat / 100000.0 AS lat, lon / 100000.0 AS lon "
            "FROM positions WHERE flight_id = ?",
            (flight_id,),
        ).fetchall()
    if not rows:
        return None
    return max(haversine_nm(rlat, rlon, r["lat"], r["lon"]) for r in rows)


# Commit every N flights so a multi-thousand-flight purge doesn't hold the
# SQLite write lock for the whole run (would starve the collector). The
# delete/update for a single flight stays in one transaction; only the
# batch boundary commits early.
# Audit-13 A13-084: constant lives in `_purge_helpers.BATCH_SIZE` so the
# three purge scripts can't drift.
from _purge_helpers import BATCH_SIZE as _BATCH_SIZE


def apply_purge(
    conn: sqlite3.Connection,
    ghosts: dict[int, list[int]],
    rlat: float,
    rlon: float,
) -> None:
    """Delete ghost positions and recompute max_distance_nm for affected flights.

    NOT atomic across the whole run. Audit-12 Phase 3 switched from one
    transaction wrapping the entire flight loop to per-``_BATCH_SIZE``
    commits — the original blanket transaction held the SQLite write
    lock long enough to starve the collector on multi-thousand-flight
    purges. The trade-off: a Ctrl-C / OSError / disk-full mid-run leaves
    the DB partially purged (some flights have updated
    ``max_distance_nm``, others still reference deleted position IDs).

    Each *individual* flight's ``DELETE positions`` + ``UPDATE flights``
    pair is still atomic (one transaction up to the batch boundary).
    The script is idempotent: re-running it after an interrupted run
    finishes the work cleanly because the find-phase rescans every flight.
    """
    pending = 0
    for fid, ghost_ids in ghosts.items():
        placeholders = ",".join("?" * len(ghost_ids))
        conn.execute(
            f"DELETE FROM positions WHERE id IN ({placeholders})", ghost_ids
        )
        new_max = max_distance_after_purge(conn, fid, [], rlat, rlon)
        conn.execute(
            "UPDATE flights SET max_distance_nm = ? WHERE id = ?", (new_max, fid)
        )
        pending += 1
        if pending >= _BATCH_SIZE:
            conn.commit()
            pending = 0
    if pending:
        conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove ghost ADS-B positions and fix max_distance_nm"
    )
    parser.add_argument("--db",        default=config.DB_PATH,        help="SQLite DB path")
    parser.add_argument("--max-speed", default=config.MAX_SPEED_KTS,  type=int,
                        help=f"Speed threshold in kts (default: {config.MAX_SPEED_KTS})")
    parser.add_argument("--apply",     action="store_true",
                        help="Commit changes (default: dry-run)")
    parser.add_argument("--i-have-a-backup", action="store_true",
                        help="Skip the automatic VACUUM INTO snapshot taken "
                             "before --apply (you've made one yourself)")
    args = parser.parse_args()

    # Audit-13 A13-056: use the shared connection helper so the script
    # inherits WAL mode + busy_timeout=30s — raw sqlite3.connect leaves
    # busy_timeout at 0 and fails immediately under collector contention.
    conn = database.connect(args.db)

    print(f"Scanning {args.db}  threshold: {args.max_speed} kts  "
          f"({'APPLY' if args.apply else 'dry-run'})")

    ghosts = find_ghost_ids(conn, args.max_speed)
    total = sum(len(v) for v in ghosts.values())

    if total == 0:
        print("No ghost positions found.")
        conn.close()
        return

    print(f"\n{total} ghost position(s) across {len(ghosts)} flight(s):\n")
    rlat, rlon = config.RECEIVER_LAT, config.RECEIVER_LON

    for fid, ghost_ids in sorted(ghosts.items()):
        flight = conn.execute(
            "SELECT icao_hex, callsign, max_distance_nm FROM flights WHERE id = ?",
            (fid,),
        ).fetchone()
        label = f"{flight['icao_hex']} {flight['callsign'] or ''}".strip() if flight else f"flight {fid}"
        old_max = flight["max_distance_nm"] if flight else None
        new_max = max_distance_after_purge(conn, fid, ghost_ids, rlat, rlon)
        old_str = f"{old_max:.1f}" if old_max is not None else "NULL"
        new_str = f"{new_max:.1f}" if new_max is not None else "NULL"
        print(f"  [{fid}] {label:20s}  {len(ghost_ids)} ghost(s)  "
              f"max_dist {old_str} → {new_str} nm")

    if not args.apply:
        print("\nDry-run — pass --apply to commit changes.")
        conn.close()
        return

    if not args.i_have_a_backup:
        snapshot = database.snapshot_db(args.db)
        print(f"\nSnapshot: {snapshot}")

    apply_purge(conn, ghosts, rlat, rlon)
    print(f"\nDone — removed {total} ghost position(s), "
          f"updated {len(ghosts)} flight(s).")
    conn.close()


if __name__ == "__main__":
    main()
