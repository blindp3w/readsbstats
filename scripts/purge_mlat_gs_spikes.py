#!/usr/bin/env python3
"""
purge_mlat_gs_spikes.py — one-shot cleanup of MLAT ground-speed spikes.

Detects MLAT positions where the GS acceleration between consecutive
positions exceeds a physical limit (default: 8 kts/s).  These are
single-sample multilateration glitches where the reported GS jumps to
an implausible value and immediately returns to normal.

Bad gs values are set to NULL in the positions table (the position itself
is kept).  max_gs in the flights table is then recomputed from surviving
gs values.  Additionally, any flight whose max_gs exceeds all stored
position gs values ("orphan max_gs") is recalculated.

Usage:
    python purge_mlat_gs_spikes.py [options]

Options:
    --db PATH               SQLite DB path (default: config.DB_PATH)
    --accel-limit N         Max acceleration in kts/s (default: 8.0)
    --apply                 Commit changes (default: dry-run)
"""

import argparse
import itertools
import sqlite3
import statistics

from readsbstats import config, database

# Audit-12 #199 — `_new_max_gs` was duplicated here and in
# purge_bad_gs.py. Aliased to the shared helper.
from _purge_helpers import new_max_gs as _new_max_gs


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scan_mlat_spikes(
    conn: sqlite3.Connection,
    accel_limit: float,
) -> dict[int, list[int]]:
    """
    Scan all flights for MLAT GS spikes.
    Returns {flight_id: [position_ids with spike gs]}.

    improvements.md #126: streams one ordered query through
    ``itertools.groupby`` instead of one SELECT per flight.

    Note: the WHERE filter keeps every position with a non-null GS (not
    just MLAT) so non-MLAT readings can advance ``prev_gs`` between MLAT
    samples — the per-row branch below preserves the original semantics.
    Flights that have no MLAT GS at all still appear here but never
    produce bad_ids, so the result set is unchanged.
    """
    cursor = conn.execute(
        "SELECT flight_id, id, ts, gs, source_type FROM positions "
        "WHERE gs IS NOT NULL ORDER BY flight_id, ts"
    )

    bad: dict[int, list[int]] = {}

    for fid, group in itertools.groupby(cursor, key=lambda r: r["flight_id"]):
        bad_ids: list[int] = []
        prev_gs = None
        prev_ts = None

        for pos in group:
            pid, ts, gs, source_type = pos["id"], pos["ts"], pos["gs"], pos["source_type"]

            if source_type != "mlat":
                # Non-MLAT positions: advance reference normally (gs is non-null
                # by virtue of the WHERE filter).
                prev_gs = gs
                prev_ts = ts
                continue

            if prev_gs is not None and prev_ts is not None:
                dt = ts - prev_ts
                if dt > 0:
                    accel = abs(gs - prev_gs) / dt
                    if accel > accel_limit:
                        bad_ids.append(pid)
                        continue  # don't advance prev_gs on bad positions

            prev_gs = gs
            prev_ts = ts

        if bad_ids:
            bad[fid] = bad_ids

    return bad


def scan_statistical_outliers(
    conn: sqlite3.Connection,
    outlier_factor: float,
    min_readings: int,
) -> dict[int, list[int]]:
    """
    Scan MLAT flights for GS values that are statistical outliers vs. the
    flight's own distribution.  A position is an outlier when its GS exceeds
    outlier_factor × p75 of all MLAT GS values in that flight.  Flights with
    fewer than min_readings MLAT GS values are skipped (too few points for a
    stable p75).

    This catches isolated leading/trailing spikes that the acceleration filter
    misses because they have no adjacent reference point.

    Returns {flight_id: [position_ids with outlier gs]}.
    """
    # improvements.md #126: stream one ordered query through
    # ``itertools.groupby`` instead of one SELECT per flight.
    cursor = conn.execute(
        "SELECT flight_id, id, gs FROM positions "
        "WHERE gs IS NOT NULL AND source_type = 'mlat' ORDER BY flight_id"
    )

    bad: dict[int, list[int]] = {}

    for fid, group in itertools.groupby(cursor, key=lambda r: r["flight_id"]):
        rows = list(group)

        if len(rows) < min_readings:
            continue

        gs_sorted = sorted(r["gs"] for r in rows)
        p75 = statistics.quantiles(gs_sorted, n=4)[2]
        threshold = p75 * outlier_factor

        bad_ids = [r["id"] for r in rows if r["gs"] > threshold]
        if bad_ids:
            bad[fid] = bad_ids

    return bad


