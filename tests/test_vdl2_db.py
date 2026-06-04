"""Tests for the VDL2 SQLite store: schema, FTS5, retention."""
from __future__ import annotations

import time

import pytest

from readsbstats.vdl2 import db as vdl2_db
from tests._helpers import make_vdl2_db


def _msg(**kw):
    base = {"ts": int(time.time()), "icao_hex": "48e95d", "registration": "SP-LYF",
            "flight": "LO6550", "label": "H1", "body": "hello warsaw"}
    base.update(kw)
    return base


class TestSchema:
    def test_table_and_indexes_created(self):
        conn = make_vdl2_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "icao_hex" in cols and "body" in cols and "raw" in cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == vdl2_db.VDL2_SCHEMA_VERSION

    def test_fts_available_on_this_build(self):
        conn = make_vdl2_db()
        # Dev + Pi both ship FTS5; if this ever fails the LIKE fallback covers it.
        assert vdl2_db.fts5_available(conn) is True
        assert vdl2_db.has_fts(conn) is True

    def test_ensure_schema_idempotent(self):
        conn = make_vdl2_db()
        vdl2_db.ensure_schema(conn)  # second call must not raise
        assert vdl2_db.has_fts(conn) is True


class TestInsertAndSearch:
    def test_insert_and_count(self):
        conn = make_vdl2_db()
        n = vdl2_db.insert_messages(conn, [_msg(), _msg(icao_hex="48af11")])
        conn.commit()
        assert n == 2
        assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 2

    def test_fts_match_finds_body(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(body="depart EPWA gate 12"),
                                       _msg(body="unrelated content")])
        conn.commit()
        rows = conn.execute(
            "SELECT m.id FROM vdl2_messages m "
            "WHERE m.id IN (SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?)",
            ('"EPWA"',),
        ).fetchall()
        assert len(rows) == 1


class TestRetention:
    def test_prune_deletes_old_and_keeps_fts_in_sync(self):
        conn = make_vdl2_db()
        now = 1_000_000_000
        old = now - 100 * 86400      # older than 90 days
        new = now - 1 * 86400
        vdl2_db.insert_messages(conn, [
            _msg(ts=old, body="ancient krakow message"),
            _msg(ts=new, body="recent krakow message"),
        ])
        conn.commit()

        removed = vdl2_db.prune(conn, 90, now=now)
        assert removed == 1
        assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 1

        # FTS must not return the pruned row (external-content delete trigger).
        hits = conn.execute(
            "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"krakow"',)
        ).fetchall()
        assert len(hits) == 1

    def test_null_body_insert_search_and_prune_stay_consistent(self):
        # Messages with no text store body=NULL. The external-content FTS
        # delete/update triggers must stay in sync (pass old.body symmetrically)
        # so a later prune of a NULL-body row can't corrupt the FTS index.
        conn = make_vdl2_db()
        now = 1_000_000_000
        vdl2_db.insert_messages(conn, [
            _msg(ts=now - 100 * 86400, body=None),                 # old, NULL body
            _msg(ts=now - 1 * 86400, body="recent gdansk message"),  # kept
        ])
        conn.commit()
        removed = vdl2_db.prune(conn, 90, now=now)
        assert removed == 1
        # FTS query after pruning a NULL-body row must not raise / must be sane.
        hits = conn.execute(
            "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"gdansk"',)
        ).fetchall()
        assert len(hits) == 1
        # Integrity check confirms the external-content index is not malformed.
        assert conn.execute("INSERT INTO vdl2_fts(vdl2_fts) VALUES('integrity-check')") is not None

    def test_prune_zero_days_keeps_everything(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(ts=1), _msg(ts=2)])
        conn.commit()
        assert vdl2_db.prune(conn, 0) == 0
        assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 2


class TestNoFtsFallback:
    def test_has_fts_false_when_only_base_table(self):
        # Simulate a build/DB without FTS by creating only the base table.
        conn = vdl2_db.connect(":memory:")
        conn.executescript(vdl2_db._DDL_MESSAGES)
        assert vdl2_db.has_fts(conn) is False
        # Inserts still work (no triggers); LIKE search is the API's fallback.
        vdl2_db.insert_messages(conn, [_msg(body="warsaw approach")])
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM vdl2_messages WHERE body LIKE ?", ("%warsaw%",)
        ).fetchall()
        assert len(rows) == 1
