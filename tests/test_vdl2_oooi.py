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


class TestParseCmLogon:
    """Our own synthesized ATN CM-logon body (`atn.summarize_cm_logon`):
    `LOGON <flight> <dep>-<dest>` under label `CM`."""

    def test_valid(self):
        assert oooi.parse_cm_logon("CM", "LOGON RYR8414 EPMO-LATI") == {
            "flight": "RYR8414", "dep_icao": "EPMO", "dest_icao": "LATI"}

    def test_wrong_label(self):
        assert oooi.parse_cm_logon("H1", "LOGON RYR8414 EPMO-LATI") is None
        assert oooi.parse_cm_logon("cm", "LOGON RYR8414 EPMO-LATI") is None
        assert oooi.parse_cm_logon(None, "LOGON RYR8414 EPMO-LATI") is None

    def test_no_route(self):
        assert oooi.parse_cm_logon("CM", "LOGON RYR8414") is None
        assert oooi.parse_cm_logon("CM", "LOGON") is None

    def test_junk(self):
        for body in ("", None, 42, "LOGON RYR8414 epmo-lati", "LOGON RYR8414 EPMO-LATIX",
                     "LOGON RYR8414 EPMO-LATI trailing", "xLOGON RYR8414 EPMO-LATI",
                     "LOGON  RYR8414 EPMO-LATI", "LOGON RYR8414 EPMO-LATI\n"):
            assert oooi.parse_cm_logon("CM", body) is None, body


class TestParseLabel26:
    """Air France label-26 report: HHMM dep flight/DD HHMM(ETA) dest [runway]."""

    def test_real_with_runway(self):
        assert oooi.parse_label26("1049EPWAAF1147/18\r1241LFPG27R") == {
            "flight": "AF1147", "dep_icao": "EPWA", "dest_icao": "LFPG",
            "eta": "1241", "runway": "27R"}

    def test_newline_separator(self):
        rec = oooi.parse_label26("1049EPWAAF1147/18\n1241LFPG27R")
        assert rec is not None and rec["dest_icao"] == "LFPG"

    def test_no_separator(self):
        rec = oooi.parse_label26("1049EPWAAF1147/181241LFPG27R")
        assert rec is not None and rec["eta"] == "1241"

    def test_without_runway(self):
        assert oooi.parse_label26("1305LFPGAF1346/10\r1310EPWA") == {
            "flight": "AF1346", "dep_icao": "LFPG", "dest_icao": "EPWA",
            "eta": "1310", "runway": None}

    def test_flight_with_trailing_letter(self):
        rec = oooi.parse_label26("1305LFPGAF134A/10\r1310EPWA")
        assert rec is not None and rec["flight"] == "AF134A"

    def test_rejects_junk(self):
        for body in ("", None, 42, "position report krakow",
                     "1049epwaaf1147/18\r1241lfpg27r",       # lowercase
                     "1049EPWAAF1147/18\r1241LFPG27R trailing junk!",
                     "1049EPWAAF1147/18",                       # no ETA/dest
                     "1049EPWAAF1147/1\r1241LFPG",             # 1-digit day
                     "1049EPWA/18\r1241LFPG",                  # no flight
                     "x1049EPWAAF1147/18\r1241LFPG27R",
                     "1049EPWAAF1147/18\r1241LFP"):
            assert oooi.parse_label26(body) is None, body


class TestParseLabel20Rst:
    """KLM label-20 ``RST`` report: day, HHMMSS, callsign, dep, dest."""

    def test_real_double_space(self):
        assert oooi.parse_label20_rst("RST53A27145652KLM49N  EHAMEPWA") == {
            "flight": "KLM49N", "dep_icao": "EHAM", "dest_icao": "EPWA"}

    def test_real_reverse_direction(self):
        assert oooi.parse_label20_rst("RST31A30181806KLM60M  EPWAEHAM") == {
            "flight": "KLM60M", "dep_icao": "EPWA", "dest_icao": "EHAM"}

    def test_single_space(self):
        assert oooi.parse_label20_rst("RST31A13111945KLM9938 EPWAEHAM") == {
            "flight": "KLM9938", "dep_icao": "EPWA", "dest_icao": "EHAM"}

    def test_non_route_label20_bodies(self):
        for body in ("POSN52117E020255,,121107,075,",
                     "FMM||LOT2YE/02.LZ-EAO.RKG.ULT/800..FKG"):
            assert oooi.parse_label20_rst(body) is None

    def test_rejects_junk(self):
        for body in ("", None, 42, "RST53A27145652KLM49N  EHAMEPWA extra",
                     "rst53a27145652klm49n  ehamepwa",
                     "RST53A27145652KLM49N  EHAM",
                     "RST53A2714565KLM49N  EHAMEPWA",
                     "xRST53A27145652KLM49N  EHAMEPWA",
                     "RST53A27145652KLM49NEHAMEPWA"):
            assert oooi.parse_label20_rst(body) is None, body
