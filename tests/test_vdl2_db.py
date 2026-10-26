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


class TestMigrate:
    def test_migrate_adds_indexes_to_existing_db(self):
        # Simulate a DB created before the id-aligned indexes existed: base table
        # only, user_version already at current. migrate() must still add them.
        conn = vdl2_db.connect(":memory:")
        conn.executescript(vdl2_db._DDL_MESSAGES)
        conn.execute(f"PRAGMA user_version = {vdl2_db.VDL2_SCHEMA_VERSION}")
        conn.commit()
        vdl2_db.migrate(conn)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(vdl2_messages)")}
        assert "idx_vdl2_label_id" in idx
        assert "idx_vdl2_icao_id" in idx
        assert "idx_vdl2_reg_id" in idx

    def test_fts_built_and_populated_when_available_later(self):
        # DB first created without FTS (only base table), rows inserted, then
        # migrate() runs on an FTS-capable build → FTS created AND rebuilt from
        # the existing rows.
        conn = vdl2_db.connect(":memory:")
        conn.executescript(vdl2_db._DDL_MESSAGES)
        conn.execute(f"PRAGMA user_version = {vdl2_db.VDL2_SCHEMA_VERSION}")
        conn.commit()
        vdl2_db.insert_messages(conn, [_msg(body="retroactive lublin message")])
        conn.commit()
        assert vdl2_db.has_fts(conn) is False
        vdl2_db.migrate(conn)
        assert vdl2_db.has_fts(conn) is True
        hits = conn.execute(
            "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"lublin"',)
        ).fetchall()
        assert len(hits) == 1   # 'rebuild' populated the index from existing rows


