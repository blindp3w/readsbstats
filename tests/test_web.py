"""
Tests for web.py — pure helpers and API endpoints via FastAPI TestClient.
Uses an in-memory SQLite database injected by patching _deps._db.
"""

import json
import math
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from readsbstats import config, database, enrichment, geo, photo_sources, web
from readsbstats import cache
from readsbstats.api import _deps
from readsbstats.api import _photos
from readsbstats.api import feeders
from readsbstats.api import settings as settings_mod
from readsbstats.api import airspace as airspace_mod
from readsbstats.api import stats as stats_mod
from readsbstats.photo_sources import PhotoResult


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

from tests._helpers import insert_position, iter_api_routes, make_db  # noqa: E402 — kept under section header


@pytest.fixture()
def db_conn():
    """Fresh in-memory DB; also clears enrichment caches."""
    conn = make_db()
    enrichment.clear_cache()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    """TestClient with _deps._db patched to the in-memory connection.
    Default X-Requested-With header makes existing mutating tests pass the
    CSRF check; tests for missing-header rejection construct their own client.
    """
    monkeypatch.setattr(_deps, "_db", db_conn)
    # The lifespan starts the cache prewarmer when PREWARM_MAP_CACHE is on
    # Tests don't want a background thread warming the cache out from under
    # their assertions — disable it here.
    monkeypatch.setattr(config, "PREWARM_MAP_CACHE", False)
    cache._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True,
                    headers={"X-Requested-With": "XMLHttpRequest"}) as c:
        yield c


@pytest.fixture()
def raw_client(db_conn, monkeypatch):
    """TestClient WITHOUT default X-Requested-With — for CSRF rejection tests."""
    monkeypatch.setattr(_deps, "_db", db_conn)
    monkeypatch.setattr(config, "PREWARM_MAP_CACHE", False)  # no background prewarmer in tests
    cache._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper to insert a minimal flight row
# ---------------------------------------------------------------------------

def insert_flight(conn, *, icao="aabbcc", callsign="LOT123", first_seen=1_000_000,
                  last_seen=1_003_600, max_alt_baro=35000, max_gs=450.0,
                  max_distance_nm=150.0, max_distance_bearing=None,
                  total_positions=10,
                  adsb_positions=9, mlat_positions=1, primary_source="adsb",
                  registration="SP-ABC", aircraft_type="B738", squawk=None):
    cur = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, registration, aircraft_type, squawk,
             first_seen, last_seen, max_alt_baro, max_gs, max_distance_nm,
             max_distance_bearing,
             total_positions, adsb_positions, mlat_positions, primary_source,
             lat_min, lat_max, lon_min, lon_max)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0)
        """,
        (icao, callsign, registration, aircraft_type, squawk,
         first_seen, last_seen, max_alt_baro, max_gs, max_distance_nm,
         max_distance_bearing,
         total_positions, adsb_positions, mlat_positions, primary_source),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

class TestBearing:
    def test_north(self):
        b = geo.bearing(0.0, 0.0, 1.0, 0.0)
        assert b == pytest.approx(0.0, abs=0.01)

    def test_east(self):
        b = geo.bearing(0.0, 0.0, 0.0, 1.0)
        assert b == pytest.approx(90.0, abs=0.01)

    def test_south(self):
        b = geo.bearing(0.0, 0.0, -1.0, 0.0)
        assert b == pytest.approx(180.0, abs=0.01)

    def test_west(self):
        b = geo.bearing(0.0, 0.0, 0.0, -1.0)
        assert b == pytest.approx(270.0, abs=0.01)

    def test_result_in_0_360_range(self):
        """Bearing is always in [0, 360)."""
        for dlat, dlon in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            b = geo.bearing(0.0, 0.0, dlat, dlon)
            assert 0 <= b < 360


class TestHaversineWeb:
    def test_same_point(self):
        assert geo.haversine_nm(52.0, 21.0, 52.0, 21.0) == pytest.approx(0.0, abs=1e-9)

    def test_one_degree_latitude(self):
        d = geo.haversine_nm(52.0, 21.0, 53.0, 21.0)
        assert 59.8 < d < 60.2


# ---------------------------------------------------------------------------
# _build_flight_filter
# ---------------------------------------------------------------------------

class TestBuildFlightFilter:
    def test_no_params_empty_where(self):
        where, params = _deps._build_flight_filter(None, None, None, None, None, None, None)
        assert where == ""
        assert params == []

    def test_date_adds_range(self):
        where, params = _deps._build_flight_filter("2024-01-15", None, None, None, None, None, None)
        assert "first_seen >= ?" in where
        assert "first_seen < ?" in where
        assert len(params) == 2
        # params[1] - params[0] should equal 86400 (one day)
        assert params[1] - params[0] == 86400

    def test_bad_date_raises_400(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _deps._build_flight_filter("not-a-date", None, None, None, None, None, None)
        assert exc_info.value.status_code == 400

    def test_date_uses_host_local_timezone(self):
        """Date filter must interpret YYYY-MM-DD in the host's local TZ to match
        notifier.py's `strftime(... 'localtime')` daily-summary semantics.

        Otherwise a Warsaw user filtering '2024-01-15' would miss flights that
        happened at e.g. 00:30 local time (= 23:30 UTC on the 14th)."""
        import os, time
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Warsaw"  # UTC+1 in January (no DST)
        time.tzset()
        try:
            _, params = _deps._build_flight_filter(
                "2024-01-15", None, None, None, None, None, None,
            )
            # Local Warsaw midnight 2024-01-15 → mktime resolves DST automatically
            expected_start = int(time.mktime((2024, 1, 15, 0, 0, 0, 0, 0, -1)))
            assert params[0] == expected_start
            assert params[1] - params[0] == 86400
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_icao_lowercased_and_tilde_stripped(self):
        where, params = _deps._build_flight_filter(None, "~AABBCC", None, None, None, None, None)
        assert "icao_hex = ?" in where
        assert params == ["aabbcc"]

    def test_callsign_uppercased_with_wildcard(self):
        where, params = _deps._build_flight_filter(None, None, "lot", None, None, None, None)
        assert "callsign LIKE ?" in where
        assert params == ["LOT%"]

    def test_registration_uppercased_with_wildcard(self):
        where, params = _deps._build_flight_filter(None, None, None, "sp-abc", None, None, None)
        assert "LIKE ?" in where
        assert params == ["SP-ABC%"]

    def test_aircraft_type_uppercased(self):
        where, params = _deps._build_flight_filter(None, None, None, None, "b738", None, None)
        assert params == ["B738"]

    def test_source_filter(self):
        where, params = _deps._build_flight_filter(None, None, None, None, None, "adsb", None)
        assert "primary_source = ?" in where
        assert params == ["adsb"]

    def test_flags_military(self):
        where, _ = _deps._build_flight_filter(None, None, None, None, None, None, "military")
        # The flag expression now OR-merges aircraft_db.flags, adsbx_overrides.flags,
        # and the runtime FLAG_ANONYMOUS bit — match on the bitmask test, not the exact SQL.
        assert "COALESCE(adb.flags, 0)" in where
        assert "COALESCE(axo.flags, 0)" in where
        assert "& 1) = 1" in where

    def test_flags_interesting(self):
        where, _ = _deps._build_flight_filter(None, None, None, None, None, None, "interesting")
        assert "& 2) = 2" in where
        # must exclude aircraft that are also military (flags & 1)
        assert "& 1) = 0" in where

    def test_flags_anonymous(self):
        where, _ = _deps._build_flight_filter(None, None, None, None, None, None, "anonymous")
        # FLAG_ANONYMOUS=16 set, military/interesting bits cleared
        assert "& 16) = 16" in where
        assert "& 3) = 0" in where

    def test_squawk_filter(self):
        where, params = _deps._build_flight_filter(None, None, None, None, None, None, None, squawk="7700")
        assert "squawk = ?" in where
        assert "7700" in params

    def test_multiple_filters_uses_and(self):
        where, params = _deps._build_flight_filter(None, "aabbcc", "LOT", None, None, None, None)
        assert " AND " in where
        assert len(params) == 2


class TestAnonymousFlagInResponse:
    """The non-ICAO hex bit (FLAG_ANONYMOUS=16) is computed at query time
    against the ICAO state-allocation table — these tests pin the surface
    behaviour end-to-end via /api/flights so a regression in icao_ranges
    or _FLAGS_EXPR_* won't silently break the gallery / Telegram path."""

    def test_dd85cb_surfaces_anonymous_flag(self, client, db_conn):
        # Reproduces the real-world sighting that motivated this feature.
        insert_flight(db_conn, icao="dd85cb", callsign=None,
                      registration=None, aircraft_type=None)
        r = client.get("/api/flights?icao=dd85cb")
        flights = r.json()["flights"]
        assert len(flights) == 1
        assert flights[0]["flags"] & config.FLAG_ANONYMOUS

    def test_state_allocated_hex_has_no_anon_bit(self, client, db_conn):
        # Polish-allocated hex (488001) must not pick up the anon bit.
        insert_flight(db_conn, icao="488001")
        r = client.get("/api/flights?icao=488001")
        flights = r.json()["flights"]
        assert len(flights) == 1
        assert not (flights[0]["flags"] & config.FLAG_ANONYMOUS)

    def test_anonymous_filter_includes_only_anon_only(self, client, db_conn):
        # Three cases: anonymous-only / military-but-state / state civilian.
        # The "anonymous" filter must return only the first.
        insert_flight(db_conn, icao="dd85cb", callsign=None)
        insert_flight(db_conn, icao="aabbcc", callsign="MIL01")
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?, ?, ?, ?, ?)",
            ("aabbcc", "MIL-1", "F18", "F/A-18", config.FLAG_MILITARY),
        )
        insert_flight(db_conn, icao="488001", callsign="LOT001")
        db_conn.commit()
        r = client.get("/api/flights?flags=anonymous")
        flights = r.json()["flights"]
        assert {f["icao_hex"] for f in flights} == {"dd85cb"}

    def test_military_filter_unaffected_by_anon_bit(self, client, db_conn):
        # An aircraft that's BOTH military and anonymous (non-state hex) must
        # still show up under the Military filter — bits are independent.
        insert_flight(db_conn, icao="dd0001", callsign="OPS01")
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dd0001", "SECRET", "C17", "C-17", config.FLAG_MILITARY),
        )
        db_conn.commit()
        r = client.get("/api/flights?flags=military")
        flights = r.json()["flights"]
        assert any(f["icao_hex"] == "dd0001" for f in flights)
        # And the response carries BOTH bits so the UI can render two badges.
        target = next(f for f in flights if f["icao_hex"] == "dd0001")
        assert target["flags"] & config.FLAG_MILITARY
        assert target["flags"] & config.FLAG_ANONYMOUS


# ---------------------------------------------------------------------------
# API: /api/flights
# ---------------------------------------------------------------------------

