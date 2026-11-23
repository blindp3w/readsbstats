"""Tests for vdl2/h1rep.py: ADS-B-validated H1 position reports (ABS/EB0/SB0 multi-fix,
CF FDE/MM, ``(POS-``) and the shared ``fix_epoch`` time resolver (ADR-0019 Batch 5).

Bodies are real dumpvdl2 rows (CRLF line breaks) from the 2026-11-23 snapshot.
"""
from __future__ import annotations

import calendar

import pytest

from readsbstats.vdl2 import h1rep


def utc(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s))


# --- real bodies ------------------------------------------------------------------
CSV_BODY = (
    "ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,BCG27-S200-0011\r\n"
    "N5211.0,E02052.4,231028,02421,-03.5,301,029,CL,00000,0,\r\n"
    "N5210.9,E02051.9,231028,02563,-03.5,305,031,CL,00000,0,\r\n"
    "N5210.8,E02051.3,231028,02698,-04.0,307,031,CL,00000,0,\r\n:"
)
CSV_RECV = 1795429730   # 2026-11-23 10:28:50 UTC

FIXED_BODY = (
    "ABS011DA_S       EPWALEMD4\r\n"
    " 52188  207841003004137-06-270019X1XIH-0801090000--\r\n"
    " 52192  206771004006901-108291021X1P/9W0661160000--\r\n"
    " 52168  205691005009471-14-287024X20NXW0110120000--\r\n"
    " 52144  20452100601"          # truncated tail line: no fix
)
FIXED_RECV = 1795342130   # 2026-11-22 10:08:50 UTC

EB0_BODY = "EB0031AA_ SP-LRG EPWAKMIA1\r\n 52184 0208871128 02846-0232930310W0K2R118183"
EB0_RECV = 1795433320   # 2026-11-23 11:28:40 UTC

EB0_NONPOS = (
    "EB0021CA_8   1\r\n SP-LRHLOT1TO 559RKSIEPWA\r\nBSM5C70990043 1454502023  156101\r\n"
    "161126062124226  193 1746\r\n 999 999 999\r\n -10 999 999 999 999 999\r\n"
    " 999 999 999 999 999 999\r\n161126184343161  111 1511\r\n 999 999"
)
ABS_NONPOS = (
    "ABS071BA_\r\nS       0285EPWAEGLL151126 287CL201537 2148\r\nBCG27-S200-0011 20132+\r\n"
    "+++++++++ 10    --INVD\r\n  329 45-4715-4715-2672   000   034 -075    -1100"
)

FDE_BODY = ("FDE064806112311752.17762/20.84221\r\n06460114706    0   0\r\n"
            "06460114895    0   0")
FDE_RECV = 1795416502   # 2026-11-23 06:48:22 UTC
FDE_NOPOS = "FDE1227481123ACA\n12254714301   0   0\n12254714308   0   0"
MM_BODY = "MM110717112311752.17882/20.85423\r\n110109032089 292\r\n11051114 340  41"
MM_RECV = 1795432060   # 2026-11-23 11:07:40 UTC
MM_NOPOS = "MM1017231123ACD\n10152214 934 497\n10152214 9402997"

POS_BODY = "(POS-KLM75C  -5222N02005E/104548 F180\r\nRMK/FUEL   5.4 M0.59)"
POS_RECV = 1795257957   # 2026-11-21 10:45:57 UTC