class TestSignalColumns:
    """sig_level / noise_level columns (dumpvdl2 per-frame dBFS) — added to an
    existing DB via the idempotent migrate() ALTER, like the indexes."""

    # vdl2_messages exactly as it existed BEFORE sig_level/noise_level — simulates
    # a real prod DB so migrate() must ALTER the two columns in (the current
    # _DDL_MESSAGES already has them, so it can't exercise the upgrade path).
    _LEGACY_DDL = """
    CREATE TABLE vdl2_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, icao_hex TEXT,
        registration TEXT, flight TEXT, label TEXT, mode TEXT, block_id TEXT,
        ack TEXT, msgno TEXT, freq REAL, station_id TEXT, toaddr TEXT, dsta TEXT,
        lat REAL, lon REAL, alt INTEGER, epu REAL, app_name TEXT, app_ver TEXT,
        body TEXT, raw TEXT, decoder TEXT
    );
    """

    def _legacy_db(self):
        conn = vdl2_db.connect(":memory:")
        conn.executescript(self._LEGACY_DDL)
        conn.execute(f"PRAGMA user_version = {vdl2_db.VDL2_SCHEMA_VERSION}")
        conn.commit()
        return conn

    def test_migrate_adds_signal_columns_to_existing_db(self):
        conn = self._legacy_db()
        before = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sig_level" not in before and "noise_level" not in before
        vdl2_db.migrate(conn)
        after = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sig_level" in after and "noise_level" in after

    def test_migrate_signal_columns_idempotent(self):
        conn = self._legacy_db()
        vdl2_db.migrate(conn)
        vdl2_db.migrate(conn)  # second run must not raise (already-present is a no-op)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sig_level" in cols and "noise_level" in cols

    def test_fresh_db_has_signal_columns(self):
        conn = make_vdl2_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sig_level" in cols and "noise_level" in cols

    def test_insert_and_readback_signal(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(sig_level=-45.7, noise_level=-52.8)])
        conn.commit()
        row = conn.execute(
            "SELECT sig_level, noise_level FROM vdl2_messages"
        ).fetchone()
        assert row["sig_level"] == -45.7 and row["noise_level"] == -52.8

    def test_add_missing_columns_swallows_duplicate_on_race(self):
        # The race: another connection added the columns AFTER our table_info
        # snapshot, so our ALTER hits 'duplicate column name' (SQLite has no ADD
        # COLUMN IF NOT EXISTS). The try/except must swallow it. Simulate the
        # stale snapshot by reporting the columns absent while the real table
        # already has them — the ALTER then raises a genuine SQLite error.
        real = make_vdl2_db()  # already has sig_level + noise_level

        class StalePrecheckConn:
            def execute(self, sql, *a):
                if "table_info" in sql:
                    return iter([])           # stale: columns look absent
                return real.execute(sql, *a)  # real ALTER → real duplicate error

        vdl2_db._add_missing_columns(StalePrecheckConn())   # must not raise
        cols = {r[1] for r in real.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sig_level" in cols and "noise_level" in cols


class TestBatchedPrune:
    def test_prune_batches_delete_all_old(self):
        conn = make_vdl2_db()
        now = 1_000_000_000
        old = [_msg(ts=now - 100 * 86400, body=f"old {i}") for i in range(5)]
        vdl2_db.insert_messages(conn, old + [_msg(ts=now - 1 * 86400, body="recent")])
        conn.commit()
        removed = vdl2_db.prune(conn, 90, now=now, batch=2)   # forces multiple batches
        assert removed == 5
        assert conn.execute("SELECT COUNT(*) FROM vdl2_messages").fetchone()[0] == 1
        # FTS stays in sync across batches.
        assert conn.execute(
            "SELECT COUNT(*) FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"old"',)
        ).fetchone()[0] == 0


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

    def test_fts5_available_false_when_module_missing(self):
        # An SQLite build without the FTS5 module raises OperationalError on
        # the probe — must mean False, not an exception out of ensure_schema.
        import sqlite3

        class NoFts5Conn:
            def execute(self, sql, *a):
                raise sqlite3.OperationalError("no such module: fts5")

        assert vdl2_db.fts5_available(NoFts5Conn()) is False

    def test_close_all_web_conns_swallows_close_errors(self):
        # Best-effort teardown: an already-broken reader conn must not stop
        # the rest from being closed, and the registry must end empty.
        import sqlite3

        class BoomConn:
            def close(self):
                raise sqlite3.ProgrammingError("already closed")

        vdl2_db._web_conns.append(BoomConn())
        vdl2_db.close_all_web_conns()      # must not raise
        assert vdl2_db._web_conns == []


# FTS schema exactly as it was BEFORE the update trigger was narrowed to body.
_LEGACY_FTS_DDL = """
CREATE VIRTUAL TABLE vdl2_fts USING fts5(body, content='vdl2_messages', content_rowid='id');
CREATE TRIGGER vdl2_ai AFTER INSERT ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER vdl2_ad AFTER DELETE ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(vdl2_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER vdl2_au AFTER UPDATE ON vdl2_messages BEGIN
    INSERT INTO vdl2_fts(vdl2_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO vdl2_fts(rowid, body) VALUES (new.id, new.body);
END;
"""


class TestSublabelMfiColumns:
    """sublabel / mfi columns (libacars-stripped H1 prefix) + the FTS update
    trigger narrowed to ``UPDATE OF body``."""

    _LEGACY_DDL = TestSignalColumns._LEGACY_DDL

    def _legacy_db(self, *, with_fts: bool = False):
        conn = vdl2_db.connect(":memory:")
        conn.executescript(self._LEGACY_DDL)
        conn.execute(f"PRAGMA user_version = {vdl2_db.VDL2_SCHEMA_VERSION}")
        if with_fts:
            # Pre-change FTS: the update trigger fired on ANY column update.
            conn.executescript(_LEGACY_FTS_DDL)
        conn.commit()
        return conn

    @staticmethod
    def _trigger_sql(conn) -> str:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='vdl2_au'"
        ).fetchone()[0]

    def test_migrate_adds_columns_to_existing_db(self):
        conn = self._legacy_db()
        before = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sublabel" not in before and "mfi" not in before
        vdl2_db.migrate(conn)
        after = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sublabel" in after and "mfi" in after

    def test_migrate_idempotent(self):
        conn = self._legacy_db()
        vdl2_db.migrate(conn)
        vdl2_db.migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sublabel" in cols and "mfi" in cols

    def test_fresh_db_has_columns_and_in_insert_columns(self):
        conn = make_vdl2_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "sublabel" in cols and "mfi" in cols
        assert "sublabel" in vdl2_db.COLUMNS and "mfi" in vdl2_db.COLUMNS

    def test_insert_and_readback(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(sublabel="M1", mfi="B0")])
        conn.commit()
        row = conn.execute("SELECT sublabel, mfi FROM vdl2_messages").fetchone()
        assert row["sublabel"] == "M1" and row["mfi"] == "B0"

    def test_fresh_db_trigger_is_update_of_body(self):
        conn = make_vdl2_db()
        assert "update of body" in self._trigger_sql(conn).lower()

    def test_writer_migrate_replaces_legacy_trigger(self):
        conn = self._legacy_db(with_fts=True)
        assert "update of body" not in self._trigger_sql(conn).lower()
        vdl2_db.migrate(conn, build_fts=True)
        assert "update of body" in self._trigger_sql(conn).lower()
        vdl2_db.migrate(conn, build_fts=True)       # idempotent
        assert "update of body" in self._trigger_sql(conn).lower()

    def test_web_migrate_does_not_touch_trigger(self):
        conn = self._legacy_db(with_fts=True)
        vdl2_db.migrate(conn, build_fts=False)
        assert "update of body" not in self._trigger_sql(conn).lower()

    def test_body_update_still_reindexes_fts(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(body="oldtext warsaw")])
        conn.commit()
        conn.execute("UPDATE vdl2_messages SET body = 'newtext krakow'")
        conn.commit()

        def hits(term):
            return conn.execute(
                "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", (f'"{term}"',)
            ).fetchall()

        assert len(hits("newtext")) == 1
        assert hits("oldtext") == []

    def test_non_body_update_leaves_fts_intact(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(body="oldtext warsaw")])
        conn.commit()
        conn.execute("UPDATE vdl2_messages SET sublabel = 'M1', mfi = 'B0'")
        conn.commit()
        hits = conn.execute(
            "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"oldtext"',)
        ).fetchall()
        assert len(hits) == 1
        conn.execute("INSERT INTO vdl2_fts(vdl2_fts) VALUES('integrity-check')")