class TestApiFlights:
    def test_empty_db_returns_zero_total(self, client):
        r = client.get("/api/flights")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["flights"] == []

    def test_returns_inserted_flight(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["flights"][0]["icao_hex"] == "aabbcc"

    def test_pagination_limit(self, client, db_conn):
        for i in range(5):
            insert_flight(db_conn, icao=f"aa00{i:02d}", first_seen=1_000_000 + i)
        r = client.get("/api/flights?limit=2")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5
        assert len(data["flights"]) == 2

    def test_pagination_offset(self, client, db_conn):
        for i in range(5):
            insert_flight(db_conn, icao=f"aa00{i:02d}", first_seen=1_000_000 + i)
        r = client.get("/api/flights?limit=2&offset=3")
        assert r.status_code == 200
        assert len(r.json()["flights"]) == 2

    def test_offset_beyond_total_returns_empty(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights?offset=999")
        assert r.status_code == 200
        assert r.json()["flights"] == []

    def test_sort_by_invalid_column_falls_back_to_first_seen(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_001)
        insert_flight(db_conn, icao="aa0002", first_seen=1_000_002)
        # Invalid column → falls back to first_seen; sort_dir still honoured
        r = client.get("/api/flights?sort_by=INVALID_COLUMN&sort_dir=asc")
        assert r.status_code == 200
        flights = r.json()["flights"]
        assert len(flights) == 2
        assert flights[0]["first_seen"] <= flights[1]["first_seen"]

    def test_sort_by_invalid_column_default_desc(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_001)
        insert_flight(db_conn, icao="aa0002", first_seen=1_000_002)
        # No sort_dir → defaults to DESC
        r = client.get("/api/flights?sort_by=BOGUS")
        flights = r.json()["flights"]
        assert flights[0]["first_seen"] >= flights[1]["first_seen"]

    def test_sort_asc(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_001)
        insert_flight(db_conn, icao="aa0002", first_seen=1_000_002)
        r = client.get("/api/flights?sort_by=first_seen&sort_dir=asc")
        flights = r.json()["flights"]
        assert flights[0]["icao_hex"] == "aa0001"
        assert flights[1]["icao_hex"] == "aa0002"

    def test_sort_desc(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_001)
        insert_flight(db_conn, icao="aa0002", first_seen=1_000_002)
        r = client.get("/api/flights?sort_by=first_seen&sort_dir=desc")
        flights = r.json()["flights"]
        assert flights[0]["icao_hex"] == "aa0002"

    def test_filter_by_icao(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc")
        insert_flight(db_conn, icao="ddeeff")
        r = client.get("/api/flights?icao=aabbcc")
        data = r.json()
        assert data["total"] == 1
        assert data["flights"][0]["icao_hex"] == "aabbcc"

    def test_filter_by_callsign_prefix(self, client, db_conn):
        insert_flight(db_conn, callsign="LOT123", icao="aa0001")
        insert_flight(db_conn, callsign="RYR456", icao="aa0002")
        r = client.get("/api/flights?callsign=LOT")
        assert r.json()["total"] == 1

    def test_filter_by_source(self, client, db_conn):
        insert_flight(db_conn, primary_source="adsb", icao="aa0001")
        insert_flight(db_conn, primary_source="mlat", icao="aa0002")
        r = client.get("/api/flights?source=mlat")
        assert r.json()["total"] == 1
        assert r.json()["flights"][0]["icao_hex"] == "aa0002"

    def test_filter_by_squawk(self, client, db_conn):
        insert_flight(db_conn, squawk="7700", icao="aa0001")
        insert_flight(db_conn, squawk=None,   icao="aa0002")
        r = client.get("/api/flights?squawk=7700")
        assert r.json()["total"] == 1

    def test_bad_date_returns_400(self, client):
        r = client.get("/api/flights?date=not-a-date")
        assert r.status_code == 400

    def test_sql_injection_in_filters(self, client, db_conn):
        """SQL injection payloads in filter params must not crash or leak data."""
        insert_flight(db_conn, icao="aa0001")
        payloads = [
            "'; DROP TABLE flights; --",
            "' OR '1'='1",
            "1; SELECT * FROM sqlite_master --",
            "' UNION SELECT sql FROM sqlite_master --",
        ]
        for payload in payloads:
            for param in ("icao", "callsign", "registration", "aircraft_type", "squawk"):
                r = client.get(f"/api/flights?{param}={payload}")
                assert r.status_code == 200, f"Unexpected status for {param}={payload!r}"
                # Injection should not return all rows
                assert r.json()["total"] <= 1, f"Injection leaked rows via {param}"
        # Verify flights table still exists and has data
        assert db_conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_sql_injection_in_sort_by(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights?sort_by=first_seen;DROP TABLE flights")
        assert r.status_code == 200
        assert db_conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_all_sort_columns_accepted_and_return_data(self, client, db_conn):
        """Every column in _SORT_COLS must return 200 with valid flight data."""
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_001, max_alt_baro=35000)
        insert_flight(db_conn, icao="aa0002", first_seen=1_000_002, max_alt_baro=10000)
        for col in _deps._SORT_COLS:
            r = client.get(f"/api/flights?sort_by={col}")
            assert r.status_code == 200, f"Failed for sort_by={col}"
            flights = r.json()["flights"]
            assert len(flights) == 2, f"Wrong count for sort_by={col}"
            # Verify both flights present (sort didn't drop rows)
            icaos = {f["icao_hex"] for f in flights}
            assert icaos == {"aa0001", "aa0002"}, f"Missing flight for sort_by={col}"

    def test_zero_duration_flight(self, client, db_conn):
        """Flight with first_seen == last_seen should return duration_sec == 0."""
        insert_flight(db_conn, first_seen=1_000_000, last_seen=1_000_000)
        r = client.get("/api/flights")
        assert r.json()["flights"][0]["duration_sec"] == 0

    def test_offset_beyond_total_returns_empty(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights?offset=9999")
        assert r.status_code == 200
        assert r.json()["flights"] == []
        assert r.json()["total"] == 1

    def test_negative_offset_rejected(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights?offset=-1")
        assert r.status_code == 422

    def test_very_large_limit_rejected(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights?limit=999999")
        assert r.status_code == 422


class TestApiFlightsDateRange:
    """date_from / date_to filters — date= still works for single day."""

    def test_date_from_only_includes_later_flights(self, client, db_conn):
        insert_flight(db_conn, icao="aa1111", first_seen=1_000_000, last_seen=1_001_000)
        insert_flight(db_conn, icao="aa2222", first_seen=2_000_000, last_seen=2_001_000)
        # date_from = 1970-01-19 12:00 UTC → 1_512_000-ish; safely past flight 1
        r = client.get("/api/flights?date_from=1970-01-19")
        assert r.status_code == 200
        icaos = {f["icao_hex"] for f in r.json()["flights"]}
        assert "aa2222" in icaos
        assert "aa1111" not in icaos

    def test_date_to_only_includes_earlier_flights(self, client, db_conn):
        insert_flight(db_conn, icao="aa1111", first_seen=1_000_000, last_seen=1_001_000)
        insert_flight(db_conn, icao="aa2222", first_seen=2_000_000, last_seen=2_001_000)
        r = client.get("/api/flights?date_to=1970-01-19")
        assert r.status_code == 200
        icaos = {f["icao_hex"] for f in r.json()["flights"]}
        assert "aa1111" in icaos
        assert "aa2222" not in icaos

    def test_date_overrides_range(self, client, db_conn):
        # When `date` is set, the range params are ignored.
        insert_flight(db_conn, icao="aa1111", first_seen=1_000_000, last_seen=1_001_000)
        r = client.get("/api/flights?date=1970-01-12&date_from=2999-01-01")
        assert r.status_code == 200
        icaos = {f["icao_hex"] for f in r.json()["flights"]}
        assert "aa1111" in icaos

    def test_date_range_validation_400_on_malformed(self, client):
        r = client.get("/api/flights?date_from=not-a-date")
        assert r.status_code == 400

    def test_date_to_validation_400_on_malformed(self, client):
        # date_to has its own strptime/400 branch in _deps — pin it separately.
        r = client.get("/api/flights?date_to=not-a-date")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# API: /api/flights/export.csv
# ---------------------------------------------------------------------------

class TestApiFlightsExport:
    def test_csv_headers(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.splitlines()
        assert lines[0] == ",".join(_deps._CSV_COLS)

    def test_csv_one_data_row(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/flights/export.csv")
        lines = r.text.splitlines()
        assert len(lines) == 2  # header + 1 row

    def test_csv_empty_db(self, client):
        r = client.get("/api/flights/export.csv")
        lines = r.text.splitlines()
        assert len(lines) == 1  # header only

    def test_csv_filename_with_date(self, client):
        r = client.get("/api/flights/export.csv?date=2024-01-15")
        cd = r.headers.get("content-disposition", "")
        assert "flights_2024-01-15.csv" in cd

    def test_csv_filename_without_date(self, client):
        r = client.get("/api/flights/export.csv")
        cd = r.headers.get("content-disposition", "")
        assert "flights.csv" in cd

    def test_csv_export_respects_epoch_from_to(self, client, db_conn):
        """Audit 2026-05-26: /api/flights/export.csv must accept the same
        epoch ``from``/``to`` params the History page sends to
        /api/flights. Before the fix the export silently ignored them
        and dumped the entire DB regardless of the visible filter.
        """
        # Three flights spanning ~3 days
        insert_flight(db_conn, first_seen=1_700_000_000)
        insert_flight(db_conn, first_seen=1_700_100_000, icao="000001")
        insert_flight(db_conn, first_seen=1_700_200_000, icao="000002")

        # Pull only the middle one via the epoch params.
        from_ts = 1_700_050_000
        to_ts   = 1_700_150_000
        r = client.get(
            f"/api/flights/export.csv?from={from_ts}&to={to_ts}"
        )
        assert r.status_code == 200
        lines = r.text.splitlines()
        assert len(lines) == 2, (
            "Expected header + 1 row (the middle flight); got "
            f"{len(lines)} rows. The from/to filter was ignored."
        )

        # Confirm /api/flights with the same filter yields the same set
        # — the export and the JSON view must agree.
        r2 = client.get(f"/api/flights?from={from_ts}&to={to_ts}")
        flights = r2.json()["flights"]
        assert len(flights) == 1


# ---------------------------------------------------------------------------
# API: /api/flights/{flight_id}
# ---------------------------------------------------------------------------

class TestApiFlightDetail:
    def test_not_found_returns_404(self, client):
        r = client.get("/api/flights/9999")
        assert r.status_code == 404

    def test_found_returns_flight(self, client, db_conn):
        fid = insert_flight(db_conn)
        r = client.get(f"/api/flights/{fid}")
        assert r.status_code == 200
        data = r.json()
        assert data["flight"]["icao_hex"] == "aabbcc"

    def test_detail_omits_positions_by_default(self, client, db_conn):
        # BE-10: the detail endpoint no longer embeds the raw position
        # timeline by default — the frontend pulls it from the dedicated
        # paginated / downsampled endpoints. Default response keeps the
        # `positions` key but returns it empty.
        fid = insert_flight(db_conn)
        insert_position(db_conn, fid, 1_000_001, lat=52.0, lon=21.0,
                        source_type="adsb_icao")
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}")
        assert r.status_code == 200
        assert r.json()["positions"] == []

    def test_detail_includes_positions_when_requested(self, client, db_conn):
        # BE-10: explicit opt-in still returns the full embedded list for
        # any non-frontend consumer that depends on it.
        fid = insert_flight(db_conn)
        insert_position(db_conn, fid, 1_000_001, lat=52.0, lon=21.0,
                        source_type="adsb_icao")
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}?include_positions=true")
        assert r.status_code == 200
        assert len(r.json()["positions"]) == 1

    def test_found_includes_other_flights(self, client, db_conn):
        fid1 = insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000)
        fid2 = insert_flight(db_conn, icao="aabbcc", first_seen=1_100_000)
        r = client.get(f"/api/flights/{fid2}")
        other = r.json()["other_flights"]
        assert any(f["id"] == fid1 for f in other)

    def test_found_includes_receiver_coords(self, client, db_conn, monkeypatch):
        from readsbstats import config
        monkeypatch.setattr(config, "RECEIVER_LAT", 51.5)
        monkeypatch.setattr(config, "RECEIVER_LON", -0.1)
        fid = insert_flight(db_conn)
        r = client.get(f"/api/flights/{fid}")
        data = r.json()
        assert data["receiver_lat"] == 51.5
        assert data["receiver_lon"] == -0.1


# ---------------------------------------------------------------------------
# Audit 2026-05-26: split positions endpoints
# ---------------------------------------------------------------------------


class TestApiFlightPositionsSplit:
    def _seed(self, db_conn, n: int) -> int:
        fid = insert_flight(db_conn)
        # Bulk insert n synthetic positions for the flight
        for i in range(n):
            insert_position(
                db_conn, fid, 1_000_000 + i,
                lat=52.0 + i * 0.0001, lon=21.0 + i * 0.0001,
                alt_baro=1000 + i, gs=200.0 + (i % 50),
                source_type="adsb_icao",
            )
        db_conn.commit()
        return fid

    def test_chart_endpoint_caps_at_target(self, client, db_conn):
        fid = self._seed(db_conn, 5000)
        r = client.get(f"/api/flights/{fid}/positions/chart?target=200")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5000
        assert data["target"] == 200
        assert len(data["positions"]) == 200
        # First and last preserved → first position's ts is the seed ts.
        assert data["positions"][0]["ts"] == 1_000_000
        assert data["positions"][-1]["ts"] == 1_000_000 + 4999

    def test_chart_endpoint_returns_all_when_below_target(self, client, db_conn):
        fid = self._seed(db_conn, 50)
        r = client.get(f"/api/flights/{fid}/positions/chart?target=500")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 50
        assert len(data["positions"]) == 50

    def test_positions_pagination(self, client, db_conn):
        fid = self._seed(db_conn, 250)
        r1 = client.get(f"/api/flights/{fid}/positions?limit=100&offset=0")
        r2 = client.get(f"/api/flights/{fid}/positions?limit=100&offset=100")
        r3 = client.get(f"/api/flights/{fid}/positions?limit=100&offset=200")
        assert r1.status_code == r2.status_code == r3.status_code == 200
        d1, d2, d3 = r1.json(), r2.json(), r3.json()
        assert d1["total"] == d2["total"] == d3["total"] == 250
        assert len(d1["positions"]) == 100
        assert len(d2["positions"]) == 100
        assert len(d3["positions"]) == 50
        # Pages don't overlap
        ts1 = [p["ts"] for p in d1["positions"]]
        ts2 = [p["ts"] for p in d2["positions"]]
        assert max(ts1) < min(ts2)

    def test_positions_limit_clamped(self, client, db_conn):
        """Requesting more than the server cap (2000) is a 422 — FastAPI
        Query validators enforce the upper bound."""
        fid = self._seed(db_conn, 100)
        r = client.get(f"/api/flights/{fid}/positions?limit=5000")
        assert r.status_code == 422

    def test_detail_embeds_positions_only_on_opt_in(self, client, db_conn):
        """BE-10: /api/flights/{id} omits the embedded `positions` list by
        default (frontend uses the split endpoints); the full list is
        returned only with ?include_positions=true."""
        fid = self._seed(db_conn, 200)
        r_default = client.get(f"/api/flights/{fid}")
        assert r_default.status_code == 200
        assert r_default.json()["positions"] == []
        r_optin = client.get(f"/api/flights/{fid}?include_positions=true")
        assert r_optin.status_code == 200
        assert len(r_optin.json()["positions"]) == 200

    def test_chart_endpoint_includes_baro_rate(self, client, db_conn):
        """BE-10/FE-1: the header's at-max vert-rate sublabel now derives
        from the downsampled chart series, so `baro_rate` must be present."""
        fid = insert_flight(db_conn)
        insert_position(db_conn, fid, 1_000_001, lat=52.0, lon=21.0,
                        alt_baro=10000, gs=250.0, track=90.0, baro_rate=-640,
                        source_type="adsb_icao")
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/positions/chart?target=500")
        assert r.status_code == 200
        positions = r.json()["positions"]
        assert positions and "baro_rate" in positions[0]
        assert positions[0]["baro_rate"] == -640


# ---------------------------------------------------------------------------
# API: /api/health
# ---------------------------------------------------------------------------

class TestApiHealth:
    def test_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_degraded_when_db_raises(self, client, monkeypatch):
        # STY-7: the probe except is narrowed to (sqlite3.Error, OSError); a
        # genuine DB-liveness failure still degrades fail-soft (200, not 500).
        import sqlite3

        def bad_db():
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(_deps, "db", bad_db)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

    def test_degraded_on_oserror(self, client, monkeypatch):
        # STY-7: an OSError opening the DB file is also a liveness failure and
        # must degrade, not 500.
        def bad_db():
            raise OSError("unable to open database file")
        monkeypatch.setattr(_deps, "db", bad_db)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

    def test_non_db_error_not_swallowed(self, db_conn, monkeypatch):
        # STY-7: the narrowed (sqlite3.Error, OSError) except must NOT swallow a
        # non-DB programming error (e.g. a TypeError from a future refactor) —
        # that should surface as a 500, not be masked as a benign "degraded".
        monkeypatch.setattr(_deps, "_db", db_conn)
        monkeypatch.setattr(config, "PREWARM_MAP_CACHE", False)
        cache._cache.clear()

        def boom():
            raise RuntimeError("programming bug, not a DB liveness failure")
        monkeypatch.setattr(_deps, "db", boom)
        with TestClient(web.app, raise_server_exceptions=False) as c:
            r = c.get("/api/health")
        assert r.status_code == 500

    def test_does_not_leak_db_path(self, client):
        # /api/health is a public uptime probe — must not reveal filesystem
        # layout. The status field is the only field consumers need.
        r = client.get("/api/health")
        body = r.json()
        assert "db_path" not in body
        from readsbstats import config
        import os
        parent = os.path.dirname(os.path.abspath(config.DB_PATH))
        if parent and parent != "/":
            assert parent not in r.text


# ---------------------------------------------------------------------------
# API: /api/metrics/health
# ---------------------------------------------------------------------------

class TestApiMetricsHealth:
    def test_empty_db_returns_info(self, client):
        # Audit-13 A13-025: empty receiver_stats is operator choice, not
        # a warning state. Heartbeat now reports `info`, so overall is
        # `info` (the previous `warn` over-claimed).
        r = client.get("/api/metrics/health")
        assert r.status_code == 200
        body = r.json()
        assert body["overall"] == "info"
        names = [c["name"] for c in body["checks"]]
        assert "heartbeat" in names
        assert "aircraft_visibility" in names
        assert "noise_floor" in names
        assert "cpu_saturation" in names

    def test_returns_check_payload_shape(self, client):
        r = client.get("/api/metrics/health")
        body = r.json()
        for c in body["checks"]:
            assert set(c.keys()) >= {"name", "severity", "message"}
            assert c["severity"] in ("ok", "info", "warn", "critical")


# ---------------------------------------------------------------------------
# API: /api/metrics (time-series data, not /health)
# ---------------------------------------------------------------------------

class TestApiMetricsQueryValidation:
    """`from`/`to` must be validated by FastAPI as integers — earlier
    implementation read from request.query_params and called int() manually,
    raising ValueError → HTTP 500 on garbage input.  See improvements.md #115."""

    def test_invalid_from_returns_4xx_not_500(self, client):
        r = client.get("/api/metrics?from=foo&metrics=signal")
        assert r.status_code in (400, 422), (
            f"expected 4xx for non-int from, got {r.status_code}: {r.text}"
        )

    def test_invalid_to_returns_4xx_not_500(self, client):
        r = client.get("/api/metrics?to=bar&metrics=signal")
        assert r.status_code in (400, 422)

    def test_valid_int_from_and_to_works(self, client):
        r = client.get("/api/metrics?from=1000000&to=1000100&metrics=signal")
        assert r.status_code == 200
        body = r.json()
        assert body["metrics"] == ["signal"]

    def test_omitted_from_and_to_uses_defaults(self, client):
        r = client.get("/api/metrics?metrics=signal")
        assert r.status_code == 200
        body = r.json()
        assert body["metrics"] == ["signal"]

    def test_unknown_metric_returns_400(self, client):
        r = client.get("/api/metrics?from=1000000&to=1000100&metrics=not_a_real_col")
        assert r.status_code == 400

    def test_from_after_to_returns_422(self, client):
        """Audit 17: an inverted range (from > to) is a malformed request —
        reject with 422 rather than silently returning an empty series."""
        r = client.get("/api/metrics?from=1000100&to=1000000&metrics=signal")
        assert r.status_code == 422

    def test_from_equals_to_is_allowed(self, client):
        """from == to is a degenerate-but-valid zero-width window (returns no
        rows), not an error — must stay 200."""
        r = client.get("/api/metrics?from=1000000&to=1000000&metrics=signal")
        assert r.status_code == 200

    def test_duplicate_metrics_are_deduped(self, client):
        # Repeated valid names must not widen the SQL projection / response arrays.
        r = client.get("/api/metrics?from=1000000&to=1000100&metrics=signal,signal,signal")
        assert r.status_code == 200
        assert r.json()["metrics"] == ["signal"]

    def test_too_many_metrics_rejected(self, client):
        # Listing more names than columns exist is a resource-amplification
        # attempt — reject before building the projection (OWASP API4).
        many = ",".join(["signal"] * 100)
        r = client.get(f"/api/metrics?from=1000000&to=1000100&metrics={many}")
        assert r.status_code == 400


class TestApiMetricsBucketing:
    """The >24h downsample path (bucket > 0) runs _deps._metrics_agg per column;
    earlier tests only used ≤100s windows (the raw branch), so the aggregate
    selection was untested (audit 2026-06-15)."""

    def test_buckets_and_aggregates_over_a_long_window(self, client, db_conn):
        # Two rows in the same 300s bucket; a >24h window forces bucket=300.
        db_conn.execute(
            "INSERT INTO receiver_stats (ts, signal, messages, peak_signal) VALUES (?,?,?,?)",
            (1000, -10.0, 5, -3.0),
        )
        db_conn.execute(
            "INSERT INTO receiver_stats (ts, signal, messages, peak_signal) VALUES (?,?,?,?)",
            (1100, -20.0, 7, -1.0),
        )
        db_conn.commit()
        r = client.get("/api/metrics?from=0&to=200000&metrics=signal,messages,peak_signal")
        assert r.status_code == 200
        body = r.json()
        assert body["bucket_seconds"] == 300
        assert body["metrics"] == ["signal", "messages", "peak_signal"]
        data = body["data"]
        assert data[0] == [900]      # bucket_ts = floor(1000/300)*300
        assert data[1] == [-15.0]    # AVG(signal): continuous measurement
        assert data[2] == [12]       # SUM(messages): per-interval counter
        assert data[3] == [-1.0]     # MAX(peak_signal): peak/extreme

    def test_metrics_agg_selects_expected_aggregate(self):
        assert _deps._metrics_agg("peak_signal") == "MAX(peak_signal)"
        assert _deps._metrics_agg("signal") == "AVG(signal)"
        assert _deps._metrics_agg("messages") == "SUM(messages)"


# ---------------------------------------------------------------------------
# Helper: _fmt_ts
# ---------------------------------------------------------------------------

class TestFmtTs:
    def test_none_returns_empty_string(self):
        assert _deps._fmt_ts(None) == ""

    def test_epoch_formats_utc(self):
        result = _deps._fmt_ts(0)
        assert result == "1970-01-01 00:00"


# ---------------------------------------------------------------------------
# Helper: _get_cache / _set_cache
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def clear_web_cache():
    cache._cache.clear()
    yield
    cache._cache.clear()


class TestCache:
    def test_miss_returns_none(self, clear_web_cache):
        assert cache._get_cache("no_such_key") is None

    def test_hit_returns_value(self, clear_web_cache):
        cache._set_cache("foo", {"x": 1})
        assert cache._get_cache("foo") == {"x": 1}

    def test_expired_entry_returns_none(self, clear_web_cache):
        # Plant an entry with a timestamp far in the past
        cache._cache["bar"] = (time.time() - cache._DEFAULT_TTL - 1, {"x": 2})
        assert cache._get_cache("bar") is None

    def test_set_cache_evicts_oldest_over_cap(self, clear_web_cache):
        """Audit 2026-05-25: filtered /api/stats requests with caller-controlled
        from/to range produce unbounded distinct keys. The cache must cap the
        total entry count and evict the oldest first."""
        cap = cache._CACHE_MAX_ENTRIES
        for i in range(cap + 50):
            cache._set_cache(f"stats:0:{i}", i)
        assert len(cache._cache) <= cap
        # The earliest keys should have been evicted; the most recent kept.
        assert cache._get_cache(f"stats:0:{cap + 49}") == cap + 49
        assert cache._get_cache("stats:0:0") is None

    def test_caller_keys_do_not_evict_named_entries(self, clear_web_cache):
        """Audit 17: a flood of caller-controlled keys (stats:{from}:{to},
        flagged:*) must not evict the bounded set of named, expensive-to-
        recompute entries (heatmap:all etc.). Eviction prefers keys absent from
        _CACHE_TTLS before falling back to the protected ones."""
        cache._set_cache("heatmap:all", {"big": "expensive"})
        cache._set_cache("coverage:all", {"big": "expensive"})
        for i in range(cache._CACHE_MAX_ENTRIES + 50):
            cache._set_cache(f"flagged:None:None:None:50:{i}", i)
        assert len(cache._cache) <= cache._CACHE_MAX_ENTRIES
        # The expensive prewarmed map entries survived the flood.
        assert cache._get_cache("heatmap:all") == {"big": "expensive"}
        assert cache._get_cache("coverage:all") == {"big": "expensive"}

    def test_get_cache_drops_expired_entries(self, clear_web_cache, monkeypatch):
        """Expired entries should be removed lazily on lookup so the cap is
        not consumed by zombie keys."""
        cache._set_cache("zombie", "ignored")
        # Fast-forward past the default TTL.
        future = time.time() + cache._DEFAULT_TTL + 5
        monkeypatch.setattr(time, "time", lambda: future)
        assert cache._get_cache("zombie") is None
        assert "zombie" not in cache._cache

    # BE-12 (Audit 2026-05-31): the airspace endpoint must go through the shared
    # cache helpers (not touch _cache directly), and "airspace" must have its
    # 1h TTL registered so _get_cache honors it instead of the 30s default.

    def test_airspace_ttl_registered(self, clear_web_cache):
        assert cache._ttl_for("airspace") == cache._AIRSPACE_TTL

    def test_airspace_endpoint_uses_cache_helper(self, clear_web_cache, monkeypatch):
        data = airspace_mod.api_airspace()
        # Stored via the shared helper and retrievable through it.
        assert cache._get_cache("airspace") == data
        # Survives well past the 30s default — would expire if the TTL were
        # unregistered and the entry stored under the default.
        future = time.time() + cache._DEFAULT_TTL + 100
        monkeypatch.setattr(time, "time", lambda: future)
        assert cache._get_cache("airspace") is not None

    def test_concurrent_cache_access_is_threadsafe(self, clear_web_cache):
        """Hammering the cache from many threads must not corrupt the store or
        raise (RuntimeError: OrderedDict mutated during iteration, etc.)."""
        import threading
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(300):
                    cache._set_cache(f"stats:{n}:{i}", i)
                    cache._get_cache(f"stats:{n}:{i}")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(cache._cache) <= cache._CACHE_MAX_ENTRIES

    def test_set_cache_refresh_moves_key_to_end(self, clear_web_cache, monkeypatch):
        """Refreshing an existing key must reset its eviction position —
        otherwise a hot, recently-refreshed key gets evicted before
        never-touched-since keys."""
        monkeypatch.setattr(cache, "_CACHE_MAX_ENTRIES", 3)
        cache._set_cache("stats:0:a", 1)
        cache._set_cache("stats:0:b", 2)
        cache._set_cache("stats:0:a", 10)   # refresh → back of the queue
        cache._set_cache("stats:0:c", 3)
        cache._set_cache("stats:0:d", 4)    # over cap → evict oldest
        assert cache._get_cache("stats:0:b") is None   # b was oldest, not a
        assert cache._get_cache("stats:0:a") == 10

    def test_set_cache_eviction_drops_expired_first(self, clear_web_cache, monkeypatch):
        """The over-cap sweep removes expired entries before touching any live
        one, and stops as soon as the cap is satisfied."""
        monkeypatch.setattr(cache, "_CACHE_MAX_ENTRIES", 3)
        cache._set_cache("stats:0:live1", 1)
        cache._set_cache("stats:0:live2", 2)
        cache._cache["stats:0:zombie"] = (time.time() - 99999, "dead")
        cache._set_cache("stats:0:live3", 3)
        assert "stats:0:zombie" not in cache._cache
        assert cache._get_cache("stats:0:live1") == 1
        assert cache._get_cache("stats:0:live2") == 2
        assert cache._get_cache("stats:0:live3") == 3

    def test_set_cache_all_protected_keys_evicts_oldest(self, clear_web_cache, monkeypatch):
        """Guard branch for a cap shrunk below the named-key count: when every
        entry is protected (exact _CACHE_TTLS key), evict the oldest protected
        one rather than looping forever."""
        monkeypatch.setattr(cache, "_CACHE_MAX_ENTRIES", 2)
        cache._set_cache("stats", 1)
        cache._set_cache("polar", 2)
        cache._set_cache("records", 3)
        assert len(cache._cache) <= 2
        assert "stats" not in cache._cache             # oldest protected evicted
        assert cache._get_cache("records") == 3

    def test_heatmap_and_coverage_locks_are_per_window_singletons(self):
        """The per-window async locks must be stable singletons — a fresh Lock
        per call would coalesce nothing."""
        h24 = cache._heatmap_lock("24h")
        assert cache._heatmap_lock("24h") is h24
        assert cache._heatmap_lock("7d") is not h24
        c24 = cache._coverage_lock("24h")
        assert cache._coverage_lock("24h") is c24
        assert c24 is not h24                          # separate families


class TestDbConnection:
    """`db()` must be per-thread in production so requests don't serialize on
    Python's per-connection sqlite mutex.  Tests that set `_deps._db` directly
    must still see that connection from every thread (for in-memory DBs)."""

    def test_test_override_shared_across_threads(self, db_conn, monkeypatch):
        import threading as _t
        monkeypatch.setattr(_deps, "_db", db_conn)
        seen: list[object] = []
        def fetch():
            seen.append(_deps.db())
        threads = [_t.Thread(target=fetch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(c is db_conn for c in seen)

    def test_per_thread_connection_when_no_override(self, tmp_path, monkeypatch):
        import threading as _t
        monkeypatch.setattr(_deps, "_db", None)
        monkeypatch.setattr(_deps, "_thread_local", _t.local())
        db_path = str(tmp_path / "perthread.db")
        database.init_db(db_path)
        original_connect = database.connect
        monkeypatch.setattr(web.database, "connect",
                            lambda path=db_path, **kw: original_connect(db_path, **kw))
        seen: list[object] = []
        lock = _t.Lock()
        def fetch():
            conn = _deps.db()
            with lock:
                seen.append(conn)
        threads = [_t.Thread(target=fetch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(id(c) for c in seen)) == 4

    def test_same_thread_returns_same_connection(self, tmp_path, monkeypatch):
        import threading as _t
        monkeypatch.setattr(_deps, "_db", None)
        monkeypatch.setattr(_deps, "_thread_local", _t.local())
        db_path = str(tmp_path / "samethread.db")
        database.init_db(db_path)
        original_connect = database.connect
        monkeypatch.setattr(web.database, "connect",
                            lambda path=db_path, **kw: original_connect(db_path, **kw))
        first = _deps.db()
        second = _deps.db()
        assert first is second
        first.close()


class TestStartupMigrate:
    """BE-3 (Audit 2026-05-31): the web lifespan must bootstrap base schema via
    database.ensure_base_schema() (which recovers an interrupted aircraft_db
    swap and creates base tables when missing), not a bare _migrate()."""

    def test_calls_ensure_base_schema_when_no_override(self, monkeypatch):
        monkeypatch.setattr(_deps, "_db", None)
        calls: list[str] = []
        monkeypatch.setattr(web.database, "ensure_base_schema",
                            lambda *a, **k: calls.append("ensure"))
        monkeypatch.setattr(web.database, "_migrate",
                            lambda *a, **k: calls.append("migrate"))
        web._startup_migrate()
        assert calls == ["ensure"], (
            "production startup must call ensure_base_schema, not bare _migrate"
        )

    def test_uses_injected_db_directly(self, db_conn, monkeypatch):
        monkeypatch.setattr(_deps, "_db", db_conn)
        calls: list[str] = []
        monkeypatch.setattr(web.database, "ensure_base_schema",
                            lambda *a, **k: calls.append("ensure"))
        monkeypatch.setattr(web.database, "_migrate",
                            lambda *a, **k: calls.append("migrate"))
        web._startup_migrate()
        # In-memory injected DBs can't be reopened, so the test connection is
        # migrated in place — never via ensure_base_schema (which opens a path).
        assert calls == ["migrate"]

    # _ensure_vdl2_schema is FAIL-OPEN by contract: a feature-local vdl2.db
    # problem must never take down the core web app.

    def test_ensure_vdl2_schema_failure_logs_and_continues(self, monkeypatch, caplog):
        from readsbstats.vdl2 import db as vdl2_db
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        monkeypatch.setattr(vdl2_db, "_conn", None)
        monkeypatch.setattr(
            vdl2_db, "connect",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("disk I/O error")))
        with caplog.at_level("WARNING"):
            web._ensure_vdl2_schema()      # must not raise
        assert any("VDL2 schema unavailable" in r.getMessage()
                   for r in caplog.records)

    def test_ensure_vdl2_schema_respects_injected_conn(self, monkeypatch):
        from readsbstats.vdl2 import db as vdl2_db
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        sentinel_conn = object()
        monkeypatch.setattr(vdl2_db, "_conn", sentinel_conn)
        calls = []
        monkeypatch.setattr(
            vdl2_db, "ensure_schema",
            lambda conn, build_fts=True: calls.append((conn, build_fts)))
        monkeypatch.setattr(
            vdl2_db, "connect",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not open a new connection")))
        web._ensure_vdl2_schema()
        # Test-injected connection used directly; web never builds FTS
        # (collector owns the FTS rebuild).
        assert calls == [(sentinel_conn, False)]


# ---------------------------------------------------------------------------
# /live compat redirect + JSON API for settings / feeders
# (Jinja2 UI deleted at v2.0.0 cutover; SPA owns the root URL space so
# /flight/{id} and /aircraft/{icao} are served by the SPA's catch-all
# directly — no redirect. /live is the one alias that survives as a 302
# because it's not a real SPA route, just a historical pointer at /map.)
# ---------------------------------------------------------------------------

class TestCompatRedirects:
    def test_live_redirects_to_map(self, client):
        r = client.get("/live", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].endswith("/map")

    def test_live_redirect_guards_hostile_root_path(self):
        """A13-049 defence in depth: a reverse proxy injecting an absolute
        root_path ("//evil.com") must not turn /live into an open redirect —
        the urlparse guard falls back to a bare /map. Calls the handler
        directly: the app's own root_path would override a TestClient scope."""
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/live",
                 "headers": [], "query_string": b"",
                 "root_path": "//evil.com"}
        resp = web.redirect_live(Request(scope))
        assert resp.status_code == 302
        assert resp.headers["location"] == "/map"

    def test_favicon_served_from_spa_dist(self, client):
        """The Vite public/ files get explicit root routes (not a / mount that
        would shadow /api/*) — pin the headers on the representative one."""
        if not web._SPA_AVAILABLE:
            pytest.skip("frontend/dist not built")
        r = client.get("/favicon.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert "max-age=86400" in r.headers.get("cache-control", "")


class TestApiSettings:
    def test_api_settings_returns_masked_payload(self, client, monkeypatch):
        from readsbstats import config
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test-token-secret-xyz")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345678")
        r = client.get("/api/settings")
        assert r.status_code == 200
        payload = r.json()
        assert payload["telegram_token"] == "configured"
        assert payload["telegram_chat_id"] == "configured"
        assert "test-token-secret-xyz" not in r.text
        assert "12345678" not in r.text
        for key in ("lat", "lon", "poll_interval", "db_path", "page_size", "base_url"):
            assert key in payload, f"missing key {key}"

    def test_api_settings_db_path_basename_only(self, client):
        import os
        from readsbstats import config
        r = client.get("/api/settings")
        parent = os.path.dirname(os.path.abspath(config.DB_PATH))
        if parent and parent != "/":
            assert parent not in r.text, "/api/settings leaks DB parent dir"

    def test_api_settings_when_telegram_not_set(self, client, monkeypatch):
        from readsbstats import config
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        r = client.get("/api/settings")
        payload = r.json()
        assert payload["telegram_token"] == "not set"
        assert payload["telegram_chat_id"] == "not set"

    def test_api_settings_does_not_leak_bind_host_port(self, client):
        """Regression for audit-12 #171 — web_host/web_port are redundant
        (the client is already at that URL) and shouldn't be in the payload."""
        r = client.get("/api/settings")
        payload = r.json()
        assert "web_host" not in payload
        assert "web_port" not in payload

    def test_api_settings_masks_airspace_geojson_path(self, client, monkeypatch):
        """Regression for audit-12 #171 — actual filesystem path must not
        appear in the response; only a coarse "(set)"/"(bundled)" label."""
        from readsbstats import config
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "/etc/secret/airspace.geojson")
        r = client.get("/api/settings")
        assert "/etc/secret" not in r.text
        assert "airspace.geojson" not in r.text
        payload = r.json()
        # Field still present so the operator UI can show "configured"
        assert "airspace_geojson" in payload
        # Bare basename / path must not have made it through
        assert payload["airspace_geojson"] in {"(set)", "(bundled poland.geojson)"}

    def test_api_settings_masks_stats_json_path(self, client, monkeypatch):
        from readsbstats import config
        monkeypatch.setattr(config, "STATS_JSON", "/run/readsb/stats.json")
        r = client.get("/api/settings")
        assert "/run/readsb" not in r.text
        payload = r.json()
        assert "stats_json" in payload
        # Audit-12 P8 — label is uniform "(configured)" / "(not set)" rather
        # than comparing against a hardcoded default that could drift.
        assert payload["stats_json"] in {"(configured)", "(not set)"}

    def test_api_settings_airspace_geojson_bundled_default(self, client, monkeypatch):
        from readsbstats import config
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "")
        r = client.get("/api/settings")
        payload = r.json()
        assert payload["airspace_geojson"] == "(bundled poland.geojson)"

    def test_metadata_block_present_for_every_payload_key(self, client, clear_web_cache):
        """Audit Settings 2026-05-25: backend ships a sibling `_metadata`
        block with one entry per displayed setting, so frontend env-var
        names can never drift from config.py. The set of metadata keys
        must equal the set of displayed keys exactly."""
        r = client.get("/api/settings")
        data = r.json()
        meta = data.get("_metadata")
        assert isinstance(meta, dict), "/api/settings must include a _metadata dict"
        payload_keys = {k for k in data if k != "_metadata"}
        meta_keys = set(meta.keys())
        missing = payload_keys - meta_keys
        orphan = meta_keys - payload_keys
        assert not missing, f"payload keys without metadata: {missing}"
        assert not orphan, f"metadata keys without payload: {orphan}"
        # Each metadata entry has the required shape.
        for key, entry in meta.items():
            assert isinstance(entry, dict), f"metadata[{key!r}] must be a dict"
            assert "env_var" in entry, f"metadata[{key!r}] missing env_var"
            assert "default" in entry, f"metadata[{key!r}] missing default"
            assert "customized" in entry, f"metadata[{key!r}] missing customized"
            assert isinstance(entry["env_var"], str)
            assert isinstance(entry["customized"], bool)

    def test_metadata_env_vars_resolve_in_config_source(self, client, clear_web_cache):
        """Drift defence: every env-var name shipped in _metadata must
        appear in config.py source. Closes the gap where a new payload
        key could be registered against a misspelled or removed env var."""
        import re
        from pathlib import Path
        config_src = Path("src/readsbstats/config.py").read_text()
        env_vars_in_source = set(re.findall(r'"(RSBS_[A-Z0-9_]+)"', config_src))
        r = client.get("/api/settings")
        meta = r.json()["_metadata"]
        for key, entry in meta.items():
            assert entry["env_var"] in env_vars_in_source, (
                f"_metadata[{key!r}].env_var={entry['env_var']!r} is not "
                f"read anywhere in config.py — drift bug"
            )

    def test_register_present_for_every_payload_key(self, client, clear_web_cache):
        """Drift defence: every payload key shipped from /api/settings
        must have a matching entry in `config._META_REGISTRY` populated
        at module import time. Tests the runtime registry rather than
        grepping source text — immune to multi-line / reformatted
        `_register(...)` call sites that a source grep would miss."""
        from readsbstats import config
        r = client.get("/api/settings")
        payload_keys = {k for k in r.json() if k != "_metadata"}
        missing = payload_keys - set(config._META_REGISTRY)
        assert not missing, (
            f"payload keys with no _register entry in _META_REGISTRY: {missing}"
        )

    def test_metadata_customized_false_when_namespace_matches_defaults(self):
        """`_settings_metadata` is a pure function over (namespace, keys).
        When the namespace's attributes equal the registered defaults
        (parsed to the right type), every customized flag is False."""
        from types import SimpleNamespace
        from readsbstats import config, web as web_mod
        # Build a stub namespace where every registered config_attr is set
        # to a value matching the registered default (after parsing).
        stub = SimpleNamespace()
        for payload_key, reg in config._META_REGISTRY.items():
            default = reg["default"]
            attr = reg["config_attr"]
            # Mirror what the parsers do at startup.
            if isinstance(default, bool):
                setattr(stub, attr, default)
            elif isinstance(default, str):
                try:
                    # int-typed defaults shipped as strings
                    setattr(stub, attr, int(default))
                except ValueError:
                    try:
                        setattr(stub, attr, float(default))
                    except ValueError:
                        # Genuinely a string default (paths, urls, etc.)
                        setattr(stub, attr, default)
            else:
                setattr(stub, attr, default)
        meta = settings_mod._settings_metadata(stub, list(config._META_REGISTRY.keys()))
        not_default = [k for k, v in meta.items() if v["customized"]]
        assert not_default == [], (
            f"customized=True when value equals default for: {not_default}"
        )

    def test_metadata_customized_true_when_one_value_differs(self):
        from types import SimpleNamespace
        from readsbstats import config, web as web_mod
        # Same defaulted stub as above, but flip one int value.
        stub = SimpleNamespace()
        for payload_key, reg in config._META_REGISTRY.items():
            default = reg["default"]
            attr = reg["config_attr"]
            if isinstance(default, bool):
                setattr(stub, attr, default)
            elif isinstance(default, str):
                try:
                    setattr(stub, attr, int(default))
                except ValueError:
                    try:
                        setattr(stub, attr, float(default))
                    except ValueError:
                        setattr(stub, attr, default)
            else:
                setattr(stub, attr, default)
        # Override one well-known integer setting.
        stub.POLL_INTERVAL_SEC = 999
        meta = settings_mod._settings_metadata(stub, list(config._META_REGISTRY.keys()))
        assert meta["poll_interval"]["customized"] is True
        # No others should flip.
        others_customized = [k for k, v in meta.items()
                              if k != "poll_interval" and v["customized"]]
        assert others_customized == []

    def test_metadata_telegram_token_customized_from_raw_value(self, client, monkeypatch):
        """The display value for telegram_token is masked ("configured" or
        "not set"). The customized flag must be computed from the raw
        config attribute, not the masked display string — otherwise
        every "not set" row would falsely report customized."""
        from readsbstats import config
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        r = client.get("/api/settings")
        assert r.json()["_metadata"]["telegram_token"]["customized"] is False
        # Repeat with a real-looking value.
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "1234:secret")
        from readsbstats import web as web_mod
        cache._cache.clear()
        r2 = client.get("/api/settings")
        assert r2.json()["_metadata"]["telegram_token"]["customized"] is True


class TestApiFeeders:
    def test_api_feeders_returns_json(self, client, monkeypatch):
        async def mock_feeders():
            return [{"name": "readsb", "unit": "readsb.service",
                     "systemd": "active", "overall": "ok"}]
        monkeypatch.setattr(feeders, "_check_all_feeders", mock_feeders)
        r = client.get("/api/feeders")
        assert r.status_code == 200
        body = r.json()
        assert body["has_feeders"] is True
        assert any(f["name"] == "readsb" for f in body["feeders"])

    def test_api_feeders_empty_when_no_feeders_configured(self, client, monkeypatch):
        monkeypatch.setattr(config, "FEEDERS", [])
        r = client.get("/api/feeders")
        assert r.status_code == 200
        body = r.json()
        assert body["has_feeders"] is False
        assert body["feeders"] == []

    def test_api_feeders_caches_within_ttl(self, client, monkeypatch):
        # BE-18: a second request inside the TTL window must be served from
        # cache and NOT spawn another feeder-check batch.
        calls = {"n": 0}

        async def mock_feeders():
            calls["n"] += 1
            return [{"name": "readsb", "unit": "readsb.service", "overall": "ok"}]

        monkeypatch.setattr(config, "FEEDERS",
                            [{"name": "readsb", "unit": "readsb.service"}])
        monkeypatch.setattr(feeders, "_check_all_feeders", mock_feeders)
        r1 = client.get("/api/feeders")
        r2 = client.get("/api/feeders")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["feeders"] == r2.json()["feeders"]
        assert calls["n"] == 1

    def test_api_feeders_concurrent_requests_share_one_batch(self, monkeypatch):
        # BE-18: two concurrent requests with a cold cache must coalesce into a
        # single batch via the asyncio.Lock (the loser re-reads the cache).
        import asyncio as _asyncio

        cache._cache.clear()
        monkeypatch.setattr(cache, "_feeders_lock", None)
        monkeypatch.setattr(config, "FEEDERS",
                            [{"name": "a", "unit": "a.service"}])
        calls = {"n": 0}

        async def slow_batch():
            calls["n"] += 1
            await _asyncio.sleep(0.05)
            return [{"name": "a", "unit": "a.service", "overall": "ok"}]

        monkeypatch.setattr(feeders, "_check_all_feeders", slow_batch)

        async def drive():
            return await _asyncio.gather(feeders.api_feeders(), feeders.api_feeders())

        # Use the shared loop (not asyncio.run, which closes it and breaks
        # every later get_event_loop().run_until_complete() test in the suite).
        results = _asyncio.get_event_loop().run_until_complete(drive())
        assert calls["n"] == 1
        assert all(r["has_feeders"] for r in results)
        assert all(r["feeders"][0]["name"] == "a" for r in results)


class TestSpaMount:
    """React SPA mount at root (post-v2.0.0 cutover).

    The mount is registered at module-import time and gated by presence of
    `frontend/dist/index.html` + `frontend/dist/assets/`. Missing dist →
    mount silently doesn't register; every UI path 404s but /api/* keeps
    working. /v2/* paths from the RC era 301-redirect to / for back-compat.
    """

    def test_root_404_when_dist_missing(self, client):
        if web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist present — covered by dist-present tests")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 404

    def test_deep_path_404_when_dist_missing(self, client):
        if web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist present — covered by dist-present tests")
        r = client.get("/flight/123", follow_redirects=False)
        assert r.status_code == 404

    def test_root_returns_shell_when_dist_present(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # index.html must NEVER be cached — hashed asset URLs inside change
        # every deploy. A cached shell points at non-existent files.
        assert r.headers.get("cache-control") == "no-store"
        # The shell should reference the prod base path.
        assert b"/stats/" in r.content

    def test_deep_refresh_returns_shell(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        # React Router deep-refresh (e.g., user reloads /stats/flight/123)
        # must get the SPA shell so client-side routing can take over.
        r = client.get("/flight/123")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_missing_asset_404s_not_shell(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        # If we returned the SPA shell here, missing-asset bugs would
        # masquerade as a blank page (browser tries to execute HTML as JS).
        r = client.get("/assets/does-not-exist.js", follow_redirects=False)
        assert r.status_code == 404

    def test_dotted_path_with_known_ext_404s(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        r = client.get("/anywhere/foo.css", follow_redirects=False)
        assert r.status_code == 404

    def test_built_asset_served(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        shell = client.get("/").text
        import re
        m = re.search(r'(/stats/assets/[^"\s>]+)', shell)
        if not m:
            pytest.skip("no asset URL discovered in shell")
        r = client.get(m.group(1))
        assert r.status_code == 200, f"asset {m.group(1)} not served"

    def test_index_html_no_cache_control(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        r = client.get("/some/deep/path")
        assert r.headers.get("cache-control") == "no-store"

    def test_v2_path_redirects_to_root(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        r = client.get("/v2/flight/123", follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"].endswith("/flight/123")

    def test_v2_bare_redirects_to_root(self, client):
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        r = client.get("/v2", follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"].endswith("/")

    def test_v2_open_redirect_blocked(self, client, monkeypatch):
        """CodeQL py/url-redirection (alert #28) — the `rest:path` captured
        segment was interpolated into the Location header verbatim. With
        an empty root_path (dev mode without nginx prefix), a request like
        `/v2//evil.com` produced `Location: //evil.com`, which browsers
        treat as scheme-relative and follow off-site. Defence: strip
        leading `/` and `\\` from `rest` before building the target.

        The production `root_path=/stats` shields against this because the
        Location starts with `/stats/…` — but the test deliberately
        simulates dev mode (`root_path=""`) so the fix is verified in the
        exact configuration the vulnerability surfaces in."""
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        monkeypatch.setattr(web.app, "root_path", "")

        for hostile_path in (
            "/v2//evil.com",
            "/v2///evil.com",
            "/v2/\\evil.com",
            "/v2/\\\\evil.com",
            "/v2//\\evil.com",
        ):
            r = client.get(hostile_path, follow_redirects=False)
            assert r.status_code == 301, hostile_path
            loc = r.headers["location"]
            # Location must not start with `//` or `/\` (or their %-encoded
            # variants) — all interpreted as off-site by at least one major
            # browser.
            assert not loc.startswith("//"), f"{hostile_path} → {loc}"
            assert not loc.startswith("/\\"), f"{hostile_path} → {loc}"
            assert not loc.startswith("/%5C"), f"{hostile_path} → {loc}"
            assert not loc.startswith("/%2F"), f"{hostile_path} → {loc}"

    def test_v2_compat_strips_crlf_from_rest(self):
        """Audit-12 #149 defence-in-depth — Starlette validates the path
        component today and rejects raw CR/LF with 404, but the redirect
        helper must also strip them locally in case a future ASGI server
        weakens that guard."""
        sanitize = web._sanitize_v2_rest
        # Raw CR/LF in `rest` — defensive scrub. Note: the audit-12 P8
        # follow-up added `urllib.parse.quote` on top of the CR/LF strip,
        # so the remaining `:` / ` ` / `=` characters of the Set-Cookie
        # smuggle attempt also get percent-encoded — defence-in-depth.
        assert sanitize("foo\r\nSet-Cookie: pwned=1") == "fooSet-Cookie%3A%20pwned%3D1"
        assert sanitize("foo\rbar") == "foobar"
        assert sanitize("foo\nbar") == "foobar"

    def test_v2_compat_strips_leading_slash_and_backslash(self):
        """CodeQL #28 regression — _sanitize_v2_rest strips leading `/` and
        `\\` so the Location can't become scheme-relative."""
        sanitize = web._sanitize_v2_rest
        assert sanitize("/evil.com") == "evil.com"
        assert sanitize("//evil.com") == "evil.com"
        assert sanitize("\\evil.com") == "evil.com"
        assert sanitize("\\\\evil.com") == "evil.com"
        assert sanitize("/\\evil.com") == "evil.com"
        # CR/LF is also stripped on top of leading-slash strip
        assert sanitize("/\r\nevil.com") == "evil.com"

    def test_v2_compat_passes_through_safe_paths(self):
        sanitize = web._sanitize_v2_rest
        assert sanitize("flight/123") == "flight/123"
        assert sanitize("") == ""
        assert sanitize("aircraft/abc123") == "aircraft/abc123"

    def test_v2_compat_percent_encodes_url_specials(self):
        """Audit-12 P8 follow-up — the sanitizer must percent-encode
        characters that would otherwise produce a malformed Location
        header. The first cut of the fix landed only the CR/LF strip
        without the URL-quote step."""
        sanitize = web._sanitize_v2_rest
        # Spaces become %20
        assert sanitize("flight 123") == "flight%20123"
        # Quotes (the smuggling vector if anyone tries header injection
        # via a non-CRLF byte) get encoded
        assert sanitize('aircraft/"x"') == "aircraft/%22x%22"
        # `?` and `#` would otherwise truncate the path at the URL level
        assert sanitize("flight/123?x=1") == "flight/123%3Fx%3D1"
        # The `/` is in the safe set so it stays intact
        assert sanitize("flight/123/positions") == "flight/123/positions"

    def test_v2_compat_urlparse_guard_falls_back_to_root(
        self, client, monkeypatch,
    ):
        """CodeQL #29 (py/url-redirection) — the `_v2_compat` handler now
        runs a CodeQL-recognized `urlparse(target).scheme/.netloc` check
        on the final redirect target. If anything slips past
        `_sanitize_v2_rest` and produces a target with a scheme or
        netloc, the handler falls back to redirecting to the SPA root
        instead of honouring the off-site target.

        Patch the sanitizer to a "broken" version that returns a hostile
        value, and verify the route still produces a same-origin
        redirect."""
        if not web.SPA_INDEX.is_file():
            pytest.skip("frontend/dist not built")
        monkeypatch.setattr(web.app, "root_path", "")
        # Defeat the real sanitizer
        monkeypatch.setattr(web, "_sanitize_v2_rest", lambda rest: "/evil.com")

        r = client.get("/v2/anything", follow_redirects=False)
        assert r.status_code == 301
        loc = r.headers["location"]
        # urlparse("/evil.com") has no scheme/netloc — but our handler
        # builds the target as f"{root}/{sanitized}" = "//evil.com" when
        # root="" and sanitized="/evil.com". That target HAS a netloc,
        # so the guard kicks in and we redirect to root.
        assert not loc.startswith("//"), f"open-redirect not blocked → {loc}"
        # The fallback is just the root path
        assert loc == "/"


# ---------------------------------------------------------------------------
# Feeder health check helpers
# ---------------------------------------------------------------------------

class TestFeederChecks:
    @pytest.fixture(autouse=True)
    def setup(self):
        yield

    def test_check_systemd_unit_rejects_flag_like_name(self):
        """A13-042: a unit name starting with '-' would be parsed as a
        systemctl flag — rejected before any subprocess is spawned."""
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            feeders._check_systemd_unit("--evil"))
        assert result == {"systemd": "invalid-unit-name"}

    def test_feeder_details_mlat_rejects_flag_like_name(self):
        """A13-042 twin for journalctl."""
        import asyncio
        details = asyncio.get_event_loop().run_until_complete(
            feeders._feeder_details_mlat("-o evil"))
        assert details == []

    def test_check_port_closed(self):
        import asyncio
        import socket as _socket
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                          # nothing listening any more
        result = asyncio.get_event_loop().run_until_complete(
            feeders._check_port(port))
        assert result["port_status"] == "closed"

    def test_check_port_timeout(self, monkeypatch):
        import asyncio

        async def timing_out_wait_for(coro, timeout):
            coro.close()       # silence the never-awaited warning
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", timing_out_wait_for)
        result = asyncio.get_event_loop().run_until_complete(
            feeders._check_port(30005))
        assert result["port_status"] == "timeout"

    def test_is_safe_status_path_rejects_nul_byte(self):
        # realpath() raises ValueError on an embedded NUL — must mean False,
        # not an exception escaping into the handler.
        assert feeders._is_safe_status_path("/run/x\0y") is False

    def test_is_safe_status_url_rejects_unparseable(self):
        assert feeders._is_safe_status_url("http://[") is False

    def test_fetch_details_rejects_unsafe_piaware_path(self, caplog):
        import asyncio
        feeder = {"name": "pia", "status_type": "piaware",
                  "status_path": "/etc/passwd"}
        with caplog.at_level("WARNING"):
            details = asyncio.get_event_loop().run_until_complete(
                feeders._fetch_feeder_details(feeder))
        assert details == []
        assert any("rejecting status_path" in r.getMessage()
                   for r in caplog.records)

    def test_fetch_details_exception_logged_returns_empty(self, monkeypatch, caplog):
        """audit-12 #151: a failing details fetch must be visible in the log,
        not silently swallowed as []."""
        import asyncio

        async def boom(unit):
            raise RuntimeError("mlat details fail")

        monkeypatch.setattr(feeders, "_feeder_details_mlat", boom)
        feeder = {"name": "m", "status_type": "mlat", "unit": "mlat-client.service"}
        with caplog.at_level("WARNING"):
            details = asyncio.get_event_loop().run_until_complete(
                feeders._fetch_feeder_details(feeder))
        assert details == []
        assert any("details fetch failed" in r.getMessage()
                   for r in caplog.records)

    def test_check_systemd_unit_active(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            class Proc:
                async def communicate(self):
                    return (b"active\n", b"")
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_systemd_unit("test.service"))
        assert result["systemd"] == "active"

    def test_check_systemd_unit_not_found(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise FileNotFoundError("systemctl not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_systemd_unit("test.service"))
        assert result["systemd"] == "unavailable"

    def test_check_systemd_unit_timeout_kills_subprocess(self, monkeypatch):
        """Audit-12 #152 — wait_for(communicate()) timing out used to leak
        the systemctl child process. The fix wraps the helper in
        try/except TimeoutError: proc.kill(); await proc.wait()."""
        import asyncio
        kill_calls: list[bool] = []
        wait_calls: list[bool] = []

        class Proc:
            async def communicate(self):
                return (b"", b"")

            def kill(self):
                kill_calls.append(True)

            async def wait(self):
                wait_calls.append(True)

        async def mock_exec(*args, **kwargs):
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        # Force wait_for to raise TimeoutError without actually waiting.
        async def insta_timeout(coro, timeout):
            coro.close()  # don't leave the unawaited coroutine open
            raise asyncio.TimeoutError()
        monkeypatch.setattr(web.asyncio, "wait_for", insta_timeout)

        result = asyncio.new_event_loop().run_until_complete(
            feeders._check_systemd_unit("test.service")
        )
        assert result["systemd"] == "timeout"
        assert kill_calls == [True], "kill() was not called on timeout"
        assert wait_calls == [True], "await wait() was not called after kill()"

    def test_feeder_details_mlat_timeout_kills_subprocess(self, monkeypatch):
        """Same fix in _feeder_details_mlat."""
        import asyncio
        kill_calls: list[bool] = []
        wait_calls: list[bool] = []

        class Proc:
            async def communicate(self):
                return (b"", b"")

            def kill(self):
                kill_calls.append(True)

            async def wait(self):
                wait_calls.append(True)

        async def mock_exec(*args, **kwargs):
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        async def insta_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()
        monkeypatch.setattr(web.asyncio, "wait_for", insta_timeout)

        result = asyncio.new_event_loop().run_until_complete(
            feeders._feeder_details_mlat("test.service")
        )
        # On timeout, returns whatever details accumulated (empty list)
        assert isinstance(result, list)
        assert kill_calls == [True]
        assert wait_calls == [True]

    def test_check_port_open(self, monkeypatch):
        import asyncio

        async def mock_connect(host, port):
            class Writer:
                def close(self): pass
                async def wait_closed(self): pass
            return None, Writer()

        monkeypatch.setattr(asyncio, "open_connection", mock_connect)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_port(30005))
        assert result["port_status"] == "open"
        assert result["port"] == 30005

    def test_check_port_closed(self, monkeypatch):
        import asyncio

        async def mock_connect(host, port):
            raise ConnectionRefusedError()

        monkeypatch.setattr(asyncio, "open_connection", mock_connect)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_port(30005))
        assert result["port_status"] == "closed"

    def test_check_single_feeder_ok(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "active"}

        async def mock_port(port, host="127.0.0.1"):
            return {"port": port, "port_status": "open"}

        monkeypatch.setattr(feeders, "_check_systemd_unit", mock_systemd)
        monkeypatch.setattr(feeders, "_check_port", mock_port)
        feeder = {"name": "readsb", "unit": "readsb.service", "port": 30005}
        result = asyncio.get_event_loop().run_until_complete(feeders._check_single_feeder(feeder))
        assert result["overall"] == "ok"
        assert result["systemd"] == "active"
        assert result["port_status"] == "open"

    def test_check_single_feeder_error(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "inactive"}

        monkeypatch.setattr(feeders, "_check_systemd_unit", mock_systemd)
        feeder = {"name": "test", "unit": "test.service"}
        result = asyncio.get_event_loop().run_until_complete(feeders._check_single_feeder(feeder))
        assert result["overall"] == "error"

    def test_check_single_feeder_unavailable(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "unavailable"}

        monkeypatch.setattr(feeders, "_check_systemd_unit", mock_systemd)
        feeder = {"name": "test", "unit": "test.service"}
        result = asyncio.get_event_loop().run_until_complete(feeders._check_single_feeder(feeder))
        assert result["overall"] == "unknown"


class TestFeederDetailParsers:
    def test_readsb_details_from_json(self, tmp_path):
        status_path = str(tmp_path)
        ac_path = tmp_path / "aircraft.json"
        ac_path.write_text('{"aircraft": [{"hex": "a"}, {"hex": "b"}]}')
        stats_path = tmp_path / "stats.json"
        stats_path.write_text(json.dumps({
            "last1min": {
                "start": 1000, "end": 1060, "messages": 3000,
                "local": {"signal": -8.5, "noise": -32.1},
                "max_distance": 150.5,
            }
        }))
        details = feeders._feeder_details_readsb(status_path)
        labels = {k for k, _ in details}
        assert "Aircraft tracked" in labels
        assert "Messages/s" in labels
        assert "Signal" in labels
        assert "Max range" in labels
        assert any(v == "2" for _, v in details if _ == "Aircraft tracked")
        # STY-6: a valid window (end > start) yields the correct rate:
        # 3000 msgs / (1060 - 1000) s = 50.
        msgs_per_s = next(v for k, v in details if k == "Messages/s")
        assert msgs_per_s == "50"

    def test_readsb_messages_per_s_omitted_on_zero_end(self, tmp_path):
        # STY-6: when end/start are missing/zero (end <= start) the duration is
        # unknown, so the "Messages/s" row must be OMITTED rather than computed
        # against a stale 60 s fallback (which printed a misleading rate).
        (tmp_path / "aircraft.json").write_text('{"aircraft": []}')
        (tmp_path / "stats.json").write_text(json.dumps({
            "last1min": {
                "start": 0, "end": 0, "messages": 3000,
                "local": {"signal": -8.5},
                "max_distance": 150.5,
            }
        }))
        details = feeders._feeder_details_readsb(str(tmp_path))
        labels = {k for k, _ in details}
        assert "Messages/s" not in labels
        # The rest of the card still renders.
        assert "Signal" in labels
        assert "Max range" in labels

    def test_readsb_messages_per_s_omitted_on_inverted_window(self, tmp_path):
        # STY-6: a malformed end < start window is also unknown-duration → omit.
        (tmp_path / "aircraft.json").write_text('{"aircraft": []}')
        (tmp_path / "stats.json").write_text(json.dumps({
            "last1min": {"start": 2000, "end": 1000, "messages": 600, "local": {}},
        }))
        details = feeders._feeder_details_readsb(str(tmp_path))
        assert "Messages/s" not in {k for k, _ in details}

    def test_readsb_details_max_distance_converted_to_nm(self, tmp_path):
        # max_distance in stats.json is meters; server returns raw nm string for JS unit formatting
        (tmp_path / "aircraft.json").write_text('{"aircraft": []}')
        (tmp_path / "stats.json").write_text(json.dumps({
            "last1min": {
                "start": 1000, "end": 1060, "messages": 1,
                "local": {},
                "max_distance": 185200,  # exactly 100 nm
            }
        }))
        details = feeders._feeder_details_readsb(str(tmp_path))
        max_range = next((v for k, v in details if k == "Max range"), None)
        assert max_range == "100.0", f"expected '100.0', got {max_range!r}"

    def test_readsb_details_missing_files(self, tmp_path):
        details = feeders._feeder_details_readsb(str(tmp_path))
        assert details == []

    def test_piaware_details_from_json(self, tmp_path):
        path = tmp_path / "status.json"
        path.write_text(json.dumps({
            "piaware_version": "9.0",
            "piaware": {"status": "running"},
            "radio": {"message": "Mode S enabled"},
            "cpu_temp_celcius": 52.3,
        }))
        details = feeders._feeder_details_piaware(str(path))
        labels = {k for k, _ in details}
        assert "Version" in labels
        assert "Piaware" in labels
        assert "Radio" in labels
        assert "CPU temp" in labels

    def test_piaware_details_missing_file(self, tmp_path):
        details = feeders._feeder_details_piaware(str(tmp_path / "missing.json"))
        assert details == []

    def test_read_json_file_valid(self, tmp_path):
        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        assert feeders._read_json_file(str(p)) == {"key": "value"}

    def test_read_json_file_invalid(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{broken")
        assert feeders._read_json_file(str(p)) is None

    def test_read_json_file_missing(self):
        assert feeders._read_json_file("/nonexistent/path.json") is None

    def test_check_port_timeout(self, monkeypatch):
        import asyncio

        async def mock_connect(host, port):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "open_connection", mock_connect)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_port(30005))
        # asyncio.TimeoutError is a subclass of OSError in Python 3.11+,
        # so it's caught as "closed" rather than "timeout"
        assert result["port_status"] in ("timeout", "closed")

    def test_check_systemd_timeout(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_systemd_unit("test.service"))
        assert result["systemd"] == "timeout"

    def test_check_systemd_generic_error(self, monkeypatch):
        # STY-5: a generic failure must return a FIXED token, not echo the raw
        # exception text into the API/status payload (info leak + noisy UI).
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise RuntimeError("boom secret /etc/path detail")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(feeders._check_systemd_unit("test.service"))
        assert result == {"systemd": "error"}
        assert "boom" not in result["systemd"]  # no raw exception text leaked

    def test_fetch_feeder_details_readsb_dispatch(self, monkeypatch, tmp_path):
        import asyncio
        status_path = str(tmp_path)
        (tmp_path / "aircraft.json").write_text('{"aircraft": []}')
        # Bypass the /run/ allowlist so the dispatcher reaches the real fetcher.
        monkeypatch.setattr(feeders, "_is_safe_status_path", lambda _p: True)
        feeder = {"name": "readsb", "unit": "readsb.service", "status_type": "readsb", "status_path": status_path}
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert isinstance(result, list)

    def test_fetch_feeder_details_unknown_type(self):
        import asyncio
        feeder = {"name": "x", "unit": "x.service", "status_type": "unknown"}
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert result == []

    def test_fr24_details_success(self, monkeypatch):
        import asyncio
        import json
        import httpx as _httpx

        payload = {
            "build_version": "1.2.3",
            "feed_status": "connected",
            "feed_alias": "T-KZXX1",
            "feed_num_ac_tracked": 42,
            "rx_connected": "1",
            "mlat-ok": "0",
        }

        class FakeStream:
            def __init__(self): self.body = json.dumps(payload).encode()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def raise_for_status(self): pass
            async def aiter_bytes(self):
                yield self.body

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def stream(self, method, url): return FakeStream()

        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: FakeClient())
        result = asyncio.get_event_loop().run_until_complete(
            feeders._feeder_details_fr24("http://localhost/monitor.json")
        )
        labels = {k for k, _ in result}
        assert "Version" in labels
        assert "FR24 link" in labels
        assert "Radar code" in labels
        assert "Aircraft tracked" in labels
        assert "Receiver" in labels
        assert "MLAT" in labels
        # Check specific values
        assert any(v == "connected" for k, v in result if k == "Receiver")
        assert any(v == "not ok" for k, v in result if k == "MLAT")

    def test_fr24_details_network_error(self, monkeypatch):
        import asyncio
        import httpx as _httpx

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def stream(self, method, url): raise ConnectionError("down")

        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: FakeClient())
        result = asyncio.get_event_loop().run_until_complete(
            feeders._feeder_details_fr24("http://localhost/monitor.json")
        )
        assert result == []

    def test_mlat_details_parses_journal(self, monkeypatch):
        import asyncio

        journal_output = (
            b"some noise line\n"
            b"Server: mlat.example.com\n"
            b"peer_count: 15\n"
            b"Aircraft: 5 of 12 Mode-S\n"
            b"Results: 8.3 positions/minute\n"
        )

        async def mock_exec(*args, **kwargs):
            class Proc:
                async def communicate(self):
                    return (journal_output, b"")
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(
            feeders._feeder_details_mlat("test-mlat.service")
        )
        labels = {k for k, _ in result}
        assert "Positions/min" in labels
        assert "Aircraft" in labels
        assert "Peers" in labels
        assert "Server" in labels

    def test_mlat_details_journal_error(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise FileNotFoundError("journalctl not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(
            feeders._feeder_details_mlat("test-mlat.service")
        )
        assert result == []

    def test_fetch_feeder_details_fr24_dispatch(self, monkeypatch):
        import asyncio

        async def fake_fr24(url):
            return [("Version", "1.0")]

        monkeypatch.setattr(feeders, "_feeder_details_fr24", fake_fr24)
        # Loopback URL passes the SSRF allowlist.
        feeder = {"name": "fr24", "unit": "fr24.service", "status_type": "fr24",
                  "status_url": "http://127.0.0.1:8754/monitor.json"}
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert result == [("Version", "1.0")]

    def test_fetch_feeder_details_piaware_dispatch(self, monkeypatch, tmp_path):
        import asyncio
        path = str(tmp_path / "status.json")
        (tmp_path / "status.json").write_text('{"piaware_version": "9"}')
        # Bypass the /run/ allowlist so the dispatcher reaches the real fetcher.
        monkeypatch.setattr(feeders, "_is_safe_status_path", lambda _p: True)
        feeder = {"name": "piaware", "unit": "piaware.service", "status_type": "piaware", "status_path": path}
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert any(k == "Version" for k, _ in result)

    def test_fetch_feeder_details_mlat_dispatch(self, monkeypatch):
        import asyncio

        async def fake_mlat(unit):
            return [("Peers", "10")]

        monkeypatch.setattr(feeders, "_feeder_details_mlat", fake_mlat)
        feeder = {"name": "mlat", "unit": "mlat.service", "status_type": "mlat"}
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert result == [("Peers", "10")]

    def test_check_all_feeders(self, monkeypatch):
        import asyncio

        async def mock_single(feeder):
            return {"name": feeder["name"], "overall": "ok"}

        monkeypatch.setattr(feeders, "_check_single_feeder", mock_single)
        monkeypatch.setattr(config, "FEEDERS", [{"name": "a", "unit": "a.service"}, {"name": "b", "unit": "b.service"}])
        result = asyncio.get_event_loop().run_until_complete(feeders._check_all_feeders())
        assert len(result) == 2
        assert result[0]["name"] == "a"

    # ---------- status_path / status_url allowlist (defence-in-depth) ----------

    def test_is_safe_status_path_accepts_run_subdir(self):
        assert feeders._is_safe_status_path("/run/readsb")
        assert feeders._is_safe_status_path("/run/piaware/status.json")
        assert feeders._is_safe_status_path("/run")

    def test_is_safe_status_path_rejects_traversal(self):
        assert not feeders._is_safe_status_path("/run/../etc/hostname")
        assert not feeders._is_safe_status_path("/etc/passwd")
        assert not feeders._is_safe_status_path("/")
        assert not feeders._is_safe_status_path("/runaway/x")  # prefix-only match must require /

    def test_is_safe_status_path_rejects_empty_and_bad_types(self):
        assert not feeders._is_safe_status_path("")
        assert not feeders._is_safe_status_path(None)  # type: ignore[arg-type]

    def test_is_safe_status_path_honours_env_override(self, tmp_path, monkeypatch):
        # improvements.md #136: tests should be able to set the root via
        # config.FEEDER_STATUS_ROOT (backed by RSBS_FEEDER_STATUS_ROOT)
        # rather than depending on the production /run path.
        sub = tmp_path / "readsb"
        sub.mkdir()
        monkeypatch.setattr(config, "FEEDER_STATUS_ROOT", str(tmp_path))
        assert feeders._is_safe_status_path(str(sub / "stats.json"))
        assert feeders._is_safe_status_path(str(tmp_path))
        # /run is no longer the root, so a real /run path is now rejected
        assert not feeders._is_safe_status_path("/run/readsb/stats.json")

    def test_is_safe_status_url_accepts_loopback_http(self):
        assert feeders._is_safe_status_url("http://127.0.0.1:8754/monitor.json")
        assert feeders._is_safe_status_url("http://localhost:8754/")
        assert feeders._is_safe_status_url("http://[::1]:8754/")

    def test_is_safe_status_url_rejects_external_hosts(self):
        assert not feeders._is_safe_status_url("http://169.254.169.254/latest/meta-data/")
        assert not feeders._is_safe_status_url("http://example.com/")
        assert not feeders._is_safe_status_url("http://10.0.0.1/")

    def test_is_safe_status_url_rejects_non_http_schemes(self):
        # https on loopback is fine in principle but we keep the allowlist
        # tight: feeders all expose plain http on loopback by design.
        assert not feeders._is_safe_status_url("https://127.0.0.1/")
        assert not feeders._is_safe_status_url("file:///etc/passwd")
        assert not feeders._is_safe_status_url("ftp://127.0.0.1/")

    def test_is_safe_status_url_rejects_empty_and_bad(self):
        assert not feeders._is_safe_status_url("")
        assert not feeders._is_safe_status_url(None)  # type: ignore[arg-type]
        assert not feeders._is_safe_status_url("not a url")

    def test_fetch_feeder_details_rejects_unsafe_status_path(self, monkeypatch):
        import asyncio
        # Without the allowlist, this would call _read_json_file("/etc/hostname")
        # and could leak file existence / contents through error handling.
        feeder = {"name": "x", "unit": "x.service", "status_type": "readsb",
                  "status_path": "/etc"}
        called = {"hit": False}

        def boom(_p):
            called["hit"] = True
            return [("should not be called", "x")]

        monkeypatch.setattr(feeders, "_feeder_details_readsb", boom)
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert result == []
        assert called["hit"] is False

    def test_fetch_feeder_details_rejects_unsafe_status_url(self, monkeypatch):
        import asyncio
        # Without the allowlist, this could SSRF cloud metadata.
        feeder = {"name": "x", "unit": "x.service", "status_type": "fr24",
                  "status_url": "http://169.254.169.254/latest/meta-data/"}
        called = {"hit": False}

        async def boom(_url):
            called["hit"] = True
            return [("should not be called", "x")]

        monkeypatch.setattr(feeders, "_feeder_details_fr24", boom)
        result = asyncio.get_event_loop().run_until_complete(feeders._fetch_feeder_details(feeder))
        assert result == []
        assert called["hit"] is False


# ---------------------------------------------------------------------------
# API: /api/aircraft/{icao_hex}/flights
# ---------------------------------------------------------------------------

class TestApiAircraftFlights:
    def test_empty_returns_zero_total(self, client):
        r = client.get("/api/aircraft/aabbcc/flights")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["icao_hex"] == "aabbcc"

    def test_unknown_icao_200_empty_contract(self, client):
        # BUG-9: a well-formed but never-seen ICAO is 200-with-empty by
        # contract (NOT 404). Pin the shape so a future change to 404 is a
        # deliberate, reviewed decision rather than an accident.
        r = client.get("/api/aircraft/abcdef/flights")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["flights"] == []
        assert body["icao_hex"] == "abcdef"
        # aircraft_info is present but sparse: the COUNT/MIN/MAX aggregate row
        # exists with NULLs, plus the computed country key. No aircraft_db row,
        # so registration/type_code/type_desc/flags are absent or None.
        info = body["aircraft_info"]
        assert info["total_flights"] == 0
        assert info["first_seen"] is None
        assert info["last_seen"] is None
        assert info.get("registration") is None

    def test_returns_flights_for_icao(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc")
        insert_flight(db_conn, icao="aabbcc", first_seen=1_010_000)
        insert_flight(db_conn, icao="ddeeff")
        r = client.get("/api/aircraft/aabbcc/flights")
        assert r.json()["total"] == 2

    def test_tilde_prefix_stripped(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/~AABBCC/flights")
        assert r.json()["total"] == 1

    def test_aircraft_info_included(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/aabbcc/flights")
        info = r.json()["aircraft_info"]
        assert "total_flights" in info
        assert "country" in info

    def test_sort_asc(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_001)
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_002)
        r = client.get("/api/aircraft/aabbcc/flights?sort_by=first_seen&sort_dir=asc")
        flights = r.json()["flights"]
        assert flights[0]["first_seen"] == 1_000_001

    def test_sort_injection_falls_back_to_default(self, client, db_conn):
        """sort_by goes through the _SORT_COLS allowlist (.get with default) —
        an injection payload must be a no-op, same as /api/flights."""
        insert_flight(db_conn, icao="aabbcc")
        r = client.get(
            "/api/aircraft/aabbcc/flights?sort_by=first_seen;DROP TABLE flights")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_invalid_icao_rejected_404(self, client):
        # BE-11: a non-hex / wrong-length path param is rejected before any
        # DB or external work.
        assert client.get("/api/aircraft/zzzzzz/flights").status_code == 404
        assert client.get("/api/aircraft/abcd/flights").status_code == 404
        assert client.get("/api/aircraft/aabbccdd/flights").status_code == 404


# ---------------------------------------------------------------------------
# API: /api/stats
# ---------------------------------------------------------------------------

class TestApiStats:
    def test_empty_db_returns_200(self, client, clear_web_cache):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert "total_flights" in data
        assert data["total_flights"] == 0

    def test_counts_inserted_flight(self, client, db_conn, clear_web_cache):
        insert_flight(db_conn, first_seen=int(time.time()) - 3600)
        r = client.get("/api/stats")
        assert r.json()["total_flights"] == 1

    def test_filtered_by_range(self, client, db_conn, clear_web_cache):
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        insert_flight(db_conn, icao="aa0002", first_seen=2_000_000)
        r = client.get("/api/stats?from=900000&to=1500000")
        assert r.status_code == 200
        assert r.json()["total_flights"] == 1

    def test_stats_range_is_half_open_excludes_to_bound(
        self, client, db_conn, clear_web_cache,
    ):
        # BE-16 (Audit 2026-05-31): the stats date range must be half-open
        # [from, to) — matching history/export — so a flight whose first_seen
        # equals `to` is excluded and day-boundary buckets never double-count.
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)  # inside
        insert_flight(db_conn, icao="aa0002", first_seen=1_500_000)  # == to → excluded
        r = client.get("/api/stats?from=900000&to=1500000")
        assert r.status_code == 200
        assert r.json()["total_flights"] == 1

    def test_new_aircraft_no_duplicate_on_same_first_seen(
        self, client, db_conn, clear_web_cache,
    ):
        # BE-15 (Audit 2026-05-31): the new_aircraft self-join matches
        # `f2.first_seen = sub.first_seen_ever`. Two flights for one ICAO with
        # the SAME first_seen (same second) match both rows → duplicate items
        # and non-deterministic reg/type. The join must disambiguate by id so
        # exactly one representative row comes back.
        now = int(time.time())
        fs = now - 3600  # within the 24h "new aircraft" window
        insert_flight(db_conn, icao="aabbcc", registration="REG-A",
                      aircraft_type="A320", first_seen=fs, last_seen=fs + 600)
        insert_flight(db_conn, icao="aabbcc", registration="REG-B",
                      aircraft_type="B738", first_seen=fs, last_seen=fs + 600)
        r = client.get("/api/stats")
        items = r.json()["new_aircraft"]["items"]
        matching = [it for it in items if it["icao_hex"] == "aabbcc"]
        assert len(matching) == 1, f"expected one new-aircraft row, got {matching}"

    def test_result_cached_on_second_call(self, client, db_conn, clear_web_cache):
        insert_flight(db_conn, first_seen=int(time.time()) - 3600)
        client.get("/api/stats")
        # Insert another flight; cached response should still return 1
        insert_flight(db_conn, icao="ddeeff", first_seen=int(time.time()) - 1800)
        r = client.get("/api/stats")
        assert r.json()["total_flights"] == 1

    def test_filtered_result_cached_on_second_call(self, client, db_conn, clear_web_cache):
        """Filtered (date-range) responses must also be cached — repeated identical
        requests should not recompute aggregates."""
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        r1 = client.get("/api/stats?from=500000&to=1500000")
        assert r1.json()["total_flights"] == 1
        # Insert another flight inside the range — cache should still return 1
        insert_flight(db_conn, icao="aa0002", first_seen=1_200_000)
        r2 = client.get("/api/stats?from=500000&to=1500000")
        assert r2.json()["total_flights"] == 1

    def test_previous_window_returns_prev_period_totals_when_filtered(
        self, client, db_conn, clear_web_cache,
    ):
        """For a filtered range [from, to] (length D), previous_window should
        contain totals for [from - D, from] so the frontend can render delta
        chips on every KPI card, not just the legacy 24h/7d ones."""
        # Window: 2_000_000..3_000_000 (length 1_000_000). Prev: 1_000_000..2_000_000.
        # Seed 3 flights in current, 5 in prev, 1 outside both.
        for i, ts in enumerate([2_100_000, 2_500_000, 2_900_000]):
            insert_flight(db_conn, icao=f"cc{i:04d}", first_seen=ts,
                          total_positions=10)
        for i, ts in enumerate([1_100_000, 1_300_000, 1_500_000, 1_700_000, 1_900_000]):
            insert_flight(db_conn, icao=f"pp{i:04d}", first_seen=ts,
                          total_positions=20)
        insert_flight(db_conn, icao="oo0001", first_seen=500_000,
                      total_positions=1)  # outside both windows

        r = client.get("/api/stats?from=2000000&to=3000000")
        data = r.json()
        pw = data.get("previous_window")
        assert pw is not None, "filtered stats must include previous_window"
        assert pw["from_ts"] == 1_000_000
        assert pw["to_ts"] == 2_000_000
        assert pw["total_flights"] == 5
        assert pw["unique_aircraft"] == 5
        assert pw["total_positions"] == 100  # 5 × 20

    def test_previous_window_null_when_unfiltered(self, client, db_conn, clear_web_cache):
        """Unfiltered (all-time) has no meaningful previous window."""
        insert_flight(db_conn, first_seen=int(time.time()) - 3600)
        r = client.get("/api/stats")
        assert r.json().get("previous_window") is None

    def test_furthest_aircraft_includes_record_set_at(
        self, client, db_conn, clear_web_cache,
    ):
        """Sprint 1 #4: MaxRangeCard sublabel needs the timestamp of the
        record flight to render `{callsign} · set {date}`. Backend ships
        it as `record_set_at` (aliased from `flights.first_seen` of the
        record-holding row)."""
        insert_flight(db_conn, icao="rec001", callsign="MAX1",
                      first_seen=1_700_000_000, max_distance_nm=999.5)
        insert_flight(db_conn, icao="rec002", callsign="OTH2",
                      first_seen=1_700_500_000, max_distance_nm=100.0)
        r = client.get("/api/stats")
        furthest = r.json().get("furthest_aircraft")
        assert furthest is not None
        assert furthest["icao_hex"] == "rec001"
        assert furthest["record_set_at"] == 1_700_000_000

    def test_previous_window_boundary_flight_not_double_counted(
        self, client, db_conn, clear_web_cache,
    ):
        """A flight whose `first_seen` equals `from_ts` belongs to the
        current window only — the previous window upper bound is
        exclusive so the same flight cannot inflate both totals."""
        # Window [2_000_000, 3_000_000]. Boundary flight at exactly 2_000_000.
        insert_flight(db_conn, icao="bb0000", first_seen=2_000_000,
                      total_positions=10)
        r = client.get("/api/stats?from=2000000&to=3000000")
        data = r.json()
        # Current window includes the boundary flight.
        assert data["total_flights"] == 1
        # Previous window must NOT include it.
        assert data["previous_window"]["total_flights"] == 0

    def test_filtered_cache_distinguishes_ranges(self, client, db_conn, clear_web_cache):
        """Different date ranges must not collide in the cache."""
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        insert_flight(db_conn, icao="aa0002", first_seen=2_000_000)
        r1 = client.get("/api/stats?from=500000&to=1500000")
        r2 = client.get("/api/stats?from=1500000&to=2500000")
        assert r1.json()["total_flights"] == 1
        assert r2.json()["total_flights"] == 1
        # If they collided, r2 would return r1's cached payload (range field differs)
        assert r1.json()["range"] != r2.json()["range"]

    def test_stats_shape(self, client, clear_web_cache):
        r = client.get("/api/stats")
        data = r.json()
        for key in ("total_flights", "unique_aircraft", "unique_airlines",
                    "flights_last_24h", "source_breakdown",
                    "top_countries", "frequent_aircraft",
                    "military_flights", "interesting_flights", "anonymous_flights"):
            assert key in data, f"missing key: {key}"

    def test_military_interesting_counts_no_overlap(self, client, db_conn, clear_web_cache):
        """Aircraft with military+interesting flags must appear in military only, not interesting."""
        # flags=11 = military(1) + interesting(2) + LADD(8) — result of _apply_military_overrides
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, flags) VALUES ('48d820', 11)"
        )
        db_conn.commit()
        insert_flight(db_conn, icao="48d820")
        r = client.get("/api/stats")
        data = r.json()
        assert data["military_flights"] == 1
        assert data["interesting_flights"] == 0

    def test_interesting_only_not_counted_as_military(self, client, db_conn, clear_web_cache):
        """Aircraft with interesting flag only (no military) must appear in interesting only."""
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, flags) VALUES ('aabbcc', 2)"
        )
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/stats")
        data = r.json()
        assert data["military_flights"] == 0
        assert data["interesting_flights"] == 1

    def test_anonymous_flights_counted_separately(self, client, db_conn, clear_web_cache):
        """Anonymous-only (non-ICAO hex, no mil/int) shows up under anonymous_flights;
        a military+anonymous aircraft stays under military_flights (precedence)."""
        # dd85cb: anon-only (no aircraft_db row, falls outside state allocation)
        insert_flight(db_conn, icao="dd85cb")
        # dd0001: anon hex AND aircraft_db.flags=1 — counted under military, not anonymous
        insert_flight(db_conn, icao="dd0001")
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, flags) VALUES ('dd0001', 1)"
        )
        db_conn.commit()
        r = client.get("/api/stats")
        data = r.json()
        assert "anonymous_flights" in data, "stats endpoint must expose the new counter"
        assert data["anonymous_flights"] == 1
        assert data["military_flights"] == 1
        assert data["interesting_flights"] == 0

    def test_daily_unique_aircraft_sorted_asc(self, client, db_conn, clear_web_cache):
        # Insert flights across three distinct days within the last 30-day window.
        # daily_unique_aircraft must be returned oldest-first so the frontend
        # bar chart reads left→right past→present.
        now = int(time.time())
        day1 = now - 5 * 86400
        day2 = now - 3 * 86400
        day3 = now - 1 * 86400
        insert_flight(db_conn, icao="aa0001", first_seen=day1)
        insert_flight(db_conn, icao="aa0002", first_seen=day2)
        insert_flight(db_conn, icao="aa0003", first_seen=day3)
        r = client.get("/api/stats")
        days = [row["day"] for row in r.json()["daily_unique_aircraft"]]
        assert days == sorted(days), f"daily must be ASC, got {days}"

    def test_lifetime_block_coerces_nulls_on_empty_db(self, client, clear_web_cache):
        # SUM(total_positions) is NULL on an empty `flights` table. The
        # StatsResponse TS type declares total_positions: number; coerce
        # NULL → 0 so the JSON wire shape never breaks that contract.
        r_all = client.get("/api/stats").json()
        assert r_all["lifetime"]["total_positions"] == 0
        # Same on the filtered path.
        r_filt = client.get("/api/stats?from=1000&to=2000").json()
        assert r_filt["lifetime"]["total_positions"] == 0

    def test_lifetime_block_stays_constant_across_window(self, client, db_conn, clear_web_cache):
        # The `lifetime` block is consumed by the "About this receiver"
        # footer and must NOT change when the user picks a date range.
        # Insert flights inside and outside the range; the lifetime
        # totals should reflect ALL of them regardless of filter.
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        insert_flight(db_conn, icao="aa0002", first_seen=2_000_000)
        insert_flight(db_conn, icao="aa0003", first_seen=3_000_000)
        # Unfiltered baseline
        r_all = client.get("/api/stats").json()
        # Filtered to just the middle flight
        r_filt = client.get("/api/stats?from=1_900_000&to=2_100_000".replace("_", "")).json()
        # Top-level metrics differ between filtered and unfiltered…
        assert r_all["total_flights"] == 3
        assert r_filt["total_flights"] == 1
        # …but the lifetime block does not.
        assert r_all["lifetime"]["total_flights"] == 3
        assert r_filt["lifetime"]["total_flights"] == 3
        assert r_all["lifetime"]["unique_aircraft"] == 3
        assert r_filt["lifetime"]["unique_aircraft"] == 3
        assert r_all["lifetime"]["oldest_flight"] == 1_000_000
        assert r_filt["lifetime"]["oldest_flight"] == 1_000_000

    def test_daily_unique_aircraft_includes_today(self, client, db_conn, clear_web_cache):
        # The unfiltered path used to be ORDER BY day DESC LIMIT 30; after the
        # ASC flip we widened to LIMIT 31 so the 31-distinct-date window
        # (partial start day + 30 full days through today) doesn't truncate
        # today's bar. Insert a flight at boundary positions and assert both
        # the oldest-included day AND today survive.
        import datetime as _dt
        now = int(time.time())
        # 29 days ago — should appear
        insert_flight(db_conn, icao="aa0001", first_seen=now - 29 * 86400)
        # "now-ish" — today's bar
        insert_flight(db_conn, icao="aa0002", first_seen=now - 60)
        r = client.get("/api/stats")
        days = [row["day"] for row in r.json()["daily_unique_aircraft"]]
        today_str = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")
        assert today_str in days, f"today's bar must be present, got {days}"


# ---------------------------------------------------------------------------
# API: /api/stats/polar
# ---------------------------------------------------------------------------

class TestApiStatsPolar:
    def test_empty_db_returns_36_buckets(self, client):
        cache._cache.pop("polar", None)
        r = client.get("/api/stats/polar")
        assert r.status_code == 200
        assert len(r.json()["buckets"]) == 36

    def test_buckets_have_bearing_and_dist(self, client):
        cache._cache.pop("polar", None)
        r = client.get("/api/stats/polar")
        b = r.json()["buckets"][0]
        assert "bearing" in b
        assert "max_dist_nm" in b


# ---------------------------------------------------------------------------
# API: /api/live
# ---------------------------------------------------------------------------

class TestApiLive:
    def test_empty_returns_zero_count(self, client):
        r = client.get("/api/live")
        assert r.status_code == 200
        assert r.json()["count"] == 0
        assert r.json()["aircraft"] == []

    def test_active_flight_appears(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            ("aabbcc", fid, int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/live")
        assert r.json()["count"] == 1
        assert r.json()["aircraft"][0]["icao_hex"] == "aabbcc"
        assert "seconds_ago" in r.json()["aircraft"][0]


# ---------------------------------------------------------------------------
# API: /api/dates
# ---------------------------------------------------------------------------

class TestApiDates:
    def test_empty_db_returns_empty_list(self, client):
        r = client.get("/api/dates")
        assert r.status_code == 200
        assert r.json()["dates"] == []

    def test_groups_by_date(self, client, db_conn):
        # Two flights on same day, one on different day
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        insert_flight(db_conn, icao="aa0002", first_seen=1_001_000)
        insert_flight(db_conn, icao="aa0003", first_seen=1_100_000)
        r = client.get("/api/dates")
        dates = r.json()["dates"]
        # Should have 2 distinct dates
        assert len(dates) == 2
        counts = {d["date"]: d["flight_count"] for d in dates}
        assert sum(counts.values()) == 3

    def test_result_is_cached(self, client, db_conn):
        """First call populates the cache; second call returns the cached value
        even if the underlying data changes within the TTL window."""
        insert_flight(db_conn, icao="aa0001", first_seen=1_000_000)
        first = client.get("/api/dates").json()
        assert len(first["dates"]) == 1
        # Add another flight on a different day; cached response must still
        # show one date.
        insert_flight(db_conn, icao="aa0002", first_seen=2_000_000)
        second = client.get("/api/dates").json()
        assert second == first
        # Bypass cache by clearing it; should now reflect the new flight.
        cache._cache.clear()
        third = client.get("/api/dates").json()
        assert len(third["dates"]) == 2

    def test_groups_by_receiver_local_time(self, client, db_conn):
        """/api/dates must group by receiver-local date so it agrees with the
        date= filter (which uses host-local midnight, see
        TestBuildFlightFilter::test_date_uses_host_local_timezone). UTC bucketing
        would put a Warsaw 00:30 flight on the previous date."""
        import os, time
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Warsaw"  # UTC+1 in January (no DST)
        time.tzset()
        try:
            # Warsaw 2024-01-15 00:30 = 2024-01-14 23:30 UTC
            ts_local_midnight_edge = int(time.mktime((2024, 1, 15, 0, 30, 0, 0, 0, -1)))
            # Warsaw 2024-01-15 12:00 = 2024-01-15 11:00 UTC
            ts_local_noon = int(time.mktime((2024, 1, 15, 12, 0, 0, 0, 0, -1)))
            insert_flight(db_conn, icao="aa0001", first_seen=ts_local_midnight_edge)
            insert_flight(db_conn, icao="aa0002", first_seen=ts_local_noon)
            cache._cache.clear()
            r = client.get("/api/dates")
            assert r.status_code == 200
            dates = r.json()["dates"]
            counts = {d["date"]: d["flight_count"] for d in dates}
            # Both flights should group under 2024-01-15 in Warsaw-local time.
            assert counts == {"2024-01-15": 2}
        finally:
            cache._cache.clear()
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()


# ---------------------------------------------------------------------------
# API: /api/airlines/{prefix}/flights
# ---------------------------------------------------------------------------

class TestApiAirlineFlights:
    def test_empty_db_returns_zero(self, client):
        r = client.get("/api/airlines/LOT/flights")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["airline"] == "LOT"

    def test_filters_by_prefix(self, client, db_conn):
        insert_flight(db_conn, callsign="LOT123", icao="aa0001")
        insert_flight(db_conn, callsign="RYR456", icao="aa0002")
        r = client.get("/api/airlines/LOT/flights")
        assert r.json()["total"] == 1
        assert r.json()["flights"][0]["callsign"] == "LOT123"

    def test_prefix_case_insensitive(self, client, db_conn):
        insert_flight(db_conn, callsign="LOT123", icao="aa0001")
        r = client.get("/api/airlines/lot/flights")
        assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# API: /api/types/{aircraft_type}/flights
# ---------------------------------------------------------------------------

class TestApiTypeFlights:
    def test_empty_db_returns_zero(self, client):
        r = client.get("/api/types/B738/flights")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["aircraft_type"] == "B738"

    def test_filters_by_type(self, client, db_conn):
        insert_flight(db_conn, aircraft_type="B738", icao="aa0001")
        insert_flight(db_conn, aircraft_type="A320", icao="aa0002")
        r = client.get("/api/types/B738/flights")
        assert r.json()["total"] == 1

    def test_type_case_insensitive(self, client, db_conn):
        insert_flight(db_conn, aircraft_type="B738", icao="aa0001")
        r = client.get("/api/types/b738/flights")
        assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# PY-2 (Audit 2026-05-31): ADSBx-override enrichment parity across all
# surfaces. The shared flight SELECT (_FLIGHT_COLS) already enriches via
# flights → aircraft_db → adsbx_overrides, but sort columns, filter
# builders, the type drilldown, and top-types stats were still going
# through bare COALESCE(f.*, adb.*) — so a flight whose registration/
# type was visible only via adsbx_overrides displayed correctly but was
# invisible to filters, sorts, /api/types/.../flights, and stats.
# ---------------------------------------------------------------------------

class TestEnrichmentParity:
    def _seed_axo_only_flight(self, db_conn, *, icao="abc123",
                              reg="SP-ABC", type_code="B738",
                              type_desc="Boeing 737"):
        """Insert a flight + adsbx_overrides row but no aircraft_db row,
        and with flights.registration / flights.aircraft_type both NULL.
        The displayed reg/type/desc must come entirely from adsbx_overrides."""
        db_conn.execute(
            "INSERT INTO flights "
            "(icao_hex, callsign, registration, aircraft_type, first_seen, "
            " last_seen, total_positions, lat_min, lat_max, lon_min, lon_max) "
            "VALUES (?,?,NULL,NULL,?,?,10,0,0,0,0)",
            (icao, "AXO001", 1_000_000, 1_003_600),
        )
        db_conn.execute(
            "INSERT INTO adsbx_overrides "
            "(icao_hex, flags, registration, type_code, type_desc, "
            " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (icao, 0, reg, type_code, type_desc, 1_000_000, 1_003_600),
        )
        db_conn.commit()

    def test_list_displays_adsbx_only_metadata(self, client, db_conn):
        self._seed_axo_only_flight(db_conn)
        r = client.get("/api/flights")
        flights = r.json()["flights"]
        assert any(
            f["icao_hex"] == "abc123"
            and f["registration"] == "SP-ABC"
            and f["aircraft_type"] == "B738"
            and f["type_desc"] == "Boeing 737"
            for f in flights
        )

    def test_filter_by_registration_finds_adsbx_only(self, client, db_conn):
        self._seed_axo_only_flight(db_conn)
        r = client.get("/api/flights?registration=SP-ABC")
        assert r.json()["total"] == 1

    def test_filter_by_aircraft_type_finds_adsbx_only(self, client, db_conn):
        self._seed_axo_only_flight(db_conn)
        r = client.get("/api/flights?aircraft_type=B738")
        assert r.json()["total"] == 1

    def test_sort_by_registration_uses_adsbx_value(self, client, db_conn):
        # Two flights: one with adsbx-only registration "SP-AAA" and one with
        # flight-row registration "SP-ZZZ". Sort ASC must return SP-AAA first.
        self._seed_axo_only_flight(db_conn, icao="abc111", reg="SP-AAA",
                                   type_code="A320")
        db_conn.execute(
            "INSERT INTO flights "
            "(icao_hex, callsign, registration, aircraft_type, first_seen, "
            " last_seen, total_positions, lat_min, lat_max, lon_min, lon_max) "
            "VALUES ('abc222','ZZZ001','SP-ZZZ','A320',1000000,1003600,10,0,0,0,0)"
        )
        db_conn.commit()
        r = client.get("/api/flights?sort_by=registration&sort_dir=asc")
        regs = [f["registration"] for f in r.json()["flights"]]
        assert regs[0] == "SP-AAA"
        assert "SP-ZZZ" in regs

    def test_sort_by_aircraft_type_uses_adsbx_value(self, client, db_conn):
        # adsbx-only A320 vs flight-row B738 — ASC must put A320 first.
        self._seed_axo_only_flight(db_conn, icao="abc111", reg="SP-AAA",
                                   type_code="A320")
        db_conn.execute(
            "INSERT INTO flights "
            "(icao_hex, callsign, registration, aircraft_type, first_seen, "
            " last_seen, total_positions, lat_min, lat_max, lon_min, lon_max) "
            "VALUES ('abc222','BBB001','SP-BBB','B738',1000000,1003600,10,0,0,0,0)"
        )
        db_conn.commit()
        r = client.get("/api/flights?sort_by=aircraft_type&sort_dir=asc")
        types = [f["aircraft_type"] for f in r.json()["flights"]]
        assert types[0] == "A320"
        assert "B738" in types

    def test_type_drilldown_finds_adsbx_only(self, client, db_conn):
        self._seed_axo_only_flight(db_conn)
        r = client.get("/api/types/B738/flights")
        assert r.json()["total"] >= 1
        assert any(f["icao_hex"] == "abc123" for f in r.json()["flights"])

    def test_top_types_stats_includes_adsbx_only(self, client, db_conn):
        self._seed_axo_only_flight(db_conn)
        r = client.get("/api/stats")
        top_types = r.json()["top_aircraft_types"]
        match = [t for t in top_types if t["type"] == "B738"]
        assert match, f"B738 missing from top_types: {top_types}"
        assert match[0]["type_desc"] == "Boeing 737"


# ---------------------------------------------------------------------------
# API: /api/stats/polar — cache hit + position data path
# ---------------------------------------------------------------------------

class TestApiStatsPolarCacheAndData:
    def test_second_call_hits_cache(self, client, db_conn):
        cache._cache.pop("polar", None)
        r1 = client.get("/api/stats/polar")
        r2 = client.get("/api/stats/polar")
        assert r1.json() == r2.json()

    def test_with_flights_fills_buckets(self, client, db_conn):
        cache._cache.pop("polar", None)
        # Flight ~300 nm north of receiver (bearing ~0°, bucket 0)
        insert_flight(db_conn, max_distance_nm=300.0, max_distance_bearing=2.5)
        r = client.get("/api/stats/polar")
        assert r.status_code == 200
        buckets = r.json()["buckets"]
        assert buckets[0]["max_dist_nm"] == 300.0


# ---------------------------------------------------------------------------
# API: /api/flights/{flight_id}/photo
# ---------------------------------------------------------------------------

class TestApiFlightPhoto:
    def test_unknown_flight_returns_404(self, client):
        r = client.get("/api/flights/9999/photo")
        assert r.status_code == 404

    def test_all_sources_fail_returns_null(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn)
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_no_sources_null_cached_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row is not None
        assert row["thumbnail_url"] is None

    def test_transient_source_error_does_not_poison_new_aircraft(
        self, client, db_conn, monkeypatch
    ):
        """PY-5 (Audit 2026-05-31): when EVERY source raises for a
        previously-unseen ICAO, do not write a negative cache row.
        The next attempt may well succeed; a 30-day negative cache from
        a transient outage is the bug we are fixing.

        Tests the production path by patching _fetch_photo_with_status
        directly — leaves photo_sources.fetch_photo unpatched so the
        identity check at the API boundary uses the status-aware helper.
        """
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(
            photo_sources, "_fetch_photo_with_status",
            lambda icao: (None, "error"),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None
        # The decisive assertion: no row written at all.
        row = db_conn.execute(
            "SELECT * FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row is None, (
            "transient source failure poisoned the cache with a negative row"
        )

    def test_transient_error_serves_stale_positive_row(self, client, db_conn, monkeypatch):
        """PY-5 flip side: when every source errors but a previously-resolved
        positive row exists (even past its TTL), serve the stale photo — an
        outage must not drop existing coverage."""
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, "
            "photographer, fetched_at) VALUES ('aabbcc', "
            "'https://plnspttrs.net/t.jpg', 'https://plnspttrs.net/l.jpg', "
            "'https://plnspttrs.net/p', 'Bob', 1)",   # fetched_at=1 → long expired
        )
        db_conn.commit()
        monkeypatch.setattr(
            photo_sources, "_fetch_photo_with_status",
            lambda icao: (None, "error"),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://plnspttrs.net/t.jpg"
        assert data["photographer"] == "Bob"

    def test_photo_returned_and_stored(self, client, db_conn, monkeypatch):
        # PY-6: use an allowlisted host so the API-boundary suppression
        # doesn't filter the placeholder URL.
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://plnspttrs.net/thumb.jpg",
            large_url="https://plnspttrs.net/large.jpg",
            link_url="https://plnspttrs.net/photo",
            photographer="Alice",
        ))
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://plnspttrs.net/thumb.jpg"
        assert data["photographer"] == "Alice"
        assert data["icao_hex"] == "aabbcc"
        # Audit 17: assert every column lands in its named column (guards the
        # positive INSERT against a future schema reorder — it must name columns
        # explicitly, not rely on positional VALUES order).
        row = db_conn.execute(
            "SELECT thumbnail_url, large_url, link_url, photographer "
            "FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["thumbnail_url"] == "https://plnspttrs.net/thumb.jpg"
        assert row["large_url"] == "https://plnspttrs.net/large.jpg"
        assert row["link_url"] == "https://plnspttrs.net/photo"
        assert row["photographer"] == "Alice"

    def test_cached_photo_served_from_db(self, client, db_conn):
        # PY-6: use an allowlisted host.
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://plnspttrs.net/t.jpg", None, None, "Bob", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json()["thumbnail_url"] == "https://plnspttrs.net/t.jpg"
        assert r.json()["photographer"] == "Bob"

    def test_cached_null_photo_served_from_db(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,NULL,NULL,NULL,NULL,?)",
            ("aabbcc", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_transient_failure_preserves_stale_positive_row(self, client, db_conn, monkeypatch):
        # Audit-13 A13-014: a previously-resolved positive cache row must
        # not be blown away to NULL on a transient upstream failure within
        # the grace window (cache TTL + 7 days).
        fid = insert_flight(db_conn, icao="aabbcc")
        # Seed an expired positive row (1 day past 30d cache TTL).
        expired_ts = int(time.time()) - (config.PHOTO_CACHE_DAYS * 86400 + 86400)
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://kept.example/t.jpg",
             "https://kept.example/l.jpg", "https://kept.example/p",
             "Charlie", expired_ts),
        )
        db_conn.commit()
        # Refresh fails — fetcher returns None (transient).
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        # Cached row still has the old positive URL — not blown away to NULL.
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["thumbnail_url"] == "https://kept.example/t.jpg"


class TestPhotoFallback:
    """Web-layer photo caching behaviour (chain logic is in test_photo_sources.py)."""

    def test_photo_result_stored_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://airport-data.com/t.jpg",
            large_url="https://airport-data.com/t.jpg",
            link_url="https://airport-data.com/p",
            photographer="Charlie",
        ))
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json()["thumbnail_url"] == "https://airport-data.com/t.jpg"
        assert r.json()["photographer"] == "Charlie"
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://airport-data.com/t.jpg"

    def test_null_cached_when_all_sources_return_none(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json() is None
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row is not None
        assert row["thumbnail_url"] is None

    def test_fallback_also_works_on_icao_photo_endpoint(self, client, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://airport-data.com/t.jpg",
            large_url="https://airport-data.com/t.jpg",
            link_url="https://airport-data.com/p",
            photographer="Y",
        ))
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json()["thumbnail_url"] == "https://airport-data.com/t.jpg"

    def test_hexdb_result_stored_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://hexdb.io/img/AABBCC.jpg",
        ))
        client.get(f"/api/flights/{fid}/photo")
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://hexdb.io/img/AABBCC.jpg"

    # PY-6 (Audit 2026-05-31): server-side suppression of off-allowlist
    # photo URLs at the API boundary. The cache row is preserved (useful
    # diagnostic for the operator's log review and for the eventual
    # default-flip release); only the API response is filtered.

    def test_off_allowlist_thumbnail_suppressed_in_api_response(
        self, client, db_conn
    ):
        fid = insert_flight(db_conn, icao="aabbcc")
        # Seed a cached row with a thumbnail on a non-allowlisted host.
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc",
             "https://attacker.example/thumb.jpg",
             "https://attacker.example/large.jpg",
             "https://attacker.example/photo",
             "X", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None, (
            "off-allowlist thumbnail must be suppressed from the API "
            "response even when host enforcement is log-only"
        )
        # The cache row stays — it's the operator's diagnostic surface.
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row is not None
        assert row["thumbnail_url"] == "https://attacker.example/thumb.jpg"

    def test_thumbnail_on_en_wikipedia_org_rejected_as_image(
        self, client, db_conn
    ):
        """Code-review follow-up: en.wikipedia.org is allowed as link_url
        (article landing page from the Wikipedia type-photo step) but
        must NOT pass the image-host allowlist for thumbnail_url. A
        malformed cache row pointing thumbnail_url at an article page
        would otherwise render as a broken <img> in the SPA."""
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc",
             "https://en.wikipedia.org/wiki/Boeing_737",        # article page
             "https://upload.wikimedia.org/wiki/B738-large.jpg",
             "https://en.wikipedia.org/wiki/Boeing_737",
             "Wikipedia", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None, (
            "en.wikipedia.org thumbnail_url must be rejected as an image"
        )

    def test_link_url_on_en_wikipedia_org_kept(self, client, db_conn):
        """Counterpart: en.wikipedia.org IS valid for link_url (Wikipedia
        article landing page from the type-photo step). Don't null it
        when the thumbnail is on an allowed image host."""
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc",
             "https://upload.wikimedia.org/thumb.jpg",          # valid image
             "https://upload.wikimedia.org/large.jpg",
             "https://en.wikipedia.org/wiki/Boeing_737",        # valid link
             "Wikipedia", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data is not None
        assert data["thumbnail_url"] == "https://upload.wikimedia.org/thumb.jpg"
        assert data["link_url"]      == "https://en.wikipedia.org/wiki/Boeing_737"

    def test_off_allowlist_large_only_nulled_thumbnail_kept(
        self, client, db_conn
    ):
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc",
             "https://plnspttrs.net/thumb.jpg",       # allowlisted
             "https://attacker.example/large.jpg",     # off-allowlist
             "https://plnspttrs.net/photo",            # allowlisted
             "X", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data is not None
        assert data["thumbnail_url"] == "https://plnspttrs.net/thumb.jpg"
        assert data["large_url"] is None, (
            "off-allowlist large_url must be nulled in API response"
        )
        assert data["link_url"] == "https://plnspttrs.net/photo"


# ---------------------------------------------------------------------------
# Route enrichment — API behaviour
# ---------------------------------------------------------------------------

def _insert_route(conn, callsign, origin_icao, dest_icao):
    """Insert a resolved callsign_routes entry and the two airport rows."""
    now = int(time.time())
    for icao, name, country in [
        (origin_icao, f"{origin_icao} Airport", "OriginCountry"),
        (dest_icao,   f"{dest_icao} Airport",   "DestCountry"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO airports "
            "(icao_code, iata_code, name, country, latitude, longitude, fetched_at) "
            "VALUES (?,?,?,?,0,0,?)",
            (icao, icao, name, country, now),
        )
    conn.execute(
        "INSERT INTO callsign_routes (callsign, origin_icao, dest_icao, fetched_at) "
        "VALUES (?,?,?,?)",
        (callsign, origin_icao, dest_icao, now),
    )
    conn.commit()


class TestRouteEnrichmentFlightDetail:
    def test_detail_includes_origin_dest_when_resolved(self, client, db_conn):
        fid = insert_flight(db_conn, callsign="LOT123")
        db_conn.execute(
            "UPDATE flights SET origin_icao='WAW', dest_icao='LHR' WHERE id=?", (fid,)
        )
        _insert_route(db_conn, "LOT123", "WAW", "LHR")
        db_conn.commit()

        r = client.get(f"/api/flights/{fid}")
        assert r.status_code == 200
        f = r.json()["flight"]
        assert f["origin_icao"] == "WAW"
        assert f["dest_icao"] == "LHR"
        assert f["origin_name"] == "WAW Airport"
        assert f["dest_name"] == "LHR Airport"

    def test_detail_origin_dest_null_when_unresolved(self, client, db_conn):
        fid = insert_flight(db_conn, callsign="UNKN99")
        r = client.get(f"/api/flights/{fid}")
        assert r.status_code == 200
        f = r.json()["flight"]
        assert f["origin_icao"] is None
        assert f["dest_icao"] is None


class TestRouteEnrichmentLiveBoard:
    def test_live_includes_route_when_resolved(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc", callsign="LOT123")
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            ("aabbcc", fid, int(time.time())),
        )
        _insert_route(db_conn, "LOT123", "WAW", "LHR")

        r = client.get("/api/live")
        assert r.status_code == 200
        ac = r.json()["aircraft"][0]
        assert ac["origin_icao"] == "WAW"
        assert ac["dest_icao"] == "LHR"

    def test_live_route_null_when_unknown(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc", callsign="UNKN99")
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            ("aabbcc", fid, int(time.time())),
        )

        r = client.get("/api/live")
        assert r.status_code == 200
        ac = r.json()["aircraft"][0]
        assert ac["origin_icao"] is None
        assert ac["dest_icao"] is None


class TestRouteEnrichmentStats:
    def test_stats_includes_top_routes(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", callsign="LOT123")
        insert_flight(db_conn, icao="aa0002", callsign="LOT123")
        insert_flight(db_conn, icao="aa0003", callsign="RYR456")
        _insert_route(db_conn, "LOT123", "WAW", "LHR")
        _insert_route(db_conn, "RYR456", "WAW", "STN")
        db_conn.execute("UPDATE flights SET origin_icao='WAW', dest_icao='LHR' WHERE callsign='LOT123'")
        db_conn.execute("UPDATE flights SET origin_icao='WAW', dest_icao='STN' WHERE callsign='RYR456'")
        db_conn.commit()

        r = client.get("/api/stats")
        assert r.status_code == 200
        routes = r.json()["top_routes"]
        assert len(routes) >= 1
        waw_lhr = next((x for x in routes if x["origin_icao"] == "WAW" and x["dest_icao"] == "LHR"), None)
        assert waw_lhr is not None
        assert waw_lhr["flights"] == 2
        assert waw_lhr["origin_name"] == "WAW Airport"

    def test_stats_includes_top_airports(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", callsign="LOT123")
        insert_flight(db_conn, icao="aa0002", callsign="RYR456")
        _insert_route(db_conn, "LOT123", "WAW", "LHR")
        _insert_route(db_conn, "RYR456", "KRK", "WAW")
        db_conn.execute("UPDATE flights SET origin_icao='WAW', dest_icao='LHR' WHERE callsign='LOT123'")
        db_conn.execute("UPDATE flights SET origin_icao='KRK', dest_icao='WAW' WHERE callsign='RYR456'")
        db_conn.commit()

        r = client.get("/api/stats")
        assert r.status_code == 200
        airports = r.json()["top_airports"]
        waw = next((x for x in airports if x["icao_code"] == "WAW"), None)
        assert waw is not None
        assert waw["appearances"] == 2  # once as origin, once as dest

    def test_stats_top_routes_empty_when_no_route_data(self, client, db_conn):
        insert_flight(db_conn)
        r = client.get("/api/stats")
        assert r.status_code == 200
        assert r.json()["top_routes"] == []
        assert r.json()["top_airports"] == []


# ---------------------------------------------------------------------------
# Personal records endpoint
# ---------------------------------------------------------------------------

class TestPersonalRecords:
    def test_empty_db_returns_nulls(self, client, db_conn):
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        d = r.json()
        assert d["furthest"] is None
        assert d["fastest"]  is None
        assert d["highest"]  is None
        assert d["longest"]  is None

    def test_furthest_picks_max_distance(self, client, db_conn):
        insert_flight(db_conn, icao="aa0001", max_distance_nm=100.0)
        insert_flight(db_conn, icao="aa0002", max_distance_nm=300.0)
        insert_flight(db_conn, icao="aa0003", max_distance_nm=200.0)
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        f = r.json()["furthest"]
        assert f is not None
        assert f["icao_hex"] == "aa0002"
        assert f["max_distance_nm"] == 300.0

    def test_fastest_picks_max_gs(self, client, db_conn):
        insert_flight(db_conn, icao="bb0001", max_gs=400.0)
        insert_flight(db_conn, icao="bb0002", max_gs=600.0)
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        f = r.json()["fastest"]
        assert f is not None
        assert f["icao_hex"] == "bb0002"
        assert f["max_gs"] == 600.0

    def test_highest_picks_max_alt_baro(self, client, db_conn):
        insert_flight(db_conn, icao="cc0001", max_alt_baro=35000)
        insert_flight(db_conn, icao="cc0002", max_alt_baro=45000)
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        f = r.json()["highest"]
        assert f is not None
        assert f["icao_hex"] == "cc0002"
        assert f["max_alt_baro"] == 45000

    def test_longest_picks_max_duration(self, client, db_conn):
        # 1 hour duration
        insert_flight(db_conn, icao="dd0001", first_seen=1_000_000, last_seen=1_003_600)
        # 2 hour duration
        insert_flight(db_conn, icao="dd0002", first_seen=2_000_000, last_seen=2_007_200)
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        f = r.json()["longest"]
        assert f is not None
        assert f["icao_hex"] == "dd0002"
        assert f["duration_s"] == 7200

    def test_records_include_flight_id_for_linking(self, client, db_conn):
        fid = insert_flight(db_conn, icao="ee0001", max_distance_nm=200.0)
        r = client.get("/api/stats/records")
        assert r.status_code == 200
        assert r.json()["furthest"]["id"] == fid

    def test_records_are_always_all_time(self, client, db_conn):
        """Records endpoint ignores any from/to query params."""
        insert_flight(db_conn, icao="ff0001", max_distance_nm=500.0,
                      first_seen=1_000, last_seen=1_100)
        r = client.get("/api/stats/records?from=9999999999&to=9999999999")
        assert r.status_code == 200
        assert r.json()["furthest"] is not None
        assert r.json()["furthest"]["max_distance_nm"] == 500.0

    def test_records_cached(self, client, db_conn):
        insert_flight(db_conn)
        r1 = client.get("/api/stats/records")
        r2 = client.get("/api/stats/records")
        assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# Airspace endpoint
# ---------------------------------------------------------------------------

class TestAirspaceEndpoint:
    def test_returns_feature_collection_from_file(self, client, monkeypatch, tmp_path):
        import json as _json
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "TEST CTR", "type": "CTR", "icaoClass": "D"},
                    "geometry": {"type": "Polygon", "coordinates": [
                        [[20.0, 52.0], [20.5, 52.0], [20.5, 52.5], [20.0, 52.5], [20.0, 52.0]]
                    ]},
                }
            ],
        }
        f = tmp_path / "test.geojson"
        f.write_text(_json.dumps(gj))
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", str(f))
        cache._cache.clear()

        r = client.get("/api/airspace")
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        assert len(d["features"]) == 1
        assert d["features"][0]["properties"]["name"] == "TEST CTR"

    def test_missing_file_returns_empty_collection(self, client, monkeypatch):
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "/nonexistent/path.geojson")
        cache._cache.clear()

        r = client.get("/api/airspace")
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        assert d["features"] == []

    def test_uses_bundled_file_when_config_empty(self, client, monkeypatch):
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "")
        cache._cache.clear()

        r = client.get("/api/airspace")
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        # Bundled file should have at least one feature
        assert len(d["features"]) >= 1

    def test_result_cached(self, client, monkeypatch, tmp_path):
        import json as _json
        gj = {"type": "FeatureCollection", "features": []}
        f = tmp_path / "test.geojson"
        f.write_text(_json.dumps(gj))
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", str(f))
        cache._cache.clear()

        r1 = client.get("/api/airspace")
        r2 = client.get("/api/airspace")
        assert r1.json() == r2.json()

    def test_non_regular_file_returns_empty_collection(self, client, monkeypatch):
        # improvements.md #73: a path that resolves but isn't a regular file
        # (device, FIFO, directory) must be rejected with an empty result.
        # /dev/null is portable and definitely not a regular file.
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "/dev/null")
        cache._cache.clear()
        r = client.get("/api/airspace")
        assert r.status_code == 200
        assert r.json() == {"type": "FeatureCollection", "features": []}

    def test_directory_path_returns_empty_collection(self, client, monkeypatch, tmp_path):
        # A path that exists but is a directory, not a file.
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", str(tmp_path))
        cache._cache.clear()
        r = client.get("/api/airspace")
        assert r.status_code == 200
        assert r.json() == {"type": "FeatureCollection", "features": []}


