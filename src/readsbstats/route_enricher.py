"""
readsbstats — flight route enrichment via adsbdb.com.

Looks up origin/destination airports for callsigns using the free
https://api.adsbdb.com/v0/callsign/{callsign} API (no auth required).

Runs as a background daemon thread inside the web process so the
collector (Pi-side, possibly offline) is unaffected.  The enricher
opens its own SQLite connection; the web process reads the results
through the shared WAL file.
"""

import logging
import sqlite3
import threading
import time

import httpx

from . import config, database

log = logging.getLogger("route_enricher")

_ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
_TIMEOUT    = 8.0


# ---------------------------------------------------------------------------
# Pure parsing — no I/O, easily unit-testable
# ---------------------------------------------------------------------------

def _parse_response(data) -> dict | None:
    """
    Extract origin/dest from an adsbdb.com JSON response dict.

    Returns a dict with keys:
        origin_icao, origin_iata, origin_name, origin_country, origin_lat, origin_lon,
        dest_icao, dest_iata, dest_name, dest_country, dest_lat, dest_lon
    or None if the route is not present in the response.
    """
    try:
        fr = data["response"]["flightroute"]
    except (KeyError, TypeError):
        return None
    if not fr:
        return None

    origin = fr.get("origin") or {}
    dest   = fr.get("destination") or {}

    if not origin and not dest:
        return None

    return {
        "origin_icao":    origin.get("icao_code"),
        "origin_iata":    origin.get("iata_code"),
        "origin_name":    origin.get("name"),
        "origin_country": origin.get("country"),
        "origin_lat":     origin.get("latitude"),
        "origin_lon":     origin.get("longitude"),
        "dest_icao":      dest.get("icao_code"),
        "dest_iata":      dest.get("iata_code"),
        "dest_name":      dest.get("name"),
        "dest_country":   dest.get("country"),
        "dest_lat":       dest.get("latitude"),
        "dest_lon":       dest.get("longitude"),
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _is_confirmed_unknown(conn: sqlite3.Connection, callsign: str) -> bool:
    """
    Return True if callsign_routes has a NULL-result entry that is still
    fresh (within ROUTE_CACHE_DAYS).  A resolved entry (with origin_icao
    set) is NOT a confirmed unknown.
    """
    cutoff = int(time.time()) - config.ROUTE_CACHE_DAYS * 86400
    row = conn.execute(
        "SELECT origin_icao, dest_icao FROM callsign_routes "
        "WHERE callsign = ? AND fetched_at > ?",
        (callsign, cutoff),
    ).fetchone()
    if row is None:
        return False
    # A row with both columns NULL is the "confirmed unknown" sentinel
    return row["origin_icao"] is None and row["dest_icao"] is None


def _store_route(conn: sqlite3.Connection, callsign: str, route: dict | None) -> None:
    """Upsert into callsign_routes and (for resolved routes) airports tables."""
    now = int(time.time())

    if route is None:
        conn.execute(
            "INSERT OR REPLACE INTO callsign_routes "
            "(callsign, origin_icao, dest_icao, fetched_at) VALUES (?,NULL,NULL,?)",
            (callsign, now),
        )
    else:
        for prefix in ("origin", "dest"):
            icao = route.get(f"{prefix}_icao")
            if icao:
                conn.execute(
                    "INSERT OR REPLACE INTO airports "
                    "(icao_code, iata_code, name, country, latitude, longitude, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        icao,
                        route.get(f"{prefix}_iata"),
                        route.get(f"{prefix}_name"),
                        route.get(f"{prefix}_country"),
                        route.get(f"{prefix}_lat"),
                        route.get(f"{prefix}_lon"),
                        now,
                    ),
                )
        conn.execute(
            "INSERT OR REPLACE INTO callsign_routes "
            "(callsign, origin_icao, dest_icao, fetched_at) VALUES (?,?,?,?)",
            (callsign, route.get("origin_icao"), route.get("dest_icao"), now),
        )

    conn.commit()


