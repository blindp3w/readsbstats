"""Tests for readsbstats.mcp_server.shaping (ADR-0022, Decision 6)."""

import json

import pytest

from readsbstats import config, geo
from readsbstats.mcp_server import shaping, timeparse

RLAT, RLON = 52.1234, 21.4321


@pytest.fixture(autouse=True)
def setup():
    yield


def compact(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


FULL_FLIGHT_ROW = {
    "id": 42, "icao_hex": "48c1ea", "callsign": "LOT123", "registration": "SP-LWA",
    "aircraft_type": "B738", "type_desc": "BOEING 737-800", "flags": 3, "squawk": "1234",
    "category": "A3", "primary_source": "adsb", "first_seen": 1_700_000_000,
    "last_seen": 1_700_003_600, "duration_sec": 3600, "max_alt_baro": 35000, "max_gs": 451.6,
    "max_distance_nm": 123.456, "total_positions": 900, "adsb_positions": 800,
    "mlat_positions": 100, "lat_min": 51.0, "lat_max": 53.0, "lon_min": 20.0, "lon_max": 22.0,
    "origin_icao": "EPWA", "dest_icao": "EGLL", "origin_name": "Chopin", "origin_country": "PL",
    "dest_name": "Heathrow", "dest_country": "GB", "has_acars": 1, "airline_name": "LOT Polish",
    "max_distance_bearing": 77.0, "something_new": "leak",
}
ALLOWED = {"id", "icao_hex", "callsign", "registration", "aircraft_type", "type_desc", "flags",
           "squawk", "primary_source", "first_seen", "last_seen", "duration_min", "max_alt_ft",
           "max_gs_kt", "max_distance_nm", "origin_icao", "origin_name", "dest_icao",
           "dest_name", "has_acars", "airline_name"}


class TestFlags:
    def test_names(self):
        assert shaping.FLAG_NAMES == {
            config.FLAG_MILITARY: "military", config.FLAG_INTERESTING: "interesting",
            config.FLAG_PIA: "pia", config.FLAG_LADD: "ladd", config.FLAG_ANONYMOUS: "anonymous"}

    @pytest.mark.parametrize("n,want", [
        (0, []), (None, []), (1, ["military"]), (3, ["military", "interesting"]),
        (31, ["military", "interesting", "pia", "ladd", "anonymous"]), (16, ["anonymous"]),
        ("x", []), (True, []),
    ])
    def test_decode(self, n, want):
        assert shaping.decode_flags(n) == want


class TestFlightRow:
    def test_only_allowed_keys(self):
        out = shaping.flight_row(FULL_FLIGHT_ROW)
        assert set(out) <= ALLOWED
        for k in ("lat_min", "lat_max", "lon_min", "lon_max", "category", "something_new",
                  "max_distance_bearing", "total_positions", "origin_country"):
            assert k not in out

    def test_values(self):
        out = shaping.flight_row(FULL_FLIGHT_ROW)
        assert out["id"] == 42 and out["icao_hex"] == "48c1ea"
        assert out["flags"] == ["military", "interesting"]
        assert out["duration_min"] == 60
        assert out["max_alt_ft"] == 35000
        assert out["max_gs_kt"] == 452
        assert out["max_distance_nm"] == 123.5
        assert out["first_seen"] == timeparse.to_local_iso(1_700_000_000)
        assert out["last_seen"] == timeparse.to_local_iso(1_700_003_600)
        assert out["origin_icao"] == "EPWA" and out["dest_name"] == "Heathrow"
        assert out["has_acars"] is True

    def test_none_values_omitted_and_has_acars_optional(self):
        out = shaping.flight_row({"id": 1, "icao_hex": "abcdef", "callsign": None, "flags": 0,
                                  "first_seen": 1_700_000_000, "last_seen": 1_700_000_060})
        assert "callsign" not in out and "has_acars" not in out
        assert "flags" not in out
        assert out["duration_min"] == 1

    def test_untrusted_strings_cleaned(self):
        row = dict(FULL_FLIGHT_ROW, callsign="AB\x00C\x1b[31m" + "X" * 200,
                   type_desc="  desc\x07  ", origin_name="N" * 500, registration="\x00\x01")
        out = shaping.flight_row(row)
        assert "\x00" not in out["callsign"] and "\x1b" not in out["callsign"]
        assert len(out["callsign"]) <= 80
        assert out["type_desc"] == "desc"
        assert len(out["origin_name"]) == 80
        assert "registration" not in out

    def test_bad_icao_dropped(self):
        assert "icao_hex" not in shaping.flight_row(dict(FULL_FLIGHT_ROW, icao_hex="../etc"))

    def test_not_a_dict(self):
        assert shaping.flight_row(None) is None


class TestAircraftInfo:
    def test_allowlist(self):
        out = shaping.aircraft_info({
            "registration": "SP-LWA", "type_code": "B738", "type_desc": "B737", "flags": 1,
            "total_flights": 5, "first_seen": 1_700_000_000, "last_seen": 1_700_003_600,
            "total_duration_sec": 7200, "country": "Poland", "extra": "x"})
        assert out == {"registration": "SP-LWA", "type": "B738", "type_desc": "B737",
                       "flags": ["military"], "total_flights": 5,
                       "first_seen": timeparse.to_local_iso(1_700_000_000),
                       "last_seen": timeparse.to_local_iso(1_700_003_600),
                       "total_hours": 2.0, "country": "Poland"}


class TestLiveAircraft:
    def test_distance_bearing_and_rounding(self):
        a = {"icao_hex": "48c1ea", "flight_id": 7, "last_seen": 1, "callsign": "LOT1",
             "registration": "SP-A", "aircraft_type": "B738", "flags": 0, "primary_source": "adsb",
             "origin_icao": "EPWA", "dest_icao": "EGLL", "lat": 52.5678912, "lon": 21.9876543,
             "seconds_ago": 4}
        out = shaping.live_aircraft(a, RLAT, RLON)
        d = geo.haversine_nm(RLAT, RLON, 52.5678912, 21.9876543)
        b = geo.bearing(RLAT, RLON, 52.5678912, 21.9876543)
        assert out["distance_nm"] == round(d, 1)
        assert out["bearing_deg"] == round(b) % 360
        assert "lat" not in out and "lon" not in out
        assert out["seconds_ago"] == 4 and out["callsign"] == "LOT1"
        assert "receiver_lat" not in out and "last_seen" not in out

    def test_snapshot_fields(self):
        a = {"flight_id": 1, "ts": 5, "lat": 52.0, "lon": 21.0, "alt_baro": 12000, "gs": 301.26,
             "track": 181.4, "source_type": "adsb_icao", "icao_hex": "48c1ea", "callsign": None,
             "flags": 1, "seconds_ago": 12, "trail": [[1, 2, 3]], "category": "A3"}
        out = shaping.live_aircraft(a, RLAT, RLON)
        assert out["alt_ft"] == 12000 and out["gs_kt"] == 301 and out["track_deg"] == 181
        assert out["flags"] == ["military"]
        assert "trail" not in out and "callsign" not in out and "category" not in out
        assert "lat" not in out and "lon" not in out

    def test_missing_receiver_or_position(self):
        out = shaping.live_aircraft({"icao_hex": "48c1ea", "lat": None, "lon": None}, RLAT, RLON)
        assert "distance_nm" not in out and "lat" not in out
        out = shaping.live_aircraft({"icao_hex": "48c1ea", "lat": 1.0, "lon": 1.0}, None, None)
        assert "distance_nm" not in out and "lat" not in out and "lon" not in out

    def test_shape_sky_sorts_by_distance_and_limits(self):
        raw = {"receiver_lat": RLAT, "receiver_lon": RLON, "aircraft": [
            {"icao_hex": "000001", "lat": RLAT + 3.0, "lon": RLON},
            {"icao_hex": "000002", "lat": RLAT + 1.0, "lon": RLON},
            {"icao_hex": "000003", "lat": None, "lon": None},
            {"icao_hex": "000004", "lat": RLAT + 2.0, "lon": RLON}]}
        out = shaping.shape_sky(raw, limit=3)
        assert [a["icao_hex"] for a in out["aircraft"]] == ["000002", "000004", "000001"]
        assert out["count"] == 4 and out["truncated"] is True
        out = shaping.shape_sky(raw, limit=10)
        assert [a["icao_hex"] for a in out["aircraft"]][-1] == "000003"
        assert "truncated" not in out
        assert "receiver_lat" not in out
        assert all("lat" not in a and "lon" not in a for a in out["aircraft"])

    def test_shape_sky_extras(self):
        out = shaping.shape_sky({"receiver_lat": RLAT, "receiver_lon": RLON, "aircraft": [],
                                 "at": 1_700_000_000, "is_live": False}, limit=5)
        assert out["at"] == timeparse.to_local_iso(1_700_000_000) and out["is_live"] is False
        assert out["count"] == 0 and out["aircraft"] == []


class TestLocated:
    AC = {"icao_hex": "48c1ea", "callsign": "LOT1", "lat": 52.5678912, "lon": 21.9876543}

    def test_live_aircraft_with_location(self):
        out = shaping.live_aircraft(self.AC, RLAT, RLON, with_location=True)
        assert out["lat"] == 52.5679 and out["lon"] == 21.9877
        assert out["distance_nm"] > 0 and "receiver_lat" not in out

    def test_live_aircraft_without_location(self):
        out = shaping.live_aircraft(self.AC, RLAT, RLON)
        assert "lat" not in out and "lon" not in out
        out = shaping.live_aircraft(self.AC, RLAT, RLON, with_location=False)
        assert "lat" not in out and "lon" not in out

    @pytest.mark.parametrize("lat,lon", [(None, 21.0), (52.0, None), (91.0, 21.0),
                                         (52.0, 181.0), ("x", "y"), (True, 21.0),
                                         (float("nan"), 21.0)])
    def test_live_aircraft_invalid_position_adds_nothing(self, lat, lon):
        out = shaping.live_aircraft({"icao_hex": "48c1ea", "lat": lat, "lon": lon},
                                    RLAT, RLON, with_location=True)
        assert "lat" not in out and "lon" not in out

    def test_shape_sky_with_location(self):
        raw = {"receiver_lat": RLAT, "receiver_lon": RLON, "aircraft": [self.AC]}
        out = shaping.shape_sky(raw, limit=5, with_location=True)
        assert out["receiver"] == {"lat": 52.1234, "lon": 21.4321}
        assert out["aircraft"][0]["lat"] == 52.5679
        assert "receiver_lat" not in out and "receiver_lon" not in out

    def test_shape_sky_receiver_is_rounded(self):
        out = shaping.shape_sky({"receiver_lat": 52.123456, "receiver_lon": 21.432149,
                                 "aircraft": []}, limit=5, with_location=True)
        assert out["receiver"] == {"lat": 52.1235, "lon": 21.4321}

    def test_shape_sky_without_location(self):
        raw = {"receiver_lat": RLAT, "receiver_lon": RLON, "aircraft": [self.AC]}
        for out in (shaping.shape_sky(raw, limit=5),
                    shaping.shape_sky(raw, limit=5, with_location=False)):
            assert "receiver" not in out
            assert "lat" not in out["aircraft"][0] and "lon" not in out["aircraft"][0]

    @pytest.mark.parametrize("rlat,rlon", [(None, RLON), (RLAT, None), (200.0, RLON),
                                           (RLAT, 200.0), (None, None), ("x", RLON)])
    def test_shape_sky_invalid_receiver_is_omitted(self, rlat, rlon):
        out = shaping.shape_sky({"receiver_lat": rlat, "receiver_lon": rlon,
                                 "aircraft": [self.AC]}, limit=5, with_location=True)
        assert "receiver" not in out

    def test_shape_profile_with_location(self):
        raw = {"total": 2, "positions": [
            {"ts": 1_700_000_000, "lat": 52.123456, "lon": 21.654321, "alt_baro": 1000},
            {"ts": 1_700_000_060, "lat": None, "lon": 21.0, "alt_baro": 2000}]}
        p0, p1 = shaping.shape_profile(raw, with_location=True)["points"]
        assert p0["lat"] == 52.1235 and p0["lon"] == 21.6543 and p0["alt_ft"] == 1000
        assert "lat" not in p1 and "lon" not in p1
        for p in shaping.shape_profile(raw)["points"]:
            assert "lat" not in p and "lon" not in p

    def test_located_200_rows_truncate_to_the_nearest(self):
        aircraft = [{"icao_hex": f"{i:06x}", "flight_id": 1000 + i, "callsign": f"LOT{i:04d}",
                     "registration": f"SP-L{i:03d}", "aircraft_type": "B738", "flags": 0,
                     "origin_icao": "EPWA", "dest_icao": "EGLL", "primary_source": "adsb",
                     "seconds_ago": i, "lat": 52.0 + i * 0.01234567, "lon": 21.0 + i * 0.01234567}
                    for i in range(200)]
        full = shaping.shape_sky({"receiver_lat": RLAT, "receiver_lon": RLON,
                                  "aircraft": aircraft}, limit=200, with_location=True)
        nearest = [a["icao_hex"] for a in full["aircraft"]]
        out = shaping.cap_size(full)
        # 200 located rows exceed the cap (~49 KB): the tail is cut, the nearest rows survive.
        assert out["truncated"] is True
        assert shaping._size(out) <= shaping.MAX_RESPONSE_BYTES
        kept = [a["icao_hex"] for a in out["aircraft"]]
        assert 100 <= len(kept) < 200
        assert kept == nearest[:len(kept)]
        assert all("lat" in a and "lon" in a for a in out["aircraft"])


class TestStripLocation:
    def test_keep_lat_lon_receiver_only(self):
        obj = {"receiver": {"lat": 1.0, "lon": 2.0}, "receiver_lat": 1, "receiver_lon": 2,
               "aircraft": [{"lat": 3.0, "lon": 4.0, "lat_min": 5, "lat_max": 6, "lon_min": 7,
                             "lon_max": 8, "latitude": 9, "longitude": 10, "ok": 1}]}
        keep = frozenset({"lat", "lon", "receiver"})
        assert shaping.strip_location(obj, keep=keep) == {
            "receiver": {"lat": 1.0, "lon": 2.0}, "aircraft": [{"lat": 3.0, "lon": 4.0, "ok": 1}]}

    def test_keep_does_not_retain_other_keys(self):
        obj = {"receiver": {"lat": 1.0}, "receiver_lat": 1, "lat_min": 2, "a": {"lon_max": 3}}
        out = shaping.strip_location(obj, keep=frozenset({"lat", "lon", "receiver"}))
        assert out == {"receiver": {"lat": 1.0}, "a": {}}

    def test_default_strips_receiver_and_lat_lon(self):
        obj = {"receiver": {"lat": 1.0, "lon": 2.0}, "x": {"lat": 1, "lon": 2, "ok": 1}}
        assert shaping.strip_location(obj) == {"x": {"ok": 1}}

    def test_recursive(self):
        obj = {"receiver_lat": 1, "a": [{"lat_min": 1, "keep": 2, "x": {"lon_max": 3, "ok": 1}}],
               "b": {"receiver_lon": 1, "lat_max": 2, "lon_min": 3, "z": [1, {"lon_max": 4}]}}
        assert shaping.strip_location(obj) == {"a": [{"keep": 2, "x": {"ok": 1}}],
                                               "b": {"z": [1, {}]}}

    def test_scalars_pass(self):
        assert shaping.strip_location(5) == 5 and shaping.strip_location("x") == "x"
        assert shaping.strip_location(None) is None


class TestCapSize:
    def test_small_unchanged(self):
        p = {"a": [1, 2, 3]}
        assert shaping.cap_size(p) == {"a": [1, 2, 3]}

    def test_truncates_longest_list(self):
        p = {"total": 9999, "short": [1, 2], "flights": [{"id": i, "s": "x" * 40} for i in range(2000)]}
        out = shaping.cap_size(p, max_bytes=5000)
        assert len(compact(out).encode()) <= 5000
        assert out["truncated"] is True
        assert out["short"] == [1, 2]
        assert 0 < len(out["flights"]) < 2000
        assert out["flights"][0]["id"] == 0           # head is kept
        assert out["total"] == 9999

    def test_nested_list_trimmed(self):
        p = {"a": {"rows": list(range(10000))}}
        out = shaping.cap_size(p, max_bytes=2000)
        assert len(compact(out).encode()) <= 2000 and out["truncated"] is True

    def test_default_cap_is_48k(self):
        out = shaping.cap_size({"rows": ["x" * 100] * 2000})
        assert len(compact(out).encode()) <= 48_000 and out["truncated"] is True

    def test_unshrinkable_payload_gets_error_stub(self):
        out = shaping.cap_size({"blob": "x" * 5000}, max_bytes=1000)
        assert out["truncated"] is True and len(compact(out)) <= 1000


class TestStats:
    RAW = {
        "total_flights": 120, "total_positions": 5000, "unique_aircraft": 80,
        "unique_airlines": 12, "db_size_bytes": 12345, "oldest_flight": 1_700_000_000,
        "flights_last_24h": 10, "flights_last_7d": 70,
        "source_breakdown": {"adsb": 90.0, "mlat": 5.0, "other": 5.0},
        "top_airlines": [{"airline": "LOT", "airline_name": "LOT Polish\x00", "flights": 50,
                          "unique_aircraft": 9, "junk": 1}] * 15,
        "top_aircraft_types": [{"type": "B738", "type_desc": "B737-800", "flights": 40,
                                "unique_aircraft": 8}] * 12,
        "top_routes": [{"origin_icao": "EPWA", "dest_icao": "EGLL", "origin_name": "Chopin",
                        "dest_name": "Heathrow", "flights": 7}] * 11,
        "top_airports": [{"icao_code": "EPWA", "name": "Chopin", "country": "PL",
                          "appearances": 30}] * 20,
        "hourly_distribution": [{"hour": h, "count": h} for h in range(24)],
        "daily_unique_aircraft": [{"day": "2026-10-01", "unique_aircraft": 5, "flights": 6}],
        "altitude_distribution": [{"band": "40k+", "count": 3}],
        "military_flights": 4, "interesting_flights": 2, "anonymous_flights": 1,
        "squawk_counts": {"7700": 0, "7600": 1, "7500": 0},
        "new_aircraft": {"total": 3, "items": [{"icao_hex": "48c1ea", "registration": "SP-A",
                                                "aircraft_type": "B738", "type_desc": "x",
                                                "flags": 1, "first_seen_ever": 1_700_000_000}]},
        "furthest_aircraft": dict(FULL_FLIGHT_ROW, record_set_at=1_700_000_000),
        "receiver_lat": RLAT, "receiver_lon": RLON,
        "trends": {"flights_24h_prev": 9, "flights_7d_prev": 60},
        "previous_window": {"from_ts": 1, "to_ts": 2, "total_flights": 3, "total_positions": 4,
                            "unique_aircraft": 5},
        "lifetime": {"total_flights": 999}, "heatmap": [{"dow": 1, "hour": 2, "count": 3}],
        "top_countries": [{"country": "Poland", "flights": 9, "unique_aircraft": 3}],
        "frequent_aircraft": [{"icao_hex": "48c1ea", "registration": "SP-A",
                               "aircraft_type": "B738", "type_desc": "x", "flags": 0,
                               "flights": 9, "last_seen": 1_700_000_000}],
        "range": None,
    }

    def test_default_sections(self):
        out = shaping.shape_stats(dict(self.RAW), None)
        assert out["total_flights"] == 120 and out["unique_aircraft"] == 80
        assert out["unique_airlines"] == 12 and out["total_positions"] == 5000
        assert out["military_flights"] == 4
        assert out["oldest_flight"] == timeparse.to_local_iso(1_700_000_000)
        for sec in ("top_airlines", "top_aircraft_types", "top_routes", "top_airports"):
            assert len(out[sec]) == 10
        for absent in ("hourly_distribution", "heatmap", "receiver_lat", "receiver_lon",
                       "db_size_bytes", "lifetime", "range", "furthest_aircraft"):
            assert absent not in out
        assert out["top_airlines"][0] == {"airline": "LOT", "airline_name": "LOT Polish",
                                          "flights": 50, "unique_aircraft": 9}

    def test_requested_sections_only(self):
        out = shaping.shape_stats(dict(self.RAW), ["hourly_distribution", "furthest_aircraft",
                                                   "new_aircraft", "frequent_aircraft",
                                                   "top_countries", "trends"])
        assert set(out) == {"hourly_distribution", "furthest_aircraft", "new_aircraft",
                            "frequent_aircraft", "top_countries", "trends"}
        assert len(out["hourly_distribution"]) == 24
        assert set(out["furthest_aircraft"]) <= ALLOWED | {"record_set_at"}
        assert out["furthest_aircraft"]["record_set_at"] == timeparse.to_local_iso(1_700_000_000)
        assert out["new_aircraft"]["total"] == 3
        assert out["new_aircraft"]["items"][0]["first_seen"] == timeparse.to_local_iso(1_700_000_000)
        assert out["new_aircraft"]["items"][0]["flags"] == ["military"]
        assert out["frequent_aircraft"][0]["last_seen"] == timeparse.to_local_iso(1_700_000_000)
        assert out["trends"]["flights_24h_prev"] == 9

    def test_unknown_section_is_error(self):
        with pytest.raises(ValueError, match="unknown section"):
            shaping.shape_stats(dict(self.RAW), ["receiver_lat"])

    def test_sections_allowlist_excludes_location(self):
        assert not {"receiver_lat", "receiver_lon", "range", "lifetime"} & set(shaping.STATS_SECTIONS)

    def test_summary_section_name(self):
        out = shaping.shape_stats(dict(self.RAW), ["summary"])
        assert set(out) >= {"total_flights", "unique_aircraft"}
        assert "top_airlines" not in out

    def test_missing_keys_tolerated(self):
        out = shaping.shape_stats({"total_flights": 1}, None)
        assert out["total_flights"] == 1

    def test_all_output_has_no_location_keys(self):
        out = shaping.shape_stats(dict(self.RAW), list(shaping.STATS_SECTIONS))
        txt = compact(out)
        for bad in ("receiver_lat", "receiver_lon", "lat_min", "lon_max"):
            assert bad not in txt


class TestMetrics:
    def test_summary_and_points(self):
        ts = list(range(1_700_000_000, 1_700_000_000 + 10 * 60, 60))
        raw = {"bucket_seconds": 0, "metrics": ["signal", "noise"],
               "data": [ts, [float(i) for i in range(10)], [-30.0] * 10]}
        out = shaping.shape_metrics(raw)
        s = out["metrics"]["signal"]
        assert (s["min"], s["max"], s["last"], s["avg"]) == (0.0, 9.0, 9.0, 4.5)
        assert s["points"][0] == [timeparse.to_local_iso(ts[0]), 0.0]
        assert len(s["points"]) == 10
        assert out["metrics"]["noise"]["avg"] == -30.0
        assert out["bucket_seconds"] == 0

    def test_downsample_to_48(self):
        n = 1000
        ts = list(range(1_700_000_000, 1_700_000_000 + n * 10, 10))
        raw = {"bucket_seconds": 0, "metrics": ["signal"], "data": [ts, [float(i) for i in range(n)]]}
        pts = shaping.shape_metrics(raw)["metrics"]["signal"]["points"]
        assert len(pts) == 48
        vals = [p[1] for p in pts]
        assert vals == sorted(vals)
        assert min(vals) < 20 and max(vals) > 980
        assert pts[0][0] == timeparse.to_local_iso(ts[0])

    def test_none_values_skipped(self):
        raw = {"bucket_seconds": 300, "metrics": ["signal"],
               "data": [[1, 2, 3, 4], [None, 2.0, None, 4.0]]}
        s = shaping.shape_metrics(raw)["metrics"]["signal"]
        assert s["avg"] == 3.0 and s["min"] == 2.0 and s["max"] == 4.0 and s["last"] == 4.0
        assert len(s["points"]) == 2

    def test_empty(self):
        raw = {"bucket_seconds": 0, "metrics": ["signal"], "data": [[], []]}
        s = shaping.shape_metrics(raw)["metrics"]["signal"]
        assert s == {"min": None, "avg": None, "max": None, "last": None, "points": []}
        assert shaping.shape_metrics({"bucket_seconds": 0, "metrics": [], "data": []})["metrics"] == {}


class TestMisc:
    def test_photo(self):
        raw = {"icao_hex": "48c1ea", "thumbnail_url": "https://t.example/a.jpg",
               "large_url": "http://insecure/a.jpg", "link_url": "https://l.example/x",
               "photographer": "Jan\x00 K", "fetched_at": 5, "is_type_photo": False,
               "type_code": None, "type_desc": None}
        out = shaping.shape_photo(raw)
        assert out == {"thumbnail_url": "https://t.example/a.jpg", "link_url": "https://l.example/x",
                       "photographer": "Jan K", "is_type_photo": False}

    def test_photo_none_or_no_urls(self):
        assert shaping.shape_photo(None) == {"photo": None}
        assert shaping.shape_photo({"thumbnail_url": "javascript:alert(1)"}) == {"photo": None}

    def test_photo_type(self):
        out = shaping.shape_photo({"thumbnail_url": "https://t.example/a.jpg",
                                   "is_type_photo": True, "type_code": "B738"})
        assert out["is_type_photo"] is True and out["type_code"] == "B738"

    def test_watchlist_entry(self):
        out = shaping.watchlist_entry({"id": 3, "match_type": "icao", "value": "48c1ea",
                                       "label": "My\x00 plane", "created_at": 1_700_000_000,
                                       "airborne": 1})
        assert out == {"id": 3, "match_type": "icao", "value": "48c1ea", "label": "My plane",
                       "created_at": timeparse.to_local_iso(1_700_000_000), "airborne": True}

    def test_watchlist_entry_minimal(self):
        out = shaping.watchlist_entry({"id": 3, "match_type": "icao", "value": "48c1ea",
                                       "label": None})
        assert out == {"id": 3, "match_type": "icao", "value": "48c1ea"}

    def test_profile(self):
        raw = {"total": 500, "target": 2, "positions": [
            {"ts": 1_700_000_000, "lat": 52.1, "lon": 21.1, "alt_baro": 1000, "alt_geom": 1100,
             "gs": 200.04, "track": 90.0, "baro_rate": 64, "source_type": "adsb_icao"},
            {"ts": 1_700_000_060, "lat": 52.2, "lon": 21.2, "alt_baro": None, "gs": None}]}
        out = shaping.shape_profile(raw)
        assert out["total_positions"] == 500
        p0, p1 = out["points"]
        assert p0 == {"time": timeparse.to_local_iso(1_700_000_000), "alt_ft": 1000, "gs_kt": 200,
                      "track_deg": 90, "vrate_fpm": 64, "source": "adsb_icao"}
        assert p1 == {"time": timeparse.to_local_iso(1_700_000_060)}
        assert "lat" not in compact(out) and "lon" not in compact(out)

    def test_feeders_no_details(self):
        raw = {"has_feeders": True, "feeders": [
            {"name": "FR24", "unit": "fr24feed", "systemd": "active", "port": 8754,
             "port_status": "open", "overall": "ok",
             "details": [["Radar code", "T-EPWA123"], ["MLAT server", "x"]]}]}
        out = shaping.shape_feeders(raw)
        assert out == [{"name": "FR24", "systemd": "active", "port_status": "open",
                        "overall": "ok"}]

    def test_health_checks(self):
        raw = {"overall": "warn", "as_of": 1_700_000_000, "checks": [
            {"name": "heartbeat", "severity": "warn", "message": "late\x00", "value": 5.5,
             "threshold": 3, "extra": 1}]}
        out = shaping.shape_health(raw)
        assert out == {"overall": "warn", "as_of": timeparse.to_local_iso(1_700_000_000),
                       "checks": [{"name": "heartbeat", "severity": "warn", "message": "late",
                                   "value": 5.5, "threshold": 3}]}

    def test_records(self):
        raw = {"furthest": dict(FULL_FLIGHT_ROW), "fastest": None, "highest": dict(FULL_FLIGHT_ROW),
               "longest": dict(FULL_FLIGHT_ROW, duration_s=3600)}
        out = shaping.shape_records(raw)
        assert out["fastest"] is None
        assert set(out["furthest"]) <= ALLOWED and set(out["longest"]) <= ALLOWED

    def test_daily_counts(self):
        raw = {"dates": [{"date": f"2026-10-{d:02d}", "flight_count": d} for d in range(30, 0, -1)]}
        out = shaping.shape_daily(raw, days=3)
        assert out["days"] == [{"date": "2026-10-30", "flights": 30},
                               {"date": "2026-10-29", "flights": 29},
                               {"date": "2026-10-28", "flights": 28}]

    def test_polar(self):
        raw = {"buckets": [{"bearing": 0, "max_dist_nm": 100.0}, {"bearing": 10, "max_dist_nm": 0.0}]}
        assert shaping.shape_polar(raw) == {"buckets": [
            {"bearing_deg": 0, "max_nm": 100.0}, {"bearing_deg": 10, "max_nm": 0.0}]}

    def test_flagged(self):
        raw = {"total": 7, "aircraft": [{
            "icao_hex": "48c1ea", "registration": "SP-A", "aircraft_type": "B738", "type_desc": "x",
            "flags": 17, "country": "Poland", "flight_count": 3, "first_seen": 1_700_000_000,
            "last_seen": 1_700_003_600, "thumbnail_url": "https://x/t.jpg", "photographer": "p"}]}
        out = shaping.shape_flagged(raw)
        assert out["total"] == 7 and out["count"] == 1
        a = out["aircraft"][0]
        assert a["flags"] == ["military", "anonymous"] and a["flight_count"] == 3
        assert "thumbnail_url" not in a and "photographer" not in a
