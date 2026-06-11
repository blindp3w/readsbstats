"""Tests for /map page and /api/map/snapshot endpoint."""

import time

import pytest
from fastapi.testclient import TestClient

from readsbstats import config, database, enrichment, web
from readsbstats import cache
from readsbstats.api import _deps


# ---------------------------------------------------------------------------
# Fixtures (match test_web.py conventions)
# ---------------------------------------------------------------------------

from tests._helpers import make_db  # noqa: E402 — kept under section header


@pytest.fixture()
def db_conn():
    conn = make_db()
    enrichment.clear_cache()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    monkeypatch.setattr(_deps, "_db", db_conn)
    cache._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_flight_with_position(
    conn, *, icao="aabbcc", ts_offset=0, lat=52.1, lon=21.0,
    alt_baro=35000, gs=450.0, track=90.0, source_type="adsb_icao",
):
    """Insert a flight with one position at now + ts_offset."""
    now = int(time.time())
    fid = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, registration, aircraft_type, first_seen, last_seen,
             total_positions, primary_source, lat_min, lat_max, lon_min, lon_max)
        VALUES (?,?,?,?,?,?,?,?,0,0,0,0)
        """,
        (icao, "TST123", "SP-TST", "B738", now, now + 3600, 1, "adsb"),
    ).lastrowid
    pos_ts = now + ts_offset
    conn.execute(
        """
        INSERT INTO positions (flight_id, ts, lat, lon, alt_baro, gs, track, source_type)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (fid, pos_ts, lat, lon, alt_baro, gs, track, source_type),
    )
    conn.commit()
    return fid


# ---------------------------------------------------------------------------
# Tests: index
# ---------------------------------------------------------------------------

class TestIndex:
    def test_ts_flight_index_created_by_background_migration(self, tmp_path):
        from readsbstats import database
        db_path = str(tmp_path / "idx.db")
        database.init_db(db_path)
        database._build_positions_indexes(db_path)
        conn = database.connect(db_path)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(positions)")}
        conn.close()
        assert "idx_positions_ts_flight" in indexes


# ---------------------------------------------------------------------------
# Tests: /api/map/snapshot
# (The /map Jinja page was deleted at v2.0.0 cutover; the React SPA at
#  /v2/map owns the live-map UI. The /live compat redirect → /v2/map is
#  tested in test_web.py::TestCompatRedirects.)
# ---------------------------------------------------------------------------

