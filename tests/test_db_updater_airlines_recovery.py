"""Regression for W (Audit 2026-06-01): symmetric crash-recovery for the
airlines staging swap.

`update_aircraft_db` performs an atomic-ish rename swap and has matching
`recover_aircraft_db_swap()` called on every startup. `update_airlines_db`
does the identical swap but had no recovery — an interrupted run leaves
`airlines_old`/`airlines_new` lingering indefinitely.

Adds the sibling `recover_airlines_db_swap()` and asserts the three
orphan states the audit cares about:
  * airlines_new only            → build phase crashed
  * airlines_old only (no airlines) → mid-swap, restore from _old
  * airlines + airlines_old      → swap done, final drop missed
"""
from __future__ import annotations

import sqlite3

import pytest

from readsbstats import database


def _make_conn():
    """In-memory DB with only the airlines DDL fragment needed for these tests."""
    conn = database.connect(":memory:")
    # We don't need the full project DDL — just the airlines table.
    conn.execute(
        "CREATE TABLE airlines (icao_code TEXT PRIMARY KEY, name TEXT, "
        "iata_code TEXT, country TEXT, active INTEGER)"
    )
    conn.execute(
        "INSERT INTO airlines VALUES ('AAA','Alpha Air','AA','PL',1)"
    )
    conn.commit()
    return conn


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


class TestRecoverAirlinesDbSwap:
    def test_drops_orphan_airlines_new(self):
        """Build phase crashed: airlines_new is half-populated junk."""
        conn = _make_conn()
        conn.execute(
            "CREATE TABLE airlines_new (icao_code TEXT PRIMARY KEY, name TEXT, "
            "iata_code TEXT, country TEXT, active INTEGER)"
        )
        conn.execute("INSERT INTO airlines_new VALUES ('BBB','Bogus',NULL,NULL,1)")
        conn.commit()

        database.recover_airlines_db_swap(conn)

        assert "airlines_new" not in _tables(conn)
        # Canonical data preserved.
        assert conn.execute(
            "SELECT name FROM airlines WHERE icao_code='AAA'"
        ).fetchone()[0] == "Alpha Air"

    def test_restores_airlines_old_when_canonical_missing(self):
        """Mid-swap: first RENAME succeeded → airlines_old exists, airlines
        does NOT. Recovery must rename _old back to canonical."""
        conn = _make_conn()
        conn.execute("ALTER TABLE airlines RENAME TO airlines_old")
        conn.commit()
        assert "airlines" not in _tables(conn)

        database.recover_airlines_db_swap(conn)

        tables = _tables(conn)
        assert "airlines" in tables, "expected canonical restored from _old"
        assert "airlines_old" not in tables
        assert conn.execute(
            "SELECT name FROM airlines WHERE icao_code='AAA'"
        ).fetchone()[0] == "Alpha Air"

    def test_drops_orphan_airlines_old_when_canonical_present(self):
        """Final-drop missed: both RENAMEs succeeded, leftover _old lingers."""
        conn = _make_conn()
        # Simulate the post-swap state: canonical present + stale _old left over.
        conn.execute(
            "CREATE TABLE airlines_old (icao_code TEXT PRIMARY KEY, name TEXT, "
            "iata_code TEXT, country TEXT, active INTEGER)"
        )
        conn.commit()

        database.recover_airlines_db_swap(conn)

        assert "airlines_old" not in _tables(conn)
        # Canonical data untouched.
        assert conn.execute(
            "SELECT COUNT(*) FROM airlines"
        ).fetchone()[0] == 1

    def test_no_op_when_nothing_to_recover(self):
        conn = _make_conn()
        database.recover_airlines_db_swap(conn)  # must not raise
        assert _tables(conn) == {"airlines"}