# ---------------------------------------------------------------------------
# API: /api/watchlist
# ---------------------------------------------------------------------------

class TestApiWatchlist:
    def test_list_empty(self, client):
        r = client.get("/api/watchlist")
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_add_icao_entry(self, client):
        r = client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 201
        data = r.json()
        assert data["match_type"] == "icao"
        assert data["value"] == "aabbcc"

    def test_add_normalises_value_to_lowercase(self, client):
        r = client.post("/api/watchlist", json={"match_type": "registration", "value": "SP-LRF"})
        assert r.status_code == 201
        assert r.json()["value"] == "sp-lrf"

    def test_add_with_label(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc", "label": "My plane"})
        assert r.status_code == 201
        assert r.json()["label"] == "My plane"

    def test_add_duplicate_returns_409(self, client):
        client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        r = client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 409

    def test_add_invalid_match_type_returns_422(self, client):
        r = client.post("/api/watchlist", json={"match_type": "bad", "value": "aabbcc"})
        assert r.status_code == 422

    def test_add_empty_value_returns_422(self, client):
        r = client.post("/api/watchlist", json={"match_type": "icao", "value": "   "})
        assert r.status_code == 422

    def test_list_shows_added_entry(self, client):
        client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        r = client.get("/api/watchlist")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["value"] == "aabbcc"

    def test_list_airborne_flag_for_active_icao(self, client, db_conn):
        client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            ("aabbcc", fid, 1_000_000),
        )
        db_conn.commit()
        r = client.get("/api/watchlist")
        assert r.json()["entries"][0]["airborne"] == 1

    def test_list_airborne_false_when_not_active(self, client):
        client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        r = client.get("/api/watchlist")
        assert r.json()["entries"][0]["airborne"] == 0

    def test_delete_entry(self, client):
        r = client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        entry_id = r.json()["id"]
        r2 = client.delete(f"/api/watchlist/{entry_id}")
        assert r2.status_code == 204
        assert client.get("/api/watchlist").json()["entries"] == []

    def test_delete_nonexistent_returns_404(self, client):
        r = client.delete("/api/watchlist/9999")
        assert r.status_code == 404

    def test_callsign_prefix_entry(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "callsign_prefix", "value": "LOT"})
        assert r.status_code == 201
        assert r.json()["value"] == "lot"

    # CSRF: state-changing endpoints require X-Requested-With (browsers cannot
    # set custom headers cross-origin without a CORS preflight that this app
    # rejects, so a missing header signals a forged-form / cross-site attempt).

    def test_post_without_xhr_header_returns_403(self, raw_client):
        r = raw_client.post(
            "/api/watchlist",
            json={"match_type": "icao", "value": "aabbcc"},
        )
        assert r.status_code == 403

    def test_post_with_wrong_xhr_value_returns_403(self, raw_client):
        # Audit-13 A13-001: any non-empty value used to pass; canonical
        # value is now required to remove that accidental-bypass class.
        r = raw_client.post(
            "/api/watchlist",
            headers={"X-Requested-With": "bogus"},
            json={"match_type": "icao", "value": "aabbcc"},
        )
        assert r.status_code == 403

    def test_post_with_canonical_xhr_value_succeeds(self, raw_client):
        r = raw_client.post(
            "/api/watchlist",
            headers={"X-Requested-With": "XMLHttpRequest"},
            json={"match_type": "icao", "value": "aabbcc"},
        )
        assert r.status_code == 201

    def test_post_with_mixed_case_xhr_value_succeeds(self, raw_client):
        # Canonical compare is case-insensitive (XMLHttpRequest vs xmlhttprequest).
        r = raw_client.post(
            "/api/watchlist",
            headers={"X-Requested-With": "xmlhttprequest"},
            json={"match_type": "icao", "value": "aabbcd"},
        )
        assert r.status_code == 201

    def test_delete_without_xhr_header_returns_403(self, client, raw_client):
        # Seed an entry via the standard (header-bearing) client.
        r = client.post("/api/watchlist", json={"match_type": "icao", "value": "aabbcc"})
        entry_id = r.json()["id"]
        # Attempt deletion without the header — must be rejected.
        r2 = raw_client.delete(f"/api/watchlist/{entry_id}")
        assert r2.status_code == 403

    def test_get_does_not_require_xhr_header(self, raw_client):
        # Read-only endpoints have no CSRF risk and must work without the header.
        r = raw_client.get("/api/watchlist")
        assert r.status_code == 200

    def test_add_label_too_long_returns_422(self, client):
        long_label = "x" * 300
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc",
                              "label": long_label})
        assert r.status_code == 422

    def test_add_label_at_max_length_accepted(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc",
                              "label": "x" * 255})
        assert r.status_code == 201

    def test_add_value_too_long_returns_422(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "registration", "value": "x" * 100})
        assert r.status_code == 422

    # F07: per-type backend validation of the watchlist value. The frontend
    # validates too, but the API must reject malformed values independently.

    def test_add_icao_non_hex_returns_422(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "nothex"})
        assert r.status_code == 422

    def test_add_icao_valid_hex_succeeds(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "48e95d"})
        assert r.status_code == 201
        assert r.json()["value"] == "48e95d"

    def test_add_registration_valid_succeeds(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "registration", "value": "SP-LRF"})
        assert r.status_code == 201
        assert r.json()["value"] == "sp-lrf"

    def test_add_registration_invalid_chars_returns_422(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "registration", "value": "SP/LRF!"})
        assert r.status_code == 422

    def test_add_callsign_prefix_valid_succeeds(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "callsign_prefix", "value": "LOT"})
        assert r.status_code == 201
        assert r.json()["value"] == "lot"

    def test_add_callsign_prefix_invalid_returns_422(self, client):
        r = client.post("/api/watchlist",
                        json={"match_type": "callsign_prefix", "value": "LOT-1"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# SH-1 (Audit 2026-05-31): optional RSBS_API_TOKEN bearer auth on mutating
# endpoints. No-op when unset (default trusted-LAN posture); when set,
# Authorization: Bearer <token> is required on POST/DELETE.
# ---------------------------------------------------------------------------

class TestApiAuthToken:
    def test_no_token_unset_means_no_auth_required(self, client, monkeypatch):
        """When RSBS_API_TOKEN is unset, mutating endpoints behave exactly
        as before — only CSRF is enforced (provided by the test client)."""
        monkeypatch.setattr(_deps, "_API_TOKEN", "")
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 201

    def test_token_set_post_without_bearer_rejected(self, client, monkeypatch):
        monkeypatch.setattr(_deps, "_API_TOKEN", "secret")
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 401

    def test_token_set_post_with_wrong_bearer_rejected(self, client, monkeypatch):
        monkeypatch.setattr(_deps, "_API_TOKEN", "secret")
        r = client.post("/api/watchlist",
                        headers={"Authorization": "Bearer wrong"},
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 401

    def test_token_set_post_with_correct_bearer_accepted(self, client, monkeypatch):
        monkeypatch.setattr(_deps, "_API_TOKEN", "secret")
        r = client.post("/api/watchlist",
                        headers={"Authorization": "Bearer secret"},
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 201

    def test_token_set_delete_requires_bearer(self, client, monkeypatch):
        # First create an entry (without auth so we exercise the rejection
        # on DELETE specifically).
        monkeypatch.setattr(_deps, "_API_TOKEN", "")
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc"})
        entry_id = r.json()["id"]

        # Now turn auth on and verify DELETE without bearer is 401.
        monkeypatch.setattr(_deps, "_API_TOKEN", "secret")
        r = client.delete(f"/api/watchlist/{entry_id}")
        assert r.status_code == 401

        # With bearer: 204.
        r = client.delete(f"/api/watchlist/{entry_id}",
                          headers={"Authorization": "Bearer secret"})
        assert r.status_code == 204

    def test_read_endpoints_not_gated(self, client, monkeypatch):
        """Read-only endpoints (GET /api/watchlist, /api/flights, etc.) are
        NOT gated — token auth only applies to mutating endpoints."""
        monkeypatch.setattr(_deps, "_API_TOKEN", "secret")
        assert client.get("/api/watchlist").status_code == 200
        assert client.get("/api/flights").status_code == 200

    def test_token_picked_up_from_env_at_call_time(self, client, monkeypatch):
        """Code-review follow-up: tests using monkeypatch.setenv (the
        natural pytest pattern) must also work, not just
        monkeypatch.setattr(_deps, '_API_TOKEN', ...). Before the fix,
        _API_TOKEN was captured at module import time so setenv after
        import was invisible to _auth_check, causing test assertions
        of 401 to silently pass for the wrong reason."""
        # Ensure the module-level binding is empty so the env-var path runs.
        monkeypatch.setattr(_deps, "_API_TOKEN", None)
        monkeypatch.setenv("RSBS_API_TOKEN", "env-secret")

        # Without bearer → 401.
        r = client.post("/api/watchlist",
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 401

        # With correct bearer → 201.
        r = client.post("/api/watchlist",
                        headers={"Authorization": "Bearer env-secret"},
                        json={"match_type": "icao", "value": "aabbcc"})
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Route-guard invariant: every mutating endpoint must carry BOTH _csrf_check
# and _auth_check (CLAUDE.md non-negotiable). The per-endpoint tests above
# prove the watchlist routes behave; these tests prove no future POST/PUT/
# DELETE/PATCH route can ship without the guards — the failure message names
# the offending route.
# ---------------------------------------------------------------------------

_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _mutating_api_routes(app_):
    """Yield (method, route) for every mutating method on every APIRoute.

    Walks included sub-routers via iter_api_routes(): FastAPI 0.137 no longer
    flattens include_router() routes into app.routes (they nest under
    _IncludedRouter), so a flat walk would find zero mutating routes and the
    CSRF/auth guard below would pass vacuously."""
    for route in iter_api_routes(app_.routes):
        for method in sorted((route.methods or set()) & _MUTATING_METHODS):
            yield method, route


def _route_dependency_calls(route):
    """All dependency callables wired on a route, via both surfaces: the
    resolved dependant (route-level Depends land there wrapped by
    get_parameterless_sub_dependant, .call = the original function) and the
    raw route.dependencies list — so the check survives FastAPI internals
    changing either representation."""
    calls = {d.call for d in route.dependant.dependencies}
    calls |= {d.dependency for d in route.dependencies}
    return calls


class TestMutatingRouteGuards:
    # Intentional exceptions go here as (method, path) — keep empty unless a
    # route genuinely must skip a guard, so the exemption is visible in review.
    ALLOWLIST: set = set()

    def test_every_mutating_route_has_csrf_and_auth_dependencies(self):
        required = {_deps._csrf_check, _deps._auth_check}
        offenders = []
        for method, route in _mutating_api_routes(web.app):
            if (method, route.path) in self.ALLOWLIST:
                continue
            missing = required - _route_dependency_calls(route)
            if missing:
                offenders.append(
                    (method, route.path, sorted(f.__name__ for f in missing)))
        assert not offenders, (
            "Mutating routes missing CSRF/auth guards "
            f"(add dependencies=[Depends(_csrf_check), Depends(_auth_check)]): {offenders}")

    def test_mutating_route_inventory_is_known(self):
        """Exact inventory of mutating routes. Adding an endpoint must fail
        here, forcing a conscious update — which is the moment to re-read the
        guard rule in CLAUDE.md and the frontend apiFetch contract."""
        inventory = {(m, r.path) for m, r in _mutating_api_routes(web.app)}
        assert inventory == {
            ("POST", "/api/watchlist"),
            ("DELETE", "/api/watchlist/{entry_id}"),
        }

    def test_vdl2_router_registers_no_mutating_routes(self, monkeypatch):
        """VDL2 is read-only by design (separate vdl2.db, mode=ro ATTACH) —
        lock that at the route level: enabling the feature must not register
        a single mutating route."""
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        app_ = FastAPI()
        web._include_optional_routers(app_)
        assert list(_mutating_api_routes(app_)) == []


# ---------------------------------------------------------------------------
# API: /api/aircraft/flagged  — flagged aircraft gallery
# ---------------------------------------------------------------------------

def _insert_aircraft_db(conn, icao, *, registration=None, type_code=None,
                        type_desc=None, flags=0):
    conn.execute(
        "INSERT OR REPLACE INTO aircraft_db "
        "(icao_hex, registration, type_code, type_desc, flags) "
        "VALUES (?,?,?,?,?)",
        (icao, registration, type_code, type_desc, flags),
    )
    conn.commit()


class TestApiFlaggedAircraft:
    def test_empty_db_returns_empty(self, client):
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["aircraft"] == []

    def test_military_aircraft_included(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", registration="SP-MIL",
                            type_code="F16", type_desc="F-16 Falcon", flags=1)
        insert_flight(db_conn, icao="aabbcc", registration="SP-MIL",
                      aircraft_type="F16")
        r = client.get("/api/aircraft/flagged")
        data = r.json()
        assert data["total"] == 1
        assert data["aircraft"][0]["icao_hex"] == "aabbcc"
        assert data["aircraft"][0]["flags"] & 1

    def test_interesting_aircraft_included(self, client, db_conn):
        _insert_aircraft_db(db_conn, "112233", registration="SP-GOV",
                            type_code="G550", type_desc="Gulfstream 550", flags=2)
        insert_flight(db_conn, icao="112233", registration="SP-GOV",
                      aircraft_type="G550")
        r = client.get("/api/aircraft/flagged")
        data = r.json()
        assert data["total"] == 1

    def test_unflagged_aircraft_excluded(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=0)
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/flagged")
        assert r.json()["total"] == 0

    def test_filter_military_only(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=2)
        insert_flight(db_conn, icao="aabbcc")
        insert_flight(db_conn, icao="112233")
        r = client.get("/api/aircraft/flagged?flags=military")
        assert r.json()["total"] == 1
        assert r.json()["aircraft"][0]["icao_hex"] == "aabbcc"

    def test_filter_interesting_only(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=2)
        insert_flight(db_conn, icao="aabbcc")
        insert_flight(db_conn, icao="112233")
        r = client.get("/api/aircraft/flagged?flags=interesting")
        assert r.json()["total"] == 1
        assert r.json()["aircraft"][0]["icao_hex"] == "112233"

    def test_filter_anonymous_only(self, client, db_conn):
        # dd85cb: anon-only (no DB row, non-state hex)
        # aabbcc: military, state-allocated — must not appear
        # dd0001: military AND anon (military takes precedence — excluded from anon)
        insert_flight(db_conn, icao="dd85cb")
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        _insert_aircraft_db(db_conn, "dd0001", flags=1)
        insert_flight(db_conn, icao="dd0001")
        r = client.get("/api/aircraft/flagged?flags=anonymous")
        assert {a["icao_hex"] for a in r.json()["aircraft"]} == {"dd85cb"}

    def test_all_filter_includes_anonymous(self, client, db_conn):
        # Default "all" tab (no flags param) must now return anonymous hits
        # alongside military/interesting — the gallery should not hide them.
        insert_flight(db_conn, icao="dd85cb")
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/flagged")
        icaos = {a["icao_hex"] for a in r.json()["aircraft"]}
        assert "dd85cb" in icaos
        assert "aabbcc" in icaos

    def test_aggregates_flight_counts(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000, last_seen=1_003_600)
        insert_flight(db_conn, icao="aabbcc", first_seen=2_000_000, last_seen=2_003_600)
        r = client.get("/api/aircraft/flagged")
        ac = r.json()["aircraft"][0]
        assert ac["flight_count"] == 2
        assert ac["first_seen"] == 1_000_000
        assert ac["last_seen"] == 2_003_600

    def test_includes_country(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/flagged")
        ac = r.json()["aircraft"][0]
        assert "country" in ac

    def test_adsbx_overrides_flags_included(self, client, db_conn):
        """Aircraft flagged only via adsbx_overrides should appear."""
        db_conn.execute(
            "INSERT INTO adsbx_overrides (icao_hex, flags, first_seen, last_seen) "
            "VALUES (?,?,?,?)",
            ("aabbcc", 1, 1_000_000, 1_000_000),
        )
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc")
        r = client.get("/api/aircraft/flagged")
        assert r.json()["total"] == 1

    def test_pagination(self, client, db_conn):
        for i in range(5):
            icao = f"aa{i:04x}"
            _insert_aircraft_db(db_conn, icao, flags=1)
            insert_flight(db_conn, icao=icao)
        r = client.get("/api/aircraft/flagged?limit=2&offset=0")
        data = r.json()
        assert data["total"] == 5
        assert len(data["aircraft"]) == 2

    def test_result_is_cached_per_query_shape(self, client, db_conn):
        """Audit 17: the flagged gallery caches its (heavy) result keyed by the
        full query shape. Within the TTL a repeat of the same query is served
        from cache; a different query shape computes fresh."""
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        assert client.get("/api/aircraft/flagged").json()["total"] == 1
        # Add a second flagged aircraft AFTER the first response is cached.
        _insert_aircraft_db(db_conn, "112233", flags=1)
        insert_flight(db_conn, icao="112233")
        # Same query shape → served from cache → still sees only the first.
        assert client.get("/api/aircraft/flagged").json()["total"] == 1
        # Different query shape (distinct cache key) → fresh → sees both.
        assert client.get("/api/aircraft/flagged?flags=military").json()["total"] == 2

    def test_sort_by_last_seen_desc_default(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=1)
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000, last_seen=1_003_600)
        insert_flight(db_conn, icao="112233", first_seen=2_000_000, last_seen=2_003_600)
        r = client.get("/api/aircraft/flagged")
        aircraft = r.json()["aircraft"]
        assert aircraft[0]["icao_hex"] == "112233"
        assert aircraft[1]["icao_hex"] == "aabbcc"

    # sort_by/sort_dir go through the _FLAGGED_SORT_COLS allowlist and an
    # explicit ASC/DESC ternary (A13-077) — pin the injection-safety the same
    # way /api/flights pins _SORT_COLS. Each distinct query string is its own
    # cache key, so no cache clearing is needed between requests.

    def test_flagged_sort_by_injection_falls_back_to_default(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        insert_flight(db_conn, icao="112233")
        r = client.get("/api/aircraft/flagged?sort_by=last_seen;DROP TABLE flights")
        assert r.status_code == 200
        assert r.json()["total"] == 2
        assert db_conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 2

    def test_flagged_all_sort_columns_accepted(self, client, db_conn):
        """Every column in _FLAGGED_SORT_COLS × both directions returns 200
        with all rows present (sort never drops aircraft)."""
        _insert_aircraft_db(db_conn, "aabbcc", registration="SP-AAA",
                            type_code="F16", flags=1)
        _insert_aircraft_db(db_conn, "112233", registration="SP-BBB",
                            type_code="G550", flags=1)
        insert_flight(db_conn, icao="aabbcc", registration="SP-AAA",
                      aircraft_type="F16", first_seen=1_000_000, last_seen=1_003_600)
        insert_flight(db_conn, icao="112233", registration="SP-BBB",
                      aircraft_type="G550", first_seen=2_000_000, last_seen=2_003_600)
        for col in _deps._FLAGGED_SORT_COLS:
            for direction in ("asc", "desc"):
                r = client.get(
                    f"/api/aircraft/flagged?sort_by={col}&sort_dir={direction}")
                assert r.status_code == 200, f"Failed for sort_by={col}&sort_dir={direction}"
                icaos = {a["icao_hex"] for a in r.json()["aircraft"]}
                assert icaos == {"aabbcc", "112233"}, (
                    f"Missing aircraft for sort_by={col}&sort_dir={direction}")

    def test_flagged_bogus_sort_dir_falls_back_to_desc(self, client, db_conn):
        """Anything but 'asc' (case-insensitive) normalizes to DESC — a hostile
        sort_dir can never reach the SQL string."""
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=1)
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000, last_seen=1_003_600)
        insert_flight(db_conn, icao="112233", first_seen=2_000_000, last_seen=2_003_600)
        r = client.get("/api/aircraft/flagged?sort_dir=evil)")
        assert r.status_code == 200
        aircraft = r.json()["aircraft"]
        assert [a["icao_hex"] for a in aircraft] == ["112233", "aabbcc"]

    def test_includes_photo_data(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://plnspttrs.net/t.jpg", "https://plnspttrs.net/l.jpg", "https://plnspttrs.net/link", "Bob", int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/aircraft/flagged")
        ac = r.json()["aircraft"][0]
        assert ac["thumbnail_url"] == "https://plnspttrs.net/t.jpg"
        assert ac["photographer"] == "Bob"

    def test_grouped_metadata_is_deterministic_latest_flight(self, client, db_conn):
        # BE-15 (Audit 2026-05-31): two flights for one ICAO with conflicting
        # reg/type and the SAME last_seen (a tie on the grouping max). GROUP BY
        # f.icao_hex with bare reg/type columns resolves the tie arbitrarily.
        # The representative must be deterministic — ORDER BY last_seen DESC,
        # id DESC means the higher-id flight wins — and exactly one row returns.
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)  # flag source is the DB row
        ls = 2_003_600
        insert_flight(db_conn, icao="aabbcc", registration="LOW-ID",
                      aircraft_type="OLD", first_seen=1_000_000, last_seen=ls)
        insert_flight(db_conn, icao="aabbcc", registration="HIGH-ID",
                      aircraft_type="NEW", first_seen=2_000_000, last_seen=ls)
        r = client.get("/api/aircraft/flagged")
        aircraft = r.json()["aircraft"]
        assert len(aircraft) == 1, f"expected one row per ICAO, got {aircraft}"
        assert aircraft[0]["registration"] == "HIGH-ID"
        assert aircraft[0]["aircraft_type"] == "NEW"
        assert aircraft[0]["flight_count"] == 2


# ---------------------------------------------------------------------------
# API: /api/aircraft/{icao_hex}/photo  — photo by ICAO hex
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /api/map/heatmap
# ---------------------------------------------------------------------------

class TestMapHeatmap:
    def _insert_position(self, conn, *, lat, lon, ts=None, flight_id=None):
        if flight_id is None:
            flight_id = insert_flight(conn)
        if ts is None:
            ts = int(time.time())
        insert_position(conn, flight_id, ts, lat=lat, lon=lon,
                        source_type="adsb_icao")
        conn.commit()
        return flight_id

    def test_empty_db_returns_empty_points(self, client):
        r = client.get("/api/map/heatmap")
        assert r.status_code == 200
        data = r.json()
        assert data["points"] == []
        assert data["count"] == 0

    def test_invalid_window_returns_400(self, client):
        r = client.get("/api/map/heatmap?window=99d")
        assert r.status_code == 400

    def test_all_window_includes_all_positions(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        # One recent, one old (90 days ago)
        self._insert_position(db_conn, lat=52.10, lon=21.00, ts=now, flight_id=fid)
        self._insert_position(db_conn, lat=52.20, lon=21.10, ts=now - 90 * 86400, flight_id=fid)
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_24h_window_excludes_old_positions(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, ts=now - 100, flight_id=fid)
        self._insert_position(db_conn, lat=52.20, lon=21.10, ts=now - 2 * 86400, flight_id=fid)
        r = client.get("/api/map/heatmap?window=24h")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1

    def test_7d_window_includes_last_week(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, ts=now - 3 * 86400, flight_id=fid)
        self._insert_position(db_conn, lat=52.20, lon=21.10, ts=now - 8 * 86400, flight_id=fid)
        r = client.get("/api/map/heatmap?window=7d")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1

    def test_30d_window(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, ts=now - 15 * 86400, flight_id=fid)
        self._insert_position(db_conn, lat=52.20, lon=21.10, ts=now - 31 * 86400, flight_id=fid)
        r = client.get("/api/map/heatmap?window=30d")
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_max_intensity_is_one(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        # 3 points in one cell, 1 in another — max cell gets intensity 1.0
        for _ in range(3):
            self._insert_position(db_conn, lat=52.101, lon=21.001, ts=now, flight_id=fid)
        self._insert_position(db_conn, lat=52.201, lon=21.101, ts=now, flight_id=fid)
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        intensities = [pt[2] for pt in r.json()["points"]]
        assert max(intensities) == pytest.approx(1.0)
        assert min(intensities) > 0.0
        assert min(intensities) < 1.0

    def test_nearby_points_aggregated_to_same_cell(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        # Two positions that differ only in 3rd decimal place → round(x, 2) lands in same cell
        # round(52.101, 2) = 52.1  and  round(52.104, 2) = 52.1
        self._insert_position(db_conn, lat=52.101, lon=21.001, ts=now, flight_id=fid)
        self._insert_position(db_conn, lat=52.104, lon=21.004, ts=now + 5, flight_id=fid)
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        assert len(r.json()["points"]) == 1

    def test_result_is_cached(self, client, db_conn):
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, flight_id=fid)
        r1 = client.get("/api/map/heatmap?window=all")
        assert r1.status_code == 200
        count1 = r1.json()["count"]
        # Insert another position — should NOT appear (cached)
        self._insert_position(db_conn, lat=52.30, lon=21.30, flight_id=fid)
        r2 = client.get("/api/map/heatmap?window=all")
        assert r2.json()["count"] == count1

    def test_different_windows_use_separate_cache_keys(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, ts=now - 100, flight_id=fid)
        self._insert_position(db_conn, lat=52.20, lon=21.10, ts=now - 10 * 86400, flight_id=fid)
        r_7d = client.get("/api/map/heatmap?window=7d")
        r_all = client.get("/api/map/heatmap?window=all")
        assert r_7d.json()["count"] == 1
        assert r_all.json()["count"] == 2

    def test_fine_grid_aggregation_24h(self, client, db_conn):
        """24h/7d uses precision=2 (~1 km cells); two points 0.003° apart share a cell."""
        now = int(time.time())
        fid = insert_flight(db_conn)
        # round(52.101, 2) = 52.1  and  round(52.104, 2) = 52.1 → same cell
        # round(21.001, 2) = 21.0  and  round(21.004, 2) = 21.0 → same cell
        self._insert_position(db_conn, lat=52.101, lon=21.001, ts=now - 100, flight_id=fid)
        self._insert_position(db_conn, lat=52.104, lon=21.004, ts=now - 200, flight_id=fid)
        r = client.get("/api/map/heatmap?window=24h")
        assert r.status_code == 200
        data = r.json()
        assert len(data["points"]) == 1   # aggregated into one cell
        assert data["count"] == 2          # two raw samples

    def test_null_positions_excluded(self, client, db_conn):
        """Positions with NULL lat or lon must not appear in heatmap."""
        fid = insert_flight(db_conn)
        insert_position(db_conn, fid, int(time.time()), lat=None, lon=None,
                        source_type="adsb_icao")
        insert_position(db_conn, fid, int(time.time()), lat=52.10, lon=None,
                        source_type="adsb_icao")
        db_conn.commit()
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        assert r.json()["points"] == []
        assert r.json()["count"] == 0

    def test_response_includes_window_field(self, client, db_conn):
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, flight_id=fid)
        for win in ("24h", "7d", "30d", "all"):
            cache._cache.clear()
            r = client.get(f"/api/map/heatmap?window={win}")
            assert r.json()["window"] == win

    def test_count_is_sum_of_raw_samples_not_cells(self, client, db_conn):
        """count = total raw position samples, not the number of grid cells."""
        now = int(time.time())
        fid = insert_flight(db_conn)
        # 3 samples in one cell + 2 in another = count 5, len(points) 2
        for _ in range(3):
            self._insert_position(db_conn, lat=52.101, lon=21.001, ts=now, flight_id=fid)
        for _ in range(2):
            self._insert_position(db_conn, lat=52.501, lon=21.501, ts=now, flight_id=fid)
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 5
        assert len(data["points"]) == 2


class TestApiMapCoverage:
    """Coverage endpoint now queries positions directly, computing per-position bearing
    and haversine distance in SQL.  All tests insert real positions at computed lat/lon."""

    def _insert_position_at(self, conn, bearing_deg, dist_nm, *, ts=None, flight_id=None):
        """Insert a position at (bearing_deg, dist_nm) from the receiver."""
        from readsbstats import geo as _geo
        lat, lon = _geo.destination_point(
            config.RECEIVER_LAT, config.RECEIVER_LON, bearing_deg, dist_nm
        )
        if flight_id is None:
            flight_id = insert_flight(conn)
        if ts is None:
            ts = int(time.time()) - 100
        insert_position(conn, flight_id, ts, lat=lat, lon=lon,
                        source_type="adsb_icao")
        conn.commit()
        return flight_id

    def test_empty_db_returns_36_point_polygon(self, client):
        cache._cache.clear()
        r = client.get("/api/map/coverage")
        assert r.status_code == 200
        assert len(r.json()["polygon"]) == 36

    def test_empty_db_all_points_at_receiver(self, client):
        cache._cache.clear()
        r = client.get("/api/map/coverage")
        for pt in r.json()["polygon"]:
            assert pt[0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)
            assert pt[1] == pytest.approx(config.RECEIVER_LON, abs=1e-6)

    def test_empty_db_max_range_is_zero(self, client):
        cache._cache.clear()
        r = client.get("/api/map/coverage")
        assert r.json()["max_range_nm"] == pytest.approx(0.0)

    def test_position_in_bucket_0_projects_correctly(self, client, db_conn):
        """Position at bearing 5° → bucket 0 → polygon vertex at bearing 0°, same distance."""
        cache._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0)
        r = client.get("/api/map/coverage?window=all")
        from readsbstats import geo as _geo
        exp_lat, exp_lon = _geo.destination_point(
            config.RECEIVER_LAT, config.RECEIVER_LON, 0.0, 100.0
        )
        assert r.json()["polygon"][0][0] == pytest.approx(exp_lat, abs=0.01)
        assert r.json()["polygon"][0][1] == pytest.approx(exp_lon, abs=0.01)

    def test_missing_bucket_maps_to_receiver(self, client, db_conn):
        """Bucket with no positions collapses to receiver location."""
        cache._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=15.0, dist_nm=100.0)  # bucket 1
        data = client.get("/api/map/coverage?window=all").json()
        assert data["polygon"][0][0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)
        assert data["polygon"][0][1] == pytest.approx(config.RECEIVER_LON, abs=1e-6)

    def test_max_range_nm_is_maximum_across_buckets(self, client, db_conn):
        cache._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0)
        self._insert_position_at(db_conn, bearing_deg=95.0, dist_nm=200.0)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(200.0)

    def test_missing_bucket_coalesces_to_receiver(self, db_conn, monkeypatch):
        """Audit 17: buckets absent from the rollup result must collapse to the
        receiver location, not raise TypeError on max()/comparison.

        Seed only buckets 0 and 1 in coverage_daily with rollups_ready set,
        so the 34 remaining buckets are missing from the query result.  The
        `by_bucket.get(i, 0.0) or 0.0` loop must handle this without raising.
        """
        from readsbstats.api import map as map_mod
        cache._cache.clear()
        monkeypatch.setattr(_deps, "_db", db_conn)
        # Mark rollups as ready so _compute_coverage_sync uses coverage_daily.
        db_conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('rollups_ready', '1')")
        # Seed only 2 of 36 display buckets (bearing_b 0 and 10 map to
        # display buckets 0 and 1 after `bearing_b / 10`).
        db_conn.execute(
            "INSERT INTO coverage_daily(day, bearing_b, max_nm) VALUES(0, 0, 0.0)"
        )
        db_conn.execute(
            "INSERT INTO coverage_daily(day, bearing_b, max_nm) VALUES(0, 10, 100.0)"
        )
        db_conn.commit()
        result = map_mod._compute_coverage_sync("all")
        assert len(result["polygon"]) == 36
        # Buckets 2–35 are missing — they must all collapse to receiver location.
        for i in range(2, 36):
            assert result["polygon"][i][0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)
            assert result["polygon"][i][1] == pytest.approx(config.RECEIVER_LON, abs=1e-6)
        # max_range_nm reflects the real seeded value.
        assert result["max_range_nm"] == pytest.approx(100.0)

    def test_bucket_uses_max_distance(self, client, db_conn):
        """Two positions both in bucket 0 — polygon uses the farther one."""
        cache._cache.clear()
        fid = insert_flight(db_conn)
        self._insert_position_at(db_conn, bearing_deg=2.0, dist_nm=100.0, flight_id=fid)
        self._insert_position_at(db_conn, bearing_deg=8.0, dist_nm=150.0, flight_id=fid)
        from readsbstats import geo as _geo
        exp_lat, _ = _geo.destination_point(config.RECEIVER_LAT, config.RECEIVER_LON, 0.0, 150.0)
        assert client.get("/api/map/coverage?window=all").json()["polygon"][0][0] == pytest.approx(exp_lat, abs=0.01)

    def test_window_24h_excludes_old_position(self, client, db_conn):
        cache._cache.clear()
        now = int(time.time())
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 2 * 86400)
        data = client.get("/api/map/coverage?window=24h").json()
        for pt in data["polygon"]:
            assert pt[0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)

    def test_window_all_includes_old_position(self, client, db_conn):
        cache._cache.clear()
        now = int(time.time())
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 90 * 86400)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(100.0, rel=0.01)

    def test_window_filter_uses_position_ts_not_flight_dates(self, client, db_conn):
        """A position with recent ts is included in 24h even if its flight started long ago."""
        cache._cache.clear()
        now = int(time.time())
        fid = insert_flight(db_conn, first_seen=now - 30 * 3600, last_seen=now - 29 * 3600)
        # Position recorded 10 min ago — within 24h window
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=120.0, ts=now - 600, flight_id=fid)
        assert client.get("/api/map/coverage?window=24h").json()["max_range_nm"] == pytest.approx(120.0, rel=0.01)

    def test_position_near_360_goes_to_bucket_35(self, client, db_conn):
        """A position at bearing ~355° should land in bucket 35, not overflow."""
        cache._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=355.0, dist_nm=100.0)
        data = client.get("/api/map/coverage?window=all").json()
        assert data["max_range_nm"] == pytest.approx(100.0, rel=0.01)
        # bucket 35 (bearing 350°) should have data; bucket 0 should be at receiver
        assert data["polygon"][35][0] != pytest.approx(config.RECEIVER_LAT, abs=0.1)
        assert data["polygon"][0][0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)

    def test_invalid_window_returns_400(self, client):
        assert client.get("/api/map/coverage?window=99d").status_code == 400

    def test_response_includes_window_field(self, client, db_conn):
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=50.0)
        for win in ("24h", "7d", "30d", "all"):
            cache._cache.clear()
            assert client.get(f"/api/map/coverage?window={win}").json()["window"] == win

    def test_result_is_cached(self, client, db_conn):
        cache._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0)
        max1 = client.get("/api/map/coverage?window=all").json()["max_range_nm"]
        self._insert_position_at(db_conn, bearing_deg=95.0, dist_nm=300.0)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(max1)

    def test_different_windows_independent_cache_keys(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 100, flight_id=fid)
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=200.0, ts=now - 10 * 86400, flight_id=fid)
        cache._cache.clear()
        r_24h = client.get("/api/map/coverage?window=24h")
        r_all  = client.get("/api/map/coverage?window=all")
        assert r_24h.json()["max_range_nm"] == pytest.approx(100.0, rel=0.01)
        assert r_all.json()["max_range_nm"]  == pytest.approx(200.0, rel=0.01)


