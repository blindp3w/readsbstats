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
