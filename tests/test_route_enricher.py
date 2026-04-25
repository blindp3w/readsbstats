"""
Tests for route_enricher.py — adsbdb.com callsign-to-route lookup and caching.
Uses an in-memory SQLite database; no real network I/O.
"""

import sqlite3
import time

import pytest

from readsbstats import config, database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def insert_flight(conn, *, icao="aabbcc", callsign="LOT123",
                  first_seen=1_000_000, last_seen=1_003_600, active=False):
    cur = conn.execute(
        """INSERT INTO flights
           (icao_hex, callsign, first_seen, last_seen, total_positions,
            lat_min, lat_max, lon_min, lon_max)
           VALUES (?,?,?,?,10,0,0,0,0)""",
        (icao, callsign, first_seen, last_seen),
    )
    fid = cur.lastrowid
    if active:
        conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            (icao, fid, last_seen),
        )
    conn.commit()
    return fid


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.parse = route_enricher._parse_response
        yield

    def test_valid_response_returns_dict(self):
        data = {"response": {"flightroute": {
            "callsign": "LOT123",
            "origin": {
                "icao_code": "WAW", "iata_code": "WAW",
                "name": "Warsaw Chopin Airport", "country": "Poland",
                "latitude": 52.1657, "longitude": 20.9671,
            },
            "destination": {
                "icao_code": "LHR", "iata_code": "LHR",
                "name": "London Heathrow Airport", "country": "United Kingdom",
                "latitude": 51.4775, "longitude": -0.4614,
            },
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] == "WAW"
        assert result["dest_icao"] == "LHR"
        assert result["origin_name"] == "Warsaw Chopin Airport"
        assert result["dest_name"] == "London Heathrow Airport"
        assert result["origin_lat"] == pytest.approx(52.1657)
        assert result["dest_lon"] == pytest.approx(-0.4614)

    def test_missing_response_key_returns_none(self):
        assert self.parse({}) is None
        assert self.parse({"response": {}}) is None

    def test_empty_flightroute_returns_none(self):
        assert self.parse({"response": {"flightroute": None}}) is None
        assert self.parse({"response": {"flightroute": {}}}) is None

    def test_origin_only_still_parsed(self):
        data = {"response": {"flightroute": {
            "origin": {"icao_code": "WAW", "name": "Warsaw", "country": "Poland",
                       "iata_code": "WAW", "latitude": 52.1, "longitude": 20.9},
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] == "WAW"
        assert result["dest_icao"] is None

    def test_malformed_json_returns_none(self):
        assert self.parse("not a dict") is None
        assert self.parse(None) is None
        assert self.parse({"response": "wrong"}) is None


# ---------------------------------------------------------------------------
# DB helpers: _store_route, _is_confirmed_unknown
# ---------------------------------------------------------------------------

class TestStoreRoute:
    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.re = route_enricher
        self.conn = make_db()
        yield
        self.conn.close()

    def test_store_resolved_route_inserts_callsign_routes(self):
        route = {
            "origin_icao": "WAW", "origin_iata": "WAW",
            "origin_name": "Warsaw Chopin Airport", "origin_country": "Poland",
            "origin_lat": 52.1657, "origin_lon": 20.9671,
            "dest_icao": "LHR", "dest_iata": "LHR",
            "dest_name": "London Heathrow Airport", "dest_country": "United Kingdom",
            "dest_lat": 51.4775, "dest_lon": -0.4614,
        }
        self.re._store_route(self.conn, "LOT123", route)

        row = self.conn.execute(
            "SELECT * FROM callsign_routes WHERE callsign = 'LOT123'"
        ).fetchone()
        assert row is not None
        assert row["origin_icao"] == "WAW"
        assert row["dest_icao"] == "LHR"

    def test_store_resolved_route_inserts_airports(self):
        route = {
            "origin_icao": "WAW", "origin_iata": "WAW",
            "origin_name": "Warsaw Chopin Airport", "origin_country": "Poland",
            "origin_lat": 52.1657, "origin_lon": 20.9671,
            "dest_icao": "LHR", "dest_iata": "LHR",
            "dest_name": "London Heathrow Airport", "dest_country": "United Kingdom",
            "dest_lat": 51.4775, "dest_lon": -0.4614,
        }
        self.re._store_route(self.conn, "LOT123", route)

        waw = self.conn.execute(
            "SELECT * FROM airports WHERE icao_code = 'WAW'"
        ).fetchone()
        assert waw is not None
        assert waw["name"] == "Warsaw Chopin Airport"
        lhr = self.conn.execute(
            "SELECT * FROM airports WHERE icao_code = 'LHR'"
        ).fetchone()
        assert lhr is not None
        assert lhr["country"] == "United Kingdom"

    def test_store_none_marks_confirmed_unknown(self):
        self.re._store_route(self.conn, "UNKN123", None)
        row = self.conn.execute(
            "SELECT * FROM callsign_routes WHERE callsign = 'UNKN123'"
        ).fetchone()
        assert row is not None
        assert row["origin_icao"] is None
        assert row["dest_icao"] is None

    def test_is_confirmed_unknown_fresh(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        self.re._store_route(self.conn, "UNKN123", None)
        assert self.re._is_confirmed_unknown(self.conn, "UNKN123") is True

    def test_is_confirmed_unknown_expired(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 1)
        old_ts = int(time.time()) - 2 * 86400  # 2 days ago
        self.conn.execute(
            "INSERT INTO callsign_routes (callsign, origin_icao, dest_icao, fetched_at) VALUES (?,NULL,NULL,?)",
            ("OLD123", old_ts),
        )
        self.conn.commit()
        assert self.re._is_confirmed_unknown(self.conn, "OLD123") is False

    def test_is_confirmed_unknown_for_resolved_route_returns_false(self):
        route = {
            "origin_icao": "WAW", "origin_iata": "WAW",
            "origin_name": "Warsaw", "origin_country": "Poland",
            "origin_lat": 52.0, "origin_lon": 21.0,
            "dest_icao": "LHR", "dest_iata": "LHR",
            "dest_name": "Heathrow", "dest_country": "UK",
            "dest_lat": 51.0, "dest_lon": -0.5,
        }
        self.re._store_route(self.conn, "LOT123", route)
        assert self.re._is_confirmed_unknown(self.conn, "LOT123") is False

    def test_store_upserts_on_duplicate(self):
        self.re._store_route(self.conn, "LOT123", None)
        route = {
            "origin_icao": "WAW", "origin_iata": "WAW",
            "origin_name": "Warsaw", "origin_country": "Poland",
            "origin_lat": 52.0, "origin_lon": 21.0,
            "dest_icao": "LHR", "dest_iata": "LHR",
            "dest_name": "Heathrow", "dest_country": "UK",
            "dest_lat": 51.0, "dest_lon": -0.5,
        }
        self.re._store_route(self.conn, "LOT123", route)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM callsign_routes WHERE callsign='LOT123'"
        ).fetchone()[0]
        assert count == 1
        row = self.conn.execute(
            "SELECT origin_icao FROM callsign_routes WHERE callsign='LOT123'"
        ).fetchone()
        assert row["origin_icao"] == "WAW"


# ---------------------------------------------------------------------------
# _apply_to_flights
# ---------------------------------------------------------------------------

class TestApplyToFlights:
    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.re = route_enricher
        self.conn = make_db()
        yield
        self.conn.close()

    def test_apply_sets_origin_dest_on_matching_flights(self):
        insert_flight(self.conn, icao="aabbcc", callsign="LOT123")
        insert_flight(self.conn, icao="ddeeff", callsign="LOT123")  # same callsign, different aircraft
        route = {
            "origin_icao": "WAW", "dest_icao": "LHR",
            "origin_iata": None, "origin_name": None, "origin_country": None,
            "origin_lat": None, "origin_lon": None,
            "dest_iata": None, "dest_name": None, "dest_country": None,
            "dest_lat": None, "dest_lon": None,
        }
        self.re._apply_to_flights(self.conn, "LOT123", route)

        rows = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM flights WHERE callsign = 'LOT123'"
        ).fetchall()
        assert all(r["origin_icao"] == "WAW" for r in rows)
        assert all(r["dest_icao"] == "LHR" for r in rows)

    def test_apply_none_leaves_nulls(self):
        insert_flight(self.conn, callsign="UNKN123")
        self.re._apply_to_flights(self.conn, "UNKN123", None)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM flights WHERE callsign='UNKN123'"
        ).fetchone()
        assert row["origin_icao"] is None
        assert row["dest_icao"] is None

    def test_apply_does_not_touch_other_callsigns(self):
        insert_flight(self.conn, icao="aabbcc", callsign="LOT123")
        insert_flight(self.conn, icao="ddeeff", callsign="RYR456")
        route = {
            "origin_icao": "WAW", "dest_icao": "LHR",
            "origin_iata": None, "origin_name": None, "origin_country": None,
            "origin_lat": None, "origin_lon": None,
            "dest_iata": None, "dest_name": None, "dest_country": None,
            "dest_lat": None, "dest_lon": None,
        }
        self.re._apply_to_flights(self.conn, "LOT123", route)
        row = self.conn.execute(
            "SELECT origin_icao FROM flights WHERE callsign='RYR456'"
        ).fetchone()
        assert row["origin_icao"] is None


# ---------------------------------------------------------------------------
# _enrich_batch
# ---------------------------------------------------------------------------

class TestEnrichBatch:
    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.re = route_enricher
        self.conn = make_db()
        yield
        self.conn.close()

    def _mock_route(self, origin="WAW", dest="LHR"):
        return {
            "origin_icao": origin, "origin_iata": origin,
            "origin_name": f"{origin} Airport", "origin_country": "Country",
            "origin_lat": 52.0, "origin_lon": 21.0,
            "dest_icao": dest, "dest_iata": dest,
            "dest_name": f"{dest} Airport", "dest_country": "Country",
            "dest_lat": 51.0, "dest_lon": -0.5,
        }

    def test_enriches_closed_flight_with_callsign(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="LOT123", active=False)
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: self._mock_route())

        count = self.re._enrich_batch(self.conn)

        assert count == 1
        row = self.conn.execute(
            "SELECT origin_icao FROM flights WHERE callsign='LOT123'"
        ).fetchone()
        assert row["origin_icao"] == "WAW"

    def test_skips_active_flight(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="LOT123", active=True)
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or self._mock_route())

        self.re._enrich_batch(self.conn)

        assert len(calls) == 0

    def test_skips_flight_without_callsign(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign=None)
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)

        self.re._enrich_batch(self.conn)

        assert len(calls) == 0

    def test_skips_already_resolved(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="LOT123")
        # Store a fresh resolved entry
        self.re._store_route(self.conn, "LOT123", self._mock_route())
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or self._mock_route())

        self.re._enrich_batch(self.conn)

        assert len(calls) == 0

    def test_skips_confirmed_unknown_within_window(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="UNKN123")
        self.re._store_route(self.conn, "UNKN123", None)
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)

        self.re._enrich_batch(self.conn)

        assert len(calls) == 0

    def test_retries_expired_unknown(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 1)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="OLD123")
        old_ts = int(time.time()) - 2 * 86400
        self.conn.execute(
            "INSERT INTO callsign_routes (callsign, origin_icao, dest_icao, fetched_at) VALUES (?,NULL,NULL,?)",
            ("OLD123", old_ts),
        )
        self.conn.commit()
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)

        self.re._enrich_batch(self.conn)

        assert "OLD123" in calls

    def test_batch_size_limits_api_calls(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 2)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        for i in range(5):
            insert_flight(self.conn, icao=f"aa00{i:02d}", callsign=f"TST{i:03d}")
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)

        count = self.re._enrich_batch(self.conn)

        assert count == 2
        assert len(calls) == 2

    def test_network_failure_does_not_store_confirmed_unknown(self, monkeypatch):
        """A transient network error must NOT write a NULL sentinel to callsign_routes.

        Bug: _fetch_route catches all exceptions and returns None.  _enrich_batch then
        calls _store_route(conn, cs, None) which writes a NULL sentinel — blacklisting
        the callsign for ROUTE_CACHE_DAYS (30 days) even though the API was just
        unreachable.  We test through _fetch_route by mocking httpx.Client directly.
        """
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="ERR123")

        import httpx

        class _FailingClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def get(self, *a, **kw): raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _FailingClient())
        self.re._enrich_batch(self.conn)

        # No sentinel should have been written — callsign remains unresolved
        row = self.conn.execute(
            "SELECT * FROM callsign_routes WHERE callsign='ERR123'"
        ).fetchone()
        assert row is None, "network failure must not store a confirmed-unknown sentinel"

    def test_network_failure_callsign_retried_next_batch(self, monkeypatch):
        """After a network failure the callsign must be retried in the next batch."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="ERR123")

        import httpx

        class _FailingClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def get(self, *a, **kw): raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _FailingClient())
        self.re._enrich_batch(self.conn)

        # Now API recovers — callsign must appear in the next batch
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)
        self.re._enrich_batch(self.conn)
        assert "ERR123" in calls, "callsign not retried after transient failure"

    def test_transient_failures_logged_as_warning_at_batch_level(self, monkeypatch, caplog):
        """When any callsign in a batch fails transiently, a WARNING must be emitted
        once for the batch — not silently swallowed at DEBUG."""
        import logging
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, icao="aa0001", callsign="ERR001")
        insert_flight(self.conn, icao="aa0002", callsign="ERR002")

        import httpx

        class _FailingClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def get(self, *a, **kw): raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _FailingClient())

        # propagate=False means caplog must attach to the logger itself, not root
        with caplog.at_level(logging.WARNING, logger="route_enricher"):
            self.re.log.propagate = True
            try:
                self.re._enrich_batch(self.conn)
            finally:
                self.re.log.propagate = False

        assert any(r.levelno >= logging.WARNING for r in caplog.records), \
            "expected at least one WARNING when batch encounters transient errors"

    def test_no_warning_when_all_succeed(self, monkeypatch, caplog):
        """A fully successful batch must not emit any WARNING."""
        import logging
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="LOT123")
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: None)  # 404-style: no route but no error

        with caplog.at_level(logging.WARNING, logger="route_enricher"):
            self.re.log.propagate = True
            try:
                self.re._enrich_batch(self.conn)
            finally:
                self.re.log.propagate = False

        assert not any(r.levelno >= logging.WARNING for r in caplog.records), \
            "successful batch must not produce WARNING"

    def test_multiple_flights_same_callsign_resolved_once(self, monkeypatch):
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, icao="aa0001", callsign="LOT123")
        insert_flight(self.conn, icao="aa0002", callsign="LOT123")
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or self._mock_route())

        self.re._enrich_batch(self.conn)

        # DISTINCT in the query — only one API call
        assert calls.count("LOT123") == 1
        # But both flights updated
        rows = self.conn.execute(
            "SELECT origin_icao FROM flights WHERE callsign='LOT123'"
        ).fetchall()
        assert all(r["origin_icao"] == "WAW" for r in rows)


# ---------------------------------------------------------------------------
# _fetch_route() — HTTP interaction
# ---------------------------------------------------------------------------

class TestFetchRoute:
    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.mod = route_enricher
        yield

    def test_returns_parsed_route_on_200(self, monkeypatch):
        import httpx

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"response": {"flightroute": {
                    "origin": {"icao_code": "WAW", "iata_code": "WAW", "name": "Warsaw"},
                    "destination": {"icao_code": "LHR", "iata_code": "LHR", "name": "Heathrow"},
                }}}

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        result = self.mod._fetch_route("LOT281")
        assert result["origin_icao"] == "WAW"
        assert result["dest_icao"] == "LHR"

    def test_returns_none_on_404(self, monkeypatch):
        import httpx

        class FakeResp:
            status_code = 404

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        result = self.mod._fetch_route("UNKNOWN")
        assert result is None

    def test_raises_transient_on_network_error(self, monkeypatch):
        import httpx

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._TransientError):
            self.mod._fetch_route("LOT123")

    def test_raises_transient_on_http_500(self, monkeypatch):
        import httpx

        class FakeResp:
            status_code = 500
            def raise_for_status(self):
                raise httpx.HTTPStatusError("500", request=None, response=self)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._TransientError):
            self.mod._fetch_route("LOT123")


# ---------------------------------------------------------------------------
# run_enricher_loop
# ---------------------------------------------------------------------------

class TestRunEnricherLoop:
    def test_loop_calls_enrich_batch_and_sleeps(self, monkeypatch, tmp_path):
        from readsbstats import route_enricher
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)

        calls = []

        def fake_enrich(conn):
            calls.append(1)
            if len(calls) >= 2:
                raise KeyboardInterrupt()
            return 0

        monkeypatch.setattr(route_enricher, "_enrich_batch", fake_enrich)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(config, "ROUTE_ENRICH_INTERVAL", 0)

        with pytest.raises(KeyboardInterrupt):
            route_enricher.run_enricher_loop(db_path)
        assert len(calls) == 2