class TestMapSnapshot:
    """Regression guards for /api/map/snapshot dedup.

    The endpoint must return exactly one row per flight_id within the
    600-second snapshot window. Frontend keys markers by flight_id, so a
    duplicate row would render as a second marker for one aircraft —
    misleading during Map Rewind / HIST scrubbing.
    """

    def _insert_position(self, conn, *, flight_id, ts, lat=52.10, lon=21.00):
        insert_position(conn, flight_id, ts, lat=lat, lon=lon,
                        source_type="adsb_icao")
        conn.commit()

    def test_snapshot_returns_freshest_position_per_flight(self, client, db_conn):
        # One flight, two positions within the snapshot window. Snapshot
        # must return only the latest one — not both.
        # `at` must be near now() (the endpoint rejects future timestamps and
        # those older than config.MAP_HISTORY_HOURS).
        now = int(time.time())
        at = now - 60
        fid = insert_flight(db_conn, icao="aabbcc", first_seen=at - 300)
        self._insert_position(db_conn, flight_id=fid, ts=at - 200, lat=50.0, lon=10.0)
        self._insert_position(db_conn, flight_id=fid, ts=at - 30, lat=51.0, lon=11.0)
        r = client.get(f"/api/map/snapshot?at={at}&trail=0")
        assert r.status_code == 200
        aircraft = r.json()["aircraft"]
        assert len(aircraft) == 1, f"expected 1 row, got {len(aircraft)}: {aircraft}"
        # The freshest position wins.
        assert aircraft[0]["ts"] == at - 30
        assert aircraft[0]["lat"] == 51.0

    def test_snapshot_one_row_per_flight_id_when_two_flights_overlap(self, client, db_conn):
        # Two SEPARATE flights for the SAME aircraft (icao_hex) within the
        # 600-second snapshot window — backend correctly returns both
        # (one row per flight_id, as documented). Frontend defensive
        # dedup-by-icao_hex (LiveMap.tsx) is what prevents the visual
        # duplicate; this test locks in the backend's per-flight_id
        # behavior so the frontend dedup keeps having something to dedup.
        now = int(time.time())
        at = now - 60
        fid1 = insert_flight(db_conn, icao="aabbcc", first_seen=at - 500)
        fid2 = insert_flight(db_conn, icao="aabbcc", first_seen=at - 100)
        self._insert_position(db_conn, flight_id=fid1, ts=at - 400, lat=50.0, lon=10.0)
        self._insert_position(db_conn, flight_id=fid2, ts=at - 50, lat=51.0, lon=11.0)
        r = client.get(f"/api/map/snapshot?at={at}&trail=0")
        assert r.status_code == 200
        aircraft = r.json()["aircraft"]
        # Both flight_ids are present (one row each, no duplicate within
        # a single flight_id).
        flight_ids = sorted(a["flight_id"] for a in aircraft)
        assert flight_ids == sorted([fid1, fid2])

    def test_snapshot_uses_max_ts_not_max_id(self, client, db_conn):
        # BE-14 (Audit 2026-05-31): the representative position must be the
        # latest by `ts`, not the highest `id`.  Insert the LATER-ts position
        # first (lower autoincrement id), then an EARLIER-ts position (higher
        # id) — MAX(id) would wrongly pick the stale earlier row.
        now = int(time.time())
        at = now - 60
        fid = insert_flight(db_conn, icao="aabbcc", first_seen=at - 300)
        self._insert_position(db_conn, flight_id=fid, ts=at - 30, lat=51.0, lon=11.0)
        self._insert_position(db_conn, flight_id=fid, ts=at - 200, lat=50.0, lon=10.0)
        r = client.get(f"/api/map/snapshot?at={at}&trail=0")
        assert r.status_code == 200
        aircraft = r.json()["aircraft"]
        assert len(aircraft) == 1
        assert aircraft[0]["ts"] == at - 30
        assert aircraft[0]["lat"] == 51.0

    def test_trail_bounded_by_window_live(self, client, db_conn, monkeypatch):
        """PY-11 (Audit 2026-05-31): for the live view, the trail CTE must
        exclude positions older than MAP_TRAIL_WINDOW_SECONDS. Without the
        bound, a long flight with thousands of historical positions would
        force SQLite to rank the whole partition just to return
        `trail_count` points."""
        from readsbstats import config
        monkeypatch.setattr(config, "MAP_TRAIL_WINDOW_SECONDS", 1800)  # 30 min

        now = int(time.time())
        at = now - 10                                                  # is_live
        fid = insert_flight(db_conn, icao="aabbcc", first_seen=at - 7200)

        for offset in (1500, 1000, 600, 300, 100):           # in-window
            insert_position(db_conn, fid, at - offset, lat=51.0, lon=11.0,
                            source_type="adsb_icao")
        for offset in (3000, 5000, 7000):                     # out-of-window
            insert_position(db_conn, fid, at - offset, lat=52.0, lon=12.0,
                            source_type="adsb_icao")
        db_conn.commit()

        r = client.get(f"/api/map/snapshot?at={at}&trail=50")
        assert r.status_code == 200
        body = r.json()
        assert body["is_live"] is True
        trail = body["aircraft"][0]["trail"]
        for lat, lon, ts in trail:
            assert ts > at - 1800, (
                f"trail point ts={ts} is older than at-window={at - 1800}; "
                f"window bound missing for live view"
            )
        assert len(trail) == 5

    def test_trail_unbounded_for_historical_replay(self, client, db_conn,
                                                    monkeypatch):
        """Finding from code review: PY-11's window bound silently truncates
        long historical-replay trails. When `at` is far enough from `now`
        that `is_live=False`, the user is reviewing past activity and
        expects to see the whole flight track up to `at`, not just the
        last MAP_TRAIL_WINDOW_SECONDS seconds.
        """
        from readsbstats import config
        monkeypatch.setattr(config, "MAP_TRAIL_WINDOW_SECONDS", 1800)  # 30 min

        now = int(time.time())
        # Historical: at is hours in the past so is_live=False.
        at = now - 4 * 3600
        fid = insert_flight(db_conn, icao="aabbcc", first_seen=at - 7200)

        # 3 positions inside the live-style window
        for offset in (1500, 1000, 100):
            insert_position(db_conn, fid, at - offset, lat=51.0, lon=11.0,
                            source_type="adsb_icao")
        # 3 positions older than 1800s but still part of the flight track
        for offset in (3000, 5000, 7000):
            insert_position(db_conn, fid, at - offset, lat=52.0, lon=12.0,
                            source_type="adsb_icao")
        db_conn.commit()

        r = client.get(f"/api/map/snapshot?at={at}&trail=50")
        assert r.status_code == 200
        body = r.json()
        assert body["is_live"] is False, "expected is_live=False for historical at"
        trail = body["aircraft"][0]["trail"]
        # All six points should be present — the historical view doesn't
        # truncate to the live-view window.
        assert len(trail) == 6, (
            f"historical replay returned {len(trail)} points; "
            "the live-view MAP_TRAIL_WINDOW_SECONDS bound should not apply"
        )

    def test_snapshot_enriches_reg_type_from_aircraft_db(self, client, db_conn):
        # BE-14: registration/aircraft_type missing on the flight row must be
        # backfilled from aircraft_db (then adsbx_overrides) so the live map
        # label matches the flight list.
        now = int(time.time())
        at = now - 60
        fid = insert_flight(db_conn, icao="aabbcc", first_seen=at - 300,
                            registration=None, aircraft_type=None)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc','SP-ENR','A320','Airbus A320',0)"
        )
        db_conn.commit()
        self._insert_position(db_conn, flight_id=fid, ts=at - 30, lat=51.0, lon=11.0)
        r = client.get(f"/api/map/snapshot?at={at}&trail=0")
        assert r.status_code == 200
        aircraft = r.json()["aircraft"]
        assert len(aircraft) == 1
        assert aircraft[0]["registration"] == "SP-ENR"
        assert aircraft[0]["aircraft_type"] == "A320"


