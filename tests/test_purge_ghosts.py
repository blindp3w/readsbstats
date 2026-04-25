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
        sys.argv = ["purge_ghosts", "--db", db_path, "--max-speed", "2000", "--apply"]
        main()
        out = capsys.readouterr().out
        assert "Done" in out
        # Verify ghost was removed
        conn2 = database.connect(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM positions WHERE flight_id = ?", (fid,)).fetchone()[0]
        assert count == 2
        conn2.close()
