#!/usr/bin/env python3
"""
purge_bad_gs.py — one-shot cleanup of implausible ground-speed values.

Two checks are applied to each position:

  1. Hard-limit check: gs > MAX_GS_CIVIL_KTS for civil aircraft, or
     gs > MAX_GS_MILITARY_KTS for military/unknown aircraft.

  2. Cross-validation check: the reported gs deviates from the
     position-derived implied speed by more than MAX_GS_DEVIATION_KTS.
     Applied when the time delta to the previous position is within the
     meaningful range (MIN_DT_ADSB..MAX_DT seconds for ADS-B,
     MIN_DT_OTHER..MAX_DT for MLAT/other sources).

Bad gs values are set to NULL in the positions table (the position itself
is kept).  max_gs in the flights table is then recomputed from surviving
gs values.

Usage:
    python purge_bad_gs.py [options]

Options:
    --db PATH               SQLite DB path (default: config.DB_PATH)
    --civil-limit N         Hard limit for civil aircraft in kts (default: 750)
    --military-limit N      Hard limit for military/unknown aircraft in kts (default: 1800)
    --deviation N           Cross-validation deviation threshold in kts (default: 100)
    --apply                 Commit changes (default: dry-run)
"""

import argparse
import itertools
import sqlite3

from readsbstats import config, database, geo, posenc

# Audit-12 #199 — `_new_max_gs` was duplicated here and in
# purge_mlat_gs_spikes.py. Aliased to the shared helper so a fix lands
# in both places at once.
from _purge_helpers import new_max_gs as _new_max_gs

haversine_nm = geo.haversine_nm

# PERF-2: chunk size for the batched aircraft_db flag lookup. SQLite's
# default SQLITE_MAX_VARIABLE_NUMBER is 999 (>=3.32: 32766); 500 stays
# comfortably under every build while keeping the round-trip count low.
_AIRCRAFT_DB_CHUNK = 500

# Min/max dt (seconds) for cross-validation.
# ADS-B positions use GPS — accurate enough at short intervals.
# MLAT/other positions have higher position uncertainty, need longer dt.
_MIN_DT_ADSB  = 5
_MIN_DT_OTHER = 30
_MAX_DT       = 120

# Cross-validation is skipped below this GS (kts).
# Slow aircraft in turns can have position-derived speed well below their
# reported GS, causing false positives at the 100 kt deviation threshold.
_MIN_GS_XVAL  = 300


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _is_military(flags: int) -> bool:
    return bool(flags & config.FLAG_MILITARY)


def _flags_for_icaos(
    conn: sqlite3.Connection,
    icaos: set[str],
) -> dict[str, int]:
    """Return {icao_hex: flags} for just the given ICAOs, queried in chunks.

    PERF-2: the previous implementation preloaded the ENTIRE aircraft_db
    (~620k rows / hundreds of MB transient on the 8 GB Pi, competing with
    the live collector) even though scan_flights only ever needs the flags
    for the handful of ICAOs that actually have flights. This pulls only
    those rows, in batches of ``_AIRCRAFT_DB_CHUNK``. ICAOs absent from
    aircraft_db simply don't appear in the result — callers apply their own
    default (``-1`` → "not in aircraft_db"), preserving prior behaviour.
    """
    out: dict[str, int] = {}
    icao_list = list(icaos)
    for i in range(0, len(icao_list), _AIRCRAFT_DB_CHUNK):
        chunk = icao_list[i:i + _AIRCRAFT_DB_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT icao_hex, flags FROM aircraft_db "
            f"WHERE icao_hex IN ({placeholders})",
            chunk,
        ):
            out[row[0]] = row[1]
    return out


