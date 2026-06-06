"""Tests for purge_mlat_gs_spikes.py."""

import sqlite3

import pytest

from readsbstats import database
from purge_mlat_gs_spikes import (
    apply_purge,
    scan_mlat_spikes,
    scan_orphan_max_gs,
    scan_statistical_outliers,
    _new_max_gs,
)


from tests._helpers import make_db  # noqa: E402 — kept under section header


def insert_flight(conn, icao="aabbcc", max_gs=500.0) -> int:
    cur = conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen, max_gs) "
        "VALUES (?, 1000, 9000, ?)",
        (icao, max_gs),
    )
    conn.commit()
    return cur.lastrowid


def insert_pos(conn, flight_id, ts, gs, source_type="mlat") -> int:
    cur = conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, gs, source_type) "
        "VALUES (?, ?, 52.0, 21.0, ?, ?)",
        (flight_id, ts, gs, source_type),
    )
    conn.commit()
    return cur.lastrowid


ACCEL_LIMIT = 8.0


# ---------------------------------------------------------------------------
# scan_mlat_spikes
# ---------------------------------------------------------------------------

class TestScanMlatSpikes:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_spike_detected(self):
        """MLAT position with 90→700 in 7s must be flagged."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        pid = insert_pos(self.conn, fid, 1007, 700.0)  # 610/7 = 87 kts/s
        insert_pos(self.conn, fid, 1014, 95.0)
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid in bad
        assert pid in bad[fid]

    def test_normal_acceleration_not_flagged(self):
        """MLAT position with 90→125 in 5s (7 kts/s) must not be flagged."""
        fid = insert_flight(self.conn, max_gs=125.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        insert_pos(self.conn, fid, 1005, 125.0)
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid not in bad

    def test_exactly_at_threshold_not_flagged(self):
        """Acceleration exactly at limit (8.0 kts/s) must not be flagged."""
        fid = insert_flight(self.conn, max_gs=140.0)
        insert_pos(self.conn, fid, 1000, 100.0)
        insert_pos(self.conn, fid, 1005, 140.0)  # 40/5 = 8.0 exactly
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid not in bad

    def test_adsb_positions_not_flagged(self):
        """ADS-B positions must not be flagged regardless of acceleration."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0, source_type="adsb_icao")
        insert_pos(self.conn, fid, 1007, 700.0, source_type="adsb_icao")
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid not in bad

    def test_prev_gs_not_advanced_on_spike(self):
        """After a spike, prev_gs must stay at the last good value."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        insert_pos(self.conn, fid, 1005, 700.0)  # spike
        # If prev_gs advanced to 700, this would be 50/5=10 kts/s (flagged)
        # But if prev_gs stayed at 90, this is 560/5=112 kts/s (also flagged)
        # Either way it's a spike, but the point is prev_gs must not advance
        pid3 = insert_pos(self.conn, fid, 1010, 650.0)
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert pid3 in bad[fid]

    def test_recovery_after_spike(self):
        """Good position after a spike must not be flagged (prev_gs=last good)."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        insert_pos(self.conn, fid, 1007, 700.0)  # spike
        pid3 = insert_pos(self.conn, fid, 1014, 95.0)  # |90-95|/14 = 0.36 kts/s
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert pid3 not in bad[fid]

    def test_deceleration_spike(self):
        """Spike in the other direction (high→low) must also be caught."""
        fid = insert_flight(self.conn, max_gs=400.0)
        insert_pos(self.conn, fid, 1000, 400.0)
        pid = insert_pos(self.conn, fid, 1005, 10.0)  # 390/5 = 78 kts/s
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid in bad
        assert pid in bad[fid]

    def test_multiple_spikes_in_same_flight(self):
        """Multiple spikes in one flight must all be flagged."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        pid1 = insert_pos(self.conn, fid, 1007, 700.0)
        insert_pos(self.conn, fid, 1014, 95.0)
        pid2 = insert_pos(self.conn, fid, 1021, 500.0)
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert pid1 in bad[fid]
        assert pid2 in bad[fid]

    def test_first_position_not_flagged(self):
        """First position with no predecessor must not be flagged."""
        fid = insert_flight(self.conn, max_gs=500.0)
        insert_pos(self.conn, fid, 1000, 500.0)
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert fid not in bad

    def test_mixed_sources_only_mlat_flagged(self):
        """In a mixed-source flight, only MLAT spikes are flagged."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0, source_type="adsb_icao")
        insert_pos(self.conn, fid, 1007, 700.0, source_type="adsb_icao")  # adsb — skip
        pid = insert_pos(self.conn, fid, 1014, 90.0, source_type="mlat")
        # prev_gs=700 (from adsb), mlat gs=90, |700-90|/7 = 87 kts/s
        bad = scan_mlat_spikes(self.conn, ACCEL_LIMIT)
        assert pid in bad[fid]


