"""Tests for the RTE (Teledyne route message) filed-route parser."""
from __future__ import annotations

from readsbstats.vdl2 import rte


# Real first-lines from the live LOT/EPWA feed (public broadcast data).
T1B_RTE = "#T1BRTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF BCG59-U000-08E7 BCG38-0MFC-0017 L 1237 05JUN26"
BARE_RTE = "RTE 1 14JUN26 2124 SP-LVA LOT3HM EPWA/EVRA BCG59-U000-08E7 BCG38-0MFC-0017 L 2110 14JUN26"


class TestParseRoute:
    def test_t1b_prefixed_rte(self):
        assert rte.parse_route(T1B_RTE) == {
            "dep": "EPWA", "arr": "EDDF",
            "company_route": "BCG59-U000-08E7 BCG38-0MFC-0017",
        }

    def test_bare_rte(self):
        assert rte.parse_route(BARE_RTE) == {
            "dep": "EPWA", "arr": "EVRA",
            "company_route": "BCG59-U000-08E7 BCG38-0MFC-0017",
        }

    def test_multiline_uses_first_line_only(self):
        body = T1B_RTE + "\nNCMM\nMSG 3457901 A 1237 05JUN26 ES H"
        out = rte.parse_route(body)
        assert out["dep"] == "EPWA" and out["arr"] == "EDDF"
        assert "\n" not in out["company_route"]

    def test_route_without_l_marker(self):
        # No trailing ' L <time>' — company_route is the remainder of the line.
        out = rte.parse_route("RTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF BCG59-U000-08E7")
        assert out == {"dep": "EPWA", "arr": "EDDF", "company_route": "BCG59-U000-08E7"}

    def test_no_company_route(self):
        # dep/arr present but nothing after → company_route omitted, still valid.
        out = rte.parse_route("RTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF")
        assert out == {"dep": "EPWA", "arr": "EDDF"}

    def test_company_route_keeps_non_time_L_substring(self):
        # The trailing-bookkeeping trim is digit-anchored (' L <time>'); a ' L X'
        # that is NOT a time must survive. Locks the \s+L\s+\d contract so a future
        # regex change can't silently truncate a valid company route mid-string.
        out = rte.parse_route("RTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF SEGX L FOO BAR")
        assert out["company_route"] == "SEGX L FOO BAR"


class TestRejects:
    def test_t1b_non_route_base64(self):
        assert rte.parse_route("#T1B7KPVaZT4Pszh2wW5UVGnH7WK2f/qybtFX5rPf1y75Mod5u") is None

    def test_t1b_empty(self):
        assert rte.parse_route("#T1B-\n\rEOR") is None

    def test_rte_without_dep_arr_pair(self):
        assert rte.parse_route("RTE 1 05JUN26 1306 SP-LVS LOT377 no airports here") is None

    def test_non_rte_body(self):
        assert rte.parse_route("#M1BPOSN52081E020017") is None

    def test_non_string(self):
        assert rte.parse_route(None) is None
        assert rte.parse_route(123) is None
