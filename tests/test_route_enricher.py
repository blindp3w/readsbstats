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

from tests._helpers import make_db  # noqa: E402 — kept under section header


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
                "icao_code": "EPWA", "iata_code": "WAW",
                "name": "Warsaw Chopin Airport", "country": "Poland",
                "latitude": 52.1657, "longitude": 20.9671,
            },
            "destination": {
                "icao_code": "EGLL", "iata_code": "LHR",
                "name": "London Heathrow Airport", "country": "United Kingdom",
                "latitude": 51.4775, "longitude": -0.4614,
            },
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] == "EPWA"
        assert result["dest_icao"] == "EGLL"
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
            "origin": {"icao_code": "EPWA", "name": "Warsaw", "country": "Poland",
                       "iata_code": "WAW", "latitude": 52.1, "longitude": 20.9},
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] == "EPWA"
        assert result["dest_icao"] is None

    def test_malformed_json_returns_none(self):
        assert self.parse("not a dict") is None
        assert self.parse(None) is None
        assert self.parse({"response": "wrong"}) is None

    # F04: adsbdb airport fields are validated at the trust boundary.

    def test_bad_icao_code_rejected(self):
        # An over-length / non-4-char ICAO code must be dropped, not stored.
        data = {"response": {"flightroute": {
            "origin": {"icao_code": "INVALID8", "iata_code": "WAW",
                       "name": "Warsaw", "country": "Poland",
                       "latitude": 52.1, "longitude": 20.9},
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] is None
        # The rest of the (valid) origin fields survive.
        assert result["origin_iata"] == "WAW"
        assert result["origin_name"] == "Warsaw"

    def test_out_of_range_latitude_rejected(self):
        data = {"response": {"flightroute": {
            "origin": {"icao_code": "EPWA", "iata_code": "WAW",
                       "name": "Warsaw", "country": "Poland",
                       "latitude": 95, "longitude": 20.9},
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_lat"] is None
        assert result["origin_lon"] == pytest.approx(20.9)
        assert result["origin_icao"] == "EPWA"

    def test_well_formed_payload_preserved_exactly(self):
        data = {"response": {"flightroute": {
            "origin": {
                "icao_code": "EPWA", "iata_code": "WAW",
                "name": "Warsaw Chopin Airport", "country": "Poland",
                "latitude": 52.1657, "longitude": 20.9671,
            },
            "destination": {
                "icao_code": "EGLL", "iata_code": "LHR",
                "name": "London Heathrow Airport", "country": "United Kingdom",
                "latitude": 51.4775, "longitude": -0.4614,
            },
        }}}
        result = self.parse(data)
        assert result is not None
        assert result["origin_icao"] == "EPWA"
        assert result["origin_iata"] == "WAW"
        assert result["origin_name"] == "Warsaw Chopin Airport"
        assert result["origin_country"] == "Poland"
        assert result["origin_lat"] == pytest.approx(52.1657)
        assert result["origin_lon"] == pytest.approx(20.9671)
        assert result["dest_icao"] == "EGLL"
        assert result["dest_iata"] == "LHR"
        assert result["dest_name"] == "London Heathrow Airport"
        assert result["dest_country"] == "United Kingdom"
        assert result["dest_lat"] == pytest.approx(51.4775)
        assert result["dest_lon"] == pytest.approx(-0.4614)


# ---------------------------------------------------------------------------
# DB helpers: _store_route
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

    def test_store_partial_dest_none_preserves_cached_dest(self):
        # Audit 2026-05-25: a later origin-only response must not wipe a
        # previously-cached dest in callsign_routes.
        full = {
            "origin_icao": "EPWA", "origin_iata": None, "origin_name": None,
            "origin_country": None, "origin_lat": None, "origin_lon": None,
            "dest_icao": "EGLL", "dest_iata": None, "dest_name": None,
            "dest_country": None, "dest_lat": None, "dest_lon": None,
        }
        self.re._store_route(self.conn, "PART123", full)
        partial = dict(full, dest_icao=None)
        self.re._store_route(self.conn, "PART123", partial)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM callsign_routes WHERE callsign='PART123'"
        ).fetchone()
        assert row["origin_icao"] == "EPWA"
        assert row["dest_icao"] == "EGLL"  # preserved

    def test_store_partial_origin_none_preserves_cached_origin(self):
        full = {
            "origin_icao": "EPWA", "origin_iata": None, "origin_name": None,
            "origin_country": None, "origin_lat": None, "origin_lon": None,
            "dest_icao": "EGLL", "dest_iata": None, "dest_name": None,
            "dest_country": None, "dest_lat": None, "dest_lon": None,
        }
        self.re._store_route(self.conn, "PART456", full)
        partial = dict(full, origin_icao=None)
        self.re._store_route(self.conn, "PART456", partial)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM callsign_routes WHERE callsign='PART456'"
        ).fetchone()
        assert row["origin_icao"] == "EPWA"  # preserved
        assert row["dest_icao"] == "EGLL"

    def test_store_none_clears_negative_cache_with_null_null(self):
        # The `route is None` negative-cache branch is unchanged: a confirmed
        # 404 still writes NULL,NULL,fetched_at. Asserted separately because
        # the upsert change could accidentally inherit the cached partial
        # path.
        self.re._store_route(self.conn, "NEG123", None)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM callsign_routes WHERE callsign='NEG123'"
        ).fetchone()
        assert row is not None
        assert row["origin_icao"] is None
        assert row["dest_icao"] is None


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

    def test_apply_none_does_not_overwrite_existing_origin_dest(self):
        # Audit-13 A13-003: previously, a flight that had been resolved at
        # T1 would be silently wiped when adsbdb returned 404 at T2 (route
        # dropped upstream). _apply_to_flights now skips the UPDATE on
        # `route is None` — negative results stay in `callsign_routes`
        # only, never propagate to `flights`.
        fid = insert_flight(self.conn, callsign="OVER123")
        self.conn.execute(
            "UPDATE flights SET origin_icao = 'EPWA', dest_icao = 'EGLL' WHERE id = ?",
            (fid,),
        )
        self.conn.commit()
        self.re._apply_to_flights(self.conn, "OVER123", None)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM flights WHERE callsign='OVER123'"
        ).fetchone()
        assert row["origin_icao"] == "EPWA"
        assert row["dest_icao"] == "EGLL"

    def test_apply_partial_dest_none_preserves_existing_dest(self):
        # Audit 2026-05-25: _parse_response accepts origin-only payloads as
        # valid routes (test_origin_only_still_parsed). Before the COALESCE
        # fix this would clobber a previously-resolved dest with NULL.
        fid = insert_flight(self.conn, callsign="PART123")
        self.conn.execute(
            "UPDATE flights SET origin_icao = 'EPWA', dest_icao = 'EGLL' WHERE id = ?",
            (fid,),
        )
        self.conn.commit()
        route = {
            "origin_icao": "EPWA", "dest_icao": None,
            "origin_iata": None, "origin_name": None, "origin_country": None,
            "origin_lat": None, "origin_lon": None,
            "dest_iata": None, "dest_name": None, "dest_country": None,
            "dest_lat": None, "dest_lon": None,
        }
        self.re._apply_to_flights(self.conn, "PART123", route)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM flights WHERE callsign='PART123'"
        ).fetchone()
        assert row["origin_icao"] == "EPWA"
        assert row["dest_icao"] == "EGLL"  # preserved, not NULL'd

    def test_apply_partial_origin_none_preserves_existing_origin(self):
        fid = insert_flight(self.conn, callsign="PART456")
        self.conn.execute(
            "UPDATE flights SET origin_icao = 'EPWA', dest_icao = 'EGLL' WHERE id = ?",
            (fid,),
        )
        self.conn.commit()
        route = {
            "origin_icao": None, "dest_icao": "EGLL",
            "origin_iata": None, "origin_name": None, "origin_country": None,
            "origin_lat": None, "origin_lon": None,
            "dest_iata": None, "dest_name": None, "dest_country": None,
            "dest_lat": None, "dest_lon": None,
        }
        self.re._apply_to_flights(self.conn, "PART456", route)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM flights WHERE callsign='PART456'"
        ).fetchone()
        assert row["origin_icao"] == "EPWA"  # preserved, not NULL'd
        assert row["dest_icao"] == "EGLL"


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

    def test_skips_malformed_callsigns(self, monkeypatch):
        """BE-8 (Audit 2026-05-31): only well-formed callsigns are sent to the
        upstream route API. A 1-char, >8-char, or non-alphanumeric-leading
        callsign is junk (truncation artifacts, abuse) and must be skipped so
        it doesn't waste adsbdb.com calls.

        PY-9 (Audit 2026-05-31): also reject callsigns with non-alphanumeric
        chars anywhere in the middle. The original GLOB `[A-Z0-9]*` only
        validated the first character; `LOT/123` would slip through.
        """
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 50)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, icao="aaaa01", callsign="A")          # too short
        insert_flight(self.conn, icao="aaaa02", callsign="TOOLONG12")  # 9 chars, too long
        insert_flight(self.conn, icao="aaaa03", callsign="@BADCS")     # bad leading char
        insert_flight(self.conn, icao="aaaa04", callsign="LOT123")     # valid
        # PY-9 mid-string non-alphanumerics:
        insert_flight(self.conn, icao="aaaa05", callsign="LOT/123")    # slash mid
        insert_flight(self.conn, icao="aaaa06", callsign="AB-CD")      # dash mid
        insert_flight(self.conn, icao="aaaa07", callsign="AB CD")      # space mid
        insert_flight(self.conn, icao="aaaa08", callsign="AB?CD")      # question mid
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route",
                            lambda cs: calls.append(cs) or self._mock_route())

        self.re._enrich_batch(self.conn)

        assert calls == ["LOT123"]

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

    def test_apply_failure_rolls_back_route_cache(self, monkeypatch):
        """Audit 2026-05-26: _store_route and _apply_to_flights must
        commit atomically. Before the fix _store_route committed first,
        so a crash inside _apply_to_flights left callsign_routes claiming
        the callsign was freshly fetched while flights stayed stale.

        Verifies that an injected sqlite.Error from _apply_to_flights
        rolls back the callsign_routes insert too.
        """
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="LOT789", active=False)

        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: self._mock_route())

        def _boom(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated apply failure")
        monkeypatch.setattr(self.re, "_apply_to_flights", _boom)

        self.re._enrich_batch(self.conn)

        # Both sides should be empty: no route cached, no flight backfilled.
        cached = self.conn.execute(
            "SELECT * FROM callsign_routes WHERE callsign='LOT789'"
        ).fetchone()
        assert cached is None, (
            "callsign_routes was committed even though _apply_to_flights "
            "raised — atomicity contract violated"
        )

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

    # PY-8 (Audit 2026-05-31): http_safe.UnsafeURLError (policy violations —
    # redirect, size-cap, non-HTTPS, private-IP) is non-retryable. Map to a
    # new _PermanentError that the loop translates into a TTL-bounded
    # negative cache row, so the same broken URL isn't fetched every batch.

    def test_unsafe_url_error_raises_permanent(self, monkeypatch):
        """_fetch_route must surface http_safe.UnsafeURLError as
        _PermanentError, not _TransientError."""
        from readsbstats import http_safe

        class _PolicyFailClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def stream(self, *a, **kw):  # safe_httpx_get prefers .stream
                raise http_safe.UnsafeURLError("redirect blocked")
            def get(self, *a, **kw):
                raise http_safe.UnsafeURLError("redirect blocked")
        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _PolicyFailClient())

        with pytest.raises(self.re._PermanentError):
            self.re._fetch_route("LOT123")

    def test_permanent_failure_writes_negative_cache_row(self, monkeypatch):
        """When _fetch_route raises _PermanentError, _enrich_batch must
        persist a negative callsign_routes row so the existing TTL
        exclusion suppresses the same callsign for ROUTE_CACHE_DAYS."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="PERM123")

        from readsbstats import http_safe

        class _PolicyFailClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def stream(self, *a, **kw):
                raise http_safe.UnsafeURLError("redirect blocked")
            def get(self, *a, **kw):
                raise http_safe.UnsafeURLError("redirect blocked")
        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _PolicyFailClient())

        # First batch: hits the policy failure → writes negative cache row.
        self.re._enrich_batch(self.conn)
        row = self.conn.execute(
            "SELECT origin_icao, dest_icao FROM callsign_routes WHERE callsign='PERM123'"
        ).fetchone()
        assert row is not None
        assert row["origin_icao"] is None
        assert row["dest_icao"] is None
        # And the transient cooldown map must NOT have been populated —
        # permanent failures get a DB-backed TTL, not an in-memory cooldown.
        assert "PERM123" not in self.re._transient_failure_at

    def test_all_permanent_failures_emit_batch_summary(self, monkeypatch, caplog):
        """Code review follow-up: when every callsign in a batch hits
        _PermanentError, the batch-level summary WARNING must still fire.
        Before the fix, only `transient_failures` guarded the summary log,
        so an upstream API migration (every callsign permanent) produced
        only per-callsign WARNINGs and no batch summary — operators
        watching for the summary pattern saw nothing.
        """
        import logging

        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, icao="aa0001", callsign="PERM001")
        insert_flight(self.conn, icao="aa0002", callsign="PERM002")
        insert_flight(self.conn, icao="aa0003", callsign="PERM003")

        def _raise_permanent(*_a, **_kw):
            raise self.re._PermanentError("redirect blocked")
        monkeypatch.setattr(self.re, "_fetch_route", _raise_permanent)

        with caplog.at_level(logging.WARNING, logger="route_enricher"):
            self.re._enrich_batch(self.conn)

        # The batch-level summary must mention permanent failures.
        summary_msgs = [
            r.getMessage() for r in caplog.records
            if "skipped" in r.getMessage() and "batch" in r.getMessage().lower()
        ]
        assert summary_msgs, (
            "No batch-level summary WARNING emitted for an all-permanent batch. "
            f"WARNINGs seen: {[r.getMessage() for r in caplog.records]}"
        )

    def test_permanent_failure_skipped_on_next_batch(self, monkeypatch):
        """After a permanent failure writes the negative row, the cutoff
        query at the top of _enrich_batch must skip the callsign so the
        broken upstream URL isn't re-fetched every batch."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        insert_flight(self.conn, callsign="PERM456")

        from readsbstats import http_safe

        attempts = []

        class _PolicyFailClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def stream(self, *a, **kw):
                attempts.append(1)
                raise http_safe.UnsafeURLError("redirect blocked")
            def get(self, *a, **kw):
                attempts.append(1)
                raise http_safe.UnsafeURLError("redirect blocked")
        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _PolicyFailClient())

        self.re._enrich_batch(self.conn)
        assert len(attempts) == 1
        # Second batch — no new fetch attempt because the TTL skip kicks in.
        self.re._enrich_batch(self.conn)
        assert len(attempts) == 1, (
            "permanent-failure callsign was re-fetched on the next batch; "
            "negative-cache row's TTL must suppress it for ROUTE_CACHE_DAYS"
        )

    def test_network_failure_callsign_retried_after_cooldown(self, monkeypatch):
        """After the per-callsign cooldown elapses, a transient-failed
        callsign is retried (audit-12 #155). For the test we set cooldown=0
        so the retry happens on the very next batch — verifies the
        cooldown-aware logic doesn't permanently blacklist on transient
        errors."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        monkeypatch.setattr(self.re, "_TRANSIENT_COOLDOWN_S", 0)
        insert_flight(self.conn, callsign="ERR123")

        import httpx

        class _FailingClient:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def get(self, *a, **kw): raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(self.re.httpx, "Client", lambda **kw: _FailingClient())
        self.re._enrich_batch(self.conn)

        # Now API recovers — callsign must appear in the next batch (cooldown=0)
        calls = []
        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: calls.append(cs) or None)
        self.re._enrich_batch(self.conn)
        assert "ERR123" in calls, "callsign not retried after transient failure"

    def test_transient_failure_cooldown_skips_immediate_retry(self, monkeypatch):
        """Regression for audit-12 #155 — without a per-callsign cooldown,
        a multi-hour upstream outage would hammer the same N callsigns
        every batch interval. Now an in-memory cooldown skips a failed
        callsign on the next batch until _TRANSIENT_COOLDOWN_S elapses."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        # Realistic cooldown — keep the callsign blocked through the test
        monkeypatch.setattr(self.re, "_TRANSIENT_COOLDOWN_S", 300)
        insert_flight(self.conn, callsign="ERR456")

        attempt_count = {"n": 0}

        def failing_fetch(cs):
            attempt_count["n"] += 1
            raise self.re._TransientError("simulated transient failure")

        monkeypatch.setattr(self.re, "_fetch_route", failing_fetch)

        # First batch — callsign tried, fails, enters cooldown
        self.re._enrich_batch(self.conn)
        assert attempt_count["n"] == 1

        # Second batch immediately after — callsign in cooldown, must NOT
        # be tried again
        self.re._enrich_batch(self.conn)
        assert attempt_count["n"] == 1, (
            f"cooldown not honored — got {attempt_count['n']} fetch attempts "
            f"instead of 1"
        )

    def test_successful_fetch_clears_cooldown(self, monkeypatch):
        """A previously-failed callsign that succeeds must clear its
        cooldown so subsequent operational state isn't sticky."""
        monkeypatch.setattr(config, "ROUTE_CACHE_DAYS", 30)
        monkeypatch.setattr(config, "ROUTE_BATCH_SIZE", 10)
        monkeypatch.setattr(config, "ROUTE_RATE_LIMIT_SEC", 0.0)
        monkeypatch.setattr(self.re, "_TRANSIENT_COOLDOWN_S", 300)
        # Pre-seed the cooldown map for this callsign — pretend it failed earlier
        self.re._transient_failure_at["LOT123"] = 0.0  # in the past, expired
        insert_flight(self.conn, callsign="LOT123")

        monkeypatch.setattr(self.re, "_fetch_route", lambda cs: self._mock_route())
        self.re._enrich_batch(self.conn)

        # Success must remove the cooldown entry
        assert "LOT123" not in self.re._transient_failure_at

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

    def _bypass_validate(self, monkeypatch):
        """Return a fake DNS result so safe_httpx_get skips real DNS lookup."""
        import socket
        import urllib.parse
        from readsbstats import http_safe

        def fake_resolve(url):
            parsed = urllib.parse.urlparse(url)
            infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
            return parsed, infos

        monkeypatch.setattr(http_safe, "_resolve_and_validate", fake_resolve)

    def test_returns_parsed_route_on_200(self, monkeypatch):
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 200
            content = b'{"response":{"flightroute":{}}}'
            def raise_for_status(self): pass
            def json(self):
                return {"response": {"flightroute": {
                    "origin": {"icao_code": "EPWA", "iata_code": "WAW", "name": "Warsaw"},
                    "destination": {"icao_code": "EGLL", "iata_code": "LHR", "name": "Heathrow"},
                }}}

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        result = self.mod._fetch_route("LOT281")
        assert result["origin_icao"] == "EPWA"
        assert result["dest_icao"] == "EGLL"

    def test_returns_none_on_404(self, monkeypatch):
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 404
            content = b""

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        result = self.mod._fetch_route("UNKNOWN")
        assert result is None

    def test_raises_transient_on_network_error(self, monkeypatch):
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._TransientError):
            self.mod._fetch_route("LOT123")

    def test_raises_transient_on_http_500(self, monkeypatch):
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 500
            content = b""
            def raise_for_status(self):
                raise httpx.HTTPStatusError("500", request=None, response=self)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._TransientError):
            self.mod._fetch_route("LOT123")

    def test_redirect_treated_as_permanent(self, monkeypatch):
        """PY-8 (Audit 2026-05-31): a 3xx from adsbdb is a policy
        violation (safe_httpx_get raises UnsafeURLError) — surface as
        _PermanentError so the loop writes a TTL-bounded negative cache
        row instead of retrying every batch with the same broken URL."""
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 302
            content = b""
            headers = {"Location": "https://attacker.example/"}

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._PermanentError):
            self.mod._fetch_route("LOT123")

    def test_non_404_4xx_treated_as_permanent(self, monkeypatch):
        """Audit 17: a non-404 4xx (400/410/422) is a deterministic client
        error — surface as _PermanentError so the loop writes a TTL-bounded
        negative cache row instead of refetching the bad callsign every batch
        forever (the old code mapped it to _TransientError → infinite leak)."""
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 400
            content = b""
            def raise_for_status(self):
                raise httpx.HTTPStatusError("400", request=None, response=self)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._PermanentError):
            self.mod._fetch_route("BADCS")

    def test_429_treated_as_transient(self, monkeypatch):
        """429 (rate limited) stays transient — retry later via the in-memory
        cooldown, don't poison the DB cache for a temporary throttle."""
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 429
            content = b""
            def raise_for_status(self):
                raise httpx.HTTPStatusError("429", request=None, response=self)

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._TransientError):
            self.mod._fetch_route("LOT123")

    def test_oversized_response_treated_as_permanent(self, monkeypatch):
        """PY-8: response over the size cap is a deterministic policy
        rejection from safe_httpx_get — permanent, not transient."""
        import httpx
        self._bypass_validate(monkeypatch)

        class FakeResp:
            status_code = 200
            content = b"x" * (1024 * 1024)  # 1 MB, over our 64 KB cap

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        with pytest.raises(self.mod._PermanentError):
            self.mod._fetch_route("LOT123")

    def test_callsign_percent_encoded_in_url(self, monkeypatch):
        import httpx
        self._bypass_validate(monkeypatch)
        captured = []

        class FakeResp:
            status_code = 404
            content = b""

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, **kw):
                captured.append(url)
                return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        self.mod._fetch_route("LOT/123")
        assert len(captured) == 1
        assert "LOT%2F123" in captured[0]
        assert "LOT/123" not in captured[0].split("/callsign/")[-1]


