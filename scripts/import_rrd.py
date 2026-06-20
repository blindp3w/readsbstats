#!/usr/bin/env python3
"""
Import historical RRD data from graphs1090 into the receiver_stats table.

Reads dump1090 RRD files (as collected by graphs1090/collectd), extracts
AVERAGE data at multiple resolution tiers, and inserts into receiver_stats.
Finest resolution is imported first so INSERT OR IGNORE preserves precise data.

Note: unlike the purge_* scripts this does NOT take a pre-write snapshot
(database.snapshot_db). Writes are INSERT OR IGNORE — non-destructive, so a
snapshot is unnecessary. If you'd rather not write the live DB while the
collector is running, stop the collector and/or back up first; a startup
WARNING repeats this.

Usage:
    python3 scripts/import_rrd.py \\
        --rrd-dir /tmp/rrd_peek/localhost/dump1090-localhost \\
        --db /mnt/ext/readsbstats/history.db

    python3 scripts/import_rrd.py --rrd-dir ... --db ... --dry-run
"""

import argparse
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

# Audit 17: dropped the ad-hoc `sys.path.insert(.../src)` — it diverged from
# every other script (which rely on the installed package / pyproject's
# `pythonpath = ["src", "scripts"]` under pytest) and would shadow an installed
# readsbstats with whatever lives at the resolved relative path.
from readsbstats import config, database
from readsbstats.metrics_collector import _COLS, _INSERT_SQL


# ---------------------------------------------------------------------------
# RRD file → receiver_stats column mapping
# ---------------------------------------------------------------------------

# (rrd_filename, column_name, is_derive)
# DERIVE values are stored as per-second rates in RRD; multiply by 60
# to convert to per-minute counts matching our schema.
SINGLE_DS = [
    ("dump1090_dbfs-signal.rrd",              "signal",           False),
    ("dump1090_dbfs-noise.rrd",               "noise",            False),
    ("dump1090_dbfs-peak_signal.rrd",         "peak_signal",      False),
    ("dump1090_messages-strong_signals.rrd",  "strong_signals",   True),
    ("dump1090_messages-local_accepted.rrd",  "messages",         True),
    ("dump1090_messages-local_accepted_0.rrd", "local_accepted_0", True),
    ("dump1090_messages-local_accepted_1.rrd", "local_accepted_1", True),
    ("dump1090_messages-positions.rrd",       "positions_total",  True),
    ("dump1090_messages-remote_accepted.rrd", "remote_accepted",  True),
    ("dump1090_range-max_range.rrd",          "max_distance_m",   False),
    ("dump1090_tracks-all.rrd",               "tracks_new",       True),
    ("dump1090_tracks-single_message.rrd",    "tracks_single",    True),
    ("dump1090_cpu-demod.rrd",                "cpu_demod",        True),
    ("dump1090_cpu-reader.rrd",               "cpu_reader",       True),
    ("dump1090_cpu-background.rrd",           "cpu_background",   True),
    ("dump1090_cpu-aircraft_json.rrd",        "cpu_aircraft_json", True),
    ("dump1090_cpu-heatmap_and_state.rrd",    "cpu_heatmap",      True),
    ("dump1090_mlat-recent.rrd",              "ac_mlat",          False),
    ("dump1090_gps-recent.rrd",               "ac_adsb",          False),
]

# Multi-DS file: dump1090_aircraft-recent.rrd
# Header: "total  positions"
# positions → ac_with_pos, (total - positions) → ac_without_pos
AIRCRAFT_RECENT = "dump1090_aircraft-recent.rrd"

# Resolution tiers: finest first so INSERT OR IGNORE preserves precise data.
# (resolution_seconds, max_rows_in_rra) — used to compute time range per tier.
RESOLUTIONS = [
    (60,  3000),   # ~50 hours
    (180, 3867),   # ~8 days
    (900, 3094),   # ~32 days
]

# Per-second → per-minute conversion factor
DERIVE_FACTOR = 60.0


# ---------------------------------------------------------------------------
# rrdtool fetch helpers
# ---------------------------------------------------------------------------

