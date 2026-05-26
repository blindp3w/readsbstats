"""
Tests for web.py — pure helpers and API endpoints via FastAPI TestClient.
Uses an in-memory SQLite database injected by patching web._db.
"""

import json
import math
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from readsbstats import config, database, enrichment, photo_sources, web
from readsbstats.photo_sources import PhotoResult


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

def make_db():
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


@pytest.fixture()
def db_conn():
    """Fresh in-memory DB; also clears enrichment caches."""
    conn = make_db()
    enrichment.clear_cache()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    """TestClient with web._db patched to the in-memory connection.
    Default X-Requested-With header makes existing mutating tests pass the
    CSRF check; tests for missing-header rejection construct their own client.
    """
    from readsbstats import route_enricher
    monkeypatch.setattr(web, "_db", db_conn)
    monkeypatch.setattr(route_enricher, "start_background_enricher", lambda: None)
    web._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True,
                    headers={"X-Requested-With": "XMLHttpRequest"}) as c:
        yield c


@pytest.fixture()
def raw_client(db_conn, monkeypatch):
    """TestClient WITHOUT default X-Requested-With — for CSRF rejection tests."""
    from readsbstats import route_enricher
    monkeypatch.setattr(web, "_db", db_conn)
    monkeypatch.setattr(route_enricher, "start_background_enricher", lambda: None)
    web._cache.clear()
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
        b = web._bearing(0.0, 0.0, 1.0, 0.0)
        assert b == pytest.approx(0.0, abs=0.01)

    def test_east(self):
        b = web._bearing(0.0, 0.0, 0.0, 1.0)
        assert b == pytest.approx(90.0, abs=0.01)

    def test_south(self):
        b = web._bearing(0.0, 0.0, -1.0, 0.0)
        assert b == pytest.approx(180.0, abs=0.01)

    def test_west(self):
        b = web._bearing(0.0, 0.0, 0.0, -1.0)
        assert b == pytest.approx(270.0, abs=0.01)

    def test_result_in_0_360_range(self):
        """Bearing is always in [0, 360)."""
        for dlat, dlon in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            b = web._bearing(0.0, 0.0, dlat, dlon)
            assert 0 <= b < 360


class TestHaversineWeb:
    def test_same_point(self):
        assert web._haversine_nm(52.0, 21.0, 52.0, 21.0) == pytest.approx(0.0, abs=1e-9)

    def test_one_degree_latitude(self):
        d = web._haversine_nm(52.0, 21.0, 53.0, 21.0)
        assert 59.8 < d < 60.2


# ---------------------------------------------------------------------------
# _build_flight_filter
# ---------------------------------------------------------------------------

class TestBuildFlightFilter:
    def test_no_params_empty_where(self):
        where, params = web._build_flight_filter(None, None, None, None, None, None, None)
        assert where == ""
        assert params == []

    def test_date_adds_range(self):
        where, params = web._build_flight_filter("2024-01-15", None, None, None, None, None, None)
        assert "first_seen >= ?" in where
        assert "first_seen < ?" in where
        assert len(params) == 2
        # params[1] - params[0] should equal 86400 (one day)
        assert params[1] - params[0] == 86400

    def test_bad_date_raises_400(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            web._build_flight_filter("not-a-date", None, None, None, None, None, None)
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
            _, params = web._build_flight_filter(
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
        where, params = web._build_flight_filter(None, "~AABBCC", None, None, None, None, None)
        assert "icao_hex = ?" in where
        assert params == ["aabbcc"]

    def test_callsign_uppercased_with_wildcard(self):
        where, params = web._build_flight_filter(None, None, "lot", None, None, None, None)
        assert "callsign LIKE ?" in where
        assert params == ["LOT%"]

    def test_registration_uppercased_with_wildcard(self):
        where, params = web._build_flight_filter(None, None, None, "sp-abc", None, None, None)
        assert "LIKE ?" in where
        assert params == ["SP-ABC%"]

    def test_aircraft_type_uppercased(self):
        where, params = web._build_flight_filter(None, None, None, None, "b738", None, None)
        assert params == ["B738"]

    def test_source_filter(self):
        where, params = web._build_flight_filter(None, None, None, None, None, "adsb", None)
        assert "primary_source = ?" in where
        assert params == ["adsb"]

    def test_flags_military(self):
        where, _ = web._build_flight_filter(None, None, None, None, None, None, "military")
        # The flag expression now OR-merges aircraft_db.flags, adsbx_overrides.flags,
        # and the runtime FLAG_ANONYMOUS bit — match on the bitmask test, not the exact SQL.
        assert "COALESCE(adb.flags, 0)" in where
        assert "COALESCE(axo.flags, 0)" in where
        assert "& 1) = 1" in where

    def test_flags_interesting(self):
        where, _ = web._build_flight_filter(None, None, None, None, None, None, "interesting")
        assert "& 2) = 2" in where
        # must exclude aircraft that are also military (flags & 1)
        assert "& 1) = 0" in where

    def test_flags_anonymous(self):
        where, _ = web._build_flight_filter(None, None, None, None, None, None, "anonymous")
        # FLAG_ANONYMOUS=16 set, military/interesting bits cleared
        assert "& 16) = 16" in where
        assert "& 3) = 0" in where

    def test_squawk_filter(self):
        where, params = web._build_flight_filter(None, None, None, None, None, None, None, squawk="7700")
        assert "squawk = ?" in where
        assert "7700" in params

    def test_multiple_filters_uses_and(self):
        where, params = web._build_flight_filter(None, "aabbcc", "LOT", None, None, None, None)
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
        for col in web._SORT_COLS:
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
        assert lines[0] == ",".join(web._CSV_COLS)

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

    def test_found_includes_positions_list(self, client, db_conn):
        fid = insert_flight(db_conn)
        # Insert a position row
        db_conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (fid, 1_000_001, 52.0, 21.0, "adsb_icao"),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}")
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
        db_conn.executemany(
            "INSERT INTO positions "
            "(flight_id, ts, lat, lon, alt_baro, gs, source_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (fid, 1_000_000 + i, 52.0 + i * 0.0001, 21.0 + i * 0.0001,
                 1000 + i, 200.0 + (i % 50), "adsb_icao")
                for i in range(n)
            ],
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

    def test_legacy_detail_still_embeds_all_positions(self, client, db_conn):
        """Backward compat: /api/flights/{id} keeps its embedded
        `positions` list at full size. The new endpoints are additive,
        not replacing."""
        fid = self._seed(db_conn, 200)
        r = client.get(f"/api/flights/{fid}")
        assert r.status_code == 200
        assert len(r.json()["positions"]) == 200


