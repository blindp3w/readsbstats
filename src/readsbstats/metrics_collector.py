"""
readsbstats — receiver metrics collector.

Periodically reads ``/run/readsb/stats.json`` and stores 43 numeric metrics
in the ``receiver_stats`` table for time-series visualisation.

Runs as a background daemon thread inside the **collector** process.
Disabled by default; enable with ``RSBS_METRICS_ENABLED=1``.
"""

import json
import logging
import sqlite3
import threading
import time

from . import config, database

log = logging.getLogger("metrics_collector")

# Column names in INSERT order — must match the receiver_stats schema exactly.
_COLS: tuple[str, ...] = (
    "ac_with_pos", "ac_without_pos", "ac_adsb", "ac_mlat",
    "signal", "noise", "peak_signal", "strong_signals",
    "local_modes", "local_bad", "local_unknown_icao",
    "local_accepted_0", "local_accepted_1",
    "samples_dropped", "samples_lost",
    "messages", "positions_total", "positions_adsb", "positions_mlat",
    "max_distance_m",
    "tracks_new", "tracks_single",
    "cpu_demod", "cpu_reader", "cpu_background", "cpu_aircraft_json", "cpu_heatmap",
    "remote_modes", "remote_bad", "remote_accepted", "remote_bytes_in", "remote_bytes_out",
    "cpr_airborne", "cpr_global_ok", "cpr_global_bad", "cpr_global_range",
    "cpr_global_speed", "cpr_global_skipped",
    "cpr_local_ok", "cpr_local_range", "cpr_local_speed", "cpr_filtered",
    "altitude_suppressed",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO receiver_stats (ts, {', '.join(_COLS)}) "
    f"VALUES (?, {', '.join('?' for _ in _COLS)})"
)


# ---------------------------------------------------------------------------
# Helpers to safely dig into nested dicts / arrays
# ---------------------------------------------------------------------------

def _g(d: dict | None, *keys):
    """Traverse nested dicts returning None on any missing key."""
    for k in keys:
        if d is None or not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _ga(d: dict | None, key: str, index: int):
    """Get an element from an array inside a dict, or None."""
    arr = d.get(key) if isinstance(d, dict) else None
    if isinstance(arr, (list, tuple)) and len(arr) > index:
        return arr[index]
    return None


# ---------------------------------------------------------------------------
# Pure parsing — no I/O, easily unit-testable
# ---------------------------------------------------------------------------

