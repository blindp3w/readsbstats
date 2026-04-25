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
import signal
import socket
import sqlite3
import sys
import time

from . import adsbx_enricher, config, database, enrichment, geo, metrics_collector, notifier

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

# ICAOs already notified for mil/interesting (first-sighting); pre-loaded from DB on startup
_notified_icao: set[str] = set()
# flight_ids already notified for an emergency squawk
_squawk_notified: set[int] = set()
# date on which the last daily summary was sent
_last_summary_date: datetime.date | None = None

_running = True


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
    """Reload the in-memory active-flights dict from the DB table."""
    _active.clear()
    rows = conn.execute(
        """
        SELECT af.icao_hex, af.flight_id, af.last_seen,
               lp.lat, lp.lon, lp.ts AS pos_ts, lp.gs
        FROM active_flights af
        LEFT JOIN (
            SELECT flight_id, lat, lon, ts, gs,
                   ROW_NUMBER() OVER (PARTITION BY flight_id ORDER BY ts DESC) AS rn
            FROM positions
        ) lp ON lp.flight_id = af.flight_id AND lp.rn = 1
        """
    ).fetchall()
    for row in rows:
        _active[row["icao_hex"]] = {
            "flight_id": row["flight_id"],
            "last_seen": row["last_seen"],
            "last_pos_ts": row["pos_ts"] if row["pos_ts"] else row["last_seen"],
            "last_lat": row["lat"] if row["pos_ts"] else None,
            "last_lon": row["lon"] if row["pos_ts"] else None,
            "last_gs": row["gs"] if row["pos_ts"] else None,
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
        "last_pos_ts": pos_ts - 1,
        "last_lat": lat,
        "last_lon": lon,
        "last_gs": gs,
    }
    return flight_id


def _close_flight(conn: sqlite3.Connection, icao: str) -> None:
    """Finalise a flight: compute primary_source, delete if too few positions."""
    state = _active.pop(icao, None)
    if state is None:
        return
    flight_id = state["flight_id"]
    conn.execute("DELETE FROM active_flights WHERE icao_hex = ?", (icao,))

    row = conn.execute(
        "SELECT total_positions, adsb_positions, mlat_positions FROM flights WHERE id = ?",
        (flight_id,),
    ).fetchone()

    if row is None:
        return

    total = row["total_positions"]
    if total < config.MIN_POSITIONS_KEEP:
        # Keep flagged aircraft (military/interesting) even with few positions —
        # a single-position sighting at the edge of range is still valuable.
        _, _, _, flags, _ = _enrich(conn, icao, None, None)
        if not (flags & (config.FLAG_MILITARY | config.FLAG_INTERESTING)):
            conn.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
            return

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
) -> None:
    """Update aggregate columns on the flights row."""
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
            callsign, registration, aircraft_type, squawk,
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


