"""End-to-end tests of the 19 MCP tools through the real web app (ADR-0022).

Chain under test: raw JSON-RPC over Streamable HTTP -> BearerAuthMiddleware ->
MCPServer tool -> ApiClient (httpx ASGITransport) -> real FastAPI app over an
in-memory SQLite database.
"""

import json
import logging
import time

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from readsbstats import cache, config, enrichment, photo_sources, web
from readsbstats.api import _deps, _photos
from readsbstats.api import feeders as feeders_mod
from readsbstats.mcp_server.auth import Client, hash_token
from readsbstats.mcp_server.client import ApiClient
from readsbstats.mcp_server.limits import Limiter
from readsbstats.mcp_server.server import build_app
from tests._helpers import insert_position, make_db

RLAT, RLON = 52.1234, 21.4321
BASE_URL = "http://127.0.0.1:8765"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
NOW = int(time.time())
SERVER_LOG = "readsbstats.mcp_server.server"

FORBIDDEN = ["receiver_lat", "receiver_lon", "lat_min", "lat_max", "lon_min", "lon_max",
             "Radar", "T-EPWA", "52.1234", "21.4321", "fr24feed"]


COORDINATE_KEYS = frozenset({"lat", "lon", "latitude", "longitude", "lat_min", "lat_max",
                             "lon_min", "lon_max", "receiver_lat", "receiver_lon"})


def coordinate_keys(obj) -> set:
    """Every coordinate-like key anywhere in a structured result (recursive walk)."""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in COORDINATE_KEYS:
                found.add(k)
            found |= coordinate_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= coordinate_keys(v)
    return found


def generous_limiter(**kw):
    args = dict(read_per_min=1000, write_per_hour=1000, photo_per_hour=1000, concurrency=4)
    args.update(kw)
    return Limiter(**args)


