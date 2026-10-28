"""Interruptible, systemd-friendly ``PRAGMA quick_check`` for service startup.

Both ingest services (collector, vdl2_collector) verify the DB after an unclean
shutdown *before* sending ``READY=1``. On a multi-GB DB on a slow disk that
check takes minutes. Run inline it has two failure modes:

* systemd's ``TimeoutStartSec`` kills the unit mid-check, the dirty sentinel
  survives, and the next start repeats the same check forever;
* a Python signal handler cannot run while the main thread is inside the
  sqlite C call, so SIGTERM is ignored until systemd escalates to SIGKILL.

``run_quick_check`` runs the PRAGMA in a worker thread and keeps the main
thread free: it extends the start timeout (``EXTEND_TIMEOUT_USEC``) while the
check makes progress, stops extending after ``max_sec`` so a truly hung check
(dead disk) is still killed by systemd, and calls ``conn.interrupt()`` when a
shutdown is requested.
"""
from __future__ import annotations

import logging
import threading
import time

_EXTEND_USEC = 60_000_000   # each heartbeat pushes the start deadline 60 s out


def run_quick_check(conn, *, label: str, notify, stopping, heartbeat_sec: float = 10.0,
                    max_sec: float = 1800.0, clock=time.monotonic,
                    poll_sec: float = 1.0) -> list | None:
    """Run ``PRAGMA quick_check(10)`` on *conn* without blocking the main thread.

    *notify(msg)* sends an sd_notify message; *stopping()* reports a shutdown
    request. Returns the PRAGMA rows, or ``None`` if the check was interrupted
    by shutdown. Re-raises the PRAGMA's exception otherwise. *conn* must have
    been opened with ``check_same_thread=False``.
    """
    log = logging.getLogger(label)
    result: dict = {}

    def _worker() -> None:
        try:
            result["rows"] = conn.execute("PRAGMA quick_check(10)").fetchall()
        except BaseException as exc:   # re-raised in the caller's thread
            result["exc"] = exc

    t = threading.Thread(target=_worker, name=f"{label}-quick-check", daemon=True)
    start = clock()
    t.start()

    next_beat = 0.0
    interrupted = False
    warned = False
    while t.is_alive():
        elapsed = clock() - start
        if elapsed < max_sec:
            if elapsed >= next_beat:
                notify(f"EXTEND_TIMEOUT_USEC={_EXTEND_USEC}\n"
                       f"STATUS={label}: quick_check running, {int(elapsed)} s")
                next_beat = elapsed + heartbeat_sec
        elif not warned:
            warned = True
            log.warning("%s: quick_check exceeded %d s; no longer extending the start "
                        "timeout (systemd may kill the unit)", label, int(max_sec))
        if not interrupted and stopping():
            interrupted = True
            log.warning("%s: shutdown requested during quick_check; interrupting", label)
            try:
                conn.interrupt()
            except Exception:
                log.exception("%s: conn.interrupt() failed", label)
        t.join(poll_sec)   # short, so the main-thread signal handler runs promptly

    log.info("%s: quick_check finished in %d s", label, int(clock() - start))
    notify("STATUS=")
    if "exc" in result:
        if stopping():
            return None
        raise result["exc"]
    return result["rows"]
