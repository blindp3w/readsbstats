"""Tests for purge_ghosts.py."""

import logging
import math
import sqlite3

import pytest

from readsbstats import database, geo
from purge_ghosts import apply_purge, find_ghost_ids, haversine_nm, max_distance_after_purge

RLAT = 52.24199
RLON = 21.02872
MAX_SPEED = 2000


from tests._helpers import insert_position, make_db  # noqa: E402 — kept under section header


def insert_flight(conn, icao="aabbcc") -> int:
    cur = conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES (?,1000,9000)",
        (icao,),
    )
    conn.commit()
    return cur.lastrowid


def insert_pos(conn, flight_id, ts, lat, lon) -> int:
    pid = insert_position(conn, flight_id, ts, lat=lat, lon=lon)
    conn.commit()
    return pid


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

    def test_both_ends_ghost_flight_is_skipped_not_mispurged(self, caplog):
        """A flight bookended by ghosts at BOTH ends poisons the forward AND the
        backward velocity pass (each anchors on a ghost). The old code trusted
        the poisoned backward result and deleted the real fixes while keeping the
        trailing ghost. The fix detects the unresolvable case and skips the flight
        (flags nothing) + warns for manual review (Audit 2026-06-20)."""
        fid = insert_flight(self.conn)
        # Two far-apart ghosts (north + south, far from Warsaw and from each
        # other) bracketing two consistent real Warsaw fixes.
        insert_pos(self.conn, fid, 1000, 59.7, 21.5)    # opening ghost (far N)
        insert_pos(self.conn, fid, 1070, 52.6, 20.75)   # real
        insert_pos(self.conn, fid, 1130, 52.5, 20.6)    # real
        insert_pos(self.conn, fid, 1200, 40.0, 21.0)    # trailing ghost (far S)

        with caplog.at_level(logging.WARNING):
            ghosts = find_ghost_ids(self.conn, MAX_SPEED)

        # Flight skipped entirely — nothing flagged, so apply_purge can't delete
        # the real fixes (strictly safer than the old mis-purge).
        assert fid not in ghosts
        assert "bookended by outliers" in caplog.text

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

    def test_recomputes_max_distance_bearing_from_survivors(self):
        """BUG (audit 2026-06-15): apply_purge updated max_distance_nm but left
        max_distance_bearing pointing at the deleted ghost, so the polar/range
        plot rendered a max-distance marker at a bearing no surviving position
        supports. The collector updates both columns in lockstep
        (collector.py:866 / :1152) — the purge must too."""
        fid = insert_flight(self.conn)
        # Surviving fixes due NORTH of the receiver (bearing ~0°).
        surv_lat, surv_lon = RLAT + 0.5, RLON
        insert_pos(self.conn, fid, 1000, surv_lat, surv_lon)      # ~30 nm N, anchor survivor
        # Ghost far due EAST (bearing ~90°), 5 s later → huge implied speed →
        # flagged AND the farthest point.
        ghost_lat, ghost_lon = RLAT, RLON + 8.0
        gid = insert_pos(self.conn, fid, 1005, ghost_lat, ghost_lon)
        insert_pos(self.conn, fid, 1070, RLAT + 0.4, RLON)        # ~24 nm N, survivor

        # Simulate the collector having recorded the GHOST as the max-distance point.
        ghost_bearing = geo.bearing(RLAT, RLON, ghost_lat, ghost_lon)  # ~90°
        self.conn.execute(
            "UPDATE flights SET max_distance_nm = ?, max_distance_bearing = ? WHERE id = ?",
            (geo.haversine_nm(RLAT, RLON, ghost_lat, ghost_lon), ghost_bearing, fid),
        )
        self.conn.commit()

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert ghosts.get(fid) == [gid]   # precondition: ghost detected
        apply_purge(self.conn, ghosts, RLAT, RLON)

        row = self.conn.execute(
            "SELECT max_distance_nm, max_distance_bearing FROM flights WHERE id = ?",
            (fid,),
        ).fetchone()
        expected_bearing = geo.bearing(RLAT, RLON, surv_lat, surv_lon)      # ~0° (due N)
        expected_dist = geo.haversine_nm(RLAT, RLON, surv_lat, surv_lon)    # ~30 nm
        # pytest.approx: lat/lon round-trip through posenc ×1e5 scaling (~1 m).
        assert row["max_distance_bearing"] == pytest.approx(expected_bearing, abs=0.1)
        assert row["max_distance_nm"] == pytest.approx(expected_dist, abs=0.1)
        # And specifically NOT the stale ghost bearing.
        assert abs(row["max_distance_bearing"] - ghost_bearing) > 1.0

    def test_all_positions_deleted_nulls_distance_and_bearing(self):
        """No survivors → both max_distance_nm and max_distance_bearing clear to NULL."""
        fid = insert_flight(self.conn)
        p1 = insert_pos(self.conn, fid, 1000, 52.6, 20.75)
        p2 = insert_pos(self.conn, fid, 1010, 52.5, 20.6)
        self.conn.execute(
            "UPDATE flights SET max_distance_nm = 50, max_distance_bearing = 123 WHERE id = ?",
            (fid,),
        )
        self.conn.commit()

        apply_purge(self.conn, {fid: [p1, p2]}, RLAT, RLON)   # every position removed

        row = self.conn.execute(
            "SELECT max_distance_nm, max_distance_bearing FROM flights WHERE id = ?",
            (fid,),
        ).fetchone()
        assert row["max_distance_nm"] is None
        assert row["max_distance_bearing"] is None

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
        from tests._helpers import CountingConn as _CountingConn

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

    def test_interrupted_apply_is_resumable(self):
        """apply_purge commits per _BATCH_SIZE; a crash mid-run leaves earlier
        batches committed and later ones rolled back. Re-running must finish the
        job — the docstring's idempotency claim. Audit 2026-06-20."""
        from purge_ghosts import _BATCH_SIZE
        from tests._helpers import CountingConn

        n_flights = _BATCH_SIZE * 2 + 5
        ghost_ids_all: list[int] = []
        for i in range(n_flights):
            fid = insert_flight(self.conn, icao=f"a{i:05x}")
            insert_pos(self.conn, fid, 1000 + i, 52.6, 20.75)            # real anchor
            ghost_ids_all.append(insert_pos(self.conn, fid, 1005 + i, 59.7, 21.5))  # ghost

        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert len(ghosts) == n_flights

        ph = ",".join("?" * len(ghost_ids_all))

        def remaining_ghosts() -> int:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM positions WHERE id IN ({ph})", ghost_ids_all
            ).fetchone()[0]

        # Crash on the 2nd batch commit, then simulate the crash's rollback.
        with pytest.raises(RuntimeError, match="interrupt"):
            apply_purge(CountingConn(self.conn, raise_on_commit=2), ghosts, RLAT, RLON)
        self.conn.rollback()

        after_crash = remaining_ghosts()
        assert 0 < after_crash < len(ghost_ids_all)   # partial: batch 1 purged, rest not

        # Re-running the scan+purge finishes the job cleanly.
        apply_purge(self.conn, find_ghost_ids(self.conn, MAX_SPEED), RLAT, RLON)
        assert remaining_ghosts() == 0


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
        insert_position(conn, fid, 1000, lat=52.0, lon=21.0)
        insert_position(conn, fid, 1005, lat=52.001, lon=21.001)
        insert_position(conn, fid, 1010, lat=80.0, lon=21.0)  # ghost
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
        insert_position(conn, fid, 1000, lat=52.0, lon=21.0)
        insert_position(conn, fid, 1005, lat=52.001, lon=21.001)
        insert_position(conn, fid, 1010, lat=80.0, lon=21.0)
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
        insert_position(conn, fid, 1000, lat=52.0, lon=21.0)
        insert_position(conn, fid, 1010, lat=80.0, lon=21.0)
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


# ---------------------------------------------------------------------------
# Audit 2026-05-26: NULL-coordinate defence
# ---------------------------------------------------------------------------


class TestNullCoordinateGuard:
    """Historical rows can have NULL lat/lon (collector crashes, schema
    migrations, dirty shutdowns). Passing None to haversine_nm crashes
    with TypeError; the purge must skip those rows."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_find_ghost_ids_skips_null_coord_rows(self):
        fid = insert_flight(self.conn)
        insert_position(self.conn, fid, 1000, lat=None, lon=None)
        # Valid positions on either side of the null
        insert_pos(self.conn, fid, 999, 52.6, 20.75)
        insert_pos(self.conn, fid, 1001, 52.61, 20.76)
        self.conn.commit()

        # Before the fix: TypeError(math.radians(None)) aborts the scan.
        ghosts = find_ghost_ids(self.conn, MAX_SPEED)
        assert ghosts == {}  # neighbouring rows are slow enough not to be ghosts
