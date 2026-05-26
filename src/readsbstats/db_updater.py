"""
readsbstats — aircraft/airline database updater.

Downloads and imports:
  1. tar1090-db aircraft CSV  (https://github.com/wiedehopf/tar1090-db, csv branch)
     → aircraft_db table: icao_hex, registration, type_code, type_desc, flags
  2. OpenFlights airlines.dat
     → airlines table: icao_code, name, iata_code, country

Also backfills the flights table with missing registration/aircraft_type values.

Run manually or via systemd timer (readsbstats-updater.timer):
    /opt/readsbstats/venv/bin/python /opt/readsbstats/db_updater.py
"""

import csv
import gzip
import io
import logging
import sqlite3
import sys
import time
import urllib.request

from . import config, database, enrichment, http_safe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("db_updater")

# Direct raw-content URL avoids the 302 redirect through github.com/...
# /raw/... so the SSRF-safe fetcher (which blocks all redirects) can be used
# without an exception.
AIRCRAFT_CSV_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
)
AIRLINES_DAT_URL = (
    "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
)

HEADERS = {"User-Agent": "readsbstats/1.0 db_updater"}

# Generous response cap (50 MB).  The compressed aircraft DB is ~10 MB today;
# the airlines CSV is ~80 KB.  Anything dramatically larger is suspicious.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> bytes:
    """Download *url* through the shared SSRF-safe fetcher."""
    body, _ = http_safe.safe_urlopen(
        url, timeout=60, max_bytes=_MAX_DOWNLOAD_BYTES, extra_headers=HEADERS,
    )
    return body


# ---------------------------------------------------------------------------
# Aircraft database import
# ---------------------------------------------------------------------------

_FLAG_BITS = (config.FLAG_MILITARY, config.FLAG_INTERESTING, config.FLAG_PIA, config.FLAG_LADD)


def _parse_flags(flags_str: str) -> int:
    """Parse tar1090-db binary flag string into an integer bitmask.

    The CSV stores flags as a string of '0'/'1' characters, not a number:
      position 0 = military  (bit value 1)
      position 1 = interesting (bit value 2)
      position 2 = PIA        (bit value 4)
      position 3 = LADD       (bit value 8)

    Examples: '10' → 1 (military), '0001' → 8 (LADD), '11' → 3 (military+interesting).
    Any character outside '01' or an empty string returns 0.
    """
    if not flags_str or not all(c in "01" for c in flags_str):
        return 0
    return sum(bit for bit, ch in zip(_FLAG_BITS, flags_str) if ch == "1")


_HEX_CHARS = frozenset("0123456789abcdef")


def _parse_aircraft_csv_row(parts: list[str]) -> tuple | None:
    """Parse one tar1090-db CSV row into a row tuple for aircraft_db.

    Returns None for an empty row or any row whose first column is not
    a valid 6-character lowercase hex ICAO address. Extracted from
    update_aircraft_db so streaming and unit tests share the same path.

    Format (no header): icao_hex; registration; type_code; flags; type_desc
    """
    if not parts:
        return None
    icao_hex = parts[0].strip().lower()
    if len(icao_hex) != 6 or not all(c in _HEX_CHARS for c in icao_hex):
        return None
    reg       = parts[1].strip() if len(parts) > 1 else ""
    type_code = parts[2].strip() if len(parts) > 2 else ""
    flags_str = parts[3].strip() if len(parts) > 3 else "0"
    type_desc = parts[4].strip() if len(parts) > 4 else ""
    return (
        icao_hex,
        reg or None,
        type_code or None,
        type_desc or None,
        _parse_flags(flags_str),
    )