def scan_flights(
    conn: sqlite3.Connection,
    civil_limit: int,
    military_limit: int,
    deviation: int,
) -> dict[int, list[int]]:
    """
    Scan all flights and return {flight_id: [position_ids with bad gs]}.

    improvements.md #126: streams one ordered ``positions`` query through
    ``itertools.groupby`` instead of one SELECT per flight, eliminating
    ~35 k round trips on a typical DB.  The per-flight icao lookup is
    also batched into a single query.
    """
    # Bulk-load (flight_id → icao_hex) so we don't query flights once per
    # flight inside the loop.
    icao_by_fid: dict[int, str] = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, icao_hex FROM flights").fetchall()
    }
    # PERF-2: load military flags for ONLY the ICAOs that have flights, in
    # batches — not the whole ~620k-row aircraft_db. ``None`` icao_hex (a
    # flight with no ICAO) can't match aircraft_db, so drop it from the lookup
    # set; it falls through to the -1 default below exactly as before.
    needed_icaos = {icao for icao in icao_by_fid.values() if icao is not None}
    flags_by_icao: dict[str, int] = _flags_for_icaos(conn, needed_icaos)

    # v6 positions: decode the scaled INTEGER columns in SQL; the source
    # code is decoded per-row via posenc at the boundary below.
    cursor = conn.execute(
        "SELECT flight_id, id, ts, lat / 100000.0 AS lat, lon / 100000.0 AS lon, "
        "gs / 10.0 AS gs, source FROM positions "
        "WHERE gs IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY flight_id, ts"
    )

    bad: dict[int, list[int]] = {}

    for fid, group in itertools.groupby(cursor, key=lambda r: r["flight_id"]):
        icao_hex = icao_by_fid.get(fid)
        if icao_hex is None:
            continue
        flags = flags_by_icao.get(icao_hex, -1)   # -1 = not in aircraft_db
        found_in_db = flags >= 0
        is_mil = _is_military(flags) if found_in_db else False
        gs_hard_limit = military_limit if (is_mil or not found_in_db) else civil_limit

        positions = list(group)

        bad_ids: list[int] = []
        prev = None

        for pos in positions:
            # Row keys: flight_id, id, ts, lat, lon, gs, source
            pid, ts, lat, lon, gs = (
                pos["id"], pos["ts"], pos["lat"], pos["lon"], pos["gs"],
            )
            source_type = posenc.decode_source(pos["source"])

            if gs is None:
                prev = pos
                continue

            flagged = False

            # 1. Hard-limit check
            if gs > gs_hard_limit:
                flagged = True

            # 2. Cross-validation against position-derived speed.
            # Skipped for slow aircraft (gs < _MIN_GS_XVAL) to avoid false
            # positives from turns where displacement < gs*dt.
            if not flagged and prev is not None and gs >= _MIN_GS_XVAL:
                pts, plat, plon = prev["ts"], prev["lat"], prev["lon"]
                dt = ts - pts
                is_adsb = posenc.is_adsb_source(source_type)
                min_dt = _MIN_DT_ADSB if is_adsb else _MIN_DT_OTHER
                # Defensive: the WHERE clause already filters NULLs from
                # the cursor, but cross-validation pairs current+previous,
                # so a defensive call-site guard catches future schema or
                # query-source changes that bypass the SELECT.
                if (min_dt <= dt <= _MAX_DT
                        and None not in (plat, plon, lat, lon)):
                    dist = haversine_nm(plat, plon, lat, lon)
                    implied = dist / (dt / 3600.0)
                    if abs(gs - implied) > deviation:
                        flagged = True

            if flagged:
                bad_ids.append(pid)
            else:
                prev = pos   # only advance reference on good positions

        if bad_ids:
            bad[fid] = bad_ids

    return bad


# Audit-13 A13-084: single source of truth in `_purge_helpers.BATCH_SIZE`.
from _purge_helpers import BATCH_SIZE as _BATCH_SIZE


def apply_purge(conn: sqlite3.Connection, bad: dict[int, list[int]]) -> None:
    """Null gs for bad positions and recompute max_gs for affected flights.

    NOT atomic across the whole run — see ``purge_ghosts.apply_purge``'s
    docstring for the full rationale (audit-12 Phase 3 trade-off). The
    script is idempotent: re-run finishes any interrupted purge."""
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Null implausible gs values and fix max_gs in flights"
    )
    parser.add_argument("--db",              default=config.DB_PATH)
    parser.add_argument("--civil-limit",     default=config.MAX_GS_CIVIL_KTS,    type=int)
    parser.add_argument("--military-limit",  default=config.MAX_GS_MILITARY_KTS, type=int)
    parser.add_argument("--deviation",       default=config.MAX_GS_DEVIATION_KTS, type=int)
    parser.add_argument("--apply",           action="store_true",
                        help="Commit changes (default: dry-run)")
    parser.add_argument("--i-have-a-backup", action="store_true",
                        help="Skip the automatic VACUUM INTO snapshot taken "
                             "before --apply (you've made one yourself)")
    args = parser.parse_args()

    # Audit-13 A13-056: use database.connect() for WAL + busy_timeout=30s.
    conn = database.connect(args.db)

    print(
        f"Scanning {args.db}\n"
        f"  hard limits : civil={args.civil_limit} kts  military={args.military_limit} kts\n"
        f"  cross-val   : deviation>{args.deviation} kts  "
        f"dt {_MIN_DT_ADSB}-{_MAX_DT}s (ADS-B) / {_MIN_DT_OTHER}-{_MAX_DT}s (other)\n"
        f"  mode        : {'APPLY' if args.apply else 'dry-run'}\n"
    )

    bad = scan_flights(conn, args.civil_limit, args.military_limit, args.deviation)
    total_pos = sum(len(v) for v in bad.values())

    if total_pos == 0:
        print("No implausible gs values found.")
        conn.close()
        return

    print(f"{total_pos} position(s) across {len(bad)} flight(s):\n")

    for fid, bad_ids in sorted(bad.items()):
        flight = conn.execute(
            "SELECT icao_hex, callsign, registration, max_gs FROM flights WHERE id = ?",
            (fid,),
        ).fetchone()
        if not flight:
            continue
        label = " ".join(filter(None, [flight["callsign"], flight["registration"]])) or flight["icao_hex"]
        old_max = flight["max_gs"]
        new_max = _new_max_gs(conn, fid, bad_ids)
        old_str = f"{old_max:.1f}" if old_max is not None else "NULL"
        new_str = f"{new_max:.1f}" if new_max is not None else "NULL"
        print(f"  [{fid:>5}] {label:25s}  {len(bad_ids):>3} bad gs  max_gs {old_str} → {new_str} kts")

    if not args.apply:
        print("\nDry-run — pass --apply to commit changes.")
        conn.close()
        return

    if not args.i_have_a_backup:
        snapshot = database.snapshot_db(args.db)
        print(f"\nSnapshot: {snapshot}")

    apply_purge(conn, bad)
    print(f"\nDone — nulled {total_pos} gs value(s), updated {len(bad)} flight(s).")
    conn.close()


if __name__ == "__main__":
    main()
