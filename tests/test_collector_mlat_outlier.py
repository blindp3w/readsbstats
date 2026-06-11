"""Regression for W-4 (Audit 2026-06-01): _close_flight's statistical MLAT
outlier filter must not erase valid ground-speed readings when ≥75% of the
flight's MLAT GS samples are 0.

When p75 of the GS distribution is 0 (ground movement, low-quality feed),
threshold = p75 * factor = 0, and the old `gs > threshold` predicate matches
EVERY positive reading — nulling all of them and recomputing max_gs as NULL.
Legitimate taxi/takeoff-roll fixes silently disappeared. Fix: guard the
filter behind `if p75 > 0`.
"""
from __future__ import annotations

import pytest

from readsbstats import collector, config, enrichment
from tests._helpers import insert_position, make_db


@pytest.fixture(autouse=True)
def setup():
    collector._active.clear()
    collector._notified_icao.clear()
    collector._squawk_notified.clear()
    enrichment.clear_cache()
    yield


def _seed_flight_with_mlat_gs(conn, gs_values: list[float]) -> int:
    """Open a flight, insert MLAT positions with the given gs values, return flight_id."""
    fid = collector._open_flight(
        conn, "aabbcc", 1000, None, None, None,
        None, None, 52.0, 21.0, None, None, "mlat", None, None,
    )
    # Insert each MLAT position with the supplied gs.
    for i, gs in enumerate(gs_values):
        insert_position(conn, fid, 1000 + i, lat=52.0, lon=21.0, gs=gs,
                        source_type="mlat")
    # Reflect the inserts in the aggregated counts so _close_flight does not
    # take the "too few positions" branch.
    conn.execute(
        "UPDATE flights SET total_positions=?, adsb_positions=0, mlat_positions=?, "
        "max_gs=? WHERE id=?",
        (len(gs_values), len(gs_values), max(gs_values), fid),
    )
    conn.commit()
    return fid


class TestMlatOutlierP75ZeroGuard:
    def test_zero_heavy_mlat_keeps_valid_taxi_readings(self):
        """W-4 regression: 19 zeros + 3 valid (gs=15) → all 3 valid readings survive.

        statistics.quantiles uses the "exclusive" interpolation by default, so
        p75 only collapses to 0 when the upper quartile *cut point* lands on
        a pair of zeros. With 22 samples, the 75% cut point is between indices
        16 and 17 (both zero) → p75 = 0 → threshold = 0 → old code nulled all
        positive readings.
        """
        conn = make_db()
        try:
            fid = _seed_flight_with_mlat_gs(conn, [0.0]*19 + [15.0]*3)
            with conn:
                collector._close_flight(conn, "aabbcc")

            survivors = conn.execute(
                "SELECT gs / 10.0 AS gs FROM positions WHERE flight_id=? "
                "AND gs IS NOT NULL AND gs > 0 ORDER BY gs",
                (fid,),
            ).fetchall()
            assert len(survivors) == 3, (
                f"expected 3 surviving positive GS rows, got {len(survivors)}"
            )

            max_gs = conn.execute(
                "SELECT max_gs FROM flights WHERE id=?", (fid,),
            ).fetchone()["max_gs"]
            assert max_gs == 15.0, f"expected max_gs=15.0, got {max_gs}"
        finally:
            conn.close()

    def test_high_gs_outlier_still_filtered_when_p75_positive(self):
        """Existing behaviour preserved: when p75 > 0, real outliers (5× p75)
        are still nulled out — this is the filter's actual job."""
        conn = make_db()
        try:
            # 9 readings of gs=100 + 3 of gs=120 + 1 spike of gs=900.
            # p75 ≈ 120, threshold = 120 * 5 = 600, so gs=900 is filtered.
            gs_values = [100.0]*9 + [120.0]*3 + [900.0]
            fid = _seed_flight_with_mlat_gs(conn, gs_values)
            with conn:
                collector._close_flight(conn, "aabbcc")

            survivors = conn.execute(
                "SELECT gs / 10.0 AS gs FROM positions WHERE flight_id=? "
                "AND gs IS NOT NULL ORDER BY gs DESC LIMIT 1",
                (fid,),
            ).fetchone()
            assert survivors["gs"] == 120.0, (
                f"outlier should have been nulled; saw {survivors['gs']} as max"
            )

            max_gs = conn.execute(
                "SELECT max_gs FROM flights WHERE id=?", (fid,),
            ).fetchone()["max_gs"]
            assert max_gs == 120.0
        finally:
            conn.close()