def _recover_aborted_swap(conn: sqlite3.Connection) -> None:
    """Detect and recover from a swap interrupted between RENAMEs.

    Three table-name presences are possible after an interrupted run:
      * `aircraft_db_new` only → build phase crashed; drop the stale
        staging table.
      * `aircraft_db_old` only (no `aircraft_db`) → first RENAME
        succeeded but the second didn't. Rename old back to canonical.
      * `aircraft_db` + `aircraft_db_old` → second RENAME succeeded but
        the final DROP didn't. Drop the leftover old copy.

    Runs at the top of `update_aircraft_db` before anything else
    touches these tables.
    """
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('aircraft_db', 'aircraft_db_new', 'aircraft_db_old')"
        ).fetchall()
    }
    if "aircraft_db" not in names and "aircraft_db_old" in names:
        log.warning(
            "aircraft_db recovery: restoring aircraft_db_old after "
            "interrupted swap"
        )
        conn.execute("ALTER TABLE aircraft_db_old RENAME TO aircraft_db")
    elif "aircraft_db_old" in names:
        log.info("aircraft_db recovery: dropping leftover aircraft_db_old")
        conn.execute("DROP TABLE aircraft_db_old")
    if "aircraft_db_new" in names:
        log.info("aircraft_db recovery: dropping leftover aircraft_db_new")
        conn.execute("DROP TABLE aircraft_db_new")


