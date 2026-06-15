"""Tests for the VDL2 read-only API + the optional-router gating in web.py."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from readsbstats import config, web
from readsbstats.api import _deps
from readsbstats.api import vdl2 as vdl2_api
from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_db, make_vdl2_db


@pytest.fixture(autouse=True)
def _clear_cache():
    # /api/vdl2/stats is cached module-globally; clear between tests so cached
    # values (or a cached 200 hiding a 503 path) don't leak across tests.
    from readsbstats import cache
    cache._cache.clear()
    yield


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

    def test_fts_search_multi_term_is_and(self, client):
        # "position report krakow" message exists; "clearance request" + "depart
        # EPWA gate 12" do not contain both terms.
        both = client.get("/api/vdl2/messages?q=report%20krakow").json()
        assert len(both["messages"]) == 1
        # AND across terms: no single message contains both 'gate' and 'krakow'.
        neither = client.get("/api/vdl2/messages?q=gate%20krakow").json()
        assert neither["messages"] == []

    def test_fts_search_special_chars_no_500(self, client):
        # Punctuation must not raise an FTS5 MATCH syntax error.
        r = client.get('/api/vdl2/messages?q=gate "12" (EPWA)')
        assert r.status_code == 200

    def test_raw_excluded_from_list(self, client):
        data = client.get("/api/vdl2/messages?limit=1").json()
        assert "raw" not in data["messages"][0]

    def test_short_query_fts_path_still_honored(self, client):
        # On an FTS build a 1-char `q` IS honored via MATCH (single-letter tokens
        # are valid FTS terms). The bodies share no single letter that matches
        # exactly one message, so just assert it filters (not the full feed) and
        # doesn't 500. 'k' appears in 'krakow' and 'clearance'/'gate' too — use a
        # letter present in exactly one body's tokens for a tight assertion: 'x'
        # appears in no seeded body, so MATCH 'x' → [].
        data = client.get("/api/vdl2/messages?q=x").json()
        assert data["messages"] == []   # 1-char MATCH honored, nothing matches 'x'


class TestNoFtsFallback:
    """BUG-4/F09: on a build/DB without FTS5, search falls back to LIKE. A
    too-short term (<2 chars) is a useless `LIKE '%x%'` full scan, so it's
    skipped — but the OLD code then appended NO predicate at all, returning the
    unfiltered newest-N feed. A too-short term on the no-FTS path must yield []."""

    @pytest.fixture()
    def nofts_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        _seed(conn)
        # Force the LIKE fallback: drop the FTS index so has_fts() is False,
        # exactly as on a SQLite build without FTS5.
        conn.execute("DROP TABLE IF EXISTS vdl2_fts")
        conn.commit()
        assert vdl2_db.has_fts(conn) is False
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            yield c
        conn.close()

    def test_baseline_no_fts_two_char_query_filters(self, nofts_client):
        # Sanity: a >=2-char term on the no-FTS path uses LIKE and filters.
        data = nofts_client.get("/api/vdl2/messages?q=krakow").json()
        assert len(data["messages"]) == 1
        assert "krakow" in data["messages"][0]["body"]

    def test_short_query_no_fts_returns_empty_not_full_feed(self, nofts_client):
        # The defect: a 1-char term on the no-FTS path returned the full feed
        # because neither predicate was appended. It must now return [].
        data = nofts_client.get("/api/vdl2/messages?q=a").json()
        assert data["messages"] == [], (
            "a too-short term on the no-FTS path must yield [], not the full feed"
        )
        # No `q` at all still returns the full feed (the guard is q-specific).
        allmsgs = nofts_client.get("/api/vdl2/messages").json()
        assert len(allmsgs["messages"]) == 3

    def test_short_query_no_fts_per_aircraft_returns_empty(self, nofts_client):
        # Same guard on the per-airframe endpoint (shares _query_messages).
        data = nofts_client.get("/api/vdl2/messages/48e95d?q=a").json()
        assert data["messages"] == []


class TestPerAircraft:
    def test_by_icao(self, client):
        data = client.get("/api/vdl2/messages/48e95d").json()
        assert len(data["messages"]) == 2
        assert all(m["icao_hex"] == "48e95d" for m in data["messages"])

    def test_bad_icao_404(self, client):
        assert client.get("/api/vdl2/messages/zzz").status_code == 404

    def test_since_until_window(self, client):
        # _seed inserts 48e95d messages at base-30 and base-10 (base≈now).
        now = int(time.time())
        data = client.get(f"/api/vdl2/messages/48e95d?since={now - 15}").json()
        assert len(data["messages"]) == 1  # only the base-10 message is within window
        none = client.get(f"/api/vdl2/messages/48e95d?until={now - 3600}").json()
        assert none["messages"] == []


class TestStatsExtended:
    def test_top_labels_airlines_hourly(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        now = int(time.time())
        # IATA-format flight ids (LO6550, LO0304) — must group on the 2-char
        # operator prefix, NOT substr(,1,3) which would split LO6/LO0.
        vdl2_db.insert_messages(vconn, [
            {"ts": now - 100, "icao_hex": "48e95d", "flight": "LO6550", "label": "H1", "body": "a"},
            {"ts": now - 200, "icao_hex": "48af11", "flight": "LO0304", "label": "H1", "body": "b"},
            {"ts": now - 300, "icao_hex": "48af11", "flight": "FR99X", "label": "Q0", "body": "c"},
            {"ts": now - 5 * 3600, "icao_hex": "48af11", "flight": "LO9999", "label": "H1", "body": "d"},
        ])
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        core = make_db()
        core.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES ('LOT', 'LOT Polish Airlines', 'LO', 'Poland', 1)"
        )
        core.commit()
        monkeypatch.setattr(_deps, "_db", core)

        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            data = c.get("/api/vdl2/stats").json()

        labels = {l["label"]: l["messages"] for l in data["top_labels"]}
        assert labels["H1"] == 3 and labels["Q0"] == 1
        airlines = {a["code"]: a for a in data["top_airlines"]}
        assert airlines["LO"]["messages"] == 3                  # LO6550+LO0304+LO9999, NOT fragmented
        assert airlines["LO"]["name"] == "LOT Polish Airlines"  # resolved via iata_code
        assert airlines["FR"]["name"] is None                   # unknown code → degrade
        assert len(data["hourly"]) == 24
        assert data["hourly"][23] == 3   # current hour (newest bucket) — must not be dropped
        assert data["hourly"][18] == 1   # ~5h ago bucket
        vconn.close()
        core.close()


class TestStats:
    def test_counts(self, client):
        data = client.get("/api/vdl2/stats").json()
        assert data["total"] == 3
        assert data["aircraft"] == 2
        assert data["last_hour"] == 3



class TestStatsOverlap:
    """The flights-overlap KPI runs on the CORE history.db connection with
    vdl2.db ATTACHed read-only (not the vdl2.db connection the rest of stats
    uses). File-based DBs: in-memory can't be cross-attached."""

    def _core_db(self, path):
        from readsbstats import database
        conn = database.connect(path, uri=True)
        conn.executescript(database.DDL)
        database._migrate(conn)
        return conn

    def _flight(self, conn, icao, first_seen, last_seen):
        conn.execute(
            "INSERT INTO flights (icao_hex, callsign, first_seen, last_seen) VALUES (?,?,?,?)",
            (icao, "LO1", first_seen, last_seen),
        )
        conn.commit()

    def test_overlap_pct_when_attached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vdl2_path = str(tmp_path / "vdl2.db")
        monkeypatch.setattr(config, "VDL2_DB_PATH", vdl2_path)
        now = int(time.time())
        vconn = vdl2_db.connect(vdl2_path)
        vdl2_db.ensure_schema(vconn)
        vdl2_db.insert_messages(vconn, [{"ts": now - 100, "icao_hex": "48e95d", "body": "x"}])
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)   # stats web_conn uses the file
        core = self._core_db(str(tmp_path / "history.db"))
        self._flight(core, "48e95d", now - 200, now - 50)   # has ACARS in window
        self._flight(core, "aabbcc", now - 200, now - 50)   # none
        _deps._maybe_attach_vdl2(core)
        assert _deps.vdl2_attached(core) is True
        monkeypatch.setattr(_deps, "_db", core)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            data = c.get("/api/vdl2/stats").json()
        assert data["flights_overlap_pct"] == 50.0
        vconn.close()
        core.close()

    def test_overlap_pct_null_when_not_attached(self, monkeypatch):
        # The default in-memory client fixture path: core conn has no vdl2db
        # attached → the KPI degrades to null rather than failing the card.
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())   # no ATTACH
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            data = c.get("/api/vdl2/stats").json()
        assert data["flights_overlap_pct"] is None
        vconn.close()


