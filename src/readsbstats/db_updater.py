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

from . import config, database, enrichment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("db_updater")

AIRCRAFT_CSV_URL = (
    "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
)
AIRLINES_DAT_URL = (
    "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
)

HEADERS = {"User-Agent": "readsbstats/1.0 db_updater"}


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


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


def update_aircraft_db(conn: sqlite3.Connection) -> int:
    """Download tar1090-db CSV and replace aircraft_db table. Returns row count."""
    log.info("Downloading aircraft database from tar1090-db…")
    t0 = time.time()
    raw = _fetch(AIRCRAFT_CSV_URL)
    log.info("Downloaded %.1f MB in %.1fs", len(raw) / 1_048_576, time.time() - t0)

    # Decompress
    with gzip.open(io.BytesIO(raw)) as gz:
        text = gz.read().decode("utf-8", errors="replace")

    # Use csv.reader to correctly handle quoted fields
    # Format (no header): icao_hex, registration, type_code, flags, type_desc
    _HEX = frozenset("0123456789abcdef")
    rows = []
    for parts in csv.reader(io.StringIO(text), delimiter=";"):
        if not parts:
            continue
        icao_hex = parts[0].strip().lower()
        # ICAO addresses are always exactly 6 hex characters
        if len(icao_hex) != 6 or not all(c in _HEX for c in icao_hex):
            continue
        reg       = parts[1].strip() if len(parts) > 1 else ""
        type_code = parts[2].strip() if len(parts) > 2 else ""
        flags_str = parts[3].strip() if len(parts) > 3 else "0"
        type_desc = parts[4].strip() if len(parts) > 4 else ""

        flags = _parse_flags(flags_str)

        rows.append((
            icao_hex,
            reg       or None,
            type_code or None,
            type_desc or None,
            flags,
        ))

    log.info("Parsed %d aircraft records", len(rows))

    with conn:
        conn.execute("DELETE FROM aircraft_db")
        conn.executemany(
            "INSERT OR IGNORE INTO aircraft_db "
            "(icao_hex, registration, type_code, type_desc, flags) VALUES (?,?,?,?,?)",
            rows,
        )

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
        enrichment.clear_cache()
        log.info("db_updater complete")
    except Exception:
        log.exception("db_updater failed")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
