"""Tests for the decoder-agnostic VDL2 normalizer."""
from __future__ import annotations

import time

from readsbstats import config
from readsbstats.vdl2 import m1bpos, normalize


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

    def test_no_signal_levels(self):
        # vdlm2dec's JSON carries no signal field — both must be NULL so the
        # Metrics signal/SNR row self-hides on a vdlm2dec feed.
        rec = normalize.normalize(SAMPLE_VDLM2DEC)
        assert rec["sig_level"] is None
        assert rec["noise_level"] is None


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

    def test_negative_freq_dropped(self):
        """A glitchy negative freq must normalize to None, not a tiny negative
        MHz value that would show as a junk channel. Audit 2026-06-20."""
        raw = {"vdl2": {"freq": -1000000,
                        "avlc": {"src": {"addr": "48af11"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["freq"] is None

    def test_explicit_decoder_arg_overrides_config(self, monkeypatch):
        """normalize(raw, decoder=…) overrides config.VDL2_DECODER — the public
        per-call contract, not just the config-driven path. Audit 2026-06-20."""
        monkeypatch.setattr(config, "VDL2_DECODER", "vdlm2dec")
        raw = {"vdl2": {"avlc": {"src": {"addr": "48af11"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["decoder"] == "dumpvdl2"

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

    def test_registration_leading_dot_stripped(self):
        # dumpvdl2's ACARS reg field is left-padded with '.' (e.g. '.TC-NCU').
        # Must be stripped so it matches vdlm2dec's clean tail / core regs.
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"reg": ".TC-NCU", "msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["registration"] == "TC-NCU"

    def test_registration_dot_only_is_none(self):
        # A reg of just the pad char carries no identity; record survives on body.
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"reg": ".", "msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["registration"] is None

    def test_registration_leading_digit_preserved(self):
        # lstrip removes only the leading dot/space — a reg starting with a digit
        # (e.g. Maltese 9H-) must keep that digit.
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"reg": ".9H-WAU", "msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["registration"] == "9H-WAU"

    def test_registration_dot_space_pad_stripped(self):
        # lstrip(". ") strips a leading dot+space pad that clean_short_text's outer
        # .strip() leaves behind (the space is interior to the leading dot).
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"reg": ". SP-LVS", "msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["registration"] == "SP-LVS"

    def test_registration_interior_dot_preserved(self):
        # Contract lock: only LEADING dots are stripped (lstrip, not replace/strip);
        # an interior dot must survive so a real reg is never collapsed.
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"reg": ".A.BC", "msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["registration"] == "A.BC"

    def test_station_id_from_vdl2_station(self):
        # dumpvdl2 serializes the --station-id value as vdl2.station (not station_id,
        # not top-level). Confirmed against dumpvdl2 src/fmtr-json.c.
        raw = {"vdl2": {"station": "homepi",
                        "avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["station_id"] == "homepi"

    def test_station_id_absent_is_none(self):
        # --station-id not configured → key absent → NULL (not a crash).
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["station_id"] is None

    def test_bare_cpdlc_frame_gets_label_and_body(self):
        # A pure-ATN CPDLC frame (no acars) → label='CPDLC' + intent body, so it is
        # no longer a content-free bare row. reg/flight stay None (no ACARS wrapper).
        raw = {"vdl2": {"freq": 136725000, "t": {"sec": 1781477820}, "avlc": {
            "src": {"addr": "48C233"},
            "x25": {"clnp": {"cotp": {"cpdlc": {"atc_downlink_message": {
                "msg_data": {"msg_elements": [
                    {"msg_element": {"choice_label": "WILCO"}}]}}}}}},
        }}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["label"] == "CPDLC"
        assert rec["body"] == "WILCO"
        assert rec["icao_hex"] == "48c233"
        assert rec["registration"] is None
        assert rec["flight"] is None
        assert rec["freq"] == 136.725

    def test_cm_logon_frame_gets_label_body_and_dsta(self):
        # ATN Context-Management logon (real frame): the aircraft's own FMS flight id
        # + dep + dest land in body; dest feeds dsta. `flight` stays NULL on purpose —
        # it's an ICAO callsign, while top_airlines assumes IATA-style ACARS flight ids.
        from tests._helpers import dumpvdl2_record
        rec = dumpvdl2_record("cm_logon", 1_700_000_000)
        assert rec["label"] == "CM"
        assert rec["body"] == "LOGON RYR8414 EPMO-LATI"
        assert rec["dsta"] == "LATI"
        assert rec["flight"] is None
        assert rec["registration"] is None
        assert rec["icao_hex"] == "48c132"

    def test_cm_logon_without_route_keeps_dsta_none(self):
        raw = {"vdl2": {"avlc": {"src": {"addr": "48C132"}, "x25": {"clnp": {"cotp": {
            "x225_spdu": {"x227_apdu": {"context_mgmt": {"cm_aircraft_message": {
                "choice": "cmLogonRequest",
                "data": {"atn_context_mgmt_logon_request": {"flight_id": "RYR8414"}}}}}}}}}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["label"] == "CM" and rec["body"] == "LOGON RYR8414"
        assert rec["dsta"] is None

    def test_xid_dsta_takes_precedence_over_cm(self):
        # XID and CM never co-occur in one real frame; if they did, XID wins.
        raw = {"vdl2": {"avlc": {
            "src": {"addr": "48C132"},
            "xid": {"vdl_params": [{"name": "dst_airport", "value": "EPWA"}]},
            "x25": {"clnp": {"cotp": {"x225_spdu": {"x227_apdu": {"context_mgmt": {
                "cm_aircraft_message": {"choice": "cmLogonRequest", "data": {
                    "atn_context_mgmt_logon_request": {
                        "flight_id": "RYR8414", "departure_airport": "EPMO",
                        "destination_airport": "LATI"}}}}}}}}},
        }}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["dsta"] == "EPWA"
        assert rec["body"] == "LOGON RYR8414 EPMO-LATI"

    def test_bare_transport_frame_stays_bare(self):
        # Content-free ATN transport/routing (x25, no cpdlc, no acars) keeps NULL
        # label/body but survives on icao_hex — "keep bare" is intentional.
        raw = {"vdl2": {"avlc": {
            "src": {"addr": "896622"},
            "x25": {"clnp": {"idrp": {"err": False}}},
        }}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None
        assert rec["label"] is None
        assert rec["body"] is None
        assert rec["icao_hex"] == "896622"

    def test_signal_levels_captured(self):
        # dumpvdl2 emits per-frame sig_level/noise_level (dBFS) at the vdl2 level,
        # siblings of freq — confirmed against real --output decoded:json captures.
        raw = {"vdl2": {"freq": 136725000, "sig_level": -47.6, "noise_level": -52.8,
                        "avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["sig_level"] == -47.6
        assert rec["noise_level"] == -52.8

    def test_signal_levels_absent_are_none(self):
        # A frame without the fields (older dumpvdl2 / partial frame) → NULL, no crash.
        raw = {"vdl2": {"avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["sig_level"] is None
        assert rec["noise_level"] is None

    def test_signal_levels_numeric_strings_parsed(self):
        # _float_or_none tolerates quoted numerics, consistent with freq/lat/lon.
        raw = {"vdl2": {"sig_level": "-47.6", "noise_level": "-52.8",
                        "avlc": {"src": {"addr": "4bb875"},
                                 "acars": {"msg_text": "x"}}}}
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["sig_level"] == -47.6
        assert rec["noise_level"] == -52.8


class TestMoreFlag:
    @staticmethod
    def _rec(acars=None, **extra):
        avlc = {"src": {"addr": "4bb875"}}
        if acars is not None:
            avlc["acars"] = acars
        avlc.update(extra)
        return normalize.normalize({"vdl2": {"avlc": avlc}}, decoder="dumpvdl2")

    def test_more_true_is_1(self):
        assert self._rec({"msg_text": "x", "more": True})["more"] == 1

    def test_more_false_is_0(self):
        assert self._rec({"msg_text": "x", "more": False})["more"] == 0

    def test_acars_without_more_key_is_0(self):
        assert self._rec({"msg_text": "x"})["more"] == 0

    def test_xid_frame_is_none(self):
        raw = {"vdl2": {"avlc": {"src": {"addr": "48c132"}, "cmd": "XID", "xid": {
            "vdl_params": [{"name": "dst_airport", "value": "LATI"}]}}}}
        assert normalize.normalize(raw, decoder="dumpvdl2")["more"] is None

    def test_atn_frame_is_none(self):
        # x25.more is an X.25 flag, not ACARS fragmentation — must not leak in
        import json
        from tests._helpers import DUMPVDL2_FRAMES
        rec = normalize.normalize(json.loads(DUMPVDL2_FRAMES["cm_logon"]), decoder="dumpvdl2")
        assert rec["label"] == "CM" and rec["more"] is None

    def test_real_fragment_frame(self):
        import json
        from tests._helpers import DUMPVDL2_FRAMES
        rec = normalize.normalize(json.loads(DUMPVDL2_FRAMES["m1_route"]), decoder="dumpvdl2")
        assert rec["more"] == 1

    def test_vdlm2dec_leaves_more_unset(self):
        rec = normalize.normalize(dict(SAMPLE_VDLM2DEC), decoder="vdlm2dec")
        assert rec.get("more") is None


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


def _dump(avlc_extra: dict, *, src: str = "4D241C") -> dict:
    """Minimal dumpvdl2 frame wrapper around an ``avlc`` payload."""
    return {"vdl2": {"freq": 136775000, "t": {"sec": 1782559365},
                     "avlc": {"src": {"addr": src}, **avlc_extra}}}


def _xid(vdl_params, *, xid_type: str = "XID_CMD_HO") -> dict:
    return _dump({"frame_type": "U", "cmd": "XID",
                  "xid": {"err": False, "type": xid_type, "vdl_params": vdl_params}})


# Real XID handoff-initiation frame (trimmed): ac_location + dst_airport.
XID_HO_PARAMS = [
    {"name": "param_set_id", "value": "V"},
    {"name": "conn_mgmt", "value": 1},
    {"name": "xid_sequencing", "value": {"seq": 3, "retry": 4}},
    {"name": "avlc_specific_options", "value": [37]},
    {"name": "dst_airport", "value": "EPWA"},
    {"name": "ac_location", "value": {"loc": {"lat": 52.299999, "lon": 20.5}, "alt": 5000}},
]


class TestDumpvdl2Xid:
    """XID frames carry the aircraft's coarse position + destination airport."""

    def test_xid_handoff_maps_position_alt_dest(self):
        rec = normalize.normalize(_xid(XID_HO_PARAMS), decoder="dumpvdl2")
        assert rec is not None
        assert abs(rec["lat"] - 52.3) < 0.001
        assert rec["lon"] == 20.5
        assert rec["alt"] == 5000
        assert rec["dsta"] == "EPWA"
        assert rec["icao_hex"] == "4d241c"

    def test_xid_gs_location_is_never_used(self):
        # gs_location is the GROUND STATION's position, not the aircraft's.
        rec = normalize.normalize(
            _xid([{"name": "gs_location", "value": {"lat": 52.1, "lon": 20.9}}]),
            decoder="dumpvdl2")
        assert rec is not None
        assert rec["lat"] is None and rec["lon"] is None and rec["alt"] is None

    def test_xid_lcr_without_location_all_none(self):
        params = [
            {"name": "param_set_id", "value": "V"},
            {"name": "conn_mgmt", "value": 6},
            {"name": "lcr_cause", "value": {"cause_code": 8, "delay": 0}},
        ]
        rec = normalize.normalize(_xid(params, xid_type="XID_CMD_LCR"), decoder="dumpvdl2")
        assert rec is not None          # still a row: icao_hex present
        assert rec["icao_hex"] == "4d241c"
        assert rec["lat"] is None and rec["lon"] is None
        assert rec["alt"] is None and rec["dsta"] is None

    def test_xid_junk_vdl_params_types_do_not_raise(self):
        for params in ("junk", 5, None, {"name": "dst_airport"},
                       [1, "x", None, ["a"], {"name": 7, "value": "EPWA"}, {"value": "EPWA"}]):
            rec = normalize.normalize(_xid(params), decoder="dumpvdl2")
            assert rec is not None
            assert rec["lat"] is None and rec["lon"] is None
            assert rec["alt"] is None and rec["dsta"] is None

    def test_xid_non_dict_xid_ignored(self):
        raw = _dump({"frame_type": "U", "cmd": "XID", "xid": "junk"})
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec is not None and rec["lat"] is None

    def test_xid_bad_ac_location_shapes(self):
        for loc in ("52.3,20.5", None, 7, [], {}, {"alt": 5000, "loc": "x"},
                    {"alt": 5000}):
            rec = normalize.normalize(
                _xid([{"name": "ac_location", "value": loc}]), decoder="dumpvdl2")
            assert rec is not None
            assert rec["lat"] is None and rec["lon"] is None

    def test_xid_alt_kept_when_loc_missing(self):
        rec = normalize.normalize(
            _xid([{"name": "ac_location", "value": {"alt": 5000}}]), decoder="dumpvdl2")
        assert rec["alt"] == 5000 and rec["lat"] is None

    def test_xid_out_of_range_lat_dropped(self):
        rec = normalize.normalize(
            _xid([{"name": "ac_location", "value": {"loc": {"lat": 123.0, "lon": 20.5}}}]),
            decoder="dumpvdl2")
        assert rec["lat"] is None
        assert rec["lon"] == 20.5

    def test_xid_dst_airport_non_string_is_none(self):
        rec = normalize.normalize(
            _xid([{"name": "dst_airport", "value": {"x": 1}}]), decoder="dumpvdl2")
        assert rec["dsta"] is None

    def test_non_xid_frame_keeps_none_position(self):
        raw = _dump({"acars": {"label": "H1", "msg_text": "x"}})
        rec = normalize.normalize(raw, decoder="dumpvdl2")
        assert rec["lat"] is None and rec["lon"] is None
        assert rec["alt"] is None and rec["dsta"] is None


def _h1(msg_text: str, **acars_extra) -> dict:
    return _dump({"acars": {"reg": ".LN-NIB", "label": "H1", "msg_text": msg_text,
                            **acars_extra}}, src="4787A0")


class TestSublabelMfi:
    """libacars strips the H1 ``#<SL>B`` / ``/<MFI> `` prefix into separate fields."""

    POS_BODY = "POSN52256E019186,ADOXO,200245,380,PENOR,202912,KSD,M52,226050,11122DB"
    RP_BODY = ("POSN52182E020495,WA932,074020,63,WA791,074058,INRAS,P10,242026,120"
               "/DTENGM,19R,48,091243,73/PR1489,154,320,120,,33,40,207045,M46,65,P22,P0"
               "/RP:DA:EPWA:AA:ENGM(19R):D:OLIL6G:F:OLILA.L621.GRUDA..OSLOG..PENOR..K")
    MFI_BODY = "EPWW.AFN/FMHENT1KP,.SP-EXM,,064457/FPON52007E020075,1/FCOATC,01/FCOADS,01A21F"

    def test_sublabel_mapped_mfi_none(self):
        rec = normalize.normalize(_h1(self.POS_BODY, sublabel="M1"), decoder="dumpvdl2")
        assert rec["sublabel"] == "M1"
        assert rec["mfi"] is None
        assert rec["body"] == self.POS_BODY      # body stays un-prefixed

    def test_sublabel_and_mfi_mapped(self):
        rec = normalize.normalize(
            _h1(self.MFI_BODY, sublabel="M1", mfi="B0"), decoder="dumpvdl2")
        assert rec["sublabel"] == "M1"
        assert rec["mfi"] == "B0"

    def test_missing_keys_are_none(self):
        rec = normalize.normalize(_h1("hello"), decoder="dumpvdl2")
        assert rec["sublabel"] is None and rec["mfi"] is None

    def test_non_string_sublabel_coerced_safely(self):
        rec = normalize.normalize(
            _h1("hello", sublabel={"x": 1}, mfi=["B0"]), decoder="dumpvdl2")
        assert rec["sublabel"] is None and rec["mfi"] is None

    def test_vdlm2dec_has_no_sublabel_or_mfi(self):
        rec = normalize.normalize(SAMPLE_VDLM2DEC, decoder="vdlm2dec")
        assert rec.get("sublabel") is None
        assert rec.get("mfi") is None


class TestAcarsText:
    def test_prefix_rebuilt_with_sublabel(self):
        assert normalize.acars_text("POSN52", "M1", None) == "#M1BPOSN52"

    def test_prefix_rebuilt_with_mfi(self):
        assert normalize.acars_text("EPWW.AFN/FMH", "M1", "B0") == "#M1B/B0 EPWW.AFN/FMH"

    def test_no_sublabel_unchanged(self):
        assert normalize.acars_text("hello", None, None) == "hello"
        assert normalize.acars_text("hello", "", "B0") == "hello"

    def test_empty_or_none_body_unchanged(self):
        assert normalize.acars_text(None, "M1", "B0") is None
        assert normalize.acars_text("", "M1", None) == ""

    def test_m1_position_frame_parses_via_m1bpos(self):
        rec = normalize.normalize(
            _h1(TestSublabelMfi.POS_BODY, sublabel="M1"), decoder="dumpvdl2")
        assert m1bpos.parse_position(rec["body"]) is None   # stripped body: no tag
        text = normalize.acars_text(rec["body"], rec["sublabel"], rec["mfi"])
        assert text.startswith("#M1BPOSN52256E019186")
        pos = m1bpos.parse_position(text)
        assert pos is not None
        assert abs(pos["lat"] - 52.42667) < 0.001

    def test_m1_route_frame_parses_via_m1bpos(self):
        rec = normalize.normalize(
            _h1(TestSublabelMfi.RP_BODY, sublabel="M1"), decoder="dumpvdl2")
        text = normalize.acars_text(rec["body"], rec["sublabel"], rec["mfi"])
        route = m1bpos.parse_route(text)
        assert route is not None
        assert route["dep"] == "EPWA" and route["arr"] == "ENGM(19R)"
