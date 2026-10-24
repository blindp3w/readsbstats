"""Tests for the Label-16 AUTPOS body position parser (vdl2/positions.py).

Validated against a real vdlm2dec LOT feed: precise ACARS positions live in the
message BODY (e.g. 'N 52.166,E 020.772'), not the lat/lon columns (which carry
only coarse ~0.1° VDL2 XID link-frame fixes). This parser extracts the precise
in-body coordinates for the map overlay.
"""
from __future__ import annotations

from readsbstats.vdl2 import positions


class TestParsePosition:
    def test_comma_separated_real_qr(self):
        # Real Label-16 AUTPOS body from the live feed.
        rec = positions.parse_position("WA921  ,N 52.166,E 020.772,4406, 251,2054, 72\\TS154458,050626")
        assert rec is not None
        assert abs(rec["lat"] - 52.166) < 1e-6
        assert abs(rec["lon"] - 20.772) < 1e-6

    def test_space_separated(self):
        rec = positions.parse_position("153103,68416,1652, 150,N 52.180 E 20.086")
        assert rec is not None
        assert abs(rec["lat"] - 52.180) < 1e-6
        assert abs(rec["lon"] - 20.086) < 1e-6

    def test_double_space_after_e(self):
        rec = positions.parse_position("RW15   ,N 52.206,E  20.932,1097,0159,1419,033")
        assert rec is not None
        assert abs(rec["lon"] - 20.932) < 1e-6

    def test_south_west_signs(self):
        rec = positions.parse_position("XX ,S 33.900,W 018.600,1000")
        assert rec is not None
        assert rec["lat"] < 0 and rec["lon"] < 0
        assert abs(rec["lat"] + 33.900) < 1e-6 and abs(rec["lon"] + 18.600) < 1e-6


class TestParse59g:
    # Real LOT 59,G ground-telemetry bodies from the live feed.
    POS = "59,G,0542,1,1,EPWA,52.15,20.59,52.15,20.61,10,269013,0,32.1,10586,13,38,276,290,451B76A"
    STATUS = "59,G,EPGD,EPWA,33/-,,1,,0,,6,145,04,,"

    def test_position_form(self):
        rec = positions.parse_59g(self.POS)
        assert rec is not None
        assert abs(rec["lat"] - 52.15) < 1e-6
        assert abs(rec["lon"] - 20.59) < 1e-6

    def test_status_subform_rejected(self):
        # label-37 runway/status form: fields[6/7] aren't coordinates.
        assert positions.parse_59g(self.STATUS) is None

    def test_integer_coded_fields_rejected(self):
        # A status-style row with numeric, in-range BUT non-decimal fields 6/7 must
        # NOT plot a bogus point — the decimal-fraction requirement is the structural
        # discriminator, not the incidental empty field in self.STATUS.
        assert positions.parse_59g("59,G,EPGD,EPWA,33/-,,12,34,x") is None
        assert positions.parse_59g("59,G,0542,1,1,EPWA,52,20,...") is None  # integer degrees

    def test_out_of_range_rejected(self):
        assert positions.parse_59g("59,G,0542,1,1,EPWA,95.0,20.59,...") is None

    def test_too_few_fields(self):
        assert positions.parse_59g("59,G,0542,1,1") is None

    def test_non_59g_and_non_string(self):
        assert positions.parse_59g("#M1BPOSN52081E020017") is None
        assert positions.parse_59g("") is None
        assert positions.parse_59g(None) is None


class TestRejects:
    def test_blank_fix_marker_returns_none(self):
        # Real "no fix" body: 'N   .    MMMM.MMM' — must not parse.
        assert positions.parse_position("144155,,1457, 130,N   .    MMMM.MMM") is None

    def test_engine_block_returns_none(self):
        assert positions.parse_position("#DFBB44C\nSP-LVC 54279 EPWAEGLLTO050626134630") is None

    def test_out_of_range_rejected(self):
        assert positions.parse_position("N 99.9,E 200.000") is None

    def test_integer_only_not_matched(self):
        # Requires a decimal fraction to avoid matching stray integers in noise.
        assert positions.parse_position("N 52,E 20 cargo manifest") is None

    def test_empty_and_none(self):
        assert positions.parse_position("") is None
        assert positions.parse_position(None) is None


