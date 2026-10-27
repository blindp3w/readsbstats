"""Tests for the origin / dest / airport filters on /api/flights and
/api/flights/export.csv (ADR-0022, Decision 7)."""

import csv
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from readsbstats import cache, config, enrichment, web
from readsbstats.api import _deps
from tests._helpers import make_db


@pytest.fixture(autouse=True)
def setup():
    enrichment.clear_cache()
    cache._cache.clear()
    yield


@pytest.fixture()
def db_conn():
    conn = make_db()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    monkeypatch.setattr(_deps, "_db", db_conn)
    monkeypatch.setattr(config, "PREWARM_MAP_CACHE", False)
    with TestClient(web.app, raise_server_exceptions=True,
                    headers={"X-Requested-With": "XMLHttpRequest"}) as c:
        yield c


def insert_flight(conn, *, icao, callsign, first_seen, origin=None, dest=None):
    conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, first_seen, last_seen, total_positions,
             adsb_positions, mlat_positions, primary_source,
             lat_min, lat_max, lon_min, lon_max, origin_icao, dest_icao)
        VALUES (?,?,?,?,10,10,0,'adsb',0,0,0,0,?,?)
        """,
        (icao, callsign, first_seen, first_seen + 3600, origin, dest),
    )
    conn.commit()


@pytest.fixture()
def seeded(db_conn):
    insert_flight(db_conn, icao="aaaaaa", callsign="LOT1", first_seen=1_000_000,
                  origin="EPWA", dest="EGLL")
    insert_flight(db_conn, icao="bbbbbb", callsign="BAW2", first_seen=1_001_000,
                  origin="EGLL", dest="EPWA")
    insert_flight(db_conn, icao="cccccc", callsign="LOT3", first_seen=1_002_000,
                  origin="EPKK", dest="LOWW")
    insert_flight(db_conn, icao="dddddd", callsign="LOT4", first_seen=1_003_000)
    return db_conn


def _callsigns(resp):
    return sorted(f["callsign"] for f in resp.json()["flights"])


# ---------------------------------------------------------------------------
# _build_flight_filter unit tests
# ---------------------------------------------------------------------------

def _bf(**kw):
    return _deps._build_flight_filter(None, None, None, None, None, None, None, **kw)


class TestBuildFlightFilter:
    def test_origin(self):
        where, params = _bf(origin="EPWA")
        assert where == "WHERE f.origin_icao = ?"
        assert params == ["EPWA"]

    def test_dest(self):
        where, params = _bf(dest="EGLL")
        assert where == "WHERE f.dest_icao = ?"
        assert params == ["EGLL"]

    def test_airport_matches_either_end(self):
        where, params = _bf(airport="EPWA")
        assert where == "WHERE (f.origin_icao = ? OR f.dest_icao = ?)"
        assert params == ["EPWA", "EPWA"]

    def test_lowercase_is_uppercased(self):
        _, params = _bf(origin="epwa", dest="egll", airport="epkk")
        assert params == ["EPWA", "EGLL", "EPKK", "EPKK"]

    @pytest.mark.parametrize("bad", ["EPW", "EPWA1", "EP-A", "' or 1=1"])
    @pytest.mark.parametrize("param", ["origin", "dest", "airport"])
    def test_invalid_raises_400(self, param, bad):
        with pytest.raises(HTTPException) as exc:
            _bf(**{param: bad})
        assert exc.value.status_code == 400
        assert param in exc.value.detail
        assert "4-character ICAO airport code" in exc.value.detail

    @pytest.mark.parametrize("empty", [None, ""])
    def test_empty_not_applied(self, empty):
        where, params = _bf(origin=empty, dest=empty, airport=empty)
        assert where == ""
        assert params == []

    def test_combined_with_other_filters_uses_and(self):
        where, params = _deps._build_flight_filter(
            None, None, "LOT", None, None, None, None,
            origin="EPWA", airport="EGLL",
        )
        assert where == (
            "WHERE f.callsign LIKE ? AND f.origin_icao = ? "
            "AND (f.origin_icao = ? OR f.dest_icao = ?)"
        )
        assert params == ["LOT%", "EPWA", "EGLL", "EGLL"]

    def test_positional_seven_arg_call_still_works(self):
        where, params = _deps._build_flight_filter(
            None, "aabbcc", None, None, None, None, None)
        assert where == "WHERE f.icao_hex = ?"
        assert params == ["aabbcc"]


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

class TestFlightsApi:
    def test_origin(self, client, seeded):
        r = client.get("/api/flights?origin=EPWA")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert _callsigns(r) == ["LOT1"]

    def test_dest(self, client, seeded):
        r = client.get("/api/flights?dest=EPWA")
        assert r.json()["total"] == 1
        assert _callsigns(r) == ["BAW2"]

    def test_airport_both_directions_excludes_null_route(self, client, seeded):
        r = client.get("/api/flights?airport=EPWA")
        assert r.json()["total"] == 2
        assert _callsigns(r) == ["BAW2", "LOT1"]

    def test_lowercase_accepted(self, client, seeded):
        r = client.get("/api/flights?airport=epwa")
        assert r.json()["total"] == 2

    def test_origin_and_dest_combined(self, client, seeded):
        r = client.get("/api/flights?origin=EPWA&dest=EGLL")
        assert _callsigns(r) == ["LOT1"]

    def test_combined_with_callsign_prefix(self, client, seeded):
        r = client.get("/api/flights?airport=EPWA&callsign=LOT")
        assert r.json()["total"] == 1
        assert _callsigns(r) == ["LOT1"]

    def test_no_match(self, client, seeded):
        r = client.get("/api/flights?airport=KJFK")
        assert r.json()["total"] == 0
        assert r.json()["flights"] == []

    @pytest.mark.parametrize("param", ["origin", "dest", "airport"])
    @pytest.mark.parametrize("bad", ["EPW", "EPWA1", "EP-A"])
    def test_invalid_code_400(self, client, seeded, param, bad):
        r = client.get(f"/api/flights?{param}={bad}")
        assert r.status_code == 400

    def test_empty_value_ignored(self, client, seeded):
        r = client.get("/api/flights?airport=")
        assert r.status_code == 200
        assert r.json()["total"] == 4


class TestExportApi:
    @staticmethod
    def _rows(resp):
        return list(csv.reader(io.StringIO(resp.text)))[1:]

    def test_airport_filter(self, client, seeded):
        r = client.get("/api/flights/export.csv?airport=EPWA")
        assert r.status_code == 200
        assert len(self._rows(r)) == 2

    def test_origin_filter(self, client, seeded):
        r = client.get("/api/flights/export.csv?origin=EPKK")
        assert len(self._rows(r)) == 1

    def test_invalid_code_400(self, client, seeded):
        r = client.get("/api/flights/export.csv?dest=EPW")
        assert r.status_code == 400
