"""Unit tests for the daily heatmap/coverage rollups."""
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
