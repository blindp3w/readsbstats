"""
readsbstats — collector daemon.

Polls /run/readsb/aircraft.json every POLL_INTERVAL_SEC seconds, detects
flight events (gaps > FLIGHT_GAP_SEC = new flight), and writes positions +
flight aggregates to SQLite.

Run as a systemd service (see systemd/readsbstats-collector.service).
"""

import datetime
import json
import logging
import math
import os
import pathlib
import queue
import re
import signal
import socket
import sqlite3
import statistics
import sys
import threading
import time

from . import adsbx_enricher, config, database, enrichment, geo, icao_ranges, metrics_collector, notifier, route_enricher
from .cleaners import clean_short_text

# 24-bit Mode-S address, lowercase hex. Validated *after* stripping the
# leading ~ that marks MLAT entries. 000000 / ffffff are ADS-B sentinels
# for "no transponder address" and are rejected at the call site.
_ICAO_RE = re.compile(r"[0-9a-f]{6}")


def _coerce_float(v) -> float | None:
    """Return ``v`` as a finite float, or None for anything that would
    raise inside a comparison or arithmetic (strings, bools, NaN/Inf).

    bool is excluded explicitly because ``isinstance(True, int)`` is
    True in Python and we want to reject feed data like ``"gs": true``."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def _coerce_int(v) -> int | None:
    """Like ``_coerce_float`` but for integer-typed columns. Floats are
    truncated; non-finite values return None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if math.isfinite(v) else None
    return None


def _cap(value, maxlen: int):
    """BE-8 (Audit 2026-05-31): bound a feed-supplied string to ``maxlen``
    characters. A corrupt or abusive feed could otherwise persist an
    arbitrarily long callsign/registration/etc. into the ``flights`` row.
    Non-strings and empties collapse to None."""
    if not isinstance(value, str):
        return None
    return value[:maxlen] or None

# Sentinel written at startup, removed only on clean shutdown.
# Presence on next startup → previous run ended uncleanly → run quick_check.
_SENTINEL: pathlib.Path = pathlib.Path(config.DB_PATH).parent / ".dirty_shutdown"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# In-memory state (rebuilt from active_flights table on startup)
# ---------------------------------------------------------------------------
# { icao_hex: {"flight_id": int, "last_seen": int, "last_pos_ts": int} }
_active: dict[str, dict] = {}

# ICAOs already notified for mil/interesting (first-sighting); pre-loaded from DB on startup.
# Intentionally unbounded (audit-12 #186): a bounded LRU would re-alert for the
# oldest ICAOs after wraparound, which is the wrong behaviour for "first-ever
# sighting" semantics. The set is bounded in practice by the number of distinct
# flagged/anonymous ICAOs we've ever seen (~tens of thousands over years; <50MB
# of resident memory). Pre-load happens via `_load_notified()` on startup.
_notified_icao: set[str] = set()
# flight_ids already notified for an emergency squawk. Bounded by
# max-concurrent-active-flights: `_close_flight` calls `discard(flight_id)`
# when the flight finalises (audit-12 #186).
_squawk_notified: set[int] = set()
# date on which the last daily summary was sent
_last_summary_date: datetime.date | None = None

_running = True

# Single long-lived dispatch thread + queue.  Photo download + Telegram upload
# can take ~20 s per alert in the worst case; doing this inline in `_poll()`
# would block the poll loop, and spawning a thread per poll would let threads
# accumulate.  The consumer reads alerts serially.
# Bounded so a slow Telegram / photo-CDN during a burst can't grow the queue
# without limit (the consumer does a DB lookup + up to a ~10 MB photo download
# per item — an unbounded backlog is a slow memory leak). 500 is generous:
# normal bursts never approach it. On overflow the hot-path producer
# (`_enqueue_alert`, called from `_poll`) sheds load rather than blocking — a
# blocking put in `_poll` would stall the systemd watchdog (PERF-3).
_NOTIFICATION_QUEUE_MAXSIZE = 500
_notification_queue: "queue.Queue[tuple | None]" = queue.Queue(
    maxsize=_NOTIFICATION_QUEUE_MAXSIZE
)
_consumer_thread: threading.Thread | None = None
# Back-compat alias for tests that want to assert the dispatch thread is
# daemon / non-main.
_notifications_thread: threading.Thread | None = None


