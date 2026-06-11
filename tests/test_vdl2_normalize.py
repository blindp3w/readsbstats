"""Tests for the decoder-agnostic VDL2 normalizer."""
from __future__ import annotations

import time

from readsbstats import config
from readsbstats.vdl2 import normalize


SAMPLE_VDLM2DEC = {
    "timestamp": 1749065117.645,
    "station_id": "EPWA-1",
    "freq": 136.725,
    "hex": "48E95D",          # uppercase from decoder — must be lowercased
    "icao": "48E95D",
    "toaddr": "11920A",
    "tail": "SP-LYF",
    "flight": "LO6550",
    "label": "H1",
    "mode": "2",
    "block_id": "0",
    "ack": "!",
    "msgno": "D51A",
    "text": "#DFBABS001DA_S DTNHEPWA 86",
    "dsta": "EPWA",
    "app": {"name": "vdlm2dec", "ver": "2.4"},
}


class TestNormalizeVdlm2dec:
    def test_maps_core_fields(self):
        rec = normalize.normalize(SAMPLE_VDLM2DEC)
        assert rec is not None
        assert rec["icao_hex"] == "48e95d"          # lowercased to match core
        assert rec["registration"] == "SP-LYF"
        assert rec["flight"] == "LO6550"
        assert rec["label"] == "H1"
        assert rec["dsta"] == "EPWA"
        assert rec["body"] == "#DFBABS001DA_S DTNHEPWA 86"
        assert rec["app_name"] == "vdlm2dec"
        assert rec["app_ver"] == "2.4"
        assert rec["decoder"] == "vdlm2dec"
        assert rec["freq"] == 136.725

    def test_epoch_float_timestamp_becomes_int_seconds(self):
        rec = normalize.normalize(SAMPLE_VDLM2DEC)
        assert rec["ts"] == 1749065117
        assert isinstance(rec["ts"], int)

    def test_missing_timestamp_falls_back_to_now(self):
        raw = {k: v for k, v in SAMPLE_VDLM2DEC.items() if k != "timestamp"}
        before = int(time.time())
        rec = normalize.normalize(raw)
        assert rec["ts"] >= before

    def test_raw_json_preserved(self):
        rec = normalize.normalize(SAMPLE_VDLM2DEC)
        assert '"hex":"48E95D"' in rec["raw"]   # verbatim, original casing

    def test_missing_fields_tolerated(self):
        rec = normalize.normalize({"hex": "abcdef", "timestamp": 1})
        assert rec is not None
        assert rec["icao_hex"] == "abcdef"
        assert rec["body"] is None
        assert rec["registration"] is None

    def test_non_string_field_coerced_to_none(self):
        # clean_short_text drops non-string values rather than binding a non-str
        rec = normalize.normalize({"hex": "abcdef", "tail": 12345, "text": "hi"})
        assert rec["registration"] is None

    def test_body_capped(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_BODY_MAX", 10)
        rec = normalize.normalize({"hex": "abcdef", "text": "x" * 100})
        assert len(rec["body"]) == 10

    def test_numeric_strings_parsed(self):
        # Some decoder dialects quote numeric fields; they must still store as
        # numbers, not silently drop to NULL.
        rec = normalize.normalize({"hex": "abcdef", "text": "x", "freq": "136.975",
                                   "lat": "52.2", "lon": "21.0", "alt": "36000"})
        assert rec["freq"] == 136.975
        assert rec["lat"] == 52.2
        assert rec["alt"] == 36000

    def test_label_uppercased(self):
        rec = normalize.normalize({"hex": "abcdef", "label": "h1", "text": "x"})
        assert rec["label"] == "H1"

    def test_raw_capped(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_RAW_MAX", 50)
        rec = normalize.normalize({"hex": "abcdef", "text": "x", "pad": "z" * 500})
        assert len(rec["raw"]) == 50

    def test_far_future_ts_clamped_to_now(self):
        future = int(time.time()) + 10 * 86400
        rec = normalize.normalize({"hex": "abcdef", "text": "x", "timestamp": future})
        assert rec["ts"] <= int(time.time()) + 1   # clamped to ~now

    def test_old_ts_preserved(self):
        rec = normalize.normalize({"hex": "abcdef", "text": "x", "timestamp": 1_000_000})
        assert rec["ts"] == 1_000_000   # legitimate old/backfill ts kept

    def test_non_dict_returns_none(self):
        assert normalize.normalize("not a dict") is None
        assert normalize.normalize(None) is None
        assert normalize.normalize([1, 2, 3]) is None

    def test_empty_record_dropped(self):
        # No identity and no body → nothing worth storing.
        assert normalize.normalize({"timestamp": 123, "freq": 136.9}) is None

    # F05: identity/coordinate fields validated at the trust boundary.

    def test_invalid_icao_hex_rejected(self):
        # "48ok01" is 6 chars but not all hex — must not pass through.
        rec = normalize.normalize({"hex": "48ok01", "text": "hi"})
        assert rec is not None
        assert rec["icao_hex"] is None

    def test_out_of_range_lat_rejected(self):
        rec = normalize.normalize({"hex": "abcdef", "text": "x",
                                   "lat": 95, "lon": 21.0})
        assert rec["lat"] is None
        assert rec["lon"] == 21.0

    def test_valid_icao_and_coords_preserved(self):
        rec = normalize.normalize({"hex": "48E95D", "text": "x",
                                   "lat": 52.2, "lon": 21.0})
        assert rec["icao_hex"] == "48e95d"
        assert rec["lat"] == 52.2
        assert rec["lon"] == 21.0


class TestNormalizeDumpvdl2:
    def test_basic_acars_mapping(self):
        raw = {
            "vdl2": {
                "freq": 136975000,
                "t": {"sec": 1749065200},
                "avlc": {
                    "src": {"addr": "48AF11"},
                    "acars": {"reg": "SP-LVS", "flight": "LO0304",
                              "label": "H1", "msg_text": "HELLO"},
                },
            }
        }
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["icao_hex"] == "48af11"
        assert rec["registration"] == "SP-LVS"
        assert rec["flight"] == "LO0304"
        assert rec["body"] == "HELLO"
        assert rec["decoder"] == "dumpvdl2"
        assert rec["freq"] == 136.975   # 136975000 Hz → MHz

    def test_empty_dumpvdl2_dropped(self):
        assert normalize.normalize({"vdl2": {"avlc": {}}}, decoder="dumpvdl2") is None

    def test_invalid_icao_hex_rejected(self):
        # F05: dumpvdl2 src.addr is validated the same way. A non-hex 6-char
        # value is dropped; the body keeps the record alive.
        raw = {
            "vdl2": {
                "avlc": {
                    "src": {"addr": "48ok01"},
                    "acars": {"msg_text": "HELLO"},
                },
            }
        }
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["icao_hex"] is None


class TestHelperCoercion:
    """Residual branches of the _ts / _num field coercers."""

    def test_ts_bool_falls_back_to_now(self):
        # isinstance(True, int) is True — a JSON `true` must not become ts=1.
        now = int(time.time())
        assert abs(normalize._ts(True) - now) <= 1

    def test_ts_numeric_string_parses(self):
        assert normalize._ts("1749065117.6") == 1749065117

    def test_ts_garbage_string_falls_back_to_now(self):
        now = int(time.time())
        assert abs(normalize._ts("yesterday") - now) <= 1

    def test_num_numeric_string_accepted(self):
        # Some decoder dialects quote freq/lat/lon.
        assert normalize._num("136.975") == 136.975

    def test_num_garbage_string_rejected(self):
        assert normalize._num("not-a-number") is None
