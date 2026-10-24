"""Tests for the ADS-C (FANS-1/A over ACARS, label B6) basic_report extractor."""
from __future__ import annotations

import json

from readsbstats.vdl2 import adsc

# A real dumpvdl2 label-B6 ADS-C frame (A6-ENQ, EK098X) as stored in `raw`; validated
# against the ADS-B track of the same airframe (~0.1 nm median error).
REAL_RAW = '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785072609,"usec":718550},"freq":136725000,"burst_len_octets":137,"hdr_bits_fixed":0,"octets_corrected_by_fec":1,"idx":0,"sig_level":-46.262058,"noise_level":-52.787064,"freq_skew":1.281805,"avlc":{"src":{"addr":"896313","type":"Aircraft","status":"Airborne"},"dst":{"addr":"11920A","type":"Ground station"},"cr":"Command","frame_type":"I","rseq":5,"sseq":5,"poll":false,"acars":{"err":false,"crc_ok":true,"more":false,"reg":".A6-ENQ","mode":"2","label":"B6","blk_id":"3","ack":"!","flight":"EK098X","msg_num":"F05","msg_num_seq":"A","msg_text":"/RUHAAYA.ADS.A6-ENQ07251F1875DE40B25C401F0D25259875D040D500232523B8760640CB000E015061FFBC87A2","arinc622":{"msg_type":"adsc_msg","crc_ok":true,"gs_addr":"RUHAAYA","air_addr":".A6-ENQ","adsc":{"tags":[{"basic_report":{"lat":52.202053,"lon":20.719013,"alt":2852,"ts_sec":1808.0,"pos_accuracy_nm":0.05,"nav_redundancy":true,"tcas_avail":true}},{"predicted_route":{"next_wpt":{"lat":52.237759,"lon":20.7094,"alt":3408,"eta_sec":35},"next_next_wpt":{"lat":52.227459,"lon":20.746479,"alt":3248}}},{"earth_ref_data":{"true_trk_deg":3.691406,"true_trk_valid":true,"gnd_spd_kts":195.5,"vspd_ftmin":-272}}],"err":false}}}}}}'


def _raw(tags) -> str:
    return json.dumps({"vdl2": {"avlc": {"acars": {"arinc622": {"adsc": {"tags": tags}}}}}})


class TestParseBasicReport:
    def test_real_frame(self):
        rec = adsc.parse_basic_report(REAL_RAW)
        assert rec == {"lat": 52.20205, "lon": 20.71901, "alt": 2852}

    def test_first_basic_report_tag_wins(self):
        raw = _raw([{"earth_ref_data": {}},
                    {"basic_report": {"lat": 50.0, "lon": 19.0, "alt": 100}},
                    {"basic_report": {"lat": 51.0, "lon": 20.0, "alt": 200}}])
        assert adsc.parse_basic_report(raw) == {"lat": 50.0, "lon": 19.0, "alt": 100}

    def test_missing_alt_is_none(self):
        raw = _raw([{"basic_report": {"lat": 50.0, "lon": 19.0}}])
        assert adsc.parse_basic_report(raw) == {"lat": 50.0, "lon": 19.0, "alt": None}

    def test_no_basic_report_tag(self):
        assert adsc.parse_basic_report(_raw([{"predicted_route": {}}, {"earth_ref_data": {}}])) is None
        assert adsc.parse_basic_report(_raw([])) is None

    def test_non_dict_tags_and_levels(self):
        assert adsc.parse_basic_report(_raw(["x", 5, None, ["basic_report"]])) is None
        assert adsc.parse_basic_report(_raw({"basic_report": {}})) is None   # tags not a list
        assert adsc.parse_basic_report(_raw([{"basic_report": "x"}])) is None
        assert adsc.parse_basic_report(json.dumps({"vdl2": {"avlc": {"acars": {"arinc622": []}}}})) is None
        assert adsc.parse_basic_report(json.dumps({"vdl2": 5})) is None
        assert adsc.parse_basic_report(json.dumps([1, 2])) is None

    def test_out_of_range_or_missing_coords(self):
        assert adsc.parse_basic_report(_raw([{"basic_report": {"lat": 95.0, "lon": 19.0}}])) is None
        assert adsc.parse_basic_report(_raw([{"basic_report": {"lat": 50.0, "lon": 190.0}}])) is None
        assert adsc.parse_basic_report(_raw([{"basic_report": {"lat": 50.0}}])) is None
        assert adsc.parse_basic_report(_raw([{"basic_report": {"lat": True, "lon": 19.0}}])) is None

    def test_truncated_json_string(self):
        # RSBS_VDL2_RAW_MAX can cut the stored raw mid-JSON
        assert adsc.parse_basic_report(REAL_RAW[:600]) is None

    def test_non_str_and_empty(self):
        for bad in (None, 5, b"{}", "", {"vdl2": {}}):
            assert adsc.parse_basic_report(bad) is None