# Real bodies from the live feed (each validated against ADS-B tracks of the same airframe).
L17 = "104800,1656,1049,  7520,N 52.237,E 20.877,24OCT26,22051,   5,   7"
L17_DASH = "203618,2310,2241,------,N 41.797,E 12.250,18SEP26,27441,  19,  14"
L38 = "/AY1476/240926/E 20.735/N 52.402/36945/-------/      /- 48/  563/ 29"
L2F = "1310+5219+2087/0130"
L14 = ("PRESENT POSITION REPORT \nDMY 23OCT26,UTC 082912,LAT N 52.223,LON E 20.812,"
       "CAS 149,WD 24235,WS\n   15,ALT 2126,FOB 11200,ETA 0831")
B0 = "/EPWW.AFN/FMHLOT45,.SP-LRA,48AE00,154016/FPON52016E020114,1/FCOATC,01/FCOADS,010BB9"


class TestParseLabel17:
    def test_real_body(self):
        rec = positions.parse_label17(L17)
        assert rec is not None
        assert abs(rec["lat"] - 52.237) < 1e-6 and abs(rec["lon"] - 20.877) < 1e-6

    def test_dash_altitude_rejected(self):
        # `------` altitude rows carry the DEPARTURE airport's coords (up to ~1200 nm off).
        assert positions.parse_label17(L17_DASH) is None

    def test_negative_altitude_ok(self):
        assert positions.parse_label17("104800,1656,1049,   -20,N 52.237,E 20.877,24OCT26,1,2,3") is not None

    def test_too_few_fields_and_junk(self):
        assert positions.parse_label17("N 52.237,E 20.877") is None
        assert positions.parse_label17("junk,junk,junk,1234,nothing here") is None

    def test_out_of_range(self):
        assert positions.parse_label17("1,2,3, 4500,N 95.000,E 20.877,x") is None

    def test_non_str_and_empty(self):
        for bad in (None, 5, b"x", "", ["a"]):
            assert positions.parse_label17(bad) is None


class TestParseLabel38:
    def test_real_body_lon_first(self):
        rec = positions.parse_label38(L38)
        assert rec is not None
        assert abs(rec["lat"] - 52.402) < 1e-6 and abs(rec["lon"] - 20.735) < 1e-6

    def test_west_south(self):
        rec = positions.parse_label38("/AY1/240926/W 3.500/S 12.250/1/")
        assert rec == {"lat": -12.25, "lon": -3.5}

    def test_lat_first_order_rejected(self):
        assert positions.parse_label38("/AY1/240926/N 52.402/E 20.735/1/") is None

    def test_junk_out_of_range_non_str(self):
        assert positions.parse_label38("38/") is None
        assert positions.parse_label38("/AY1/240926/E 200.735/N 52.402/1/") is None
        for bad in (None, 5, "", b"x"):
            assert positions.parse_label38(bad) is None


class TestParseLabel2F:
    def test_real_body(self):
        rec = positions.parse_label2f(L2F)
        assert rec is not None
        assert rec["lat"] == 52.19 and rec["lon"] == 20.87

    def test_hundredths_of_degree_not_minutes(self):
        # 5219 is 52.19 deg (hundredths), NOT 52 deg 19' (= 52.3167): the latter was 19.5 nm off.
        rec = positions.parse_label2f(L2F)
        assert rec is not None and abs(rec["lat"] - 52.3167) > 0.1

    def test_five_digit_lon_and_signs(self):
        rec = positions.parse_label2f("1310-3350+12345/0130")
        assert rec == {"lat": -33.5, "lon": 123.45}
        rec = positions.parse_label2f("1310+5219-0087/0130")
        assert rec == {"lat": 52.19, "lon": -0.87}

    def test_real_second_sample(self):
        rec = positions.parse_label2f("2116+5220+2082/0232")
        assert rec == {"lat": 52.2, "lon": 20.82}

    def test_junk_out_of_range_non_str(self):
        assert positions.parse_label2f("junk") is None
        assert positions.parse_label2f("1310+9519+2087/0130") is None   # lat 95.19
        assert positions.parse_label2f("1310+5219+2087") is None        # no trailing slash
        assert positions.parse_label2f("x1310+5219+2087/0130") is None  # anchored
        for bad in (None, 5, "", b"x"):
            assert positions.parse_label2f(bad) is None


