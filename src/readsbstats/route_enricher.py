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
import urllib.parse

import httpx

from . import config, database, http_safe

log = logging.getLogger("route_enricher")

_ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
_TIMEOUT    = 8.0
# Per-request response cap.  Real adsbdb callsign payloads are < 2 KB; an
# adversarial / compromised upstream returning multi-MB JSON would otherwise
# pin memory in the background thread.
_RESPONSE_MAX_BYTES = 64 * 1024

# Audit-12 #155 — per-callsign cooldown after a transient (network / HTTP)
# failure. Without it, a multi-hour upstream outage hammers the same N
# callsigns every batch interval. The cooldown is in-memory only (lost on
# restart, by design — restart is itself a recovery signal).
_TRANSIENT_COOLDOWN_S = 300
_transient_failure_at: dict[str, float] = {}


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
    """Back-fill origin_icao / dest_icao on all flights sharing this callsign.

    Audit-13 A13-003: when `route is None` (adsbdb returned 404 / negative
    cache), skip the UPDATE entirely. Previously this wrote NULL,NULL
    which silently clobbered previously-resolved origin/dest on flights
    if adsbdb later dropped the route. The negative result is still
    persisted in `callsign_routes` by the caller; flight rows that were
    already resolved must remain so.
    """
    if route is None:
        return
    origin = route.get("origin_icao")
    dest   = route.get("dest_icao")
    conn.execute(
        "UPDATE flights SET origin_icao = ?, dest_icao = ? WHERE callsign = ?",
        (origin, dest, callsign),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP fetch — synchronous, called from background thread
# ---------------------------------------------------------------------------

# Alias to the shared exception in http_safe (audit-12 #198). Kept under the
# leading-underscore name so existing tests (`self.re._TransientError`) keep
# matching without import churn.
_TransientError = http_safe.TransientError


def _fetch_route(callsign: str, client: httpx.Client | None = None) -> dict | None:
    """
    Call adsbdb.com; return a parsed route dict or None.

    Returns None for a confirmed "route unknown" (404 or empty payload).
    Raises _TransientError for network failures or unexpected HTTP errors so
    callers can skip persisting any result and retry later.

    Audit-13 A13-068: prefer passing in an already-open `httpx.Client`
    (reused across the loop's lifetime). `client=None` is supported for
    direct-call tests; the function then opens and closes its own.
    """
    # Percent-encode the callsign — it ultimately originates from
    # third-party ADS-B telemetry and could carry path-traversal /
    # query-injection characters.  Standard callsigns won't be affected.
    url = _ADSBDB_URL.format(callsign=urllib.parse.quote(callsign, safe=""))

    def _call(c: httpx.Client) -> dict | None:
        resp = http_safe.safe_httpx_get(
            c, url, max_bytes=_RESPONSE_MAX_BYTES,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _parse_response(resp.json())

    try:
        if client is not None:
            return _call(client)
        with httpx.Client(
            timeout=_TIMEOUT,
            headers={"User-Agent": "readsbstats/1.0"},
        ) as own_client:
            return _call(own_client)
    except httpx.HTTPStatusError as exc:
        log.debug("Route fetch HTTP error for %s: %s", callsign, exc)
        raise _TransientError(str(exc)) from exc
    except Exception as exc:
        log.debug("Route fetch failed for %s: %s", callsign, exc)
        raise _TransientError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Background batch enrichment
# ---------------------------------------------------------------------------

def _enrich_batch(conn: sqlite3.Connection, client: httpx.Client | None = None) -> int:
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
    cooled_off = 0
    now = time.time()
    for row in rows:
        cs = row["callsign"]
        # Skip if we just failed for this callsign — see _TRANSIENT_COOLDOWN_S.
        last_fail = _transient_failure_at.get(cs)
        if last_fail is not None and now - last_fail < _TRANSIENT_COOLDOWN_S:
            cooled_off += 1
            continue
        # Audit-13 A13-017: prune the cooldown record once it has expired
        # so the dict doesn't grow unboundedly after long upstream
        # outages (previously only `pop` was called on success — failed
        # callsigns lingered forever).
        if last_fail is not None and now - last_fail >= _TRANSIENT_COOLDOWN_S:
            _transient_failure_at.pop(cs, None)
        try:
            # Pass client only when we own one (loop path). Direct test calls
            # and the no-client path use _fetch_route's own context manager.
            route = _fetch_route(cs) if client is None else _fetch_route(cs, client=client)
            _store_route(conn, cs, route)
            _apply_to_flights(conn, cs, route)
            # Success clears any prior failure record so future operational
            # state isn't sticky.
            _transient_failure_at.pop(cs, None)
        except _TransientError as exc:
            log.debug("Transient error for %s — cooldown %ds: %s",
                      cs, _TRANSIENT_COOLDOWN_S, exc)
            _transient_failure_at[cs] = time.time()
            transient_failures += 1
        except Exception:
            log.exception("Route enricher error for callsign %s", cs)
        processed += 1
        if config.ROUTE_RATE_LIMIT_SEC > 0:
            time.sleep(config.ROUTE_RATE_LIMIT_SEC)

    if cooled_off:
        log.debug("Route enricher: skipped %d callsign(s) in cooldown", cooled_off)

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
    """Entry point for the background thread. Runs until process exits.

    Audit-13 A13-068: one `httpx.Client` lives for the lifetime of the
    loop so the TLS session persists across batches.
    """
    conn = database.connect(db_path)
    with httpx.Client(
        timeout=_TIMEOUT,
        headers={"User-Agent": "readsbstats/1.0"},
    ) as client:
        while True:
            try:
                _enrich_batch(conn, client=client)
            except Exception:
                log.exception("Route enricher batch error")
            time.sleep(config.ROUTE_ENRICH_INTERVAL)


_enricher_thread: threading.Thread | None = None


def start_background_enricher() -> threading.Thread:
    """Idempotently start the route enricher daemon thread."""
    global _enricher_thread
    if _enricher_thread is not None and _enricher_thread.is_alive():
        return _enricher_thread
    t = threading.Thread(
        target=run_enricher_loop,
        args=(config.DB_PATH,),
        daemon=True,
        name="route-enricher",
    )
    _enricher_thread = t
    t.start()
    log.info("Route enricher background thread started")
    return t
