"""Tests for the ATN/OSI summary extractor (CPDLC) used at dumpvdl2 ingest."""
from __future__ import annotations

from readsbstats.vdl2 import atn


# A real dumpvdl2 ATN CPDLC downlink frame's `avlc` subtree (src 48C233, GS 11922A)
# from the 2026-06-14/15 overnight capture; cotp.pdu_list trimmed, cpdlc intact.
# Addresses are public VDL2/Mode-S identifiers.
CPDLC_WILCO_AVLC = {
    "src": {"addr": "48C233", "type": "Aircraft", "status": "Airborne"},
    "dst": {"addr": "11922A", "type": "Ground station"},
    "cr": "Command",
    "frame_type": "I",
    "x25": {
        "err": False,
        "pkt_type": 0,
        "pkt_type_name": "Data",
        "clnp": {
            "err": False,
            "priority": 11,
            "pdu_id": 3719,
            "cotp": {
                "pdu_list": [{"tpdu_code_descr": "Data"}],
                "cpdlc": {
                    "atc_downlink_message": {
                        "header": {"msg_id": 4, "msg_ref": 5,
                                   "logical_ack": "required"},
                        "msg_data": {
                            "msg_elements": [
                                {"msg_element": {"choice_label": "WILCO",
                                                 "choice": "dM0NULL", "data": {}}}
                            ]
                        },
                    }
                },
            },
        },
    },
}


def _cpdlc_avlc(elements, direction="atc_downlink_message"):
    """Build a minimal avlc dict carrying the given CPDLC msg_elements."""
    return {
        "src": {"addr": "48c233"},
        "x25": {"clnp": {"cotp": {"cpdlc": {
            direction: {"msg_data": {"msg_elements": elements}}
        }}}},
    }


def _el(choice_label):
    return {"msg_element": {"choice_label": choice_label, "data": {}}}


class TestSummarizeCpdlc:
    def test_real_wilco_downlink(self):
        assert atn.summarize_cpdlc(CPDLC_WILCO_AVLC) == ("CPDLC", "WILCO")

    def test_multi_element_joined(self):
        avlc = _cpdlc_avlc([_el("CURRENT DATA AUTHORITY"), _el("WILCO")])
        assert atn.summarize_cpdlc(avlc) == ("CPDLC", "CURRENT DATA AUTHORITY; WILCO")

    def test_uplink_variant_extracts(self):
        avlc = _cpdlc_avlc([_el("CLIMB TO")], direction="atc_uplink_message")
        assert atn.summarize_cpdlc(avlc) == ("CPDLC", "CLIMB TO")

    def test_no_x25_returns_none(self):
        # An ACARS-only frame carries no ATN payload.
        assert atn.summarize_cpdlc({"src": {"addr": "48c233"},
                                    "acars": {"msg_text": "x"}}) is None

    def test_no_cotp_returns_none(self):
        avlc = {"x25": {"clnp": {"idrp": {"err": False}}}}
        assert atn.summarize_cpdlc(avlc) is None

    def test_no_cpdlc_returns_none(self):
        avlc = {"x25": {"clnp": {"cotp": {"pdu_list": [{"tpdu_code_descr": "Data Ack"}]}}}}
        assert atn.summarize_cpdlc(avlc) is None

    def test_empty_msg_elements_returns_none(self):
        # The ~45 frames with a cpdlc key but no decodable message stay bare.
        assert atn.summarize_cpdlc(_cpdlc_avlc([])) is None

    def test_element_without_choice_label_returns_none(self):
        avlc = _cpdlc_avlc([{"msg_element": {"choice": "dM0NULL", "data": {}}}])
        assert atn.summarize_cpdlc(avlc) is None

    def test_non_dict_inputs_return_none(self):
        assert atn.summarize_cpdlc(None) is None
        assert atn.summarize_cpdlc("nope") is None
        assert atn.summarize_cpdlc({}) is None


# ── ATN Context-Management logon ─────────────────────────────────────────────

def _real_cm_avlc():
    """The `avlc` subtree of a real dumpvdl2 CM logon frame (48C132, RYR8414
    EPMO→LATI) — captured from the Pi feed, test_helpers DUMPVDL2_FRAMES."""
    import json

    from tests._helpers import DUMPVDL2_FRAMES
    return json.loads(DUMPVDL2_FRAMES["cm_logon"])["vdl2"]["avlc"]


