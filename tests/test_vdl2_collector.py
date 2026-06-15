"""Tests for the VDL2 ingest collector's datagram handling + end-to-end insert."""
from __future__ import annotations

import dataclasses
import json
import re
import signal
import socket
import sqlite3
import threading
import time

import pytest

from readsbstats import config, vdl2_collector
from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_vdl2_db


@pytest.fixture(autouse=True)
def setup():
    """Snapshot/restore the module-level counters and stop flag so tests that
    mutate them can't leak into each other."""
    saved = dataclasses.asdict(vdl2_collector._stats)
    yield
    vdl2_collector._stats.__dict__.update(saved)
    vdl2_collector._stop.clear()


def test_main_disabled_returns_0(monkeypatch):
    from readsbstats import config
    monkeypatch.setattr(config, "VDL2_ENABLED", False)
    # No socket bind, no DB writes; sends READY (no-op without NOTIFY_SOCKET) + exits 0.
    assert vdl2_collector.main() == 0


def test_quick_check_ok_on_healthy_db():
    conn = make_vdl2_db()
    assert vdl2_collector._quick_check(conn) is True


def test_handle_datagram_parses_and_appends():
    pending: list[dict] = []
    raw = {"timestamp": 1, "hex": "48E95D", "tail": "SP-LYF",
           "flight": "LO6550", "label": "H1", "text": "hi"}
    added = vdl2_collector._handle_datagram(json.dumps(raw).encode(), pending)
    assert added == 1
    assert pending[0]["icao_hex"] == "48e95d"


def test_handle_datagram_multiline():
    pending: list[dict] = []
    line1 = json.dumps({"timestamp": 1, "hex": "aaaaaa", "text": "a"})
    line2 = json.dumps({"timestamp": 2, "hex": "bbbbbb", "text": "b"})
    added = vdl2_collector._handle_datagram((line1 + "\n" + line2).encode(), pending)
    assert added == 2


def test_handle_datagram_drops_malformed():
    pending: list[dict] = []
    added = vdl2_collector._handle_datagram(b"{not valid json", pending)
    assert added == 0
    assert pending == []


def test_handle_datagram_drops_empty_record():
    pending: list[dict] = []
    # valid JSON but no identity/body → normalizer returns None
    added = vdl2_collector._handle_datagram(json.dumps({"freq": 136.9}).encode(), pending)
    assert added == 0


def test_flush_rolls_back_on_partial_failure():
    # A trigger aborts the 2nd row mid-executemany; without rollback the 1st row
    # would persist in the open transaction and commit later (and duplicate on
    # retry). _flush must roll back so NO rows remain.
    conn = make_vdl2_db()
    conn.execute(
        "CREATE TRIGGER poison BEFORE INSERT ON vdl2_messages "
        "WHEN NEW.body = 'POISON' BEGIN SELECT RAISE(ABORT, 'poison'); END"
    )
    pending = [
        {"ts": 1, "icao_hex": "aaaaaa", "body": "ok"},
        {"ts": 2, "icao_hex": "bbbbbb", "body": "POISON"},
    ]
    n = vdl2_collector._flush(conn, pending)
    assert n == 0
    assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 0  # rolled back


def test_flush_writes_to_db():
    conn = make_vdl2_db()
    pending: list[dict] = []
    vdl2_collector._handle_datagram(
        json.dumps({"timestamp": 1, "hex": "48e95d", "text": "warsaw"}).encode(), pending
    )
    n = vdl2_collector._flush(conn, pending)
    assert n == 1
    assert pending == []
    assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 1


