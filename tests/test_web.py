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

from readsbstats import config, database, enrichment, web


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
                    headers={"X-Requested-With": "tests"}) as c:
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
        assert "((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 1" in where

    def test_flags_interesting(self):
        where, _ = web._build_flight_filter(None, None, None, None, None, None, "interesting")
        assert "((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 2) = 2" in where
        # must exclude aircraft that are also military (flags & 1)
        assert "((COALESCE(adb.flags, 0) | COALESCE(axo.flags, 0)) & 1) = 0" in where

    def test_squawk_filter(self):
        where, params = web._build_flight_filter(None, None, None, None, None, None, None, squawk="7700")
        assert "squawk = ?" in where
        assert "7700" in params

    def test_multiple_filters_uses_and(self):
        where, params = web._build_flight_filter(None, "aabbcc", "LOT", None, None, None, None)
        assert " AND " in where
        assert len(params) == 2


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


# ---------------------------------------------------------------------------
# HTML page routes
# ---------------------------------------------------------------------------

class TestPageRoutes:
    def test_stats_page_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_history_page_returns_html(self, client):
        r = client.get("/history")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_flight_page_returns_html(self, client, db_conn):
        fid = insert_flight(db_conn)
        r = client.get(f"/flight/{fid}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_aircraft_page_returns_html(self, client):
        r = client.get("/aircraft/aabbcc")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_live_page_returns_html(self, client):
        r = client.get("/live")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_settings_page_returns_html(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_settings_page_masks_telegram_token(self, client):
        r = client.get("/settings")
        from readsbstats import config
        if config.TELEGRAM_TOKEN:
            assert config.TELEGRAM_TOKEN not in r.text

    def test_settings_page_does_not_leak_db_directory(self, client):
        # The full DB path leaks filesystem layout (e.g. /mnt/ext/...).
        # The settings page should display only the basename, not the parent dir.
        from readsbstats import config
        import os
        r = client.get("/settings")
        parent = os.path.dirname(os.path.abspath(config.DB_PATH))
        # An empty parent means DB_PATH was a bare filename — nothing to leak.
        if parent and parent != "/":
            assert parent not in r.text, f"settings page leaks DB parent dir {parent}"

    def test_watchlist_page_returns_html(self, client):
        r = client.get("/watchlist")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_feeders_page_returns_html(self, client, monkeypatch):
        import asyncio

        async def mock_feeders():
            return [{"name": "readsb", "unit": "readsb.service",
                     "systemd": "active", "overall": "ok"}]

        monkeypatch.setattr(web, "_check_all_feeders", mock_feeders)
        r = client.get("/feeders")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "readsb" in r.text

    def test_feeders_page_no_feeders(self, client, monkeypatch):
        monkeypatch.setattr(config, "FEEDERS", [])
        r = client.get("/feeders")
        assert r.status_code == 200
        assert "No feeders configured" in r.text


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
        feeder = {"name": "fr24", "unit": "fr24.service", "status_type": "fr24", "status_url": "http://x"}
        result = asyncio.get_event_loop().run_until_complete(web._fetch_feeder_details(feeder))
        assert result == [("Version", "1.0")]

    def test_fetch_feeder_details_piaware_dispatch(self, monkeypatch, tmp_path):
        import asyncio
        path = str(tmp_path / "status.json")
        (tmp_path / "status.json").write_text('{"piaware_version": "9"}')
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
                    "top_countries", "frequent_aircraft"):
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

class _FakeResponse:
    """Minimal httpx response stand-in."""
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)


class _FakeAsyncClient:
    """Async context manager that returns canned httpx responses.

    payload can be:
      - a dict  → same response for all URLs
      - a callable(url) → return payload dict per URL (raise to simulate error)
    """
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url, **kwargs):
        if callable(self._payload):
            return _FakeResponse(self._payload(url))
        return _FakeResponse(self._payload)


