"""VDL2 ingest collector — standalone systemd service.

Run via ``python -m readsbstats.vdl2_collector`` (see
systemd/readsbstats-vdl2.service). **Consume-only**: the operator runs an
external decoder (vdlm2dec by default) and points it at this listener with
line-delimited JSON over UDP, e.g.::

    vdlm2dec -g 14 -j 127.0.0.1:5555 136.725 136.775 136.875 136.975

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

_running = True
_WATCHDOG_INTERVAL_SEC = 20
_BATCH = 50              # flush after this many buffered records
_RECV_TIMEOUT_SEC = 1.0  # also the idle-flush / shutdown-responsiveness cadence
_MAX_DATAGRAM = 65535
_MAX_PENDING = 5000      # cap the retry buffer so a stuck DB can't grow it unboundedly


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
    while _running:
        _sd_notify("WATCHDOG=1")
        for _ in range(_WATCHDOG_INTERVAL_SEC):
            if not _running:
                return
            time.sleep(1)


def _prune_loop() -> None:
    """Retention prune on its OWN connection (writes from a separate thread must
    not share the ingest connection). Sleeps in 1 s slices for responsive exit."""
    conn = vdl2_db.connect()
    try:
        while _running:
            for _ in range(max(1, config.VDL2_PURGE_INTERVAL_SEC)):
                if not _running:
                    return
                time.sleep(1)
            try:
                removed = vdl2_db.prune(conn, config.VDL2_RETENTION_DAYS)
                if removed:
                    log.info("retention: pruned %d messages older than %d days",
                             removed, config.VDL2_RETENTION_DAYS)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("retention prune failed: %s", exc)
    finally:
        conn.close()


def _handle_datagram(data: bytes, pending: list[dict]) -> int:
    """Parse one UDP datagram (one or more newline-delimited JSON objects),
    normalize, and append records to ``pending``. Returns the number added.
    Malformed lines are dropped (debug-logged) — never crashes the loop.
    Factored out so it can be unit-tested without a socket."""
    added = 0
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            log.debug("dropping malformed datagram line (%d bytes)", len(line))
            continue
        rec = normalize(raw)
        if rec is not None:
            pending.append(rec)
            added += 1
    return added


def _shutdown(sig, frame) -> None:
    global _running
    log.info("received signal %s, shutting down…", sig)
    _running = False


def main() -> int:
    if not config.VDL2_ENABLED:
        log.info("VDL2 disabled (RSBS_VDL2_ENABLED unset/false) — exiting")
        return 0

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    conn = vdl2_db.connect()
    vdl2_db.ensure_schema(conn)
    if not vdl2_db.has_fts(conn):
        log.warning("FTS5 unavailable in this SQLite build — search will use LIKE fallback")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((config.VDL2_UDP_HOST, config.VDL2_UDP_PORT))
    except OSError as exc:
        log.error("cannot bind UDP %s:%d — %s", config.VDL2_UDP_HOST, config.VDL2_UDP_PORT, exc)
        return 1
    sock.settimeout(_RECV_TIMEOUT_SEC)
    log.info("VDL2 ingest listening on udp://%s:%d (decoder=%s, db=%s)",
             config.VDL2_UDP_HOST, config.VDL2_UDP_PORT, config.VDL2_DECODER, config.VDL2_DB_PATH)

    threading.Thread(target=_watchdog_loop, name="vdl2-watchdog", daemon=True).start()
    threading.Thread(target=_prune_loop, name="vdl2-prune", daemon=True).start()
    _sd_notify("READY=1")

    pending: list[dict] = []
    total = 0
    try:
        while _running:
            try:
                data, _addr = sock.recvfrom(_MAX_DATAGRAM)
            except socket.timeout:
                if pending:
                    total += _flush(conn, pending)
                continue
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("recv error: %s", exc)
                continue
            _handle_datagram(data, pending)
            if len(pending) >= _BATCH:
                total += _flush(conn, pending)
    finally:
        _flush(conn, pending)
        sock.close()
        conn.close()
        _sd_notify("STOPPING=1")
        log.info("VDL2 ingest stopped (%d messages stored this run)", total)
    return 0


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
        conn.commit()
    except sqlite3.OperationalError as exc:
        log.warning("vdl2 insert failed, will retry %d buffered messages: %s", len(pending), exc)
        if len(pending) > _MAX_PENDING:
            log.error("vdl2 pending buffer over %d — dropping %d messages", _MAX_PENDING, len(pending))
            pending.clear()
        return 0
    except Exception as exc:
        log.error("vdl2 insert error, dropping %d messages: %s", len(pending), exc)
        pending.clear()
        return 0
    pending.clear()
    return n


if __name__ == "__main__":
    sys.exit(main())
