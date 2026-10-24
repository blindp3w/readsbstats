"""Shared test helpers — extracted from per-test duplicates.

Audit-13 A13-085 / A13-086 created this module to deduplicate code
that was repeated identically across many test files:

- `make_db()` was defined in 13 test files (one connection, full DDL,
  `_migrate()`). Centralising it removes the drift surface — when
  schema rules change, only this file needs to know.
- `CountingConn` was defined in three purge test files. Same drift
  argument.

Anything added here should be either thread-of-truth (single source
for behaviour repeated everywhere) or a thin fixture-style wrapper.
Avoid adding per-test logic that only one file consumes.
"""

from __future__ import annotations

import sqlite3

from fastapi.routing import APIRoute

from readsbstats import database


def iter_api_routes(routes):
    """Yield every leaf ``APIRoute``, descending into included sub-routers.

    FastAPI 0.137 stopped flattening ``include_router()`` routes into
    ``app.routes``: each include is now an ``_IncludedRouter`` wrapper that holds
    the original ``APIRouter`` on ``.original_router`` (so the route/router
    instances are preserved). Older FastAPI kept flat ``APIRoute`` objects. This
    walk handles both — and recurses, so nested includes are covered too. Used by
    the route-guard / gating tests, which must see the real leaf routes (under the
    flat assumption they silently found *none* on 0.137 — a false-green for the
    mutating-route CSRF/auth guard)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)  # _IncludedRouter (0.137+)
        if included is not None and hasattr(included, "routes"):
            yield from iter_api_routes(included.routes)


def make_db() -> sqlite3.Connection:
    """Fresh in-memory SQLite with the full DDL + migrations applied.

    Matches the production startup path: `database.connect()` (which sets
    WAL + busy_timeout + the project's pragmas), then `executescript(DDL)`
    for tables/indexes, then `_migrate()` for any column additions or
    background-migration-relevant schema bumps. Always returns the same
    shape; if a test needs a real-file DB, it should use a tempfile path
    and call `database.connect(path)` directly.
    """
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def make_vdl2_db() -> sqlite3.Connection:
    """Fresh in-memory VDL2 SQLite with its schema applied.

    Parallels :func:`make_db` but for the separate ``vdl2.db`` store. Uses
    the feature's own connect()/ensure_schema() so FTS5 + triggers are built
    exactly as in production (when this SQLite build has FTS5)."""
    from readsbstats.vdl2 import db as vdl2_db

    conn = vdl2_db.connect(":memory:")
    vdl2_db.ensure_schema(conn)
    return conn


def insert_position(conn, flight_id, ts, lat=None, lon=None, alt_baro=None,
                    alt_geom=None, gs=None, track=None, baro_rate=None,
                    rssi=None, source_type="adsb_icao"):
    """Insert one v6 positions row from HUMAN units (degrees, knots, dB).
    Tests must use this instead of hand-written INSERT INTO positions —
    it owns the posenc encoding so schema changes touch one place.
    Returns the new row id."""
    from readsbstats import posenc
    cur = conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, alt_baro, alt_geom,"
        " gs, track, baro_rate, rssi, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (flight_id, ts, posenc.enc5(lat), posenc.enc5(lon), alt_baro, alt_geom,
         posenc.enc1(gs), posenc.enc1(track), baro_rate, posenc.enc1(rssi),
         posenc.encode_source(source_type)),
    )
    return cur.lastrowid


class CountingConn:
    """Sqlite3 connection wrapper that counts `.commit()` calls.

    Used by `tests/test_purge_*.py` to assert that batched purges commit
    once per `_BATCH_SIZE` flights — not per flight, not once at the end.
    Forwards every other attribute access to the wrapped connection.
    """

    def __init__(self, c: sqlite3.Connection, raise_on_commit: int | None = None):
        self._c = c
        self.commits = 0
        # When set, the Nth commit() raises instead of committing — simulating a
        # mid-run crash so tests can verify interrupted purges are resumable.
        self._raise_on_commit = raise_on_commit

    def __getattr__(self, name: str):  # pragma: no cover — passthrough
        return getattr(self._c, name)

    def commit(self) -> None:
        self.commits += 1
        if self._raise_on_commit is not None and self.commits >= self._raise_on_commit:
            raise RuntimeError("simulated interruption")
        self._c.commit()

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            # `with conn:` commits on clean exit — count it like .commit()
            self.commits += 1
        return self._c.__exit__(exc_type, exc, tb)


# Real dumpvdl2 frames (captured from the Pi feed) for tests that exercise the
# true ingest path: M1 position report, M1 with /RP: filed route, M1 with an
# MFI (B0), an XID handoff carrying ac_location + dst_airport, and an ATN
# Context-Management logon (flight id + departure + destination).
DUMPVDL2_FRAMES = {
    'm1_pos': '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785096166,"usec":698842},"freq":136775000,"burst_len_octets":117,"hdr_bits_fixed":0,"octets_corrected_by_fec":3,"idx":0,"sig_level":-48.149063,"noise_level":-52.339676,"freq_skew":2.204034,"avlc":{"src":{"addr":"4787A0","type":"Aircraft","status":"Airborne"},"dst":{"addr":"2391C5","type":"Ground station"},"cr":"Command","frame_type":"I","rseq":4,"sseq":2,"poll":false,"acars":{"err":false,"crc_ok":true,"more":false,"reg":".LN-NIB","mode":"2","label":"H1","blk_id":"8","ack":"!","flight":"DY1577","msg_num":"F57","msg_num_seq":"A","sublabel":"M1","msg_text":"POSN52256E019186,ADOXO,200245,380,PENOR,202912,KSD,M52,226050,11122DB"}}}}',
    'm1_route': '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785138022,"usec":410578},"freq":136875000,"burst_len_octets":256,"hdr_bits_fixed":0,"octets_corrected_by_fec":0,"idx":0,"sig_level":-44.885689,"noise_level":-52.587124,"freq_skew":3.288023,"avlc":{"src":{"addr":"47BF9D","type":"Aircraft","status":"Airborne"},"dst":{"addr":"2B86E1","type":"Ground station"},"cr":"Command","frame_type":"I","rseq":5,"sseq":5,"poll":false,"acars":{"err":false,"crc_ok":true,"more":true,"reg":".LN-FGJ","mode":"2","label":"H1","blk_id":"5","ack":"!","flight":"DY02KU","msg_num":"F16","msg_num_seq":"A","sublabel":"M1","msg_text":"POSN52182E020495,WA932,074020,63,WA791,074058,INRAS,P10,242026,120/DTENGM,19R,48,091243,73/PR1489,154,320,120,,33,40,207045,M46,65,P22,P0/RP:DA:EPWA:AA:ENGM(19R):D:OLIL6G:F:OLILA.L621.GRUDA..OSLOG..PENOR..K"}}}}',
    'm1_mfi': '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785134699,"usec":713040},"freq":136725000,"burst_len_octets":130,"hdr_bits_fixed":0,"octets_corrected_by_fec":0,"idx":0,"sig_level":-46.04364,"noise_level":-52.785419,"freq_skew":3.254894,"avlc":{"src":{"addr":"48EA00","type":"Aircraft","status":"Airborne"},"dst":{"addr":"11920A","type":"Ground station"},"cr":"Command","frame_type":"I","rseq":4,"sseq":7,"poll":false,"acars":{"err":false,"crc_ok":true,"more":false,"reg":".SP-EXM","mode":"2","label":"H1","blk_id":"9","ack":"!","flight":"E401KP","msg_num":"F00","msg_num_seq":"A","sublabel":"M1","mfi":"B0","msg_text":"EPWW.AFN/FMHENT1KP,.SP-EXM,,064457/FPON52007E020075,1/FCOATC,01/FCOADS,01A21F"}}}}',
    'xid_ho': '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785065967,"usec":212646},"freq":136725000,"burst_len_octets":65,"hdr_bits_fixed":0,"octets_corrected_by_fec":0,"idx":0,"sig_level":-46.492935,"noise_level":-52.837135,"freq_skew":4.27249,"avlc":{"src":{"addr":"48C132","type":"Aircraft","status":"Airborne"},"dst":{"addr":"11920A","type":"Ground station"},"cr":"Command","frame_type":"U","cmd":"XID","pf":true,"xid":{"err":false,"type":"XID_CMD_HO","type_descr":"Handoff Initiation","pub_params":[{"name":"param_set_id","value":"8885:1993"},{"name":"procedure_classes","value":[0,1]},{"name":"hdlc_options","value":[32,132,128]}],"vdl_params":[{"name":"param_set_id","value":"V"},{"name":"xid_sequencing","value":{"seq":1,"retry":0}},{"name":"dst_airport","value":"LATI"},{"name":"ac_location","value":{"loc":{"lat":52.299999,"lon":20.5},"alt":31000}},{"name":"avlc_specific_options","value":[36]},{"name":"conn_mgmt","value":1}]}}}}',
    'cm_logon': '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"station":"homepi","t":{"sec":1785066050,"usec":874883},"freq":136725000,"burst_len_octets":112,"hdr_bits_fixed":0,"octets_corrected_by_fec":0,"idx":0,"sig_level":-45.792747,"noise_level":-52.760574,"freq_skew":4.056958,"avlc":{"src":{"addr":"48C132","type":"Aircraft","status":"Airborne"},"dst":{"addr":"11920A","type":"Ground station"},"cr":"Command","frame_type":"I","rseq":4,"sseq":3,"poll":false,"x25":{"err":false,"pkt_type":0,"pkt_type_name":"Data","chan_group":11,"chan_num":254,"sseq":2,"rseq":1,"more":false,"reasm_status":"skipped","clnp":{"err":false,"compressed":true,"local_ref_a":0,"priority":8,"lifetime":50.0,"flags":192,"pdu_id":343,"cotp":{"pdu_list":[{"err":false,"tpdu_code":240,"tpdu_code_descr":"Data","extended":false,"dst_ref":61730,"sseq":0,"req_of_ack":0,"eot":1,"reasm_status":"skipped","variable_part_params":[{"name":"checksum","value":[229,150]}]}],"x225_spdu":{"spdu_id":232,"spdu_type":"Short Connect","x227_apdu":{"acse_apdu":{"choice":"aarq","data":{"assoc_request":{"app_ctx_name":[1,3,27,3,1],"ap_title":{"choice":"ap-title-form2","data":{"ap_title_form2":[1,3,27,1,4768050,0]}},"ae_qualifier":{"choice":"ae-qualifier-form2","data":{"ae_qualifier_form2":1}}}}},"context_mgmt":{"cm_aircraft_message":{"choice":"cmLogonRequest","data":{"atn_context_mgmt_logon_request":{"flight_id":"RYR8414","long_tsap":{"rdp":[65,82,89,82,0],"short_tsap":{"ars":[72,193,50],"loc_sys_nsel_tsel":[0,0,0,0,0,0,0,1,1,1,1]}},"ground_initiated_apps":[{"ae_qualifier_version_address":{"ae_qualifier":22,"version":1,"ap_address":{"choice":"longTsap","data":{"long_tsap":{"rdp":[65,82,89,82,0],"short_tsap":{"ars":[72,193,50],"loc_sys_nsel_tsel":[0,0,0,0,0,0,0,1,1,2,2]}}}}}}],"departure_airport":"EPMO","destination_airport":"LATI"}}}}}}}}}}}}',
}


def dumpvdl2_record(name: str, ts: int | None = None) -> dict:
    """Normalized dumpvdl2 record for a DUMPVDL2_FRAMES entry, optionally re-timestamped."""
    import json

    from readsbstats.vdl2 import normalize

    obj = json.loads(DUMPVDL2_FRAMES[name])
    if ts is not None:
        obj["vdl2"]["t"]["sec"] = ts
    return normalize.normalize(obj, "dumpvdl2")