def scan_orphan_max_gs(conn: sqlite3.Connection) -> dict[int, float | None]:
    """
    Find flights where max_gs exceeds all stored position gs values.
    Returns {flight_id: correct_max_gs}.
    """
    # Threshold > 1 kts to avoid floating-point noise from matching values.
    # Audit 17: LEFT JOIN (was INNER) so a flight whose GS samples were ALL
    # nulled (e.g. by a prior purge) but still carries a stale max_gs is caught
    # too — `max_stored_gs IS NULL` means the correct value is NULL. The old
    # INNER JOIN dropped those flights, leaving the phantom max_gs forever.
    rows = conn.execute(
        "SELECT f.id, f.max_gs, MAX(p.gs) AS max_stored_gs "
        "FROM flights f "
        "LEFT JOIN positions p ON p.flight_id = f.id AND p.gs IS NOT NULL "
        "WHERE f.max_gs IS NOT NULL "
        "GROUP BY f.id "
        "HAVING max_stored_gs IS NULL OR f.max_gs - max_stored_gs > 1"
    ).fetchall()
    return {r["id"]: r["max_stored_gs"] for r in rows}


# Audit-13 A13-084: single source of truth in `_purge_helpers.BATCH_SIZE`.
from _purge_helpers import BATCH_SIZE as _BATCH_SIZE