class TestMapSnapshot:
    def test_live_snapshot_no_at(self, client, db_conn):
        fid = insert_flight_with_position(db_conn)
        resp = client.get("/api/map/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is True
        assert "aircraft" in data
        assert len(data["aircraft"]) == 1
        assert data["aircraft"][0]["flight_id"] == fid

    def test_live_snapshot_position_fields(self, client, db_conn):
        insert_flight_with_position(db_conn, lat=52.5, lon=21.3, track=180.0)
        data = client.get("/api/map/snapshot").json()
        ac = data["aircraft"][0]
        assert ac["lat"] == pytest.approx(52.5)
        assert ac["lon"] == pytest.approx(21.3)
        assert ac["track"] == pytest.approx(180.0)
        assert ac["alt_baro"] == 35000
        assert "flags" in ac
        assert "trail" in ac

    def test_receiver_coords_in_response(self, client, db_conn):
        resp = client.get("/api/map/snapshot")
        data = resp.json()
        assert data["receiver_lat"] == pytest.approx(config.RECEIVER_LAT)
        assert data["receiver_lon"] == pytest.approx(config.RECEIVER_LON)

    def test_historical_snapshot_finds_aircraft(self, client, db_conn):
        now = int(time.time())
        insert_flight_with_position(db_conn, ts_offset=-300)  # position 5 min ago
        resp = client.get(f"/api/map/snapshot?at={now - 60}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is False
        assert len(data["aircraft"]) == 1

    def test_snapshot_too_old_empty(self, client, db_conn):
        insert_flight_with_position(db_conn)
        old_ts = int(time.time()) - 7200  # 2h ago, position was ~now
        resp = client.get(f"/api/map/snapshot?at={old_ts}")
        assert resp.status_code == 200
        assert resp.json()["aircraft"] == []

    def test_future_timestamp_rejected(self, client, db_conn):
        future_ts = int(time.time()) + 300
        resp = client.get(f"/api/map/snapshot?at={future_ts}")
        assert resp.status_code == 400

    def test_beyond_history_limit_rejected(self, client, db_conn, monkeypatch):
        monkeypatch.setattr(config, "MAP_HISTORY_HOURS", 1)
        old_ts = int(time.time()) - 7201  # just over 1h
        resp = client.get(f"/api/map/snapshot?at={old_ts}")
        assert resp.status_code == 400

    def test_trail_included_by_default(self, client, db_conn):
        insert_flight_with_position(db_conn)
        data = client.get("/api/map/snapshot").json()
        assert "trail" in data["aircraft"][0]
        assert isinstance(data["aircraft"][0]["trail"], list)

    def test_trail_zero_returns_empty_list(self, client, db_conn):
        insert_flight_with_position(db_conn)
        data = client.get("/api/map/snapshot?trail=0").json()
        assert data["aircraft"][0]["trail"] == []

    def test_trail_capped_at_50(self, client, db_conn):
        """trail=100 is accepted (no 422) and capped internally to 50."""
        insert_flight_with_position(db_conn)
        resp = client.get("/api/map/snapshot?trail=100")
        assert resp.status_code == 200

    def test_multiple_aircraft(self, client, db_conn):
        insert_flight_with_position(db_conn, icao="aabbcc")
        insert_flight_with_position(db_conn, icao="ddeeff")
        data = client.get("/api/map/snapshot").json()
        assert len(data["aircraft"]) == 2

    def test_aircraft_outside_window_excluded(self, client, db_conn):
        """Position more than 600s before `at` should not appear."""
        now = int(time.time())
        insert_flight_with_position(db_conn, ts_offset=-700)  # 700s ago — outside window
        resp = client.get(f"/api/map/snapshot?at={now}")
        assert resp.status_code == 200
        assert resp.json()["aircraft"] == []

    def test_negative_trail_rejected(self, client, db_conn):
        resp = client.get("/api/map/snapshot?trail=-1")
        assert resp.status_code == 422

    def test_at_within_30s_of_now_is_live(self, client, db_conn):
        near_now = int(time.time()) - 15
        resp = client.get(f"/api/map/snapshot?at={near_now}")
        assert resp.status_code == 200
        assert resp.json()["is_live"] is True

    def test_empty_db_returns_empty_list(self, client):
        data = client.get("/api/map/snapshot").json()
        assert data["aircraft"] == []
        assert data["is_live"] is True

    def test_category_field_in_response(self, client, db_conn):
        """category must be present in each aircraft dict (may be None)."""
        insert_flight_with_position(db_conn)
        ac = client.get("/api/map/snapshot").json()["aircraft"][0]
        assert "category" in ac

    def test_sidebar_fields_in_response(self, client, db_conn):
        """Snapshot must include seconds_ago, origin_icao, dest_icao for sidebar."""
        insert_flight_with_position(db_conn)
        ac = client.get("/api/map/snapshot").json()["aircraft"][0]
        assert "seconds_ago" in ac
        assert "origin_icao" in ac
        assert "dest_icao" in ac
        assert ac["seconds_ago"] >= 0

    def test_live_redirects_to_map(self, client):
        # /live is a historical alias kept as a 302 after the Jinja UI was
        # deleted; the SPA's catch-all serves /map natively.
        r = client.get("/live", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].endswith("/map")

    def test_api_live_picks_highest_ts_not_highest_id(self, client, db_conn):
        """api_live must return the latest fix by timestamp. Insert two
        positions out of ts order so rowid order and ts order disagree: the
        old ORDER BY id DESC wrongly picks the higher-rowid (lower-ts) row."""
        now = int(time.time())
        fid = db_conn.execute(
            """
            INSERT INTO flights
                (icao_hex, callsign, registration, aircraft_type, first_seen, last_seen,
                 total_positions, primary_source, lat_min, lat_max, lon_min, lon_max)
            VALUES ('aabbcc', 'TST123', 'SP-TST', 'B738', ?, ?, 2, 'adsb', 0, 0, 0, 0)
            """,
            (now, now + 3600),
        ).lastrowid
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc', ?, ?)",
            (fid, now),
        )
        # Lower rowid, HIGHER ts — this is the correct "latest" position.
        db_conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?, ?, 52.0, 21.0, 'adsb_icao')",
            (fid, now),
        )
        # Higher rowid, LOWER ts — the old ORDER BY id DESC wrongly picks this.
        db_conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?, ?, 53.0, 20.0, 'adsb_icao')",
            (fid, now - 50),
        )
        db_conn.commit()
        resp = client.get("/api/live")
        assert resp.status_code == 200
        aircraft = resp.json()["aircraft"]
        assert len(aircraft) == 1
        assert aircraft[0]["lat"] == pytest.approx(52.0)