# ---------------------------------------------------------------------------
# API: /api/health
# ---------------------------------------------------------------------------

class TestApiHealth:
    def test_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_degraded_when_db_raises(self, client, monkeypatch):
        def bad_db():
            raise RuntimeError("disk error")
        monkeypatch.setattr(web, "db", bad_db)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

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
    def test_empty_db_returns_warn(self, client):
        r = client.get("/api/metrics/health")
        assert r.status_code == 200
        body = r.json()
        assert body["overall"] == "warn"
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


# ---------------------------------------------------------------------------
# Helper: _fmt_ts
# ---------------------------------------------------------------------------

class TestFmtTs:
    def test_none_returns_empty_string(self):
        assert web._fmt_ts(None) == ""

    def test_epoch_formats_utc(self):
        result = web._fmt_ts(0)
        assert result == "1970-01-01 00:00"


# ---------------------------------------------------------------------------
# Helper: _get_cache / _set_cache
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def clear_web_cache():
    web._cache.clear()
    yield
    web._cache.clear()


class TestCache:
    def test_miss_returns_none(self, clear_web_cache):
        assert web._get_cache("no_such_key") is None

    def test_hit_returns_value(self, clear_web_cache):
        web._set_cache("foo", {"x": 1})
        assert web._get_cache("foo") == {"x": 1}

    def test_expired_entry_returns_none(self, clear_web_cache):
        # Plant an entry with a timestamp far in the past
        web._cache["bar"] = (time.time() - web._DEFAULT_TTL - 1, {"x": 2})
        assert web._get_cache("bar") is None

    def test_set_cache_evicts_oldest_over_cap(self, clear_web_cache):
        """Audit 2026-05-25: filtered /api/stats requests with caller-controlled
        from/to range produce unbounded distinct keys. The cache must cap the
        total entry count and evict the oldest first."""
        cap = web._CACHE_MAX_ENTRIES
        for i in range(cap + 50):
            web._set_cache(f"stats:0:{i}", i)
        assert len(web._cache) <= cap
        # The earliest keys should have been evicted; the most recent kept.
        assert web._get_cache(f"stats:0:{cap + 49}") == cap + 49
        assert web._get_cache("stats:0:0") is None

    def test_get_cache_drops_expired_entries(self, clear_web_cache, monkeypatch):
        """Expired entries should be removed lazily on lookup so the cap is
        not consumed by zombie keys."""
        web._set_cache("zombie", "ignored")
        # Fast-forward past the default TTL.
        future = time.time() + web._DEFAULT_TTL + 5
        monkeypatch.setattr(web.time, "time", lambda: future)
        assert web._get_cache("zombie") is None
        assert "zombie" not in web._cache


