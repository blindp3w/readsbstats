"""Tests for the #M1BPOS parser (vdl2/m1bpos.py): ddmmm position + filed route.

Fixtures are real bodies from the live vdlm2dec LOT feed. #M1BPOS positions use
the ddmmm format (degrees + minutes), NOT decimal degrees — verified empirically
(minute field maxes at 595/596 over 83 samples) and against the airframes decoder.
"""
from __future__ import annotations

from readsbstats.vdl2 import m1bpos

# Real plain-position bodies (no /RP: route block).
POS1 = "#M1BPOSN52081E020017,N51491E019372,191139,370,N52416E020461,191735,BOKSU,M51,19155,1276064"
POS2 = "#M1BPOSN52084E020525,POL01,162815,370,ASL01,163417,ETU01,M56,193065,276EED7"


class TestParsePosition:
    def test_ddmmm_first_fixture(self):
        # N52081 -> 52 + 8.1/60 = 52.135 ; E020017 -> 20 + 1.7/60 = 20.02833
        assert m1bpos.parse_position(POS1) == {"lat": 52.135, "lon": 20.02833}

    def test_ddmmm_second_fixture(self):
        # N52084 -> 52 + 8.4/60 = 52.14 ; E020525 -> 20 + 52.5/60 = 20.875
        assert m1bpos.parse_position(POS2) == {"lat": 52.14, "lon": 20.875}

    def test_accepts_minute_field_599_boundary(self):
        # 599 = 59.9' is the largest valid ddmmm minute value (600 is rejected
        # by the test below). Lock the boundary: N52599 -> 52 + 59.9/60.
        assert m1bpos.parse_position("#M1BPOSN52599E020017,REST") == {
            "lat": round(52 + 59.9 / 60, 5),
            "lon": round(20 + 1.7 / 60, 5),
        }

    def test_rejects_minute_field_over_599(self):
        # 60.0' is not valid ddmmm — reject rather than mislocate.
        assert m1bpos.parse_position("#M1BPOSN52600E020017,REST") is None

    def test_rejects_non_m1bpos(self):
        assert m1bpos.parse_position("#M1BPRGSOMETHING") is None
        assert m1bpos.parse_position("LIMCEPMO1009") is None

    def test_south_west_hemisphere(self):
        # S34300 -> -(34 + 30.0/60) = -34.5 ; W018200 -> -(18 + 20.0/60) = -18.33333
        assert m1bpos.parse_position("#M1BPOSS34300W018200,rest") == {"lat": -34.5, "lon": -18.33333}

    def test_rejects_lon_minute_over_599(self):
        assert m1bpos.parse_position("#M1BPOSN52000E020600,rest") is None

    def test_rejects_empty_and_none(self):
        assert m1bpos.parse_position("") is None
        assert m1bpos.parse_position(None) is None


# Real #M1BPOS bodies carrying the full /RP: filed-route block.
RP_SID_STAR_APP = "#M1BPOSN52147E020507,WA931,134630,64,INRAS,134923,RODEV,M0,29924,106/DTEKCH,04L,64,145001,/PR1303,310,360,106,,0,4,,M56,65,,,M10,P0,36090,,1197,316/RP:DA:EPWA:AA:EKCH:R:29O(04L):D:OLIL6G:A:TIDV3A:AP:ILS04L..INR"
RP_COMPANY_ROUTE = "#M1BPOSN52081E020135,WA903,104850,155,DIBLO,105348,INDIG,M11,202031,194,73/RP:DA:EPWA:AA:EHAM:CR:OFP519(18R)..DIBLO..INDIG..ALUKA..PITEN..BUMIL..APNOC.Z45.OMEPA.N125.BLUFA:A:BLUF1A:F:ARTIP..SPL01..PEV01..PEVOS:"
RP_APP_AFTER_ENROUTE = "#M1BPOSN52086E019235,WA903,042142,277,NORKU,052401,SONSA,M37,190082,123,73/RP:DA:EPWA:AA:EHAM:CR:OFP537(27O)..NORKU:A:NORK2A:F:ARTIP..SPL01..TIDVO:AP:ILS 27.ARTIP:F:VECTOR/PR1339,248,400,123,,46,30,225023,M48,6"


class TestParseRoute:
    def test_sid_star_approach_no_company_route(self):
        assert m1bpos.parse_route(RP_SID_STAR_APP) == {
            "dep": "EPWA", "arr": "EKCH",
            "sid": "OLIL6G", "star": "TIDV3A", "approach": "ILS04L..INR",
        }

    def test_company_route_and_star(self):
        r = m1bpos.parse_route(RP_COMPANY_ROUTE)
        assert r["dep"] == "EPWA" and r["arr"] == "EHAM"
        assert r["company_route"].startswith("OFP519(18R)..DIBLO")
        assert r["company_route"].endswith("BLUFA")
        assert r["star"] == "BLUF1A"
        assert "sid" not in r and "approach" not in r

    def test_approach_after_trailing_enroute_segment(self):
        assert m1bpos.parse_route(RP_APP_AFTER_ENROUTE) == {
            "dep": "EPWA", "arr": "EHAM",
            "company_route": "OFP537(27O)..NORKU", "star": "NORK2A",
            "approach": "ILS 27.ARTIP",
        }

    def test_none_without_rp_block(self):
        assert m1bpos.parse_route(POS1) is None      # plain #M1BPOS, no /RP:
        assert m1bpos.parse_route("LIMCEPMO1009") is None
        assert m1bpos.parse_route("") is None
        assert m1bpos.parse_route(None) is None

    def test_none_when_arr_missing(self):
        # /RP: block with DA but no AA -> not a usable route
        assert m1bpos.parse_route("#M1BPOSN52000E020000,x/RP:DA:EPWA:D:OLIL6G") is None

    def test_none_when_dep_missing(self):
        assert m1bpos.parse_route("#M1BPOSN52000E020000,x/RP:AA:EHAM:A:NORK2A") is None
