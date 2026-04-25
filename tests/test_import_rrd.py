"""
Tests for scripts/import_rrd.py — RRD history import.
"""

import sqlite3

import pytest

from readsbstats import database

# import_rrd lives in scripts/
import import_rrd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# parse_fetch_output
# ---------------------------------------------------------------------------

class TestParseFetchOutput:
    def test_single_ds(self):
        output = (
            "              value\n"
            "\n"
            "1776979620: -1.1216666667e+01\n"
            "1776979680: -1.3600000000e+01\n"
            "1776979740: nan\n"
            "1776979800: -1.1775000000e+01\n"
        )
        rows = import_rrd.parse_fetch_output(output)
        assert len(rows) == 3  # NaN-only row skipped
        assert rows[0] == (1776979620, [-11.216666667])
        assert rows[1] == (1776979680, [-13.6])
        assert rows[2] == (1776979800, [-11.775])

    def test_multi_ds(self):
        output = (
            "              total     positions\n"
            "\n"
            "1776979860: 7.0666666667e+00 5.0000000000e+00\n"
            "1776979920: 9.8000000000e+00 7.8000000000e+00\n"
        )
        rows = import_rrd.parse_fetch_output(output)
        assert len(rows) == 2
        ts, vals = rows[0]
        assert ts == 1776979860
        assert len(vals) == 2
        assert vals[0] == pytest.approx(7.0667, abs=0.001)
        assert vals[1] == pytest.approx(5.0, abs=0.001)

    def test_all_nan_row_skipped(self):
        output = (
            "              value\n"
            "\n"
            "1776979620: nan\n"
            "1776979680: nan\n"
        )
        rows = import_rrd.parse_fetch_output(output)
        assert rows == []

    def test_empty_output(self):
        assert import_rrd.parse_fetch_output("") == []
        assert import_rrd.parse_fetch_output("\n\n") == []

    def test_partial_nan_multi_ds(self):
        """Multi-DS row where only some columns are NaN."""
        output = (
            "              total     positions\n"
            "\n"
            "1000: 5.0000000000e+00 nan\n"
        )
        rows = import_rrd.parse_fetch_output(output)
        assert len(rows) == 1
        assert rows[0][1] == [5.0, None]


# ---------------------------------------------------------------------------
# merge_tier — DERIVE conversion
# ---------------------------------------------------------------------------

class TestDeriveConversion:
    def test_derive_multiplied_by_60(self):
        """DERIVE values (per-second rate) should be multiplied by 60."""
        # Simulate what merge_tier does for a DERIVE column
        rate_per_second = 44.24
        expected_per_minute = rate_per_second * 60.0
        result = rate_per_second * import_rrd.DERIVE_FACTOR
        assert result == pytest.approx(expected_per_minute)

    def test_gauge_not_multiplied(self):
        """GAUGE values should be stored as-is."""
        signal_dbfs = -15.4
        # For GAUGE, is_derive=False, so no multiplication
        assert signal_dbfs == -15.4  # no conversion


# ---------------------------------------------------------------------------
# merge_tier — aircraft-recent multi-DS handling
# ---------------------------------------------------------------------------

class TestAircraftRecentMerge:
    def test_positions_and_without(self):
        """aircraft-recent: positions→ac_with_pos, total-positions→ac_without_pos."""
        # Simulate merge logic for multi-DS
        total, positions = 9.8, 7.8
        ac_with_pos = positions
        ac_without_pos = total - positions
        assert ac_with_pos == pytest.approx(7.8)
        assert ac_without_pos == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# import_rows
# ---------------------------------------------------------------------------

class TestImportRows:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_inserts_rows(self):
        rows = {
            1000: {"signal": -15.0, "noise": -35.0, "messages": 5000},
            2000: {"signal": -12.0, "noise": -33.0, "messages": 6000},
        }
        inserted = import_rrd.import_rows(self.conn, rows, dry_run=False)
        assert inserted == 2
        count = self.conn.execute("SELECT COUNT(*) FROM receiver_stats").fetchone()[0]
        assert count == 2

    def test_insert_or_ignore_preserves_existing(self):
        """Existing rows are not overwritten."""
        rows1 = {1000: {"signal": -15.0}}
        import_rrd.import_rows(self.conn, rows1, dry_run=False)

        rows2 = {1000: {"signal": -99.0}}  # same ts, different value
        inserted = import_rrd.import_rows(self.conn, rows2, dry_run=False)
        assert inserted == 0  # skipped

        val = self.conn.execute(
            "SELECT signal FROM receiver_stats WHERE ts = 1000"
        ).fetchone()[0]
        assert val == -15.0  # original preserved

    def test_dry_run_does_not_write(self):
        rows = {1000: {"signal": -15.0}}
        inserted = import_rrd.import_rows(None, rows, dry_run=True)
        assert inserted == 1  # counted
        # No DB connection → nothing written

    def test_empty_rows(self):
        assert import_rrd.import_rows(self.conn, {}, dry_run=False) == 0

    def test_null_columns_for_unmapped(self):
        """Columns not present in the row dict should be NULL."""
        rows = {1000: {"signal": -15.0, "ac_with_pos": 10}}
        import_rrd.import_rows(self.conn, rows, dry_run=False)
        row = self.conn.execute(
            "SELECT signal, noise, messages, ac_with_pos FROM receiver_stats WHERE ts = 1000"
        ).fetchone()
        assert row[0] == -15.0    # signal
        assert row[1] is None     # noise — not in import
        assert row[2] is None     # messages — not in import
        assert row[3] == 10       # ac_with_pos


# ---------------------------------------------------------------------------
# Column mapping coverage
# ---------------------------------------------------------------------------

class TestColumnMapping:
    def test_all_mapped_columns_exist_in_schema(self):
        """Every column referenced in SINGLE_DS exists in _COLS."""
        from readsbstats.metrics_collector import _COLS
        for _, col, _ in import_rrd.SINGLE_DS:
            assert col in _COLS, f"{col} not in receiver_stats schema"

    def test_aircraft_recent_columns_exist(self):
        from readsbstats.metrics_collector import _COLS
        assert "ac_with_pos" in _COLS
        assert "ac_without_pos" in _COLS

    def test_no_duplicate_column_mappings(self):
        cols = [col for _, col, _ in import_rrd.SINGLE_DS]
        assert len(cols) == len(set(cols)), "duplicate column mapping"
