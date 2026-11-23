"""Tests for vdl2/h1route.py: reported dep/dest from ABS/EB0/SB0 headers, B44, R64 and
label-15 FST01 bodies (ADR-0019 Batch 7). Bodies are real dumpvdl2 rows from the
2026-11-23 snapshot."""
from __future__ import annotations

import pytest

from readsbstats.vdl2 import h1route

ABS_CSV = ("ABS026BA_S      ,B7378MAX ,261123,LO4TP ,EPWA,LROP,0318,BCG27-S200-0011\r\n"
           "N5210.5,E02055.2,231035,01973,-02.8,286,026,IC,00000,0,\r\n")
ABS_FIXED_A = "ABS011DA_S       EPWALIRF4\r\n 52184 0208871128 02846-0232930310W0K2R118183"
ABS_FIXED_B = "ABS001DA_        EDDFEPWA490\r\n 52184 0208871128 02846"
EB0_FIXED = "EB0031AA_ SP-LRG EPWAKMIA1\r\n 52184 0208871128 02846-0232930310W0K2R118183"
B44 = ("B44C\r\nSP-LVH  4824HT EPWALPPTTO140926122650\r\n 995398160B70260B711BCG3C-L200-0004\r\n"
       "   341 -6631118 9550170   40   23157760")
B44_ARR = "B44C\r\nSP-LYI  2994EB EPWAENGMTA231126064744\r\n 998399460D21660D369BCG3C-L200-0004"
R64 = ("R641V0106,180302,, F-HPND,5318410-07,736661,736834,06164,EPWA,LFPG,17:43:03,2026:10:11,"
       "AF1047  ,000004,200186,ITT_#10100")
FST_POS = "FST01EGLLEPWAN52232E0207997 36  33  51     35262      188  10531050"
FST_NOPOS = "FST01EPWAEGLL 00            26  79         21              09100658"


def r(label, sublabel, body):
    return h1route.parse_route(label, sublabel, body)


class TestAbs:
    def test_csv_dialect(self):
        assert r("H1", "DF", ABS_CSV) == {"dep_icao": "EPWA", "dest_icao": "LROP", "source": "abs"}

    @pytest.mark.parametrize("body,dep,dest", [
        (ABS_FIXED_A, "EPWA", "LIRF"),
        (ABS_FIXED_B, "EDDF", "EPWA"),
        (EB0_FIXED, "EPWA", "KMIA"),
    ])
    def test_fixed_dialect(self, body, dep, dest):
        assert r("H1", "DF", body) == {"dep_icao": dep, "dest_icao": dest, "source": "abs"}

    def test_sb0_prefix(self):
        assert r("H1", "DF", "SB0031AA_ SP-LRG EPWAKMIA1\r\n x")["source"] == "abs"

    def test_wrong_sublabel_or_label_ignored(self):
        assert r("H1", "T8", ABS_CSV) is None
        assert r("H1", None, ABS_CSV) is None
        assert r("16", "DF", ABS_CSV) is None

    def test_csv_bad_fields_rejected(self):
        assert r("H1", "DF", "ABS026BA_S ,B7378MAX ,261123,LO4TP ,EPWA,EPWA,0318,X\r\n") is None
        assert r("H1", "DF", "ABS026BA_S ,B7378MAX ,261123,LO4TP ,epwa,LROP,0318,X\r\n") is None
        assert r("H1", "DF", "ABS026BA_S ,B7378MAX ,261123,LO4TP ,EPW,LROP,0318,X\r\n") is None
        assert r("H1", "DF", "ABS026BA_S ,B7378MAX ,261123\r\n") is None

    def test_fixed_same_airport_or_no_airports_rejected(self):
        assert r("H1", "DF", "ABS011DA_S       EPWAEPWA4\r\n x") is None
        assert r("H1", "DF", "EB0031AA_ SP-LRG\r\n 52184 0208871128") is None

    def test_fix_line_never_read_as_header(self):
        # only the first line is the header
        assert r("H1", "DF", "ABS011DA_S  \r\nEPWALIRF4") is None


class TestB44:
    def test_takeoff(self):
        assert r("H1", "DF", B44) == {"dep_icao": "EPWA", "dest_icao": "LPPT", "source": "b44"}

    def test_arrival_form(self):
        assert r("H1", "DF", B44_ARR) == {"dep_icao": "EPWA", "dest_icao": "ENGM", "source": "b44"}

    def test_wrong_sublabel_ignored(self):
        assert r("H1", "T8", B44) is None

    def test_no_route_on_second_line(self):
        assert r("H1", "DF", "B44C\r\nSP-LVH  4824HT\r\n x") is None
        assert r("H1", "DF", "B44C") is None


class TestR64:
    def test_route(self):
        assert r("H1", "T8", R64) == {"dep_icao": "EPWA", "dest_icao": "LFPG", "source": "r64"}

    def test_r642_variant(self):
        assert r("H1", "T8", R64.replace("R641", "R642"))["source"] == "r64"

    def test_wrong_sublabel_ignored(self):
        assert r("H1", "DF", R64) is None

    def test_short_or_bad_fields_rejected(self):
        assert r("H1", "T8", "R641V0106,180302") is None
        assert r("H1", "T8", R64.replace("LFPG", "EPWA")) is None
        assert r("H1", "T8", R64.replace("LFPG", "LF1G")) is None


class TestFst01:
    def test_with_position(self):
        assert r("15", None, FST_POS) == {"dep_icao": "EGLL", "dest_icao": "EPWA", "source": "fst01"}

    def test_without_position(self):
        assert r("15", None, FST_NOPOS) == {"dep_icao": "EPWA", "dest_icao": "EGLL", "source": "fst01"}

    def test_wrong_label_ignored(self):
        assert r("16", None, FST_POS) is None
        assert r("H1", "DF", FST_POS) is None

    def test_other_label15_bodies(self):
        assert r("15", None, "FST02EGLLEPWA") is None
        assert r("15", None, "FST01EGLLEGLL 00") is None


class TestTolerance:
    @pytest.mark.parametrize("body", [None, "", 5, b"ABS"])
    def test_junk_bodies(self, body):
        assert r("H1", "DF", body) is None
        assert r("15", None, body) is None