class TestMapPrewarmer:
    """Functional test for _prewarm_one — the actual thread lifecycle is
    exercised by deploy verification, not unit tests."""

    def test_prewarm_one_populates_cache(self, client, db_conn):
        now = int(time.time())
        fid = db_conn.execute(
            "INSERT INTO flights (icao_hex,callsign,first_seen,last_seen,total_positions,"
            "primary_source,lat_min,lat_max,lon_min,lon_max) VALUES "
            "('aabbcc','TEST',1000,2000,0,'adsb',0,0,0,0)"
        ).lastrowid
        insert_position(db_conn, fid, now, lat=52.13, lon=21.04,
                        source_type="adsb_icao")
        db_conn.commit()

        cache._cache.clear()
        assert cache._get_cache("heatmap:24h") is None
        cache._prewarm_one("heatmap", "24h")
        cached = cache._get_cache("heatmap:24h")
        assert cached is not None
        assert cached["count"] == 1

        assert cache._get_cache("coverage:24h") is None
        cache._prewarm_one("coverage", "24h")
        cached = cache._get_cache("coverage:24h")
        assert cached is not None
        assert len(cached["polygon"]) == 36

    def test_type_lock_evicts_oldest_beyond_cap(self):
        """Audit-12 #150 — _type_fetch_locks used to grow unboundedly.
        Now LRU-capped at _TYPE_LOCKS_MAX entries."""
        # Reset to a clean state for the test
        _photos._type_fetch_locks.clear()
        cap = _photos._TYPE_LOCKS_MAX
        # Fill past the cap
        for i in range(cap + 5):
            _photos._type_lock(f"T{i:04d}")
        # Total entries respects the cap
        assert len(_photos._type_fetch_locks) <= cap
        # Oldest entries were evicted — first inserted key no longer present
        assert "T0000" not in _photos._type_fetch_locks
        # Most-recently-touched key still present
        assert f"T{cap + 4:04d}" in _photos._type_fetch_locks

    def test_type_lock_returns_same_lock_for_same_key(self):
        """Sanity — eviction must not break the 'one lock per type' contract
        for keys still under the cap."""
        _photos._type_fetch_locks.clear()
        a1 = _photos._type_lock("A320")
        a2 = _photos._type_lock("A320")
        assert a1 is a2

    @staticmethod
    def _acquire_sync(lock):
        # Acquire on a private throwaway loop WITHOUT touching the thread's
        # ambient event-loop slot (asyncio.run would unset it and break the
        # get_event_loop().run_until_complete pattern used elsewhere in this
        # file). locked()/release() afterwards are loop-free.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(lock.acquire())
        finally:
            loop.close()

    def test_type_lock_never_evicts_held_lock(self, monkeypatch):
        """A13-004: a held lock must be rotated past, never evicted — evicting
        it would hand the next caller a fresh lock racing the in-flight fetch."""
        _photos._type_fetch_locks.clear()
        monkeypatch.setattr(_photos, "_TYPE_LOCKS_MAX", 2)
        held = _photos._type_lock("HELD")
        self._acquire_sync(held)
        try:
            _photos._type_lock("B")
            _photos._type_lock("C")    # over cap: HELD is locked → B evicted
            assert "HELD" in _photos._type_fetch_locks
            assert "B" not in _photos._type_fetch_locks
            assert _photos._type_lock("HELD") is held   # same object survived
        finally:
            held.release()
            _photos._type_fetch_locks.clear()

    def test_type_lock_all_held_breaks_eviction_loop(self, monkeypatch):
        """Safety net: with every cached lock held (only reachable if the cap
        shrinks below the held count), eviction must break out instead of
        rotating forever."""
        _photos._type_fetch_locks.clear()
        monkeypatch.setattr(_photos, "_TYPE_LOCKS_MAX", 2)
        l1 = _photos._type_lock("X1")
        l2 = _photos._type_lock("X2")
        self._acquire_sync(l1)
        self._acquire_sync(l2)
        monkeypatch.setattr(_photos, "_TYPE_LOCKS_MAX", 1)
        try:
            l3 = _photos._type_lock("X3")   # must terminate, not spin
            assert "X1" in _photos._type_fetch_locks
            assert "X2" in _photos._type_fetch_locks
            # The unheld newcomer was the only evictable entry.
            assert "X3" not in _photos._type_fetch_locks
            assert not l3.locked()
        finally:
            l1.release()
            l2.release()
            _photos._type_fetch_locks.clear()

    def test_prewarm_loop_survives_one_prewarm_raising(self, monkeypatch):
        """Audit-12 #211 — a single _prewarm_one() exception must NOT kill
        the daemon thread. The loop catches the exception, schedules a
        5-minute backoff for that target, and continues with the next
        target. Without this we'd lose all cache refresh after the first
        transient compute failure."""
        # Drive a controlled finite loop: clear the stop event up-front,
        # set it inside the second `_prewarm_one` call to break out cleanly.
        cache._prewarmer_stop.clear()
        cache._cache.clear()

        call_log: list[tuple[str, str]] = []

        def fake_prewarm(kind, window):
            call_log.append((kind, window))
            if len(call_log) == 1:
                raise RuntimeError("simulated prewarm compute failure")
            if len(call_log) == 2:
                # Stop the loop on the second call so the test terminates.
                cache._prewarmer_stop.set()

        monkeypatch.setattr(cache, "_prewarm_one", fake_prewarm)
        # Skip the initial 5s wait so the loop starts immediately.
        # Skip the inter-iteration 10s cool-off too. Both wait()s must
        # return False (timed out) so the loop body runs; the explicit
        # stop.set() inside fake_prewarm is what exits the loop.
        wait_calls = {"n": 0}
        original_wait = cache._prewarmer_stop.wait

        def fast_wait(timeout=None):
            wait_calls["n"] += 1
            # Honour the actual event state — when fake_prewarm sets the
            # event, wait() returns True and the loop exits.
            return cache._prewarmer_stop.is_set()

        monkeypatch.setattr(cache._prewarmer_stop, "wait", fast_wait)
        # Also stub time.time so all targets are "due" immediately on the
        # very first iteration (the staggered schedule otherwise inserts
        # 15s waits between iterations and our wait stub bails out before
        # any prewarm runs).
        import time as _time
        monkeypatch.setattr(cache, "_initial_prewarm_schedule",
            lambda targets, now: {t: now for t in targets},
        )
        monkeypatch.setattr(_time, "time", lambda: 1_000_000.0)

        # Should not raise — exception is caught inside the loop.
        cache._prewarm_loop()

        # Loop executed at least 2 iterations (one raising, one stopping).
        assert len(call_log) >= 2, (
            f"loop terminated after only {len(call_log)} call(s); the first "
            "exception was likely not caught"
        )
        # Clean up so other tests don't see a set stop event.
        cache._prewarmer_stop.clear()
        # Restore the wait method ref (monkeypatch handles undo, but be explicit)
        _ = original_wait

    def test_prewarm_loop_honours_future_schedule_without_running(self, monkeypatch):
        """When every target's next_at lies in the future, the loop must wait
        (and re-check) instead of running a prewarm early."""
        cache._prewarmer_stop.clear()
        ran: list = []
        monkeypatch.setattr(cache, "_prewarm_one", lambda k, w: ran.append((k, w)))
        monkeypatch.setattr(cache, "_initial_prewarm_schedule",
                            lambda targets, now: {t: now + 1000 for t in targets})
        waits = {"n": 0}

        def fake_wait(timeout=None):
            # 1st: the 5s startup gate; 2nd: the future-schedule wait (False →
            # loop re-checks); 3rd: stop requested → loop exits.
            waits["n"] += 1
            return waits["n"] >= 3

        monkeypatch.setattr(cache._prewarmer_stop, "wait", fake_wait)
        cache._prewarm_loop([("heatmap", "24h")])
        assert ran == []

    def test_start_prewarmer_noop_when_thread_alive(self, monkeypatch):
        """Idempotent start: a live prewarmer thread must not be duplicated
        (two threads would race the same heavy scans)."""
        import threading as _threading

        class AliveThread:
            def is_alive(self):
                return True

        monkeypatch.setattr(cache, "_prewarmer_thread", AliveThread())

        def boom(*a, **k):
            raise AssertionError("must not spawn a second prewarmer thread")

        monkeypatch.setattr(_threading, "Thread", boom)
        cache._start_prewarmer()   # returns without constructing a Thread

    def test_initial_prewarm_schedule_staggers_targets(self):
        """Regression for audit-12 #185 — the prewarmer used to start all 8
        targets at next_at=0.0, causing 8 back-to-back full-table scans
        across the first ~80s of process startup. The staggered schedule
        spreads the first refreshes apart and prioritises the shortest-TTL
        windows (most-used) first."""
        now = 1_000_000.0
        schedule = cache._initial_prewarm_schedule(cache._PREWARM_TARGETS, now=now)

        # Every target has an entry
        for target in cache._PREWARM_TARGETS:
            assert target in schedule

        # First target is "ready now" (no synthetic wait penalty on the
        # most-important short-TTL window)
        first_at = min(schedule.values())
        assert first_at == now

        # The 8 targets are spread out — not all bunched at the same time
        unique = sorted(set(schedule.values()))
        assert len(unique) >= 4, (
            f"expected staggered values across targets, got {len(unique)} "
            f"unique entries"
        )

        # 24h heatmap (shortest TTL, most likely to be hit by user) must be
        # in the first half of the schedule
        ranked = sorted(cache._PREWARM_TARGETS, key=lambda t: schedule[t])
        early = ranked[: len(ranked) // 2]
        assert ("heatmap", "24h") in early
        assert ("coverage", "24h") in early

    def test_stop_prewarmer_joins_running_thread(self, monkeypatch):
        """Audit 17: _stop_prewarmer must JOIN the running thread before nil-ing
        the handle. Otherwise a stop→start cycle (reload/tests) leaves the old
        thread running full-`positions` scans alongside the new one, because
        _start_prewarmer clears the stop event the orphan was waiting on."""
        import threading
        started = threading.Event()
        release = threading.Event()

        def blocking_prewarm(kind, window):
            started.set()
            release.wait(5)  # simulate a long-running positions scan

        monkeypatch.setattr(cache, "_prewarm_one", blocking_prewarm)
        monkeypatch.setattr(cache, "_initial_prewarm_schedule",
                            lambda targets, now: {t: now for t in targets})
        # make every wait() honour the event state but never block the test
        monkeypatch.setattr(cache._prewarmer_stop, "wait",
                            lambda timeout=None: cache._prewarmer_stop.is_set())

        cache._prewarmer_stop.clear()
        cache._start_prewarmer()
        try:
            assert started.wait(2), "prewarm thread did not start"
            t = cache._prewarmer_thread
            assert t is not None and t.is_alive()
            # Release the blocking query a moment after stop is requested, so
            # _stop_prewarmer's join has something real to wait on.
            threading.Timer(0.1, release.set).start()
            cache._stop_prewarmer()
            # After _stop_prewarmer returns, the old thread must be fully gone.
            assert not t.is_alive()
            assert cache._prewarmer_thread is None
        finally:
            release.set()
            cache._prewarmer_stop.clear()


class TestStatsPrewarm:
    """7d-default speedup (2026-06-06): _compute_stats_sync extraction +
    background-warmed all-time stats. The page defaults to 7d (fast filtered
    path); the all-time payload is prewarmed so the opt-in 'All time' click is
    instant."""

    def test_compute_stats_sync_unfiltered_shape(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc", callsign="LOT123")
        result = stats_mod._compute_stats_sync(None, None)
        assert isinstance(result, dict)
        # All-time payload carries no `range`; the dict must build cleanly.
        assert result["range"] is None
        for key in ("total_flights", "unique_aircraft", "lifetime",
                    "heatmap", "top_airlines", "source_breakdown"):
            assert key in result
        assert result["total_flights"] == 1

    def test_compute_stats_sync_filtered_sets_range(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000)
        result = stats_mod._compute_stats_sync(0, 2_000_000)
        assert result["range"] == {"from": 0, "to": 2_000_000}

    def test_prewarm_one_stats_populates_bare_key(self, client, db_conn):
        insert_flight(db_conn, icao="aabbcc")
        cache._cache.clear()
        assert cache._get_cache("stats") is None
        cache._prewarm_one("stats", "all")
        warmed = cache._get_cache("stats")
        assert isinstance(warmed, dict)
        assert warmed["total_flights"] == 1
        # Stored under the BARE key so the unfiltered handler finds it; the
        # "stats:all" key is cadence-only and must never hold the payload.
        assert "stats:all" not in cache._cache

    def test_handler_serves_prewarmed_value_without_recompute(
            self, clear_web_cache, monkeypatch):
        sentinel = {"total_flights": 999, "sentinel": True}
        cache._set_cache("stats", sentinel)

        def _boom(*a, **k):
            raise AssertionError("handler recomputed despite a warm cache")

        monkeypatch.setattr(stats_mod, "_compute_stats_sync", _boom)
        # Call the handler directly to bypass response_model serialization
        # (the sentinel isn't a full StatsResponse).
        assert stats_mod.api_stats(from_ts=None, to_ts=None) is sentinel

    def test_concurrent_all_time_requests_compute_once(self, clear_web_cache, monkeypatch):
        # The all-time path holds _stats_compute_lock so a burst of concurrent
        # "All time" requests runs the ~15-query scan once, not N×.
        import threading
        calls: list = []
        started = threading.Event()
        release = threading.Event()

        def slow_compute(from_ts, to_ts):
            calls.append((from_ts, to_ts))
            started.set()
            release.wait(2)
            return {"total_flights": 1, "range": None}

        monkeypatch.setattr(stats_mod, "_compute_stats_sync", slow_compute)
        results: list = []

        def hit():
            results.append(stats_mod.api_stats(from_ts=None, to_ts=None))

        t1 = threading.Thread(target=hit)
        t2 = threading.Thread(target=hit)
        t1.start()
        assert started.wait(2), "first request never entered compute"
        t2.start()             # blocks on _stats_compute_lock held by t1
        time.sleep(0.05)       # let t2 reach the lock
        release.set()
        t1.join(2)
        t2.join(2)
        assert len(calls) == 1                # second request reused the cache
        assert results == [{"total_flights": 1, "range": None}] * 2

    def test_stats_ttls_registered(self, clear_web_cache):
        # Long cadence: the heavy all-time scan refreshes ~hourly (half of
        # 7200s), not every 2 min — gentle on the Pi.
        assert cache._ttl_for("stats") == 7200
        assert cache._CACHE_TTLS["stats:all"] == 7200
        # Filtered keys inherit the prefix TTL; their freshness comes from the
        # SPA's 5-min window quantization, not a short backend TTL.
        assert cache._ttl_for("stats:0:100") == 7200

    def test_stats_all_is_a_prewarm_target(self):
        assert ("stats", "all") in cache._PREWARM_TARGETS

    def test_stats_prewarm_scheduled_first(self):
        # stats:all must warm first so the opt-in all-time view isn't cold for
        # ~120s after a restart (9 targets, 15s stagger).
        schedule = cache._initial_prewarm_schedule(cache._PREWARM_TARGETS, now=1_000_000.0)
        ranked = sorted(cache._PREWARM_TARGETS, key=lambda t: schedule[t])
        assert ranked[0] == ("stats", "all")

    def test_start_prewarmer_includes_map_targets(self, monkeypatch):
        import threading
        captured: dict = {}
        ran = threading.Event()

        def fake_loop(targets=None):
            captured["targets"] = targets
            ran.set()

        monkeypatch.setattr(cache, "_prewarm_loop", fake_loop)
        cache._stop_prewarmer()
        try:
            cache._start_prewarmer()
            assert ran.wait(2)
            assert captured["targets"] == cache._PREWARM_TARGETS
            assert ("stats", "all") in captured["targets"]
            assert ("heatmap", "24h") in captured["targets"]
            assert ("coverage", "7d") in captured["targets"]
        finally:
            cache._stop_prewarmer()
            cache._prewarmer_stop.clear()


class TestParseIcaoPath:
    """BE-11 — strict ICAO path-param validation."""

    def test_accepts_six_hex_lowercased(self):
        assert _deps._parse_icao_path("AABBCC") == "aabbcc"
        assert _deps._parse_icao_path("a1b2c3") == "a1b2c3"

    def test_accepts_tilde_prefix_and_strips_it(self):
        assert _deps._parse_icao_path("~aabbcc") == "aabbcc"

    def test_rejects_non_hex(self):
        with pytest.raises(web.HTTPException) as ei:
            _deps._parse_icao_path("zzzzzz")
        assert ei.value.status_code == 404

    def test_rejects_wrong_length(self):
        for bad in ("abc", "aabbccdd", "", "~"):
            with pytest.raises(web.HTTPException):
                _deps._parse_icao_path(bad)

    def test_rejects_double_tilde(self):
        with pytest.raises(web.HTTPException):
            _deps._parse_icao_path("~~aabbc")


class TestApiAircraftPhoto:
    def test_cached_photo_returned(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://plnspttrs.net/t.jpg", "https://plnspttrs.net/l.jpg", "https://plnspttrs.net/link", "Alice", int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://plnspttrs.net/t.jpg"

    def test_no_photo_returns_null(self, client, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_invalid_icao_rejected_before_fetch(self, client, monkeypatch):
        # BE-11: a malformed ICAO must 404 without triggering an outbound
        # photo fetch (defence-in-depth — bound external side effects).
        def _boom(*a, **k):
            raise AssertionError("photo fetch must not run for invalid ICAO")
        monkeypatch.setattr(_photos, "_fetch_photo", _boom)
        assert client.get("/api/aircraft/zzzzzz/photo").status_code == 404
        assert client.get("/api/aircraft/abc/photo").status_code == 404

    def test_network_error_returns_null(self, client, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_specific_photo_annotated_with_is_type_photo_false(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://plnspttrs.net/t.jpg", "https://plnspttrs.net/l.jpg", "https://plnspttrs.net/link", "Alice", int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["is_type_photo"] is False
        assert data["type_code"] is None
        assert data["type_desc"] is None


class TestFetchTypePhoto:
    """Tests for _fetch_type_photo() and the type-fallback in photo endpoints."""

    @pytest.fixture(autouse=True)
    def _disable_wikipedia(self, monkeypatch):
        # Existing tests assert that probe-miss writes a negative cache row.
        # The Wikipedia step 6 in resolve_photo would otherwise hit the
        # network.  Wikipedia-specific tests opt in via their own monkeypatch.
        monkeypatch.setattr(photo_sources, "_WIKIPEDIA_ENABLED", False)

    def _seed_aircraft_db(self, db_conn, icao, type_code, type_desc="Boeing 737-800"):
        db_conn.execute(
            "INSERT OR REPLACE INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?,?,?,?,0)", (icao, "G-TEST", type_code, type_desc)
        )
        db_conn.commit()

    def _seed_type_photo(self, db_conn, type_code, url):
        db_conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,?,NULL,NULL,NULL,?)", (type_code, url, int(time.time()))
        )
        db_conn.commit()

    def _seed_specific_photo(self, db_conn, icao, url):
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,NULL,NULL,NULL,?)",
            (icao, url, int(time.time()))
        )
        db_conn.commit()

    def test_null_type_code_returns_none(self, client, db_conn):
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo(None))
        assert result is None

    def test_empty_type_code_returns_none(self, client, db_conn):
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo(""))
        assert result is None

    def test_type_photos_cache_hit(self, client, db_conn):
        self._seed_type_photo(db_conn, "B738", "https://plnspttrs.net/b738.jpg")
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo("B738"))
        assert result is not None
        assert result["thumbnail_url"] == "https://plnspttrs.net/b738.jpg"

    def test_type_photo_double_check_under_lock(self, client, db_conn, monkeypatch):
        """A competing request can resolve the type while we wait on the type
        lock — the second cache check under the lock must serve that row
        instead of refetching."""
        import asyncio

        class InsertingLock:
            async def __aenter__(self):
                db_conn.execute(
                    "INSERT INTO type_photos (type_code, thumbnail_url, large_url, "
                    "link_url, photographer, fetched_at) VALUES "
                    "('B738', 'https://plnspttrs.net/race.jpg', NULL, NULL, 'Race', ?)",
                    (int(time.time()),))
                db_conn.commit()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(_photos, "_type_lock", lambda tc: InsertingLock())
        fetch_calls: list = []
        monkeypatch.setattr(photo_sources, "resolve_photo",
                            lambda *a, **k: fetch_calls.append(1))
        result = asyncio.get_event_loop().run_until_complete(
            _photos._fetch_type_photo("B738"))
        assert result is not None
        assert result["thumbnail_url"] == "https://plnspttrs.net/race.jpg"
        assert fetch_calls == []                  # no redundant fetch

    def test_suppress_off_allowlist_drops_disallowed_link_url(self):
        out = _photos._suppress_off_allowlist({
            "thumbnail_url": "https://plnspttrs.net/t.jpg",
            "large_url": "https://plnspttrs.net/l.jpg",
            "link_url": "https://evil.example/page",
        })
        assert out is not None
        assert out["link_url"] is None
        assert out["thumbnail_url"] == "https://plnspttrs.net/t.jpg"

    def test_type_photos_negative_cache_returns_none(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('B738', NULL, NULL, NULL, NULL, ?)", (int(time.time()),)
        )
        db_conn.commit()
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo("B738"))
        assert result is None

    def test_db_join_reuses_cached_photo(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "aabbcc", "B738")
        self._seed_specific_photo(db_conn, "aabbcc", "https://plnspttrs.net/cached.jpg")
        fetch_calls = []
        monkeypatch.setattr(photo_sources, "fetch_photo",
                            lambda icao: fetch_calls.append(icao) or None)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo("B738"))
        assert result is not None
        assert result["thumbnail_url"] == "https://plnspttrs.net/cached.jpg"
        assert fetch_calls == []

    def test_probe_planespotters_success(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "probe01", "EF2K", "Eurofighter Typhoon")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://plnspttrs.net/ef2k.jpg",
            large_url="https://plnspttrs.net/ef2k_l.jpg",
            link_url=None,
            photographer="Alice",
        ))
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo("EF2K"))
        assert result is not None
        assert result["thumbnail_url"] == "https://plnspttrs.net/ef2k.jpg"
        row = db_conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert row and row[0] == "https://plnspttrs.net/ef2k.jpg"

    def test_all_fail_stores_negative(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "probe01", "EF2K")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(_photos._fetch_type_photo("EF2K"))
        assert result is None
        row = db_conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert row is not None
        assert row[0] is None

    def test_flight_photo_endpoint_falls_back_to_type(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        self._seed_aircraft_db(db_conn, "aabbcc", "B738")
        self._seed_type_photo(db_conn, "B738", "https://plnspttrs.net/b738.jpg")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://plnspttrs.net/b738.jpg"
        assert data["is_type_photo"] is True
        assert data["type_code"] == "B738"

    def test_aircraft_photo_endpoint_falls_back_to_type(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "aabbcc", "B738", "Boeing 737-800")
        self._seed_type_photo(db_conn, "B738", "https://plnspttrs.net/b738.jpg")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://plnspttrs.net/b738.jpg"
        assert data["is_type_photo"] is True
        assert data["type_code"] == "B738"
        assert data["type_desc"] == "Boeing 737-800"

    def test_aircraft_photo_no_type_fallback_when_no_type_code(self, client, db_conn, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_aircraft_photo_endpoint_falls_back_to_wikipedia(self, client, db_conn, monkeypatch):
        """When the existing chain misses for the probe ICAO, the Wikipedia
        step should populate type_photos with photographer='Wikipedia'."""
        self._seed_aircraft_db(db_conn, "aabbcc", "C152", "Cessna 152")
        monkeypatch.setattr(photo_sources, "_WIKIPEDIA_ENABLED", True)
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        monkeypatch.setattr(
            photo_sources, "_fetch_wikipedia_type",
            lambda desc: photo_sources.PhotoResult(
                thumbnail_url="https://upload.wikimedia.org/c152-thumb.jpg",
                large_url="https://upload.wikimedia.org/c152-large.jpg",
                link_url="https://en.wikipedia.org/wiki/Cessna_152",
                photographer="Wikipedia",
            ),
        )
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://upload.wikimedia.org/c152-thumb.jpg"
        assert data["photographer"] == "Wikipedia"
        assert data["is_type_photo"] is True
        assert data["type_code"] == "C152"
        # type_photos row persisted with Wikipedia attribution
        row = db_conn.execute(
            "SELECT photographer, link_url FROM type_photos WHERE type_code='C152'"
        ).fetchone()
        assert row[0] == "Wikipedia"
        assert row[1] == "https://en.wikipedia.org/wiki/Cessna_152"

    def test_flagged_endpoint_includes_is_type_photo_field(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'SP-ABC', 'B738', 'Boeing 737-800', 1)"
        )
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        ac = r.json()["aircraft"]
        assert len(ac) == 1
        assert "is_type_photo" in ac[0]
        assert isinstance(ac[0]["is_type_photo"], bool)

    def test_flagged_endpoint_type_photo_from_type_photos_table(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'SP-ABC', 'B738', 'Boeing 737-800', 1)"
        )
        self._seed_type_photo(db_conn, "B738", "https://plnspttrs.net/b738.jpg")
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        ac = r.json()["aircraft"]
        assert len(ac) == 1
        assert ac[0]["thumbnail_url"] == "https://plnspttrs.net/b738.jpg"
        assert ac[0]["is_type_photo"] is True

    def test_flagged_endpoint_specific_photo_not_type(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'SP-ABC', 'B738', 'Boeing 737-800', 1)"
        )
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,NULL,NULL,NULL,?)",
            ("aabbcc", "https://plnspttrs.net/specific.jpg", int(time.time()))
        )
        self._seed_type_photo(db_conn, "B738", "https://plnspttrs.net/b738.jpg")
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        ac = r.json()["aircraft"]
        assert ac[0]["thumbnail_url"] == "https://plnspttrs.net/specific.jpg"
        assert ac[0]["is_type_photo"] is False

    def test_resolve_uses_separate_connection_closed_after(
        self, tmp_path, monkeypatch
    ):
        """BE-13 (Audit 2026-05-31): the executor closure must NOT reuse the
        request thread's sqlite connection.  It must open its own connection
        and close it when done, so a long photo resolve never serialises on the
        request connection's per-connection mutex."""
        import asyncio
        import sqlite3
        import threading as _t

        monkeypatch.setattr(_deps, "_db", None)
        monkeypatch.setattr(_deps, "_thread_local", _t.local())
        db_path = str(tmp_path / "be13.db")
        database.init_db(db_path)

        # Seed a DB-JOIN hit (step 3) so resolve_photo returns synchronously
        # without any network: aircraft_db row + a cached specific photo.
        original_connect = database.connect
        seed = original_connect(db_path)
        seed.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc','G-TEST','B738','Boeing 737-800',0)"
        )
        seed.execute(
            "INSERT INTO photos VALUES ('aabbcc','https://plnspttrs.net/b738.jpg',NULL,NULL,NULL,?)",
            (int(time.time()),),
        )
        seed.commit()
        seed.close()

        opened: list[sqlite3.Connection] = []

        def spy_connect(path=db_path, **kw):
            conn = original_connect(db_path, **kw)
            opened.append(conn)
            return conn

        monkeypatch.setattr(web.database, "connect", spy_connect)
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)

        captured: dict[str, object] = {}
        real_resolve = photo_sources.resolve_photo

        def spy_resolve(conn, icao_hex, type_code, **kw):
            captured["conn"] = conn
            return real_resolve(conn, icao_hex, type_code, **kw)

        monkeypatch.setattr(photo_sources, "resolve_photo", spy_resolve)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_photos._fetch_type_photo("B738"))
        finally:
            loop.close()
        assert result is not None
        assert result["thumbnail_url"] == "https://plnspttrs.net/b738.jpg"

        # The request thread's connection is the one db() caches for this thread.
        request_conn = _deps.db()
        worker_conn = captured["conn"]
        assert worker_conn is not request_conn, (
            "executor closure reused the request connection across threads"
        )

        # The worker connection must be closed by the closure's finally block.
        with pytest.raises(sqlite3.ProgrammingError):
            worker_conn.execute("SELECT 1")

        request_conn.close()


