"""Tests for scripts/backfill_vdl2_dumpvdl2.py (one-shot fill of dumpvdl2 columns
that pre-fix ingest left NULL: sublabel/mfi + XID lat/lon/alt/dsta)."""
from __future__ import annotations

import json

import pytest

from backfill_vdl2_dumpvdl2 import backfill, main
from readsbstats.vdl2 import db as vdl2_db
from readsbstats.vdl2 import normalize
from tests._helpers import dumpvdl2_record

_FILLED = ("sublabel", "mfi", "lat", "lon", "alt", "dsta")
_FULL_TEXT = "POSN52256E019186,ADOXO,200245,380,PENOR,202912,KSD,M52,226050,11122DB"


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "vdl2.db")
    conn = vdl2_db.connect(path)
    vdl2_db.ensure_schema(conn)
    conn.close()
    return path


def _prefix_row(name: str, ts: int = 1_700_000_000) -> dict:
    """A record as pre-fix ingest stored it: real raw, derived columns NULL."""
    rec = dumpvdl2_record(name, ts)
    for c in _FILLED:
        rec[c] = None
    return rec


def _cm_prefix_row(ts: int = 1_700_000_000) -> dict:
    """A CM logon as pre-2.27 ingest stored it: content-free (label/body/dsta NULL)."""
    rec = dumpvdl2_record("cm_logon", ts)
    for c in ("label", "body", "dsta"):
        rec[c] = None
    return rec


def _seed(path: str, records: list[dict]) -> None:
    conn = vdl2_db.connect(path)
    vdl2_db.insert_messages(conn, records)
    conn.commit()
    conn.close()


def _rows(path: str) -> dict:
    conn = vdl2_db.connect(path)
    try:
        return {r["icao_hex"]: dict(r) for r in conn.execute("SELECT * FROM vdl2_messages")}
    finally:
        conn.close()


def test_fills_sublabel_mfi_and_xid_columns(db_path):
    _seed(db_path, [_prefix_row("m1_pos"), _prefix_row("m1_mfi"), _prefix_row("xid_ho")])
    res = backfill(db_path)
    assert res["scanned"] == 3 and res["updated"] == 3 and res["bad_json"] == 0
    rows = _rows(db_path)
    assert rows["4787a0"]["sublabel"] == "M1" and rows["4787a0"]["mfi"] is None
    assert rows["48ea00"]["sublabel"] == "M1" and rows["48ea00"]["mfi"] == "B0"
    xid = rows["48c132"]
    assert (xid["lat"], xid["lon"], xid["alt"], xid["dsta"]) == (52.299999, 20.5, 31000, "LATI")
    assert res["filled"]["sublabel"] == 2 and res["filled"]["mfi"] == 1
    assert res["filled"]["dsta"] == 1 and res["filled"]["lat"] == 1


def test_second_run_is_noop(db_path):
    _seed(db_path, [_prefix_row("m1_route"), _prefix_row("xid_ho")])
    assert backfill(db_path)["updated"] == 2
    again = backfill(db_path)
    assert again["updated"] == 0 and again["scanned"] == 2


def test_invalid_raw_counted_not_fatal(db_path):
    bad = _prefix_row("m1_pos")
    bad["icao_hex"] = "000bad"
    bad["raw"] = bad["raw"][:60] + '"sublabel":"M1'    # truncated mid-JSON, still matches the LIKE filter
    _seed(db_path, [bad, _prefix_row("xid_ho")])
    res = backfill(db_path)
    assert res["bad_json"] == 1 and res["updated"] == 1
    assert _rows(db_path)["000bad"]["sublabel"] is None


def test_never_overwrites_existing_values(db_path):
    rec = _prefix_row("xid_ho")
    rec["dsta"] = "EPWA"        # already set (e.g. by a vdlm2dec-style writer)
    _seed(db_path, [rec])
    res = backfill(db_path)
    row = _rows(db_path)["48c132"]
    assert row["dsta"] == "EPWA"
    assert row["lat"] == 52.299999 and res["filled"].get("dsta", 0) == 0