class TestFixEpoch:
    def test_hhmm_same_day(self):
        recv = utc(2026, 11, 23, 10, 30)
        assert h1rep.fix_epoch(recv, 10, 28) == utc(2026, 11, 23, 10, 28)

    def test_hhmm_slightly_in_future_allowed(self):
        recv = utc(2026, 11, 23, 10, 30)
        assert h1rep.fix_epoch(recv, 10, 35) == utc(2026, 11, 23, 10, 35)   # <= recv+600

    def test_hhmm_far_future_rolls_back_a_day(self):
        recv = utc(2026, 11, 23, 10, 30)
        assert h1rep.fix_epoch(recv, 10, 41) == utc(2026, 11, 22, 10, 41)

    def test_midnight_rollover(self):
        recv = utc(2026, 11, 24, 0, 3)
        assert h1rep.fix_epoch(recv, 23, 59) == utc(2026, 11, 23, 23, 59)

    def test_seconds(self):
        recv = utc(2026, 11, 23, 6, 48, 22)
        assert h1rep.fix_epoch(recv, 6, 48, 6) == utc(2026, 11, 23, 6, 48, 6)

    @pytest.mark.parametrize("hh,mm,ss", [(24, 0, 0), (0, 60, 0), (-1, 0, 0), (0, 0, 60)])
    def test_invalid_time(self, hh, mm, ss):
        assert h1rep.fix_epoch(utc(2026, 11, 23, 10), hh, mm, ss) is None

    def test_day_in_receive_month(self):
        recv = utc(2026, 11, 23, 10, 28, 50)
        assert h1rep.fix_epoch(recv, 10, 28, day=23) == utc(2026, 11, 23, 10, 28)
        assert h1rep.fix_epoch(recv, 22, 0, day=22) == utc(2026, 11, 22, 22, 0)

    def test_day_month_rollover_goes_to_previous_month(self):
        recv = utc(2026, 12, 1, 0, 5)
        assert h1rep.fix_epoch(recv, 23, 58, day=30) == utc(2026, 11, 30, 23, 58)

    def test_day_year_rollover(self):
        recv = utc(2027, 1, 1, 0, 5)
        assert h1rep.fix_epoch(recv, 23, 58, day=31) == utc(2026, 12, 31, 23, 58)

    def test_day_31_in_short_month_falls_back(self):
        recv = utc(2026, 11, 1, 0, 5)   # November has no 31st; October does
        assert h1rep.fix_epoch(recv, 23, 0, day=31) == utc(2026, 10, 31, 23, 0)

    def test_invalid_day(self):
        recv = utc(2026, 11, 23, 10)
        assert h1rep.fix_epoch(recv, 10, 0, day=0) is None
        assert h1rep.fix_epoch(recv, 10, 0, day=32) is None

    def test_day_close_future_within_two_days_kept(self):
        recv = utc(2026, 11, 23, 10)
        assert h1rep.fix_epoch(recv, 12, 0, day=24) == utc(2026, 11, 24, 12)


class TestAbsCsv:
    def test_three_fixes_with_embedded_time_temp_wind(self):
        out = h1rep.parse_positions("DF", CSV_BODY, CSV_RECV)
        assert len(out) == 3
        f = out[0]
        assert f["source"] == "h1-abs"
        assert abs(f["lat"] - (52 + 11.0 / 60)) < 1e-4
        assert abs(f["lon"] - (20 + 52.4 / 60)) < 1e-4
        assert f["ts"] == utc(2026, 11, 23, 10, 28)
        assert f["alt"] == 2421
        assert f["temp_c"] == -3.5 and f["wind_dir"] == 301 and f["wind_kt"] == 29
        assert out[2]["alt"] == 2698 and out[2]["temp_c"] == -4.0

    def test_south_west_signs(self):
        body = "ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,X\r\n" \
               "S3345.0,W07012.6,231028,02421,-03.5,301,029,CL,00000,0,"
        (f,) = h1rep.parse_positions("DF", body, CSV_RECV)
        assert abs(f["lat"] + (33 + 45 / 60)) < 1e-4
        assert abs(f["lon"] + (70 + 12.6 / 60)) < 1e-4

    def test_sb0_prefix_accepted(self):
        body = CSV_BODY.replace("ABS023BA_S", "SB0023BA_S", 1)
        assert len(h1rep.parse_positions("DF", body, CSV_RECV)) == 3

    def test_invalid_day_skips_fix(self):
        body = CSV_BODY.replace("231028,02421", "991028,02421")
        assert len(h1rep.parse_positions("DF", body, CSV_RECV)) == 2

    def test_invalid_time_skips_fix(self):
        body = CSV_BODY.replace("231028,02421", "232528,02421")   # 25:28
        assert len(h1rep.parse_positions("DF", body, CSV_RECV)) == 2

    def test_out_of_range_coords_dropped(self):
        body = ("ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,X\r\n"
                "N9511.0,E02052.4,231028,02421,-03.5,301,029,CL,00000,0,\r\n"
                "N5211.0,E19952.4,231028,02421,-03.5,301,029,CL,00000,0,")
        assert h1rep.parse_positions("DF", body, CSV_RECV) == []

    def test_fix_count_capped(self):
        line = "N5211.0,E02052.4,231028,02421,-03.5,301,029,CL,00000,0,\r\n"
        body = "ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,X\r\n" + line * 200
        assert len(h1rep.parse_positions("DF", body, CSV_RECV)) == h1rep.MAX_FIXES == 64

    def test_month_rollover_for_day(self):
        recv = utc(2026, 12, 1, 0, 3)
        body = "ABS023BA_S      ,B7378MAX ,261123,LO513 ,EPWA,LUKK,0293,X\r\n" \
               "N5211.0,E02052.4,302358,02421,-03.5,301,029,CL,00000,0,"
        (f,) = h1rep.parse_positions("DF", body, recv)
        assert f["ts"] == utc(2026, 11, 30, 23, 58)