class TestParseLabel14:
    def test_real_body(self):
        rec = positions.parse_label14(L14)
        assert rec is not None
        assert abs(rec["lat"] - 52.223) < 1e-6 and abs(rec["lon"] - 20.812) < 1e-6

    def test_south_west(self):
        assert positions.parse_label14("LAT S 12.5,LON W 3.25,CAS 1") == {"lat": -12.5, "lon": -3.25}

    def test_no_lat_lon_keywords(self):
        # a bare "N 52.2 E 20.8" fix is not a label-14 form
        assert positions.parse_label14("N 52.223,E 20.812") is None

    def test_junk_out_of_range_non_str(self):
        assert positions.parse_label14("LAT N 95.0,LON E 20.8") is None
        assert positions.parse_label14("LAT N 52.0,LON E 200.8") is None
        for bad in (None, 5, "", b"x"):
            assert positions.parse_label14(bad) is None


class TestParseAfnFpo:
    def test_real_body(self):
        rec = positions.parse_afn_fpo(B0)
        assert rec is not None
        # N52 01.6' E020 11.4'
        assert abs(rec["lat"] - (52 + 1.6 / 60)) < 1e-5
        assert abs(rec["lon"] - (20 + 11.4 / 60)) < 1e-5

    def test_south_west(self):
        rec = positions.parse_afn_fpo("/FPOS12300W045150,1/")
        assert rec is not None and rec["lat"] < 0 and rec["lon"] < 0

    def test_minute_over_599_rejected(self):
        assert positions.parse_afn_fpo("/FPON52600E020114,1/") is None
        assert positions.parse_afn_fpo("/FPON52016E020600,1/") is None

    def test_junk_out_of_range_non_str(self):
        assert positions.parse_afn_fpo("/EPWW.AFN/FMHLOT45,/FCOATC,01") is None
        assert positions.parse_afn_fpo("/FPON95016E020114,1/") is None
        for bad in (None, 5, "", b"x"):
            assert positions.parse_afn_fpo(bad) is None


class TestParseByLabel:
    def test_dispatch_each_label(self):
        assert positions.parse_by_label("16", "WA921  ,N 52.166,E 020.772,4406") == {"lat": 52.166, "lon": 20.772}
        assert positions.parse_by_label("17", L17) is not None
        assert positions.parse_by_label("38", L38) is not None
        assert positions.parse_by_label("2F", L2F) is not None
        assert positions.parse_by_label("14", L14) is not None
        assert positions.parse_by_label("B0", B0) is not None

    def test_generic_group_uses_parse_position(self):
        for lab in ("10", "13", "18", "27", "33", "12", "43", "41", "2P"):
            assert positions.parse_by_label(lab, "X,N 52.166,E 020.772,1") == {"lat": 52.166, "lon": 20.772}

    def test_label17_dash_via_dispatch(self):
        assert positions.parse_by_label("17", L17_DASH) is None

    def test_unknown_label_none(self):
        assert positions.parse_by_label("H1", "N 52.166,E 020.772") is None
        assert positions.parse_by_label("ZZ", L17) is None
        assert positions.parse_by_label(None, L17) is None

    def test_label17_text_under_unmapped_label_none(self):
        assert positions.parse_by_label("H1", L17) is None
        assert positions.parse_by_label("38", L17) is None   # wrong parser for the label

    def test_map_keys(self):
        assert set(positions.PRECISE_LABEL_PARSERS) == {
            "16", "10", "13", "18", "27", "33", "12", "43", "41", "2P",
            "17", "38", "2F", "14", "B0"}