def test_dry_run_writes_nothing(db_path):
    _seed(db_path, [_prefix_row("m1_pos"), _prefix_row("xid_ho")])
    res = backfill(db_path, dry_run=True)
    assert res["updated"] == 2 and res["filled"]["sublabel"] == 1
    rows = _rows(db_path)
    assert all(rows[h][c] is None for h in rows for c in _FILLED)


def test_dry_run_on_pre_fix_schema_does_not_migrate(tmp_path):
    path = str(tmp_path / "old.db")
    conn = vdl2_db.connect(path)
    vdl2_db.ensure_schema(conn)
    vdl2_db.insert_messages(conn, [_prefix_row("m1_pos")])
    conn.commit()
    conn.execute("ALTER TABLE vdl2_messages DROP COLUMN sublabel")
    conn.execute("ALTER TABLE vdl2_messages DROP COLUMN mfi")
    conn.commit()
    conn.close()
    res = backfill(path, dry_run=True)
    assert res["updated"] == 1
    conn = vdl2_db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
    conn.close()
    assert "sublabel" not in cols


def test_chunking_covers_all_rows(db_path):
    recs = []
    for i in range(7):
        r = _prefix_row("m1_pos", 1_700_000_000 + i)
        r["icao_hex"] = f"4787a{i}"
        recs.append(r)
    _seed(db_path, recs)
    res = backfill(db_path, chunk=3)
    assert res["scanned"] == 7 and res["updated"] == 7


def test_fts_search_still_works_after_backfill(db_path):
    conn = vdl2_db.connect(db_path)
    if not vdl2_db.has_fts(conn):
        conn.close()
        pytest.skip("FTS5 unavailable")
    conn.close()
    _seed(db_path, [_prefix_row("m1_route")])
    backfill(db_path)
    conn = vdl2_db.connect(db_path)
    try:
        hit = conn.execute(
            "SELECT m.sublabel FROM vdl2_fts f JOIN vdl2_messages m ON m.id = f.rowid "
            "WHERE vdl2_fts MATCH 'OLIL6G'"
        ).fetchall()
        assert len(hit) == 1 and hit[0]["sublabel"] == "M1"
    finally:
        conn.close()


def test_fills_cm_logon_label_body_and_dsta(db_path):
    _seed(db_path, [_cm_prefix_row()])
    res = backfill(db_path)
    assert res["scanned"] == 1 and res["updated"] == 1
    row = _rows(db_path)["48c132"]
    assert (row["label"], row["body"], row["dsta"]) == ("CM", "LOGON RYR8414 EPMO-LATI", "LATI")
    assert row["flight"] is None
    assert res["filled"]["label"] == 1 and res["filled"]["body"] == 1
    assert res["filled"]["dsta"] == 1


def test_cm_second_run_is_noop(db_path):
    _seed(db_path, [_cm_prefix_row(), _prefix_row("xid_ho")])
    assert backfill(db_path)["updated"] == 2
    again = backfill(db_path)
    assert again["updated"] == 0 and again["scanned"] == 2


def test_cm_never_overwrites_existing_label_body(db_path):
    rec = _cm_prefix_row()
    rec["body"] = "custom"
    _seed(db_path, [rec])
    backfill(db_path)
    row = _rows(db_path)["48c132"]
    assert row["body"] == "custom" and row["label"] == "CM"


def test_existing_rows_label_body_untouched(db_path):
    # ACARS rows already carry label/body; the widened column set must not change them.
    rec = _prefix_row("m1_pos")
    full = dumpvdl2_record("m1_pos", 1_700_000_000)
    rec["label"], rec["body"] = full["label"], full["body"]
    _seed(db_path, [rec])
    backfill(db_path)
    row = _rows(db_path)["4787a0"]
    assert row["label"] == full["label"] and row["body"] == full["body"]