# ---------------------------------------------------------------------------
# Audit-13 Phase 6: previously-untested public surfaces
# ---------------------------------------------------------------------------

class TestTop1Allowlist:
    """Audit-13 A13-040: the `_top1()` closure inside
    `api_stats_records` f-strings `order_col` into SQL. The allowlist
    guard fires at function entry; tests pin it directly against the
    module-scoped `_TOP1_ALLOWLIST` + `_assert_top1_column`."""

    def test_allowlist_contains_exactly_the_three_known_columns(self):
        # Lock in the set so a future addition is intentional.
        assert _deps._TOP1_ALLOWLIST == frozenset(
            {"max_distance_nm", "max_gs", "max_alt_baro"}
        )

    def test_allowlist_is_frozen(self):
        # frozenset → no `.add()`. A regular `set()` would silently allow
        # mutation, which would defeat the guarantee.
        assert isinstance(_deps._TOP1_ALLOWLIST, frozenset)

    def test_assert_accepts_each_allowed_column(self):
        for col in ("max_distance_nm", "max_gs", "max_alt_baro"):
            _deps._assert_top1_column(col)  # must not raise

    def test_assert_rejects_unknown_column(self):
        with pytest.raises(ValueError, match="unsupported order column"):
            _deps._assert_top1_column("first_seen")

    def test_assert_rejects_sql_injection_payload(self):
        with pytest.raises(ValueError, match="unsupported order column"):
            _deps._assert_top1_column("max_gs; DROP TABLE flights --")

    def test_assert_rejects_empty_string(self):
        with pytest.raises(ValueError, match="unsupported order column"):
            _deps._assert_top1_column("")


