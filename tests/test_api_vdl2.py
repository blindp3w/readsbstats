"""Tests for the VDL2 read-only API + the optional-router gating in web.py."""
from __future__ import annotations

import re
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
from tests._helpers import dumpvdl2_record, iter_api_routes, make_db, make_vdl2_db


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


class TestFeedFilterSemantics:
    """hex/reg filters keep prefix, case-insensitive and literal-wildcard semantics
    after being rewritten into index-friendly forms."""

    @pytest.fixture()
    def conn(self, monkeypatch):
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            {"ts": base - 5, "icao_hex": "48e95d", "registration": "SP-LYF", "label": "H1", "body": "a"},
            {"ts": base - 4, "icao_hex": "48e95e", "registration": "sp-lyg", "label": "H1", "body": "b"},
            {"ts": base - 3, "icao_hex": "4a0001", "registration": "D_ABC", "label": "H1", "body": "c"},
            {"ts": base - 2, "icao_hex": "4a0002", "registration": "DXABC", "label": "H1", "body": "d"},
            {"ts": base - 1, "icao_hex": None, "registration": "50%OFF", "label": "H1", "body": "e"},
        ])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        yield conn
        conn.close()

    @staticmethod
    def _feed(**kw):
        args = dict(limit=100, before_id=None, label=None, hex_=None, reg=None,
                    since=None, until=None, q=None)
        args.update(kw)
        return vdl2_api._query_messages(**args)["messages"]

    def test_hex_prefix_lengths(self, conn):
        assert {m["icao_hex"] for m in self._feed(hex_="48")} == {"48e95d", "48e95e"}
        assert {m["icao_hex"] for m in self._feed(hex_="48e95")} == {"48e95d", "48e95e"}
        assert {m["icao_hex"] for m in self._feed(hex_="48e95d")} == {"48e95d"}
        assert {m["icao_hex"] for m in self._feed(hex_="4")} == {"48e95d", "48e95e", "4a0001", "4a0002"}

    def test_hex_is_case_insensitive(self, conn):
        assert {m["icao_hex"] for m in self._feed(hex_="48E95D")} == {"48e95d"}
        assert {m["icao_hex"] for m in self._feed(hex_="4A00")} == {"4a0001", "4a0002"}

    def test_hex_non_hex_input_matches_nothing_without_error(self, conn):
        assert self._feed(hex_="zz") == []
        assert self._feed(hex_="48e95d1") == []          # longer than an address
        assert self._feed(hex_="4_") == []               # '_' is literal, not a wildcard

    def test_reg_prefix_is_case_insensitive(self, conn):
        assert {m["registration"] for m in self._feed(reg="sp-ly")} == {"SP-LYF", "sp-lyg"}
        assert {m["registration"] for m in self._feed(reg="SP-LYG")} == {"sp-lyg"}

    def test_reg_wildcard_characters_match_literally(self, conn):
        assert {m["registration"] for m in self._feed(reg="D_")} == {"D_ABC"}   # not DXABC
        assert {m["registration"] for m in self._feed(reg="50%")} == {"50%OFF"}


class TestFeedFilterUsesIndex:
    """VM-step budget: the old `LIKE ? ESCAPE` forms full-scan the table even though
    an index exists; the rewritten hex/reg filters must seek (a few hundred steps
    vs tens of thousands for the 6000-row scan)."""

    @pytest.fixture()
    def conn(self, monkeypatch):
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            {"ts": base - i, "icao_hex": f"{i:06x}", "registration": f"SP-{i:04d}",
             "label": "H1", "body": "x"} for i in range(6000)])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        yield conn
        conn.close()

    @staticmethod
    def _steps(conn, **kw):
        n = [0]

        def tick():
            n[0] += 1
            return 0
        args = dict(limit=100, before_id=None, label=None, hex_=None, reg=None,
                    since=None, until=None, q=None)
        args.update(kw)
        conn.set_progress_handler(tick, 1)
        try:
            msgs = vdl2_api._query_messages(**args)["messages"]
        finally:
            conn.set_progress_handler(None, 0)
        return n[0], len(msgs)

    def test_full_hex_seeks(self, conn):
        steps, found = self._steps(conn, hex_="00012b")
        assert found == 1 and steps < 1000

    def test_prefix_hex_seeks(self, conn):
        steps, found = self._steps(conn, hex_="0001a")
        assert found == 16 and steps < 3000

    def test_reg_prefix_seeks(self, conn):
        steps, found = self._steps(conn, reg="sp-001")
        assert found == 10 and steps < 1000


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



class TestStatsSingleFlight:
    def test_concurrent_cache_misses_share_one_compute(self, monkeypatch):
        calls = []
        gate = threading.Event()

        def slow_compute():
            calls.append(1)
            gate.wait(5)
            return {"total": 7}

        monkeypatch.setattr(vdl2_api, "_compute_stats", slow_compute)
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(vdl2_api.api_vdl2_stats(summary=False)))
            for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.005)
        time.sleep(0.1)          # let the second thread reach the lock
        gate.set()
        for t in threads:
            t.join(5)
        assert len(calls) == 1
        assert results == [{"total": 7}, {"total": 7}]

    def test_summary_misses_share_one_compute(self, monkeypatch):
        calls = []
        gate = threading.Event()

        def slow_summary():
            calls.append(1)
            gate.wait(5)
            return {"total": 1, "last_hour": 1, "aircraft": 1}

        monkeypatch.setattr(vdl2_api, "_compute_stats_summary", slow_summary)
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(vdl2_api.api_vdl2_stats(summary=True)))
            for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.005)
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(5)
        assert len(calls) == 1 and len(results) == 2


class TestStatsSummary:
    def test_summary_returns_exactly_the_headline_keys(self, client):
        data = client.get("/api/vdl2/stats?summary=true").json()
        assert set(data) == {"total", "last_hour", "aircraft"}
        assert data == {"total": 3, "last_hour": 3, "aircraft": 2}

    def test_full_stats_unchanged(self, client):
        data = client.get("/api/vdl2/stats").json()
        assert {"top_labels", "top_airlines", "hourly", "flights_overlap_pct"} <= set(data)

    def test_summary_does_not_run_the_full_compute(self, client, monkeypatch):
        def boom():
            raise AssertionError("full stats must not run for summary=true")
        monkeypatch.setattr(vdl2_api, "_compute_stats", boom)
        assert client.get("/api/vdl2/stats?summary=true").status_code == 200

    def test_summary_cached_under_its_own_key(self, client, monkeypatch):
        from readsbstats import cache
        client.get("/api/vdl2/stats?summary=true")
        assert cache._get_cache("vdl2-stats-summary") is not None
        assert cache._get_cache("vdl2-stats") is None     # full not computed/cached
        monkeypatch.setattr(vdl2_api, "_compute_stats_summary",
                            lambda: pytest.fail("second call must hit the cache"))
        assert client.get("/api/vdl2/stats?summary=true").status_code == 200

    def test_summary_served_from_cached_full_stats(self, client, monkeypatch):
        full = client.get("/api/vdl2/stats").json()
        monkeypatch.setattr(vdl2_api, "_compute_stats_summary",
                            lambda: pytest.fail("should reuse the cached full stats"))
        data = client.get("/api/vdl2/stats?summary=true").json()
        assert data == {k: full[k] for k in ("total", "last_hour", "aircraft")}