class TestAbsFixedWidth:
    def test_three_fixes_truncated_tail_ignored(self):
        out = h1rep.parse_positions("DF", FIXED_BODY, FIXED_RECV)
        assert len(out) == 3
        f = out[0]
        assert f["source"] == "h1-abs"
        assert f["lat"] == 52.188 and f["lon"] == 20.784
        assert f["ts"] == utc(2026, 11, 22, 10, 3)
        assert f["alt"] == 4137
        assert "temp_c" not in f and "wind_dir" not in f and "wind_kt" not in f
        assert out[1]["ts"] == utc(2026, 11, 22, 10, 4) and out[1]["alt"] == 6901
        assert out[2]["ts"] == utc(2026, 11, 22, 10, 5) and out[2]["alt"] == 9471

    def test_eb0_header_with_registration(self):
        (f,) = h1rep.parse_positions("DF", EB0_BODY, EB0_RECV)
        assert f["lat"] == 52.184 and f["lon"] == 20.887
        assert f["ts"] == utc(2026, 11, 23, 11, 28)
        assert f["alt"] == 2846

    def test_eb0_other_layout_yields_nothing(self):
        assert h1rep.parse_positions("DF", EB0_NONPOS, 1794854627) == []

    def test_abs_other_layout_yields_nothing(self):
        assert h1rep.parse_positions("DF", ABS_NONPOS, 1794773748) == []

    def test_header_only(self):
        assert h1rep.parse_positions("DF", "ABS011DA_S       EPWALEMD4", FIXED_RECV) == []
        assert h1rep.parse_positions("DF", "ABS011DA_S       EPWALEMD4\r\n", FIXED_RECV) == []

    def test_midnight_rollover(self):
        recv = utc(2026, 11, 24, 0, 3)
        body = "ABS011DA_S       EPWALEMD4\r\n 52188  207842359004137-06-270019X1XIH"
        (f,) = h1rep.parse_positions("DF", body, recv)
        assert f["ts"] == utc(2026, 11, 23, 23, 59)

    def test_out_of_range_lat_dropped(self):
        body = "ABS011DA_S       EPWALEMD4\r\n 95188  207841003004137-06-270019X1XIH"
        assert h1rep.parse_positions("DF", body, FIXED_RECV) == []

    def test_bad_time_dropped(self):
        body = "ABS011DA_S       EPWALEMD4\r\n 52188  207841063004137-06-270019X1XIH"
        assert h1rep.parse_positions("DF", body, FIXED_RECV) == []