def parse_fetch_output(output: str) -> list[tuple[int, list[float | None]]]:
    """
    Parse rrdtool fetch output into (timestamp, [values]) pairs.

    Output format:
        <header line with DS names>
        <blank line>
        ts: val1 [val2 ...]
        ...

    NaN values are returned as None.
    """
    rows = []
    lines = output.strip().split("\n")
    for line in lines:
        if ":" not in line:
            continue
        ts_str, rest = line.split(":", 1)
        # BUG-7: tolerate a colon-bearing line whose prefix isn't an integer
        # (a re-emitted header, an rrdtool warning, etc.). Skip it like the
        # value loop below skips unparseable values, rather than raising and
        # aborting the whole import mid-run after partial commits.
        try:
            ts = int(ts_str.strip())
        except ValueError:
            continue
        parts = rest.strip().split()
        values = []
        for p in parts:
            try:
                v = float(p)
                values.append(None if math.isnan(v) else v)
            except ValueError:
                values.append(None)
        # Skip rows where ALL values are None
        if all(v is None for v in values):
            continue
        rows.append((ts, values))
    return rows


def fetch_rrd(path: str, resolution: int, start: int, end: int) -> list[tuple[int, list[float | None]]]:
    """
    Run rrdtool fetch AVERAGE and parse the output.
    Returns list of (timestamp, [values]) pairs, NaN-only rows excluded.
    """
    cmd = [
        "rrdtool", "fetch", path, "AVERAGE",
        "--resolution", str(resolution),
        "--start", str(start),
        "--end", str(end),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: rrdtool fetch failed for {os.path.basename(path)}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return []
    return parse_fetch_output(result.stdout)


def get_last_update(path: str) -> int | None:
    """Get last_update timestamp from an RRD file."""
    result = subprocess.run(
        ["rrdtool", "info", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.split("\n"):
        if line.startswith("last_update"):
            return int(line.split("=")[1].strip())
    return None


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge_tier(rrd_dir: str, resolution: int,
               start: int, end: int) -> dict[int, dict[str, float]]:
    """
    Fetch all mapped RRD files at the given resolution and merge by timestamp.
    Returns {ts: {column_name: value, ...}}.
    """
    merged: dict[int, dict[str, float]] = {}

    # Single-DS files
    for filename, col, is_derive in SINGLE_DS:
        path = os.path.join(rrd_dir, filename)
        if not os.path.exists(path):
            continue
        rows = fetch_rrd(path, resolution, start, end)
        for ts, vals in rows:
            if vals[0] is None:
                continue
            value = vals[0] * DERIVE_FACTOR if is_derive else vals[0]
            merged.setdefault(ts, {})[col] = value

    # Multi-DS file: aircraft-recent.rrd (columns: total, positions)
    ac_path = os.path.join(rrd_dir, AIRCRAFT_RECENT)
    if os.path.exists(ac_path):
        rows = fetch_rrd(ac_path, resolution, start, end)
        for ts, vals in rows:
            if len(vals) < 2:
                continue
            total, positions = vals[0], vals[1]
            if positions is not None:
                merged.setdefault(ts, {})["ac_with_pos"] = positions
            if total is not None and positions is not None:
                merged.setdefault(ts, {})["ac_without_pos"] = total - positions
            # If only `total` is known (positions NaN) we can't split with-vs-
            # without, so leave both NULL rather than mislabel total as with-pos.

    return merged


# ---------------------------------------------------------------------------
# DB import
# ---------------------------------------------------------------------------

def import_rows(conn: sqlite3.Connection | None, rows: dict[int, dict],
                dry_run: bool) -> int:
    """
    Insert merged rows into receiver_stats.
    Returns the number of rows actually inserted (real run) or that WOULD be
    inserted (dry-run). In dry-run, when ``conn`` is a (read-only) connection the
    count excludes timestamps already present — so the preview matches a real
    INSERT OR IGNORE run on a partially-populated DB; ``conn=None`` counts every
    row as net-new.
    """
    if not rows:
        return 0

    existing: set[int] = set()
    if dry_run and conn is not None:
        # ts is the PRIMARY KEY, so this range read is indexed. A missing table
        # (fresh DB) → no existing rows → everything is net-new.
        try:
            existing = {
                r[0] for r in conn.execute(
                    "SELECT ts FROM receiver_stats WHERE ts BETWEEN ? AND ?",
                    (min(rows), max(rows)),
                )
            }
        except sqlite3.OperationalError:
            existing = set()

    inserted = 0
    batch = 0
    for ts in sorted(rows):
        if dry_run:
            if ts not in existing:
                inserted += 1
            continue
        values = tuple(rows[ts].get(c) for c in _COLS)
        cur = conn.execute(_INSERT_SQL, (ts, *values))
        if cur.rowcount > 0:
            inserted += 1
        batch += 1
        if batch % 1000 == 0:
            conn.commit()

    if not dry_run:
        conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import graphs1090 RRD history into receiver_stats.",
    )
    parser.add_argument(
        "--rrd-dir", required=True,
        help="Path to extracted dump1090-localhost directory containing .rrd files",
    )
    parser.add_argument(
        "--db", default=config.DB_PATH,
        help=f"SQLite database path (default: {config.DB_PATH})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be imported without writing to DB",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.rrd_dir):
        print(f"ERROR: {args.rrd_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Verify rrdtool is available
    result = subprocess.run(["rrdtool", "--version"], capture_output=True)
    if result.returncode != 0:
        print("ERROR: rrdtool not found. Install with: apt install rrdtool", file=sys.stderr)
        sys.exit(1)

    # Find the time range from a representative RRD file
    ref_file = os.path.join(args.rrd_dir, "dump1090_dbfs-signal.rrd")
    if not os.path.exists(ref_file):
        print(f"ERROR: reference file not found: {ref_file}", file=sys.stderr)
        sys.exit(1)

    last_update = get_last_update(ref_file)
    if last_update is None:
        print("ERROR: cannot read last_update from RRD file", file=sys.stderr)
        sys.exit(1)

    end = last_update

    print(f"RRD dir:      {args.rrd_dir}")
    print(f"Database:     {args.db}")
    print(f"Last update:  {end}")
    print(f"Dry run:      {args.dry_run}")
    print()

    # Unlike the purge_* scripts, this importer does NOT take a pre-write
    # database.snapshot_db() before mutating. Writes are INSERT OR IGNORE
    # (non-destructive — existing rows are preserved, only new timestamps are
    # added), so a snapshot is unnecessary for data safety. Still warn the
    # operator: if you'd rather not write the live DB while the collector is
    # running, stop the collector and/or back up first.
    if not args.dry_run:
        print(
            "WARNING: no snapshot is taken before import. Writes are "
            "INSERT OR IGNORE (non-destructive), but if you're concerned, "
            "stop the collector and/or back up the DB before continuing.",
            file=sys.stderr,
        )

    # Ensure the receiver_stats table exists — WITHOUT running the full schema
    # migration. init_db() runs _migrate(), which on a pre-v6 DB rebuilds the
    # positions table (irrelevant to a metrics-only import, and it aborts on a
    # large legacy table). Create just the one table from its canonical DDL.
    # (Audit 2026-06-20)
    if not args.dry_run:
        # database.connect() already sets WAL + busy_timeout (Audit-13 A13-056).
        conn = database.connect(args.db)
        conn.execute(database._DDL_RECEIVER_STATS)
        conn.commit()
    else:
        # Dry-run: open a READ-ONLY connection so import_rows can count only
        # net-new timestamps without writing any data rows. (mode=ro reads a
        # consistent snapshot; a WAL DB still touches empty -shm/-wal sidecars,
        # but the DB content is untouched.) A missing or inaccessible DB → None →
        # every row counted as new. (Audit 2026-06-20)
        try:
            conn = sqlite3.connect(
                f"file:{os.path.abspath(args.db)}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            conn = None

    total_inserted = 0

    for resolution, max_rows in RESOLUTIONS:
        # Each tier only covers its own time window
        tier_start = end - (resolution * max_rows)
        print(f"Fetching at {resolution}s resolution (covers ~{resolution * max_rows // 3600}h)...")
        merged = merge_tier(args.rrd_dir, resolution, tier_start, end)
        print(f"  {len(merged)} timestamps with data")

        if merged:
            timestamps = sorted(merged)
            t_min = datetime.fromtimestamp(timestamps[0], tz=timezone.utc)
            t_max = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc)
            print(f"  Range: {t_min:%Y-%m-%d %H:%M} → {t_max:%Y-%m-%d %H:%M} UTC")

            # Dispatch on the MODE, not connection presence: in dry-run `conn` is
            # a read-only connection (used to count net-new timestamps), so keying
            # on `conn is not None` would take the write path and fail against a
            # read-only DB. import_rows handles all three (conn, dry_run) cases.
            inserted = import_rows(conn, merged, dry_run=args.dry_run)
            total_inserted += inserted
            print(f"  Inserted: {inserted} new rows (skipped {len(merged) - inserted} duplicates)")
        print()

    if conn is not None:
        conn.close()

    print(f"Done — {total_inserted} rows imported across {len(RESOLUTIONS)} tiers.")
    if args.dry_run:
        print("(dry-run mode — no data was written)")


if __name__ == "__main__":
    main()