def add_flight(conn, *, icao, callsign, first_seen, last_seen=None, origin=None, dest=None,
               atype="B738", reg=None, alt=35000, gs=450.0, dist=120.0):
    cur = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, registration, aircraft_type, first_seen, last_seen,
             max_alt_baro, max_gs, max_distance_nm, max_distance_bearing,
             total_positions, adsb_positions, mlat_positions, primary_source,
             lat_min, lat_max, lon_min, lon_max, origin_icao, dest_icao)
        VALUES (?,?,?,?,?,?,?,?,?,45.0,10,10,0,'adsb',51.9,52.9,20.9,21.9,?,?)
        """,
        (icao, callsign, reg, atype, first_seen, last_seen or first_seen + 3000,
         alt, gs, dist, origin, dest))
    conn.commit()
    return cur.lastrowid


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    enrichment.clear_cache()
    cache._cache.clear()
    monkeypatch.setattr(config, "PREWARM_MAP_CACHE", False)
    monkeypatch.setattr(config, "RECEIVER_LAT", RLAT)
    monkeypatch.setattr(config, "RECEIVER_LON", RLON)
    yield
    cache._cache.clear()


@pytest.fixture()
def db(monkeypatch):
    conn = make_db()
    monkeypatch.setattr(_deps, "_db", conn)
    t = int(time.time())
    for icao, name, country in [("EPWA", "Warsaw Chopin\x00", "Poland"), ("EGLL", "London Heathrow", "UK"),
                                ("EDDF", "Frankfurt", "Germany"), ("EPKK", "Krakow", "Poland")]:
        conn.execute("INSERT INTO airports (icao_code, iata_code, name, country, latitude, longitude,"
                     " fetched_at) VALUES (?,?,?,?,0,0,?)", (icao, icao[:3], name, country, t))
    conn.execute("INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags)"
                 " VALUES ('48c1ea','SP-LWA','B738','BOEING 737-800',1)")
    conn.execute("INSERT INTO airlines (icao_code, name, iata_code, country, active)"
                 " VALUES ('LOT','LOT Polish Airlines','LO','Poland',1)")
    f1 = add_flight(conn, icao="48c1ea", callsign="LOT123", first_seen=NOW - 7200, origin="EPWA",
                    dest="EGLL", reg="SP-LWA")
    add_flight(conn, icao="48c1ea", callsign="LOT122", first_seen=NOW - 30 * 3600, origin="EGLL",
               dest="EPWA", reg="SP-LWA", alt=30000)
    add_flight(conn, icao="3c6444", callsign="DLH456", first_seen=NOW - 6000, origin="EDDF",
               dest="EPWA", atype="A320", alt=37000)
    add_flight(conn, icao="4ca123", callsign="RYR789", first_seen=NOW - 5000, origin="EPKK",
               dest="EGLL", atype="B738", alt=38000)
    add_flight(conn, icao="aabbcc", callsign="LOT999", first_seen=NOW - 4000, atype="E190",
               alt=28000)
    for i in range(6):
        insert_position(conn, f1, NOW - 7200 + i * 600, lat=52.3 + i * 0.05, lon=21.0 + i * 0.05,
                        alt_baro=1000 * (i + 1), gs=300.0 + i, track=90.0)
    # live aircraft: an open flight with a recent position
    live_id = add_flight(conn, icao="3c6444", callsign="DLH457", first_seen=NOW - 900,
                         last_seen=NOW - 5, origin="EDDF", dest="EPWA", atype="A320")
    insert_position(conn, live_id, NOW - 20, lat=52.6789, lon=21.8765, alt_baro=12000, gs=410.0,
                    track=270.0)
    conn.execute("INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('3c6444',?,?)",
                 (live_id, NOW - 5))
    conn.execute("INSERT INTO callsign_routes (callsign, origin_icao, dest_icao, fetched_at)"
                 " VALUES ('DLH457','EDDF','EPWA',?)", (t,))
    for i in range(30):
        conn.execute("INSERT INTO receiver_stats (ts, signal, noise, messages, peak_signal)"
                     " VALUES (?,?,?,?,?)", (NOW - 3600 + i * 60, -20.0 + i * 0.1, -45.0, 1000 + i, -3.0))
    conn.execute("INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer,"
                 " fetched_at) VALUES ('48c1ea','https://plnspttrs.net/t.jpg','https://plnspttrs.net/l.jpg',"
                 "'https://plnspttrs.net/p',?,?)", ("Bob\x00 B", t))
    conn.commit()

    async def no_photo(*a, **k):
        return None
    monkeypatch.setattr(_photos, "_fetch_type_photo", no_photo)
    monkeypatch.setattr(photo_sources, "_fetch_photo_with_status", lambda icao: (None, "miss"))

    async def fake_unit(unit):
        return {"systemd": "active"}

    async def fake_port(port, host="127.0.0.1"):
        return {"port_status": "open"}

    async def fake_details(feeder):
        return [("Radar code", "T-EPWA123"), ("MLAT server", "mlat.example")]
    monkeypatch.setattr(config, "FEEDERS", [{"name": "FR24", "unit": "fr24feed", "port": 8754}])
    monkeypatch.setattr(feeders_mod, "_check_systemd_unit", fake_unit)
    monkeypatch.setattr(feeders_mod, "_check_port", fake_port)
    monkeypatch.setattr(feeders_mod, "_fetch_feeder_details", fake_details)
    yield conn
    conn.close()


TOKENS = {hash_token("tok-rw"): Client("rw", frozenset({"read", "watchlist"})),
          hash_token("tok-ro"): Client("ro", frozenset({"read"}))}


@pytest.fixture()
def api(db):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app))
    return ApiClient("http://127.0.0.1:8080", http=http)


class Session:
    def __init__(self, c):
        self.c = c
        self.n = 0

    def raw(self, name, args=None, token="tok-rw"):
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                "params": {"name": name, "arguments": args or {}}}
        h = dict(H)
        if token:
            h["Authorization"] = f"Bearer {token}"
        r = self.c.post("/mcp", headers=h, content=json.dumps(body))
        assert r.status_code == 200, r.text
        return r.json()["result"]

    def call(self, name, args=None, token="tok-rw"):
        """Return parsed JSON of a successful call (text and structuredContent agree)."""
        res = self.raw(name, args, token)
        assert not res.get("isError"), res["content"][0]["text"]
        data = json.loads(res["content"][0]["text"])
        assert res.get("structuredContent") == data
        return data

    def err(self, name, args=None, token="tok-rw"):
        res = self.raw(name, args, token)
        assert res.get("isError") is True, res
        return res["content"][0]["text"]


@pytest.fixture()
def mcp(api):
    app = build_app(TOKENS, api=api, limiter=generous_limiter())
    with TestClient(app, base_url=BASE_URL) as c:
        yield Session(c)


# ---- search_flights ---------------------------------------------------------
class TestSearchFlights:
    def test_default_period_24h(self, mcp):
        out = mcp.call("search_flights")
        assert out["total"] == 5 and out["count"] == 5 and out["offset"] == 0
        assert [f["callsign"] for f in out["flights"]][0] == "DLH457"        # newest first
        assert all("lat_min" not in f for f in out["flights"])

    def test_all_period(self, mcp):
        assert mcp.call("search_flights", {"period": "all"})["total"] == 6

    def test_airport_filter_count_equals_seeded(self, mcp):
        out = mcp.call("search_flights", {"airport": "EPWA"})
        assert out["total"] == 3 and out["count"] == 3        # LOT123, DLH456, DLH457 within 24h
        assert mcp.call("search_flights", {"airport": "epwa", "period": "all"})["total"] == 4

    def test_origin_dest(self, mcp):
        out = mcp.call("search_flights", {"origin": "EPKK"})
        assert [f["callsign"] for f in out["flights"]] == ["RYR789"]
        out = mcp.call("search_flights", {"dest": "EGLL"})
        assert {f["callsign"] for f in out["flights"]} == {"LOT123", "RYR789"}

    def test_filters(self, mcp):
        assert mcp.call("search_flights", {"callsign": "lot"})["total"] == 2
        assert mcp.call("search_flights", {"icao_hex": "~48C1EA", "period": "all"})["total"] == 2
        assert mcp.call("search_flights", {"registration": "sp-l"})["total"] == 1
        assert mcp.call("search_flights", {"aircraft_type": "a320"})["total"] == 2
        assert mcp.call("search_flights", {"source": "adsb"})["total"] == 5

    def test_flags_filter_and_names(self, mcp):
        out = mcp.call("search_flights", {"flags": "military"})
        assert out["total"] == 1
        f = out["flights"][0]
        assert f["callsign"] == "LOT123" and f["flags"] == ["military"]
        assert f["origin_icao"] == "EPWA" and f["dest_name"] == "London Heathrow"
        assert f["origin_name"] == "Warsaw Chopin"
        assert f["registration"] == "SP-LWA" and f["type_desc"] == "BOEING 737-800"
        assert f["duration_min"] == 50 and f["max_alt_ft"] == 35000

    def test_sort_and_paging(self, mcp):
        out = mcp.call("search_flights", {"sort_by": "max_alt_baro", "sort_dir": "desc", "limit": 2})
        assert out["count"] == 2 and out["total"] == 5
        top = out["flights"][0]["max_alt_ft"]
        assert top >= out["flights"][1]["max_alt_ft"]
        page2 = mcp.call("search_flights", {"limit": 2, "offset": 2, "sort_by": "callsign",
                                            "sort_dir": "asc"})
        assert page2["offset"] == 2 and page2["count"] == 2

    def test_explicit_dates(self, mcp):
        day = time.strftime("%Y-%m-%d", time.localtime(NOW - 30 * 3600))
        out = mcp.call("search_flights", {"period": day})
        assert all(f["first_seen"].startswith(day) for f in out["flights"])

    @pytest.mark.parametrize("args", [
        {"period": "banana"}, {"period": "9999d"}, {"limit": 51}, {"limit": 0}, {"offset": 10001},
        {"squawk": "1289"}, {"squawk": "12"}, {"flags": "pia"}, {"source": "radar"},
        {"sort_by": "lat_min"}, {"sort_dir": "up"}, {"airport": "EPW"}, {"airport": "EP/A"},
        {"callsign": "LOT%"}, {"icao_hex": "xyz"}, {"aircraft_type": "B7381"},
        {"registration": "a b"},
    ])
    def test_bad_arguments_are_errors(self, mcp, args):
        mcp.err("search_flights", args)

    def test_future_period_error(self, mcp):
        mcp.err("search_flights", {"period": "2999-01-01"})


# ---- single flight tools ----------------------------------------------------
class TestFlightTools:
    def _fid(self, mcp, callsign="LOT123"):
        return next(f["id"] for f in mcp.call("search_flights", {"period": "all", "limit": 50})["flights"]
                    if f["callsign"] == callsign)

    def test_get_flight(self, mcp):
        fid = self._fid(mcp)
        out = mcp.call("get_flight", {"flight_id": fid})
        assert out["flight"]["callsign"] == "LOT123" and out["flight"]["id"] == fid
        assert out["flight"]["airline_name"] == "LOT Polish Airlines"
        assert [f["callsign"] for f in out["other_flights"]] == ["LOT122"]

    def test_get_flight_unknown(self, mcp):
        assert "not found" in mcp.err("get_flight", {"flight_id": 999999}).lower()

    @pytest.mark.parametrize("fid", [0, -1, "x", 1.5, 10 ** 12])
    def test_get_flight_bad_id(self, mcp, fid):
        mcp.err("get_flight", {"flight_id": fid})

    def test_flight_profile_has_no_coordinates(self, mcp):
        fid = self._fid(mcp)
        out = mcp.call("flight_profile", {"flight_id": fid, "points": 4})
        assert out["total_positions"] == 6 and len(out["points"]) == 4
        for p in out["points"]:
            assert set(p) <= {"time", "alt_ft", "gs_kt", "track_deg", "vrate_fpm", "source"}
            assert "alt_ft" in p and "time" in p
        text = json.dumps(out)
        assert '"lat"' not in text and '"lon"' not in text

    def test_flight_profile_points_bounds(self, mcp):
        fid = self._fid(mcp)
        mcp.err("flight_profile", {"flight_id": fid, "points": 1})
        mcp.err("flight_profile", {"flight_id": fid, "points": 101})
        assert mcp.call("flight_profile", {"flight_id": fid})["total_positions"] == 6

    def test_aircraft_history(self, mcp):
        out = mcp.call("aircraft_history", {"icao_hex": "48C1EA"})
        assert out["total"] == 2 and len(out["flights"]) == 2
        assert out["aircraft"]["registration"] == "SP-LWA" and out["aircraft"]["flags"] == ["military"]
        assert out["aircraft"]["total_flights"] == 2

    def test_aircraft_history_unknown_is_empty(self, mcp):
        out = mcp.call("aircraft_history", {"icao_hex": "ffffff"})
        assert out["total"] == 0 and out["flights"] == []

    def test_aircraft_history_bad_hex(self, mcp):
        mcp.err("aircraft_history", {"icao_hex": "../settings"})

    def test_airline_and_type(self, mcp):
        out = mcp.call("airline_flights", {"prefix": "lot"})
        assert out["total"] == 3 and {f["callsign"] for f in out["flights"]} >= {"LOT123", "LOT999"}
        out = mcp.call("type_flights", {"aircraft_type": "a320"})
        assert out["total"] == 2
        mcp.err("airline_flights", {"prefix": "L/T"})
        mcp.err("type_flights", {"aircraft_type": "../x"})

    def test_photo(self, mcp):
        out = mcp.call("aircraft_photo", {"icao_hex": "48c1ea"})
        assert out["thumbnail_url"] == "https://plnspttrs.net/t.jpg"
        assert out["photographer"] == "Bob B" and out["is_type_photo"] is False

    def test_photo_none(self, mcp):
        assert mcp.call("aircraft_photo", {"icao_hex": "3c6444"}) == {"photo": None}

    def test_photo_timeout_message(self, db, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        api = ApiClient("http://127.0.0.1:8080",
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TestClient(build_app(TOKENS, api=api, limiter=generous_limiter()),
                        base_url=BASE_URL) as c:
            text = Session(c).err("aircraft_photo", {"icao_hex": "3c6444"})
        assert "being fetched" in text and "ask again in a minute" in text

    def test_photo_uses_photo_budget_not_read_budget(self, api):
        lim = generous_limiter(read_per_min=1, photo_per_hour=2)
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("aircraft_photo", {"icao_hex": "48c1ea"})
            s.call("aircraft_photo", {"icao_hex": "48c1ea"})
            assert "rate limit" in s.err("aircraft_photo", {"icao_hex": "48c1ea"})
            s.call("records")                                 # read budget untouched
            assert "rate limit" in s.err("records")


# ---- aircraft lists / sky ---------------------------------------------------
class TestSkyTools:
    def test_flagged(self, mcp):
        out = mcp.call("flagged_aircraft", {"kind": "military"})
        assert out["total"] == 1 and out["aircraft"][0]["icao_hex"] == "48c1ea"
        a = out["aircraft"][0]
        assert a["flags"] == ["military"] and a["registration"] == "SP-LWA"
        assert a["flight_count"] == 2
        assert mcp.call("flagged_aircraft")["total"] >= 1
        assert mcp.call("flagged_aircraft", {"sort_by": "flight_count", "limit": 5})["total"] >= 1
        mcp.err("flagged_aircraft", {"kind": "bogus"})
        mcp.err("flagged_aircraft", {"sort_by": "lat"})

    def test_live(self, mcp):
        out = mcp.call("live_aircraft")
        assert out["count"] == 1
        a = out["aircraft"][0]
        assert a["icao_hex"] == "3c6444" and a["callsign"] == "DLH457"
        assert a["origin_icao"] == "EDDF" and a["dest_icao"] == "EPWA"
        assert "lat" not in a and "lon" not in a
        assert a["distance_nm"] > 0 and 0 <= a["bearing_deg"] < 360
        assert "receiver_lat" not in out
        mcp.err("live_aircraft", {"limit": 201})

    def test_sky_at_now(self, mcp):
        out = mcp.call("sky_at", {"time": "now"})
        assert out["count"] >= 1 and out["is_live"] is True
        a = next(x for x in out["aircraft"] if x["icao_hex"] == "3c6444")
        assert a["alt_ft"] == 12000 and "distance_nm" in a and "trail" not in a
        assert "lat" not in a and "lon" not in a

    def test_sky_at_past_iso(self, mcp):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 7200 + 1200 + 30))
        out = mcp.call("sky_at", {"time": ts})
        assert out["is_live"] is False
        assert any(a["icao_hex"] == "48c1ea" for a in out["aircraft"])

    @pytest.mark.parametrize("t", ["banana", "2999-01-01", "2001-01-01", ""])
    def test_sky_at_errors(self, mcp, t):
        mcp.err("sky_at", {"time": t})


# ---- stats and receiver -----------------------------------------------------
class TestStatsTools:
    def test_stats_default(self, mcp):
        out = mcp.call("stats")
        assert out["period"]["from"] and out["period"]["to"]
        assert out["total_flights"] == 5 and out["unique_aircraft"] == 4
        assert out["military_flights"] == 1
        assert out["top_airlines"][0]["airline"] == "LOT"
        assert "top_routes" in out and "top_airports" in out and "top_aircraft_types" in out
        assert "hourly_distribution" not in out

    def test_stats_all_time(self, mcp):
        out = mcp.call("stats", {"period": "all"})
        assert out["total_flights"] == 6 and out["period"] == {"all_time": True}

    def test_stats_sections(self, mcp):
        out = mcp.call("stats", {"period": "7d", "sections": ["hourly_distribution", "furthest_aircraft",
                                                              "new_aircraft"]})
        assert len(out["hourly_distribution"]) == 24
        assert out["furthest_aircraft"]["callsign"]
        assert out["new_aircraft"]["total"] >= 1
        assert "total_flights" not in out

    @pytest.mark.parametrize("args", [{"period": "120d"}, {"period": "banana"},
                                      {"sections": ["receiver_lat"]}, {"sections": ["lifetime"]}])
    def test_stats_errors(self, mcp, args):
        mcp.err("stats", args)

    @pytest.mark.parametrize("period", ["24h", "7d", "30d", "90d", "today", "yesterday",
                                        "2026-07-01", "all", " 7D "])
    def test_stats_allowed_periods(self, mcp, period):
        assert "period" in mcp.call("stats", {"period": period})

    @pytest.mark.parametrize("period", ["2026-07-01..2026-09-30", "13d", "6h", "91d", "92d",
                                        "1d", "720h", "2026-7-1"])
    def test_stats_rejects_other_periods(self, mcp, period):
        text = mcp.err("stats", {"period": period})
        assert "24h" in text and "90d" in text and "YYYY-MM-DD" in text and "all" in text

    def test_same_period_is_snapped(self, api, monkeypatch):
        seen = []
        orig = api.call

        async def spy(route, **kw):
            seen.append(kw.get("query"))
            return await orig(route, **kw)
        monkeypatch.setattr(api, "call", spy)
        with TestClient(build_app(TOKENS, api=api, limiter=generous_limiter()), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("stats", {"period": "24h"})
            s.call("stats", {"period": "24h"})
        assert seen[0] == seen[1] and seen[0]["from"] % 600 == 0

    def test_records(self, mcp):
        out = mcp.call("records")
        assert set(out) == {"furthest", "fastest", "highest", "longest"}
        assert out["highest"]["max_alt_ft"] == 38000
        assert "lat_min" not in json.dumps(out)

    def test_daily_counts(self, mcp):
        out = mcp.call("daily_counts", {"days": 2})
        assert len(out["days"]) == 2 and out["days"][0]["flights"] >= 1
        assert len(mcp.call("daily_counts")["days"]) >= 2
        mcp.err("daily_counts", {"days": 0})
        mcp.err("daily_counts", {"days": 366})

    def test_range_by_bearing(self, mcp):
        out = mcp.call("range_by_bearing")
        assert len(out["buckets"]) == 36
        assert next(b for b in out["buckets"] if b["bearing_deg"] == 40)["max_nm"] > 0

    def test_receiver_status(self, mcp):
        out = mcp.call("receiver_status")
        assert out["api"] == "ok"
        assert out["health"]["overall"] in ("ok", "info", "warn", "error", "crit", "critical", "degraded")
        assert out["health"]["checks"]
        assert out["feeders"] == [{"name": "FR24", "systemd": "active", "port_status": "open",
                                   "overall": "ok"}]

    def test_receiver_status_survives_one_failing_part(self, api, monkeypatch):
        orig = api.call

        async def flaky(route, **kw):
            if route == "feeders":
                raise ToolError("boom")
            return await orig(route, **kw)
        monkeypatch.setattr(api, "call", flaky)
        with TestClient(build_app(TOKENS, api=api, limiter=generous_limiter()), base_url=BASE_URL) as c:
            out = Session(c).call("receiver_status")
        assert out["api"] == "ok" and out["feeders_error"] == "boom" and "feeders" not in out

    def test_receiver_status_all_parts_failing_is_an_error(self, api, monkeypatch):
        async def boom(route, **kw):
            raise ToolError("down")
        monkeypatch.setattr(api, "call", boom)
        with TestClient(build_app(TOKENS, api=api, limiter=generous_limiter()), base_url=BASE_URL) as c:
            assert "down" in Session(c).err("receiver_status")

    def test_receiver_metrics_default(self, mcp):
        out = mcp.call("receiver_metrics")
        assert set(out["metrics"]) == {"signal", "noise", "messages"}
        s = out["metrics"]["signal"]
        assert s["min"] < s["max"] and len(s["points"]) == 30
        assert out["period"]["from"] and out["metrics"]["noise"]["avg"] == -45.0

    def test_receiver_metrics_args(self, mcp):
        out = mcp.call("receiver_metrics", {"metrics": ["peak_signal", "peak_signal"], "period": "6h"})
        assert list(out["metrics"]) == ["peak_signal"]

    @pytest.mark.parametrize("args", [
        {"metrics": ["bogus"]}, {"metrics": []},
        {"metrics": ["signal", "noise", "messages", "ac_adsb", "ac_mlat", "cpu_demod", "peak_signal"]},
        {"period": "8d"}, {"period": "all"},
    ])
    def test_receiver_metrics_errors(self, mcp, args):
        mcp.err("receiver_metrics", args)


# ---- watchlist --------------------------------------------------------------
class TestWatchlist:
    def test_add_list_duplicate_remove(self, mcp, caplog):
        caplog.set_level(logging.INFO, logger=SERVER_LOG)
        assert mcp.call("watchlist_list") == {"count": 0, "entries": []}
        added = mcp.call("watchlist_add", {"match_type": "icao", "value": " 48C1EA ",
                                           "label": "My plane"})
        assert added["status"] == "added"
        assert added["entry"]["value"] == "48c1ea" and added["entry"]["label"] == "My plane"
        eid = added["entry"]["id"]
        listed = mcp.call("watchlist_list")
        assert listed["count"] == 1
        e = listed["entries"][0]
        assert e["id"] == eid and e["match_type"] == "icao" and e["airborne"] is False
        assert e["created_at"].startswith(str(time.localtime().tm_year))

        again = mcp.call("watchlist_add", {"match_type": "icao", "value": "48c1ea", "label": "Other"})
        assert again["status"] == "exists" and again["entry"]["label"] == "My plane"
        assert again["entry"]["id"] == eid
        assert mcp.call("watchlist_list")["count"] == 1

        removed = mcp.call("watchlist_remove", {"match_type": "icao", "value": "48C1EA"})
        assert removed["status"] == "removed"
        assert removed["entry"]["id"] == eid and removed["entry"]["label"] == "My plane"
        assert mcp.call("watchlist_list") == {"count": 0, "entries": []}

        msgs = [r.getMessage() for r in caplog.records if r.name == SERVER_LOG]
        assert any("mcp write client=rw tool=watchlist_add match_type=icao value='48c1ea'"
                   " label='My plane'" in m for m in msgs)
        assert any(f"mcp write client=rw tool=watchlist_remove id={eid} match_type=icao"
                   " value='48c1ea' label='My plane'" in m for m in msgs)
        assert sum("tool=watchlist_add" in m for m in msgs) == 1       # 'exists' is not a write

    def test_airborne_flag(self, mcp):
        mcp.call("watchlist_add", {"match_type": "icao", "value": "3c6444"})
        assert mcp.call("watchlist_list")["entries"][0]["airborne"] is True

    def test_other_match_types(self, mcp):
        assert mcp.call("watchlist_add", {"match_type": "registration", "value": "SP-LWA"})["entry"]["value"] == "sp-lwa"
        assert mcp.call("watchlist_add", {"match_type": "callsign_prefix", "value": "LOT"})["status"] == "added"
        assert mcp.call("watchlist_list")["count"] == 2

    def test_readonly_token_cannot_write(self, mcp):
        for tool, args in [("watchlist_add", {"match_type": "icao", "value": "48c1ea"}),
                           ("watchlist_remove", {"match_type": "icao", "value": "48c1ea"})]:
            assert "lacks the 'watchlist' scope" in mcp.err(tool, args, token="tok-ro")
        assert mcp.call("watchlist_list", token="tok-ro")["count"] == 0

    def test_remove_missing(self, mcp):
        assert "no watchlist entry for icao 48c1ea" in mcp.err(
            "watchlist_remove", {"match_type": "icao", "value": "48c1ea"})

    def test_remove_requires_exact_match_type(self, mcp):
        mcp.call("watchlist_add", {"match_type": "registration", "value": "abc"})
        mcp.err("watchlist_remove", {"match_type": "callsign_prefix", "value": "abc"})
        assert mcp.call("watchlist_list")["count"] == 1

    @pytest.mark.parametrize("args", [
        {"match_type": "callsign_prefix", "value": "ab"},
        {"match_type": "callsign_prefix", "value": " a "},
        {"match_type": "icao", "value": "12345"},
        {"match_type": "icao", "value": "zzzzzz"},
        {"match_type": "registration", "value": "bad value!"},
        {"match_type": "callsign_prefix", "value": "toolongprefix"},
        {"match_type": "other", "value": "abc"},
        {"match_type": "icao", "value": ""},
        {"match_type": "icao", "value": "x" * 65},
        {"match_type": "icao", "value": "48c1ea", "label": "L" * 256},
    ])
    def test_invalid_adds(self, mcp, args):
        mcp.err("watchlist_add", args)
        assert mcp.call("watchlist_list")["count"] == 0

    def test_watchlist_max(self, mcp, monkeypatch):
        monkeypatch.setattr(config, "MCP_WATCHLIST_MAX", 1)
        mcp.call("watchlist_add", {"match_type": "registration", "value": "aaa"})
        assert "full" in mcp.err("watchlist_add", {"match_type": "registration", "value": "bbb"})
        assert mcp.call("watchlist_list")["count"] == 1
        # an existing entry is still reported as existing when the list is full
        assert mcp.call("watchlist_add", {"match_type": "registration", "value": "aaa"})["status"] == "exists"

    def test_write_budget(self, api):
        lim = generous_limiter(write_per_hour=1)
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("watchlist_add", {"match_type": "registration", "value": "aaa"})
            assert "rate limit" in s.err("watchlist_add", {"match_type": "registration", "value": "bbb"})
            s.call("watchlist_list")

    def test_label_control_chars_are_cleaned_before_sending(self, mcp):
        out = mcp.call("watchlist_add", {"match_type": "icao", "value": "48c1ea",
                                         "label": "ab\x00c\x1b[0m"})
        assert "\x00" not in out["entry"]["label"] and "\x1b" not in out["entry"]["label"]

    def test_empty_label_is_none(self, mcp):
        out = mcp.call("watchlist_add", {"match_type": "icao", "value": "48c1ea", "label": "  "})
        assert "label" not in out["entry"]

    def test_conflict_race_returns_existing(self, db, api, monkeypatch):
        """The API answers 409 after our pre-check saw no entry: return the stored row."""
        mcp_app = build_app(TOKENS, api=api, limiter=generous_limiter())
        db.execute("INSERT INTO watchlist (match_type, value, label, created_at) VALUES"
                   " ('icao','48c1ea','Stored',1700000000)")
        db.commit()
        orig = api.call
        calls = {"n": 0}

        async def stale_first_list(route, **kw):
            if route == "watchlist_list" and calls["n"] == 0:
                calls["n"] += 1
                return {"entries": []}
            return await orig(route, **kw)
        monkeypatch.setattr(api, "call", stale_first_list)
        with TestClient(mcp_app, base_url=BASE_URL) as c:
            out = Session(c).call("watchlist_add", {"match_type": "icao", "value": "48c1ea",
                                                    "label": "New"})
        assert out["status"] == "exists" and out["entry"]["label"] == "Stored"


# ---- cross-cutting ----------------------------------------------------------
ALL_CALLS = [
    ("search_flights", {"period": "all"}), ("search_flights", {"airport": "EPWA"}),
    ("get_flight", "FID"), ("flight_profile", "FID"), ("aircraft_history", {"icao_hex": "48c1ea"}),
    ("airline_flights", {"prefix": "LOT"}), ("type_flights", {"aircraft_type": "B738"}),
    ("aircraft_photo", {"icao_hex": "48c1ea"}), ("flagged_aircraft", {}), ("live_aircraft", {}),
    ("sky_at", {"time": "now"}), ("stats", {}),
    ("stats", {"period": "all", "sections": ["summary", "top_airlines", "top_aircraft_types",
                                              "top_routes", "top_airports", "top_countries",
                                              "frequent_aircraft", "hourly_distribution",
                                              "daily_unique_aircraft", "altitude_distribution",
                                              "source_breakdown", "squawk_counts", "new_aircraft",
                                              "furthest_aircraft", "trends", "heatmap"]}),
    ("records", {}), ("daily_counts", {}), ("range_by_bearing", {}), ("receiver_status", {}),
    ("receiver_metrics", {}), ("watchlist_list", {}),
    ("watchlist_add", {"match_type": "icao", "value": "48c1ea", "label": "x"}),
    ("watchlist_remove", {"match_type": "icao", "value": "48c1ea"}),
]


class TestCrossCutting:
    def test_every_tool_called_and_no_location_leaks(self, mcp):
        fid = mcp.call("search_flights", {"period": "all"})["flights"][0]["id"]
        seen = set()
        for name, args in ALL_CALLS:
            if args == "FID":
                args = {"flight_id": fid}
            res = mcp.raw(name, args)
            text = res["content"][0]["text"]
            assert not res.get("isError"), f"{name}: {text}"
            seen.add(name)
            assert "structuredContent" in res
            structured = json.dumps(res["structuredContent"])
            for bad in FORBIDDEN:
                assert bad not in text, f"{name} leaked {bad!r}"
                assert bad not in structured, f"{name} leaked {bad!r} in structuredContent"
            leaked = coordinate_keys(res["structuredContent"])
            assert not leaked, f"{name} returned absolute coordinates: {leaked}"
        assert len(seen) == 19

    def test_text_is_compact_json(self, mcp):
        res = mcp.raw("records")
        text = res["content"][0]["text"]
        assert "\n" not in text and ": " not in text and ", " not in text

    def test_rate_limit(self, api):
        lim = generous_limiter(read_per_min=2)
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("records")
            s.call("records")
            assert "rate limit" in s.err("records")
            s.call("records", token="tok-ro")                  # another client has its own bucket

    def test_one_token_per_call_even_with_several_api_calls(self, api):
        lim = generous_limiter(read_per_min=2)
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("receiver_status")                         # three API calls inside
            s.call("receiver_status")
            assert "rate limit" in s.err("receiver_status")

    def test_rate_limit_error_does_not_reach_api(self, api, monkeypatch):
        calls = []
        orig = api.call

        async def spy(route, **kw):
            calls.append(route)
            return await orig(route, **kw)
        monkeypatch.setattr(api, "call", spy)
        lim = generous_limiter(read_per_min=1)
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s = Session(c)
            s.call("records")
            s.err("records")
        assert calls == ["records"]

    def test_server_busy(self, api, monkeypatch):
        lim = generous_limiter(concurrency=1, acquire_timeout=0.05)

        async def slow(route, **kw):
            import asyncio
            await asyncio.sleep(0.5)
            return {}
        monkeypatch.setattr(api, "call", slow)
        import threading
        with TestClient(build_app(TOKENS, api=api, limiter=lim), base_url=BASE_URL) as c:
            s1, s2 = Session(c), Session(c)
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("a", s1.raw("records")))
            t.start()
            time.sleep(0.15)
            text = s2.err("live_aircraft")
            t.join()
        assert "busy" in text

    def test_untrusted_text_is_cleaned_in_output(self, mcp, db):
        db.execute("UPDATE flights SET callsign = ? WHERE callsign = 'LOT999'",
                   ("A\x00B\x1b" + "Z" * 120,))
        db.commit()
        out = mcp.call("search_flights")
        cs = [f["callsign"] for f in out["flights"] if f["callsign"].startswith("AB")][0]
        assert "\x00" not in cs and "\x1b" not in cs and len(cs) <= 80

    def test_response_is_capped(self, mcp, db, monkeypatch):
        from readsbstats.mcp_server import shaping
        monkeypatch.setattr(shaping, "MAX_RESPONSE_BYTES", 1500)
        out = mcp.call("search_flights", {"period": "all", "limit": 50})
        assert out["truncated"] is True and out["count"] > len(out["flights"]) >= 0

    def test_api_down_is_tool_error_not_crash(self, db):
        def handler(request):
            raise httpx.ConnectError("no", request=request)
        api = ApiClient("http://127.0.0.1:8080",
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TestClient(build_app(TOKENS, api=api, limiter=generous_limiter()), base_url=BASE_URL) as c:
            assert "unreachable" in Session(c).err("records")