def _parse_stats(data: dict) -> tuple[int | None, dict | None]:
    """
    Extract 43 metrics from a raw stats.json dict.

    Returns ``(timestamp, row_dict)`` where *timestamp* is the end-of-window
    epoch from ``last1min``.  Returns ``(None, None)`` if the ``last1min``
    section is missing.
    """
    last1 = data.get("last1min")
    if not isinstance(last1, dict):
        return None, None

    ts = last1.get("end")
    if ts is None:
        return None, None
    ts = int(ts)

    local  = last1.get("local") if isinstance(last1.get("local"), dict) else {}
    remote = last1.get("remote") if isinstance(last1.get("remote"), dict) else {}
    cpu    = last1.get("cpu") if isinstance(last1.get("cpu"), dict) else {}
    cpr    = last1.get("cpr") if isinstance(last1.get("cpr"), dict) else {}
    tracks = last1.get("tracks") if isinstance(last1.get("tracks"), dict) else {}
    actype = data.get("aircraft_count_by_type")
    actype = actype if isinstance(actype, dict) else {}
    pos_by_type = last1.get("position_count_by_type")
    pos_by_type = pos_by_type if isinstance(pos_by_type, dict) else {}

    row = {
        # Instantaneous (top-level)
        "ac_with_pos":       data.get("aircraft_with_pos"),
        "ac_without_pos":    data.get("aircraft_without_pos"),
        "ac_adsb":           actype.get("adsb_icao"),
        "ac_mlat":           actype.get("mlat"),
        # Local RF
        "signal":            local.get("signal"),
        "noise":             local.get("noise"),
        "peak_signal":       local.get("peak_signal"),
        "strong_signals":    local.get("strong_signals"),
        # Local decoder
        "local_modes":       local.get("modes"),
        "local_bad":         local.get("bad"),
        "local_unknown_icao": local.get("unknown_icao"),
        "local_accepted_0":  _ga(local, "accepted", 0),
        "local_accepted_1":  _ga(local, "accepted", 1),
        "samples_dropped":   local.get("samples_dropped"),
        "samples_lost":      local.get("samples_lost"),
        # Aggregate
        "messages":          last1.get("messages"),
        "positions_total":   last1.get("position_count_total"),
        "positions_adsb":    pos_by_type.get("adsb_icao"),
        "positions_mlat":    pos_by_type.get("mlat"),
        "max_distance_m":    last1.get("max_distance"),
        # Tracks
        "tracks_new":        tracks.get("all"),
        "tracks_single":     tracks.get("single_message"),
        # CPU (ms)
        "cpu_demod":         cpu.get("demod"),
        "cpu_reader":        cpu.get("reader"),
        "cpu_background":    cpu.get("background"),
        "cpu_aircraft_json": cpu.get("aircraft_json"),
        "cpu_heatmap":       cpu.get("heatmap_and_state"),
        # Remote / feed
        "remote_modes":      remote.get("modes"),
        "remote_bad":        remote.get("bad"),
        "remote_accepted":   _ga(remote, "accepted", 0),
        "remote_bytes_in":   remote.get("bytes_in"),
        "remote_bytes_out":  remote.get("bytes_out"),
        # CPR
        "cpr_airborne":      cpr.get("airborne"),
        "cpr_global_ok":     cpr.get("global_ok"),
        "cpr_global_bad":    cpr.get("global_bad"),
        "cpr_global_range":  cpr.get("global_range"),
        "cpr_global_speed":  cpr.get("global_speed"),
        "cpr_global_skipped": cpr.get("global_skipped"),
        "cpr_local_ok":      cpr.get("local_ok"),
        "cpr_local_range":   cpr.get("local_range"),
        "cpr_local_speed":   cpr.get("local_speed"),
        "cpr_filtered":      cpr.get("filtered"),
        # Misc
        "altitude_suppressed": last1.get("altitude_suppressed"),
    }
    return ts, row


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _read_stats_file(path: str) -> dict | None:
    """Read and parse stats.json.  Returns None on any error."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Cannot read %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _insert_row(conn: sqlite3.Connection, ts: int, row: dict) -> None:
    """INSERT OR IGNORE a single metrics row."""
    values = tuple(row.get(c) for c in _COLS)
    conn.execute(_INSERT_SQL, (ts, *values))
    conn.commit()


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

class _TransientError(Exception):
    """Raised on file-read failures; caller retries next cycle."""


def _poll_stats(conn: sqlite3.Connection, path: str) -> bool:
    """
    Read stats.json, parse, insert into receiver_stats.
    Returns True if a new row was inserted.
    """
    data = _read_stats_file(path)
    if data is None:
        raise _TransientError(f"cannot read {path}")

    ts, row = _parse_stats(data)
    if ts is None or row is None:
        log.debug("No last1min data in %s", path)
        return False

    _insert_row(conn, ts, row)
    return True


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def run_metrics_loop(db_path: str) -> None:
    """Entry point for the background thread.  Runs until process exits."""
    if not config.METRICS_ENABLED:
        log.info("Metrics collector disabled")
        return

    conn = database.connect(db_path)
    sleep_time = config.METRICS_INTERVAL

    while True:
        try:
            inserted = _poll_stats(conn, config.STATS_JSON)
            if inserted:
                log.debug("Metrics row inserted")
            sleep_time = config.METRICS_INTERVAL
        except _TransientError as exc:
            log.warning("Metrics poll failed (will retry): %s", exc)
            sleep_time = min(sleep_time * 2, 300)
        except Exception:
            log.exception("Metrics collector error")
            sleep_time = config.METRICS_INTERVAL
        time.sleep(sleep_time)


def start_metrics_collector() -> threading.Thread | None:
    """Start the metrics collector as a daemon thread.  Returns None if disabled."""
    if not config.METRICS_ENABLED:
        log.info("Metrics collector disabled (RSBS_METRICS_ENABLED not set)")
        return None
    t = threading.Thread(
        target=run_metrics_loop,
        args=(config.DB_PATH,),
        daemon=True,
        name="metrics-collector",
    )
    t.start()
    log.info(
        "Metrics collector started (source: %s, interval: %ds)",
        config.STATS_JSON, config.METRICS_INTERVAL,
    )
    return t