def test_cm_dry_run_writes_nothing(db_path):
    _seed(db_path, [_cm_prefix_row()])
    res = backfill(db_path, dry_run=True)
    assert res["updated"] == 1
    row = _rows(db_path)["48c132"]
    assert row["label"] is None and row["body"] is None and row["dsta"] is None


def test_fts_finds_backfilled_cm_body(db_path):
    conn = vdl2_db.connect(db_path)
    if not vdl2_db.has_fts(conn):
        conn.close()
        pytest.skip("FTS5 unavailable")
    conn.close()
    _seed(db_path, [_cm_prefix_row()])
    backfill(db_path)
    conn = vdl2_db.connect(db_path)
    try:
        hit = conn.execute(
            "SELECT m.label FROM vdl2_fts f JOIN vdl2_messages m ON m.id = f.rowid "
            "WHERE vdl2_fts MATCH 'LATI'"
        ).fetchall()
        assert len(hit) == 1 and hit[0]["label"] == "CM"
    finally:
        conn.close()


def test_main_rejects_missing_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["x", "--db", str(tmp_path / "nope.db")])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1
    assert not (tmp_path / "nope.db").exists()


# --- multi-block ACARS: `more` fill + fragment supersede pass ----------------

def _block_row(ts: int, text: str, more: bool, *, icao: str = "48E95C",
               msgno: str = "D75") -> dict:
    """A block as pre-2.28 ingest stored it: real raw, ``more``/``superseded`` NULL."""
    frame = {"vdl2": {"t": {"sec": ts}, "freq": 136725000, "avlc": {
        "src": {"addr": icao}, "acars": {
            "more": more, "reg": ".SP-LYF", "label": "H1", "msg_num": msgno,
            "sublabel": "M1", "msg_text": text}}}}
    rec = normalize.normalize(frame, "dumpvdl2")
    rec["more"] = None
    rec["superseded"] = None
    return rec


def _blocks(path: str) -> list[dict]:
    conn = vdl2_db.connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, more, superseded FROM vdl2_messages ORDER BY id")]
    finally:
        conn.close()


def _seed_message(path: str) -> None:
    _seed(path, [
        _block_row(1_700_000_000, _FULL_TEXT[:35], True),
        _block_row(1_700_000_004, _FULL_TEXT[:55], True),
        _block_row(1_700_000_008, _FULL_TEXT, False),
        _block_row(1_700_000_009, "an orphan fragment with no final", True,
                   icao="48E95D", msgno="D80"),
    ])


def test_fills_more_and_supersedes_fragments(db_path):
    _seed_message(db_path)
    res = backfill(db_path)
    assert res["filled"]["more"] == 4
    assert res["superseded"] == 2
    got = _blocks(db_path)
    assert [r["more"] for r in got] == [1, 1, 0, 1]
    assert [r["superseded"] for r in got] == [1, 1, None, None]


def test_supersede_second_run_is_noop(db_path):
    _seed_message(db_path)
    backfill(db_path)
    res = backfill(db_path)
    assert res["updated"] == 0 and res["superseded"] == 0


def test_supersede_pass_chunks(db_path):
    _seed_message(db_path)
    res = backfill(db_path, chunk=1)
    assert res["superseded"] == 2


def test_supersede_pass_on_already_filled_more(db_path):
    # `more` populated by new ingest but the DB predates marking: the pass alone fixes it
    _seed_message(db_path)
    backfill(db_path)
    conn = vdl2_db.connect(db_path)
    conn.execute("UPDATE vdl2_messages SET superseded = NULL")
    conn.commit()
    conn.close()
    assert backfill(db_path)["superseded"] == 2


def test_more_dry_run_writes_nothing(db_path):
    _seed_message(db_path)
    res = backfill(db_path, dry_run=True)
    assert res["filled"]["more"] == 4
    assert res["superseded"] is None          # supersede pass is skipped on a dry run
    got = _blocks(db_path)
    assert all(r["more"] is None and r["superseded"] is None for r in got)
