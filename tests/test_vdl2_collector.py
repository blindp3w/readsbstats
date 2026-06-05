"""Tests for the VDL2 ingest collector's datagram handling + end-to-end insert."""
from __future__ import annotations

import json

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
