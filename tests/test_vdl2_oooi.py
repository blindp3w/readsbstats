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


class TestParseQseries:
    """Q-series compact OOOI reports: QP=OUT, QQ=OFF, QR=ON, QS=IN.

    Body = <dep ICAO×4><arr ICAO×4><HHMM>[<HHMM out-echo>][tail]. All samples are
    verbatim from the 2026-06 live dump.
    """

    def test_qp_out_report(self):
        rec = oooi.parse_qseries("QP", "EPMOLIRA1616 192")
        assert rec == {
            "phase": "out",
            "dep_icao": "EPMO",
            "dest_icao": "LIRA",
            "t": "1616",
            "t2": None,
        }

    def test_qq_off_carries_out_echo(self):
        # QQ (OFF) bodies append the earlier OUT time: QP EPMOGCTS1059 → QQ ...11121059.
        rec = oooi.parse_qseries("QQ", "EPMOGCTS11121059")
        assert rec is not None
        assert rec["phase"] == "off"
        assert rec["t"] == "1112"
        assert rec["t2"] == "1059"

    def test_qq_slash_suffix(self):
        rec = oooi.parse_qseries("QQ", "EPWALIME0416/FB   71/ETA 0609/FN W608PV")
        assert rec is not None
        assert rec["t"] == "0416"
        assert rec["t2"] is None

    def test_qq_multiline_position_tail(self):
        rec = oooi.parse_qseries("QQ", "EPWAEDDM0920\n001FE08092005N5210.6E02055.5")
        assert rec is not None
        assert rec["t"] == "0920"
        assert rec["t2"] is None

    def test_qr_on_report_bare(self):
        rec = oooi.parse_qseries("QR", "LIRAEPMO2106")
        assert rec is not None
        assert rec["phase"] == "on"
        assert rec["dep_icao"] == "LIRA"
        assert rec["dest_icao"] == "EPMO"
        assert rec["t"] == "2106"

    def test_qs_in_trailing_digits(self):
        rec = oooi.parse_qseries("QS", "LEBLEPMO2302  96")
        assert rec is not None
        assert rec["phase"] == "in"
        assert rec["t"] == "2302"

    def test_t2_only_for_qq(self):
        # A second HHMM group on a non-QQ label is tail data, not an out-echo.
        rec = oooi.parse_qseries("QR", "LIRAEPMO21062118")
        assert rec is not None
        assert rec["t"] == "2106"
        assert rec["t2"] is None

    def test_rejects_seven_letter_prefix(self):
        assert oooi.parse_qseries("QR", "EPMOLIR2106") is None

    def test_rejects_lowercase(self):
        assert oooi.parse_qseries("QP", "epmolira1616") is None

    def test_rejects_letter_after_time(self):
        assert oooi.parse_qseries("QR", "LIRAEPMO2106Z") is None

    def test_rejects_invalid_hhmm(self):
        assert oooi.parse_qseries("QR", "LIRAEPMO9999") is None

    def test_rejects_label_outside_q_set(self):
        assert oooi.parse_qseries("Q0", "LIRAEPMO2106") is None
        assert oooi.parse_qseries("H1", "LIRAEPMO2106") is None
        assert oooi.parse_qseries(None, "LIRAEPMO2106") is None

    def test_rejects_empty_and_none_body(self):
        assert oooi.parse_qseries("QP", "") is None
        assert oooi.parse_qseries("QP", None) is None


class TestParseLabel49:
    """Label 49 is airline-defined; this targets the observed Etihad/LOT movement
    form: <prefix> <flight ICAO>/<DDHHMM><dep ICAO×4><arr ICAO×4>. Route source
    only — no OOOI times are derived from it."""

    def test_etd_movement_report(self):
        rec = oooi.parse_label49("01DCAP    ETD159/090545OMAAEPWA")
        assert rec == {"flight": "ETD159", "dep_icao": "OMAA", "dest_icao": "EPWA"}

    def test_flight_with_letter_suffix(self):
        rec = oooi.parse_label49("01ICCL    LOT15K/111616EPWAKEWR")
        assert rec is not None
        assert rec["flight"] == "LOT15K"
        assert rec["dep_icao"] == "EPWA"
        assert rec["dest_icao"] == "KEWR"

    def test_tolerates_second_line(self):
        rec = oooi.parse_label49("01DCAP    ETD159/090545OMAAEPWA\n+ 2014155.1+ 20.9")
        assert rec is not None
        assert rec["dep_icao"] == "OMAA"

    def test_rejects_five_digit_timestamp(self):
        assert oooi.parse_label49("01DCAP    ETD159/09054OMAAEPWA") is None

    def test_rejects_lowercase_and_garbage(self):
        assert oooi.parse_label49("01dcap    etd159/090545omaaepwa") is None
        assert oooi.parse_label49("position report krakow") is None
        assert oooi.parse_label49("#DFB021,14922,0249,19836/V103D01") is None

    def test_rejects_empty_and_none(self):
        assert oooi.parse_label49("") is None
        assert oooi.parse_label49(None) is None