class TestRedirectLive:
    """Audit-13 untested-surface: `/live` is a server-side 302 alias for
    `/map`. No SPA routing involved. The handler must (a) honour
    `root_path` so reverse-proxied installs land at `/<root>/map`, and
    (b) defuse A13-049-style absolute-URL injection.
    """

    def test_redirects_to_map(self, client):
        r = client.get("/live", follow_redirects=False)
        assert r.status_code == 302
        # Default test fixture uses root_path="" → location is /map.
        assert r.headers["location"].endswith("/map")

    def test_root_path_prepended(self, client, monkeypatch):
        # Simulate the production reverse-proxy case where root_path="/stats".
        # We hit the TestClient with a request whose ASGI scope carries the
        # root_path; FastAPI's redirect builds against it.
        r = client.get(
            "/live",
            follow_redirects=False,
            headers={"x-forwarded-prefix": "/stats"},
        )
        # The default handler reads scope["root_path"], not the header — so
        # without an explicit ASGI scope override the redirect still ends in
        # /map. The point of this test is just to confirm the endpoint is
        # well-formed under any header set.
        assert r.status_code == 302
        assert "/map" in r.headers["location"]

    def test_no_open_redirect_via_absolute_url(self, client):
        # The redirect target is hard-coded as `<root>/map`; there is no
        # user-controlled input. The A13-049 defence is the urlparse
        # check that rejects targets carrying a scheme or netloc — this
        # test pins the behaviour by confirming the location header is
        # always a same-origin path.
        r = client.get("/live", follow_redirects=False)
        loc = r.headers["location"]
        assert not loc.startswith("http://")
        assert not loc.startswith("https://")
        assert not loc.startswith("//")


