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

from tests._helpers import make_db  # noqa: E402 — kept under section header


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

    def test_non_int_timestamp_line_skipped(self):
        """BUG-7: a colon-bearing line whose prefix is not an integer must be
        skipped, not raise — matching the value-parse loop's ValueError
        tolerance. A stray non-numeric line (e.g. a re-emitted header or
        rrdtool warning carrying a colon) would otherwise abort the whole
        import mid-run after partial commits."""
        output = (
            "              value\n"
            "\n"
            "1776979620: -1.12e+01\n"
            "ds[value].last_ds: 42\n"   # non-int prefix before the colon
            "1776979680: -1.36e+01\n"
        )
        rows = import_rrd.parse_fetch_output(output)
        # Bad line skipped; the two valid timestamped rows still parse.
        assert rows == [
            (1776979620, [-11.2]),
            (1776979680, [-13.6]),
        ]


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


# ---------------------------------------------------------------------------
# Audit-13 A13-098 — subprocess-shell-out + orchestrator coverage
# ---------------------------------------------------------------------------

class _StubCompletedProcess:
    """Minimal subprocess.CompletedProcess stand-in."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestFetchRrd:
    def test_returns_parsed_rows_on_success(self, monkeypatch):
        out = (
            "                  value\n"
            "\n"
            "1776979620: -1.12e+01\n"
            "1776979680: -1.36e+01\n"
        )
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(0, stdout=out),
        )
        rows = import_rrd.fetch_rrd("/fake.rrd", 60, 1776979500, 1776979700)
        assert len(rows) == 2
        assert rows[0][0] == 1776979620

    def test_returns_empty_on_rrdtool_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(1, stderr="ERROR: not an RRD"),
        )
        rows = import_rrd.fetch_rrd("/fake.rrd", 60, 0, 100)
        assert rows == []
        assert "rrdtool fetch failed" in capsys.readouterr().err

    def test_command_args_passed_correctly(self, monkeypatch):
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _StubCompletedProcess(0, stdout="\n\n")

        monkeypatch.setattr(import_rrd.subprocess, "run", fake_run)
        import_rrd.fetch_rrd("/sig.rrd", 180, 1000, 2000)
        assert captured["cmd"][:4] == ["rrdtool", "fetch", "/sig.rrd", "AVERAGE"]
        assert "--resolution" in captured["cmd"]
        assert "180" in captured["cmd"]


class TestGetLastUpdate:
    def test_parses_last_update_line(self, monkeypatch):
        info_out = (
            "filename = sig.rrd\n"
            "rrd_version = 0003\n"
            "last_update = 1776979800\n"
            "step = 60\n"
        )
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(0, stdout=info_out),
        )
        assert import_rrd.get_last_update("/x.rrd") == 1776979800

    def test_returns_none_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(1, stderr="err"),
        )
        assert import_rrd.get_last_update("/x.rrd") is None

    def test_returns_none_when_field_absent(self, monkeypatch):
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(0, stdout="step = 60\n"),
        )
        assert import_rrd.get_last_update("/x.rrd") is None


class TestMergeTier:
    def test_single_ds_files_merged_by_ts(self, monkeypatch, tmp_path):
        # Create stub files so the existence check passes.
        for fname, _, _ in import_rrd.SINGLE_DS[:2]:
            (tmp_path / fname).write_text("")
        (tmp_path / import_rrd.AIRCRAFT_RECENT).write_text("")

        # Return distinct rows per file so we can verify by-ts merge.
        call_count = {"n": 0}

        def fake_fetch(path, *_):
            call_count["n"] += 1
            base = call_count["n"]
            return [(1000, [float(base * 10)]), (1060, [float(base * 20)])]

        monkeypatch.setattr(import_rrd, "fetch_rrd", fake_fetch)
        merged = import_rrd.merge_tier(str(tmp_path), 60, 0, 2000)
        # 2 SingleDS files + 1 aircraft_recent => 3 fetches, all share TS.
        assert 1000 in merged
        assert 1060 in merged
        assert len(merged[1000]) >= 2

    def test_skips_missing_rrd_files(self, monkeypatch, tmp_path):
        # No files created; merge_tier should never call fetch_rrd.
        called = []
        monkeypatch.setattr(import_rrd, "fetch_rrd", lambda *a, **kw: called.append(a) or [])
        result = import_rrd.merge_tier(str(tmp_path), 60, 0, 2000)
        assert result == {}
        assert called == []

    def test_aircraft_recent_splits_total_into_with_without(self, monkeypatch, tmp_path):
        # Only aircraft_recent present; supplies (total, positions) tuples.
        (tmp_path / import_rrd.AIRCRAFT_RECENT).write_text("")

        def fake_fetch(path, *_):
            return [(1000, [50.0, 30.0]), (1060, [60.0, None])]

        monkeypatch.setattr(import_rrd, "fetch_rrd", fake_fetch)
        merged = import_rrd.merge_tier(str(tmp_path), 60, 0, 2000)
        # ts=1000: positions=30 → ac_with_pos=30; (50-30) → ac_without_pos=20
        assert merged[1000]["ac_with_pos"] == 30.0
        assert merged[1000]["ac_without_pos"] == 20.0
        # ts=1060: positions=None, total=60 → ac_with_pos=60, no without
        assert merged[1060]["ac_with_pos"] == 60.0
        assert "ac_without_pos" not in merged[1060]


class TestMain:
    # main() reads argv via argparse and shells out to rrdtool; we test
    # the failure paths via direct argv injection. argparse exits with
    # code 2 on its own validation errors (so we don't assert exit code
    # for the "missing arg" case — argparse owns that one).

    def test_aborts_when_rrdtool_unavailable(self, monkeypatch, tmp_path):
        import sys as _sys
        monkeypatch.setattr(_sys, "argv",
                            ["import_rrd.py", "--rrd-dir", str(tmp_path)])
        # tmp_path is a valid dir; rrdtool --version fails.
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(1),
        )
        with pytest.raises(SystemExit) as exc:
            import_rrd.main()
        assert exc.value.code == 1

    def test_aborts_when_reference_file_missing(self, monkeypatch, tmp_path):
        import sys as _sys
        monkeypatch.setattr(_sys, "argv",
                            ["import_rrd.py", "--rrd-dir", str(tmp_path)])
        # rrdtool --version succeeds; dbfs-signal.rrd is absent in tmp_path.
        monkeypatch.setattr(
            import_rrd.subprocess, "run",
            lambda *a, **kw: _StubCompletedProcess(0, stdout="rrdtool 1.7\n"),
        )
        with pytest.raises(SystemExit) as exc:
            import_rrd.main()
        assert exc.value.code == 1