# ---------------------------------------------------------------------------
# scan_orphan_max_gs
# ---------------------------------------------------------------------------

class TestScanOrphanMaxGs:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_orphan_detected(self):
        """Flight with max_gs > all stored position gs must be detected."""
        fid = insert_flight(self.conn, max_gs=771.3)
        insert_pos(self.conn, fid, 1000, 408.0)
        insert_pos(self.conn, fid, 1081, 410.7)
        orphans = scan_orphan_max_gs(self.conn)
        assert fid in orphans
        assert orphans[fid] == pytest.approx(410.7)

    def test_no_orphan_when_max_gs_matches(self):
        """Flight with max_gs matching stored positions must not be flagged."""
        fid = insert_flight(self.conn, max_gs=410.7)
        insert_pos(self.conn, fid, 1000, 408.0)
        insert_pos(self.conn, fid, 1081, 410.7)
        orphans = scan_orphan_max_gs(self.conn)
        assert fid not in orphans

    def test_orphan_when_all_gs_nulled(self):
        """Audit 17: a flight whose only GS samples were all nulled (e.g. by a
        prior purge) but still carries a stale max_gs is an orphan — its correct
        max_gs is NULL. The old INNER JOIN dropped such flights entirely, so the
        phantom max_gs was never reset."""
        fid = insert_flight(self.conn, max_gs=600.0)
        insert_pos(self.conn, fid, 1000, None)
        insert_pos(self.conn, fid, 1081, None)
        orphans = scan_orphan_max_gs(self.conn)
        assert fid in orphans
        assert orphans[fid] is None

    def test_no_orphan_when_max_gs_already_null(self):
        """A flight with no GS data AND max_gs already NULL is correct — not an
        orphan, must not be flagged."""
        fid = insert_flight(self.conn, max_gs=None)
        insert_pos(self.conn, fid, 1000, None)
        orphans = scan_orphan_max_gs(self.conn)
        assert fid not in orphans


# ---------------------------------------------------------------------------
# _new_max_gs — empty-list guard (audit-12 #164)
# ---------------------------------------------------------------------------

class TestNewMaxGsEmptyList:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_empty_bad_ids_does_not_raise(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 400.0)
        insert_pos(self.conn, fid, 1005, 450.0)
        result = _new_max_gs(self.conn, fid, [])
        assert result == 450.0

    def test_empty_bad_ids_with_no_positions_returns_none(self):
        fid = insert_flight(self.conn)
        result = _new_max_gs(self.conn, fid, [])
        assert result is None


