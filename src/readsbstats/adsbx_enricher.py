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

from . import config, database, enrichment, http_safe
from .cleaners import clean_short_text

log = logging.getLogger("adsbx_enricher")

_TIMEOUT = 10.0
# Per-poll response cap.  A 200 NM point query against airplanes.live returns
# < 1 MB even in the busiest skies; cap at 4 MB to give headroom while still
# bounding memory.
_RESPONSE_MAX_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Pure parsing — no I/O, easily unit-testable
# ---------------------------------------------------------------------------

_HEX_CHARS = frozenset("0123456789abcdef")

# BE-5 (Audit 2026-05-31): only the four documented dbFlags bits are stored;
# everything else is dropped so an out-of-range upstream value can't pollute
# the flags column or be misread as a known flag.
_FLAG_MASK = (
    config.FLAG_MILITARY | config.FLAG_INTERESTING
    | config.FLAG_PIA | config.FLAG_LADD
)


def _is_valid_icao_hex(s: str) -> bool:
    """A real Mode-S address is exactly 6 lowercase hex chars.

    Audit-12 #156 — reject malformed values before they land in the
    ``adsbx_overrides`` PK column. Without this, anonymous-tilde-prefixed
    hexes, garbage from upstream JSON, or shorter/longer strings would
    silently pollute the table.
    """
    return len(s) == 6 and all(c in _HEX_CHARS for c in s)


def _coerce_flags(raw) -> int | None:
    """Coerce a raw ``dbFlags`` value to masked flag bits, or ``None``.

    BE-5: ``None`` means *absent or unusable* — the UPSERT then preserves any
    stored flags via ``COALESCE`` rather than clobbering them. A present,
    parseable, non-negative value is masked to the known flag bits (so a huge
    value keeps only its low bits and a present ``0`` legitimately clears).
    A negative value is rejected to ``None``: Python two's-complement masking
    (``-1 & 15 == 15``) would otherwise spuriously set every flag.
    """
    if raw is None:
        return None
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return None
    if v < 0:
        return None
    return v & _FLAG_MASK


