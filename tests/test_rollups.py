"""Unit tests for the daily heatmap/coverage rollups."""
import sqlite3
import time

import pytest

from readsbstats import config, database, geo, rollups
from tests._helpers import insert_position, make_db


class TestBucket:
    def test_half_up_positive(self):
        assert rollups.bucket(52.2049, 100) == 5220   # floor(5220.49+0.5)
        assert rollups.bucket(52.2051, 100) == 5221

    def test_half_up_negative(self):
        # Must match SQL FLOOR(lat*scale + 0.5) for southern/western coords.
        assert rollups.bucket(-0.004, 100) == 0       # floor(0.1)
        assert rollups.bucket(-0.006, 100) == -1      # floor(-0.1)

    def test_bucket_sql_parity(self):
        """Python bucket() must agree with SQLite CAST(FLOOR(?*?+0.5) AS INTEGER)
        for knife-edge values across both scales."""
        knife_edge_values = [
            52.205, -0.005, 89.995, -89.995, 179.995, -179.995,
            0.0, 52.2049, -0.006, 21.00005,
        ]
        mem = sqlite3.connect(":memory:")
        for scale in rollups.SCALES:
            for v in knife_edge_values:
                sql_result = mem.execute(
                    "SELECT CAST(FLOOR(? * ? + 0.5) AS INTEGER)", (v, scale)
                ).fetchone()[0]
                py_result = rollups.bucket(v, scale)
                assert py_result == sql_result, (
                    f"bucket({v!r}, {scale}) = {py_result!r} "
                    f"but SQL = {sql_result!r}"
                )
        mem.close()


class TestAccumulatorFlush:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_add_and_flush_writes_both_scales(self):
        acc = rollups.RollupAccumulator()
        acc.add(ts=86400 * 100 + 5, lat=52.2, lon=21.0, dist_nm=30.0, bearing_deg=123.4)
        acc.add(ts=86400 * 100 + 10, lat=52.2, lon=21.0, dist_nm=45.0, bearing_deg=123.9)
        rollups.flush(self.conn, acc)
        rows = self.conn.execute(
            "SELECT scale, day, lat_b, lon_b, w FROM grid_daily ORDER BY scale"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (10, 100, 522, 210, 2),
            (100, 100, 5220, 2100, 2),
        ]
        cov = self.conn.execute(
            "SELECT day, bearing_b, max_nm FROM coverage_daily"
        ).fetchall()
        assert [tuple(c) for c in cov] == [(100, 123, 45.0)]

    def test_flush_upserts_additively(self):
        for _ in range(2):
            acc = rollups.RollupAccumulator()
            acc.add(ts=86400 * 100, lat=52.2, lon=21.0, dist_nm=30.0, bearing_deg=10.0)
            rollups.flush(self.conn, acc)
        w = self.conn.execute(
            "SELECT w FROM grid_daily WHERE scale = 100"
        ).fetchone()[0]
        assert w == 2

    def test_coverage_upsert_keeps_max(self):
        for dist in (50.0, 20.0):
            acc = rollups.RollupAccumulator()
            acc.add(ts=86400 * 100, lat=52.2, lon=21.0, dist_nm=dist, bearing_deg=10.0)
            rollups.flush(self.conn, acc)
        assert self.conn.execute(
            "SELECT max_nm FROM coverage_daily"
        ).fetchone()[0] == 50.0

    def test_flush_clears_accumulator(self):
        acc = rollups.RollupAccumulator()
        acc.add(ts=86400, lat=1.0, lon=1.0, dist_nm=1.0, bearing_deg=0.0)
        rollups.flush(self.conn, acc)
        assert not acc.grid and not acc.cov

    def test_ready_flag(self):
        assert rollups.ready(self.conn) is False
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('rollups_ready', '1')"
        )
        assert rollups.ready(self.conn) is True

    def test_flush_empty_accumulator_is_noop(self):
        """flush() on a brand-new accumulator must not raise and must write
        no rows to either rollup table."""
        rollups.flush(self.conn, rollups.RollupAccumulator())
        assert self.conn.execute("SELECT COUNT(*) FROM grid_daily").fetchone()[0] == 0
        assert self.conn.execute("SELECT COUNT(*) FROM coverage_daily").fetchone()[0] == 0