def update_aircraft_db(conn: sqlite3.Connection) -> int:
    """Download tar1090-db CSV and replace aircraft_db table. Returns row count."""
    log.info("Downloading aircraft database from tar1090-db…")
    t0 = time.time()
    raw = _fetch(AIRCRAFT_CSV_URL)
    log.info("Downloaded %.1f MB in %.1fs", len(raw) / 1_048_576, time.time() - t0)

    # Audit 2026-05-26: stream decode + parse instead of materialising
    # the full decompressed text in memory. The compressed payload is
    # already buffered by safe_urlopen (~10 MB), but the decoded text is
    # ~50 MB — keeping it as bytes on the heap during parsing was the
    # bulk of the Pi's update-time RSS.
    gz = gzip.GzipFile(fileobj=io.BytesIO(raw))
    text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
    rows: list[tuple] = []
    for parts in csv.reader(text_stream, delimiter=";"):
        row = _parse_aircraft_csv_row(parts)
        if row is not None:
            rows.append(row)

    log.info("Parsed %d aircraft records", len(rows))

    # Audit 2026-05-26: staging-table swap. The previous DELETE + chunked
    # INSERT pattern left aircraft_db empty between the DELETE commit and
    # the final INSERT — any crash in that window degraded enrichment
    # until the next successful run.
    #
    # CRITICAL: Python's sqlite3 module commits DDL (CREATE/DROP/ALTER)
    # immediately regardless of any surrounding `with conn:` block, so we
    # cannot wrap the swap statements in a transaction. Instead we use a
    # rename-rename-drop sequence that never leaves `aircraft_db` absent:
    #   1. Build `aircraft_db_new` in chunks (collector lock cooperation
    #      preserved; old aircraft_db stays queryable).
    #   2. Validate new count vs. previous (config.AIRCRAFT_DB_MIN_RATIO).
    #   3. RENAME aircraft_db → aircraft_db_old   (atomic per statement)
    #   4. RENAME aircraft_db_new → aircraft_db   (atomic per statement)
    #   5. DROP aircraft_db_old
    #
    # If the process dies between steps 3 and 4, `aircraft_db_old` holds
    # the only surviving copy. `_recover_aborted_swap` runs at the start
    # of each call to detect that case and rename it back.
    _recover_aborted_swap(conn)

    prev_count = conn.execute("SELECT COUNT(*) FROM aircraft_db").fetchone()[0]
    _CHUNK = 5000
    try:
        with conn:
            conn.execute(
                "CREATE TABLE aircraft_db_new ("
                "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
                "type_desc TEXT, flags INTEGER DEFAULT 0)"
            )

        for i in range(0, len(rows), _CHUNK):
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO aircraft_db_new "
                    "(icao_hex, registration, type_code, type_desc, flags) "
                    "VALUES (?,?,?,?,?)",
                    rows[i:i + _CHUNK],
                )

        new_count = conn.execute(
            "SELECT COUNT(*) FROM aircraft_db_new"
        ).fetchone()[0]
        if prev_count > 0 and new_count < int(prev_count * config.AIRCRAFT_DB_MIN_RATIO):
            raise RuntimeError(
                f"aircraft_db swap refused: new={new_count} prev={prev_count} "
                f"(ratio {new_count / prev_count:.2f} < "
                f"{config.AIRCRAFT_DB_MIN_RATIO}); upstream may be truncated"
            )

        # Steps 3-5: rename-rename-drop. Each DDL auto-commits; the
        # invariant is that `aircraft_db` is queryable as either the old
        # or new copy at every observable moment.
        conn.execute("ALTER TABLE aircraft_db RENAME TO aircraft_db_old")
        conn.execute("ALTER TABLE aircraft_db_new RENAME TO aircraft_db")
        conn.execute("DROP TABLE aircraft_db_old")
    except Exception:
        # Cleanup the staging table only. Do NOT touch aircraft_db_old
        # — if the failure happened between the two RENAMEs, it holds
        # the only surviving copy and `_recover_aborted_swap` on the
        # next call will restore it.
        try:
            conn.execute("DROP TABLE IF EXISTS aircraft_db_new")
        except sqlite3.Error:
            pass
        raise

    # Audit-13 A13-018: clear cache immediately after the write so
    # readers don't serve pre-refresh data between the commit here and
    # the run-end clear in main().
    enrichment.clear_cache()

    log.info("aircraft_db updated (%d rows)", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Airlines database import
# ---------------------------------------------------------------------------

def update_airlines_db(conn: sqlite3.Connection) -> int:
    """Download OpenFlights airlines.dat and replace airlines table. Returns row count."""
    log.info("Downloading airlines database from OpenFlights…")
    raw = _fetch(AIRLINES_DAT_URL)
    text = raw.decode("utf-8", errors="replace")

    # Format: airline_id,name,alias,iata,icao,callsign,country,active
    rows = []
    reader = csv.reader(io.StringIO(text))
    for parts in reader:
        if len(parts) < 8:
            continue
        icao_code = parts[4].strip()
        if not icao_code or icao_code == r"\N" or len(icao_code) != 3:
            continue
        name     = parts[1].strip()
        iata     = parts[3].strip() if parts[3].strip() != r"\N" else None
        country  = parts[6].strip() if parts[6].strip() != r"\N" else None
        active   = parts[7].strip() == "Y"
        if not name or name == r"\N":
            continue
        rows.append((icao_code, name, iata, country, active))

    log.info("Parsed %d airline records", len(rows))

    with conn:
        conn.execute("DELETE FROM airlines")
        conn.executemany(
            "INSERT OR REPLACE INTO airlines "
            "(icao_code, name, iata_code, country, active) VALUES (?,?,?,?,?)",
            rows,
        )

    # Audit-13 A13-018: per-step cache invalidation.
    enrichment.clear_cache()

    log.info("airlines updated (%d rows)", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Backfill existing flights with enrichment data
# ---------------------------------------------------------------------------

def backfill_flights(conn: sqlite3.Connection) -> int:
    """
    Update flights rows that have NULL registration or aircraft_type
    using data from aircraft_db. Returns number of rows updated.
    """
    log.info("Backfilling flights with missing registration/type…")

    result = conn.execute(
        """
        UPDATE flights
        SET
            registration  = COALESCE(registration,
                                (SELECT registration FROM aircraft_db
                                 WHERE icao_hex = flights.icao_hex)),
            aircraft_type = COALESCE(aircraft_type,
                                (SELECT type_code FROM aircraft_db
                                 WHERE icao_hex = flights.icao_hex))
        WHERE (registration IS NULL OR aircraft_type IS NULL)
          AND EXISTS (SELECT 1 FROM aircraft_db WHERE icao_hex = flights.icao_hex)
        """
    )
    updated = result.rowcount
    conn.commit()
    log.info("Backfilled %d flight rows", updated)
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    database.init_db()
    conn = database.connect()

    try:
        update_aircraft_db(conn)
        update_airlines_db(conn)
        backfill_flights(conn)
        # `clear_cache()` is already called inside `update_aircraft_db` and
        # `update_airlines_db` (audit-13 A13-018); this final call is a
        # belt-and-suspenders no-op kept for explicitness.
        enrichment.clear_cache()
        log.info("db_updater complete")
    except Exception:
        log.exception("db_updater failed")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
