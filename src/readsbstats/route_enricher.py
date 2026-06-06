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

from . import cleaners, config, database, http_safe

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

    # F04: validate adsbdb fields at the trust boundary — a schema-drifted or
    # compromised upstream must not persist malformed codes / out-of-range
    # coords. Invalid fields are nulled (COALESCE-safe in _store_route).
    return {
        "origin_icao":    cleaners.valid_icao_code(origin.get("icao_code"), 4),
        "origin_iata":    cleaners.valid_icao_code(origin.get("iata_code"), 3),
        "origin_name":    cleaners.clean_short_text(origin.get("name"), 128),
        "origin_country": cleaners.clean_short_text(origin.get("country"), 128),
        "origin_lat":     cleaners.valid_lat(origin.get("latitude")),
        "origin_lon":     cleaners.valid_lon(origin.get("longitude")),
        "dest_icao":      cleaners.valid_icao_code(dest.get("icao_code"), 4),
        "dest_iata":      cleaners.valid_icao_code(dest.get("iata_code"), 3),
        "dest_name":      cleaners.clean_short_text(dest.get("name"), 128),
        "dest_country":   cleaners.clean_short_text(dest.get("country"), 128),
        "dest_lat":       cleaners.valid_lat(dest.get("latitude")),
        "dest_lon":       cleaners.valid_lon(dest.get("longitude")),
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
        # Audit 2026-05-25: adsbdb sometimes returns origin-only or dest-only
        # payloads (see _parse_response). INSERT OR REPLACE would let the
        # missing side overwrite a previously-cached value with NULL; the
        # COALESCE upsert keeps the known side while refreshing fetched_at.
        conn.execute(
            "INSERT INTO callsign_routes (callsign, origin_icao, dest_icao, fetched_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(callsign) DO UPDATE SET "
            "    origin_icao = COALESCE(excluded.origin_icao, callsign_routes.origin_icao), "
            "    dest_icao   = COALESCE(excluded.dest_icao,   callsign_routes.dest_icao), "
            "    fetched_at  = excluded.fetched_at",
            (callsign, route.get("origin_icao"), route.get("dest_icao"), now),
        )

    # Audit 2026-05-26: commit lives in `_enrich_batch` so this call and
    # the matching `_apply_to_flights` form one atomic unit. A crash
    # between them previously left `callsign_routes` claiming the route
    # was fresh while matching `flights` rows stayed stale.


def _apply_to_flights(conn: sqlite3.Connection, callsign: str, route: dict | None) -> None:
    """Back-fill origin_icao / dest_icao on all flights sharing this callsign.

    Audit-13 A13-003: when `route is None` (adsbdb returned 404 / negative
    cache), skip the UPDATE entirely. Previously this wrote NULL,NULL
    which silently clobbered previously-resolved origin/dest on flights
    if adsbdb later dropped the route. The negative result is still
    persisted in `callsign_routes` by the caller; flight rows that were
    already resolved must remain so.

    Audit 2026-05-25: adsbdb also returns origin-only or dest-only payloads
    (see `_parse_response`'s `not origin and not dest` guard). Writing the
    missing side as NULL was the same data-loss bug; the COALESCE keeps
    any previously-resolved column when the incoming side is NULL.
    """
    if route is None:
        return
    origin = route.get("origin_icao")
    dest   = route.get("dest_icao")
    conn.execute(
        "UPDATE flights SET "
        "    origin_icao = COALESCE(?, origin_icao), "
        "    dest_icao   = COALESCE(?, dest_icao) "
        "WHERE callsign = ?",
        (origin, dest, callsign),
    )
    # Audit 2026-05-26: commit lives in `_enrich_batch` — see _store_route.


# ---------------------------------------------------------------------------
# HTTP fetch — synchronous, called from background thread
# ---------------------------------------------------------------------------

# Alias to the shared exception in http_safe (audit-12 #198). Kept under the
# leading-underscore name so existing tests (`self.re._TransientError`) keep
# matching without import churn.
_TransientError = http_safe.TransientError


class _PermanentError(Exception):
    """PY-8 (Audit 2026-05-31): non-retryable policy violation from
    http_safe.safe_httpx_get (redirect, size cap, non-HTTPS, private IP).

    The loop translates this into a DB-backed negative cache row in
    callsign_routes; the existing ROUTE_CACHE_DAYS TTL exclusion then
    suppresses retries for the same callsign. Distinct from
    _TransientError, which uses a process-lifetime in-memory cooldown
    (lost on restart) — permanent failures must be MORE persistent.

    Mirrors adsbx_enricher._PermanentError.
    """


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
    except http_safe.UnsafeURLError as exc:
        # PY-8: policy errors (redirect, size cap, non-HTTPS, private IP)
        # are deterministic — retries hit the same rejection every time.
        # Map to _PermanentError so the loop writes a TTL-bounded negative
        # cache row instead of looping forever.
        log.warning("Route fetch policy error for %s: %s", callsign, exc)
        raise _PermanentError(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        # Audit 17: a non-404 4xx (400/410/422 …) is a deterministic client
        # error — the same request will fail identically every batch. Map it
        # to _PermanentError so the loop writes a TTL-bounded negative cache
        # row instead of refetching the bad callsign forever. 429 (rate
        # limited) and 5xx stay transient (retry via the in-memory cooldown).
        code = getattr(exc.response, "status_code", None)
        if code is not None and 400 <= code < 500 and code != 429:
            log.warning("Route fetch permanent HTTP %s for %s", code, callsign)
            raise _PermanentError(str(exc)) from exc
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
          -- BE-8 + PY-9 (Audit 2026-05-31): only fetch fully alphanumeric
          -- callsigns of length 2-8. The first GLOB matches first-char-is
          -- alnum; the second NOT GLOB excludes any mid-string non-alnum
          -- (LOT/123, AB-CD, AB CD, AB?CD) — without it, GLOB '[A-Z0-9]*'
          -- means "first char alnum, followed by anything".
          AND length(f.callsign) BETWEEN 2 AND 8
          AND f.callsign GLOB '[A-Z0-9]*'
          AND f.callsign NOT GLOB '*[^A-Z0-9]*'
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
    permanent_failures = 0
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
            # Audit 2026-05-26: `_store_route` + `_apply_to_flights` are
            # one transaction — either both write or neither. Previously
            # `_store_route` committed first, so a crash in
            # `_apply_to_flights` left the route cache claiming the
            # callsign was fresh while flights stayed stale.
            with conn:
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
        except _PermanentError as exc:
            # PY-8 (Audit 2026-05-31): persist as a negative cache row so
            # the cutoff query at the top of this function skips the
            # callsign for ROUTE_CACHE_DAYS. The in-memory transient
            # cooldown is the wrong store: it's process-lifetime and
            # too short — a redirect or size-cap rejection won't heal
            # without operator intervention.
            log.warning("Permanent route fetch failure for %s — caching as miss: %s",
                        cs, exc)
            try:
                with conn:
                    _store_route(conn, cs, None)
            except sqlite3.Error:
                log.exception("Failed to persist negative cache for %s", cs)
            permanent_failures += 1
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
            " in batch — will retry next batch",
            transient_failures, processed,
        )
    if permanent_failures:
        # Code-review follow-up: surface permanent failures at batch-level
        # too. Without this an upstream API migration that turns every
        # callsign into a _PermanentError produced only per-callsign WARNINGs
        # and no summary; operators monitoring for the "skipped" pattern
        # saw nothing. Negative cache rows are already in place; this log
        # just gives ops one alertable line per batch.
        log.warning(
            "Route enricher: %d/%d callsign(s) skipped due to permanent API errors"
            " in batch — cached as miss, will not retry until cache expiry",
            permanent_failures, processed,
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