class TestStatsHeavyIndexPlans:
    """With the background-built indexes the two table-wide aggregates are
    covering-index scans (no per-row table lookup / full table scan)."""

    @staticmethod
    def _plan(conn, sql):
        return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))

    def test_top_labels_and_airlines_use_covering_indexes(self):
        conn = make_vdl2_db()
        vdl2_db.build_heavy_indexes(conn)
        labels = self._plan(conn, vdl2_api._TOP_LABELS_SQL)
        airlines = self._plan(conn, vdl2_api._TOP_AIRLINES_SQL)
        assert "COVERING INDEX idx_vdl2_label_icao" in labels
        assert "COVERING INDEX idx_vdl2_flight" in airlines

    def test_airlines_cannot_use_the_flight_index_before_it_is_built(self):
        conn = make_vdl2_db()      # migrate() deliberately does not build it
        assert "idx_vdl2_flight" not in self._plan(conn, vdl2_api._TOP_AIRLINES_SQL)


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


    def test_cm_logon_only_gives_route_and_dsta(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "48c132", "label": "CM", "dsta": "LATI",
             "body": "LOGON RYR8414 EPMO-LATI"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48c132").json()
        assert data["has_oooi"] is False
        assert data["dep"] is None and data["arr"] is None
        assert data["dsta"] == "LATI"
        assert data["route"] == {"dep_icao": "EPMO", "dest_icao": "LATI",
                                 "flight": "RYR8414", "source": "cm"}
        vconn.close()

    def test_cm_dsta_fallback_when_row_dsta_null(self, monkeypatch):
        # A pre-backfill CM row has body but NULL dsta: the route still supplies it.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "48c132", "label": "CM", "body": "LOGON RYR8414 EPMO-LATI"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48c132").json()
        assert data["dsta"] == "LATI"
        vconn.close()

    def test_cm_beats_label49(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "CM", "body": "LOGON RYR8414 EPMO-LATI"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "49",
             "body": "01DCAP    ETD159/090545OMAAEPWA"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["route"]["source"] == "cm"
        assert (data["route"]["dep_icao"], data["route"]["dest_icao"]) == ("EPMO", "LATI")
        vconn.close()

    def test_newest_cm_logon_wins(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 600, "icao_hex": "4d2228", "label": "CM", "body": "LOGON RYR1 EGLL-EPWA"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "CM", "body": "LOGON RYR2 EPWA-EGLL"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["route"]["flight"] == "RYR2"
        vconn.close()

    def test_label49_only_route_has_both_ends(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "4d2228", "label": "49",
             "body": "01ICCL    LOT15K/111616EPWAKEWR"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["route"] == {"dep_icao": "EPWA", "dest_icao": "KEWR",
                                 "flight": "LOT15K", "source": "label49"}
        assert data["dsta"] == "KEWR"
        vconn.close()

    def test_label26_only_route_and_dsta(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert data["has_oooi"] is False
        assert data["dsta"] == "LFPG"
        assert data["route"] == {"dep_icao": "EPWA", "dest_icao": "LFPG",
                                 "flight": "AF1147", "source": "label26"}
        vconn.close()

    def test_label20_rst_only_route_and_dsta(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "484b2a", "label": "20",
             "body": "RST53A27145652KLM49N  EHAMEPWA"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/484b2a").json()
        assert data["dsta"] == "EPWA"
        assert data["route"] == {"dep_icao": "EHAM", "dest_icao": "EPWA",
                                 "flight": "KLM49N", "source": "label20"}
        vconn.close()

    def test_label20_non_route_bodies_give_no_route(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "484b2a", "label": "20",
             "body": "POSN52117E020255,,121107,075,"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/484b2a").json()
        assert "route" not in data
        vconn.close()

    def test_cm_beats_label26(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "3944e0", "label": "CM", "body": "LOGON AFR1147 EPWA-LFPG"},
            {"ts": now - 60, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert data["route"]["source"] == "cm"
        assert data["route"]["flight"] == "AFR1147"
        vconn.close()

    def test_label26_beats_label49(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
            {"ts": now - 60, "icao_hex": "3944e0", "label": "49",
             "body": "01DCAP    ETD159/090545OMAAEPWA"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert data["route"]["source"] == "label26"
        assert data["dsta"] == "LFPG"
        vconn.close()

    def test_label20_beats_label49_and_loses_to_label26(self, monkeypatch):
        now = int(time.time())
        rows = [
            {"ts": now - 300, "icao_hex": "484b2a", "label": "20",
             "body": "RST53A27145652KLM49N  EHAMEPWA"},
            {"ts": now - 200, "icao_hex": "484b2a", "label": "49",
             "body": "01DCAP    ETD159/090545OMAAEPWA"},
        ]
        vconn, app = self._make(monkeypatch, rows)
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/484b2a").json()
        assert data["route"]["source"] == "label20"
        vconn.close()
        rows.append({"ts": now - 100, "icao_hex": "484b2a", "label": "26",
                     "body": "1049EPWAAF1147/18\r1241LFPG27R"})
        vconn, app = self._make(monkeypatch, rows)
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/484b2a").json()
        assert data["route"]["source"] == "label26"
        vconn.close()

    def test_label26_route_has_only_contract_keys(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert set(data["route"]) == {"dep_icao", "dest_icao", "flight", "source"}
        vconn.close()

    def test_label26_fills_route_on_tei_event_missing_airports(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "3944e0", "label": "H1", "body": "DEP / OT 0030"},
            {"ts": now - 60, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["dest_icao"] == "LFPG"
        vconn.close()

    def test_superseded_label26_ignored(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "3944e0", "label": "26",
             "body": "1049EPWAAF1147/18\r1241LFPG27R"},
        ])
        vconn.execute("UPDATE vdl2_messages SET superseded = 1")
        vconn.commit()
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/3944e0").json()
        assert "route" not in data
        vconn.close()

    def test_qseries_event_missing_dep_filled_from_cm(self, monkeypatch):
        # A Q-series event always carries its own pair, so exercise the TEI path:
        # a DEP with times but no airports takes dep+dest from the CM route.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "H1", "body": "DEP / OT 0030"},
            {"ts": now - 200, "icao_hex": "4d2228", "label": "H1", "body": "ARR / ON 0210"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "CM", "body": "LOGON RYR8414 EPMO-LATI"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["dep_icao"] == "EPMO" and data["dep"]["dest_icao"] == "LATI"
        assert data["arr"]["dep_icao"] == "EPMO" and data["arr"]["dest_icao"] == "LATI"
        assert data["dep"]["t_out"] == "0030"
        assert data["route"]["source"] == "cm"
        vconn.close()

    def test_event_airports_not_overwritten_by_route(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 300, "icao_hex": "4d2228", "label": "QP", "body": "EPWAEGLL0930"},
            {"ts": now - 60, "icao_hex": "4d2228", "label": "CM", "body": "LOGON RYR8414 EPMO-LATI"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/4d2228").json()
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["dest_icao"] == "EGLL"
        vconn.close()

    def test_no_route_source_omits_route_key(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "48af11", "label": "H1", "dsta": "EPWA",
             "body": "#DFB free text, not oooi"},
            {"ts": now - 50, "icao_hex": "48af11", "label": "CM", "body": "LOGON RYR8414"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48af11").json()
        assert "route" not in data
        vconn.close()

    def test_route_has_only_contract_keys(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 60, "icao_hex": "48c132", "label": "CM", "body": "LOGON RYR8414 EPMO-LATI"},
            {"ts": now - 70, "icao_hex": "48c132", "label": "49",
             "body": "01ICCL    LOT15K/111616EPWAKEWR"},
        ])
        with TestClient(app) as c:
            data = c.get("/api/vdl2/oooi/48c132").json()
        assert set(data) == {"dep", "arr", "dsta", "has_oooi", "route"}
        assert set(data["route"]) == {"dep_icao", "dest_icao", "flight", "source"}
        vconn.close()


class TestBucketFill:
    """_bucket_fill — the zero-fill+scatter helper shared by the rate and
    per-frequency timeseries columns."""

    def test_scatters_in_range_and_zero_fills_gaps(self):
        rows = [{"bi": 0, "c": 5}, {"bi": 2, "c": 3}]
        assert vdl2_api._bucket_fill(rows, 4) == [5, 0, 3, 0]

    def test_ignores_out_of_range_indices(self):
        rows = [{"bi": -1, "c": 9}, {"bi": 1, "c": 7}, {"bi": 5, "c": 9}]
        assert vdl2_api._bucket_fill(rows, 3) == [0, 7, 0]

    def test_empty_rows_all_zero(self):
        assert vdl2_api._bucket_fill([], 3) == [0, 0, 0]


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


class TestSignal:
    """Per-frequency signal + SNR reception charts (dumpvdl2-only)."""

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

    @staticmethod
    def _nonnull(col):
        return [v for v in col if v is not None]

    def test_per_freq_signal_and_snr(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 90, "icao_hex": "48e95d", "freq": 136.725,
             "sig_level": -45.0, "noise_level": -53.0, "body": "a", "decoder": "dumpvdl2"},
            {"ts": now - 80, "icao_hex": "48af11", "freq": 136.725,
             "sig_level": -47.0, "noise_level": -53.0, "body": "b", "decoder": "dumpvdl2"},
            {"ts": now - 70, "icao_hex": "48af12", "freq": 136.975,
             "sig_level": -50.0, "noise_level": -52.0, "body": "c", "decoder": "dumpvdl2"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/signal?from={now - 180}&to={now}").json()
        vconn.close()
        assert data["bucket_seconds"] == 60
        # top freq first (2 rows on 136.725 vs 1 on 136.975); shared by both matrices
        assert data["metrics"] == ["136.725", "136.975"]
        assert data["freqs"] == [136.725, 136.975]
        assert data["samples"] == 3
        # signal[0] is the ts column; signal[i+1] aligns with metrics[i]
        assert self._nonnull(data["signal"][1]) == [-46.0]   # avg(-45, -47)
        assert self._nonnull(data["signal"][2]) == [-50.0]
        # SNR = avg(sig - noise): 136.725 → avg(8, 6) = 7; 136.975 → 2
        assert self._nonnull(data["snr"][1]) == [7.0]
        assert self._nonnull(data["snr"][2]) == [2.0]
        # empty buckets are null (gap), NOT 0 — 0 dBFS is a real, very-strong value
        assert None in data["signal"][1]

    def test_sig_without_noise_excluded(self, monkeypatch):
        # A row with sig_level but no noise_level yields no SNR, so it's excluded
        # from the series AND from samples/newest — all describe the same rows.
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 50, "icao_hex": "48e95d", "freq": 136.725,
             "sig_level": -45.0, "noise_level": -53.0, "body": "a", "decoder": "dumpvdl2"},
            {"ts": now - 40, "icao_hex": "48af11", "freq": 136.725,
             "sig_level": -44.0, "body": "b", "decoder": "dumpvdl2"},  # no noise_level
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/signal?from={now - 180}&to={now}").json()
        vconn.close()
        assert data["samples"] == 1                       # only the sig+noise row
        assert self._nonnull(data["signal"][1]) == [-45.0]  # noise-less row excluded

    def test_empty_when_no_signal_rows(self, monkeypatch):
        # vdlm2dec-style rows carry no sig_level → the charts self-hide (empty).
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 30, "icao_hex": "48e95d", "freq": 136.725, "body": "x"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/signal?from={now - 180}&to={now}").json()
        vconn.close()
        assert data["metrics"] == []
        assert data["freqs"] == []
        assert data["signal"] == []
        assert data["snr"] == []
        assert data["samples"] == 0

    def test_window_validation_and_span_cap(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get(f"/api/vdl2/signal?from={now}&to={now}").status_code == 400
            assert c.get(f"/api/vdl2/signal?from=0&to={now}").status_code == 400
            ok = c.get(f"/api/vdl2/signal?from={now - 366 * 86400 + 100}&to={now}")
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
            assert c.get("/api/vdl2/signal").status_code == 503


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
            assert c.get("/api/vdl2/signal").status_code == 503

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


class TestQueryParamBounds:
    """An int >= 2**63 raises OverflowError at SQLite bind (not a sqlite3.Error, so
    _vdl2_guard would not map it): it must be rejected as 422 by the Query bounds."""

    HUGE = "99999999999999999999"

    @pytest.mark.parametrize("path", [
        "/api/vdl2/messages?before_id={h}",
        "/api/vdl2/messages?since={h}",
        "/api/vdl2/messages?until={h}",
        "/api/vdl2/messages/48e95d?before_id={h}",
        "/api/vdl2/messages/48e95d?since={h}",
        "/api/vdl2/messages/48e95d?until={h}",
        "/api/vdl2/oooi/48e95d?since={h}",
        "/api/vdl2/oooi/48e95d?until={h}",
    ])
    def test_huge_value_is_422(self, client, path):
        assert client.get(path.format(h=self.HUGE)).status_code == 422

    def test_just_over_the_cap_is_422_and_cap_is_accepted(self, client):
        assert client.get("/api/vdl2/messages?since=10000000001").status_code == 422
        assert client.get("/api/vdl2/messages?before_id=%d" % (2 ** 62 + 1)).status_code == 422
        assert client.get("/api/vdl2/messages?since=0&until=10000000000").status_code == 200
        assert client.get("/api/vdl2/messages?before_id=%d" % 2 ** 62).status_code == 200


class TestWebNeverCreatesDb:
    """The web process only reads vdl2.db: a deleted file must not be recreated
    as an empty DB (ADR-0021); VDL2 surfaces report unavailable instead."""

    @pytest.fixture()
    def missing(self, tmp_path, monkeypatch):
        path = tmp_path / "vdl2.db"
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        monkeypatch.setattr(config, "VDL2_DB_PATH", str(path))
        monkeypatch.setattr(vdl2_db, "_conn", None)
        monkeypatch.setattr(vdl2_db, "_thread_local", threading.local())
        with vdl2_db._web_conns_lock:
            vdl2_db._web_conns.clear()
        return path

    def test_connect_create_false_missing_raises_and_creates_nothing(self, tmp_path):
        path = tmp_path / "nope.db"
        with pytest.raises(sqlite3.OperationalError):
            vdl2_db.connect(str(path), create=False)
        assert not list(tmp_path.iterdir())

    def test_connect_create_false_opens_existing_read_write(self, tmp_path):
        path = str(tmp_path / "v.db")
        c = vdl2_db.connect(path)
        vdl2_db.ensure_schema(c, build_fts=False)
        c.close()
        c = vdl2_db.connect(path, create=False)
        assert c.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 0
        c.execute("PRAGMA user_version")   # still a usable rw connection
        c.close()

    def test_connect_create_false_handles_special_chars_in_path(self, tmp_path):
        d = tmp_path / "a b#c?d"
        d.mkdir()
        path = str(d / "vdl2.db")
        c = vdl2_db.connect(path)
        c.close()
        vdl2_db.connect(path, create=False).close()

    def test_web_conn_missing_file_not_created(self, missing, tmp_path):
        with pytest.raises(sqlite3.OperationalError):
            vdl2_db.web_conn()
        assert not missing.exists()
        assert not list(tmp_path.glob("vdl2.db*"))

    def test_api_503_and_health_unavailable_when_file_missing(self, missing, tmp_path):
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/messages").status_code == 503
            assert c.get("/api/vdl2/stats").status_code == 503
        assert vdl2_api.vdl2_health()["available"] is False
        assert not list(tmp_path.glob("vdl2.db*"))

    def test_startup_with_missing_file_does_not_crash_or_create(self, missing, tmp_path):
        web._ensure_vdl2_schema()      # must not raise
        assert not list(tmp_path.glob("vdl2.db*"))

    def test_startup_migrates_existing_file(self, missing):
        c = vdl2_db.connect(str(missing))     # collector-style creation
        c.close()
        web._ensure_vdl2_schema()
        c = vdl2_db.connect(str(missing), create=False)
        assert c.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 0
        c.close()


class TestConnRegistry:
    def test_close_all_web_conns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))
        monkeypatch.setattr(vdl2_db, "_conn", None)
        monkeypatch.setattr(vdl2_db, "_thread_local", threading.local())
        with vdl2_db._web_conns_lock:
            vdl2_db._web_conns.clear()
        # web_conn() never creates the file: the collector side does.
        vdl2_db.connect(str(tmp_path / "vdl2.db")).close()
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
        assert h["newest_ts"] == 123
        # No COUNT(*): a full scan of the large messages table made /api/health
        # take tens of seconds on a cold disk. Exact totals live in /vdl2/stats.
        assert "messages" not in h
        conn.close()

    def test_newest_query_uses_ts_index(self):
        """The health probe must be an index lookup, never a table scan."""
        conn = make_vdl2_db()
        plan = " ".join(
            r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + vdl2_api._HEALTH_NEWEST_SQL))
        assert "idx_vdl2_ts" in plan, plan
        assert not re.search(r"SCAN vdl2_messages(?! USING)", plan), plan
        conn.close()


class TestGating:
    def test_router_absent_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", False)
        app = FastAPI()
        web._include_optional_routers(app)
        paths = {r.path for r in iter_api_routes(app.routes)}
        assert not any(p.startswith("/api/vdl2") for p in paths)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/messages").status_code == 404

    def test_router_present_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        app = FastAPI()
        web._include_optional_routers(app)
        paths = {r.path for r in iter_api_routes(app.routes)}
        assert "/api/vdl2/messages" in paths
        assert "/api/vdl2/signal" in paths


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


class TestPreciseLabelPositions:
    """Labels 17/38/2F/14/B0 (body parsers) + B6 ADS-C (raw JSON) feed /api/vdl2/positions."""

    @staticmethod
    def _b6_raw(lat=52.202053, lon=20.719013, alt=2852) -> str:
        import json
        return json.dumps({"vdl2": {"avlc": {"acars": {"arinc622": {"adsc": {"tags": [
            {"basic_report": {"lat": lat, "lon": lon, "alt": alt}}]}}}}}})

    @pytest.fixture()
    def lab_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            {"ts": base - 10, "icao_hex": "48b017", "label": "17",
             "body": "104800,1656,1049,  7520,N 52.237,E 20.877,24OCT26,22051,   5,   7"},
            # `------` altitude: departure-airport coordinates, NOT the aircraft
            {"ts": base - 11, "icao_hex": "48b018", "label": "17",
             "body": "203618,2310,2241,------,N 41.797,E 12.250,18SEP26,27441,  19,  14"},
            {"ts": base - 12, "icao_hex": "48b038", "label": "38",
             "body": "/AY1476/240926/E 20.735/N 52.402/36945/-------/      /- 48/  563/ 29"},
            {"ts": base - 13, "icao_hex": "48b02f", "label": "2F", "body": "1310+5219+2087/0130"},
            {"ts": base - 14, "icao_hex": "48b014", "label": "14",
             "body": "PRESENT POSITION REPORT \nDMY 23OCT26,UTC 082912,LAT N 52.223,LON E 20.812,CAS 149"},
            {"ts": base - 15, "icao_hex": "48b0b0", "label": "B0",
             "body": "/EPWW.AFN/FMHLOT45,.SP-LRA,48AE00,154016/FPON52016E020114,1/FCOATC,01"},
            {"ts": base - 16, "icao_hex": "48b0b6", "label": "B6", "body": "/RUHAAYA.ADS.A6-ENQ07251F",
             "raw": self._b6_raw()},
            # B6 without a basic_report tag, and with truncated raw: no point
            {"ts": base - 17, "icao_hex": "48b0b7", "label": "B6", "body": "x",
             "raw": '{"vdl2":{"avlc":{"acars":{"arinc622":{"adsc":{"tags":[{"predicted_route":{}}]}}}}}}'},
            {"ts": base - 18, "icao_hex": "48b0b8", "label": "B6", "body": "x",
             "raw": self._b6_raw()[:60]},
            # same text under an unmapped label: not precise (no coarse columns either)
            {"ts": base - 19, "icao_hex": "48b0a1", "label": "H1",
             "body": "104800,1656,1049,  7520,N 52.237,E 20.877,24OCT26,22051,   5,   7"},
        ])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            yield c
        conn.close()

    def _by(self, client) -> dict:
        r = client.get("/api/vdl2/positions?minutes=60")
        assert r.status_code == 200
        return {p["icao_hex"]: p for p in r.json()["points"]}

    def test_label17(self, lab_client):
        p = self._by(lab_client)["48b017"]
        assert p["precise"] is True and p["label"] == "17"
        assert abs(p["lat"] - 52.237) < 1e-6 and abs(p["lon"] - 20.877) < 1e-6

    def test_label17_dash_altitude_yields_no_point(self, lab_client):
        assert "48b018" not in self._by(lab_client)

    def test_label38_lon_first(self, lab_client):
        p = self._by(lab_client)["48b038"]
        assert p["precise"] is True
        assert abs(p["lat"] - 52.402) < 1e-6 and abs(p["lon"] - 20.735) < 1e-6

    def test_label2f_hundredths(self, lab_client):
        p = self._by(lab_client)["48b02f"]
        assert p["precise"] is True and p["lat"] == 52.19 and p["lon"] == 20.87

    def test_label14(self, lab_client):
        p = self._by(lab_client)["48b014"]
        assert p["precise"] is True
        assert abs(p["lat"] - 52.223) < 1e-6 and abs(p["lon"] - 20.812) < 1e-6

    def test_b0_afn_fpo(self, lab_client):
        p = self._by(lab_client)["48b0b0"]
        assert p["precise"] is True and p["label"] == "B0"
        assert abs(p["lat"] - (52 + 1.6 / 60)) < 1e-4
        assert abs(p["lon"] - (20 + 11.4 / 60)) < 1e-4

    def test_b6_adsc_basic_report(self, lab_client):
        p = self._by(lab_client)["48b0b6"]
        assert p["precise"] is True and p["label"] == "B6"
        assert abs(p["lat"] - 52.20205) < 1e-5 and abs(p["lon"] - 20.71901) < 1e-5

    def test_b6_without_basic_report_or_truncated_yields_no_point(self, lab_client):
        by = self._by(lab_client)
        assert "48b0b7" not in by and "48b0b8" not in by

    def test_unmapped_label_not_parsed(self, lab_client):
        assert "48b0a1" not in self._by(lab_client)

    def test_b6_coarse_duplicate_not_emitted_twice(self, monkeypatch):
        # a B6 row that also has coarse lat/lon columns yields ONE (precise) point
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [
            {"ts": int(time.time()) - 5, "icao_hex": "48b0b6", "label": "B6", "body": "x",
             "lat": 52.1, "lon": 20.7, "raw": self._b6_raw()}])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            pts = c.get("/api/vdl2/positions?minutes=60").json()["points"]
        conn.close()
        assert len(pts) == 1 and pts[0]["precise"] is True
        assert abs(pts[0]["lat"] - 52.20205) < 1e-5

    def test_cache_key_unchanged(self, lab_client):
        from readsbstats import cache
        lab_client.get("/api/vdl2/positions?minutes=60")
        assert cache._get_cache("vdl2-positions-60") is not None


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
            # dumpvdl2 row: libacars moved '#CFB' into sublabel, the body is bare.
            {"ts": base - 15, "icao_hex": "48ae35", "label": "H1", "sublabel": "CF",
             "body": "RTE 1 28JUL26 1302 LO SP-LRH LOT6NY EPWA/KJFK BCG4F-45LD-0077 C L 1053 28JUL26\nFDE 214124"},
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

    def test_rte_behind_non_t1_sublabel_carries_filed_route(self, fr_client):
        msgs = fr_client.get("/api/vdl2/messages").json()["messages"]
        cf = next(m for m in msgs if m["icao_hex"] == "48ae35")
        assert cf["filed_route"] == {"dep": "EPWA", "arr": "KJFK",
                                     "company_route": "BCG4F-45LD-0077 C"}

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


class TestDumpvdl2Ingest:
    """Rows ingested through the real dumpvdl2 normalizer: libacars strips the H1
    '#<SL>B[/<MFI> ]' prefix into sublabel/mfi, so the API must rebuild it for the
    prefix-keyed parsers while leaving the stored body untouched."""

    @pytest.fixture()
    def dv_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        monkeypatch.setattr(_deps, "_db", make_db())
        conn = make_vdl2_db()
        now = int(time.time())
        vdl2_db.insert_messages(conn, [
            dumpvdl2_record("m1_pos", now - 40),
            dumpvdl2_record("m1_route", now - 30),
            dumpvdl2_record("m1_mfi", now - 20),
            dumpvdl2_record("xid_ho", now - 10),
            # vdlm2dec-shaped #M1BPOS row (prefix kept in body, no sublabel) — regression guard
            {"ts": now - 5, "icao_hex": "48ae21", "label": "H1",
             "body": "#M1BPOSN52081E020017,N51491E019372,191139,370,BOKSU,M51,19155"},
        ])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            yield c
        conn.close()

    def test_m1_pos_row_is_precise_point(self, dv_client):
        pts = dv_client.get("/api/vdl2/positions?minutes=60").json()["points"]
        mine = [p for p in pts if p["icao_hex"] == "4787a0"]
        assert len(mine) == 1
        assert mine[0]["precise"] is True
        assert abs(mine[0]["lat"] - (52 + 25.6 / 60)) < 1e-5    # N52256
        assert abs(mine[0]["lon"] - (19 + 18.6 / 60)) < 1e-5    # E019186

    def test_m1_non_pos_row_yields_no_point(self, dv_client):
        # AFN/FPO row (sublabel M1 + mfi B0, body not POS...) must not become a point
        pts = dv_client.get("/api/vdl2/positions?minutes=60").json()["points"]
        assert all(p["icao_hex"] != "48ea00" for p in pts)

    def test_m1_route_row_has_filed_route_and_untouched_body(self, dv_client):
        msgs = dv_client.get("/api/vdl2/messages").json()["messages"]
        row = next(m for m in msgs if m["icao_hex"] == "47bf9d")
        assert row["filed_route"]["dep"] == "EPWA"
        assert row["filed_route"]["arr"] == "ENGM(19R)"
        assert row["body"].startswith("POSN52182E020495")    # no '#M1B' rebuilt into body
        assert row["sublabel"] == "M1"
        assert row["mfi"] is None

    def test_mfi_exposed_and_body_unchanged(self, dv_client):
        msgs = dv_client.get("/api/vdl2/messages").json()["messages"]
        row = next(m for m in msgs if m["icao_hex"] == "48ea00")
        assert row["sublabel"] == "M1" and row["mfi"] == "B0"
        assert row["body"].startswith("EPWW.AFN/")
        assert "filed_route" not in row

    def test_xid_row_is_coarse_point(self, dv_client):
        pts = dv_client.get("/api/vdl2/positions?minutes=60").json()["points"]
        mine = [p for p in pts if p["icao_hex"] == "48c132"]
        assert len(mine) == 1
        assert mine[0]["precise"] is False
        assert abs(mine[0]["lat"] - 52.3) < 0.01
        assert abs(mine[0]["lon"] - 20.5) < 0.01

    def test_xid_dsta_reported_by_oooi(self, dv_client):
        data = dv_client.get("/api/vdl2/oooi/48c132").json()
        assert data["has_oooi"] is False
        assert data["dsta"] == "LATI"

    def test_vdlm2dec_m1bpos_still_works(self, dv_client):
        pts = dv_client.get("/api/vdl2/positions?minutes=60").json()["points"]
        mine = [p for p in pts if p["icao_hex"] == "48ae21"]
        assert len(mine) == 1 and mine[0]["precise"] is True
        assert abs(mine[0]["lat"] - 52.135) < 1e-6
        msgs = dv_client.get("/api/vdl2/messages").json()["messages"]
        row = next(m for m in msgs if m["icao_hex"] == "48ae21")
        assert row["sublabel"] is None and row["mfi"] is None


class TestSupersededFragments:
    """Multi-block ACARS: earlier block rows hidden once the final block carries the
    full text (``superseded``); ``fragments=true`` shows them."""

    _POS = "#M1BPOSN52081E020017,N51491E019372,191139,370,BOKSU,M51,19155"

    @pytest.fixture()
    def frag_client(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        base = int(time.time())
        vdl2_db.insert_messages(conn, [
            {"ts": base - 40, "icao_hex": "48ae21", "label": "H1", "msgno": "D75",
             "more": 1, "body": self._POS[:40]},
            {"ts": base - 35, "icao_hex": "48ae21", "label": "H1", "msgno": "D75",
             "more": 0, "body": self._POS},
            {"ts": base - 30, "icao_hex": "48ae22", "label": "H1", "msgno": "D80",
             "more": 1, "body": "an orphan fragment with no final block"},
        ])
        # the collector marks at ingest; do the same here
        vdl2_db.mark_superseded(conn, [{"ts": base - 35, "icao_hex": "48ae21", "label": "H1",
                                        "msgno": "D75", "more": 0, "body": self._POS}])
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            c.conn = conn
            yield c
        conn.close()

    def test_default_hides_superseded_keeps_orphan_fragment(self, frag_client):
        msgs = frag_client.get("/api/vdl2/messages").json()["messages"]
        assert [m["body"] for m in msgs] == ["an orphan fragment with no final block", self._POS]

    def test_fragments_true_shows_all(self, frag_client):
        msgs = frag_client.get("/api/vdl2/messages?fragments=true").json()["messages"]
        assert len(msgs) == 3
        hidden = [m for m in msgs if m["superseded"] == 1]
        assert len(hidden) == 1 and hidden[0]["more"] == 1

    def test_rows_carry_more_and_superseded(self, frag_client):
        msgs = frag_client.get("/api/vdl2/messages").json()["messages"]
        final = next(m for m in msgs if m["body"] == self._POS)
        assert final["more"] == 0 and final["superseded"] is None
        orphan = next(m for m in msgs if m["msgno"] == "D80")
        assert orphan["more"] == 1 and orphan["superseded"] is None

    def test_aircraft_endpoint_same_behaviour(self, frag_client):
        d = frag_client.get("/api/vdl2/messages/48ae21").json()["messages"]
        assert len(d) == 1 and d[0]["body"] == self._POS
        d = frag_client.get("/api/vdl2/messages/48ae21?fragments=true").json()["messages"]
        assert len(d) == 2

    def test_pagination_not_broken_by_hidden_rows(self, frag_client):
        first = frag_client.get("/api/vdl2/messages?limit=1").json()
        assert len(first["messages"]) == 1 and first["next_before_id"] is not None
        nxt = frag_client.get(
            f"/api/vdl2/messages?limit=1&before_id={first['next_before_id']}").json()
        assert [m["body"] for m in nxt["messages"]] == [self._POS]   # hidden id 1 skipped
        assert nxt["next_before_id"] is not None   # page was full; one more (empty) page ends it
        last = frag_client.get(
            f"/api/vdl2/messages?limit=1&before_id={nxt['next_before_id']}").json()
        assert last["messages"] == [] and last["next_before_id"] is None

    def test_positions_emit_one_point_for_multi_block_m1bpos(self, frag_client):
        # fragment is cut mid-coordinates, so only the full text parses; give the
        # fragment a complete fix too to prove the superseded filter (not the parser)
        # is what drops it.
        c = frag_client.conn
        c.execute("UPDATE vdl2_messages SET body = ? WHERE msgno = 'D75' AND more = 1",
                  (self._POS,))
        c.commit()
        pts = [p for p in frag_client.get("/api/vdl2/positions?minutes=60").json()["points"]
               if p["icao_hex"] == "48ae21"]
        assert len(pts) == 1

    def test_positions_include_unsuperseded_duplicate(self, frag_client):
        c = frag_client.conn
        c.execute("UPDATE vdl2_messages SET body = ?, superseded = NULL WHERE msgno = 'D75' AND more = 1",
                  (self._POS,))
        c.commit()
        pts = [p for p in frag_client.get("/api/vdl2/positions?minutes=60").json()["points"]
               if p["icao_hex"] == "48ae21"]
        assert len(pts) == 2     # control: without the mark both rows yield a point

    def test_oooi_ignores_superseded_rows(self, frag_client):
        c = frag_client.conn
        now = int(time.time())
        vdl2_db.insert_messages(c, [
            {"ts": now - 20, "icao_hex": "48ae25", "label": "H1", "superseded": 1,
             "body": "DEP / FI LO6550/AN SP-LYF/DA EPWA/DS EGLL/OT 0030"},
        ])
        c.commit()
        assert frag_client.get("/api/vdl2/oooi/48ae25").json()["has_oooi"] is False
        c.execute("UPDATE vdl2_messages SET superseded = NULL WHERE icao_hex = '48ae25'")
        c.commit()
        assert frag_client.get("/api/vdl2/oooi/48ae25").json()["has_oooi"] is True

    def test_stats_still_count_every_frame(self, frag_client):
        assert frag_client.get("/api/vdl2/stats").json()["total"] == 3


class TestBatch5Positions:
    """ADS-B-validated sources (ADR-0019): H1 ABS/EB0 multi-fix, CF FDE/MM, ``(POS-``,
    labels 83 / 15 / 80. Parsed at query time; every point carries ``source``."""

    CSV = ("ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,BCG27-S200-0011\r\n"
           "N5211.0,E02052.4,{d}{h:02d}{m0:02d},02421,-03.5,301,029,CL,00000,0,\r\n"
           "N5210.9,E02051.9,{d}{h:02d}{m1:02d},02563,-03.5,305,031,CL,00000,0,\r\n"
           "N5210.8,E02051.3,{d}{h:02d}{m2:02d},02698,-04.0,307,031,CL,00000,0,\r\n:")

    @staticmethod
    def _client(monkeypatch, rows):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, rows)
        conn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", conn)
        app = FastAPI()
        web._include_optional_routers(app)
        return conn, TestClient(app)

    @staticmethod
    def _pts(c, minutes=60):
        r = c.get(f"/api/vdl2/positions?minutes={minutes}")
        assert r.status_code == 200
        return r.json()["points"]

    @staticmethod
    def _csv_body(recv, back=(3, 2, 1)):
        """CSV ABS body whose fixes are `back` minutes before `recv` (UTC DDHHMM)."""
        import datetime as dt
        times = [dt.datetime.fromtimestamp(recv - 60 * b, dt.timezone.utc) for b in back]
        body = ("ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,BCG27-S200-0011")
        for t in times:
            body += (f"\r\nN5211.0,E02052.4,{t:%d%H%M},02421,-03.5,301,029,CL,00000,0,")
        return body + "\r\n:", [int(t.replace(second=0).timestamp()) for t in times]

    def test_multi_fix_abs_yields_points_with_per_fix_ts(self, monkeypatch):
        recv = int(time.time()) - 30
        body, fix_ts = self._csv_body(recv)
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489001", "label": "H1", "sublabel": "DF", "body": body}])
        with client as c:
            pts = [p for p in self._pts(c) if p["icao_hex"] == "489001"]
        conn.close()
        assert len(pts) == 3
        assert sorted(p["ts"] for p in pts) == sorted(fix_ts)
        assert len({p["ts"] for p in pts}) == 3          # one message id, distinct ts
        p = next(p for p in pts if p["ts"] == fix_ts[0])
        assert p["source"] == "h1-abs" and p["precise"] is True and p["label"] == "H1"
        assert p["alt"] == 2421 and p["temp_c"] == -3.5
        assert p["wind_dir"] == 301 and p["wind_kt"] == 29
        assert abs(p["lat"] - (52 + 11.0 / 60)) < 1e-4

    def test_fixed_width_has_alt_but_no_wind(self, monkeypatch):
        import datetime as dt
        recv = int(time.time()) - 30
        t = dt.datetime.fromtimestamp(recv - 120, dt.timezone.utc)
        body = ("ABS011DA_S       EPWALEMD4\r\n"
                f" 52188  20784{t:%H%M}004137-06-270019X1XIH-0801090000--")
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489002", "label": "H1", "sublabel": "DF", "body": body}])
        with client as c:
            (p,) = [p for p in self._pts(c) if p["icao_hex"] == "489002"]
        conn.close()
        assert p["source"] == "h1-abs" and p["alt"] == 4137 and p["lat"] == 52.188
        assert p["ts"] == int(t.replace(second=0).timestamp())
        assert "temp_c" not in p and "wind_dir" not in p and "wind_kt" not in p

    def test_old_embedded_fix_outside_window_is_dropped(self, monkeypatch):
        # message received now, but two fixes are older than the 10-minute window
        recv = int(time.time()) - 30
        body, fix_ts = self._csv_body(recv, back=(30, 20, 1))
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489003", "label": "H1", "sublabel": "DF", "body": body}])
        with client as c:
            pts = [p for p in self._pts(c, minutes=10) if p["icao_hex"] == "489003"]
            allpts = [p for p in self._pts(c, minutes=60) if p["icao_hex"] == "489003"]
        conn.close()
        assert [p["ts"] for p in pts] == [fix_ts[2]]
        assert len(allpts) == 3

    def test_future_fix_beyond_slack_dropped(self, monkeypatch):
        recv = int(time.time()) - 30
        body, _ = self._csv_body(recv, back=(-20, -15, 1))   # 20/15 min in the future
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489004", "label": "H1", "sublabel": "DF", "body": body}])
        with client as c:
            pts = [p for p in self._pts(c) if p["icao_hex"] == "489004"]
        conn.close()
        # fix_epoch rolls a "future" HHMM back a day only for HHMM-only formats; DDHHMM stays
        # in the future and must be filtered out by the handler.
        assert len(pts) == 1

    def test_superseded_h1_row_ignored(self, monkeypatch):
        recv = int(time.time()) - 30
        body, _ = self._csv_body(recv)
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489005", "label": "H1", "sublabel": "DF", "body": body,
             "superseded": 1}])
        with client as c:
            assert [p for p in self._pts(c) if p["icao_hex"] == "489005"] == []
        conn.close()

    def test_wrong_sublabel_h1_body_never_yields_points(self, monkeypatch):
        recv = int(time.time()) - 30
        body, _ = self._csv_body(recv)
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489006", "label": "H1", "sublabel": "M1", "body": body},
            {"ts": recv - 1, "icao_hex": "489007", "label": "H1", "sublabel": None, "body": body},
            {"ts": recv - 2, "icao_hex": "489008", "label": "H1", "sublabel": "CF", "body": body},
            {"ts": recv - 3, "icao_hex": "489009", "label": "Q0", "sublabel": "DF", "body": body},
        ])
        with client as c:
            pts = self._pts(c)
        conn.close()
        assert not [p for p in pts if p["icao_hex"] in {"489006", "489007", "489008", "489009"}]

    def test_cf_fde_and_pos_sources(self, monkeypatch):
        import datetime as dt
        recv = int(time.time()) - 30
        t = dt.datetime.fromtimestamp(recv - 20, dt.timezone.utc)
        fde = f"FDE{t:%H%M%S}1123117" + "52.17762/20.84221\r\n06460114706    0   0"
        pos = f"(POS-KLM75C  -5222N02005E/{t:%H%M%S} F180\r\nRMK/FUEL   5.4 M0.59)"
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489010", "label": "H1", "sublabel": "CF", "body": fde},
            {"ts": recv, "icao_hex": "489011", "label": "H1", "sublabel": "DF", "body": pos},
            {"ts": recv, "icao_hex": "489012", "label": "H1", "sublabel": "CF",
             "body": "FDE1227481123ACA\n12254714301   0   0"},
        ])
        with client as c:
            by = {p["icao_hex"]: p for p in self._pts(c)}
        conn.close()
        assert by["489010"]["source"] == "h1-cf" and by["489010"]["lat"] == 52.17762
        assert by["489010"]["ts"] == int(t.timestamp())
        assert by["489011"]["source"] == "h1-pos" and by["489011"]["alt"] == 18000
        assert "489012" not in by

    def test_labels_83_15_80(self, monkeypatch):
        import datetime as dt
        recv = int(time.time()) - 30
        t = dt.datetime.fromtimestamp(recv - 7, dt.timezone.utc)
        pr = f"001PR{t:%d%H%M%S}N5207.4E02041.4430426"
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "48a083", "label": "83", "body": pr},
            {"ts": recv - 1, "icao_hex": "48a084", "label": "83",
             "body": "HAAB,ESSA,210319, 52.41,  20.34,39002,263,- 23.3, 13300"},
            {"ts": recv - 2, "icao_hex": "48a085", "label": "83",
             "body": "4DH3 ETAT2  0751/21 EKCH/EPWA .OY-KBO\r\n/ETA 0848"},
            {"ts": recv - 3, "icao_hex": "48a015", "label": "15",
             "body": "FST01EGLLEPWAN52232E0207997 36  33  51     35262      188  10531050"},
            {"ts": recv - 4, "icao_hex": "48a016", "label": "15",
             "body": "FST01EPWAEGLL 00            26  79         21              09100658"},
            {"ts": recv - 5, "icao_hex": "48a080", "label": "80",
             "body": "3N01 POSRPT 0872/19 LGAV/EPWA .SX-DVX\r\n/POS N52141E020397/ALT +05475/MCH 284/FOB 0032/ETA 1702"},
            {"ts": recv - 6, "icao_hex": "48a081", "label": "80", "body": "MOCC: HI NO DATA RECEIVED FOR"},
        ])
        with client as c:
            by = {p["icao_hex"]: p for p in self._pts(c)}
        conn.close()
        assert set(by) == {"48a083", "48a084", "48a015", "48a080"}
        assert all(p["source"] == "label" and p["precise"] is True for p in by.values())
        assert by["48a083"]["ts"] == int(t.timestamp()) and "alt" not in by["48a083"]
        assert by["48a084"]["ts"] == recv - 1 and by["48a084"]["alt"] == 39002
        assert by["48a015"]["ts"] == recv - 3 and by["48a015"]["lat"] == 52.232
        assert by["48a080"]["alt"] == 5475

    def test_existing_sources_tagged(self, monkeypatch):
        recv = int(time.time()) - 30
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "48c016", "label": "16",
             "body": "WA921  ,N 52.166,E 020.772,4406, 251,2054, 72\\TS154458,050626"},
            {"ts": recv - 1, "icao_hex": "48c0a1", "label": "H1", "sublabel": "M1",
             "body": "POSN52256E019186"},
            {"ts": recv - 2, "icao_hex": "48c059", "label": "36",
             "body": "59,G,1101,1,1,SP-LVS,52.15,20.59,52.15,20.61,0"},
            {"ts": recv - 3, "icao_hex": "48c0c0", "label": "H1", "lat": 52.3, "lon": 20.5,
             "body": "x"},
        ])
        with client as c:
            by = {p["icao_hex"]: p for p in self._pts(c)}
        conn.close()
        assert by["48c016"]["source"] == "label"
        assert by["48c0a1"]["source"] == "m1bpos"
        assert by["48c059"]["source"] == "59g"
        assert by["48c0c0"]["source"] == "xid" and by["48c0c0"]["precise"] is False
        assert "alt" not in by["48c016"]

    def test_final_cap_respected_with_multi_fix(self, monkeypatch):
        monkeypatch.setattr(vdl2_api, "_POSITIONS_CAP", 4)
        recv = int(time.time()) - 30
        rows = []
        for i in range(3):
            body, _ = self._csv_body(recv - i * 5)
            rows.append({"ts": recv - i * 5, "icao_hex": f"4891{i:02x}", "label": "H1",
                         "sublabel": "DF", "body": body})
        conn, client = self._client(monkeypatch, rows)
        with client as c:
            r = c.get("/api/vdl2/positions?minutes=60").json()
        conn.close()
        assert r["count"] == len(r["points"]) == 4
        ts = [p["ts"] for p in r["points"]]
        assert ts == sorted(ts, reverse=True)

    def test_h1_scan_cap_constant_and_window_bound(self, monkeypatch):
        assert vdl2_api._POSITIONS_H1_SCAN_CAP == 8000
        monkeypatch.setattr(vdl2_api, "_POSITIONS_H1_SCAN_CAP", 2)
        recv = int(time.time()) - 30
        body, _ = self._csv_body(recv)
        rows = [{"ts": recv - i, "icao_hex": f"4892{i:02x}", "label": "H1", "sublabel": "DF",
                 "body": body} for i in range(5)]
        conn, client = self._client(monkeypatch, rows)
        with client as c:
            pts = self._pts(c)
        conn.close()
        assert len({p["icao_hex"] for p in pts}) == 2    # only the 2 newest candidates scanned

    def test_h1_candidate_query_uses_label_index(self, monkeypatch):
        conn = make_vdl2_db()
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + vdl2_api._H1_POSITIONS_SQL, (0, 10)))
        conn.close()
        assert "idx_vdl2_label_ts_id" in plan

    def test_schema_optional_fields_absent_when_unset(self, monkeypatch):
        recv = int(time.time()) - 30
        conn, client = self._client(monkeypatch, [
            {"ts": recv, "icao_hex": "48c016", "label": "16",
             "body": "WA921  ,N 52.166,E 020.772,4406, 251,2054, 72\\TS154458,050626"}])
        with client as c:
            (p,) = self._pts(c)
        conn.close()
        assert set(p) == {"lat", "lon", "icao_hex", "ts", "label", "precise", "source"}


