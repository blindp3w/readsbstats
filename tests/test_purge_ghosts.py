"""Tests for purge_ghosts.py."""

import math
import sqlite3

import pytest

from readsbstats import database
from purge_ghosts import apply_purge, find_ghost_ids, haversine_nm, max_distance_after_purge

RLAT = 52.24199
RLON = 21.02872
MAX_SPEED = 2000


def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def insert_flight(conn, icao="aabbcc") -> int:
    cur = conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES (?,1000,9000)",
        (icao,),
    )
    conn.commit()
    return cur.lastrowid


def insert_pos(conn, flight_id, ts, lat, lon) -> int:
    cur = conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?,?,?,?)",
        (flight_id, ts, lat, lon),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# find_ghost_ids
# ---------------------------------------------------------------------------

class TestFindGhostIds:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_no_positions_returns_empty(self):
        assert find_ghost_ids(self.conn, MAX_SPEED) == {}

    def test_single_position_never_a_ghost(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        assert find_ghost_ids(self.conn, MAX_SPEED) == {}

    def test_detects_teleporting_adsb_ghost(self):
        """Position implying ~323k kts must be flagged."""
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)   # real, ~23 nm from receiver
        gid = insert_pos(self.conn, fid, 1005, 59.7, 21.5)  # ghost, ~449 nm jump in 5 s
        insert_pos(self.conn, fid, 1070, 52.5, 20.6)   # real again, 70 s after p1 → ~412 kts

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert fid in ghosts
        assert ghosts[fid] == [gid]

    def test_real_positions_not_flagged(self):
        """10 nm in 60 s ≈ 600 kts — well within threshold."""
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0)
        insert_pos(self.conn, fid, 1060, 52.17, 21.0)  # ~10 nm in 60 s
        assert find_ghost_ids(self.conn, MAX_SPEED) == {}

    def test_ghost_does_not_cascade_to_next_real_position(self):
        """After a ghost is rejected, the next real position is checked against
        the last good position — not against the ghost."""
        fid = insert_flight(self.conn)
        p1 = insert_pos(self.conn, fid, 1000, 52.6, 20.75)   # real
        gid = insert_pos(self.conn, fid, 1005, 59.7, 21.5)   # ghost
        p3 = insert_pos(self.conn, fid, 1070, 52.5, 20.6)    # real, 70 s after p1 → ~412 kts

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert ghosts.get(fid, []) == [gid]   # only the ghost flagged

    def test_multiple_consecutive_ghosts(self):
        """Two consecutive ghost positions are both flagged."""
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        g1 = insert_pos(self.conn, fid, 1005, 59.7, 21.5)
        g2 = insert_pos(self.conn, fid, 1010, 59.68, 21.53)  # still at 59°N, still a ghost from p1
        insert_pos(self.conn, fid, 1015, 52.5, 20.6)

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert set(ghosts.get(fid, [])) == {g1, g2}

    def test_first_position_is_ghost_triggers_backward_pass(self):
        """When the first position is a ghost that anchors all real positions incorrectly
        (forward pass leaves only 1 survivor), the backward pass fallback must flag it."""
        fid = insert_flight(self.conn)
        g1 = insert_pos(self.conn, fid, 1000, 59.7, 21.5)   # ghost at 59°N (~449 nm from receiver)
        p1 = insert_pos(self.conn, fid, 1070, 52.6, 20.75)  # real near Warsaw
        p2 = insert_pos(self.conn, fid, 1130, 52.5, 20.6)   # real near Warsaw

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert ghosts.get(fid, []) == [g1]  # ghost is pos1, not the Warsaw positions

    def test_two_flights_independent(self):
        """Ghost in one flight does not affect the other."""
        fid1 = insert_flight(self.conn, icao="aabbcc")
        fid2 = insert_flight(self.conn, icao="ddeeff")

        insert_pos(self.conn, fid1, 1000, 52.6, 20.75)
        gid = insert_pos(self.conn, fid1, 1005, 59.7, 21.5)

        insert_pos(self.conn, fid2, 2000, 52.0, 21.0)
        insert_pos(self.conn, fid2, 2060, 52.17, 21.0)

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert fid1 in ghosts
        assert ghosts[fid1] == [gid]
        assert fid2 not in ghosts


