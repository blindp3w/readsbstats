"""VDL2 ingest collector — standalone systemd service.

Run via ``python -m readsbstats.vdl2_collector`` (see
systemd/readsbstats-vdl2.service). **Consume-only**: the operator runs an
external decoder (dumpvdl2 recommended and the code default; set
``RSBS_VDL2_DECODER`` to match the decoder in use) and points it at this
listener with line-delimited JSON over UDP, e.g. with dumpvdl2::

    dumpvdl2 --output decoded:json:udp:address=127.0.0.1,port=5556 …

(the legacy vdlm2dec works too with ``RSBS_VDL2_DECODER=vdlm2dec``:
``vdlm2dec -g 14 -j 127.0.0.1:5556 136.725 136.775 136.875 136.975``).

This service binds ``RSBS_VDL2_UDP_HOST:RSBS_VDL2_UDP_PORT``, normalizes each
datagram into the separate ``vdl2.db`` (never touches ``history.db``), and
prunes rows past the retention window. It is a no-op (clean exit) when
``RSBS_VDL2_ENABLED`` is false, so the unit can be enabled but dormant.

Watchdog: ``WATCHDOG=1`` is emitted from its own thread (never inline in the
recv loop), mirroring collector.py, so a slow write can't starve the heartbeat.
"""
from __future__ import annotations

import json
import logging
import dataclasses
import os
import signal
import socket
import sqlite3
import sys
import threading
import time

from . import config
from .vdl2 import db as vdl2_db
from .vdl2.normalize import normalize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("vdl2")

_stop = threading.Event()   # set by signal handlers; threads wait on it
_WATCHDOG_INTERVAL_SEC = 20
_BATCH = 50              # flush after this many buffered records
_RECV_TIMEOUT_SEC = 1.0  # also the idle-flush / shutdown-responsiveness cadence
_MAX_DATAGRAM = 65535
_MAX_PENDING = 5000      # cap the retry buffer so a stuck DB can't grow it unboundedly
_SUMMARY_INTERVAL_SEC = 60
_DIALECT_WARN_DATAGRAMS = 200   # datagrams with zero normalized records ⇒ likely wrong RSBS_VDL2_DECODER


@dataclasses.dataclass
class _Counters:
    """Application-level ingest health (the systemd watchdog only proves the
    process is alive). Logged as a periodic summary so an operator can see
    'decoder running but DB empty' without enabling debug."""
    datagrams: int = 0
    records: int = 0
    malformed: int = 0
    committed: int = 0
    flush_failures: int = 0
    dropped: int = 0
    last_commit_ts: float = 0.0
    dialect_warned: bool = False   # one-time "decoder dialect mismatch?" warning already emitted


_stats = _Counters()


def _log_summary(pending_len: int) -> None:
    age = f"{int(time.time() - _stats.last_commit_ts)}" if _stats.last_commit_ts else "n/a"
    log.info(
        "vdl2 ingest: datagrams=%d records=%d malformed=%d committed=%d "
        "flush_failures=%d dropped=%d pending=%d last_commit_age_s=%s",
        _stats.datagrams, _stats.records, _stats.malformed, _stats.committed,
        _stats.flush_failures, _stats.dropped, pending_len, age,
    )
    # A decoder feeding the wrong JSON dialect yields datagrams that normalize to
    # nothing — silent otherwise. Warn once per process.
    if (not _stats.dialect_warned and _stats.records == 0
            and _stats.datagrams >= _DIALECT_WARN_DATAGRAMS):
        _stats.dialect_warned = True
        log.warning(
            "received %d datagrams but normalized 0 records — RSBS_VDL2_DECODER=%s "
            "may not match the decoder's JSON dialect",
            _stats.datagrams, config.VDL2_DECODER,
        )


def _sd_notify(msg: str) -> None:
    """Notify systemd (no-op outside systemd). Same wire protocol as collector.py."""
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


def _watchdog_loop() -> None:
    while not _stop.is_set():
        _sd_notify("WATCHDOG=1")
        _stop.wait(_WATCHDOG_INTERVAL_SEC)   # returns immediately once stop is set