class TestAdscMotionFields:
    @staticmethod
    def _raw(tags) -> str:
        import json
        return json.dumps({"vdl2": {"avlc": {"acars": {"arinc622": {"adsc": {"tags": tags}}}}}})

    def test_earth_ref_attached_to_adsc_point(self, monkeypatch):
        raw = self._raw([{"basic_report": {"lat": 52.202053, "lon": 20.719013, "alt": 2852}},
                         {"earth_ref_data": {"true_trk_deg": 3.691406, "true_trk_valid": True,
                                             "gnd_spd_kts": 195.5, "vspd_ftmin": -272}}])
        raw2 = self._raw([{"basic_report": {"lat": 52.1, "lon": 20.7, "alt": 100}}])
        raw3 = self._raw([{"basic_report": {"lat": 52.2, "lon": 20.6, "alt": 100}},
                          {"earth_ref_data": {"true_trk_deg": "x", "gnd_spd_kts": 150.0,
                                              "vspd_ftmin": "y"}}])
        now = int(time.time())
        conn, client = TestBatch5Positions._client(monkeypatch, [
            {"ts": now - 5, "icao_hex": "48b0b6", "label": "B6", "body": "x", "raw": raw},
            {"ts": now - 6, "icao_hex": "48b0b7", "label": "B6", "body": "x", "raw": raw2},
            {"ts": now - 7, "icao_hex": "48b0b8", "label": "B6", "body": "x", "raw": raw3},
        ])
        with client as c:
            by = {p["icao_hex"]: p for p in c.get("/api/vdl2/positions?minutes=60").json()["points"]}
        conn.close()
        assert by["48b0b6"]["source"] == "adsc"
        assert by["48b0b6"]["track"] == 3.6914 and by["48b0b6"]["gs"] == 195.5
        assert by["48b0b6"]["vs"] == -272
        assert not {"track", "gs", "vs"} & set(by["48b0b7"])        # tag absent -> fields absent
        assert by["48b0b8"]["gs"] == 150.0                          # point kept, bad fields absent
        assert "track" not in by["48b0b8"] and "vs" not in by["48b0b8"]