class TestDispatch:
    def test_wrong_sublabel_for_abs(self):
        assert h1rep.parse_positions("CF", CSV_BODY, CSV_RECV) == []
        assert h1rep.parse_positions(None, CSV_BODY, CSV_RECV) == []
        assert h1rep.parse_positions("M1", FIXED_BODY, FIXED_RECV) == []

    def test_unknown_prefix(self):
        body = CSV_BODY.replace("ABS023BA_S", "XYZ023BA_S", 1)
        assert h1rep.parse_positions("DF", body, CSV_RECV) == []

    def test_cf_body_under_df(self):
        assert h1rep.parse_positions("DF", FDE_BODY, FDE_RECV) == []

    def test_non_str_body(self):
        assert h1rep.parse_positions("DF", None, 1) == []
        assert h1rep.parse_positions("DF", b"ABS", 1) == []
        assert h1rep.parse_positions("DF", "", 1) == []

    def test_shape_alone_not_trusted(self):
        # a fixed-width-looking line without the ABS/EB0/SB0 header
        assert h1rep.parse_positions("DF", "HELLO\r\n 52188  207841003004137-06", FIXED_RECV) == []


class TestCf:
    def test_fde_lat_lon(self):
        (f,) = h1rep.parse_positions("CF", FDE_BODY, FDE_RECV)
        assert f["source"] == "h1-cf"
        assert f["lat"] == 52.17762 and f["lon"] == 20.84221

    def test_fde_time_is_hhmmss(self):
        (f,) = h1rep.parse_positions("CF", FDE_BODY, FDE_RECV)
        assert f["ts"] == utc(2026, 11, 23, 6, 48, 6)   # '064806' + '1123' + '117'

    def test_mm(self):
        (f,) = h1rep.parse_positions("CF", MM_BODY, MM_RECV)
        assert f["lat"] == 52.17882 and f["lon"] == 20.85423
        assert f["ts"] == utc(2026, 11, 23, 11, 7, 17)

    def test_no_position_bodies(self):
        assert h1rep.parse_positions("CF", FDE_NOPOS, 1795436869) == []
        assert h1rep.parse_positions("CF", MM_NOPOS, 1795429056) == []

    def test_wrong_sublabel(self):
        assert h1rep.parse_positions("DF", MM_BODY, MM_RECV) == []

    def test_out_of_range_dropped(self):
        assert h1rep.parse_positions("CF", "FDE064806112311792.17762/20.84221", FDE_RECV) == []

    def test_invalid_time_dropped(self):
        assert h1rep.parse_positions("CF", "FDE256106112311752.17762/20.84221", FDE_RECV) == []

    def test_negative_coords(self):
        (f,) = h1rep.parse_positions("CF", "FDE064806112311733.17762/-20.84221", FDE_RECV)
        assert f["lon"] == -20.84221


class TestPos:
    def test_klm_pos(self):
        (f,) = h1rep.parse_positions("DF", POS_BODY, POS_RECV)
        assert f["source"] == "h1-pos"
        assert abs(f["lat"] - (52 + 22 / 60)) < 1e-4
        assert abs(f["lon"] - (20 + 5 / 60)) < 1e-4
        assert f["ts"] == utc(2026, 11, 21, 10, 45, 48)
        assert f["alt"] == 18000

    def test_south_west(self):
        body = "(POS-KLM75C  -3345S15112W/104548 F180\r\nRMK/FUEL   5.4 M0.59)"
        (f,) = h1rep.parse_positions("DF", body, POS_RECV)
        assert abs(f["lat"] + (33 + 45 / 60)) < 1e-4
        assert abs(f["lon"] + (151 + 12 / 60)) < 1e-4

    def test_out_of_range_dropped(self):
        body = "(POS-KLM75C  -9122N02005E/104548 F180\r\nRMK/FUEL   5.4 M0.59)"
        assert h1rep.parse_positions("DF", body, POS_RECV) == []

    def test_invalid_time_dropped(self):
        body = "(POS-KLM75C  -5222N02005E/256100 F180\r\nRMK/FUEL   5.4 M0.59)"
        assert h1rep.parse_positions("DF", body, POS_RECV) == []

    def test_wrong_sublabel(self):
        assert h1rep.parse_positions("CF", POS_BODY, POS_RECV) == []
        assert h1rep.parse_positions(None, POS_BODY, POS_RECV) == []
