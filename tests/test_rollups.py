"""Unit tests for the daily heatmap/coverage rollups."""
import sqlite3

import pytest

from readsbstats import rollups
from tests._helpers import make_db


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