# ---------------------------------------------------------------------------
# max_distance_after_purge
# ---------------------------------------------------------------------------

class TestMaxDistanceAfterPurge:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_returns_none_when_all_positions_are_ghosts(self):
        fid = insert_flight(self.conn)
        gid = insert_pos(self.conn, fid, 1000, 59.7, 21.5)
        result = max_distance_after_purge(self.conn, fid, [gid], RLAT, RLON)
        assert result is None

    def test_returns_max_of_surviving_positions(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)   # ~23 nm
        gid = insert_pos(self.conn, fid, 1005, 59.7, 21.5)  # ghost, ~449 nm
        insert_pos(self.conn, fid, 1010, 52.5, 20.6)    # ~21 nm

        result = max_distance_after_purge(self.conn, fid, [gid], RLAT, RLON)
        assert result is not None
        assert result < 100  # ghost excluded; real positions are ~20-25 nm

    def test_empty_ghost_ids_does_not_raise(self):
        """Regression for audit-12 #143 / improvements.md #118 — empty
        ghost_ids must not produce `id NOT IN ()` SQL syntax error.
        apply_purge calls this with [] after DELETE; the dry-run report
        calls with the real list."""
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)   # ~23 nm
        insert_pos(self.conn, fid, 1010, 52.5, 20.6)    # ~21 nm

        # Must not raise sqlite3.OperationalError
        result = max_distance_after_purge(self.conn, fid, [], RLAT, RLON)
        assert result is not None
        assert result < 100

    def test_empty_ghost_ids_query_omits_not_in_clause(self):
        """Regression guard for improvements.md #118 — confirm the helper
        takes the no-IN branch (not just that it doesn't crash) so a
        future port to a stricter SQL engine doesn't silently regress."""
        captured: list[str] = []

        class _SpyConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kw):
                captured.append(sql)
                return self._inner.execute(sql, *args, **kw)

        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        max_distance_after_purge(_SpyConn(self.conn), fid, [], RLAT, RLON)
        assert any("NOT IN" not in s and "FROM positions" in s for s in captured)
        assert not any("NOT IN ()" in s for s in captured)

    def test_empty_ghost_ids_with_no_positions_returns_none(self):
        fid = insert_flight(self.conn)
        result = max_distance_after_purge(self.conn, fid, [], RLAT, RLON)
        assert result is None


# ---------------------------------------------------------------------------
# apply_purge
# ---------------------------------------------------------------------------