class TestBackfill:
    def test_backfill_matches_raw_group_by(self, tmp_path):
        path = str(tmp_path / "bf.db")
        conn = database.connect(path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 0, 0)")
        fid = conn.execute("SELECT id FROM flights").fetchone()[0]
        now = int(time.time())
        # three positions across two PAST days + one with NULL coords (skipped)
        known_lat, known_lon = 52.20, 21.00
        for ts, lat, lon in [
            (now - 3 * 86400, known_lat, known_lon),
            (now - 3 * 86400 + 60, 52.21, 21.01),
            (now - 2 * 86400, 52.90, 20.50),
        ]:
            insert_position(conn, fid, ts, lat=lat, lon=lon,
                            source_type="adsb_icao")
        insert_position(conn, fid, now - 2 * 86400, source_type="mlat")
        conn.commit()
        conn.close()

        rollups.backfill_and_finalize(path)

        conn = database.connect(path)
        assert rollups.ready(conn)
        assert conn.execute(
            "SELECT SUM(w) FROM grid_daily WHERE scale = 100").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM coverage_daily").fetchone()[0] >= 1
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='positions'")}
        assert "idx_positions_ts_flight" not in idx
        assert "idx_positions_ts_lat_lon" not in idx

        # Pin exact bucket placement for the known row (lat=52.20, lon=21.00):
        # scale-100 → (5220, 2100), scale-10 → (522, 210)
        expected_fine = (rollups.bucket(known_lat, rollups.FINE_SCALE),
                         rollups.bucket(known_lon, rollups.FINE_SCALE))
        assert expected_fine == (5220, 2100)
        fine_row = conn.execute(
            "SELECT lat_b, lon_b FROM grid_daily WHERE scale = ? AND lat_b = ? AND lon_b = ?",
            (rollups.FINE_SCALE, 5220, 2100)).fetchone()
        assert fine_row is not None, "Expected fine-scale bucket (5220, 2100) not found"

        # Pin backfill-SQL vs Python-twin parity for the coverage row:
        # max_nm and bearing_b for known_lat/known_lon must match geo helpers.
        expected_nm = geo.haversine_nm(
            config.RECEIVER_LAT, config.RECEIVER_LON, known_lat, known_lon)
        expected_bearing_b = int(geo.bearing(
            config.RECEIVER_LAT, config.RECEIVER_LON, known_lat, known_lon)) % 360
        cov_row = conn.execute(
            "SELECT max_nm FROM coverage_daily WHERE bearing_b = ?",
            (expected_bearing_b,)).fetchone()
        assert cov_row is not None, (
            f"No coverage row for bearing_b={expected_bearing_b}")
        assert cov_row[0] == pytest.approx(expected_nm, rel=1e-6), (
            f"coverage max_nm {cov_row[0]!r} != haversine_nm {expected_nm!r}")

        conn.close()

    def test_backfill_watermark_not_skipped_by_live_flush(self, tmp_path):
        """Regression: a live-flushed row for day D must NOT cause the backfill
        to skip day D's historical positions.

        The old `have` mechanism checked ``SELECT DISTINCT day FROM grid_daily``
        and skipped any day already in that set.  A single midnight-straddle fix
        flushed by the collector puts day D in grid_daily before the backfill
        runs — the backfill then skips all historical rows for D, and the loss
        is permanent and silent.

        The fix (watermark in meta) only advances the watermark AFTER a day's
        transaction commits, so pre-seeded rows are merged via the upsert and
        never silently dropped.
        """
        path = str(tmp_path / "wm.db")
        conn = database.connect(path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 0, 0)")
        fid = conn.execute("SELECT id FROM flights").fetchone()[0]
        now = int(time.time())
        day_d = (now - 3 * 86400) // 86400

        # 5 historical positions on day D
        for i in range(5):
            insert_position(conn, fid, day_d * 86400 + i * 60,
                            lat=52.20, lon=21.00, source_type="adsb_icao")

        # Simulate a live-flushed straddle row for day D (COARSE and FINE scales)
        conn.execute(
            "INSERT INTO grid_daily(scale, day, lat_b, lon_b, w) VALUES (?, ?, 522, 210, 1)",
            (rollups.COARSE_SCALE, day_d))
        conn.execute(
            "INSERT INTO grid_daily(scale, day, lat_b, lon_b, w) VALUES (?, ?, 5220, 2100, 1)",
            (rollups.FINE_SCALE, day_d))
        conn.commit()
        conn.close()

        rollups.backfill_and_finalize(path)

        conn = database.connect(path)
        try:
            # Historical rows (5) must be merged with the pre-seeded row (1) = 6 total
            total = conn.execute(
                "SELECT SUM(w) FROM grid_daily WHERE scale = ?",
                (rollups.FINE_SCALE,)).fetchone()[0]
            assert total == 6, (
                f"Expected 6 (5 historical + 1 pre-seeded), got {total}. "
                "Old 'have' bug would have left only 1 (the seeded row — day skipped entirely)."
            )
            assert rollups.ready(conn)
        finally:
            conn.close()

class TestPruneFine:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_prunes_old_fine_keeps_coarse_and_recent(self):
        now = int(time.time())
        today = now // 86400
        old = today - config.GRID_FINE_RETENTION_DAYS - 1
        self.conn.executemany(
            "INSERT INTO grid_daily(scale, day, lat_b, lon_b, w) VALUES (?,?,?,?,1)",
            [(100, old, 1, 1), (100, today, 1, 1), (10, old, 1, 1)],
        )
        rollups.prune_fine(self.conn, now)
        rows = {tuple(r) for r in self.conn.execute("SELECT scale, day FROM grid_daily")}
        assert rows == {(100, today), (10, old)}

    def test_prunes_boundary_exactly(self):
        """Row at day == cutoff_day survives (DELETE uses strict <)."""
        now = int(time.time())
        today = now // 86400
        cutoff_day = today - config.GRID_FINE_RETENTION_DAYS
        # cutoff_day itself must survive; cutoff_day - 1 must be deleted
        self.conn.executemany(
            "INSERT INTO grid_daily(scale, day, lat_b, lon_b, w) VALUES (?,?,?,?,1)",
            [(100, cutoff_day, 1, 1), (100, cutoff_day - 1, 1, 2)],
        )
        rollups.prune_fine(self.conn, now)
        days = {r[0] for r in self.conn.execute(
            "SELECT day FROM grid_daily WHERE scale = 100")}
        assert cutoff_day in days
        assert cutoff_day - 1 not in days


    def test_backfill_is_idempotent(self, tmp_path):
        path = str(tmp_path / "bf2.db")
        conn = database.connect(path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 0, 0)")
        fid = conn.execute("SELECT id FROM flights").fetchone()[0]
        insert_position(conn, fid, int(time.time()) - 2 * 86400,
                        lat=52.2, lon=21.0, source_type="adsb_icao")
        conn.commit()
        conn.close()

        rollups.backfill_and_finalize(path)
        rollups.backfill_and_finalize(path)   # short-circuits on ready flag

        conn = database.connect(path)
        try:
            assert conn.execute(
                "SELECT SUM(w) FROM grid_daily WHERE scale = 100").fetchone()[0] == 1
        finally:
            conn.close()
