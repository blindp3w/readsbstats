"""Tests for the VDL2 ingest collector's datagram handling + end-to-end insert."""
from __future__ import annotations

import json
import socket

from readsbstats import config, vdl2_collector
from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_vdl2_db


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