def _prune_loop() -> None:
    """Retention prune on its OWN connection (writes from a separate thread must
    not share the ingest connection). Prunes once right at thread start (a service
    restarted more often than the interval would otherwise never prune), then every
    ``VDL2_PURGE_INTERVAL_SEC``. The connection is (re)opened inside the try so a
    transient open failure is logged and retried on the next tick instead of
    silently killing the thread. Waits on the stop event for responsive exit.

    The same thread builds the heavy stats indexes (``vdl2_db._DDL_HEAVY_INDEXES``)
    right after the first successful prune: sequential on one connection, so the two
    heavy writers never fight for the write lock (a long catch-up prune would starve
    CREATE INDEX past busy_timeout), and on the already-pruned table. A failed build
    is retried on the next tick; until it succeeds the stats queries stay slow."""
    conn = None
    indexes_ready = False
    try:
        while not _stop.is_set():
            try:
                if conn is None:
                    conn = vdl2_db.connect()
                removed = vdl2_db.prune(conn, config.VDL2_RETENTION_DAYS)
                if removed:
                    log.info("retention: pruned %d messages older than %d days",
                             removed, config.VDL2_RETENTION_DAYS)
                    conn.execute("PRAGMA optimize")   # refresh planner stats after a big delete
            except Exception as exc:
                log.warning("retention prune failed: %s", exc)
            if conn is not None and not indexes_ready:
                indexes_ready = _build_heavy_indexes(conn)
            if _stop.wait(max(1, config.VDL2_PURGE_INTERVAL_SEC)):
                break
    finally:
        if conn is not None:
            conn.close()


def _build_heavy_indexes(conn) -> bool:
    """One attempt at the heavy-index build; True once the indexes exist. A full
    table scan on a cold Pi, so it runs on the prune thread, never on startup."""
    try:
        log.info("vdl2: building heavy indexes in the background")
        t0 = time.monotonic()
        vdl2_db.build_heavy_indexes(conn)
        log.info("vdl2: heavy indexes ready (%.1f s)", time.monotonic() - t0)
        return True
    except Exception as exc:
        log.warning("vdl2: background index build failed (retrying next tick): %s", exc)
        return False


def _handle_datagram(data: bytes, pending: list[dict]) -> int:
    """Parse one UDP datagram (one or more newline-delimited JSON objects),
    normalize, and append records to ``pending``. Returns the number added.
    Malformed lines are counted (surfaced in the periodic summary, not logged
    per-line) — never crashes the loop: any exception from parsing or
    normalizing one line (incl. RecursionError on deeply nested JSON) drops just
    that line. Factored out for socket-free unit tests."""
    _stats.datagrams += 1
    added = 0
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = normalize(json.loads(line))
        except Exception as exc:   # untrusted UDP input: one bad line never escapes
            _stats.malformed += 1
            log.debug("dropping malformed datagram line: %s", type(exc).__name__)
            continue
        if rec is not None:
            pending.append(rec)
            added += 1
    _stats.records += added
    return added


def _shutdown(sig, frame) -> None:
    log.info("received signal %s, shutting down…", sig)
    _stop.set()


def _dirty_sentinel_path() -> str:
    d = os.path.dirname(os.path.abspath(config.VDL2_DB_PATH)) or "."
    return os.path.join(d, ".vdl2_dirty_shutdown")


def _quick_check(conn) -> bool:
    """PRAGMA quick_check(10) → True iff the DB reports 'ok'. Mirrors core ADR-0007."""
    try:
        rows = conn.execute("PRAGMA quick_check(10)").fetchall()
        return len(rows) == 1 and rows[0][0] == "ok"
    except sqlite3.Error as exc:
        log.error("vdl2 quick_check raised: %s", exc)
        return False


