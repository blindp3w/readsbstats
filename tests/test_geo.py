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


# ---------------------------------------------------------------------------
# Audit-13 Phase 6: direct tests for haversine_nm + bearing
#
# Previously only exercised indirectly via `TestDestinationPoint`. The
# audit (untested-public-surfaces section) called out the lack of
# compass-point coverage on `bearing` and zero direct tests on
# `haversine_nm`.
# ---------------------------------------------------------------------------

class TestHaversineNm:
    def test_identical_points_zero(self):
        assert geo.haversine_nm(52.2, 21.0, 52.2, 21.0) == pytest.approx(0.0, abs=1e-9)

    def test_one_degree_lat_at_equator_is_60_nm(self):
        # 1° of latitude is ~60 nm everywhere (great-circle definition).
        d = geo.haversine_nm(0.0, 0.0, 1.0, 0.0)
        assert d == pytest.approx(60.0, abs=0.5)

    def test_one_degree_lon_at_equator_is_60_nm(self):
        d = geo.haversine_nm(0.0, 0.0, 0.0, 1.0)
        assert d == pytest.approx(60.0, abs=0.5)

    def test_one_degree_lon_at_high_latitude_shrinks(self):
        # cos(60°) ≈ 0.5 → 1° lon ≈ 30 nm at 60°N.
        d = geo.haversine_nm(60.0, 0.0, 60.0, 1.0)
        assert d == pytest.approx(30.0, abs=0.5)

    def test_symmetry(self):
        # Distance is order-independent.
        a = geo.haversine_nm(52.2, 21.0, 30.1, -10.5)
        b = geo.haversine_nm(30.1, -10.5, 52.2, 21.0)
        assert a == pytest.approx(b, rel=1e-9)

    def test_antipodal_is_half_earth_circumference(self):
        # π × EARTH_RADIUS_NM = π × 3440.065 ≈ 10807.28 nm.
        import math
        d = geo.haversine_nm(0.0, 0.0, 0.0, 180.0)
        assert d == pytest.approx(math.pi * geo.EARTH_RADIUS_NM, abs=0.1)


class TestBearing:
    REC_LAT = 52.24199
    REC_LON = 21.02872

    def test_due_north_is_zero(self):
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT + 1.0, self.REC_LON)
        assert b == pytest.approx(0.0, abs=0.5)

    def test_due_east_is_90(self):
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT, self.REC_LON + 1.0)
        assert b == pytest.approx(90.0, abs=0.5)

    def test_due_south_is_180(self):
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT - 1.0, self.REC_LON)
        assert b == pytest.approx(180.0, abs=0.5)

    def test_due_west_is_270(self):
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT, self.REC_LON - 1.0)
        assert b == pytest.approx(270.0, abs=0.5)

    def test_northeast_in_first_quadrant(self):
        # Equal lat + lon deltas put the destination NE-ish. At 52°N the
        # cos(lat) factor shrinks the effective east displacement, so the
        # initial bearing is < 45° (more north than east). Just check it
        # falls in the right quadrant.
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT + 1.0, self.REC_LON + 1.0)
        assert 0.0 < b < 90.0

    def test_southwest_in_third_quadrant(self):
        b = geo.bearing(self.REC_LAT, self.REC_LON, self.REC_LAT - 1.0, self.REC_LON - 1.0)
        assert 180.0 < b < 270.0

    def test_always_normalised_to_0_360(self):
        # Pick a few destinations covering all quadrants; bearing must
        # never be negative or ≥ 360 (the (… + 360) % 360 normalisation
        # is the only guard).
        for dest_lat, dest_lon in (
            (60.0, 10.0), (40.0, 30.0), (10.0, -20.0), (-30.0, -45.0),
        ):
            b = geo.bearing(self.REC_LAT, self.REC_LON, dest_lat, dest_lon)
            assert 0.0 <= b < 360.0

    def test_destination_point_roundtrip(self):
        # Inverse of destination_point: dest at bearing B from origin
        # must report initial bearing B back from origin.
        for expected in (15.0, 75.0, 135.0, 200.0, 285.0, 350.0):
            dest_lat, dest_lon = geo.destination_point(
                self.REC_LAT, self.REC_LON, expected, 200.0
            )
            measured = geo.bearing(self.REC_LAT, self.REC_LON, dest_lat, dest_lon)
            assert measured == pytest.approx(expected, abs=1.0), (
                f"bearing={expected} → measured={measured}"
            )