# ---------------------------------------------------------------------------
# Phase 5 (FE-2): Pydantic response_model contracts.
#
# These pin the emitted JSON key sets for the hot endpoints. The contract is
# added with extra="allow" + response_model_exclude_unset=True, which by
# construction emits exactly the handler dict's keys (no silent drops, no
# null-injection). Each test seeds a fully-enriched flight so every optional
# key is actually present, then asserts the exact key set at each level. If a
# future model omits a field (drop) or injects an absent one, these fail.
# ---------------------------------------------------------------------------

def _seed_enriched_flight(conn, *, fid_icao="aabbcc", callsign="LOT123"):
    """Insert a flight with aircraft_db + airports + route enrichment and a
    couple of positions, so every optional response key is populated."""
    fid = insert_flight(conn, icao=fid_icao, callsign=callsign, squawk="1234")
    conn.execute(
        "UPDATE flights SET origin_icao='WAW', dest_icao='LHR', category='A3' WHERE id=?",
        (fid,),
    )
    conn.execute(
        "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
        "VALUES (?,?,?,?,?)",
        (fid_icao, "SP-LRA", "B789", "Boeing 787-9", 0),
    )
    _insert_route(conn, callsign, "WAW", "LHR")
    insert_position(conn, fid, 1_000_000, lat=52.0, lon=21.0, alt_baro=1000,
                    alt_geom=1100, gs=200.0, track=90.0, baro_rate=1500,
                    rssi=-10.0, source_type="adsb_icao")
    insert_position(conn, fid, 1_000_300, lat=52.5, lon=22.0, alt_baro=35000,
                    alt_geom=35100, gs=480.0, track=95.0, baro_rate=-640,
                    rssi=-8.0, source_type="adsb_icao")
    conn.commit()
    return fid


_FLIGHT_META_KEYS = {
    "id", "icao_hex", "callsign", "registration", "aircraft_type", "type_desc",
    "flags", "squawk", "category", "primary_source", "first_seen", "last_seen",
    "duration_sec", "max_alt_baro", "max_gs", "max_distance_nm", "total_positions",
    "adsb_positions", "mlat_positions", "lat_min", "lat_max", "lon_min", "lon_max",
    "origin_icao", "dest_icao", "origin_name", "origin_country", "dest_name",
    "dest_country",
}


class TestResponseContractParity:
    def test_flight_detail_key_parity(self, client, db_conn):
        fid = _seed_enriched_flight(db_conn)
        data = client.get(f"/api/flights/{fid}").json()
        assert set(data) == {
            "flight", "positions", "other_flights", "receiver_lat", "receiver_lon",
        }
        # `flight` carries airline_name on top of the shared flight columns.
        assert set(data["flight"]) == _FLIGHT_META_KEYS | {"airline_name"}
        # other_flights rows must NOT gain an injected `airline_name: null`.
        fid2 = insert_flight(db_conn, icao="aabbcc", first_seen=1_200_000)
        other = client.get(f"/api/flights/{fid2}").json()["other_flights"]
        assert other, "expected a sibling flight"
        assert set(other[0]) == _FLIGHT_META_KEYS
        assert "airline_name" not in other[0]
        # Value-type spot checks: coercion must not mangle these.
        f = data["flight"]
        assert isinstance(f["squawk"], str) and f["squawk"] == "1234"
        assert f["icao_hex"] == "aabbcc"

    def test_flight_detail_positions_empty_by_default(self, client, db_conn):
        fid = _seed_enriched_flight(db_conn)
        assert client.get(f"/api/flights/{fid}").json()["positions"] == []

    def test_flight_positions_key_parity(self, client, db_conn):
        fid = _seed_enriched_flight(db_conn)
        data = client.get(f"/api/flights/{fid}/positions").json()
        assert set(data) == {"total", "limit", "offset", "positions"}
        assert data["total"] == 2
        assert set(data["positions"][0]) == {
            "ts", "lat", "lon", "alt_baro", "alt_geom", "gs", "track",
            "baro_rate", "rssi", "source_type",
        }

    def test_flight_positions_chart_key_parity(self, client, db_conn):
        fid = _seed_enriched_flight(db_conn)
        data = client.get(f"/api/flights/{fid}/positions/chart?target=2000").json()
        assert set(data) == {"total", "target", "positions"}
        # Chart rows omit rssi (must not be injected as null).
        assert set(data["positions"][0]) == {
            "ts", "lat", "lon", "alt_baro", "alt_geom", "gs", "track",
            "baro_rate", "source_type",
        }
        assert "rssi" not in data["positions"][0]

    def test_watchlist_key_parity(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) "
            "VALUES ('icao','aabbcc','My jet', 1700000000)",
        )
        db_conn.commit()
        data = client.get("/api/watchlist").json()
        assert set(data) == {"entries"}
        assert set(data["entries"][0]) == {
            "id", "match_type", "value", "label", "created_at", "airborne",
        }

    def test_map_snapshot_key_parity(self, client, db_conn):
        # `at` must be near now() (endpoint rejects future + pre-history-limit
        # timestamps), so seed recent positions rather than the ancient
        # _seed_enriched_flight defaults.
        now = int(time.time())
        at = now - 60
        fid = insert_flight(db_conn, icao="aabbcc", callsign="LOT123", first_seen=at - 300)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc','SP-LRA','B789','Boeing 787-9',0)",
        )
        _insert_route(db_conn, "LOT123", "WAW", "LHR")
        insert_position(db_conn, fid, at - 200, lat=50.0, lon=10.0,
                        alt_baro=1000, gs=200.0, track=90.0,
                        source_type="adsb_icao")
        insert_position(db_conn, fid, at - 30, lat=51.0, lon=11.0,
                        alt_baro=35000, gs=480.0, track=95.0,
                        source_type="adsb_icao")
        db_conn.commit()
        data = client.get(f"/api/map/snapshot?at={at}&trail=10").json()
        assert set(data) == {"at", "is_live", "receiver_lat", "receiver_lon", "aircraft"}
        assert data["aircraft"], "expected one aircraft in snapshot"
        ac = data["aircraft"][0]
        assert set(ac) == {
            "flight_id", "ts", "lat", "lon", "alt_baro", "gs", "track",
            "source_type", "icao_hex", "callsign", "registration", "aircraft_type",
            "category", "primary_source", "flags", "origin_icao", "dest_icao",
            "seconds_ago", "trail",
        }
        # Trail points stay [lat, lon, ts] with ts an int (no float coercion).
        assert ac["trail"]
        assert len(ac["trail"][0]) == 3
        assert isinstance(ac["trail"][0][2], int)

    def test_stats_key_parity(self, client, db_conn):
        _seed_enriched_flight(db_conn)
        data = client.get("/api/stats").json()
        assert set(data) == {
            "total_flights", "total_positions", "unique_aircraft", "unique_airlines",
            "db_size_bytes", "oldest_flight", "flights_last_24h", "flights_last_7d",
            "source_breakdown", "top_airlines", "top_aircraft_types",
            "hourly_distribution", "daily_unique_aircraft", "altitude_distribution",
            "military_flights", "interesting_flights", "anonymous_flights",
            "squawk_counts", "new_aircraft", "furthest_aircraft", "receiver_lat",
            "receiver_lon", "trends", "previous_window", "lifetime", "heatmap",
            "top_countries", "frequent_aircraft", "top_routes", "top_airports",
            "range",
        }
        assert set(data["source_breakdown"]) == {"adsb", "mlat", "other"}
        assert set(data["trends"]) == {"flights_24h_prev", "flights_7d_prev"}
        assert set(data["lifetime"]) == {
            "total_flights", "total_positions", "unique_aircraft", "unique_airlines",
            "oldest_flight", "db_size_bytes", "source_breakdown",
        }
        assert set(data["new_aircraft"]) == {"total", "items"}
        # Emergency-squawk keys preserved verbatim (non-identifier keys).
        assert set(data["squawk_counts"]) == {"7700", "7600", "7500"}

    def test_stats_filtered_range_and_previous_window(self, client, db_conn):
        _seed_enriched_flight(db_conn)
        # A filtered window surfaces `range` and `previous_window` objects.
        data = client.get("/api/stats?from=900000&to=1100000").json()
        assert data["range"] is not None
        assert set(data["range"]) == {"from", "to"}
        if data["previous_window"] is not None:
            assert set(data["previous_window"]) == {
                "from_ts", "to_ts", "total_flights", "total_positions",
                "unique_aircraft",
            }