def test_shutdown_drain_counts_toward_run_total(monkeypatch, tmp_path, caplog):
    """BUG-12: the final ``finally`` drain of a sub-BATCH pending buffer must be
    added to the run total. One datagram (< _BATCH) stays buffered until the
    shutdown flush, so the run-total log must report 1, not 0."""
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    # Keep the dirty-shutdown sentinel inside tmp_path (away from the real DB dir).
    monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))

    conn = make_vdl2_db()
    monkeypatch.setattr(vdl2_db, "connect", lambda *a, **k: conn)
    # Schema already built by make_vdl2_db; don't rebuild on the live connection.
    monkeypatch.setattr(vdl2_db, "ensure_schema", lambda c: None)
    # Don't let the prune thread touch our single in-memory connection.
    monkeypatch.setattr(vdl2_collector, "_prune_loop", lambda: None)

    datagram = json.dumps({"timestamp": 1, "hex": "48e95d", "text": "drain"}).encode()

    class _FakeSock:
        def __init__(self, *a, **k):
            self._calls = 0

        def bind(self, addr):
            pass

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            self._calls += 1
            if self._calls == 1:
                return datagram, ("127.0.0.1", 5556)
            # Second poll: ask the loop to stop, then break out via a non-timeout
            # OSError so the timeout idle-flush branch is skipped and the single
            # record survives to the shutdown finally-drain.
            vdl2_collector._stop.set()
            raise OSError("test shutdown")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _FakeSock())

    committed_before = vdl2_collector._stats.committed
    try:
        with caplog.at_level("INFO", logger="vdl2"):
            rc = vdl2_collector.main()
    finally:
        vdl2_collector._stop.clear()  # don't leak the stop flag into other tests

    assert rc == 0
    # The single buffered record was actually written by the shutdown drain...
    assert vdl2_collector._stats.committed - committed_before == 1
    # ...and the run total reflects it (would be 0 if finally discarded _flush()'s return).
    stored = [r.getMessage() for r in caplog.records if "messages stored this run" in r.getMessage()]
    assert stored, "expected a run-total shutdown log line"
    assert "(1 messages stored this run)" in stored[-1]


# ---------------------------------------------------------------------------
# Periodic summary
# ---------------------------------------------------------------------------

def test_log_summary_reports_counters_and_na_age(caplog):
    vdl2_collector._stats.last_commit_ts = 0.0
    with caplog.at_level("INFO", logger="vdl2"):
        vdl2_collector._log_summary(3)
    msg = caplog.records[-1].getMessage()
    assert "pending=3" in msg
    assert "last_commit_age_s=n/a" in msg


def test_log_summary_numeric_age_after_commit(caplog):
    vdl2_collector._stats.last_commit_ts = time.time()
    with caplog.at_level("INFO", logger="vdl2"):
        vdl2_collector._log_summary(0)
    msg = caplog.records[-1].getMessage()
    assert re.search(r"last_commit_age_s=\d+$", msg)


# ---------------------------------------------------------------------------
# sd_notify wire protocol
# ---------------------------------------------------------------------------

def test_sd_notify_sends_and_translates_abstract_addr(monkeypatch):
    # systemd abstract-namespace addresses start with "@" in the env var and
    # "\0" on the wire. A fake socket keeps this runnable on macOS (abstract
    # AF_UNIX sockets are Linux-only).
    sent = []
    closed = []

    class FakeSock:
        def sendto(self, data, addr):
            sent.append((data, addr))

        def close(self):
            closed.append(True)

    monkeypatch.setenv("NOTIFY_SOCKET", "@abstract")
    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSock())
    vdl2_collector._sd_notify("READY=1")
    assert sent == [(b"READY=1", "\0abstract")]
    assert closed == [True]


def test_sd_notify_noop_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    def boom(*a, **k):
        raise AssertionError("socket must not be created without NOTIFY_SOCKET")

    monkeypatch.setattr(socket, "socket", boom)
    vdl2_collector._sd_notify("READY=1")   # must not raise


# ---------------------------------------------------------------------------
# Retention prune loop
# ---------------------------------------------------------------------------

class _StopAfterOneWait(threading.Event):
    """wait() returns False once (run one loop iteration), then True (exit).
    Subclasses Event so set()/clear()/is_set() keep working for teardown."""
    def __init__(self):
        super().__init__()
        self._waits = 0

    def wait(self, timeout=None):
        self._waits += 1
        return self._waits > 1


