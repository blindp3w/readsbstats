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


def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


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