class TestMoreSupersededColumns:
    """``more`` (acars.more) / ``superseded`` (fragment hidden by a final block)."""

    _LEGACY_DDL = TestSignalColumns._LEGACY_DDL

    def test_migrate_adds_columns_to_existing_db(self):
        conn = vdl2_db.connect(":memory:")
        conn.executescript(self._LEGACY_DDL)
        conn.execute(f"PRAGMA user_version = {vdl2_db.VDL2_SCHEMA_VERSION}")
        conn.commit()
        before = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "more" not in before and "superseded" not in before
        vdl2_db.migrate(conn)
        vdl2_db.migrate(conn)       # idempotent
        after = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "more" in after and "superseded" in after

    def test_fresh_db_has_columns_and_more_in_insert_columns(self):
        conn = make_vdl2_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        assert "more" in cols and "superseded" in cols
        assert "more" in vdl2_db.COLUMNS
        assert "superseded" in vdl2_db.COLUMNS

    def test_insert_and_readback(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg(more=1), _msg(more=0), _msg()])
        conn.commit()
        got = [r["more"] for r in conn.execute("SELECT more FROM vdl2_messages ORDER BY id")]
        assert got == [1, 0, None]


class TestMarkSuperseded:
    _FULL = "POSN52256E019186,ADOXO,200245,380,PENOR,202912,KSD,M52,226050,11122DB"
    _PART = _FULL[:40]
    T = 1_700_000_000

    def _db(self, rows):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, rows)
        conn.commit()
        return conn

    def _frag(self, **kw):
        base = {"ts": self.T, "icao_hex": "48e95c", "label": "H1", "msgno": "D75",
                "more": 1, "body": self._PART}
        base.update(kw)
        return base

    def _final(self, **kw):
        base = {"ts": self.T + 5, "icao_hex": "48e95c", "label": "H1", "msgno": "D75",
                "more": 0, "body": self._FULL}
        base.update(kw)
        return base

    @staticmethod
    def _flags(conn):
        return [r["superseded"] for r in
                conn.execute("SELECT superseded FROM vdl2_messages ORDER BY id")]

    def _run(self, frag_kw=None, final_kw=None):
        frag, final = self._frag(**(frag_kw or {})), self._final(**(final_kw or {}))
        conn = self._db([frag, final])
        n = vdl2_db.mark_superseded(conn, [final])
        conn.commit()
        return conn, n

    def test_contained_fragment_marked(self):
        conn, n = self._run()
        assert n == 1
        assert self._flags(conn) == [1, None]    # the final itself is never marked

    def test_fragment_text_not_in_final_not_marked(self):
        conn, n = self._run(frag_kw={"body": "something else entirely, different text"})
        assert n == 0 and self._flags(conn) == [None, None]

    @pytest.mark.parametrize("kw", [
        {"msgno": "D76"}, {"icao_hex": "48e95d"}, {"label": "Q0"},
    ])
    def test_other_message_identity_not_marked(self, kw):
        conn, n = self._run(frag_kw=kw)
        assert n == 0 and self._flags(conn) == [None, None]

    def test_outside_window_not_marked(self):
        w = vdl2_db.SUPERSEDE_WINDOW_SEC
        conn, n = self._run(frag_kw={"ts": self.T + 5 - w - 1})
        assert n == 0
        conn, n = self._run(frag_kw={"ts": self.T + 5 - w})    # boundary is inclusive
        assert n == 1

    def test_fragment_after_final_not_marked(self):
        conn, n = self._run(frag_kw={"ts": self.T + 6})
        assert n == 0 and self._flags(conn) == [None, None]

    def test_short_body_not_marked(self):
        conn, n = self._run(frag_kw={"body": self._FULL[:vdl2_db.SUPERSEDE_MIN_BODY - 1]})
        assert n == 0 and self._flags(conn) == [None, None]

    def test_final_that_is_not_final_is_ignored(self):
        # a more=1 / more=NULL "final" must not hide anything
        for more in (1, None):
            frag, final = self._frag(), self._final(more=more)
            conn = self._db([frag, final])
            assert vdl2_db.mark_superseded(conn, [final]) == 0

    def test_final_without_msgno_or_body_is_ignored(self):
        for kw in ({"msgno": None}, {"body": None}, {"label": None}, {"icao_hex": None}):
            frag, final = self._frag(), self._final(**kw)
            conn = self._db([frag, final])
            assert vdl2_db.mark_superseded(conn, [final]) == 0

    def test_second_call_returns_zero(self):
        conn, n = self._run()
        assert n == 1
        final = self._final()
        assert vdl2_db.mark_superseded(conn, [final]) == 0

    def test_empty_records(self):
        assert vdl2_db.mark_superseded(make_vdl2_db(), []) == 0

    def test_fts_still_finds_final_text_and_stays_consistent(self):
        conn, _ = self._run()
        hits = conn.execute(
            "SELECT rowid FROM vdl2_fts WHERE vdl2_fts MATCH ?", ('"ADOXO"',)
        ).fetchall()
        assert len(hits) == 2                       # fragment stays indexed (hidden, not deleted)
        conn.execute("INSERT INTO vdl2_fts(vdl2_fts) VALUES('integrity-check')")


