"""Tests for the OOOI body parser (vdl2/oooi.py).

OOOI (Out/Off/On/In) block-time reports are NOT identified by the ACARS `label`
(which is dominantly H1) — they live as slash-delimited TEI key-values inside the
free-text message body. The parser must recognise DEP/ARR bodies, extract the TEI
fields, tolerate carrier-variant ordering + missing optionals, and fail SOFT to
None on anything that doesn't conform.
"""
from __future__ import annotations

from readsbstats.vdl2 import oooi


class TestParseDep:
    def test_full_dep(self):
        # Real OAG example (vdl2-research.md §3).
        rec = oooi.parse_oooi("DEP / FI JA401/AN CC-AWE/DA SPJC/DS SCEL/OT 0030")
        assert rec is not None
        assert rec["type"] == "DEP"
        assert rec["flight"] == "JA401"
        assert rec["registration"] == "CC-AWE"
        assert rec["dep_icao"] == "SPJC"
        assert rec["dest_icao"] == "SCEL"
        assert rec["t_out"] == "0030"
        assert rec["t_on"] is None and rec["t_in"] is None


class TestParseOffTime:
    def test_dep_parses_3letter_off_tei(self):
        # OFF is a 3-letter TEI key; the parser must capture it (not just 2-letter keys).
        rec = oooi.parse_oooi("DEP / FI LO1/AN SP-ABC/DA EPWA/DS EGLL/OT 0030/OFF 0042")
        assert rec is not None
        assert rec["t_out"] == "0030"
        assert rec["t_off"] == "0042"

    def test_unknown_keys_are_ignored(self):
        # A non-OOOI key must not become a field (whitelist), but a real one alongside still parses.
        rec = oooi.parse_oooi("DEP / ZZ junk/OT 0030")
        assert rec is not None
        assert rec["t_out"] == "0030"


class TestParseArr:
    def test_full_arr_uses_ad_for_dest(self):
        rec = oooi.parse_oooi("ARR / FI JA401/AN CC-AWE/DA SPJC/AD SCEL/ON 0145/IN 0157")
        assert rec is not None
        assert rec["type"] == "ARR"
        assert rec["dep_icao"] == "SPJC"
        assert rec["dest_icao"] == "SCEL"   # AD when DS absent
        assert rec["t_on"] == "0145"
        assert rec["t_in"] == "0157"


class TestVariantsAndTolerance:
    def test_field_order_independent(self):
        rec = oooi.parse_oooi("DEP / OT 0030/DA SPJC/FI JA401")
        assert rec is not None
        assert rec["dep_icao"] == "SPJC" and rec["t_out"] == "0030" and rec["flight"] == "JA401"

    def test_minimal_dep_with_only_time(self):
        # Only the lead token + one recognised TEI — still meaningful.
        rec = oooi.parse_oooi("DEP / OT 0030")
        assert rec is not None
        assert rec["type"] == "DEP" and rec["t_out"] == "0030"
        assert rec["dep_icao"] is None and rec["registration"] is None

    def test_no_space_after_lead(self):
        rec = oooi.parse_oooi("DEP/OT 0030/DA EPWA")
        assert rec is not None and rec["dep_icao"] == "EPWA"


class TestRejects:
    def test_non_oooi_body_returns_none(self):
        assert oooi.parse_oooi("position report krakow 11752.20371/20.83122") is None

    def test_lead_token_must_be_exact(self):
        # 'DEPARTURE' must not be mistaken for an OOOI DEP record.
        assert oooi.parse_oooi("DEPARTURE GATE A12 FOR FLIGHT LO123") is None

    def test_lead_only_no_fields_returns_none(self):
        assert oooi.parse_oooi("DEP /") is None
        assert oooi.parse_oooi("ARR") is None

    def test_empty_and_none(self):
        assert oooi.parse_oooi("") is None
        assert oooi.parse_oooi(None) is None

    def test_garbage_fails_soft(self):
        assert oooi.parse_oooi("#DFB2A0C9F1E\x00\x01garbage") is None
