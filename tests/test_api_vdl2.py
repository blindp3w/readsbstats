"""Tests for the VDL2 read-only API + the optional-router gating in web.py."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from readsbstats import config, web
from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_vdl2_db


def _seed(conn):
    base = int(time.time())
    rows = [
        {"ts": base - 30, "icao_hex": "48e95d", "registration": "SP-LYF",
         "flight": "LO6550", "label": "H1", "body": "depart EPWA gate 12"},
        {"ts": base - 20, "icao_hex": "48af11", "registration": "SP-LVS",
         "flight": "LO0304", "label": "Q0", "body": "clearance request"},
        {"ts": base - 10, "icao_hex": "48e95d", "registration": "SP-LYF",
         "flight": "LO6550", "label": "H1", "body": "position report krakow"},
    ]
    vdl2_db.insert_messages(conn, rows)
    conn.commit()


@pytest.fixture()
def client(monkeypatch):
    """Fresh app with the VDL2 router included and an in-memory vdl2 conn injected."""
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    conn = make_vdl2_db()
    _seed(conn)
    monkeypatch.setattr(vdl2_db, "_conn", conn)
    app = FastAPI()
    web._include_optional_routers(app)
    with TestClient(app) as c:
        yield c
    conn.close()


class TestMessagesFeed:
    def test_newest_first(self, client):
        data = client.get("/api/vdl2/messages").json()
        bodies = [m["body"] for m in data["messages"]]
        assert bodies[0] == "position report krakow"   # most recent ts first
        assert len(data["messages"]) == 3

    def test_pagination_keyset(self, client):
        first = client.get("/api/vdl2/messages?limit=2").json()
        assert len(first["messages"]) == 2
        assert first["next_before_id"] is not None
        nxt = client.get(f"/api/vdl2/messages?limit=2&before_id={first['next_before_id']}").json()
        assert len(nxt["messages"]) == 1
        assert nxt["next_before_id"] is None

    def test_filter_by_label(self, client):
        data = client.get("/api/vdl2/messages?label=Q0").json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["flight"] == "LO0304"

    def test_filter_by_hex_prefix(self, client):
        data = client.get("/api/vdl2/messages?hex=48e9").json()
        assert {m["icao_hex"] for m in data["messages"]} == {"48e95d"}

    def test_filter_by_reg_prefix(self, client):
        data = client.get("/api/vdl2/messages?reg=SP-LV").json()
        assert all(m["registration"] == "SP-LVS" for m in data["messages"])

    def test_fts_search(self, client):
        data = client.get("/api/vdl2/messages?q=krakow").json()
        assert len(data["messages"]) == 1
        assert "krakow" in data["messages"][0]["body"]

    def test_fts_search_special_chars_no_500(self, client):
        # Punctuation must not raise an FTS5 MATCH syntax error.
        r = client.get('/api/vdl2/messages?q=gate "12" (EPWA)')
        assert r.status_code == 200

    def test_raw_excluded_from_list(self, client):
        data = client.get("/api/vdl2/messages?limit=1").json()
        assert "raw" not in data["messages"][0]


class TestPerAircraft:
    def test_by_icao(self, client):
        data = client.get("/api/vdl2/messages/48e95d").json()
        assert len(data["messages"]) == 2
        assert all(m["icao_hex"] == "48e95d" for m in data["messages"])

    def test_bad_icao_404(self, client):
        assert client.get("/api/vdl2/messages/zzz").status_code == 404


class TestStats:
    def test_counts(self, client):
        data = client.get("/api/vdl2/stats").json()
        assert data["total"] == 3
        assert data["aircraft"] == 2
        assert data["last_hour"] == 3


class TestGating:
    def test_router_absent_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", False)
        app = FastAPI()
        web._include_optional_routers(app)
        paths = {r.path for r in app.routes}
        assert not any(p.startswith("/api/vdl2") for p in paths)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/messages").status_code == 404

    def test_router_present_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        app = FastAPI()
        web._include_optional_routers(app)
        paths = {r.path for r in app.routes}
        assert "/api/vdl2/messages" in paths