def _apply_to_flights(conn: sqlite3.Connection, callsign: str, route: dict | None) -> None:
    """Back-fill origin_icao / dest_icao on all flights sharing this callsign."""
    origin = route.get("origin_icao") if route else None
    dest   = route.get("dest_icao")   if route else None
    conn.execute(
        "UPDATE flights SET origin_icao = ?, dest_icao = ? WHERE callsign = ?",
        (origin, dest, callsign),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP fetch — synchronous, called from background thread
# ---------------------------------------------------------------------------

class _TransientError(Exception):
    """Raised by _fetch_route when the failure is transient (network, timeout,
    unexpected HTTP error).  Callers must NOT store a confirmed-unknown sentinel
    for these — the callsign should be retried on the next batch."""


def _fetch_route(callsign: str) -> dict | None:
    """
    Call adsbdb.com; return a parsed route dict or None.

    Returns None for a confirmed "route unknown" (404 or empty payload).
    Raises _TransientError for network failures or unexpected HTTP errors so
    callers can skip persisting any result and retry later.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                _ADSBDB_URL.format(callsign=callsign),
                headers={"User-Agent": "readsbstats/1.0"},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return _parse_response(resp.json())
    except httpx.HTTPStatusError as exc:
        log.debug("Route fetch HTTP error for %s: %s", callsign, exc)
        raise _TransientError(str(exc)) from exc
    except Exception as exc:
        log.debug("Route fetch failed for %s: %s", callsign, exc)
        raise _TransientError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Background batch enrichment
# ---------------------------------------------------------------------------

def _enrich_batch(conn: sqlite3.Connection) -> int:
    """
    Find up to ROUTE_BATCH_SIZE closed flights with unresolved callsigns,
    fetch from adsbdb.com, and persist results.

    A callsign is "unresolved" if it has no entry in callsign_routes whose
    fetched_at is newer than ROUTE_CACHE_DAYS (expired NULL entries are retried).

    Returns the number of distinct callsigns processed.
    """
    cutoff = int(time.time()) - config.ROUTE_CACHE_DAYS * 86400
    rows = conn.execute(
        """
        SELECT DISTINCT f.callsign
        FROM flights f
        WHERE f.callsign IS NOT NULL
          AND f.id NOT IN (SELECT flight_id FROM active_flights)
          AND NOT EXISTS (
              SELECT 1 FROM callsign_routes cr
              WHERE cr.callsign = f.callsign
                AND cr.fetched_at > ?
          )
        LIMIT ?
        """,
        (cutoff, config.ROUTE_BATCH_SIZE),
    ).fetchall()

    processed = 0
    transient_failures = 0
    for row in rows:
        cs = row["callsign"]
        try:
            route = _fetch_route(cs)
            _store_route(conn, cs, route)
            _apply_to_flights(conn, cs, route)
        except _TransientError as exc:
            log.debug("Transient error for %s — skipping, will retry next batch: %s", cs, exc)
            transient_failures += 1
        except Exception:
            log.exception("Route enricher error for callsign %s", cs)
        processed += 1
        if config.ROUTE_RATE_LIMIT_SEC > 0:
            time.sleep(config.ROUTE_RATE_LIMIT_SEC)

    if transient_failures:
        log.warning(
            "Route enricher: %d/%d callsign(s) skipped due to transient API errors"
            " — will retry next batch",
            transient_failures, processed,
        )
    if processed:
        log.info("Route enrichment: processed %d callsign(s)", processed)
    return processed


def run_enricher_loop(db_path: str) -> None:
    """Entry point for the background thread. Runs until process exits."""
    conn = database.connect(db_path)
    while True:
        try:
            _enrich_batch(conn)
        except Exception:
            log.exception("Route enricher batch error")
        time.sleep(config.ROUTE_ENRICH_INTERVAL)


def start_background_enricher() -> threading.Thread:
    """Start the route enricher as a daemon thread. Call once at web startup."""
    t = threading.Thread(
        target=run_enricher_loop,
        args=(config.DB_PATH,),
        daemon=True,
        name="route-enricher",
    )
    t.start()
    log.info("Route enricher background thread started")
    return t