def _cm_avlc(logon, choice="cmLogonRequest"):
    """Minimal avlc carrying a CM aircraft message with the given logon dict."""
    return {"x25": {"clnp": {"cotp": {"x225_spdu": {"x227_apdu": {"context_mgmt": {
        "cm_aircraft_message": {
            "choice": choice,
            "data": {"atn_context_mgmt_logon_request": logon},
        }}}}}}}}


class TestCmLogon:
    def test_real_frame(self):
        assert atn.cm_logon(_real_cm_avlc()) == {
            "flight": "RYR8414", "dep": "EPMO", "dest": "LATI"}

    def test_real_frame_summary(self):
        assert atn.summarize_cm_logon(_real_cm_avlc()) == ("CM", "LOGON RYR8414 EPMO-LATI")

    def test_missing_airports_summary_is_flight_only(self):
        avlc = _cm_avlc({"flight_id": "RYR8414"})
        assert atn.cm_logon(avlc) == {"flight": "RYR8414", "dep": None, "dest": None}
        assert atn.summarize_cm_logon(avlc) == ("CM", "LOGON RYR8414")

    def test_one_airport_missing_summary_is_flight_only(self):
        avlc = _cm_avlc({"flight_id": "RYR8414", "departure_airport": "EPMO"})
        assert atn.summarize_cm_logon(avlc) == ("CM", "LOGON RYR8414")

    def test_no_flight_summary_is_bare_logon(self):
        avlc = _cm_avlc({"departure_airport": "EPMO", "destination_airport": "LATI"})
        assert atn.cm_logon(avlc) == {"flight": None, "dep": "EPMO", "dest": "LATI"}
        assert atn.summarize_cm_logon(avlc) == ("CM", "LOGON")

    def test_bad_airports_become_none(self):
        for bad in ("epmo", "EP1O", "EPMOX", "EPM", "", " EPMO", "EPMO\n", 1234, None, ["EPMO"]):
            avlc = _cm_avlc({"flight_id": "RYR8414", "departure_airport": bad,
                             "destination_airport": "LATI"})
            assert atn.cm_logon(avlc) == {"flight": "RYR8414", "dep": None, "dest": "LATI"}, bad

    def test_bad_flight_ids_become_none(self):
        for bad in ("ryr8414", "R", "RYR841412X", "RYR 84", "RYR-84", "", 8414, None, {}):
            avlc = _cm_avlc({"flight_id": bad, "departure_airport": "EPMO",
                             "destination_airport": "LATI"})
            assert atn.cm_logon(avlc)["flight"] is None, bad

    def test_other_choice_is_none(self):
        assert atn.cm_logon(_cm_avlc({"flight_id": "RYR8414"}, choice="cmContactRequest")) is None
        assert atn.summarize_cm_logon(_cm_avlc({"flight_id": "RYR8414"}, choice="cmForward")) is None

    def test_non_dict_levels_are_none(self):
        assert atn.cm_logon(None) is None
        assert atn.cm_logon([]) is None
        assert atn.cm_logon({}) is None
        assert atn.cm_logon({"x25": "x"}) is None
        assert atn.cm_logon({"x25": {"clnp": []}}) is None
        assert atn.cm_logon({"x25": {"clnp": {"cotp": {"x225_spdu": 5}}}}) is None
        assert atn.cm_logon({"x25": {"clnp": {"cotp": {"x225_spdu": {"x227_apdu": []}}}}}) is None
        base = _cm_avlc({"flight_id": "RYR8414"})
        apdu = base["x25"]["clnp"]["cotp"]["x225_spdu"]["x227_apdu"]
        apdu["context_mgmt"] = "nope"
        assert atn.cm_logon(base) is None
        apdu["context_mgmt"] = {"cm_aircraft_message": "nope"}
        assert atn.cm_logon(base) is None
        apdu["context_mgmt"] = {"cm_aircraft_message": {"choice": "cmLogonRequest", "data": []}}
        assert atn.cm_logon(base) is None
        apdu["context_mgmt"] = {"cm_aircraft_message": {
            "choice": "cmLogonRequest", "data": {"atn_context_mgmt_logon_request": "x"}}}
        assert atn.cm_logon(base) is None
        assert atn.summarize_cm_logon(base) is None

    def test_cpdlc_frame_still_cpdlc_and_not_cm(self):
        assert atn.summarize_cpdlc(CPDLC_WILCO_AVLC) == ("CPDLC", "WILCO")
        assert atn.cm_logon(CPDLC_WILCO_AVLC) is None
        assert atn.summarize_cm_logon(CPDLC_WILCO_AVLC) is None
        assert atn.summarize_cpdlc(_real_cm_avlc()) is None
