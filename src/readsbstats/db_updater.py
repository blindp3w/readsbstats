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

from . import adsbx_enricher, config, database, enrichment, http_safe

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

# Rows per executemany batch when streaming the aircraft CSV into the staging
# table. Module-level so tests can shrink it to exercise the multi-batch path.
_INSERT_CHUNK = 5000


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
    """Thin delegate to ``database.recover_aircraft_db_swap``.

    BE-3 (Audit 2026-05-31): the recovery logic moved to ``database`` so the
    web server and collector recover an interrupted swap on startup. Kept here
    as a delegate so ``update_aircraft_db`` and existing tests that reference
    ``db_updater._recover_aborted_swap`` keep working.
    """
    database.recover_aircraft_db_swap(conn)


def update_aircraft_db(conn: sqlite3.Connection) -> int:
    """Download tar1090-db CSV and replace aircraft_db table. Returns row count."""
    log.info("Downloading aircraft database from tar1090-db…")
    t0 = time.time()
    raw = _fetch(AIRCRAFT_CSV_URL)
    log.info("Downloaded %.1f MB in %.1fs", len(raw) / 1_048_576, time.time() - t0)

    # Atomic staging-table swap in ONE transaction:
    #   1. CREATE aircraft_db_new
    #   2. chunked INSERT (old aircraft_db stays queryable to WAL readers)
    #   3. row-count vs. previous validation (AIRCRAFT_DB_MIN_RATIO floor)
    #   4. RENAME aircraft_db → aircraft_db_old → aircraft_db_new → aircraft_db
    #   5. DROP aircraft_db_old
    # Single transaction is required on SQLite 3.45.x (Pi's Ubuntu 24.04) —
    # splitting the CREATE and the INSERTs across two transactions hit a
    # cross-transaction DDL-visibility bug where the second transaction
    # couldn't see the freshly-created staging table. The collector is
    # stopped during the refresh (scripts/update.sh), so holding the write
    # lock for the build is safe.
    # Streaming + chunked executemany keeps peak RSS bounded by one chunk —
    # ~50 MB decoded text + ~620k-row tuple list would otherwise blow the
    # Pi's MemoryMax.
    _recover_aborted_swap(conn)

    prev_count = conn.execute("SELECT COUNT(*) FROM aircraft_db").fetchone()[0]
    parsed_count = 0
    _insert_sql = (
        "INSERT OR IGNORE INTO aircraft_db_new "
        "(icao_hex, registration, type_code, type_desc, flags) VALUES (?,?,?,?,?)"
    )
    # Flush any pending implicit transaction so BEGIN IMMEDIATE can't fail with
    # "cannot start a transaction within a transaction".
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS aircraft_db_new")
        conn.execute(
            "CREATE TABLE aircraft_db_new ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )

        gz = gzip.GzipFile(fileobj=io.BytesIO(raw))
        text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        chunk: list[tuple] = []
        for parts in csv.reader(text_stream, delimiter=";"):
            row = _parse_aircraft_csv_row(parts)
            if row is None:
                continue
            chunk.append(row)
            parsed_count += 1
            if len(chunk) >= _INSERT_CHUNK:
                conn.executemany(_insert_sql, chunk)
                chunk.clear()
        if chunk:
            conn.executemany(_insert_sql, chunk)
            chunk.clear()

        log.info("Parsed %d aircraft records", parsed_count)

        new_count = conn.execute(
            "SELECT COUNT(*) FROM aircraft_db_new"
        ).fetchone()[0]
        if prev_count > 0 and new_count < int(prev_count * config.AIRCRAFT_DB_MIN_RATIO):
            raise RuntimeError(
                f"aircraft_db swap refused: new={new_count} prev={prev_count} "
                f"(ratio {new_count / prev_count:.2f} < "
                f"{config.AIRCRAFT_DB_MIN_RATIO}); upstream may be truncated"
            )

        # Swap within the same transaction — we already hold the write lock.
        conn.execute("ALTER TABLE aircraft_db RENAME TO aircraft_db_old")
        conn.execute("ALTER TABLE aircraft_db_new RENAME TO aircraft_db")
        conn.execute("DROP TABLE aircraft_db_old")
        conn.commit()
    except Exception:
        # Roll the whole staging build + swap back: aircraft_db is never absent
        # and aircraft_db_new is discarded with the transaction. Drop it
        # defensively in case the failure left the connection in autocommit.
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            conn.execute("DROP TABLE IF EXISTS aircraft_db_new")
        except sqlite3.Error:
            pass
        raise

    # Audit-13 A13-018: clear cache immediately after the write so
    # readers don't serve pre-refresh data between the commit here and
    # the run-end clear in main().
    enrichment.clear_cache()

    log.info("aircraft_db updated (%d rows)", parsed_count)
    return parsed_count


# ---------------------------------------------------------------------------
# Airlines database import
# ---------------------------------------------------------------------------

def update_airlines_db(conn: sqlite3.Connection) -> int:
    """Download OpenFlights airlines.dat and replace airlines table. Returns row count.

    PY-7 (Audit 2026-05-31): uses the same staging-table + min-ratio guard
    as update_aircraft_db. A truncated upstream response (a 200 OK with a
    half-streamed body) that parses to far fewer rows than the existing
    table is refused — readers continue to see the old `airlines` table
    until COMMIT, and an interrupted run rolls back wholesale.
    """
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

    prev_count = conn.execute("SELECT COUNT(*) FROM airlines").fetchone()[0]

    # Flush any pending implicit transaction so BEGIN IMMEDIATE can't fail.
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Audit 2026-06-01 W: defensive cleanup matching update_aircraft_db.
        # If a previous run was interrupted mid-swap, airlines_old may linger.
        # `recover_airlines_db_swap` handles this on startup, but dropping
        # here belt-and-braces guards against an unclean state surviving into
        # an in-process update.
        conn.execute("DROP TABLE IF EXISTS airlines_old")
        conn.execute("DROP TABLE IF EXISTS airlines_new")
        conn.execute(
            "CREATE TABLE airlines_new ("
            "icao_code TEXT PRIMARY KEY, name TEXT, iata_code TEXT, "
            "country TEXT, active INTEGER)"
        )
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO airlines_new "
                "(icao_code, name, iata_code, country, active) VALUES (?,?,?,?,?)",
                rows,
            )

        new_count = conn.execute("SELECT COUNT(*) FROM airlines_new").fetchone()[0]
        if prev_count > 0 and new_count < int(prev_count * config.AIRLINES_DB_MIN_RATIO):
            raise RuntimeError(
                f"airlines swap refused: new={new_count} prev={prev_count} "
                f"(ratio {new_count / prev_count:.2f} < "
                f"{config.AIRLINES_DB_MIN_RATIO}); upstream may be truncated"
            )

        conn.execute("ALTER TABLE airlines RENAME TO airlines_old")
        conn.execute("ALTER TABLE airlines_new RENAME TO airlines")
        conn.execute("DROP TABLE airlines_old")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            conn.execute("DROP TABLE IF EXISTS airlines_new")
        except sqlite3.Error:
            pass
        raise

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
        # Code-review follow-up: clear out adsbx_overrides rows for
        # airframes that haven't been seen in a long time so genuinely
        # stale metadata (re-registered tail numbers etc.) doesn't
        # accumulate forever. The UPSERT in adsbx_enricher preserves
        # confirmed values across transient gaps — this is the
        # complementary long-tail cleanup.
        adsbx_enricher.purge_stale_overrides(conn, config.ADSBX_OVERRIDES_TTL_DAYS)
        # `clear_cache()` is already called inside `update_aircraft_db`,
        # `update_airlines_db`, and `purge_stale_overrides` when it
        # actually deleted anything (audit-13 A13-018); this final call
        # is belt-and-suspenders for the no-purge case.
        enrichment.clear_cache()
        log.info("db_updater complete")
    except Exception:
        log.exception("db_updater failed")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