class TestUpdateAirlinesDbDefensiveDrop:
    def test_orphan_airlines_old_does_not_block_swap(self, monkeypatch, tmp_path):
        """Lingering airlines_old must not abort the next update run; the
        defensive DROP at the top of update_airlines_db handles it."""
        from readsbstats import db_updater

        # Build a tiny fake CSV that's also above AIRLINES_DB_MIN_RATIO of the
        # seeded count (1 row), so the swap is allowed. openflights airlines.dat
        # is comma-separated, ID, name, alias, IATA, ICAO, callsign, country, active.
        # _fetch returns the body bytes; bypass it directly.
        rows = (
            b'1,"Alpha","",\\N,"AAA","ALPHA","PL","Y"\n'
            b'2,"Beta","","BB","BBB","BETA","GB","Y"\n'
            b'3,"Gamma","","CC","CCC","GAMMA","US","Y"\n'
        )
        monkeypatch.setattr(db_updater, "_fetch", lambda url: rows)

        # Prepare an on-disk DB with airlines populated and a stale airlines_old
        # left over from a previously-aborted run.
        db_path = tmp_path / "test.db"
        bootstrap = database.connect(str(db_path))
        bootstrap.execute(
            "CREATE TABLE airlines (icao_code TEXT PRIMARY KEY, name TEXT, "
            "iata_code TEXT, country TEXT, active INTEGER)"
        )
        bootstrap.execute("INSERT INTO airlines VALUES ('XXX','seed',NULL,NULL,1)")
        bootstrap.execute(
            "CREATE TABLE airlines_old (icao_code TEXT PRIMARY KEY, name TEXT, "
            "iata_code TEXT, country TEXT, active INTEGER)"
        )
        bootstrap.commit()
        bootstrap.close()

        conn = database.connect(str(db_path))
        try:
            db_updater.update_airlines_db(conn)
            assert "airlines_old" not in _tables(conn)
            assert "airlines_new" not in _tables(conn)
            # Swap completed — new rows live in `airlines` (not the seed row).
            names = {r[0] for r in conn.execute(
                "SELECT icao_code FROM airlines"
            ).fetchall()}
            assert names == {"AAA", "BBB", "CCC"}
        finally:
            conn.close()

    def test_airlines_absent_midswap_does_not_crash(self, monkeypatch, tmp_path):
        """Interrupted mid-swap left `airlines` renamed to `airlines_old` with no
        canonical `airlines` table. update_airlines_db must self-recover (restore
        from _old, mirroring update_aircraft_db) instead of crashing at
        `SELECT COUNT(*) FROM airlines`. The normal main() path is masked by
        init_db()'s recovery; this guards a direct/standalone caller and the
        latent `DROP airlines_old` data-loss if line 284 didn't crash first."""
        from readsbstats import db_updater

        rows = (
            b'1,"Alpha","",\\N,"AAA","ALPHA","PL","Y"\n'
            b'2,"Beta","","BB","BBB","BETA","GB","Y"\n'
            b'3,"Gamma","","CC","CCC","GAMMA","US","Y"\n'
        )
        monkeypatch.setattr(db_updater, "_fetch", lambda url: rows)

        db_path = tmp_path / "test.db"
        bootstrap = database.connect(str(db_path))
        bootstrap.execute(
            "CREATE TABLE airlines (icao_code TEXT PRIMARY KEY, name TEXT, "
            "iata_code TEXT, country TEXT, active INTEGER)"
        )
        bootstrap.execute("INSERT INTO airlines VALUES ('XXX','seed',NULL,NULL,1)")
        # Simulate the mid-swap crash: the first RENAME ran, the second didn't —
        # canonical `airlines` is gone, the data survives only in airlines_old.
        bootstrap.execute("ALTER TABLE airlines RENAME TO airlines_old")
        bootstrap.commit()
        assert "airlines" not in _tables(bootstrap)
        bootstrap.close()

        conn = database.connect(str(db_path))
        try:
            db_updater.update_airlines_db(conn)   # must NOT raise "no such table: airlines"
            assert "airlines_old" not in _tables(conn)
            assert "airlines_new" not in _tables(conn)
            names = {r[0] for r in conn.execute(
                "SELECT icao_code FROM airlines"
            ).fetchall()}
            assert names == {"AAA", "BBB", "CCC"}
        finally:
            conn.close()