def _sd_notify(msg: str) -> None:
    """Send a notification to systemd (no-op if not running under systemd)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        if addr[0] == "@":
            addr = "\0" + addr[1:]
        sock.sendto(msg.encode(), addr)
    finally:
        sock.close()


# Heartbeat interval: WatchdogSec is 60s in the unit file; ticking every 20s
# leaves a 3× margin so a single skipped beat (or a long DB lock) won't trip
# the watchdog.  The thread runs independently of `_poll()` so a write blocked
# on a CREATE INDEX (background migration) cannot starve the heartbeat.
_WATCHDOG_INTERVAL_SEC = 20


def _watchdog_loop() -> None:
    while _running:
        _sd_notify("WATCHDOG=1")
        # Sleep in short slices so shutdown is responsive.
        for _ in range(_WATCHDOG_INTERVAL_SEC):
            if not _running:
                return
            time.sleep(1)


def _shutdown(sig, frame):
    global _running
    log.info("Received signal %s, shutting down…", sig)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------

haversine_nm = geo.haversine_nm


# ---------------------------------------------------------------------------
# Source classification helpers
# ---------------------------------------------------------------------------

def _is_adsb(source_type: str | None) -> bool:
    return source_type in ("adsb_icao", "adsb_icao_nt", "adsr_icao", "adsc")


def _is_mlat(source_type: str | None) -> bool:
    return source_type == "mlat"


def _primary_source(adsb: int, mlat: int, total: int) -> str:
    if total == 0:
        return "other"
    if adsb / total >= 0.8:
        return "adsb"
    if mlat / total >= 0.8:
        return "mlat"
    if (adsb + mlat) / total >= 0.5:
        return "mixed"
    return "other"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _load_active(conn: sqlite3.Connection) -> None:
    """Reload the in-memory active-flights dict from the DB table.

    Audit-13 A13-002: previous query window-functioned the entire
    `positions` table on every startup — seconds-long stall on a
    multi-million-row table. The replacement uses a per-flight
    correlated subquery with a reverse scan of `idx_positions_flight_ts`
    (which is on `(flight_id, ts)`), so each lookup is O(log n)
    instead of O(n). Ties on ts within a flight are handled in two
    stages: the poll loop rejects strictly-backward pos_ts up front, and
    the ghost filter's `dt <= 0` drops equal-ts re-reports once a prior
    fix exists; if a tie ever reached the table, the reverse index scan
    returns the highest-rowid row — the same row the old ORDER BY id DESC
    picked.
    """
    _active.clear()
    rows = conn.execute(
        """
        SELECT af.icao_hex, af.flight_id, af.last_seen,
               p.lat, p.lon, p.ts AS pos_ts, p.gs
        FROM active_flights af
        LEFT JOIN positions p ON p.id = (
            SELECT id FROM positions
            WHERE flight_id = af.flight_id
            ORDER BY ts DESC
            LIMIT 1
        )
        """
    ).fetchall()
    for row in rows:
        # Audit 17: test `pos_ts is not None`, not truthiness — a LEFT JOIN with
        # no positions row yields NULL (→ None), but a real epoch `0` is a
        # legitimate (if pathological) timestamp and must not be conflated with
        # "no position" or it would drop the restored ghost/GS-filter baseline.
        has_pos = row["pos_ts"] is not None
        _active[row["icao_hex"]] = {
            "flight_id": row["flight_id"],
            "last_seen": row["last_seen"],
            "last_pos_ts": row["pos_ts"] if has_pos else row["last_seen"],
            "last_lat": row["lat"] if has_pos else None,
            "last_lon": row["lon"] if has_pos else None,
            "last_gs": row["gs"] if has_pos else None,
            "last_gs_ts": row["pos_ts"] if has_pos else row["last_seen"],
        }
    log.info("Loaded %d active flights from DB", len(_active))


def _enrich(conn: sqlite3.Connection, icao: str, registration, aircraft_type):
    """
    Look up aircraft_db and adsbx_overrides for reg/type/flags.
    Always queries (cached) so that flags are available even when readsb
    already supplied reg+type.
    Returns (registration, aircraft_type, type_desc, flags, found_in_db).
    """
    db_row = enrichment.lookup_aircraft(conn, icao)
    type_desc    = None
    flags        = 0
    found_in_db  = db_row is not None
    if db_row:
        registration  = registration  or db_row.get("registration")
        aircraft_type = aircraft_type or db_row.get("type_code")
        type_desc     = db_row.get("type_desc")
        flags         = db_row.get("flags") or 0

    # Merge ADSBexchange overrides (OR-merge flags, fill missing fields)
    adsbx_row = enrichment.lookup_adsbx(conn, icao)
    if adsbx_row:
        flags         = flags | (adsbx_row.get("flags") or 0)
        registration  = registration  or adsbx_row.get("registration")
        aircraft_type = aircraft_type or adsbx_row.get("type_code")
        type_desc     = type_desc     or adsbx_row.get("type_desc")
        found_in_db   = True

    # Computed anonymous bit: address falls outside any state-allocated block.
    # Mirrors the SQL CASE in api._deps._FLAGS_EXPR_F so retention and notification
    # logic in this module see the same bitmask the UI / API surface.
    if icao_ranges.is_anonymous_icao(icao):
        flags |= config.FLAG_ANONYMOUS

    return registration, aircraft_type, type_desc, flags, found_in_db


def _open_flight(
    conn: sqlite3.Connection,
    icao: str,
    pos_ts: int,
    callsign,
    registration,
    aircraft_type,
    squawk,
    category,
    lat: float,
    lon: float,
    alt: int | None,
    gs,
    source_type,
    distance_nm: float | None,
    distance_bearing: float | None,
) -> int:
    """Insert a new flight row and register it as active. Returns flight_id."""
    cur = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, registration, aircraft_type, squawk, category,
             first_seen, last_seen,
             max_alt_baro, max_gs, max_distance_nm, max_distance_bearing,
             total_positions, adsb_positions, mlat_positions,
             lat_min, lat_max, lon_min, lon_max)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?)
        """,
        (
            icao, callsign, registration, aircraft_type, squawk, category,
            pos_ts, pos_ts,
            alt, gs, distance_nm, distance_bearing,
            lat, lat, lon, lon,
        ),
    )
    flight_id = cur.lastrowid
    conn.execute(
        "INSERT OR REPLACE INTO active_flights VALUES (?,?,?)",
        (icao, flight_id, pos_ts),
    )
    _active[icao] = {
        "flight_id": flight_id,
        "last_seen": pos_ts,
        # Invariant: paired with the strict `<` skip in the poll loop —
        # equal timestamps are processed as new observations; only
        # strictly-backward `pos_ts < last_pos_ts` is rejected. Keeps the
        # collector resilient to NTP backward-steps without dropping
        # legitimate same-second samples.
        "last_pos_ts": pos_ts,
        "last_lat": lat,
        "last_lon": lon,
        "last_gs": gs,
        "last_gs_ts": pos_ts,
    }
    return flight_id