class TestFlightPositions:
    """GET /api/vdl2/positions/{icao_hex}?since=&until= — per-flight route overlay."""

    HEX = "489100"

    @staticmethod
    def _pts(c, hexv="489100", since=None, until=None, expect=200):
        url = f"/api/vdl2/positions/{hexv}"
        q = []
        if since is not None:
            q.append(f"since={since}")
        if until is not None:
            q.append(f"until={until}")
        r = c.get(url + ("?" + "&".join(q) if q else ""))
        assert r.status_code == expect, r.text
        return r.json()["points"] if expect == 200 else r

    @pytest.fixture()
    def fp(self, monkeypatch):
        now = int(time.time())
        recv = now - 600
        body, fix_ts = TestBatch5Positions._csv_body(recv, back=(3, 2, 1))
        far_body, far_ts = TestBatch5Positions._csv_body(recv - 7200, back=(3, 2, 1))
        rows = [
            # multi-fix ABS for the airframe
            {"ts": recv, "icao_hex": self.HEX, "label": "H1", "sublabel": "DF", "body": body},
            # label-15 + label-80 (receive time) for the airframe, earlier and later
            {"ts": recv - 900, "icao_hex": self.HEX, "label": "15",
             "body": "FST01EGLLEPWAN52232E0207997 36  33  51     35262      188  10531050"},
            {"ts": recv + 300, "icao_hex": self.HEX, "label": "80",
             "body": "3N01 POSRPT 0872/19 LGAV/EPWA .SX-DVX\r\n/POS N52141E020397/ALT +05475/MCH 284/FOB 0032/ETA 1702"},
            # coarse XID fix
            {"ts": recv - 300, "icao_hex": self.HEX, "label": "H1", "lat": 52.3, "lon": 20.5, "body": "x"},
            # another airframe, same window
            {"ts": recv, "icao_hex": "489200", "label": "H1", "sublabel": "DF", "body": body},
            {"ts": recv - 1, "icao_hex": "489200", "label": "15",
             "body": "FST01EGLLEPWAN52232E0207997 36  33  51     35262      188  10531050"},
            # same airframe, ABS received 2 h earlier: embedded fixes far outside the window
            {"ts": recv - 7200, "icao_hex": self.HEX, "label": "H1", "sublabel": "DF", "body": far_body},
            # superseded copy for the airframe
            {"ts": recv + 10, "icao_hex": self.HEX, "label": "H1", "sublabel": "DF", "body": body,
             "superseded": 1},
            # wrong-sublabel H1 body for the airframe
            {"ts": recv + 20, "icao_hex": self.HEX, "label": "H1", "sublabel": "M1", "body": body},
        ]
        conn, client = TestBatch5Positions._client(monkeypatch, rows)
        with client as c:
            c.recv, c.fix_ts, c.far_ts, c.now, c.conn = recv, fix_ts, far_ts, now, conn
            yield c
        conn.close()

    def test_happy_path_sorted_ascending_all_sources(self, fp):
        pts = self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)
        assert [p["source"] for p in pts].count("h1-abs") == 3
        assert {p["source"] for p in pts} == {"h1-abs", "label", "xid"}
        assert len(pts) == 3 + 1 + 1 + 1       # 3 ABS fixes + label 15 + label 80 + xid
        ts = [p["ts"] for p in pts]
        assert ts == sorted(ts)
        assert all(p["icao_hex"] == self.HEX for p in pts)
        abs_pts = [p for p in pts if p["source"] == "h1-abs"]
        assert sorted(p["ts"] for p in abs_pts) == sorted(fp.fix_ts)
        assert abs_pts[0]["alt"] == 2421 and abs_pts[0]["wind_dir"] == 301
        l80 = next(p for p in pts if p["label"] == "80")
        assert l80["alt"] == 5475 and l80["precise"] is True

    def test_other_icao_excluded(self, fp):
        pts = self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)
        assert not [p for p in pts if p["icao_hex"] != self.HEX]
        other = self._pts(fp, hexv="489200", since=fp.recv - 3600, until=fp.recv + 3600)
        assert {p["icao_hex"] for p in other} == {"489200"} and len(other) == 4

    def test_uppercase_and_tilde_hex_accepted(self, fp):
        assert len(self._pts(fp, hexv="489100".upper(), since=fp.recv - 3600, until=fp.recv + 3600)) == 6

    def test_fix_outside_window_excluded_by_embedded_ts(self, fp):
        # window around the far ABS message: only its 2nd and 3rd embedded fixes fall inside
        far = sorted(fp.far_ts)
        pts = self._pts(fp, since=far[1], until=far[2])
        assert sorted(p["ts"] for p in pts) == far[1:]
        # an ABS message received inside the window whose fixes fall before it
        pts = self._pts(fp, since=fp.fix_ts[1], until=fp.recv + 3600)
        abs_ts = sorted(p["ts"] for p in pts if p["source"] == "h1-abs")
        assert abs_ts == sorted(fp.fix_ts)[1:]

    def test_until_bound_excludes_later_points(self, fp):
        pts = self._pts(fp, since=fp.recv - 3600, until=fp.recv - 100)
        assert all(p["ts"] <= fp.recv - 100 for p in pts)
        assert not [p for p in pts if p["label"] == "80"]

    def test_superseded_and_wrong_sublabel_ignored(self, fp):
        pts = self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)
        assert len([p for p in pts if p["source"] == "h1-abs"]) == 3

    def test_bad_icao_404(self, fp):
        r = self._pts(fp, hexv="zzz", since=1, until=100, expect=404)
        assert r.status_code == 404

    def test_422_cases(self, fp):
        self._pts(fp, since=100, until=100, expect=422)
        self._pts(fp, since=200, until=100, expect=422)
        self._pts(fp, since=0, until=48 * 3600 + 1, expect=422)
        self._pts(fp, since=1000, until=1000 + 48 * 3600, expect=200)      # exactly 48 h ok
        self._pts(fp, expect=422)
        self._pts(fp, since=100, expect=422)
        self._pts(fp, until=100, expect=422)
        self._pts(fp, since=-1, until=100, expect=422)
        self._pts(fp, since=1, until=10_000_000_001, expect=422)
        self._pts(fp, since="abc", until=100, expect=422)

    def test_not_cached_and_map_cache_untouched(self, fp):
        from readsbstats import cache
        self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)
        assert not [k for k in cache._cache if "positions" in str(k)]
        # a new row is visible on the next call (no cache)
        vdl2_db.insert_messages(fp.conn, [
            {"ts": fp.recv + 400, "icao_hex": self.HEX, "label": "H1", "lat": 52.4, "lon": 20.4, "body": "y"}])
        fp.conn.commit()
        assert len(self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)) == 7

    def test_cap_and_ascending_order(self, fp, monkeypatch):
        monkeypatch.setattr(vdl2_api, "_FLIGHT_POSITIONS_CAP", 3)
        pts = self._pts(fp, since=fp.recv - 3600, until=fp.recv + 3600)
        assert len(pts) == 3
        ts = [p["ts"] for p in pts]
        assert ts == sorted(ts)
        assert ts[-1] == fp.recv + 300           # the newest are kept

    def test_empty_window(self, fp):
        assert self._pts(fp, since=1000, until=2000) == []

    def test_every_candidate_query_uses_icao_index(self, monkeypatch):
        conn = make_vdl2_db()
        assert set(vdl2_api._SQL_FLIGHT) == set(vdl2_api._SQL_MAP)
        for name, sql in vdl2_api._SQL_FLIGHT.items():
            plan = " ".join(r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, [0] * sql.count("?")))
            assert "idx_vdl2_icao" in plan, (name, plan)
        conn.close()

    def test_map_sql_has_no_icao_clause(self):
        for sql in vdl2_api._SQL_MAP.values():
            assert "icao_hex = ?" not in sql
        assert vdl2_api._H1_POSITIONS_SQL == vdl2_api._SQL_MAP["h1"]