def main() -> int:
    if not config.VDL2_ENABLED:
        log.info("VDL2 disabled (RSBS_VDL2_ENABLED unset/false) — exiting")
        # Send READY so an accidentally enabled-but-disabled Type=notify unit
        # exits cleanly (inactive), not as a start protocol failure.
        _sd_notify("READY=1")
        return 0

    _stop.clear()   # allow re-entrant main() (tests / restarts within a process)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    conn = vdl2_db.connect()

    # Dirty-shutdown integrity gate (mirrors core history.db ADR-0007): a sentinel
    # present at startup means the previous run didn't stop cleanly, so verify
    # integrity BEFORE writing. On failure, refuse to write and fail the unit.
    sentinel = _dirty_sentinel_path()
    if os.path.exists(sentinel):
        log.warning("vdl2: unclean previous shutdown — running quick_check on %s", config.VDL2_DB_PATH)
        if not _quick_check(conn):
            log.critical("vdl2.db failed quick_check — refusing to write; investigate or restore")
            _sd_notify("STATUS=vdl2.db integrity check failed")
            conn.close()
            return 1
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    vdl2_db.ensure_schema(conn)
    if not vdl2_db.has_fts(conn):
        log.warning("FTS5 unavailable in this SQLite build — search will use LIKE fallback")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((config.VDL2_UDP_HOST, config.VDL2_UDP_PORT))
    except (OSError, OverflowError) as exc:
        # Bind BEFORE marking the run dirty: a port conflict or out-of-range
        # port (OverflowError, which the old `except OSError` missed) must not
        # leak the socket/connection or leave a dirty sentinel that forces an
        # unnecessary quick_check on the next start. audit 2026-06-15.
        log.error("cannot bind UDP %s:%d — %s", config.VDL2_UDP_HOST, config.VDL2_UDP_PORT, exc)
        sock.close()
        conn.close()
        return 1
    sock.settimeout(_RECV_TIMEOUT_SEC)

    try:
        open(sentinel, "w").close()   # mark this run in-progress (only after a clean bind)
    except OSError as exc:
        log.warning("vdl2: could not write dirty-shutdown sentinel: %s", exc)
    log.info("VDL2 ingest listening on udp://%s:%d (decoder=%s, db=%s)",
             config.VDL2_UDP_HOST, config.VDL2_UDP_PORT, config.VDL2_DECODER, config.VDL2_DB_PATH)

    threading.Thread(target=_watchdog_loop, name="vdl2-watchdog", daemon=True).start()
    threading.Thread(target=_prune_loop, name="vdl2-prune", daemon=True).start()
    _sd_notify("READY=1")

    pending: list[dict] = []
    total = 0
    last_summary = time.time()
    try:
        while not _stop.is_set():
            try:
                data, _addr = sock.recvfrom(_MAX_DATAGRAM)
            except socket.timeout:
                if pending:
                    total += _flush(conn, pending)
                if time.time() - last_summary >= _SUMMARY_INTERVAL_SEC:
                    _log_summary(len(pending))
                    last_summary = time.time()
                continue
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("recv error: %s", exc)
                continue
            _handle_datagram(data, pending)
            if len(pending) >= _BATCH:
                total += _flush(conn, pending)
    finally:
        total += _flush(conn, pending)   # count the final drain in the run total (BUG-12)
        sock.close()
        conn.close()
        # Clean shutdown: clear the sentinel so the next start skips quick_check.
        try:
            os.remove(sentinel)
        except OSError:
            pass
        _sd_notify("STOPPING=1")
        log.info("VDL2 ingest stopped (%d messages stored this run)", total)
    return 0


def _safe_rollback(conn) -> None:
    try:
        conn.rollback()
    except sqlite3.Error as exc:  # pragma: no cover - broken connection
        log.error("vdl2 rollback failed: %s", exc)


def _flush(conn, pending: list[dict]) -> int:
    """Write the buffered batch. Never raises — a failed write must not crash
    the recv loop (which would unwind into the finally-flush, re-raise on the
    same batch, and trip systemd's restart limit).

    - Transient lock contention (busy_timeout exceeded vs the prune thread):
      keep the batch and retry on the next flush, but cap the buffer so a
      persistently stuck DB can't grow it without bound.
    - Anything else (an unexpectedly un-bindable record): drop the batch so a
      single poison record can't wedge ingest forever."""
    if not pending:
        return 0
    try:
        n = vdl2_db.insert_messages(conn, pending)
        # Hide earlier blocks of multi-block messages whose final block just arrived;
        # same transaction, so a failure rolls back the inserts too.
        vdl2_db.mark_superseded(conn, pending)
        conn.commit()
    except sqlite3.OperationalError as exc:
        # rollback first: executemany may have inserted part of the batch into
        # the open transaction before raising; without rollback those partial
        # rows can persist on a later commit and (no UNIQUE constraint) the
        # retried batch would duplicate them. Guard the rollback too so a broken
        # connection (e.g. SQLITE_IOERR) can't make _flush raise — the docstring
        # promises it never does (a raise would crash the loop into a restart spin).
        _safe_rollback(conn)
        _stats.flush_failures += 1
        log.warning("vdl2 insert failed, will retry %d buffered messages: %s", len(pending), exc)
        if len(pending) > _MAX_PENDING:
            log.error("vdl2 pending buffer over %d — dropping %d messages", _MAX_PENDING, len(pending))
            _stats.dropped += len(pending)
            pending.clear()
        return 0
    except Exception as exc:
        _safe_rollback(conn)
        _stats.flush_failures += 1
        _stats.dropped += len(pending)
        log.error("vdl2 insert error, dropping %d messages: %s", len(pending), exc)
        pending.clear()
        return 0
    _stats.committed += n
    _stats.last_commit_ts = time.time()
    pending.clear()
    return n


if __name__ == "__main__":
    sys.exit(main())