class TestApplyPurge:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_deletes_ghost_positions(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        gid = insert_pos(self.conn, fid, 1005, 59.7, 21.5)
        insert_pos(self.conn, fid, 1010, 52.5, 20.6)

        apply_purge(self.conn, {fid: [gid]}, RLAT, RLON)

        remaining = self.conn.execute(
            "SELECT id FROM positions WHERE flight_id = ?", (fid,)
        ).fetchall()
        ids = [r[0] for r in remaining]
        assert gid not in ids
        assert len(ids) == 2

    def test_updates_max_distance_nm(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        gid = insert_pos(self.conn, fid, 1005, 59.7, 21.5)

        # Manually set the inflated value
        self.conn.execute("UPDATE flights SET max_distance_nm = 449.4 WHERE id = ?", (fid,))
        self.conn.commit()

        apply_purge(self.conn, {fid: [gid]}, RLAT, RLON)

        new_max = self.conn.execute(
            "SELECT max_distance_nm FROM flights WHERE id = ?", (fid,)
        ).fetchone()["max_distance_nm"]
        assert new_max < 100  # real position is ~23 nm, not 449 nm

    def test_empty_ghosts_dict_is_noop(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0)

        before = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        apply_purge(self.conn, {}, RLAT, RLON)
        after = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert before == after

    def test_apply_purge_batches_commits(self):
        """Regression for audit-12 #P3.2 — apply_purge must commit
        periodically rather than holding the write lock for the entire
        flight loop. On a DB with thousands of flagged flights the
        single-transaction pattern starved the collector for minutes."""
        from purge_ghosts import _BATCH_SIZE

        # sqlite3.Connection.commit is a C-level read-only slot, so we wrap
        # the connection in a thin Proxy that intercepts commit() before
        # forwarding to the real conn.
        class _CountingConn:
            def __init__(self, c):
                self._c = c
                self.commits = 0
            def __getattr__(self, name):
                return getattr(self._c, name)
            def commit(self):
                self.commits += 1
                self._c.commit()

        # Build 2 × _BATCH_SIZE + 5 flights, each with one ghost
        ghosts: dict[int, list[int]] = {}
        n_flights = _BATCH_SIZE * 2 + 5
        for i in range(n_flights):
            fid = insert_flight(self.conn, icao=f"a{i:05x}")
            insert_pos(self.conn, fid, 1000 + i, 52.6, 20.75)
            gid = insert_pos(self.conn, fid, 1005 + i, 59.7, 21.5)
            ghosts[fid] = [gid]

        counter = _CountingConn(self.conn)
        apply_purge(counter, ghosts, RLAT, RLON)

        # At least 3 commits (2 batch boundaries + final): proves batching
        assert counter.commits >= 3, (
            f"expected ≥3 commits for {n_flights} flights at batch={_BATCH_SIZE},"
            f" got {counter.commits}"
        )
        # All ghosts still deleted (correctness preserved)
        for fid, ghost_ids in ghosts.items():
            placeholders = ",".join("?" * len(ghost_ids))
            remaining = self.conn.execute(
                f"SELECT COUNT(*) FROM positions WHERE id IN ({placeholders})",
                ghost_ids,
            ).fetchone()[0]
            assert remaining == 0


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_dry_run_no_ghosts(self, tmp_path, capsys):
        from purge_ghosts import main
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        import sys
        sys.argv = ["purge_ghosts", "--db", db_path]
        main()
        out = capsys.readouterr().out
        assert "No ghost positions found" in out

    def test_dry_run_with_ghosts(self, tmp_path, capsys):
        from purge_ghosts import main
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm, total_positions) "
            "VALUES ('aabbcc', 1000, 2000, 100.0, 3)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Two good positions then a ghost (teleport)
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1000, 52.0, 21.0)", (fid,))
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1005, 52.001, 21.001)", (fid,))
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1010, 80.0, 21.0)", (fid,))  # ghost
        conn.commit()
        conn.close()
        import sys
        sys.argv = ["purge_ghosts", "--db", db_path, "--max-speed", "2000"]
        main()
        out = capsys.readouterr().out
        assert "ghost position(s)" in out
        assert "dry-run" in out.lower() or "Dry-run" in out

    def test_apply_mode(self, tmp_path, capsys):
        from purge_ghosts import main
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm, total_positions) "
            "VALUES ('aabbcc', 1000, 2000, 100.0, 3)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1000, 52.0, 21.0)", (fid,))
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1005, 52.001, 21.001)", (fid,))
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1010, 80.0, 21.0)", (fid,))
        conn.commit()
        conn.close()
        import sys
        sys.argv = ["purge_ghosts", "--db", db_path, "--max-speed", "2000",
                    "--apply", "--i-have-a-backup"]
        main()
        out = capsys.readouterr().out
        assert "Done" in out
        # Verify ghost was removed
        conn2 = database.connect(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM positions WHERE flight_id = ?", (fid,)).fetchone()[0]
        assert count == 2
        conn2.close()

    def test_apply_takes_snapshot_by_default(self, tmp_path, capsys):
        """Without --i-have-a-backup, --apply must produce a backup-*.db
        sibling file before mutating."""
        from purge_ghosts import main
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm, total_positions) "
            "VALUES ('aabbcc', 1000, 2000, 100.0, 3)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1000, 52.0, 21.0)", (fid,))
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1010, 80.0, 21.0)", (fid,))
        conn.commit()
        conn.close()
        import sys
        sys.argv = ["purge_ghosts", "--db", db_path, "--max-speed", "2000", "--apply"]
        main()
        out = capsys.readouterr().out
        assert "Snapshot:" in out
        # Exactly one backup file should now sit next to the DB.
        backups = list(tmp_path.glob("test.db.backup-*.db"))
        assert len(backups) == 1, f"expected 1 snapshot, got {backups}"