# ---------------------------------------------------------------------------
# run_enricher_loop
# ---------------------------------------------------------------------------

class TestRunEnricherLoop:
    def test_loop_calls_enrich_batch_and_sleeps(self, monkeypatch, tmp_path):
        from readsbstats import route_enricher
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)

        calls = []

        def fake_enrich(conn, *, client=None):
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


# ---------------------------------------------------------------------------
# start_background_enricher — idempotency
# ---------------------------------------------------------------------------

class TestStartBackgroundEnricher:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        import importlib
        from readsbstats import route_enricher
        importlib.reload(route_enricher)
        self.enricher = route_enricher
        self.monkeypatch = monkeypatch
        yield

    def test_starts_daemon_thread(self):
        import threading
        stop = threading.Event()
        self.monkeypatch.setattr(self.enricher, "run_enricher_loop", lambda db_path: stop.wait())
        t = self.enricher.start_background_enricher()
        assert t is not None
        assert t.daemon is True
        assert t.name == "route-enricher"
        stop.set()
        t.join(timeout=2)

    def test_idempotent_returns_same_thread(self):
        import threading
        stop = threading.Event()
        self.monkeypatch.setattr(self.enricher, "run_enricher_loop", lambda db_path: stop.wait())
        t1 = self.enricher.start_background_enricher()
        t2 = self.enricher.start_background_enricher()
        assert t1 is t2
        stop.set()
        t1.join(timeout=2)