class TestDbConnection:
    """`db()` must be per-thread in production so requests don't serialize on
    Python's per-connection sqlite mutex.  Tests that set `web._db` directly
    must still see that connection from every thread (for in-memory DBs)."""

    def test_test_override_shared_across_threads(self, db_conn, monkeypatch):
        import threading as _t
        monkeypatch.setattr(web, "_db", db_conn)
        seen: list[object] = []
        def fetch():
            seen.append(web.db())
        threads = [_t.Thread(target=fetch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(c is db_conn for c in seen)

    def test_per_thread_connection_when_no_override(self, tmp_path, monkeypatch):
        import threading as _t
        monkeypatch.setattr(web, "_db", None)
        monkeypatch.setattr(web, "_thread_local", _t.local())
        db_path = str(tmp_path / "perthread.db")
        database.init_db(db_path)
        original_connect = database.connect
        monkeypatch.setattr(web.database, "connect",
                            lambda path=db_path: original_connect(db_path))
        seen: list[object] = []
        lock = _t.Lock()
        def fetch():
            conn = web.db()
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
        monkeypatch.setattr(web, "_db", None)
        monkeypatch.setattr(web, "_thread_local", _t.local())
        db_path = str(tmp_path / "samethread.db")
        database.init_db(db_path)
        original_connect = database.connect
        monkeypatch.setattr(web.database, "connect",
                            lambda path=db_path: original_connect(db_path))
        first = web.db()
        second = web.db()
        assert first is second
        first.close()


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
        meta = web_mod._settings_metadata(stub, list(config._META_REGISTRY.keys()))
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
        meta = web_mod._settings_metadata(stub, list(config._META_REGISTRY.keys()))
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
        web_mod._cache.clear()
        r2 = client.get("/api/settings")
        assert r2.json()["_metadata"]["telegram_token"]["customized"] is True


class TestApiFeeders:
    def test_api_feeders_returns_json(self, client, monkeypatch):
        async def mock_feeders():
            return [{"name": "readsb", "unit": "readsb.service",
                     "systemd": "active", "overall": "ok"}]
        monkeypatch.setattr(web, "_check_all_feeders", mock_feeders)
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

    def test_check_systemd_unit_active(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            class Proc:
                async def communicate(self):
                    return (b"active\n", b"")
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(web._check_systemd_unit("test.service"))
        assert result["systemd"] == "active"

    def test_check_systemd_unit_not_found(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise FileNotFoundError("systemctl not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(web._check_systemd_unit("test.service"))
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
            web._check_systemd_unit("test.service")
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
            web._feeder_details_mlat("test.service")
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
        result = asyncio.get_event_loop().run_until_complete(web._check_port(30005))
        assert result["port_status"] == "open"
        assert result["port"] == 30005

    def test_check_port_closed(self, monkeypatch):
        import asyncio

        async def mock_connect(host, port):
            raise ConnectionRefusedError()

        monkeypatch.setattr(asyncio, "open_connection", mock_connect)
        result = asyncio.get_event_loop().run_until_complete(web._check_port(30005))
        assert result["port_status"] == "closed"

    def test_check_single_feeder_ok(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "active"}

        async def mock_port(port, host="127.0.0.1"):
            return {"port": port, "port_status": "open"}

        monkeypatch.setattr(web, "_check_systemd_unit", mock_systemd)
        monkeypatch.setattr(web, "_check_port", mock_port)
        feeder = {"name": "readsb", "unit": "readsb.service", "port": 30005}
        result = asyncio.get_event_loop().run_until_complete(web._check_single_feeder(feeder))
        assert result["overall"] == "ok"
        assert result["systemd"] == "active"
        assert result["port_status"] == "open"

    def test_check_single_feeder_error(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "inactive"}

        monkeypatch.setattr(web, "_check_systemd_unit", mock_systemd)
        feeder = {"name": "test", "unit": "test.service"}
        result = asyncio.get_event_loop().run_until_complete(web._check_single_feeder(feeder))
        assert result["overall"] == "error"

    def test_check_single_feeder_unavailable(self, monkeypatch):
        import asyncio

        async def mock_systemd(unit):
            return {"systemd": "unavailable"}

        monkeypatch.setattr(web, "_check_systemd_unit", mock_systemd)
        feeder = {"name": "test", "unit": "test.service"}
        result = asyncio.get_event_loop().run_until_complete(web._check_single_feeder(feeder))
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
        details = web._feeder_details_readsb(status_path)
        labels = {k for k, _ in details}
        assert "Aircraft tracked" in labels
        assert "Messages/s" in labels
        assert "Signal" in labels
        assert "Max range" in labels
        assert any(v == "2" for _, v in details if _ == "Aircraft tracked")

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
        details = web._feeder_details_readsb(str(tmp_path))
        max_range = next((v for k, v in details if k == "Max range"), None)
        assert max_range == "100.0", f"expected '100.0', got {max_range!r}"

    def test_readsb_details_missing_files(self, tmp_path):
        details = web._feeder_details_readsb(str(tmp_path))
        assert details == []

    def test_piaware_details_from_json(self, tmp_path):
        path = tmp_path / "status.json"
        path.write_text(json.dumps({
            "piaware_version": "9.0",
            "piaware": {"status": "running"},
            "radio": {"message": "Mode S enabled"},
            "cpu_temp_celcius": 52.3,
        }))
        details = web._feeder_details_piaware(str(path))
        labels = {k for k, _ in details}
        assert "Version" in labels
        assert "Piaware" in labels
        assert "Radio" in labels
        assert "CPU temp" in labels

    def test_piaware_details_missing_file(self, tmp_path):
        details = web._feeder_details_piaware(str(tmp_path / "missing.json"))
        assert details == []

    def test_read_json_file_valid(self, tmp_path):
        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        assert web._read_json_file(str(p)) == {"key": "value"}

    def test_read_json_file_invalid(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{broken")
        assert web._read_json_file(str(p)) is None

    def test_read_json_file_missing(self):
        assert web._read_json_file("/nonexistent/path.json") is None

    def test_check_port_timeout(self, monkeypatch):
        import asyncio

        async def mock_connect(host, port):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "open_connection", mock_connect)
        result = asyncio.get_event_loop().run_until_complete(web._check_port(30005))
        # asyncio.TimeoutError is a subclass of OSError in Python 3.11+,
        # so it's caught as "closed" rather than "timeout"
        assert result["port_status"] in ("timeout", "closed")

    def test_check_systemd_timeout(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(web._check_systemd_unit("test.service"))
        assert result["systemd"] == "timeout"

    def test_check_systemd_generic_error(self, monkeypatch):
        import asyncio

        async def mock_exec(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
        result = asyncio.get_event_loop().run_until_complete(web._check_systemd_unit("test.service"))
        assert result["systemd"].startswith("error:")

    def test_fetch_feeder_details_readsb_dispatch(self, monkeypatch, tmp_path):
        import asyncio
        status_path = str(tmp_path)
        (tmp_path / "aircraft.json").write_text('{"aircraft": []}')
        # Bypass the /run/ allowlist so the dispatcher reaches the real fetcher.
        monkeypatch.setattr(web, "_is_safe_status_path", lambda _p: True)
        feeder = {"name": "readsb", "unit": "readsb.service", "status_type": "readsb", "status_path": status_path}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert isinstance(result, list)

    def test_fetch_feeder_details_unknown_type(self):
        import asyncio
        feeder = {"name": "x", "unit": "x.service", "status_type": "unknown"}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert result == []

    def test_fr24_details_success(self, monkeypatch):
        import asyncio
        import httpx as _httpx

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {
                    "build_version": "1.2.3",
                    "feed_status": "connected",
                    "feed_alias": "T-KZXX1",
                    "feed_num_ac_tracked": 42,
                    "rx_connected": "1",
                    "mlat-ok": "0",
                }

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url): return FakeResp()

        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: FakeClient())
        result = asyncio.get_event_loop().run_until_complete(
            web._feeder_details_fr24("http://localhost/monitor.json")
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
            async def get(self, url): raise ConnectionError("down")

        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: FakeClient())
        result = asyncio.get_event_loop().run_until_complete(
            web._feeder_details_fr24("http://localhost/monitor.json")
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
            web._feeder_details_mlat("test-mlat.service")
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
            web._feeder_details_mlat("test-mlat.service")
        )
        assert result == []

    def test_fetch_feeder_details_fr24_dispatch(self, monkeypatch):
        import asyncio

        async def fake_fr24(url):
            return [("Version", "1.0")]

        monkeypatch.setattr(web, "_feeder_details_fr24", fake_fr24)
        # Loopback URL passes the SSRF allowlist.
        feeder = {"name": "fr24", "unit": "fr24.service", "status_type": "fr24",
                  "status_url": "http://127.0.0.1:8754/monitor.json"}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert result == [("Version", "1.0")]

    def test_fetch_feeder_details_piaware_dispatch(self, monkeypatch, tmp_path):
        import asyncio
        path = str(tmp_path / "status.json")
        (tmp_path / "status.json").write_text('{"piaware_version": "9"}')
        # Bypass the /run/ allowlist so the dispatcher reaches the real fetcher.
        monkeypatch.setattr(web, "_is_safe_status_path", lambda _p: True)
        feeder = {"name": "piaware", "unit": "piaware.service", "status_type": "piaware", "status_path": path}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert any(k == "Version" for k, _ in result)

    def test_fetch_feeder_details_mlat_dispatch(self, monkeypatch):
        import asyncio

        async def fake_mlat(unit):
            return [("Peers", "10")]

        monkeypatch.setattr(web, "_feeder_details_mlat", fake_mlat)
        feeder = {"name": "mlat", "unit": "mlat.service", "status_type": "mlat"}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert result == [("Peers", "10")]

    def test_check_all_feeders(self, monkeypatch):
        import asyncio

        async def mock_single(feeder):
            return {"name": feeder["name"], "overall": "ok"}

        monkeypatch.setattr(web, "_check_single_feeder", mock_single)
        monkeypatch.setattr(config, "FEEDERS", [{"name": "a", "unit": "a.service"}, {"name": "b", "unit": "b.service"}])
        result = asyncio.get_event_loop().run_until_complete(web._check_all_feeders())
        assert len(result) == 2
        assert result[0]["name"] == "a"

    # ---------- status_path / status_url allowlist (defence-in-depth) ----------

    def test_is_safe_status_path_accepts_run_subdir(self):
        assert web._is_safe_status_path("/run/readsb")
        assert web._is_safe_status_path("/run/piaware/status.json")
        assert web._is_safe_status_path("/run")

    def test_is_safe_status_path_rejects_traversal(self):
        assert not web._is_safe_status_path("/run/../etc/hostname")
        assert not web._is_safe_status_path("/etc/passwd")
        assert not web._is_safe_status_path("/")
        assert not web._is_safe_status_path("/runaway/x")  # prefix-only match must require /

    def test_is_safe_status_path_rejects_empty_and_bad_types(self):
        assert not web._is_safe_status_path("")
        assert not web._is_safe_status_path(None)  # type: ignore[arg-type]

    def test_is_safe_status_path_honours_env_override(self, tmp_path, monkeypatch):
        # improvements.md #136: tests should be able to set the root via
        # config.FEEDER_STATUS_ROOT (backed by RSBS_FEEDER_STATUS_ROOT)
        # rather than depending on the production /run path.
        sub = tmp_path / "readsb"
        sub.mkdir()
        monkeypatch.setattr(config, "FEEDER_STATUS_ROOT", str(tmp_path))
        assert web._is_safe_status_path(str(sub / "stats.json"))
        assert web._is_safe_status_path(str(tmp_path))
        # /run is no longer the root, so a real /run path is now rejected
        assert not web._is_safe_status_path("/run/readsb/stats.json")

    def test_is_safe_status_url_accepts_loopback_http(self):
        assert web._is_safe_status_url("http://127.0.0.1:8754/monitor.json")
        assert web._is_safe_status_url("http://localhost:8754/")
        assert web._is_safe_status_url("http://[::1]:8754/")

    def test_is_safe_status_url_rejects_external_hosts(self):
        assert not web._is_safe_status_url("http://169.254.169.254/latest/meta-data/")
        assert not web._is_safe_status_url("http://example.com/")
        assert not web._is_safe_status_url("http://10.0.0.1/")

    def test_is_safe_status_url_rejects_non_http_schemes(self):
        # https on loopback is fine in principle but we keep the allowlist
        # tight: feeders all expose plain http on loopback by design.
        assert not web._is_safe_status_url("https://127.0.0.1/")
        assert not web._is_safe_status_url("file:///etc/passwd")
        assert not web._is_safe_status_url("ftp://127.0.0.1/")

    def test_is_safe_status_url_rejects_empty_and_bad(self):
        assert not web._is_safe_status_url("")
        assert not web._is_safe_status_url(None)  # type: ignore[arg-type]
        assert not web._is_safe_status_url("not a url")

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

        monkeypatch.setattr(web, "_feeder_details_readsb", boom)
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
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

        monkeypatch.setattr(web, "_feeder_details_fr24", boom)
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
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
        web._cache.pop("polar", None)
        r = client.get("/api/stats/polar")
        assert r.status_code == 200
        assert len(r.json()["buckets"]) == 36

    def test_buckets_have_bearing_and_dist(self, client):
        web._cache.pop("polar", None)
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
        web._cache.clear()
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
            web._cache.clear()
            r = client.get("/api/dates")
            assert r.status_code == 200
            dates = r.json()["dates"]
            counts = {d["date"]: d["flight_count"] for d in dates}
            # Both flights should group under 2024-01-15 in Warsaw-local time.
            assert counts == {"2024-01-15": 2}
        finally:
            web._cache.clear()
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
# API: /api/stats/polar — cache hit + position data path
# ---------------------------------------------------------------------------

class TestApiStatsPolarCacheAndData:
    def test_second_call_hits_cache(self, client, db_conn):
        web._cache.pop("polar", None)
        r1 = client.get("/api/stats/polar")
        r2 = client.get("/api/stats/polar")
        assert r1.json() == r2.json()

    def test_with_flights_fills_buckets(self, client, db_conn):
        web._cache.pop("polar", None)
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

    def test_photo_returned_and_stored(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://example.com/thumb.jpg",
            large_url="https://example.com/large.jpg",
            link_url="https://example.com/photo",
            photographer="Alice",
        ))
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://example.com/thumb.jpg"
        assert data["photographer"] == "Alice"
        assert data["icao_hex"] == "aabbcc"
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["thumbnail_url"] == "https://example.com/thumb.jpg"

    def test_cached_photo_served_from_db(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://cached.com/t.jpg", None, None, "Bob", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json()["thumbnail_url"] == "https://cached.com/t.jpg"
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
            thumbnail_url="https://ad.com/t.jpg",
            large_url="https://ad.com/t.jpg",
            link_url="https://ad.com/p",
            photographer="Charlie",
        ))
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json()["thumbnail_url"] == "https://ad.com/t.jpg"
        assert r.json()["photographer"] == "Charlie"
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://ad.com/t.jpg"

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
            thumbnail_url="https://ad.com/t.jpg",
            large_url="https://ad.com/t.jpg",
            link_url="https://ad.com/p",
            photographer="Y",
        ))
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json()["thumbnail_url"] == "https://ad.com/t.jpg"

    def test_hexdb_result_stored_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://hexdb.io/img/AABBCC.jpg",
        ))
        client.get(f"/api/flights/{fid}/photo")
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://hexdb.io/img/AABBCC.jpg"


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
        web._cache.clear()

        r = client.get("/api/airspace")
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        assert len(d["features"]) == 1
        assert d["features"][0]["properties"]["name"] == "TEST CTR"

    def test_missing_file_returns_empty_collection(self, client, monkeypatch):
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "/nonexistent/path.geojson")
        web._cache.clear()

        r = client.get("/api/airspace")
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        assert d["features"] == []

    def test_uses_bundled_file_when_config_empty(self, client, monkeypatch):
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "")
        web._cache.clear()

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
        web._cache.clear()

        r1 = client.get("/api/airspace")
        r2 = client.get("/api/airspace")
        assert r1.json() == r2.json()

    def test_non_regular_file_returns_empty_collection(self, client, monkeypatch):
        # improvements.md #73: a path that resolves but isn't a regular file
        # (device, FIFO, directory) must be rejected with an empty result.
        # /dev/null is portable and definitely not a regular file.
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", "/dev/null")
        web._cache.clear()
        r = client.get("/api/airspace")
        assert r.status_code == 200
        assert r.json() == {"type": "FeatureCollection", "features": []}

    def test_directory_path_returns_empty_collection(self, client, monkeypatch, tmp_path):
        # A path that exists but is a directory, not a file.
        monkeypatch.setattr(config, "AIRSPACE_GEOJSON", str(tmp_path))
        web._cache.clear()
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

    def test_sort_by_last_seen_desc_default(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        _insert_aircraft_db(db_conn, "112233", flags=1)
        insert_flight(db_conn, icao="aabbcc", first_seen=1_000_000, last_seen=1_003_600)
        insert_flight(db_conn, icao="112233", first_seen=2_000_000, last_seen=2_003_600)
        r = client.get("/api/aircraft/flagged")
        aircraft = r.json()["aircraft"]
        assert aircraft[0]["icao_hex"] == "112233"
        assert aircraft[1]["icao_hex"] == "aabbcc"

    def test_includes_photo_data(self, client, db_conn):
        _insert_aircraft_db(db_conn, "aabbcc", flags=1)
        insert_flight(db_conn, icao="aabbcc")
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://t.jpg", "https://l.jpg", "https://link", "Bob", int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/aircraft/flagged")
        ac = r.json()["aircraft"][0]
        assert ac["thumbnail_url"] == "https://t.jpg"
        assert ac["photographer"] == "Bob"


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
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (flight_id, ts, lat, lon, "adsb_icao"),
        )
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
        db_conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (fid, int(time.time()), None, None, "adsb_icao"),
        )
        db_conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (fid, int(time.time()), 52.10, None, "adsb_icao"),
        )
        db_conn.commit()
        r = client.get("/api/map/heatmap?window=all")
        assert r.status_code == 200
        assert r.json()["points"] == []
        assert r.json()["count"] == 0

    def test_response_includes_window_field(self, client, db_conn):
        fid = insert_flight(db_conn)
        self._insert_position(db_conn, lat=52.10, lon=21.00, flight_id=fid)
        for win in ("24h", "7d", "30d", "all"):
            web._cache.clear()
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
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (flight_id, ts, lat, lon, "adsb_icao"),
        )
        conn.commit()
        return flight_id

    def test_empty_db_returns_36_point_polygon(self, client):
        web._cache.clear()
        r = client.get("/api/map/coverage")
        assert r.status_code == 200
        assert len(r.json()["polygon"]) == 36

    def test_empty_db_all_points_at_receiver(self, client):
        web._cache.clear()
        r = client.get("/api/map/coverage")
        for pt in r.json()["polygon"]:
            assert pt[0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)
            assert pt[1] == pytest.approx(config.RECEIVER_LON, abs=1e-6)

    def test_empty_db_max_range_is_zero(self, client):
        web._cache.clear()
        r = client.get("/api/map/coverage")
        assert r.json()["max_range_nm"] == pytest.approx(0.0)

    def test_position_in_bucket_0_projects_correctly(self, client, db_conn):
        """Position at bearing 5° → bucket 0 → polygon vertex at bearing 0°, same distance."""
        web._cache.clear()
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
        web._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=15.0, dist_nm=100.0)  # bucket 1
        data = client.get("/api/map/coverage?window=all").json()
        assert data["polygon"][0][0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)
        assert data["polygon"][0][1] == pytest.approx(config.RECEIVER_LON, abs=1e-6)

    def test_max_range_nm_is_maximum_across_buckets(self, client, db_conn):
        web._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0)
        self._insert_position_at(db_conn, bearing_deg=95.0, dist_nm=200.0)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(200.0)

    def test_bucket_uses_max_distance(self, client, db_conn):
        """Two positions both in bucket 0 — polygon uses the farther one."""
        web._cache.clear()
        fid = insert_flight(db_conn)
        self._insert_position_at(db_conn, bearing_deg=2.0, dist_nm=100.0, flight_id=fid)
        self._insert_position_at(db_conn, bearing_deg=8.0, dist_nm=150.0, flight_id=fid)
        from readsbstats import geo as _geo
        exp_lat, _ = _geo.destination_point(config.RECEIVER_LAT, config.RECEIVER_LON, 0.0, 150.0)
        assert client.get("/api/map/coverage?window=all").json()["polygon"][0][0] == pytest.approx(exp_lat, abs=0.01)

    def test_window_24h_excludes_old_position(self, client, db_conn):
        web._cache.clear()
        now = int(time.time())
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 2 * 86400)
        data = client.get("/api/map/coverage?window=24h").json()
        for pt in data["polygon"]:
            assert pt[0] == pytest.approx(config.RECEIVER_LAT, abs=1e-6)

    def test_window_all_includes_old_position(self, client, db_conn):
        web._cache.clear()
        now = int(time.time())
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 90 * 86400)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(100.0, rel=0.01)

    def test_window_filter_uses_position_ts_not_flight_dates(self, client, db_conn):
        """A position with recent ts is included in 24h even if its flight started long ago."""
        web._cache.clear()
        now = int(time.time())
        fid = insert_flight(db_conn, first_seen=now - 30 * 3600, last_seen=now - 29 * 3600)
        # Position recorded 10 min ago — within 24h window
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=120.0, ts=now - 600, flight_id=fid)
        assert client.get("/api/map/coverage?window=24h").json()["max_range_nm"] == pytest.approx(120.0, rel=0.01)

    def test_position_near_360_goes_to_bucket_35(self, client, db_conn):
        """A position at bearing ~355° should land in bucket 35, not overflow."""
        web._cache.clear()
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
            web._cache.clear()
            assert client.get(f"/api/map/coverage?window={win}").json()["window"] == win

    def test_result_is_cached(self, client, db_conn):
        web._cache.clear()
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0)
        max1 = client.get("/api/map/coverage?window=all").json()["max_range_nm"]
        self._insert_position_at(db_conn, bearing_deg=95.0, dist_nm=300.0)
        assert client.get("/api/map/coverage?window=all").json()["max_range_nm"] == pytest.approx(max1)

    def test_different_windows_independent_cache_keys(self, client, db_conn):
        now = int(time.time())
        fid = insert_flight(db_conn)
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=100.0, ts=now - 100, flight_id=fid)
        self._insert_position_at(db_conn, bearing_deg=5.0, dist_nm=200.0, ts=now - 10 * 86400, flight_id=fid)
        web._cache.clear()
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
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
            (flight_id, ts, lat, lon, "adsb_icao"),
        )
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
        db_conn.execute(
            "INSERT INTO positions (flight_id,ts,lat,lon,source_type) VALUES (?,?,?,?,?)",
            (fid, now, 52.13, 21.04, "adsb_icao"),
        )
        db_conn.commit()

        web._cache.clear()
        assert web._get_cache("heatmap:24h") is None
        web._prewarm_one("heatmap", "24h")
        cached = web._get_cache("heatmap:24h")
        assert cached is not None
        assert cached["count"] == 1

        assert web._get_cache("coverage:24h") is None
        web._prewarm_one("coverage", "24h")
        cached = web._get_cache("coverage:24h")
        assert cached is not None
        assert len(cached["polygon"]) == 36

    def test_type_lock_evicts_oldest_beyond_cap(self):
        """Audit-12 #150 — _type_fetch_locks used to grow unboundedly.
        Now LRU-capped at _TYPE_LOCKS_MAX entries."""
        # Reset to a clean state for the test
        web._type_fetch_locks.clear()
        cap = web._TYPE_LOCKS_MAX
        # Fill past the cap
        for i in range(cap + 5):
            web._type_lock(f"T{i:04d}")
        # Total entries respects the cap
        assert len(web._type_fetch_locks) <= cap
        # Oldest entries were evicted — first inserted key no longer present
        assert "T0000" not in web._type_fetch_locks
        # Most-recently-touched key still present
        assert f"T{cap + 4:04d}" in web._type_fetch_locks

    def test_type_lock_returns_same_lock_for_same_key(self):
        """Sanity — eviction must not break the 'one lock per type' contract
        for keys still under the cap."""
        web._type_fetch_locks.clear()
        a1 = web._type_lock("A320")
        a2 = web._type_lock("A320")
        assert a1 is a2

    def test_prewarm_loop_survives_one_prewarm_raising(self, monkeypatch):
        """Audit-12 #211 — a single _prewarm_one() exception must NOT kill
        the daemon thread. The loop catches the exception, schedules a
        5-minute backoff for that target, and continues with the next
        target. Without this we'd lose all cache refresh after the first
        transient compute failure."""
        # Drive a controlled finite loop: clear the stop event up-front,
        # set it inside the second `_prewarm_one` call to break out cleanly.
        web._prewarmer_stop.clear()
        web._cache.clear()

        call_log: list[tuple[str, str]] = []

        def fake_prewarm(kind, window):
            call_log.append((kind, window))
            if len(call_log) == 1:
                raise RuntimeError("simulated prewarm compute failure")
            if len(call_log) == 2:
                # Stop the loop on the second call so the test terminates.
                web._prewarmer_stop.set()

        monkeypatch.setattr(web, "_prewarm_one", fake_prewarm)
        # Skip the initial 5s wait so the loop starts immediately.
        # Skip the inter-iteration 10s cool-off too. Both wait()s must
        # return False (timed out) so the loop body runs; the explicit
        # stop.set() inside fake_prewarm is what exits the loop.
        wait_calls = {"n": 0}
        original_wait = web._prewarmer_stop.wait

        def fast_wait(timeout=None):
            wait_calls["n"] += 1
            # Honour the actual event state — when fake_prewarm sets the
            # event, wait() returns True and the loop exits.
            return web._prewarmer_stop.is_set()

        monkeypatch.setattr(web._prewarmer_stop, "wait", fast_wait)
        # Also stub time.time so all targets are "due" immediately on the
        # very first iteration (the staggered schedule otherwise inserts
        # 15s waits between iterations and our wait stub bails out before
        # any prewarm runs).
        import time as _time
        monkeypatch.setattr(
            web, "_initial_prewarm_schedule",
            lambda targets, now: {t: now for t in targets},
        )
        monkeypatch.setattr(_time, "time", lambda: 1_000_000.0)

        # Should not raise — exception is caught inside the loop.
        web._prewarm_loop()

        # Loop executed at least 2 iterations (one raising, one stopping).
        assert len(call_log) >= 2, (
            f"loop terminated after only {len(call_log)} call(s); the first "
            "exception was likely not caught"
        )
        # Clean up so other tests don't see a set stop event.
        web._prewarmer_stop.clear()
        # Restore the wait method ref (monkeypatch handles undo, but be explicit)
        _ = original_wait

    def test_initial_prewarm_schedule_staggers_targets(self):
        """Regression for audit-12 #185 — the prewarmer used to start all 8
        targets at next_at=0.0, causing 8 back-to-back full-table scans
        across the first ~80s of process startup. The staggered schedule
        spreads the first refreshes apart and prioritises the shortest-TTL
        windows (most-used) first."""
        now = 1_000_000.0
        schedule = web._initial_prewarm_schedule(web._PREWARM_TARGETS, now=now)

        # Every target has an entry
        for target in web._PREWARM_TARGETS:
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
        ranked = sorted(web._PREWARM_TARGETS, key=lambda t: schedule[t])
        early = ranked[: len(ranked) // 2]
        assert ("heatmap", "24h") in early
        assert ("coverage", "24h") in early


class TestApiAircraftPhoto:
    def test_cached_photo_returned(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://t.jpg", "https://l.jpg", "https://link", "Alice", int(time.time())),
        )
        db_conn.commit()
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://t.jpg"

    def test_no_photo_returns_null(self, client, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_network_error_returns_null(self, client, monkeypatch):
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_specific_photo_annotated_with_is_type_photo_false(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,?,?,?,?)",
            ("aabbcc", "https://t.jpg", "https://l.jpg", "https://link", "Alice", int(time.time())),
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
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo(None))
        assert result is None

    def test_empty_type_code_returns_none(self, client, db_conn):
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo(""))
        assert result is None

    def test_type_photos_cache_hit(self, client, db_conn):
        self._seed_type_photo(db_conn, "B738", "https://example.com/b738.jpg")
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo("B738"))
        assert result is not None
        assert result["thumbnail_url"] == "https://example.com/b738.jpg"

    def test_type_photos_negative_cache_returns_none(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('B738', NULL, NULL, NULL, NULL, ?)", (int(time.time()),)
        )
        db_conn.commit()
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo("B738"))
        assert result is None

    def test_db_join_reuses_cached_photo(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "aabbcc", "B738")
        self._seed_specific_photo(db_conn, "aabbcc", "https://example.com/cached.jpg")
        fetch_calls = []
        monkeypatch.setattr(photo_sources, "fetch_photo",
                            lambda icao: fetch_calls.append(icao) or None)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo("B738"))
        assert result is not None
        assert result["thumbnail_url"] == "https://example.com/cached.jpg"
        assert fetch_calls == []

    def test_probe_planespotters_success(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "probe01", "EF2K", "Eurofighter Typhoon")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: PhotoResult(
            thumbnail_url="https://example.com/ef2k.jpg",
            large_url="https://example.com/ef2k_l.jpg",
            link_url=None,
            photographer="Alice",
        ))
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo("EF2K"))
        assert result is not None
        assert result["thumbnail_url"] == "https://example.com/ef2k.jpg"
        row = db_conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert row and row[0] == "https://example.com/ef2k.jpg"

    def test_all_fail_stores_negative(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "probe01", "EF2K")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(web._fetch_type_photo("EF2K"))
        assert result is None
        row = db_conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert row is not None
        assert row[0] is None

    def test_flight_photo_endpoint_falls_back_to_type(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        self._seed_aircraft_db(db_conn, "aabbcc", "B738")
        self._seed_type_photo(db_conn, "B738", "https://example.com/b738.jpg")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://example.com/b738.jpg"
        assert data["is_type_photo"] is True
        assert data["type_code"] == "B738"

    def test_aircraft_photo_endpoint_falls_back_to_type(self, client, db_conn, monkeypatch):
        self._seed_aircraft_db(db_conn, "aabbcc", "B738", "Boeing 737-800")
        self._seed_type_photo(db_conn, "B738", "https://example.com/b738.jpg")
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda icao: None)
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://example.com/b738.jpg"
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
        self._seed_type_photo(db_conn, "B738", "https://example.com/b738.jpg")
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        ac = r.json()["aircraft"]
        assert len(ac) == 1
        assert ac[0]["thumbnail_url"] == "https://example.com/b738.jpg"
        assert ac[0]["is_type_photo"] is True

    def test_flagged_endpoint_specific_photo_not_type(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'SP-ABC', 'B738', 'Boeing 737-800', 1)"
        )
        db_conn.execute(
            "INSERT INTO photos VALUES (?,?,NULL,NULL,NULL,?)",
            ("aabbcc", "https://example.com/specific.jpg", int(time.time()))
        )
        self._seed_type_photo(db_conn, "B738", "https://example.com/b738.jpg")
        db_conn.commit()
        insert_flight(db_conn, icao="aabbcc", aircraft_type="B738")
        r = client.get("/api/aircraft/flagged")
        assert r.status_code == 200
        ac = r.json()["aircraft"]
        assert ac[0]["thumbnail_url"] == "https://example.com/specific.jpg"
        assert ac[0]["is_type_photo"] is False