class TestOooiH1Routes:
    """Batch 7: abs / b44 / r64 / fst01 route sources, below every existing source."""
    ABS = "ABS026BA_S      ,B7378MAX ,261123,LO4TP ,EPWA,LROP,0318,BCG27-S200-0011\r\nN5210.5,E02055.2,231035,01973,-02.8,286,026,IC,00000,0,\r\n"
    B44 = "B44C\r\nSP-LVH  4824HT EPWALPPTTO140926122650\r\n 995398160B70260B711"
    R64 = ("R641V0106,180302,, F-HPND,5318410-07,736661,736834,06164,EPWA,LFPG,17:43:03,"
           "2026:10:11,AF1047  ,000004,200186,ITT_#10100")
    FST = "FST01EGLLEPWAN52232E0207997 36  33  51     35262      188  10531050"
    HEX = "48e95f"

    def _route(self, monkeypatch, rows):
        now = int(time.time())
        recs = [{"ts": now - 600 + i * 10, "icao_hex": self.HEX, **row} for i, row in enumerate(rows)]
        helper = TestOooi()
        vconn, app = helper._make(monkeypatch, recs)
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/oooi/{self.HEX}").json()
        vconn.close()
        return data

    def _rows(self, which):
        table = {
            "abs": {"label": "H1", "sublabel": "DF", "body": self.ABS},
            "b44": {"label": "H1", "sublabel": "DF", "body": self.B44},
            "r64": {"label": "H1", "sublabel": "T8", "body": self.R64},
            "fst01": {"label": "15", "body": self.FST},
        }
        return [table[w] for w in which]

    @pytest.mark.parametrize("src,dep,dest", [
        ("abs", "EPWA", "LROP"), ("b44", "EPWA", "LPPT"),
        ("r64", "EPWA", "LFPG"), ("fst01", "EGLL", "EPWA"),
    ])
    def test_each_source_parsed(self, monkeypatch, src, dep, dest):
        data = self._route(monkeypatch, self._rows([src]))
        assert data["route"] == {"dep_icao": dep, "dest_icao": dest, "flight": None, "source": src}
        assert data["dsta"] == dest
        assert data["has_oooi"] is False

    def test_order_among_new_sources(self, monkeypatch):
        order = ["r64", "abs", "b44", "fst01"]
        for i, winner in enumerate(order):
            data = self._route(monkeypatch, self._rows(list(reversed(order[i:]))))
            assert data["route"]["source"] == winner

    def test_existing_sources_win(self, monkeypatch):
        existing = {"label": "49", "body": "01DCAP    ETD159/090545OMAAEPWA"}
        data = self._route(monkeypatch,
                           self._rows(["r64", "abs", "b44", "fst01"]) + [existing])
        assert data["route"]["source"] == "label49"
        assert data["route"]["dest_icao"] == "EPWA"
        # existing one is OLDER than the new rows: still wins
        data = self._route(monkeypatch, [existing] + self._rows(["r64", "abs"]))
        assert data["route"]["source"] == "label49"

    def test_route_fills_event_gap(self, monkeypatch):
        rows = [{"label": "H1", "body": "DEP / OT 0030"}] + self._rows(["r64"])
        data = self._route(monkeypatch, rows)
        assert data["dep"]["dep_icao"] == "EPWA" and data["dep"]["dest_icao"] == "LFPG"

    def test_wrong_sublabel_ignored(self, monkeypatch):
        rows = [{"label": "H1", "sublabel": "T8", "body": self.ABS},
                {"label": "H1", "sublabel": "DF", "body": self.R64},
                {"label": "H1", "body": self.B44},
                {"label": "16", "body": self.FST}]
        data = self._route(monkeypatch, rows)
        assert "route" not in data

    def test_superseded_ignored(self, monkeypatch):
        rows = self._rows(["r64"])
        rows[0]["superseded"] = 1
        rows += self._rows(["fst01"])
        data = self._route(monkeypatch, rows)
        assert data["route"]["source"] == "fst01"

    def test_newest_row_per_source_wins(self, monkeypatch):
        old = {"label": "H1", "sublabel": "T8", "body": self.R64.replace("LFPG", "LFPO")}
        data = self._route(monkeypatch, [old] + self._rows(["r64"]))
        assert data["route"]["dest_icao"] == "LFPG"

    def test_oooi_query_is_icao_indexed_and_bounded(self):
        plan = " ".join(str(tuple(r)) for r in make_vdl2_db().execute(
            "EXPLAIN QUERY PLAN " + vdl2_api._OOOI_SQL.format(where="icao_hex = ? AND ts >= ?"),
            ["48e95f", 0, 1000]).fetchall())
        assert "idx_vdl2_icao" in plan
        assert "LIMIT" in vdl2_api._OOOI_SQL.upper()