class _RaisingAsyncClient:
    """Async context manager whose get() raises an exception."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url, **kwargs):
        raise ConnectionError("network down")


class TestApiFlightPhoto:
    def test_unknown_flight_returns_404(self, client):
        r = client.get("/api/flights/9999/photo")
        assert r.status_code == 404

    def test_network_error_returns_null(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn)
        monkeypatch.setattr("readsbstats.web.httpx.AsyncClient", lambda **kw: _RaisingAsyncClient())
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_empty_photos_list_returns_null_and_caches(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            return "n/a"

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None
        # Row with NULL thumbnail should be in the photos table
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row is not None
        assert row["thumbnail_url"] is None

    def test_photo_returned_and_stored(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        payload = {
            "photos": [{
                "thumbnail":       {"src": "https://example.com/thumb.jpg"},
                "thumbnail_large": {"src": "https://example.com/large.jpg"},
                "link":            "https://example.com/photo",
                "photographer":    "Alice",
            }]
        }
        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(payload),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://example.com/thumb.jpg"
        assert data["photographer"] == "Alice"
        assert data["icao_hex"] == "aabbcc"
        # Verify it was persisted
        row = db_conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["thumbnail_url"] == "https://example.com/thumb.jpg"

    def test_cached_photo_served_from_db(self, client, db_conn):
        fid = insert_flight(db_conn, icao="aabbcc")
        # Insert a fresh cached entry
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
        # Insert a fresh "no photo" cache entry
        db_conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,NULL,NULL,NULL,NULL,?)",
            ("aabbcc", int(time.time())),
        )
        db_conn.commit()
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None


class TestPhotoFallback:
    """Tests for airport-data.com fallback when Planespotters has no photo."""

    def test_fallback_used_when_planespotters_empty(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {
                    "status": 200,
                    "data": [{
                        "image": "https://airport-data.com/thumb.jpg",
                        "link": "https://airport-data.com/photo",
                        "photographer": "Charlie",
                    }],
                }
            return {}

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://airport-data.com/thumb.jpg"
        assert data["photographer"] == "Charlie"

    def test_fallback_cached_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {
                    "status": 200,
                    "data": [{"image": "https://ad.com/t.jpg", "link": "https://ad.com/p", "photographer": "X"}],
                }
            return {}

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        client.get(f"/api/flights/{fid}/photo")
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://ad.com/t.jpg"

    def test_null_cached_when_all_sources_empty(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            if "hexdb.io" in url:
                return "n/a"
            return {}

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        assert r.json() is None
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row is not None
        assert row["thumbnail_url"] is None

    def test_planespotters_hit_skips_fallbacks(self, client, db_conn, monkeypatch):
        """When Planespotters has a photo, fallbacks should not be called."""
        fid = insert_flight(db_conn, icao="aabbcc")
        fallback_called = []

        def per_url(url):
            if "airport-data.com" in url or "hexdb.io" in url:
                fallback_called.append(url)
            return {
                "photos": [{
                    "thumbnail": {"src": "https://ps.com/t.jpg"},
                    "thumbnail_large": {"src": "https://ps.com/l.jpg"},
                    "link": "https://ps.com/p",
                    "photographer": "Alice",
                }]
            }

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json()["thumbnail_url"] == "https://ps.com/t.jpg"
        assert fallback_called == []

    def test_airport_data_hit_skips_hexdb(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        hexdb_called = []

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "hexdb.io" in url:
                hexdb_called.append(True)
                return "https://hexdb.io/img.jpg"
            return {
                "status": 200,
                "data": [{"image": "https://ad.com/t.jpg", "link": "https://ad.com/p", "photographer": "Y"}],
            }

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json()["thumbnail_url"] == "https://ad.com/t.jpg"
        assert hexdb_called == []

    def test_fallback_also_works_on_icao_photo_endpoint(self, client, monkeypatch):
        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {
                    "status": 200,
                    "data": [{"image": "https://ad.com/t.jpg", "link": "https://ad.com/p", "photographer": "Y"}],
                }
            return "n/a"

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json()["thumbnail_url"] == "https://ad.com/t.jpg"

    def test_hexdb_used_when_first_two_empty(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            if "hexdb.io" in url:
                return "https://hexdb.io/static/aircraft-images/AABBCC.jpg"
            return {}

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.status_code == 200
        data = r.json()
        assert data["thumbnail_url"] == "https://hexdb.io/static/aircraft-images/AABBCC.jpg"

    def test_hexdb_na_response_means_no_photo(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            if "hexdb.io" in url:
                return "n/a"
            return {}

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json() is None

    def test_hexdb_cached_in_db(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")

        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            return "https://hexdb.io/img/AABBCC.jpg"

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        client.get(f"/api/flights/{fid}/photo")
        row = db_conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex = 'aabbcc'").fetchone()
        assert row["thumbnail_url"] == "https://hexdb.io/img/AABBCC.jpg"

    def test_all_three_sources_attempted_on_failure(self, client, db_conn, monkeypatch):
        fid = insert_flight(db_conn, icao="aabbcc")
        call_count = [0]

        class _AllSourcesFail:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def get(self, url, **kwargs):
                call_count[0] += 1
                if "planespotters" in url:
                    return _FakeResponse({"photos": []})
                if "airport-data.com" in url:
                    raise ConnectionError("down")
                if "hexdb.io" in url:
                    raise ConnectionError("also down")
                raise ConnectionError("unknown")

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _AllSourcesFail(),
        )
        r = client.get(f"/api/flights/{fid}/photo")
        assert r.json() is None
        assert call_count[0] == 3


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

    def test_gallery_page_returns_html(self, client):
        r = client.get("/gallery")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# API: /api/aircraft/{icao_hex}/photo  — photo by ICAO hex
# ---------------------------------------------------------------------------

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
        def per_url(url):
            if "planespotters" in url:
                return {"photos": []}
            if "airport-data.com" in url:
                return {"status": 404}
            return "n/a"

        monkeypatch.setattr(
            "readsbstats.web.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(per_url),
        )
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None

    def test_network_error_returns_null(self, client, monkeypatch):
        monkeypatch.setattr("readsbstats.web.httpx.AsyncClient", lambda **kw: _RaisingAsyncClient())
        r = client.get("/api/aircraft/aabbcc/photo")
        assert r.status_code == 200
        assert r.json() is None
