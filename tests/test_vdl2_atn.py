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
