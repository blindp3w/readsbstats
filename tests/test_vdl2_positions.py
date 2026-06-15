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