def _parse_area_response(data: dict) -> list[dict]:
    """
    Extract aircraft entries from an airplanes.live v2 area response.

    Returns a list of dicts with keys:
        icao_hex, flags, registration, type_code, type_desc
    ``flags`` is ``None`` when ``dbFlags`` was absent/unusable (so the UPSERT
    preserves stored flags). Only includes entries where at least one useful
    field is present. Defensive against a non-list ``ac`` and non-dict items.
    """
    results = []
    ac_list = data.get("ac")
    if not isinstance(ac_list, list):
        return results
    for ac in ac_list:
        if not isinstance(ac, dict):
            continue
        icao = ac.get("hex")
        if not icao or not isinstance(icao, str):
            continue
        icao = icao.strip().lower()
        if not _is_valid_icao_hex(icao):
            continue

        flags = _coerce_flags(ac.get("dbFlags"))

        # PY-10 (Audit 2026-05-31): clean_short_text bounds the field
        # length so a single oversized upstream string can't bloat
        # adsbx_overrides or downstream UI/Telegram caption surfaces.
        # Bounds chosen to match collector._cap for r/t; type_desc has
        # no collector equivalent so 128 is a generous-but-bounded cap.
        reg       = clean_short_text(ac.get("r"),    32)
        type_code = clean_short_text(ac.get("t"),    16)
        type_desc = clean_short_text(ac.get("desc"), 128)

        # Store when flags are present (incl. an explicit 0) or any metadata.
        if flags is not None or reg or type_code or type_desc:
            results.append({
                "icao_hex":     icao,
                "flags":        flags,
                "registration": reg,
                "type_code":    type_code,
                "type_desc":    type_desc,
            })
    return results


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _upsert_overrides(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """
    Upsert parsed entries into adsbx_overrides.
    Returns the number of rows upserted.

    Audit-13 A13-066: previously did N round-trip `conn.execute(...)`
    calls; an entries batch of a few hundred dominated each poll's
    write latency. `executemany` with the same UPSERT clause issues
    one prepared-statement bind per row inside one transaction.
    """
    if not entries:
        return 0
    now = int(time.time())
    rows = [
        (e["icao_hex"], e["flags"],
         e["registration"], e["type_code"], e["type_desc"],
         now, now)
        for e in entries
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO adsbx_overrides
                (icao_hex, flags, registration, type_code, type_desc, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(icao_hex) DO UPDATE SET
                flags        = COALESCE(excluded.flags, adsbx_overrides.flags),
                registration = COALESCE(excluded.registration, adsbx_overrides.registration),
                type_code    = COALESCE(excluded.type_code,    adsbx_overrides.type_code),
                type_desc    = COALESCE(excluded.type_desc,    adsbx_overrides.type_desc),
                last_seen    = excluded.last_seen
            """,
            rows,
        )
    for e in entries:
        enrichment.invalidate_adsbx(e["icao_hex"])
    return len(entries)


# ---------------------------------------------------------------------------
# HTTP fetch — synchronous, called from background thread
# ---------------------------------------------------------------------------

# Alias to the shared exception in http_safe (audit-12 #198).
_TransientError = http_safe.TransientError


class _PermanentError(Exception):
    """Raised when an upstream call hit a non-retryable policy violation.

    Audit-13 A13-021: `safe_httpx_get` raises `UnsafeURLError` on size-cap,
    redirect, or non-HTTPS rejections — all of which indicate an upstream
    schema/policy change that will not heal with exponential backoff.
    The previous catch-all `_TransientError` flooded the log with retries
    forever. The loop now backs off significantly (or skips the cycle)
    on `_PermanentError` rather than treating it as transient.
    """


def _fetch_area(client: httpx.Client | None = None) -> dict:
    """
    Call airplanes.live area endpoint; return the raw JSON dict.
    Raises _TransientError on network/DNS/HTTP failures, _PermanentError on
    policy violations (redirect, size cap, non-HTTPS, private IP).

    Audit-13 A13-068: when called with an already-open `httpx.Client`
    (loop path), the TLS session and pool persist across polls. When
    called without (direct tests), open and close a one-shot Client.
    """
    url = (
        f"{config.ADSBX_API_URL}"
        f"/point/{config.RECEIVER_LAT}/{config.RECEIVER_LON}"
        f"/{config.ADSBX_RANGE_NM}"
    )

    def _call(c: httpx.Client) -> dict:
        resp = http_safe.safe_httpx_get(
            c, url, max_bytes=_RESPONSE_MAX_BYTES,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        if client is not None:
            return _call(client)
        with httpx.Client(
            timeout=_TIMEOUT,
            headers={"User-Agent": "readsbstats/1.0"},
        ) as own_client:
            return _call(own_client)
    except http_safe.UnsafeURLError as exc:
        # Policy errors (size cap, redirect, non-HTTPS, private IP) are
        # permanent — retries will hit the same rejection every time.
        raise _PermanentError(str(exc)) from exc
    except Exception as exc:
        # Includes DNS failures (plain ValueError), network errors, 5xx, etc.
        raise _TransientError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Background enrichment loop
# ---------------------------------------------------------------------------

def _poll_area(conn: sqlite3.Connection, client: httpx.Client | None = None) -> int:
    """
    Fetch aircraft in range from airplanes.live, parse, and upsert overrides.
    Returns the number of overrides upserted.
    """
    data = _fetch_area() if client is None else _fetch_area(client)
    entries = _parse_area_response(data)

    if not entries:
        return 0

    upserted = _upsert_overrides(conn, entries)
    mil_count = sum(1 for e in entries if (e["flags"] or 0) & config.FLAG_MILITARY)
    log.info(
        "ADSBx poll: %d aircraft in range, %d overrides upserted, %d military",
        len(data.get("ac") or []), upserted, mil_count,
    )
    return upserted


def run_enricher_loop(db_path: str) -> None:
    """Entry point for the background thread. Runs until process exits.

    Audit-13 A13-068: one `httpx.Client` lives for the lifetime of the
    loop, so the TLS session and connection pool persist across polls.
    """
    if not config.ADSBX_ENABLED:
        log.info("ADSBx enricher disabled")
        return
    conn = database.connect(db_path)
    sleep_time = config.ADSBX_POLL_INTERVAL
    with httpx.Client(
        timeout=_TIMEOUT,
        headers={"User-Agent": "readsbstats/1.0"},
    ) as client:
        while True:
            try:
                _poll_area(conn, client)
                sleep_time = config.ADSBX_POLL_INTERVAL
            except _PermanentError as exc:
                # Audit-13 A13-021: don't burn the upstream with retries
                # on policy errors — log once, sleep 1 hour, then resume.
                log.warning("ADSBx permanent error (1h backoff): %s", exc)
                sleep_time = 3600
            except _TransientError as exc:
                log.warning("ADSBx poll failed (will retry): %s", exc)
                sleep_time = min(sleep_time * 2, 300)
            except Exception:
                log.exception("ADSBx enricher error")
                sleep_time = config.ADSBX_POLL_INTERVAL
            time.sleep(sleep_time)


_enricher_thread: threading.Thread | None = None


def start_background_enricher() -> threading.Thread | None:
    """Idempotently start the ADSBx enricher daemon thread. Returns None if disabled."""
    global _enricher_thread
    if not config.ADSBX_ENABLED:
        log.info("ADSBx enricher disabled (RSBS_ADSBX_ENABLED=0)")
        return None
    if _enricher_thread is not None and _enricher_thread.is_alive():
        return _enricher_thread
    t = threading.Thread(
        target=run_enricher_loop,
        args=(config.DB_PATH,),
        daemon=True,
        name="adsbx-enricher",
    )
    _enricher_thread = t
    t.start()
    log.info("ADSBx enricher background thread started (source: %s)", config.ADSBX_API_URL)
    return t