def _close_flight(conn: sqlite3.Connection, icao: str) -> None:
    """Finalise a flight: compute primary_source, delete if too few positions.

    Precondition (Audit 2026-06-01 W): the caller must ensure these writes
    commit or roll back atomically. The function issues 3-5 dependent writes
    (DELETE active_flights, optional DELETE flights, UPDATE positions.gs=NULL,
    UPDATE flights.max_gs, UPDATE flights.primary_source) and does NOT open
    its own transaction. All current callers satisfy this:

      * ``_poll`` (gap closure ~ :792, expiry ~ :941): inside an implicit
        deferred transaction that ``_poll`` commits at the end of the cycle.
      * shutdown (``main`` ~ :1304): wrapped explicitly in ``with conn:``.

    Do NOT call this from a connection in autocommit mode
    (``isolation_level=None``); partial state on failure will not roll back.
    """
    state = _active.pop(icao, None)
    if state is None:
        return
    flight_id = state["flight_id"]
    conn.execute("DELETE FROM active_flights WHERE icao_hex = ?", (icao,))
    # Audit-12 #186 — keep _squawk_notified bounded by max-active-flights.
    _squawk_notified.discard(flight_id)

    row = conn.execute(
        "SELECT total_positions, adsb_positions, mlat_positions FROM flights WHERE id = ?",
        (flight_id,),
    ).fetchone()

    if row is None:
        return

    total = row["total_positions"]
    if total < config.MIN_POSITIONS_KEEP:
        # Keep flagged aircraft (military/interesting/anonymous) even with few
        # positions — single-position sightings at the edge of range are still
        # valuable, and a non-ICAO hex is the whole point of FLAG_ANONYMOUS.
        _, _, _, flags, _ = _enrich(conn, icao, None, None)
        if not (flags & (config.FLAG_MILITARY | config.FLAG_INTERESTING | config.FLAG_ANONYMOUS)):
            conn.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
            return

    # Statistical outlier filter: null MLAT GS values that are extreme outliers
    # vs. the flight's own distribution.  Catches isolated leading spikes that
    # the per-sample acceleration filter misses (no predecessor, or huge time gap).
    mlat_gs = conn.execute(
        "SELECT id, gs FROM positions "
        "WHERE flight_id = ? AND gs IS NOT NULL AND source_type = 'mlat'",
        (flight_id,),
    ).fetchall()
    if len(mlat_gs) >= config.MLAT_OUTLIER_MIN_READINGS:
        gs_sorted = sorted(r[1] for r in mlat_gs)
        p75 = statistics.quantiles(gs_sorted, n=4)[2]
        # W-4 (Audit 2026-06-01): skip the filter when p75 == 0. Otherwise
        # threshold collapses to 0 and `gs > threshold` matches every positive
        # reading — wiping legitimate taxi/takeoff-roll fixes from a flight
        # whose GS distribution is mostly zeros (ground movement, low feed).
        if p75 > 0:
            threshold = p75 * config.MLAT_OUTLIER_FACTOR
            outlier_ids = [r[0] for r in mlat_gs if r[1] > threshold]
            if outlier_ids:
                placeholders = ",".join("?" * len(outlier_ids))
                conn.execute(
                    f"UPDATE positions SET gs = NULL WHERE id IN ({placeholders})",
                    outlier_ids,
                )
                new_max = conn.execute(
                    "SELECT MAX(gs) FROM positions WHERE flight_id = ? AND gs IS NOT NULL",
                    (flight_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE flights SET max_gs = ? WHERE id = ?", (new_max, flight_id)
                )
                log.debug(
                    "MLAT GS outlier: nulled %d position(s) for flight %d "
                    "(p75=%.1f kts, threshold=%.1f kts)",
                    len(outlier_ids), flight_id, p75, threshold,
                )

    primary = _primary_source(row["adsb_positions"], row["mlat_positions"], total)
    conn.execute(
        "UPDATE flights SET primary_source = ? WHERE id = ?",
        (primary, flight_id),
    )


def _insert_position(
    conn: sqlite3.Connection,
    flight_id: int,
    ts: int,
    lat: float,
    lon: float,
    alt_baro,
    alt_geom,
    gs,
    track,
    baro_rate,
    rssi,
    messages,
    source_type,
) -> None:
    conn.execute(
        """
        INSERT INTO positions
            (flight_id, ts, lat, lon, alt_baro, alt_geom,
             gs, track, baro_rate, rssi, messages, source_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            flight_id, ts, lat, lon, alt_baro, alt_geom,
            gs, track, baro_rate, rssi, messages, source_type,
        ),
    )


def _update_flight_agg(
    conn: sqlite3.Connection,
    flight_id: int,
    icao: str,
    pos_ts: int,
    callsign,
    registration,
    aircraft_type,
    squawk,
    lat: float,
    lon: float,
    alt: int | None,
    gs,
    rssi,
    source_type: str | None,
    distance_nm: float | None,
    distance_bearing: float | None,
    category=None,
) -> None:
    """Update aggregate columns on the flights row.

    `category` uses COALESCE(existing, new) like callsign/registration —
    readsb often emits `category` only after the first position, and we
    want to capture it whenever it first appears in the flight."""
    adsb_inc = 1 if _is_adsb(source_type) else 0
    mlat_inc = 1 if _is_mlat(source_type) else 0

    conn.execute(
        """
        UPDATE flights SET
            last_seen        = MAX(last_seen, ?),
            callsign         = COALESCE(callsign, ?),
            registration     = COALESCE(registration, ?),
            aircraft_type    = COALESCE(aircraft_type, ?),
            squawk           = COALESCE(?, squawk),
            category         = COALESCE(category, ?),
            max_alt_baro     = CASE WHEN ? IS NOT NULL AND (max_alt_baro IS NULL OR ? > max_alt_baro)
                                    THEN ? ELSE max_alt_baro END,
            max_gs           = CASE WHEN ? IS NOT NULL AND (max_gs IS NULL OR ? > max_gs)
                                    THEN ? ELSE max_gs END,
            min_rssi         = CASE WHEN ? IS NOT NULL AND (min_rssi IS NULL OR ? < min_rssi)
                                    THEN ? ELSE min_rssi END,
            max_rssi         = CASE WHEN ? IS NOT NULL AND (max_rssi IS NULL OR ? > max_rssi)
                                    THEN ? ELSE max_rssi END,
            max_distance_nm  = CASE WHEN ? IS NOT NULL AND (max_distance_nm IS NULL OR ? > max_distance_nm)
                                    THEN ? ELSE max_distance_nm END,
            max_distance_bearing = CASE WHEN ? IS NOT NULL AND (max_distance_nm IS NULL OR ? > max_distance_nm)
                                    THEN ? ELSE max_distance_bearing END,
            total_positions  = total_positions + 1,
            adsb_positions   = adsb_positions + ?,
            mlat_positions   = mlat_positions + ?,
            lat_min = CASE WHEN lat_min IS NULL OR ? < lat_min THEN ? ELSE lat_min END,
            lat_max = CASE WHEN lat_max IS NULL OR ? > lat_max THEN ? ELSE lat_max END,
            lon_min = CASE WHEN lon_min IS NULL OR ? < lon_min THEN ? ELSE lon_min END,
            lon_max = CASE WHEN lon_max IS NULL OR ? > lon_max THEN ? ELSE lon_max END
        WHERE id = ?
        """,
        (
            pos_ts,
            callsign, registration, aircraft_type, squawk, category,
            alt, alt, alt,
            gs, gs, gs,
            rssi, rssi, rssi,
            rssi, rssi, rssi,
            distance_nm, distance_nm, distance_nm,
            distance_nm, distance_nm, distance_bearing,
            adsb_inc, mlat_inc,
            lat, lat,
            lat, lat,
            lon, lon,
            lon, lon,
            flight_id,
        ),
    )
    _active[icao]["last_seen"] = pos_ts


# ---------------------------------------------------------------------------
# Core poll
# ---------------------------------------------------------------------------

_last_mtime: float = 0.0


def _read_aircraft_json() -> dict | None:
    """Read and parse aircraft.json; return None if unchanged or unreadable."""
    global _last_mtime
    try:
        st = os.stat(config.AIRCRAFT_JSON)
    except OSError as exc:
        log.warning("Cannot stat %s: %s", config.AIRCRAFT_JSON, exc)
        return None

    if st.st_mtime == _last_mtime:
        return None

    try:
        with open(config.AIRCRAFT_JSON) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot read %s: %s", config.AIRCRAFT_JSON, exc)
        return None

    _last_mtime = st.st_mtime
    return data


# Audit 17: expected tuple length per notification kind (1 kind tag + N args).
# The producer in _poll builds these positionally; asserting the arity here
# turns a future field-reorder/misalignment into a visible, dropped-with-warning
# event instead of silently passing the wrong args to a Telegram caption builder.
_NOTIFY_ARITY = {"mil": 7, "int": 7, "anon": 7, "sqk": 6, "wl": 9}


def _dispatch_one(item: tuple) -> None:
    """Dispatch a single queued notification.  Exceptions are caller's
    responsibility (the consumer logs and continues)."""
    kind = item[0]
    expected = _NOTIFY_ARITY.get(kind)
    if expected is not None and len(item) != expected:
        log.warning(
            "_dispatch_one: kind=%r expected %d fields, got %d — dropping",
            kind, expected, len(item),
        )
        return
    if kind == "mil":
        notifier.notify_military(item[1], item[2], item[3], item[4], item[5], item[6])
    elif kind == "int":
        notifier.notify_interesting(item[1], item[2], item[3], item[4], item[5], item[6])
    elif kind == "anon":
        notifier.notify_anonymous(item[1], item[2], item[3], item[4], item[5], item[6])
    elif kind == "sqk":
        notifier.notify_squawk(item[1], item[2], item[3], item[4], item[5])
    elif kind == "wl":
        notifier.notify_watchlist(item[1], item[2], item[3], item[4], item[5],
                                  item[6], item[7], item[8])
    else:
        # Audit-13 A13-027: previously this silently dropped the notification.
        # A typo or future-version kind now surfaces in journalctl so the
        # alert loss is at least visible.
        log.warning("_dispatch_one: unknown notification kind=%r — dropping", kind)


def _notification_consumer() -> None:
    """Long-lived daemon: pull items off the notification queue and dispatch
    them serially.  Opens one sqlite connection at startup and stashes it on
    ``notifier._thread_local`` so every call to ``notifier._get_photo_result``
    in this thread reuses it instead of reopening per alert.  A None sentinel
    breaks the loop (used for graceful shutdown / test teardown)."""
    conn = None
    if config.DB_PATH:
        try:
            conn = database.connect(config.DB_PATH)
        except Exception:
            log.exception("Consumer failed to open DB connection; "
                          "falling back to per-alert connections")
            conn = None
    if conn is not None:
        notifier._thread_local.conn = conn
    try:
        while True:
            item = _notification_queue.get()
            try:
                if item is None:
                    return
                try:
                    _dispatch_one(item)
                except Exception:
                    log.exception("Notification dispatch error")
            finally:
                _notification_queue.task_done()
    finally:
        if conn is not None:
            try:
                del notifier._thread_local.conn
            except AttributeError:
                pass
            conn.close()


def start_notification_consumer() -> threading.Thread:
    """Idempotently start the consumer thread.  Returns the thread."""
    global _consumer_thread, _notifications_thread
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return _consumer_thread
    t = threading.Thread(
        target=_notification_consumer, daemon=True, name="tg-dispatch",
    )
    _consumer_thread = t
    _notifications_thread = t
    t.start()
    return t


def _drain_notifications(timeout: float = 1.0) -> None:
    """Block (busy-wait) until all queued notifications have been processed.
    Used by tests after triggering ``_poll()``.  Returns silently after
    *timeout* seconds even if work remains."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _notification_queue.unfinished_tasks == 0:
            return
        time.sleep(0.01)


def _enqueue_alert(item: tuple) -> None:
    """Enqueue one alert for the dispatch consumer from the poll hot path.

    Non-blocking by contract: this runs inside ``_poll()`` and a blocking
    ``put`` on a full queue could stall for as long as the consumer takes to
    drain one item (a DB lookup + up to a ~10 MB photo download) — long enough
    to miss the systemd watchdog and get the service killed. When the bounded
    queue is full we shed load: drop the alert and log a warning (PERF-3). The
    shutdown sentinel uses a blocking ``put`` instead — it runs outside
    ``_poll`` while the consumer is actively draining.
    """
    try:
        _notification_queue.put_nowait(item)
    except queue.Full:
        log.warning("notification queue full; dropping %s alert", item[0])


def stop_notification_consumer(timeout: float = 5.0) -> None:
    """Drain the notification queue, post the sentinel, and join the
    consumer thread. Audit-12 #145 — the consumer is a daemon thread the
    interpreter would otherwise kill abruptly at process exit, dropping
    any Telegram alerts queued by the last `_poll()` before SIGTERM.

    Idempotent and safe to call when the consumer was never started."""
    global _consumer_thread
    t = _consumer_thread
    if t is None or not t.is_alive():
        return
    # Process anything queued before we ask the consumer to stop. Give
    # half the budget to draining real work; the rest to the post-sentinel
    # join.
    _drain_notifications(timeout=max(0.1, timeout / 2))
    _notification_queue.put(None)
    t.join(timeout=max(0.1, timeout / 2))
    if not t.is_alive():
        _consumer_thread = None


def _poll(conn: sqlite3.Connection) -> None:
    data = _read_aircraft_json()
    if data is None:
        return

    # BE-6 (Audit 2026-05-31): validate the top-level shape before touching it.
    # _read_aircraft_json returns whatever json.load produced — a corrupt feed
    # could be a list, number, or string. A bad shape used to raise out of here
    # and abort the whole cycle via main()'s except; now we skip gracefully.
    if not isinstance(data, dict):
        log.warning("aircraft.json top-level is %s, not an object — skipping poll",
                    type(data).__name__)
        return

    ref_time = _coerce_float(data.get("now"))
    if ref_time is None:
        ref_time = time.time()
    aircraft_list = data.get("aircraft", [])
    if not isinstance(aircraft_list, list):
        log.warning("aircraft.json 'aircraft' is %s, not a list — skipping poll",
                    type(aircraft_list).__name__)
        return
    now_epoch = int(time.time())
    # Notifications are queued here and sent after the transaction commits,
    # so network I/O never blocks or extends a DB write lock.
    _tg = notifier.telegram_enabled()
    _pending: list[tuple] = []

    # Load watchlist once per poll cycle (tiny table, negligible overhead)
    watchlist = conn.execute(
        "SELECT match_type, value, label FROM watchlist"
    ).fetchall() if _tg else []

    with conn:
        for ac in aircraft_list:
            # BE-6: a non-dict entry (string/None/int from a corrupt feed)
            # would raise on the first `ac.get(...)` — skip it per-entry.
            if not isinstance(ac, dict):
                continue
            # ── Strict-skip fields: any non-numeric / out-of-range value
            # for lat/lon/seen_pos/hex skips just this aircraft. All
            # coercion happens here, BEFORE the first DB write, so a
            # malformed record cannot leave a partial flight behind.
            # (Audit 2026-05-26: previously a string lat raised TypeError
            # at the `<=` comparison, swallowed by the outer except,
            # aborting the entire poll cycle.)
            lat = _coerce_float(ac.get("lat"))
            lon = _coerce_float(ac.get("lon"))
            if lat is None or lon is None:
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue

            seen_pos = _coerce_float(ac.get("seen_pos"))
            if seen_pos is None or seen_pos > config.MAX_SEEN_POS_SEC:
                continue
            pos_ts = int(ref_time - seen_pos)

            raw_hex = ac.get("hex", "")
            if not isinstance(raw_hex, str):
                continue
            is_mlat_hex = raw_hex.startswith("~")
            icao = raw_hex.lstrip("~").lower()
            if not _ICAO_RE.fullmatch(icao):
                continue
            if icao in ("000000", "ffffff"):  # ADS-B "no transponder" sentinels
                continue

            # PY-3 (Audit 2026-05-31): coerce to a bounded string before
            # SQLite binding. A non-string `type` (dict/list/number) used
            # to raise sqlite3.ProgrammingError and roll back the whole
            # poll; an oversized string would bloat positions.source_type.
            #
            # Behaviour change (code-review note): for a numeric `type`
            # value — not produced by stock readsb but possible from a
            # custom feed — the old path raised and the outer try/except
            # skipped that aircraft entry. The new path stores
            # source_type=NULL and processes the row. Downstream
            # `_is_adsb(None)` / `_is_mlat(None)` both return False, so
            # the position is counted as `other` rather than ADS-B or
            # MLAT. Record-and-degrade beats whole-poll rollback; if a
            # feed format ever emits numeric `type` codes routinely,
            # add an explicit mapping here.
            source_type = clean_short_text(ac.get("type"), 32)
            if is_mlat_hex and not source_type:
                source_type = "mlat"

            state = _active.get(icao)
            # Strict `<` (not `<=`): equal pos_ts is permitted here; the ghost
            # filter further down (`if dt <= 0: continue`) rejects it once we
            # know the prior fix — avoids divide-by-zero in implied-kts and
            # drops no-new-fix samples that readsb sometimes re-reports.
            # See _open_flight's last_pos_ts invariant.
            if state is not None and pos_ts < state["last_pos_ts"]:
                continue

            # ── Flexible fields: bad coercion becomes NULL, the record
            # still processes. Callsign/registration/squawk are strings
            # in the feed; non-string trash becomes None.
            callsign      = (ac.get("flight") or "").strip() or None \
                if isinstance(ac.get("flight"), str) else None
            if callsign and "@" in callsign:
                callsign = None
            # BE-8: cap every feed-supplied string at ingestion so a corrupt
            # feed can't store unbounded values into the flights row.
            callsign      = _cap(callsign, 16)
            registration  = _cap(ac.get("r"), 32)
            aircraft_type = _cap(ac.get("t"), 16)
            squawk        = _cap(ac.get("squawk"), 8)
            category      = _cap(ac.get("category"), 16)

            raw_alt = ac.get("alt_baro")
            if raw_alt == "ground":
                alt_baro = 0
            else:
                alt_baro = _coerce_int(raw_alt)
            alt_geom  = _coerce_int(ac.get("alt_geom"))
            gs        = _coerce_float(ac.get("gs"))
            track     = _coerce_float(ac.get("track"))
            baro_rate = _coerce_int(ac.get("baro_rate"))
            rssi      = _coerce_float(ac.get("rssi"))
            messages  = _coerce_int(ac.get("messages"))

            # Enrich with local aircraft_db (cached); always called to get flags
            registration, aircraft_type, type_desc, flags, found_in_db = _enrich(
                conn, icao, registration, aircraft_type
            )

            # Null impossible GS values; civil aircraft use a tighter limit than
            # military or unknown (not in aircraft_db) aircraft.
            if gs is not None:
                is_military = bool(flags & config.FLAG_MILITARY)
                gs_limit = (
                    config.MAX_GS_MILITARY_KTS
                    if (is_military or not found_in_db)
                    else config.MAX_GS_CIVIL_KTS
                )
                if gs > gs_limit:
                    log.debug(
                        "Implausible GS nulled for %s: %.0f kts > %d kts limit",
                        icao, gs, gs_limit,
                    )
                    gs = None

            # Distance and bearing from receiver
            distance_nm = haversine_nm(
                config.RECEIVER_LAT, config.RECEIVER_LON, lat, lon
            )
            distance_bearing = geo.bearing(
                config.RECEIVER_LAT, config.RECEIVER_LON, lat, lon
            )
            if distance_nm > config.RECEIVER_MAX_RANGE:
                continue

            # ----------------------------------------------------------------
            # Flight detection
            # ----------------------------------------------------------------
            is_new_flight = False
            if state is None:
                flight_id = _open_flight(
                    conn, icao, pos_ts, callsign, registration, aircraft_type,
                    squawk, category, lat, lon, alt_baro, gs, source_type,
                    distance_nm, distance_bearing,
                )
                is_new_flight = True
            else:
                gap = pos_ts - state["last_seen"]
                if gap > config.FLIGHT_GAP_SEC:
                    _close_flight(conn, icao)
                    flight_id = _open_flight(
                        conn, icao, pos_ts, callsign, registration, aircraft_type,
                        squawk, category, lat, lon, alt_baro, gs, source_type,
                        distance_nm, distance_bearing,
                    )
                    is_new_flight = True
                else:
                    flight_id = state["flight_id"]

            # ----------------------------------------------------------------
            # Ghost-position filter: reject teleporting ADS-B outliers
            # Audit 2026-05-25: filters MUST run before the notification block
            # so a rejected sample cannot queue an alert or mutate the dedupe
            # sets (`_notified_icao`, `_squawk_notified`). Previously a ghost
            # ADS-B jump carrying squawk 7x00 produced an emergency alert for
            # a position the collector then discarded, and locked the flight
            # out of future legitimate squawk alerts.
            # ----------------------------------------------------------------
            if not is_new_flight:
                prev_lat = state.get("last_lat")
                prev_lon = state.get("last_lon")
                dt = pos_ts - state["last_pos_ts"]
                if prev_lat is not None and prev_lon is not None:
                    if dt <= 0:
                        continue
                    jump_nm = haversine_nm(prev_lat, prev_lon, lat, lon)
                    implied_kts = jump_nm / (dt / 3600.0)
                    if implied_kts > config.MAX_SPEED_KTS:
                        log.debug(
                            "Ghost position rejected for %s: %.0f kts implied "
                            "(%.1f nm in %ds)",
                            icao, implied_kts, jump_nm, dt,
                        )
                        continue

                    # Cross-validate reported GS against position-derived speed.
                    # Only when dt is long enough (≥30s) to make the comparison
                    # meaningful; shorter intervals have too much position noise.
                    if (gs is not None and dt >= 30
                            and abs(gs - implied_kts) > config.MAX_GS_DEVIATION_KTS):
                        log.debug(
                            "Implausible GS nulled for %s: reported %.0f kts vs "
                            "%.0f kts position-derived (%.0f kts apart)",
                            icao, gs, implied_kts, abs(gs - implied_kts),
                        )
                        gs = None

            # ----------------------------------------------------------------
            # MLAT GS acceleration filter: null single-sample spikes
            # ----------------------------------------------------------------
            if (gs is not None and _is_mlat(source_type)
                    and not is_new_flight):
                prev_gs = state.get("last_gs")
                if prev_gs is not None:
                    # Audit 17: measure acceleration over the interval since the
                    # last *valid* GS, not the last sample. last_gs retains the
                    # last trusted value across nulled samples, so pairing it
                    # with last_pos_ts (which advances on every sample) would
                    # divide a multi-interval gs-delta by a one-interval dt and
                    # inflate accel. Fall back to last_pos_ts for pre-upgrade
                    # state dicts that predate last_gs_ts.
                    dt_gs = pos_ts - state.get("last_gs_ts", state["last_pos_ts"])
                    if dt_gs > 0:
                        accel = abs(gs - prev_gs) / dt_gs
                        if accel > config.MAX_GS_ACCEL_KTS_S:
                            log.debug(
                                "MLAT GS spike nulled for %s: %.0f→%.0f kts "
                                "in %ds (%.1f kts/s > %.1f limit)",
                                icao, prev_gs, gs, dt_gs,
                                accel, config.MAX_GS_ACCEL_KTS_S,
                            )
                            gs = None

            # ----------------------------------------------------------------
            # Insert position and update aggregates
            # ----------------------------------------------------------------
            _insert_position(
                conn, flight_id, pos_ts,
                lat, lon, alt_baro, alt_geom,
                gs, track, baro_rate, rssi, messages, source_type,
            )
            _update_flight_agg(
                conn, flight_id, icao, pos_ts,
                callsign, registration, aircraft_type, squawk,
                lat, lon, alt_baro, gs, rssi, source_type,
                distance_nm, distance_bearing,
                category=category,
            )
            _active[icao]["last_pos_ts"] = pos_ts
            _active[icao]["last_lat"] = lat
            _active[icao]["last_lon"] = lon
            if gs is not None:
                _active[icao]["last_gs"] = gs
                _active[icao]["last_gs_ts"] = pos_ts

            # ----------------------------------------------------------------
            # Queue notifications (sent after transaction commits).
            # Runs AFTER the ghost/GS filters and the position write so a
            # rejected sample cannot produce a spurious alert or poison the
            # dedupe sets.
            # ----------------------------------------------------------------
            if _tg:
                # Military / interesting / anonymous: only on first-ever sighting of this ICAO.
                # Also fires mid-flight when ADSBx enricher confirms military status
                # (late-discovery: ADSBx polls every ~60s, may arrive after flight opens).
                # Precedence (highest first): military > interesting > anonymous. A
                # single ICAO gets at most one alert kind per first sighting.
                interest_mask = config.FLAG_MILITARY | config.FLAG_INTERESTING
                if config.TELEGRAM_ANONYMOUS_ALERT:
                    interest_mask |= config.FLAG_ANONYMOUS
                if (flags & interest_mask) and icao not in _notified_icao:
                    prev = conn.execute(
                        "SELECT COUNT(*) FROM flights WHERE icao_hex = ? AND id != ?",
                        (icao, flight_id),
                    ).fetchone()[0]
                    if prev == 0:
                        if flags & config.FLAG_MILITARY:
                            kind = "mil"
                        elif flags & config.FLAG_INTERESTING:
                            kind = "int"
                        else:
                            kind = "anon"
                        _pending.append(
                            (kind, icao, registration, callsign, type_desc, aircraft_type, distance_nm)
                        )
                    _notified_icao.add(icao)

                # Emergency squawk: once per flight
                if squawk in notifier.EMERGENCY_SQUAWKS and flight_id not in _squawk_notified:
                    _squawk_notified.add(flight_id)
                    _pending.append(("sqk", icao, registration, callsign, squawk, distance_nm))

                # Watchlist: once per flight (deduped via is_new_flight check)
                if is_new_flight and watchlist:
                    hit = False
                    hit_label = None
                    for entry in watchlist:
                        mt, val, lbl = entry["match_type"], entry["value"], entry["label"]
                        if mt == "icao" and val == icao:
                            hit, hit_label = True, lbl; break
                        elif mt == "registration" and val == (registration or "").lower():
                            hit, hit_label = True, lbl; break
                        elif mt == "callsign_prefix" and (callsign or "").lower().startswith(val):
                            hit, hit_label = True, lbl; break
                    if hit:
                        _pending.append(
                            ("wl", icao, registration, callsign, type_desc, aircraft_type,
                             distance_nm, hit_label, flight_id)
                        )

        # Close flights that have gone silent
        expired = [
            h for h, s in _active.items()
            if (now_epoch - s["last_seen"]) > config.FLIGHT_GAP_SEC
        ]
        for icao in expired:
            _close_flight(conn, icao)
        if expired:
            log.debug("Closed %d expired flight(s)", len(expired))

    # Enqueue alerts for the dispatch consumer.  Lazily start the consumer if
    # it isn't already running (production starts it in main(); tests may
    # invoke _poll without going through main()).
    if _pending:
        if _consumer_thread is None or not _consumer_thread.is_alive():
            start_notification_consumer()
        for item in _pending:
            _enqueue_alert(item)


# ---------------------------------------------------------------------------
# Purge old positions
# ---------------------------------------------------------------------------

def _purge(conn: sqlite3.Connection) -> None:
    if config.RETENTION_DAYS <= 0:
        return
    cutoff = int(time.time()) - config.RETENTION_DAYS * 86400

    # 1) Delete positions older than the cutoff in a single transaction.
    with conn:
        conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))

    # 2) BE-7 (Audit 2026-05-31): a flight that *crosses* the cutoff (first_seen
    #    before, last_seen at/after) keeps some positions and loses others, so
    #    every position-derived aggregate goes stale — not just total_positions.
    #    Recompute the full aggregate set from the surviving rows. The crossing
    #    set is tiny in steady state (prior purges already dropped flights whose
    #    last_seen < cutoff, so first_seen < cutoff only matches boundary-spanning
    #    flights), so the per-flight full-position fetch is cheap. positions has
    #    no per-row distance column, so max_distance_nm/bearing are recomputed in
    #    Python via geo.haversine_nm/bearing against the receiver location.
    crossing_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM flights WHERE first_seen < ? AND last_seen >= ? "
            "AND id NOT IN (SELECT flight_id FROM active_flights)",
            (cutoff, cutoff),
        ).fetchall()
    ]
    for fid in crossing_ids:
        prows = conn.execute(
            "SELECT lat, lon, alt_baro, gs, rssi, source_type "
            "FROM positions WHERE flight_id = ?",
            (fid,),
        ).fetchall()
        total = len(prows)
        if total == 0:
            # All positions purged — zero the count so step 4 can drop it.
            with conn:
                conn.execute(
                    "UPDATE flights SET total_positions = 0 WHERE id = ?", (fid,)
                )
            continue
        adsb = mlat = 0
        max_gs = max_alt = min_rssi = max_rssi = None
        lat_min = lat_max = lon_min = lon_max = None
        max_dist = max_bearing = None
        for p in prows:
            st = p["source_type"]
            if _is_adsb(st):
                adsb += 1
            elif _is_mlat(st):
                mlat += 1
            if p["gs"] is not None:
                max_gs = p["gs"] if max_gs is None else max(max_gs, p["gs"])
            if p["alt_baro"] is not None:
                max_alt = p["alt_baro"] if max_alt is None else max(max_alt, p["alt_baro"])
            if p["rssi"] is not None:
                min_rssi = p["rssi"] if min_rssi is None else min(min_rssi, p["rssi"])
                max_rssi = p["rssi"] if max_rssi is None else max(max_rssi, p["rssi"])
            lat_p, lon_p = p["lat"], p["lon"]
            if lat_p is not None and lon_p is not None:
                lat_min = lat_p if lat_min is None else min(lat_min, lat_p)
                lat_max = lat_p if lat_max is None else max(lat_max, lat_p)
                lon_min = lon_p if lon_min is None else min(lon_min, lon_p)
                lon_max = lon_p if lon_max is None else max(lon_max, lon_p)
                d = haversine_nm(config.RECEIVER_LAT, config.RECEIVER_LON, lat_p, lon_p)
                if max_dist is None or d > max_dist:
                    max_dist = d
                    max_bearing = geo.bearing(
                        config.RECEIVER_LAT, config.RECEIVER_LON, lat_p, lon_p
                    )
        primary = _primary_source(adsb, mlat, total)
        with conn:
            conn.execute(
                """
                UPDATE flights SET
                    total_positions = ?, adsb_positions = ?, mlat_positions = ?,
                    max_gs = ?, max_alt_baro = ?, min_rssi = ?, max_rssi = ?,
                    lat_min = ?, lat_max = ?, lon_min = ?, lon_max = ?,
                    max_distance_nm = ?, max_distance_bearing = ?, primary_source = ?
                WHERE id = ?
                """,
                (total, adsb, mlat, max_gs, max_alt, min_rssi, max_rssi,
                 lat_min, lat_max, lon_min, lon_max, max_dist, max_bearing,
                 primary, fid),
            )

    # 3) Audit-13 A13-013: previously the correlated COUNT(*) UPDATE
    #    inside `_purge` held the writer lock for minutes on a 200k+
    #    flights table. Batch the UPDATE so the lock is released
    #    between chunks and the watchdog/collector poll loop can
    #    interleave.
    batch_size = 500
    last_id = 0
    while True:
        ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT id FROM flights
                WHERE id > ?
                  AND last_seen < ?
                  AND id NOT IN (SELECT flight_id FROM active_flights)
                ORDER BY id
                LIMIT ?
                """,
                (last_id, cutoff, batch_size),
            ).fetchall()
        ]
        if not ids:
            break
        placeholders = ",".join("?" * len(ids))
        with conn:
            conn.execute(
                f"""
                UPDATE flights
                SET total_positions = (
                    SELECT COUNT(*) FROM positions WHERE flight_id = flights.id
                )
                WHERE id IN ({placeholders})
                """,
                ids,
            )
        last_id = ids[-1]

    # 4) Drop flights that now have too few positions to be worth keeping.
    with conn:
        conn.execute(
            """
            DELETE FROM flights
            WHERE total_positions < ?
              AND id NOT IN (SELECT flight_id FROM active_flights)
              AND last_seen < ?
            """,
            (config.MIN_POSITIONS_KEEP, cutoff),
        )
    log.info("Purge complete (cutoff epoch %d)", cutoff)


def _run_maintenance(conn: sqlite3.Connection) -> None:
    """Hourly DB maintenance: retention purge + planner-statistics refresh.
    `PRAGMA optimize` is the SQLite-recommended periodic call — it re-ANALYZEs
    only tables whose content changed enough to matter, with an internal
    row-sample cap, so it's cheap even on the Pi."""
    _purge(conn)
    conn.execute("PRAGMA optimize")


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _load_notified(conn: sqlite3.Connection) -> None:
    """Pre-fill _notified_icao from DB so restarts don't re-alert for known
    aircraft.  Includes military/interesting (via aircraft_db.flags) AND
    non-ICAO anonymous addresses (computed at query time, no DB column), so
    toggling RSBS_TELEGRAM_ANONYMOUS_ALERT after a restart doesn't trigger a
    flood of historical first-sighting alerts."""
    anon_sql = icao_ranges.anonymous_flag_sql("f.icao_hex", 1)
    # Audit-13 A13-034: bulk `set.update(generator)` rather than per-row
    # `set.add` — one C-level call instead of N Python-level loops. On a
    # 200 k-flight DB this trims ~50 ms off collector startup.
    # BE-4 (Audit 2026-05-31): OR-merge adsbx_overrides.flags too. A flight
    # flagged military/interesting ONLY via airplanes.live (no aircraft_db row)
    # would otherwise be missing from the dedupe set, so a restart re-alerts
    # for it. Mirrors the OR-merge already used by web.py's flag expressions.
    _notified_icao.update(
        row["icao_hex"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT f.icao_hex
            FROM flights f
            LEFT JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex
            LEFT JOIN adsbx_overrides axo ON axo.icao_hex = f.icao_hex
            WHERE ((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 3) != 0
               OR ({anon_sql}) != 0
            """
        )
    )
    log.info("Loaded %d previously-notified aircraft from DB", len(_notified_icao))


_summary_time_warned = False


def _parse_summary_time() -> tuple[int, int] | None:
    """Parse RSBS_SUMMARY_TIME into (hour, minute) or None if disabled/invalid."""
    global _summary_time_warned
    raw = config.TELEGRAM_SUMMARY_TIME.strip().lower()
    if raw in ("", "off"):
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        if not _summary_time_warned:
            log.warning("Invalid RSBS_SUMMARY_TIME=%r — expected HH:MM, "
                        "\"off\", or empty string to disable", config.TELEGRAM_SUMMARY_TIME)
            _summary_time_warned = True
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        if not _summary_time_warned:
            log.warning("Invalid RSBS_SUMMARY_TIME=%r — expected HH:MM, "
                        "\"off\", or empty string to disable", config.TELEGRAM_SUMMARY_TIME)
            _summary_time_warned = True
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        if not _summary_time_warned:
            log.warning("Invalid RSBS_SUMMARY_TIME=%r — hour must be 0-23, "
                        "minute must be 0-59", config.TELEGRAM_SUMMARY_TIME)
            _summary_time_warned = True
        return None
    return h, m


def _check_daily_summary(conn: sqlite3.Connection) -> None:
    global _last_summary_date
    if not notifier.telegram_enabled():
        return
    parsed = _parse_summary_time()
    if parsed is None:
        return
    h, m = parsed
    now = datetime.datetime.now()
    if now.hour == h and now.minute == m and now.date() != _last_summary_date:
        _last_summary_date = now.date()
        try:
            notifier.send_daily_summary(conn)
        except Exception:
            log.exception("Daily summary notification failed")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class StartupIntegrityError(RuntimeError):
    """Raised when the startup quick_check finds corruption or fails to run.

    Fatal for the collector: it alerts and exits without starting the poll
    loop, so a corrupt DB is never written to. The dirty sentinel is retained
    so the check repeats on the next boot until an operator recovers (see
    docs/operations.md).
    """


def _startup_integrity_check(conn: sqlite3.Connection, sentinel: pathlib.Path) -> None:
    """Run quick_check when previous shutdown was unclean.

    On a clean result: checkpoint the WAL and remove the sentinel. On
    corruption — or if quick_check itself fails to run — leave the sentinel in
    place and raise StartupIntegrityError so the caller fails closed instead of
    writing to a possibly-corrupt database.
    """
    log.warning("Unclean shutdown detected; running PRAGMA quick_check…")
    try:
        rows = conn.execute("PRAGMA quick_check(10)").fetchall()
    except Exception as exc:
        log.exception("quick_check failed to run")
        raise StartupIntegrityError("quick_check failed to run") from exc
    if len(rows) == 1 and rows[0][0] == "ok":
        log.info("DB integrity check passed; checkpointing WAL")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row and row[0] > 0:
            log.debug("WAL checkpoint partial — %d page(s) pending (busy readers)", row[0])
        sentinel.unlink(missing_ok=True)
        return
    issues = [r[0] for r in rows]
    log.critical(
        "DB CORRUPTION DETECTED after unclean shutdown (%d issue(s)): %s",
        len(issues), issues,
    )
    raise StartupIntegrityError(f"quick_check found {len(issues)} issue(s): {issues}")


def main() -> None:
    log.info(
        "Starting collector — DB: %s  source: %s  poll: %ds",
        config.DB_PATH, config.AIRCRAFT_JSON, config.POLL_INTERVAL_SEC,
    )

    _sentinel_existed = False
    try:
        _sentinel_existed = _SENTINEL.exists()
        _SENTINEL.touch()
    except OSError:
        log.warning("Cannot write sentinel file %s — skipping crash detection", _SENTINEL)

    database.init_db()
    conn = database.connect()

    if _sentinel_existed:
        try:
            _startup_integrity_check(conn, _SENTINEL)
        except StartupIntegrityError as exc:
            # Fail closed: do NOT load active flights, start background
            # threads, or enter the poll loop — writing to a corrupt DB would
            # compound the damage. Alert the operator and exit non-zero; the
            # sentinel is retained so the check repeats until recovery.
            alert = (
                "🛑 <b>readsbstats collector halted</b>\n"
                "DB integrity check failed after an unclean shutdown — refusing "
                "to start to avoid writing to a corrupt database.\n"
                f"<code>{notifier._h(str(exc))}</code>\n"
                "Recovery: restore from backup or run <code>sqlite3 .recover</code>, "
                "then delete the .dirty_shutdown sentinel (see docs/operations.md)."
            )
            try:
                notifier._send(alert)
            except Exception:
                log.exception("Failed to send DB integrity alert")
            _sd_notify("STATUS=DB integrity check failed — refusing to start")
            log.critical("Exiting (code 2): DB integrity check failed; sentinel retained")
            raise SystemExit(2)

    _load_active(conn)
    if notifier.telegram_enabled():
        _load_notified(conn)
        notifier.start_command_listener(config.DB_PATH)
    adsbx_enricher.start_background_enricher()
    # Audit 2026-06-01 W-3: route_enricher used to start in the web process,
    # making it a second writer alongside the collector. Move it here so the
    # single-writer model (collector owns SQLite writes) is restored.
    route_enricher.start_background_enricher()
    metrics_collector.start_metrics_collector()
    start_notification_consumer()
    _sd_notify("READY=1")

    # Heartbeat must be independent of _poll() — a write blocked on the
    # SQLite write lock during a background CREATE INDEX could otherwise
    # exceed WatchdogSec and have systemd kill the process.
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    threading.Thread(target=database.run_background_migrations, daemon=True).start()

    last_purge = time.time()

    while _running:
        t0 = time.time()
        try:
            _poll(conn)
        except Exception:
            log.exception("Poll error")

        if time.time() - last_purge >= config.PURGE_INTERVAL_SEC:
            try:
                _run_maintenance(conn)
            except Exception:
                log.exception("Purge error")
            last_purge = time.time()

        _check_daily_summary(conn)

        elapsed = time.time() - t0
        sleep_for = max(0.0, config.POLL_INTERVAL_SEC - elapsed)
        time.sleep(sleep_for)

    log.info("Finalising %d active flight(s)…", len(_active))
    try:
        with conn:
            for icao in list(_active):
                _close_flight(conn, icao)
    except Exception:
        log.exception("Error during shutdown finalisation")
    # Drain queued Telegram alerts before tearing down (audit-12 #145).
    try:
        stop_notification_consumer(timeout=5.0)
    except Exception:
        log.exception("Error stopping notification consumer")
    conn.close()
    try:
        _SENTINEL.unlink(missing_ok=True)
    except OSError:
        pass
    log.info("Collector stopped")


if __name__ == "__main__":
    main()
