"""
readsbstats — external ADS-B flag enrichment via airplanes.live.

Periodically polls the airplanes.live v2 area endpoint to retrieve dbFlags
for aircraft in receiver range.  Stores results in the ``adsbx_overrides``
table so that military (and interesting/PIA/LADD) status is available even
when tar1090-db has no flags for an aircraft.

Runs as a background daemon thread inside the **collector** process.
Disabled when ``RSBS_ADSBX_ENABLED`` is ``0`` / ``false`` / ``no``.
"""

import logging
import sqlite3
import threading
import time

import httpx

from . import config, database, enrichment

log = logging.getLogger("adsbx_enricher")

_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Pure parsing — no I/O, easily unit-testable
# ---------------------------------------------------------------------------

def _parse_area_response(data: dict) -> list[dict]:
    """
    Extract aircraft entries from an airplanes.live v2 area response.

    Returns a list of dicts with keys:
        icao_hex, flags, registration, type_code, type_desc
    Only includes entries where at least one useful field is present.
    """
    results = []
    for ac in data.get("ac") or []:
        icao = ac.get("hex")
        if not icao:
            continue
        icao = icao.strip().lower()
        if not icao:
            continue

        flags = 0
        raw_flags = ac.get("dbFlags")
        if raw_flags is not None:
            try:
                flags = int(raw_flags)
            except (ValueError, TypeError):
                pass

        reg       = ac.get("r") or None
        type_code = ac.get("t") or None
        type_desc = ac.get("desc") or None

        # Only store if there's something useful
        if flags or reg or type_code or type_desc:
            results.append({
                "icao_hex":     icao,
                "flags":        flags,
                "registration": reg.strip() if reg else None,
                "type_code":    type_code.strip() if type_code else None,
                "type_desc":    type_desc.strip() if type_desc else None,
            })
    return results


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _upsert_overrides(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """
    Upsert parsed entries into adsbx_overrides.
    Returns the number of rows upserted.
    """
    now = int(time.time())
    count = 0
    for e in entries:
        conn.execute(
            """
            INSERT INTO adsbx_overrides
                (icao_hex, flags, registration, type_code, type_desc, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(icao_hex) DO UPDATE SET
                flags        = excluded.flags,
                registration = COALESCE(excluded.registration, adsbx_overrides.registration),
                type_code    = COALESCE(excluded.type_code,    adsbx_overrides.type_code),
                type_desc    = COALESCE(excluded.type_desc,    adsbx_overrides.type_desc),
                last_seen    = excluded.last_seen
            """,
            (
                e["icao_hex"], e["flags"],
                e["registration"], e["type_code"], e["type_desc"],
                now, now,
            ),
        )
        enrichment.invalidate_adsbx(e["icao_hex"])
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# HTTP fetch — synchronous, called from background thread
# ---------------------------------------------------------------------------

class _TransientError(Exception):
    """Raised on network / HTTP failures; caller retries next cycle."""


def _fetch_area() -> dict:
    """
    Call airplanes.live area endpoint; return the raw JSON dict.
    Raises _TransientError on any failure.
    """
    url = (
        f"{config.ADSBX_API_URL}"
        f"/point/{config.RECEIVER_LAT}/{config.RECEIVER_LON}"
        f"/{config.ADSBX_RANGE_NM}"
    )
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                url,
                headers={"User-Agent": "readsbstats/1.0"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise _TransientError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Background enrichment loop
# ---------------------------------------------------------------------------

def _poll_area(conn: sqlite3.Connection) -> int:
    """
    Fetch aircraft in range from airplanes.live, parse, and upsert overrides.
    Returns the number of overrides upserted.
    """
    data = _fetch_area()
    entries = _parse_area_response(data)

    if not entries:
        return 0

    upserted = _upsert_overrides(conn, entries)
    mil_count = sum(1 for e in entries if e["flags"] & config.FLAG_MILITARY)
    log.info(
        "ADSBx poll: %d aircraft in range, %d overrides upserted, %d military",
        len(data.get("ac") or []), upserted, mil_count,
    )
    return upserted


def run_enricher_loop(db_path: str) -> None:
    """Entry point for the background thread. Runs until process exits."""
    if not config.ADSBX_ENABLED:
        log.info("ADSBx enricher disabled")
        return
    conn = database.connect(db_path)
    sleep_time = config.ADSBX_POLL_INTERVAL
    while True:
        try:
            _poll_area(conn)
            sleep_time = config.ADSBX_POLL_INTERVAL
        except _TransientError as exc:
            log.warning("ADSBx poll failed (will retry): %s", exc)
            sleep_time = min(sleep_time * 2, 300)
        except Exception:
            log.exception("ADSBx enricher error")
            sleep_time = config.ADSBX_POLL_INTERVAL
        time.sleep(sleep_time)


def start_background_enricher() -> threading.Thread | None:
    """Start the ADSBx enricher as a daemon thread. Returns None if disabled."""
    if not config.ADSBX_ENABLED:
        log.info("ADSBx enricher disabled (RSBS_ADSBX_ENABLED=0)")
        return None
    t = threading.Thread(
        target=run_enricher_loop,
        args=(config.DB_PATH,),
        daemon=True,
        name="adsbx-enricher",
    )
    t.start()
    log.info("ADSBx enricher background thread started (source: %s)", config.ADSBX_API_URL)
    return t