class TestHeavyIndexes:
    """idx_vdl2_label_icao / idx_vdl2_flight serve the Stats card's top-labels /
    top-airlines aggregates but cost a full-table scan to build, so they are built
    by the collector in the background (build_heavy_indexes), never by migrate()."""

    NAMES = {"idx_vdl2_label_icao", "idx_vdl2_flight"}

    @staticmethod
    def _index_names(conn):
        return {r[1] for r in conn.execute("PRAGMA index_list(vdl2_messages)")}

    def test_migrate_does_not_create_them(self):
        conn = make_vdl2_db()
        vdl2_db.migrate(conn)
        vdl2_db.ensure_schema(conn, build_fts=False)
        assert not (self.NAMES & self._index_names(conn))

    def test_build_heavy_indexes_creates_them_and_is_idempotent(self):
        conn = make_vdl2_db()
        vdl2_db.insert_messages(conn, [_msg()])
        conn.commit()
        vdl2_db.build_heavy_indexes(conn)
        assert self.NAMES <= self._index_names(conn)
        vdl2_db.build_heavy_indexes(conn)   # second call must not raise
        assert self.NAMES <= self._index_names(conn)

    def test_heavy_indexes_survive_migrate(self):
        conn = make_vdl2_db()
        vdl2_db.build_heavy_indexes(conn)
        vdl2_db.migrate(conn)
        assert self.NAMES <= self._index_names(conn)
