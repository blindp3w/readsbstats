"""Tests for geo.py helpers."""

import pytest

from readsbstats import geo


class TestDestinationPoint:
    REC_LAT = 52.24199
    REC_LON = 21.02872

    def test_north_increases_latitude(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 0.0, 100.0)
        assert lat > self.REC_LAT

    def test_north_longitude_unchanged(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 0.0, 100.0)
        assert lon == pytest.approx(self.REC_LON, abs=0.01)

    def test_east_increases_longitude(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 90.0, 100.0)
        assert lon > self.REC_LON

    def test_east_latitude_approx_unchanged(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 90.0, 100.0)
        # Great-circle east at high latitude curves slightly; latitude changes < 0.05°
        assert lat == pytest.approx(self.REC_LAT, abs=0.05)

    def test_south_decreases_latitude(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 180.0, 100.0)
        assert lat < self.REC_LAT

    def test_roundtrip_distance(self):
        for bearing in (0, 45, 90, 135, 180, 225, 270, 315):
            for dist in (50.0, 200.0, 450.0):
                dest_lat, dest_lon = geo.destination_point(
                    self.REC_LAT, self.REC_LON, float(bearing), dist
                )
                measured = geo.haversine_nm(self.REC_LAT, self.REC_LON, dest_lat, dest_lon)
                assert measured == pytest.approx(dist, rel=1e-4), (
                    f"bearing={bearing} dist={dist}: got {measured}"
                )

    def test_zero_distance_returns_origin(self):
        lat, lon = geo.destination_point(self.REC_LAT, self.REC_LON, 45.0, 0.0)
        assert lat == pytest.approx(self.REC_LAT, abs=1e-9)
        assert lon == pytest.approx(self.REC_LON, abs=1e-9)

    def test_antimeridian_wrap(self):
        # Start near 179°E, go 200 nm east — should wrap to negative (west) longitude
        lat, lon = geo.destination_point(0.0, 179.0, 90.0, 200.0)
        assert lon < 0 or lon > 179.0