def apply_purge(
    conn: sqlite3.Connection,
    bad: dict[int, list[int]],
    orphans: dict[int, float | None],
) -> None:
    """Null gs for spike positions, recompute max_gs, and fix orphans.

    NOT atomic across the whole run — see ``purge_ghosts.apply_purge``'s
    docstring for the full rationale (audit-12 Phase 3 trade-off). The
    script is idempotent: re-run finishes any interrupted purge."""
    # Audit-12 P8 — `pending` is reset between the two loops so the
    # orphan-loop's batch boundaries align with "every N orphans"
    # rather than "every N total writes carrying over from the bad-
    # flights loop". The counter has identical semantics in both
    # loops now.
    pending = 0
    for fid, bad_ids in bad.items():
        placeholders = ",".join("?" * len(bad_ids))
        conn.execute(
            f"UPDATE positions SET gs = NULL WHERE id IN ({placeholders})", bad_ids
        )
        new_max = _new_max_gs(conn, fid, bad_ids)
        conn.execute(
            "UPDATE flights SET max_gs = ? WHERE id = ?", (new_max, fid)
        )
        pending += 1
        if pending >= _BATCH_SIZE:
            conn.commit()
            pending = 0
    if pending:
        conn.commit()
    pending = 0
    for fid, correct_max in orphans.items():
        if fid not in bad:  # already handled above
            conn.execute(
                "UPDATE flights SET max_gs = ? WHERE id = ?", (correct_max, fid)
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
        description="Null MLAT gs spikes and fix max_gs in flights"
    )
    parser.add_argument("--db",             default=config.DB_PATH)
    parser.add_argument("--accel-limit",    default=config.MAX_GS_ACCEL_KTS_S,       type=float)
    parser.add_argument("--outlier-factor", default=config.MLAT_OUTLIER_FACTOR,       type=float,
                        help="Null MLAT GS > this × p75 of the flight's GS values (default: %(default)s)")
    # Audit-13 A13-022: `statistics.quantiles(data, n=4)` raises if
    # `len(data) < 2`; clamp the user input so a typo can't crash the
    # statistical-outlier pass.
    parser.add_argument("--min-gs-count",   default=config.MLAT_OUTLIER_MIN_READINGS,
                        type=lambda v: max(2, int(v)),
                        help="Min MLAT GS readings required for outlier scan (default: %(default)s, floor: 2)")
    parser.add_argument("--apply",          action="store_true",
                        help="Commit changes (default: dry-run)")
    parser.add_argument("--i-have-a-backup", action="store_true",
                        help="Skip the automatic VACUUM INTO snapshot taken "
                             "before --apply (you've made one yourself)")
    args = parser.parse_args()

    # Audit-13 A13-056: use database.connect() for WAL + busy_timeout=30s.
    conn = database.connect(args.db)

    print(
        f"Scanning {args.db}\n"
        f"  accel limit    : {args.accel_limit} kts/s (MLAT only)\n"
        f"  outlier factor : {args.outlier_factor}× p75  (min {args.min_gs_count} readings)\n"
        f"  mode           : {'APPLY' if args.apply else 'dry-run'}\n"
    )

    accel_bad = scan_mlat_spikes(conn, args.accel_limit)
    stat_bad  = scan_statistical_outliers(conn, args.outlier_factor, args.min_gs_count)

    # Merge: union of position ids per flight
    bad: dict[int, list[int]] = {}
    for fid, ids in accel_bad.items():
        bad.setdefault(fid, []).extend(ids)
    for fid, ids in stat_bad.items():
        existing = set(bad.get(fid, []))
        bad.setdefault(fid, []).extend(i for i in ids if i not in existing)

    orphans = scan_orphan_max_gs(conn)
    total_pos = sum(len(v) for v in bad.values())

    if total_pos == 0 and not orphans:
        print("No MLAT spikes or orphan max_gs found.")
        conn.close()
        return

    if total_pos > 0:
        print(f"{total_pos} spike position(s) across {len(bad)} flight(s):\n")
        for fid, bad_ids in sorted(bad.items()):
            flight = conn.execute(
                "SELECT icao_hex, callsign, registration, max_gs FROM flights WHERE id = ?",
                (fid,),
            ).fetchone()
            if not flight:
                continue
            label = " ".join(filter(None, [flight["callsign"], flight["registration"]])) or flight["icao_hex"]
            sources = []
            if fid in accel_bad:
                sources.append("accel")
            if fid in stat_bad:
                sources.append("outlier")
            old_max = flight["max_gs"]
            new_max = _new_max_gs(conn, fid, bad_ids)
            old_str = f"{old_max:.1f}" if old_max is not None else "NULL"
            new_str = f"{new_max:.1f}" if new_max is not None else "NULL"
            print(f"  [{fid:>5}] {label:25s}  {len(bad_ids):>3} spikes [{','.join(sources)}]  max_gs {old_str} → {new_str} kts")

    if orphans:
        print(f"\n{len(orphans)} flight(s) with orphan max_gs:\n")
        for fid, correct_max in sorted(orphans.items()):
            flight = conn.execute(
                "SELECT icao_hex, callsign, registration, max_gs FROM flights WHERE id = ?",
                (fid,),
            ).fetchone()
            if not flight:
                continue
            label = " ".join(filter(None, [flight["callsign"], flight["registration"]])) or flight["icao_hex"]
            old_str = f"{flight['max_gs']:.1f}" if flight['max_gs'] is not None else "NULL"
            new_str = f"{correct_max:.1f}" if correct_max is not None else "NULL"
            print(f"  [{fid:>5}] {label:25s}  max_gs {old_str} → {new_str} kts")

    if not args.apply:
        print("\nDry-run — pass --apply to commit changes.")
        conn.close()
        return

    if not args.i_have_a_backup:
        snapshot = database.snapshot_db(args.db)
        print(f"\nSnapshot: {snapshot}")

    apply_purge(conn, bad, orphans)
    print(f"\nDone — nulled {total_pos} gs spike(s) across {len(bad)} flight(s).")
    if orphans:
        fixed_orphans = len([f for f in orphans if f not in bad])
        print(f"Fixed {fixed_orphans} additional orphan max_gs value(s).")
    conn.close()


if __name__ == "__main__":
    main()