class TestStatsGroundStations:
    """Batch 7: per-ground-station downlink stats, full stats payload only (last 24 h)."""

    @staticmethod
    def _row(ts, hexv, toaddr, freq, sig=-45.0, noise=-52.0, src="Aircraft", raw=None):
        import json
        if raw is None:
            raw = json.dumps({"vdl2": {"avlc": {"src": {"addr": hexv.upper(), "type": src},
                                                "dst": {"addr": toaddr, "type": "Ground station"}}}})
        return {"ts": ts, "icao_hex": hexv, "toaddr": toaddr, "freq": freq,
                "sig_level": sig, "noise_level": noise, "raw": raw}

    def _stats(self, monkeypatch, rows, path="/api/vdl2/stats"):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        vdl2_db.insert_messages(vconn, rows)
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            data = c.get(path).json()
        vconn.close()
        return data

    def test_aggregation(self, monkeypatch):
        now = int(time.time())
        rows = [
            self._row(now - 100, "aaaaaa", "2B86E1", 136.875, sig=-40, noise=-50),   # snr 10
            self._row(now - 90, "aaaaaa", "2B86E1", 136.875, sig=-40, noise=-48),    # snr 8
            self._row(now - 80, "bbbbbb", "2B86E1", 136.875, sig=-44, noise=-50),    # snr 6
            self._row(now - 70, "bbbbbb", "2B86E1", 136.875, sig=None, noise=None),  # no snr
            self._row(now - 60, "aaaaaa", "11920A", 136.725),                        # snr 7
            self._row(now - 50, "aaaaaa", "2B86E1", 136.7750001),                    # other freq
        ]
        data = self._stats(monkeypatch, rows)
        gs = data["ground_stations"]
        assert gs[0] == {"toaddr": "2B86E1", "freq": 136.875, "frames": 4, "aircraft": 2,
                         "snr_db": 8.0}
        assert {(g["toaddr"], g["freq"]): g["frames"] for g in gs} == {
            ("2B86E1", 136.875): 4, ("11920A", 136.725): 1, ("2B86E1", 136.775): 1}
        assert [g["frames"] for g in gs] == sorted((g["frames"] for g in gs), reverse=True)

    def test_snr_null_when_no_signal_data(self, monkeypatch):
        now = int(time.time())
        data = self._stats(monkeypatch, [self._row(now - 10, "aaaaaa", "2B86E1", 136.875,
                                                   sig=None, noise=None)])
        assert data["ground_stations"][0]["snr_db"] is None
        assert data["ground_stations"][0]["frames"] == 1

    def test_ground_sourced_and_non_dumpvdl2_rows_excluded(self, monkeypatch):
        now = int(time.time())
        rows = [
            self._row(now - 10, "aaaaaa", "2B86E1", 136.875),
            self._row(now - 10, "aaaaaa", "2B86E1", 136.875, src="Ground station"),
            {"ts": now - 10, "icao_hex": "cccccc", "toaddr": "2B86E1", "freq": 136.875,
             "raw": None},                                         # vdlm2dec-style: no avlc
            {"ts": now - 10, "icao_hex": "cccccc", "toaddr": "2B86E1", "freq": 136.875,
             "raw": '{"vdl2": {}}'},
        ]
        gs = self._stats(monkeypatch, rows)["ground_stations"]
        assert len(gs) == 1 and gs[0]["frames"] == 1 and gs[0]["aircraft"] == 1

    def test_invalid_json_row_tolerated(self, monkeypatch):
        now = int(time.time())
        rows = [self._row(now - 10, "aaaaaa", "2B86E1", 136.875),
                self._row(now - 10, "bbbbbb", "2B86E1", 136.875, raw='{"vdl2":{"avlc":{"src":{"typ')]
        data = self._stats(monkeypatch, rows)
        assert data["ground_stations"][0]["frames"] == 1

    def test_only_last_24h(self, monkeypatch):
        now = int(time.time())
        rows = [self._row(now - 86400 - 60, "aaaaaa", "OLD001", 136.875),
                self._row(now - 3600, "aaaaaa", "NEW001", 136.875)]
        gs = self._stats(monkeypatch, rows)["ground_stations"]
        assert [g["toaddr"] for g in gs] == ["NEW001"]

    def test_limit_30(self, monkeypatch):
        now = int(time.time())
        rows = [self._row(now - 10, "aaaaaa", f"GS{i:04d}", 136.875) for i in range(35)]
        assert len(self._stats(monkeypatch, rows)["ground_stations"]) == 30

    def test_empty_default(self, monkeypatch):
        assert self._stats(monkeypatch, [])["ground_stations"] == []

    def test_summary_excludes_it(self, monkeypatch):
        now = int(time.time())
        data = self._stats(monkeypatch, [self._row(now - 10, "aaaaaa", "2B86E1", 136.875)],
                           "/api/vdl2/stats?summary=true")
        assert set(data) == {"total", "last_hour", "aircraft"}

    def test_query_uses_ts_index(self):
        plan = " ".join(str(tuple(r)) for r in make_vdl2_db().execute(
            "EXPLAIN QUERY PLAN " + vdl2_api._GROUND_STATIONS_SQL, (0,)).fetchall())
        assert "idx_vdl2_ts" in plan
        assert "SCAN vdl2_messages" not in plan.replace("USING INDEX", "")


