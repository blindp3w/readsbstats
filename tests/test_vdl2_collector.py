"""Tests for the VDL2 ingest collector's datagram handling + end-to-end insert."""
from __future__ import annotations

import json

from readsbstats import config, vdl2_collector
from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_vdl2_db


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