class TestMapOverlay:
    def _make(self, monkeypatch, rows):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        vdl2_db.insert_messages(vconn, rows)
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())
        app = FastAPI()
        web._include_optional_routers(app)
        return vconn, app

    def test_active_distinct_within_window(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 30,   "icao_hex": "48e95d", "body": "a"},
            {"ts": now - 40,   "icao_hex": "48e95d", "body": "a2"},   # dup airframe
            {"ts": now - 300,  "icao_hex": "48af11", "body": "b"},    # 5 min ago
            {"ts": now - 1200, "icao_hex": "48cc00", "body": "c"},    # 20 min ago
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/active?minutes=10").json()
        assert set(data["icao_hex"]) == {"48e95d", "48af11"}   # distinct, 48cc00 excluded
        assert data["count"] == 2
        vconn.close()

    def test_positions_only_with_coords_in_window(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 30,   "icao_hex": "48e95d", "lat": 52.1, "lon": 20.9, "label": "16", "body": "p1"},
            {"ts": now - 100,  "icao_hex": "48af11", "body": "no-pos"},               # no lat/lon
            {"ts": now - 200,  "icao_hex": "48cc00", "lat": 50.0, "lon": 19.9, "body": "p2"},
            {"ts": now - 7200, "icao_hex": "48dd00", "lat": 51.0, "lon": 21.0, "body": "old"},  # 2h ago
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        assert data["count"] == 2
        hexes = {p["icao_hex"] for p in data["points"]}
        assert hexes == {"48e95d", "48cc00"}
        p = next(p for p in data["points"] if p["icao_hex"] == "48e95d")
        assert p["lat"] == 52.1 and p["lon"] == 20.9 and p["label"] == "16"
        vconn.close()

    def test_positions_parses_precise_label16_body(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            # Label-16 AUTPOS: precise fix in the BODY, no lat/lon column.
            {"ts": now - 20, "icao_hex": "48e95d", "label": "16",
             "body": "WA921  ,N 52.166,E 020.772,4406, 251,2054, 72"},
            # Coarse XID column fix, no precise body.
            {"ts": now - 40, "icao_hex": "48af11", "lat": 52.1, "lon": 20.4, "body": "xid"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        by = {p["icao_hex"]: p for p in data["points"]}
        assert data["count"] == 2
        # precise body fix, parsed from the AUTPOS text
        assert abs(by["48e95d"]["lat"] - 52.166) < 1e-6
        assert abs(by["48e95d"]["lon"] - 20.772) < 1e-6
        assert by["48e95d"]["precise"] is True
        # coarse XID column fix, flagged not-precise
        assert by["48af11"]["lat"] == 52.1 and by["48af11"]["precise"] is False
        vconn.close()

    def test_positions_non_label16_body_not_precise(self, monkeypatch):
        # BE-006: a coarse XID row (has lat/lon columns, label != 16) whose body
        # happens to contain coordinate-looking text must NOT be marked precise,
        # and must return the trusted column fix — not the body text.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 20, "icao_hex": "48e95d", "label": "H1",
             "lat": 52.1, "lon": 20.4, "body": "free text N 52.166,E 020.772"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        assert data["count"] == 1
        p = data["points"][0]
        assert p["lat"] == 52.1 and p["lon"] == 20.4
        assert p["precise"] is False

    def test_positions_nofix_label16_does_not_starve_coarse(self, monkeypatch):
        # BE-007: a burst of no-fix Label-16 rows (newest) must not consume the cap
        # and starve an older valid coarse point. Two independent capped queries fix this.
        now = int(time.time())
        monkeypatch.setattr(vdl2_api, "_POSITIONS_CAP", 2)
        # Coarse row inserted FIRST (lowest id); the no-fix Label-16 burst has
        # higher ids, so under the old `ORDER BY id DESC LIMIT 2` it would crowd
        # the coarse row out entirely.
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 100, "icao_hex": "48cc00", "lat": 50.0, "lon": 19.9, "body": "xid"},
            {"ts": now - 5, "icao_hex": "48aa01", "label": "16", "body": "N   .    MMMM.MMM"},
            {"ts": now - 4, "icao_hex": "48aa02", "label": "16", "body": "no fix here"},
            {"ts": now - 3, "icao_hex": "48aa03", "label": "16", "body": "still no fix"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        hexes = {p["icao_hex"] for p in data["points"]}
        assert "48cc00" in hexes   # the valid coarse point survived the no-fix burst

    def test_positions_nofix_label16_does_not_starve_precise(self, monkeypatch):
        # BE-007-partial: newer no-fix Label-16 rows must not consume the cap and
        # starve an OLDER valid precise Label-16 fix. The label-16 candidate scan
        # over-fetches beyond _POSITIONS_CAP so parseable rows survive.
        now = int(time.time())
        monkeypatch.setattr(vdl2_api, "_POSITIONS_CAP", 2)
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 100, "icao_hex": "48ok01", "label": "16", "body": "N 52.166,E 020.772"},
            {"ts": now - 5, "icao_hex": "48bad1", "label": "16", "body": "no fix"},
            {"ts": now - 4, "icao_hex": "48bad2", "label": "16", "body": "N   .    MMMM.MMM"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        assert any(p["icao_hex"] == "48ok01" and p["precise"] is True for p in data["points"])
        vconn.close()

    def test_positions_sorted_newest_first(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "48aa01", "lat": 52.0, "lon": 20.0, "body": "x"},
            {"ts": now - 10, "icao_hex": "48aa02", "label": "16",
             "body": "N 52.166,E 020.772"},
            {"ts": now - 120, "icao_hex": "48aa03", "lat": 51.0, "lon": 19.0, "body": "y"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/positions?minutes=60").json()
        tss = [p["ts"] for p in data["points"]]
        assert tss == sorted(tss, reverse=True)
        assert "id" not in data["points"][0]   # internal id not leaked

    def test_overlay_empty(self, monkeypatch):
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get("/api/vdl2/active").json() == {"icao_hex": [], "count": 0}
            assert c.get("/api/vdl2/positions").json() == {"points": [], "count": 0}
        vconn.close()


class TestOooi:
    def _make(self, monkeypatch, rows):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        vdl2_db.insert_messages(vconn, rows)
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())
        app = FastAPI()
        web._include_optional_routers(app)
        return vconn, app

    def test_parses_latest_dep_and_arr(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 500, "icao_hex": "48e95d", "label": "H1",
             "body": "DEP / FI LO6550/AN SP-LYF/DA EPWA/DS EGLL/OT 0030"},
            {"ts": now - 100, "icao_hex": "48e95d", "label": "H1",
             "body": "ARR / FI LO6550/AN SP-LYF/DA EPWA/AD EGLL/ON 0210/IN 0218"},
            {"ts": now - 80, "icao_hex": "48e95d", "label": "H1", "body": "crew chat, no oooi"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48e95d").json()
        assert data["has_oooi"] is True
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["t_out"] == "0030"
        assert data["arr"]["dest_icao"] == "EGLL" and data["arr"]["t_in"] == "0218"
        vconn.close()

    def test_dsta_fallback_when_no_oooi(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "48af11", "label": "H1", "dsta": "EPWA",
             "body": "#DFB free text, not oooi"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48af11").json()
        assert data["has_oooi"] is False
        assert data["dsta"] == "EPWA"
        assert data["dep"] is None and data["arr"] is None
        vconn.close()

    def test_window_scopes_messages(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 5000, "icao_hex": "48e95d", "body": "DEP / DA EPWA/OT 0030"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/oooi/48e95d?since={now - 100}").json()
        assert data["has_oooi"] is False   # the only DEP is outside the window
        vconn.close()

    def test_bad_icao_404(self, monkeypatch):
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get("/api/vdl2/oooi/zzz").status_code == 404
        vconn.close()

    def test_qseries_synthesizes_dep_and_arr_events(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 400, "icao_hex": "4d2228", "label": "QP",
             "registration": "SP-RZB", "flight": "FR34BB", "body": "EPWAEGLL0930 192"},
            {"ts": now - 350, "icao_hex": "4d2228", "label": "QQ",
             "registration": "SP-RZB", "flight": "FR34BB", "body": "EPWAEGLL09420930"},
            {"ts": now - 120, "icao_hex": "4d2228", "label": "QR",
             "registration": "SP-RZB", "flight": "FR34BB", "body": "EPWAEGLL1150"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "QS",
             "registration": "SP-RZB", "flight": "FR34BB", "body": "EPWAEGLL1158  96"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["has_oooi"] is True
        assert data["dep"]["type"] == "DEP"
        assert data["dep"]["t_out"] == "0930" and data["dep"]["t_off"] == "0942"
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["dest_icao"] == "EGLL"
        assert data["dep"]["registration"] == "SP-RZB" and data["dep"]["flight"] == "FR34BB"
        assert data["arr"]["type"] == "ARR"
        assert data["arr"]["t_on"] == "1150" and data["arr"]["t_in"] == "1158"
        vconn.close()

    def test_qq_alone_fills_both_off_and_out(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "4d2228", "label": "QQ", "body": "EPMOGCTS11121059"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["has_oooi"] is True
        assert data["dep"]["t_off"] == "1112"
        assert data["dep"]["t_out"] == "1059"   # OUT echoed in the OFF report
        assert data["arr"] is None
        vconn.close()

    def test_qseries_single_phase_still_yields_event(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "4d2228", "label": "QP", "body": "EPMOLIRA1616 192"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["has_oooi"] is True
        assert data["dep"]["t_out"] == "1616" and data["dep"]["t_off"] is None
        assert data["arr"] is None
        vconn.close()

    def test_qseries_newest_per_phase_wins(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "QP", "body": "EPMOLIRA1601"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "QP", "body": "EPMOLIRA1616"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["t_out"] == "1616"
        vconn.close()

    def test_qseries_dominant_pair_excludes_other_leg(self, monkeypatch):
        # Full OOOI set for leg EPWA→EGLL plus a NEWER stray QP from the return
        # leg (fits inside the flight-window slack on quick turnarounds). The
        # dominant city pair must win or the card would show leg-2's t_out with
        # leg-1's t_on/t_in and a false ✗ route chip.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 500, "icao_hex": "4d2228", "label": "QP", "body": "EPWAEGLL0930"},
            {"ts": now - 450, "icao_hex": "4d2228", "label": "QQ", "body": "EPWAEGLL09420930"},
            {"ts": now - 200, "icao_hex": "4d2228", "label": "QR", "body": "EPWAEGLL1150"},
            {"ts": now - 150, "icao_hex": "4d2228", "label": "QS", "body": "EPWAEGLL1158"},
            {"ts": now - 30, "icao_hex": "4d2228", "label": "QP", "body": "EGLLEPWA1240"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["dest_icao"] == "EGLL"
        assert data["dep"]["t_out"] == "0930"   # NOT 1240 from the return leg
        assert data["arr"]["t_on"] == "1150" and data["arr"]["t_in"] == "1158"
        vconn.close()

    def test_qseries_lone_other_leg_partial_still_used(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 30, "icao_hex": "4d2228", "label": "QP", "body": "EGLLEPWA1240"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["t_out"] == "1240"
        assert data["dep"]["dep_icao"] == "EGLL"
        vconn.close()

    def test_tei_takes_precedence_over_qseries(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "H1",
             "body": "DEP / DA EPWA/DS EGLL/OT 0030"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "QP", "body": "EPWAEGLL0935"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["t_out"] == "0030"   # slash-TEI wins over synthetic
        vconn.close()

    def test_label49_fills_route_on_tei_event_missing_airports(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "H1", "body": "DEP / OT 0030"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "49",
             "body": "01DCAP    ETD159/090545OMAAEPWA"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["t_out"] == "0030"
        assert data["dep"]["dep_icao"] == "OMAA"
        assert data["dep"]["dest_icao"] == "EPWA"
        vconn.close()

    def test_label49_dsta_fallback_when_no_events(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "4d2228", "label": "49",
             "body": "01ICCL    LOT15K/111616EPWAKEWR"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["has_oooi"] is False
        assert data["dsta"] == "KEWR"   # destination from the label-49 city pair
        assert data["dep"] is None and data["arr"] is None
        vconn.close()

    def test_qseries_window_scoped(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 5000, "icao_hex": "4d2228", "label": "QP", "body": "EPMOLIRA1616"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/oooi/4d2228?since={now - 100}").json()
        assert data["has_oooi"] is False
        vconn.close()


class TestTimeseries:
    def _make(self, monkeypatch, rows):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        vdl2_db.insert_messages(vconn, rows)
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())
        app = FastAPI()
        web._include_optional_routers(app)
        return vconn, app

    def test_buckets_normalize_and_zero_fill(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 90, "icao_hex": "48e95d", "freq": 136.725, "body": "a"},
            {"ts": now - 80, "icao_hex": "48af11", "freq": 136.725, "body": "b"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/timeseries?from={now - 180}&to={now}").json()
        assert data["bucket_seconds"] == 60
        assert data["metrics"][0] == "rate"
        assert data["total"] == 2
        ts_col, rate_col = data["data"][0], data["data"][1]
        assert len(ts_col) == 3
        assert max(rate_col) == 2.0
        assert min(rate_col) == 0.0

    def test_top_freqs_capped_and_rate_counts_all(self, monkeypatch):
        now = int(time.time())
        rows = []
        for i, f in enumerate([136.700, 136.725, 136.775, 136.825, 136.875, 136.925, 136.975]):
            for _ in range(7 - i):
                rows.append({"ts": now - 100, "icao_hex": f"48{i:04x}", "freq": f, "body": "x"})
        vconn, app = self._make(monkeypatch, rows)
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/timeseries?from={now - 600}&to={now}").json()
        assert len(data["freqs"]) == 6
        assert data["freqs"][0] == 136.7
        assert 136.975 not in data["freqs"]
        assert data["total"] == sum(range(1, 8))

    def test_window_validation_and_503(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get(f"/api/vdl2/timeseries?from={now}&to={now}").status_code == 400
        vconn.close()

    def test_span_capped(self, monkeypatch):
        # An over-wide window must be rejected, not allocate a huge bucket grid.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get(f"/api/vdl2/timeseries?from=0&to={now}").status_code == 400
            # A 1-year window is within the cap.
            ok = c.get(f"/api/vdl2/timeseries?from={now - 366 * 86400 + 100}&to={now}")
            assert ok.status_code == 200
        vconn.close()

    def test_503_when_db_unavailable(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)

        def boom(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(vdl2_db, "web_conn", boom)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/timeseries").status_code == 503


class TestFailureModes:
    def test_endpoints_503_when_db_unavailable(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)

        def boom(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(vdl2_db, "web_conn", boom)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/messages").status_code == 503
            assert c.get("/api/vdl2/messages/48e95d").status_code == 503
            assert c.get("/api/vdl2/stats").status_code == 503
            assert c.get("/api/vdl2/active").status_code == 503
            assert c.get("/api/vdl2/positions").status_code == 503
            assert c.get("/api/vdl2/oooi/48e95d").status_code == 503

    def test_until_le_since_400(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/messages?since=100&until=100").status_code == 400
            assert c.get("/api/vdl2/messages/48e95d?since=200&until=100").status_code == 400
        conn.close()


class TestConnRegistry:
    def test_close_all_web_conns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))
        monkeypatch.setattr(vdl2_db, "_conn", None)
        monkeypatch.setattr(vdl2_db, "_thread_local", threading.local())
        with vdl2_db._web_conns_lock:
            vdl2_db._web_conns.clear()
        c = vdl2_db.web_conn()
        c.execute("SELECT 1")  # usable
        vdl2_db.close_all_web_conns()
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")  # closed


class TestHealth:
    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", False)
        assert vdl2_api.vdl2_health() == {"enabled": False, "available": False}

    def test_available(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [{"ts": 123, "icao_hex": "48e95d", "body": "x"}])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        monkeypatch.setattr(_deps, "_db", make_db())
        h = vdl2_api.vdl2_health()
        assert h["enabled"] is True and h["available"] is True
        assert h["messages"] == 1 and h["newest_ts"] == 123
        conn.close()


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


# ---------------------------------------------------------------------------
# Edge branches: helper functions, cache hits, degraded paths
# ---------------------------------------------------------------------------

class TestEdgeBranches:
    def test_fts_match_no_word_terms_returns_empty_phrase(self):
        # Pure punctuation tokenizes to nothing — the MATCH expr must stay a
        # valid (matches-nothing) phrase, not raise an FTS syntax error.
        assert vdl2_api._fts_match("!!! ???") == '""'

    def test_timeseries_bucket_spans(self):
        assert vdl2_api._timeseries_bucket(3_600) == 60
        assert vdl2_api._timeseries_bucket(604_800) == 300
        assert vdl2_api._timeseries_bucket(2_592_000) == 900
        assert vdl2_api._timeseries_bucket(7_776_000) == 3600
        assert vdl2_api._timeseries_bucket(50_000_000) == 14400

    def test_health_served_from_cache_on_second_call(self, client):
        h1 = vdl2_api.vdl2_health()
        assert vdl2_api.vdl2_health() is h1     # same cached object

    def test_health_attach_probe_error_degrades_to_false(self, client, monkeypatch):
        # history.db side down: available (vdl2 store) stays True, only the
        # attach bit degrades — the two bits are independent by contract.
        monkeypatch.setattr(
            _deps, "db",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("history down")))
        h = vdl2_api.vdl2_health()
        assert h["available"] is True
        assert h["attach_available"] is False

    def test_health_store_error_leaves_available_false(self, client, monkeypatch):
        class BrokenConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("broken store")

        monkeypatch.setattr(vdl2_db, "web_conn", lambda: BrokenConn())
        h = vdl2_api.vdl2_health()
        assert h["enabled"] is True
        assert h["available"] is False

    def test_search_falls_back_to_like_when_match_raises(self, client):
        # Build/DB skew: has_fts says yes but MATCH raises OperationalError —
        # the handler must degrade to the LIKE path, not 500.
        conn = vdl2_db._conn
        conn.execute("DROP TABLE IF EXISTS vdl2_fts")
        real_has_fts = vdl2_db.has_fts
        vdl2_db.has_fts = lambda c: True
        try:
            r = client.get("/api/vdl2/messages?q=krakow")
        finally:
            vdl2_db.has_fts = real_has_fts
        assert r.status_code == 200
        assert [m["body"] for m in r.json()["messages"]] == ["position report krakow"]

    def test_oooi_until_bounds_the_scan(self, client):
        base = int(time.time())
        conn = vdl2_db._conn
        vdl2_db.insert_messages(conn, [{
            "ts": base - 5, "icao_hex": "48e95d",
            "body": "ARR / FI JA401/AN CC-AWE/DA SPJC/AD SCEL/ON 0145/IN 0157",
        }])
        conn.commit()
        full = client.get("/api/vdl2/oooi/48e95d").json()
        assert full["arr"] is not None and full["has_oooi"] is True
        bounded = client.get(f"/api/vdl2/oooi/48e95d?until={base - 3600}").json()
        assert bounded["arr"] is None           # window excluded the ARR

    def test_stats_slow_query_logs_warning(self, client, monkeypatch, caplog):
        import itertools
        ticks = itertools.count(start=0, step=10)
        monkeypatch.setattr(vdl2_api.time, "perf_counter",
                            lambda: float(next(ticks)))
        with caplog.at_level("WARNING"):
            assert client.get("/api/vdl2/stats").status_code == 200
        assert any("vdl2 stats query slow" in rec.getMessage()
                   for rec in caplog.records)

    def test_timeseries_slow_query_logs_warning(self, client, monkeypatch, caplog):
        import itertools
        ticks = itertools.count(start=0, step=10)
        monkeypatch.setattr(vdl2_api.time, "perf_counter",
                            lambda: float(next(ticks)))
        with caplog.at_level("WARNING"):
            assert client.get("/api/vdl2/timeseries").status_code == 200
        assert any("vdl2 timeseries query slow" in rec.getMessage()
                   for rec in caplog.records)

    def test_flights_overlap_pct_none_on_core_db_error(self, client, monkeypatch):
        # Heaviest stats sub-query must degrade to None, never 503 the card.
        monkeypatch.setattr(
            _deps, "db",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("core down")))
        assert vdl2_api._flights_overlap_pct() is None

    def test_positions_skips_coarse_duplicate_of_precise(self, client):
        # One row with BOTH a parseable Label-16 AUTPOS body and XID lat/lon
        # columns must yield a single (precise) point, not a duplicate.
        base = int(time.time())
        conn = vdl2_db._conn
        vdl2_db.insert_messages(conn, [{
            "ts": base - 5, "icao_hex": "48e95d", "label": "16",
            "lat": 52.0, "lon": 20.5,
            "body": "WA921  ,N 52.166,E 020.772,4406, 251,2054, 72",
        }])
        conn.commit()
        data = client.get("/api/vdl2/positions?minutes=60").json()
        pts = [p for p in data["points"] if p["icao_hex"] == "48e95d"]
        assert len(pts) == 1
        assert pts[0]["precise"] is True
        assert abs(pts[0]["lat"] - 52.166) < 1e-6

    def test_active_and_positions_cache_hits(self, client):
        a1 = client.get("/api/vdl2/active").json()
        assert client.get("/api/vdl2/active").json() == a1
        p1 = client.get("/api/vdl2/positions").json()
        assert client.get("/api/vdl2/positions").json() == p1
        s1 = client.get("/api/vdl2/stats").json()
        assert client.get("/api/vdl2/stats").json() == s1

    def test_oooi_scan_breaks_early_when_complete(self, client):
        # Newest-first scan: once dep + arr + dsta are all found, the loop
        # must break instead of scanning the rest of the airframe's history.
        base = int(time.time())
        conn = vdl2_db._conn
        vdl2_db.insert_messages(conn, [
            {"ts": base - 30, "icao_hex": "48aaaa", "body": "old noise"},
            {"ts": base - 20, "icao_hex": "48aaaa",
             "body": "DEP / FI LO1/AN SP-ABC/DA EPWA/DS EGLL/OT 0030/OFF 0042"},
            {"ts": base - 10, "icao_hex": "48aaaa", "dsta": "EPWA",
             "body": "ARR / FI JA401/AN CC-AWE/DA SPJC/AD SCEL/ON 0145/IN 0157"},
        ])
        conn.commit()
        data = client.get("/api/vdl2/oooi/48aaaa").json()
        assert data["dep"] is not None
        assert data["arr"] is not None
        assert data["dsta"] == "EPWA"
        assert data["has_oooi"] is True

    def test_flights_overlap_pct_none_when_no_flights(self, tmp_path, monkeypatch):
        # Attach available but zero flights in the 24h window → None (the SPA
        # hides the overlap chip), not a divide-by-zero or 0.0.
        from readsbstats import database
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vdl2_path = str(tmp_path / "vdl2.db")
        monkeypatch.setattr(config, "VDL2_DB_PATH", vdl2_path)
        vconn = vdl2_db.connect(vdl2_path)
        vdl2_db.ensure_schema(vconn)
        vconn.commit()
        vconn.close()
        core = database.connect(str(tmp_path / "history.db"), uri=True)
        core.executescript(database.DDL)
        database._migrate(core)
        monkeypatch.setattr(_deps, "_db", core)
        assert vdl2_api._flights_overlap_pct() is None
        core.close()


class TestM1bposPositions:
    @pytest.fixture()
    def m1b_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            # #M1BPOS with a ddmmm fix: N52081 E020017 -> 52.135, 20.02833
            {"ts": base - 30, "icao_hex": "48ae21", "label": "H1",
             "body": "#M1BPOSN52081E020017,N51491E019372,191139,370,BOKSU,M51,19155"},
            # plain non-position H1 — must NOT yield a point
            {"ts": base - 25, "icao_hex": "48ae22", "label": "H1",
             "body": "#DFBABS011DA_S UAAAEPWA2"},
            # 59,G position sub-form (label 36) — yields a precise point at 52.15,20.59
            {"ts": base - 20, "icao_hex": "48ae23", "label": "36",
             "body": "59,G,0542,1,1,EPWA,52.15,20.59,52.15,20.61,10,269013,0,32.1,10586"},
            # 59,G status sub-form (label 37) — same prefix, NOT a position
            {"ts": base - 15, "icao_hex": "48ae24", "label": "37",
             "body": "59,G,EPGD,EPWA,33/-,,1,,0,,6,145,04,,"},
        ])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            yield c
        conn.close()

    def test_m1bpos_position_appears_as_precise_point(self, m1b_client):
        r = m1b_client.get("/api/vdl2/positions?minutes=60")
        assert r.status_code == 200
        pts = r.json()["points"]
        m1b = [p for p in pts if p["icao_hex"] == "48ae21"]
        assert len(m1b) == 1
        assert m1b[0]["precise"] is True
        assert m1b[0]["label"] == "H1"
        assert abs(m1b[0]["lat"] - 52.135) < 1e-6
        assert abs(m1b[0]["lon"] - 20.02833) < 1e-6

    def test_non_position_h1_yields_no_point(self, m1b_client):
        r = m1b_client.get("/api/vdl2/positions?minutes=60")
        assert all(p["icao_hex"] != "48ae22" for p in r.json()["points"])

    def test_59g_position_appears_as_precise_point(self, m1b_client):
        r = m1b_client.get("/api/vdl2/positions?minutes=60")
        pts = [p for p in r.json()["points"] if p["icao_hex"] == "48ae23"]
        assert len(pts) == 1
        assert pts[0]["precise"] is True
        assert abs(pts[0]["lat"] - 52.15) < 1e-6
        assert abs(pts[0]["lon"] - 20.59) < 1e-6

    def test_59g_status_subform_yields_no_point(self, m1b_client):
        r = m1b_client.get("/api/vdl2/positions?minutes=60")
        assert all(p["icao_hex"] != "48ae24" for p in r.json()["points"])


class TestFiledRoute:
    @pytest.fixture()
    def fr_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            {"ts": base - 40, "icao_hex": "48ae31", "label": "H1",
             "body": "#M1BPOSN52086E019235,WA903,042142,277,NORKU,052401,SONSA,M37/RP:DA:EPWA:AA:EHAM:CR:OFP537(27O)..NORKU:A:NORK2A:AP:ILS 27.ARTIP:F:VECTOR"},
            {"ts": base - 30, "icao_hex": "48ae32", "label": "Q0",
             "body": "clearance request"},
            {"ts": base - 35, "icao_hex": "48ae33", "label": "H1",
             "body": "#M1BPOSN52086E019235,WA903,042142,277,NORKU"},
            {"ts": base - 20, "icao_hex": "48ae34", "label": "H1",
             "body": "#T1BRTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF BCG59-U000-08E7 BCG38-0MFC-0017 L 1237 05JUN26"},
        ])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            yield c
        conn.close()

    def test_m1bpos_row_carries_filed_route(self, fr_client):
        msgs = fr_client.get("/api/vdl2/messages").json()["messages"]
        m1b = [m for m in msgs if m["icao_hex"] == "48ae31"][0]
        assert m1b["filed_route"]["dep"] == "EPWA"
        assert m1b["filed_route"]["arr"] == "EHAM"
        assert m1b["filed_route"]["star"] == "NORK2A"
        assert m1b["filed_route"]["approach"] == "ILS 27.ARTIP"

    def test_non_m1bpos_row_has_no_filed_route(self, fr_client):
        msgs = fr_client.get("/api/vdl2/messages").json()["messages"]
        other = [m for m in msgs if m["icao_hex"] == "48ae32"][0]
        assert "filed_route" not in other

    def test_m1bpos_without_route_omits_filed_route(self, fr_client):
        msgs = fr_client.get("/api/vdl2/messages").json()["messages"]
        row = next(m for m in msgs if m["icao_hex"] == "48ae33")
        assert "filed_route" not in row

    def test_rte_row_carries_filed_route(self, fr_client):
        msgs = fr_client.get("/api/vdl2/messages").json()["messages"]
        rte_row = next(m for m in msgs if m["icao_hex"] == "48ae34")
        assert rte_row["filed_route"]["dep"] == "EPWA"
        assert rte_row["filed_route"]["arr"] == "EDDF"
        assert rte_row["filed_route"]["company_route"] == "BCG59-U000-08E7 BCG38-0MFC-0017"
