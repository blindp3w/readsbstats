"""Tests for startup_check.run_quick_check: heartbeat, shutdown interrupt, error handling."""

import sqlite3
import threading
import time

import pytest

from readsbstats import startup_check


class BlockingConn:
    """execute() blocks until released (by release() or interrupt())."""

    def __init__(self, rows=(("ok",),), exc=None):
        self._rows = list(rows)
        self._exc = exc
        self._event = threading.Event()
        self.interrupts = 0
        self.started = threading.Event()

    def release(self):
        self._event.set()

    def interrupt(self):
        self.interrupts += 1
        self._interrupted = True
        self._event.set()

    def execute(self, sql):
        assert "quick_check" in sql
        self.started.set()
        self._event.wait(10)
        if getattr(self, "_interrupted", False):
            raise sqlite3.OperationalError("interrupted")
        if self._exc is not None:
            raise self._exc
        return self

    def fetchall(self):
        return self._rows


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def setup():
    yield


def _extends(msgs):
    return [m for m in msgs if m.startswith("EXTEND_TIMEOUT_USEC=")]


def test_slow_check_sends_heartbeats_and_returns_rows():
    conn = BlockingConn()
    msgs = []
    threading.Timer(0.4, conn.release).start()
    rows = startup_check.run_quick_check(
        conn, label="test", notify=msgs.append, stopping=lambda: False,
        heartbeat_sec=0.05, poll_sec=0.02,
    )
    assert rows == [("ok",)]
    assert len(_extends(msgs)) >= 2
    assert "EXTEND_TIMEOUT_USEC=60000000" in msgs[0]
    assert "STATUS=test: quick_check running" in msgs[0]
    assert conn.interrupts == 0


def test_final_status_cleared():
    conn = BlockingConn()
    conn.release()
    msgs = []
    startup_check.run_quick_check(
        conn, label="test", notify=msgs.append, stopping=lambda: False,
    )
    assert msgs[-1] == "STATUS="


def test_stopping_interrupts_and_returns_none():
    conn = BlockingConn()
    msgs = []
    flag = {"stop": False}
    threading.Timer(0.1, lambda: flag.update(stop=True)).start()
    rows = startup_check.run_quick_check(
        conn, label="test", notify=msgs.append, stopping=lambda: flag["stop"],
        heartbeat_sec=0.05, poll_sec=0.02,
    )
    assert rows is None
    assert conn.interrupts >= 1
    assert msgs[-1] == "STATUS="


def test_worker_exception_reraised():
    conn = BlockingConn(exc=sqlite3.DatabaseError("disk I/O error"))
    conn.release()
    with pytest.raises(sqlite3.DatabaseError, match="disk I/O error"):
        startup_check.run_quick_check(
            conn, label="test", notify=lambda m: None, stopping=lambda: False,
        )


def test_no_heartbeat_after_max_sec(caplog):
    """Past max_sec the main thread stops extending so systemd can kill a hung check."""
    conn = BlockingConn()
    clock = FakeClock()
    msgs = []

    def notify(m):
        msgs.append(m)
        if m.startswith("EXTEND_TIMEOUT_USEC="):
            clock.t += 100.0   # each heartbeat advances the fake clock

    # Release after the clock has run far past max_sec and several loops elapsed.
    def watcher():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if clock.t >= 300:
                time.sleep(0.3)
                break
            time.sleep(0.01)
        conn.release()

    threading.Thread(target=watcher, daemon=True).start()
    import logging
    with caplog.at_level(logging.WARNING):
        rows = startup_check.run_quick_check(
            conn, label="test", notify=notify, stopping=lambda: False,
            heartbeat_sec=0.0, max_sec=250.0, clock=clock, poll_sec=0.01,
        )
    assert rows == [("ok",)]
    # clock advanced 100 per heartbeat: heartbeats at t=0,100,200 only (< 250).
    assert len(_extends(msgs)) == 3
    assert sum("exceeded" in r.getMessage() for r in caplog.records) == 1


class SqliteLikeConn(BlockingConn):
    """Real sqlite3_interrupt() semantics: a no-op while no statement is
    running. The statement only starts after a short delay."""

    def interrupt(self):
        self.interrupts += 1
        if self.started.is_set():
            self._interrupted = True
            self._event.set()

    def execute(self, sql):
        time.sleep(0.2)   # e.g. still opening / rebuilding the WAL index
        return super().execute(sql)


def test_interrupt_before_statement_starts_is_retried():
    conn = SqliteLikeConn()
    calls = {"n": 0}

    def stopping():   # False at entry, True right after the worker starts
        calls["n"] += 1
        return calls["n"] > 1

    t0 = time.monotonic()
    rows = startup_check.run_quick_check(
        conn, label="test", notify=lambda m: None, stopping=stopping,
        heartbeat_sec=0.05, poll_sec=0.02,
    )
    assert rows is None
    assert conn.interrupts >= 2           # the first one hit no running statement
    assert time.monotonic() - t0 < 5


def test_stop_requested_before_start_skips_the_check():
    class NeverRun(BlockingConn):
        def execute(self, sql):
            self.ran = True
            raise AssertionError("check must not start once a stop is requested")

    conn = NeverRun()
    conn.ran = False
    msgs = []
    rows = startup_check.run_quick_check(
        conn, label="test", notify=msgs.append, stopping=lambda: True,
    )
    assert rows is None
    assert not _extends(msgs)
    assert conn.ran is False