def test_prune_loop_prunes_once_and_closes_conn(monkeypatch, caplog):
    conn = make_vdl2_db()
    closed = []

    class ClosingConn:
        def close(self):
            closed.append(True)
            conn.close()

        def __getattr__(self, name):
            return getattr(conn, name)

    prune_days = []
    monkeypatch.setattr(vdl2_db, "connect", lambda *a, **k: ClosingConn())
    monkeypatch.setattr(vdl2_db, "prune",
                        lambda c, days: prune_days.append(days) or 3)
    monkeypatch.setattr(vdl2_collector, "_stop", _StopAfterOneWait())
    with caplog.at_level("INFO", logger="vdl2"):
        vdl2_collector._prune_loop()
    assert prune_days == [config.VDL2_RETENTION_DAYS]
    assert closed == [True]
    assert any("retention: pruned 3 messages" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Datagram / shutdown / quick_check small branches
# ---------------------------------------------------------------------------

def test_handle_datagram_skips_blank_lines():
    pending: list[dict] = []
    malformed_before = vdl2_collector._stats.malformed
    line = json.dumps({"timestamp": 1, "hex": "48e95d", "text": "x"}).encode()
    added = vdl2_collector._handle_datagram(b"\n\n" + line + b"\n", pending)
    assert added == 1
    assert vdl2_collector._stats.malformed == malformed_before


def test_shutdown_sets_stop_event():
    assert not vdl2_collector._stop.is_set()
    vdl2_collector._shutdown(signal.SIGTERM, None)
    assert vdl2_collector._stop.is_set()


def test_quick_check_false_on_broken_connection(caplog):
    conn = make_vdl2_db()
    conn.close()
    with caplog.at_level("ERROR", logger="vdl2"):
        assert vdl2_collector._quick_check(conn) is False
    assert any("quick_check raised" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# main(): dirty-shutdown sentinel gate, bind failure, flush cadences
# ---------------------------------------------------------------------------

class _StopImmediatelySock:
    """First recvfrom stops the loop via a non-timeout OSError (the BUG-12
    pattern) so main() goes straight to the shutdown drain."""
    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def recvfrom(self, n):
        vdl2_collector._stop.set()
        raise OSError("test shutdown")

    def close(self):
        pass


def _scaffold_main(monkeypatch, tmp_path):
    """Common monkeypatch set for tests that run main() end-to-end: enabled
    config with the DB (and sentinel) inside tmp_path, one shared in-memory
    schema'd connection, no prune thread."""
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))
    conn = make_vdl2_db()
    monkeypatch.setattr(vdl2_db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(vdl2_db, "ensure_schema", lambda c: None)
    monkeypatch.setattr(vdl2_collector, "_prune_loop", lambda: None)
    return conn


def test_main_dirty_sentinel_clean_check_proceeds(monkeypatch, tmp_path, caplog):
    _scaffold_main(monkeypatch, tmp_path)
    sentinel = tmp_path / ".vdl2_dirty_shutdown"
    sentinel.touch()   # simulate an unclean previous shutdown
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _StopImmediatelySock())
    with caplog.at_level("WARNING", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 0
    assert any("unclean previous shutdown" in r.getMessage() for r in caplog.records)
    # Clean exit re-clears the sentinel so the next start skips quick_check.
    assert not sentinel.exists()


def test_main_dirty_sentinel_failed_check_refuses_to_write(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))
    conn = make_vdl2_db()
    monkeypatch.setattr(vdl2_db, "connect", lambda *a, **k: conn)
    schema_calls = []
    monkeypatch.setattr(vdl2_db, "ensure_schema", lambda c: schema_calls.append(1))
    monkeypatch.setattr(vdl2_collector, "_prune_loop", lambda: None)
    monkeypatch.setattr(vdl2_collector, "_quick_check", lambda c: False)
    sentinel = tmp_path / ".vdl2_dirty_shutdown"
    sentinel.touch()
    with caplog.at_level("CRITICAL", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 1
    assert schema_calls == []                  # refused to write anything
    assert sentinel.exists()                   # retained so the check repeats
    assert any("failed quick_check" in r.getMessage() for r in caplog.records)


def test_main_sentinel_write_failure_logs_and_continues(monkeypatch, tmp_path, caplog):
    _scaffold_main(monkeypatch, tmp_path)
    # Sentinel path under a missing directory: the startup open() raises
    # OSError (warn + continue) and the shutdown os.remove raises too (pass).
    monkeypatch.setattr(vdl2_collector, "_dirty_sentinel_path",
                        lambda: str(tmp_path / "nodir" / "sentinel"))
    # Piggyback: an FTS5-less SQLite build must only warn, never block ingest.
    monkeypatch.setattr(vdl2_db, "has_fts", lambda c: False)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _StopImmediatelySock())
    with caplog.at_level("WARNING", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 0
    assert any("could not write dirty-shutdown sentinel" in r.getMessage()
               for r in caplog.records)
    assert any("FTS5 unavailable" in r.getMessage() for r in caplog.records)


class _BindFailSock:
    """Socket whose bind() raises, to exercise main()'s bind-failure cleanup."""
    def __init__(self, exc):
        self._exc = exc
        self.closed = False

    def bind(self, addr):
        raise self._exc

    def settimeout(self, t):
        pass

    def recvfrom(self, n):  # pragma: no cover — never reached on a bind failure
        raise AssertionError("recv should not run after a bind failure")

    def close(self):
        self.closed = True


def test_main_bind_failure_cleans_up_and_returns_1(monkeypatch, tmp_path, caplog):
    # audit 2026-06-15: a bind failure must NOT leave a dirty sentinel (which
    # forces an unnecessary quick_check next start) or leak the socket.
    _scaffold_main(monkeypatch, tmp_path)
    sentinel = tmp_path / ".vdl2_dirty_shutdown"
    sock = _BindFailSock(OSError("address already in use"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: sock)
    with caplog.at_level("ERROR", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 1
    assert not sentinel.exists()   # bind failed before marking the run dirty
    assert sock.closed             # socket not leaked
    assert any("cannot bind" in r.getMessage() for r in caplog.records)


def test_main_bind_overflow_port_is_handled(monkeypatch, tmp_path):
    # An out-of-range RSBS_VDL2_UDP_PORT makes bind() raise OverflowError, which
    # the old `except OSError` did not catch → uncaught crash. Now handled.
    _scaffold_main(monkeypatch, tmp_path)
    sock = _BindFailSock(OverflowError("port must be 0-65535"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: sock)
    rc = vdl2_collector.main()
    assert rc == 1
    assert sock.closed


def test_main_bind_failure_returns_1(monkeypatch, tmp_path, caplog):
    _scaffold_main(monkeypatch, tmp_path)

    class BindFailSock:
        def bind(self, addr):
            raise OSError("address already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: BindFailSock())
    with caplog.at_level("ERROR", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 1
    assert any("cannot bind UDP" in r.getMessage() for r in caplog.records)


def test_main_idle_timeout_flushes_pending_and_logs_summary(monkeypatch, tmp_path, caplog):
    _scaffold_main(monkeypatch, tmp_path)
    monkeypatch.setattr(vdl2_collector, "_SUMMARY_INTERVAL_SEC", 0)
    datagram = json.dumps({"timestamp": 1, "hex": "48e95d", "text": "idle"}).encode()

    class IdleSock:
        def __init__(self):
            self.calls = 0

        def bind(self, addr):
            pass

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            self.calls += 1
            if self.calls == 1:
                return datagram, ("127.0.0.1", 5556)
            if self.calls == 2:
                # Idle tick: flushes the sub-batch pending buffer + summary.
                raise socket.timeout()
            vdl2_collector._stop.set()
            raise OSError("test shutdown")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: IdleSock())
    committed_before = vdl2_collector._stats.committed
    with caplog.at_level("INFO", logger="vdl2"):
        rc = vdl2_collector.main()
    assert rc == 0
    assert vdl2_collector._stats.committed - committed_before == 1
    summaries = [r.getMessage() for r in caplog.records if "vdl2 ingest:" in r.getMessage()]
    assert summaries, "expected an idle-tick summary line"
    assert "pending=0" in summaries[0]         # flushed before the summary


def test_main_flushes_when_batch_threshold_reached(monkeypatch, tmp_path):
    _scaffold_main(monkeypatch, tmp_path)
    monkeypatch.setattr(vdl2_collector, "_BATCH", 2)
    line1 = json.dumps({"timestamp": 1, "hex": "aaaaaa", "text": "a"})
    line2 = json.dumps({"timestamp": 2, "hex": "bbbbbb", "text": "b"})
    datagram = (line1 + "\n" + line2).encode()

    class TwoLineSock:
        def __init__(self):
            self.calls = 0

        def bind(self, addr):
            pass

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            self.calls += 1
            if self.calls == 1:
                return datagram, ("127.0.0.1", 5556)
            vdl2_collector._stop.set()
            raise OSError("test shutdown")

        def close(self):
            pass

    real_flush = vdl2_collector._flush
    flush_sizes = []

    def spy_flush(conn, pending):
        flush_sizes.append(len(pending))
        return real_flush(conn, pending)

    monkeypatch.setattr(socket, "socket", lambda *a, **k: TwoLineSock())
    monkeypatch.setattr(vdl2_collector, "_flush", spy_flush)
    committed_before = vdl2_collector._stats.committed
    rc = vdl2_collector.main()
    assert rc == 0
    # In-loop flush fired at the batch threshold (2 pending), so the shutdown
    # drain had nothing left.
    assert flush_sizes[0] == 2
    assert flush_sizes[-1] == 0
    assert vdl2_collector._stats.committed - committed_before == 2


# ---------------------------------------------------------------------------
# _flush failure modes
# ---------------------------------------------------------------------------

def test_flush_empty_pending_returns_zero():
    conn = make_vdl2_db()
    assert vdl2_collector._flush(conn, []) == 0


def test_flush_operational_error_retains_pending_for_retry(monkeypatch, caplog):
    conn = make_vdl2_db()

    def locked(c, p):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(vdl2_db, "insert_messages", locked)
    pending = [{"ts": 1, "icao_hex": "aaaaaa", "body": "a"},
               {"ts": 2, "icao_hex": "bbbbbb", "body": "b"}]
    failures_before = vdl2_collector._stats.flush_failures
    dropped_before = vdl2_collector._stats.dropped
    with caplog.at_level("WARNING", logger="vdl2"):
        n = vdl2_collector._flush(conn, pending)
    assert n == 0
    assert len(pending) == 2                   # retained for retry
    assert vdl2_collector._stats.flush_failures - failures_before == 1
    assert vdl2_collector._stats.dropped == dropped_before
    assert any("will retry 2 buffered messages" in r.getMessage()
               for r in caplog.records)


def test_flush_operational_error_drops_buffer_over_max_pending(monkeypatch, caplog):
    conn = make_vdl2_db()

    def locked(c, p):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(vdl2_db, "insert_messages", locked)
    monkeypatch.setattr(vdl2_collector, "_MAX_PENDING", 1)
    pending = [{"ts": 1, "icao_hex": "aaaaaa", "body": "a"},
               {"ts": 2, "icao_hex": "bbbbbb", "body": "b"}]
    dropped_before = vdl2_collector._stats.dropped
    with caplog.at_level("ERROR", logger="vdl2"):
        n = vdl2_collector._flush(conn, pending)
    assert n == 0
    assert pending == []                       # capped: buffer dropped
    assert vdl2_collector._stats.dropped - dropped_before == 2
    assert any("pending buffer over" in r.getMessage() for r in caplog.records)