class TestApplyPurgeBatching:
    """Audit-12 #P3.2 — apply_purge must commit periodically rather than
    holding the write lock for the whole flight loop."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_apply_purge_batches_commits(self):
        from purge_mlat_gs_spikes import _BATCH_SIZE
        from tests._helpers import CountingConn as _CountingConn

        bad: dict[int, list[int]] = {}
        n_flights = _BATCH_SIZE * 2 + 5
        for i in range(n_flights):
            fid = insert_flight(self.conn, icao=f"a{i:05x}")
            insert_pos(self.conn, fid, 1000 + i, 90.0)
            spike = insert_pos(self.conn, fid, 1007 + i, 700.0)
            bad[fid] = [spike]

        counter = _CountingConn(self.conn)
        apply_purge(counter, bad, {})
        assert counter.commits >= 3, (
            f"expected ≥3 commits for {n_flights} flights at batch={_BATCH_SIZE},"
            f" got {counter.commits}"
        )


# ---------------------------------------------------------------------------
# apply_purge
# ---------------------------------------------------------------------------

class TestApplyPurge:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_spike_gs_nulled_and_max_gs_updated(self):
        """apply_purge must null spike gs and recompute max_gs."""
        fid = insert_flight(self.conn, max_gs=700.0)
        insert_pos(self.conn, fid, 1000, 90.0)
        pid = insert_pos(self.conn, fid, 1007, 700.0)
        insert_pos(self.conn, fid, 1014, 95.0)

        bad = {fid: [pid]}
        apply_purge(self.conn, bad, {})

        gs = self.conn.execute("SELECT gs FROM positions WHERE id = ?", (pid,)).fetchone()[0]
        assert gs is None

        max_gs = self.conn.execute("SELECT max_gs FROM flights WHERE id = ?", (fid,)).fetchone()[0]
        assert max_gs == pytest.approx(95.0)

    def test_orphan_max_gs_fixed(self):
        """apply_purge must fix orphan max_gs from stored positions."""
        fid = insert_flight(self.conn, max_gs=771.3)
        insert_pos(self.conn, fid, 1000, 408.0)
        insert_pos(self.conn, fid, 1081, 410.7)

        apply_purge(self.conn, {}, {fid: 410.7})

        max_gs = self.conn.execute("SELECT max_gs FROM flights WHERE id = ?", (fid,)).fetchone()[0]
        assert max_gs == pytest.approx(410.7)


# ---------------------------------------------------------------------------
# scan_statistical_outliers
# ---------------------------------------------------------------------------

class TestScanStatisticalOutliers:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_isolated_leading_spike_flagged(self):
        """First isolated GS 10× the flight median must be flagged (the SP-RAM case)."""
        fid = insert_flight(self.conn, max_gs=724.0)
        spike_pid = insert_pos(self.conn, fid, 1000, 724.0)
        for t in range(2000, 2110, 10):  # 11 normal positions
            insert_pos(self.conn, fid, t, 70.0)
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert fid in bad
        assert spike_pid in bad[fid]

    def test_normal_flight_not_flagged(self):
        """Flight with uniform GS must not produce any outliers."""
        fid = insert_flight(self.conn, max_gs=80.0)
        for i, t in enumerate(range(1000, 1110, 10)):
            insert_pos(self.conn, fid, t, 70.0 + (i % 15))  # 70–84 kts
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert fid not in bad

    def test_too_few_readings_skipped(self):
        """Flight with fewer than min_readings MLAT GS values must not be scanned."""
        fid = insert_flight(self.conn, max_gs=724.0)
        spike_pid = insert_pos(self.conn, fid, 1000, 724.0)
        for t in range(2000, 2080, 10):  # 8 normal readings — total 9 < min 10
            insert_pos(self.conn, fid, t, 70.0)
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert fid not in bad

    def test_adsb_positions_excluded(self):
        """ADS-B positions must not count toward or be flagged by the outlier scan."""
        fid = insert_flight(self.conn, max_gs=724.0)
        insert_pos(self.conn, fid, 1000, 724.0, source_type="adsb_icao")  # adsb spike
        for t in range(2000, 2110, 10):
            insert_pos(self.conn, fid, t, 70.0, source_type="adsb_icao")
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert fid not in bad

    def test_multiple_outliers_all_flagged(self):
        """Multiple MLAT GS outliers in the same flight must all be flagged."""
        fid = insert_flight(self.conn, max_gs=800.0)
        pid1 = insert_pos(self.conn, fid, 1000, 800.0)
        pid2 = insert_pos(self.conn, fid, 1100, 750.0)
        for t in range(2000, 2110, 10):  # 11 normals
            insert_pos(self.conn, fid, t, 70.0)
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert fid in bad
        assert pid1 in bad[fid]
        assert pid2 in bad[fid]

    def test_normal_not_flagged_alongside_spike(self):
        """Normal GS values must survive even when a spike is flagged."""
        fid = insert_flight(self.conn, max_gs=724.0)
        insert_pos(self.conn, fid, 1000, 724.0)  # spike
        normal_pid = insert_pos(self.conn, fid, 2000, 70.0)
        for t in range(2010, 2110, 10):  # 10 more normals
            insert_pos(self.conn, fid, t, 70.0)
        bad = scan_statistical_outliers(self.conn, outlier_factor=5.0, min_readings=10)
        assert normal_pid not in bad.get(fid, [])

    # -----------------------------------------------------------------------
    # Absolute floor — protect genuinely-fast-but-plausible flights
    # -----------------------------------------------------------------------

    def test_fast_but_plausible_not_overnulled_at_low_factor(self):
        """A genuinely fast flight (climb-out / military pass) whose real
        high-GS samples are statistical outliers of its own (mostly-slow)
        distribution must NOT be nulled at a low outlier_factor: the samples
        are below the absolute plausible-real floor, so they're kept and
        max_gs is not silently lowered.
        """
        from purge_mlat_gs_spikes import _MIN_GS_OUTLIER_FLOOR

        fid = insert_flight(self.conn, max_gs=450.0)
        # 12 slow samples (taxi / low approach) → low p75.
        for t in range(1000, 1120, 10):
            insert_pos(self.conn, fid, t, 150.0)
        # 3 genuinely-fast samples, comfortably under the floor.
        fast_gs = _MIN_GS_OUTLIER_FLOOR - 100.0
        fast_pids = [
            insert_pos(self.conn, fid, 2000, fast_gs),
            insert_pos(self.conn, fid, 2010, fast_gs),
            insert_pos(self.conn, fid, 2020, fast_gs),
        ]
        # Low factor: without the floor, 2.0 × p75(~150) would null the fast ones.
        bad = scan_statistical_outliers(self.conn, outlier_factor=2.0, min_readings=10)
        for pid in fast_pids:
            assert pid not in bad.get(fid, []), (
                "genuinely-fast-but-plausible sample nulled below the "
                "absolute floor"
            )

    def test_obvious_spike_still_flagged_above_floor(self):
        """A sample above the absolute floor that is also a statistical
        outlier is still flagged — the floor only protects plausible-real
        speeds, not implausible MLAT spikes."""
        from purge_mlat_gs_spikes import _MIN_GS_OUTLIER_FLOOR

        fid = insert_flight(self.conn, max_gs=1100.0)
        for t in range(1000, 1120, 10):
            insert_pos(self.conn, fid, t, 150.0)
        spike_gs = _MIN_GS_OUTLIER_FLOOR + 400.0   # implausible MLAT spike
        spike_pid = insert_pos(self.conn, fid, 2000, spike_gs)
        bad = scan_statistical_outliers(self.conn, outlier_factor=2.0, min_readings=10)
        assert fid in bad
        assert spike_pid in bad[fid]


# ---------------------------------------------------------------------------
# main() CLI — audit-12 #203
# ---------------------------------------------------------------------------

import os
import sys
import tempfile

from purge_mlat_gs_spikes import main


def make_file_db(path: str) -> sqlite3.Connection:
    conn = database.connect(path)
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


class TestMain:
    """Sibling purge scripts (purge_bad_gs, purge_ghosts) each have a
    TestMain class exercising the CLI; this one didn't (audit-12 #203)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = make_file_db(self.db_path)
        yield
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + ext)
            except FileNotFoundError:
                pass
        # Tidy up any snapshot the apply path may have created
        for entry in os.listdir(self.tmpdir):
            try:
                os.unlink(os.path.join(self.tmpdir, entry))
            except OSError:
                pass
        os.rmdir(self.tmpdir)

    def _insert_spike_flight(self) -> int:
        fid = insert_flight(self.conn, max_gs=724.0)
        # Normal MLAT readings + one spike
        insert_pos(self.conn, fid, 1000, 70.0)
        insert_pos(self.conn, fid, 1007, 724.0)   # spike (delta/dt = 93 kts/s)
        insert_pos(self.conn, fid, 1014, 75.0)
        return fid

    def test_dry_run_does_not_modify(self, monkeypatch, capsys):
        fid = self._insert_spike_flight()
        monkeypatch.setattr("sys.argv", [
            "purge_mlat_gs_spikes.py", "--db", self.db_path,
        ])
        main()
        out = capsys.readouterr().out
        # dry-run header present
        assert "dry-run" in out.lower()
        # Detected at least one spike
        assert "spike" in out.lower()
        # Data unchanged
        row = self.conn.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] == 724.0

    def test_apply_modifies_data(self, monkeypatch, capsys):
        fid = self._insert_spike_flight()
        monkeypatch.setattr("sys.argv", [
            "purge_mlat_gs_spikes.py", "--db", self.db_path,
            "--apply", "--i-have-a-backup",
        ])
        main()
        out = capsys.readouterr().out
        assert "Done" in out
        check = sqlite3.connect(self.db_path)
        check.row_factory = sqlite3.Row
        max_gs = check.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0]
        check.close()
        # Spike nulled → max should fall to the largest normal reading (75.0)
        assert max_gs == 75.0

    def test_no_spikes_prints_clean(self, monkeypatch, capsys):
        # All-normal flight → nothing to fix.
        fid = insert_flight(self.conn, max_gs=80.0)
        insert_pos(self.conn, fid, 1000, 70.0)
        insert_pos(self.conn, fid, 1010, 75.0)
        insert_pos(self.conn, fid, 1020, 80.0)
        monkeypatch.setattr("sys.argv", [
            "purge_mlat_gs_spikes.py", "--db", self.db_path,
        ])
        main()
        out = capsys.readouterr().out
        assert "No MLAT spikes" in out

    def test_apply_takes_snapshot_by_default(self, monkeypatch, capsys):
        self._insert_spike_flight()
        monkeypatch.setattr("sys.argv", [
            "purge_mlat_gs_spikes.py", "--db", self.db_path, "--apply",
        ])
        main()
        # A snapshot file was created next to the DB
        snapshots = [
            f for f in os.listdir(self.tmpdir)
            if f.startswith("test.db.backup-")
        ]
        assert len(snapshots) >= 1, "snapshot not created on --apply (without --i-have-a-backup)"