class TestPositionsParserFailureIsolated:
    """A parser raising on one malformed radio body must not 500 the whole response
    (review finding 2026-11-23: an ABS line with '--' raised ValueError)."""

    def test_one_raising_row_keeps_other_points(self, monkeypatch):
        from readsbstats.vdl2 import h1rep as _h1rep
        recv = int(time.time()) - 30
        good, _ = TestBatch5Positions._csv_body(recv)
        conn, client = TestBatch5Positions._client(monkeypatch, [
            {"ts": recv, "icao_hex": "489001", "label": "H1", "sublabel": "DF", "body": good},
            {"ts": recv, "icao_hex": "489002", "label": "H1", "sublabel": "DF", "body": "ABS boom"},
        ])
        real = _h1rep.parse_positions

        def flaky(sub, body, ts):
            if body == "ABS boom":
                raise ValueError("malformed")
            return real(sub, body, ts)

        monkeypatch.setattr(_h1rep, "parse_positions", flaky)
        with client as c:
            r = c.get("/api/vdl2/positions?minutes=60")
            r2 = c.get(f"/api/vdl2/positions/489002?since={recv - 600}&until={recv + 600}")
        conn.close()
        assert r.status_code == 200
        assert {p["icao_hex"] for p in r.json()["points"]} == {"489001"}
        assert r2.status_code == 200 and r2.json()["points"] == []
