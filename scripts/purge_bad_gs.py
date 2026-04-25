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
import math
import sqlite3
import sys

from readsbstats import config, geo

haversine_nm = geo.haversine_nm

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
    return bool(flags & 1)


def scan_flights(
    conn: sqlite3.Connection,
    civil_limit: int,
    military_limit: int,
    deviation: int,
) -> dict[int, list[int]]:
    """
    Scan all flights and return {flight_id: [position_ids with bad gs]}.
    """
    # Load military flags once per ICAO to avoid per-position lookups.
    flags_by_icao: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute("SELECT icao_hex, flags FROM aircraft_db").fetchall()
    }

    flight_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT flight_id FROM positions WHERE gs IS NOT NULL ORDER BY flight_id"
        ).fetchall()
    ]

    bad: dict[int, list[int]] = {}

    for fid in flight_ids:
        icao = conn.execute(
            "SELECT icao_hex FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        if icao is None:
            continue
        icao_hex = icao[0]
        flags = flags_by_icao.get(icao_hex, -1)   # -1 = not in aircraft_db
        found_in_db = flags >= 0
        is_mil = _is_military(flags) if found_in_db else False
        gs_hard_limit = military_limit if (is_mil or not found_in_db) else civil_limit

        positions = conn.execute(
            "SELECT id, ts, lat, lon, gs, source_type FROM positions "
            "WHERE flight_id = ? ORDER BY ts",
            (fid,),
        ).fetchall()

        bad_ids: list[int] = []
        prev = None

        for pos in positions:
            pid, ts, lat, lon, gs, source_type = pos

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
                pts, plat, plon = prev[1], prev[2], prev[3]
                dt = ts - pts
                is_adsb = (source_type or "").startswith("adsb")
                min_dt = _MIN_DT_ADSB if is_adsb else _MIN_DT_OTHER
                if min_dt <= dt <= _MAX_DT:
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


def _new_max_gs(conn: sqlite3.Connection, flight_id: int, bad_ids: list[int]) -> float | None:
    """Return max gs from positions excluding the bad ones."""
    placeholders = ",".join("?" * len(bad_ids))
    row = conn.execute(
        f"SELECT MAX(gs) FROM positions "
        f"WHERE flight_id = ? AND id NOT IN ({placeholders}) AND gs IS NOT NULL",
        [flight_id] + bad_ids,
    ).fetchone()
    return row[0] if row else None


def apply_purge(conn: sqlite3.Connection, bad: dict[int, list[int]]) -> None:
    """Null gs for bad positions and recompute max_gs for affected flights."""
    with conn:
        for fid, bad_ids in bad.items():
            placeholders = ",".join("?" * len(bad_ids))
            conn.execute(
                f"UPDATE positions SET gs = NULL WHERE id IN ({placeholders})", bad_ids
            )
            new_max = _new_max_gs(conn, fid, bad_ids)
            conn.execute(
                "UPDATE flights SET max_gs = ? WHERE id = ?", (new_max, fid)
            )


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
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

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

    apply_purge(conn, bad)
    print(f"\nDone — nulled {total_pos} gs value(s), updated {len(bad)} flight(s).")
    conn.close()


if __name__ == "__main__":
    main()