def _poll(conn: sqlite3.Connection) -> None:
    data = _read_aircraft_json()
    if data is None:
        return

    ref_time = data.get("now", time.time())
    aircraft_list = data.get("aircraft", [])
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
            lat = ac.get("lat")
            lon = ac.get("lon")

            if lat is None or lon is None:
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue

            seen_pos = ac.get("seen_pos", 999)
            if seen_pos > config.MAX_SEEN_POS_SEC:
                continue

            pos_ts = int(ref_time - seen_pos)

            raw_hex = ac.get("hex", "")
            is_mlat_hex = raw_hex.startswith("~")
            icao = raw_hex.lstrip("~").lower()
            if not icao:
                continue

            source_type = ac.get("type")
            if is_mlat_hex and not source_type:
                source_type = "mlat"

            state = _active.get(icao)
            if state is not None and pos_ts <= state["last_pos_ts"]:
                continue

            # Raw fields from readsb
            callsign      = ac.get("flight", "").strip() or None
            if callsign and "@" in callsign:
                callsign = None
            registration  = ac.get("r") or None
            aircraft_type = ac.get("t") or None
            squawk        = ac.get("squawk") or None
            category      = ac.get("category") or None

            raw_alt  = ac.get("alt_baro")
            alt_baro = 0 if raw_alt == "ground" else (int(raw_alt) if raw_alt is not None else None)
            alt_geom  = ac.get("alt_geom")
            gs        = ac.get("gs")
            track     = ac.get("track")
            baro_rate = ac.get("baro_rate")
            rssi      = ac.get("rssi")
            messages  = ac.get("messages")

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
            # Queue notifications (sent after transaction commits)
            # ----------------------------------------------------------------
            if _tg:
                # Military / interesting: only on first-ever sighting of this ICAO.
                # Also fires mid-flight when ADSBx enricher confirms military status
                # (late-discovery: ADSBx polls every ~60s, may arrive after flight opens).
                if (flags & (config.FLAG_MILITARY | config.FLAG_INTERESTING)) and icao not in _notified_icao:
                    prev = conn.execute(
                        "SELECT COUNT(*) FROM flights WHERE icao_hex = ? AND id != ?",
                        (icao, flight_id),
                    ).fetchone()[0]
                    if prev == 0:
                        kind = "mil" if (flags & config.FLAG_MILITARY) else "int"
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

            # ----------------------------------------------------------------
            # Ghost-position filter: reject teleporting ADS-B outliers
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
                    dt_gs = pos_ts - state["last_pos_ts"]
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
            )
            _active[icao]["last_pos_ts"] = pos_ts
            _active[icao]["last_lat"] = lat
            _active[icao]["last_lon"] = lon
            if gs is not None:
                _active[icao]["last_gs"] = gs

        # Close flights that have gone silent
        expired = [
            h for h, s in _active.items()
            if (now_epoch - s["last_seen"]) > config.FLIGHT_GAP_SEC
        ]
        for icao in expired:
            _close_flight(conn, icao)
        if expired:
            log.debug("Closed %d expired flight(s)", len(expired))

    # Send queued notifications outside the DB transaction
    send_failed = 0
    for n in _pending:
        try:
            if n[0] == "mil":
                notifier.notify_military(n[1], n[2], n[3], n[4], n[5], n[6])
            elif n[0] == "int":
                notifier.notify_interesting(n[1], n[2], n[3], n[4], n[5], n[6])
            elif n[0] == "sqk":
                notifier.notify_squawk(n[1], n[2], n[3], n[4], n[5])
            elif n[0] == "wl":
                notifier.notify_watchlist(n[1], n[2], n[3], n[4], n[5], n[6], n[7], n[8])
        except Exception:
            send_failed += 1
    if send_failed:
        log.warning("Notification send failed for %d of %d queued alerts", send_failed, len(_pending))


# ---------------------------------------------------------------------------
# Purge old positions
# ---------------------------------------------------------------------------

def _purge(conn: sqlite3.Connection) -> None:
    if config.RETENTION_DAYS <= 0:
        return
    cutoff = int(time.time()) - config.RETENTION_DAYS * 86400
    with conn:
        conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
        conn.execute(
            """
            UPDATE flights
            SET total_positions = (
                SELECT COUNT(*) FROM positions WHERE flight_id = flights.id
            )
            WHERE last_seen < ?
              AND id NOT IN (SELECT flight_id FROM active_flights)
            """,
            (cutoff,),
        )
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


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _load_notified(conn: sqlite3.Connection) -> None:
    """Pre-fill _notified_icao from DB so restarts don't re-alert for known aircraft."""
    for row in conn.execute(
        """
        SELECT DISTINCT f.icao_hex
        FROM flights f
        JOIN aircraft_db adb ON adb.icao_hex = f.icao_hex
        WHERE COALESCE(adb.flags, 0) & 3 != 0
        """
    ):
        _notified_icao.add(row["icao_hex"])
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

def main() -> None:
    log.info(
        "Starting collector — DB: %s  source: %s  poll: %ds",
        config.DB_PATH, config.AIRCRAFT_JSON, config.POLL_INTERVAL_SEC,
    )
    database.init_db()
    conn = database.connect()
    _load_active(conn)
    if notifier.telegram_enabled():
        _load_notified(conn)
        notifier.start_command_listener(config.DB_PATH)
    adsbx_enricher.start_background_enricher()
    metrics_collector.start_metrics_collector()
    _sd_notify("READY=1")

    last_purge = time.time()

    while _running:
        t0 = time.time()
        try:
            _poll(conn)
        except Exception:
            log.exception("Poll error")
        _sd_notify("WATCHDOG=1")

        if time.time() - last_purge >= config.PURGE_INTERVAL_SEC:
            try:
                _purge(conn)
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
    conn.close()
    log.info("Collector stopped")


if __name__ == "__main__":
    main()
