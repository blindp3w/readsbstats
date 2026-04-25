"""Tests for purge_bad_gs.py."""

import os
import sqlite3
import tempfile

import pytest

from readsbstats import database
from purge_bad_gs import (
    _is_military,
    _new_max_gs,
    apply_purge,
    main,
    scan_flights,
    _MIN_DT_ADSB,
    _MIN_DT_OTHER,
    _MAX_DT,
    _MIN_GS_XVAL,
)


def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def make_file_db(path: str) -> sqlite3.Connection:
    conn = database.connect(path)
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def insert_flight(conn, icao="aabbcc", max_gs=500.0, callsign=None, registration=None) -> int:
    cur = conn.execute(
        "INSERT INTO flights (icao_hex, callsign, registration, first_seen, last_seen, max_gs) "
        "VALUES (?, ?, ?, 1000, 9000, ?)",
        (icao, callsign, registration, max_gs),
    )
    conn.commit()
    return cur.lastrowid


def insert_pos(conn, flight_id, ts, lat, lon, gs, source_type="adsb_icao") -> int:
    cur = conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, gs, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (flight_id, ts, lat, lon, gs, source_type),
    )
    conn.commit()
    return cur.lastrowid


def insert_aircraft_db(conn, icao, flags=0):
    conn.execute(
        "INSERT OR REPLACE INTO aircraft_db (icao_hex, flags) VALUES (?, ?)",
        (icao, flags),
    )
    conn.commit()


# Default thresholds matching CLI defaults
CIVIL_LIMIT = 750
MILITARY_LIMIT = 1800
DEVIATION = 100


# ---------------------------------------------------------------------------
# _is_military
# ---------------------------------------------------------------------------

class TestIsMilitary:
    def test_military_flag_set(self):
        assert _is_military(1) is True

    def test_military_with_other_flags(self):
        assert _is_military(0b0111) is True  # military + interesting + PIA

    def test_civil_no_flags(self):
        assert _is_military(0) is False

    def test_civil_interesting_only(self):
        assert _is_military(2) is False  # interesting flag, not military


# ---------------------------------------------------------------------------
# scan_flights — hard-limit check
# ---------------------------------------------------------------------------

class TestScanHardLimit:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_civil_gs_over_limit_flagged(self):
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        pid = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_civil_gs_at_limit_not_flagged(self):
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=CIVIL_LIMIT)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=CIVIL_LIMIT)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_military_uses_higher_limit(self):
        insert_aircraft_db(self.conn, "aabbcc", flags=1)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=1000)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=1000)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad  # 1000 < 1800

    def test_military_gs_over_limit_flagged(self):
        insert_aircraft_db(self.conn, "aabbcc", flags=1)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=2000)
        pid = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=2000)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_unknown_aircraft_uses_military_limit(self):
        """Aircraft not in aircraft_db gets the military (permissive) limit."""
        fid = insert_flight(self.conn, icao="ffffff", max_gs=1000)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=1000)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad  # 1000 < 1800

    def test_multiple_bad_positions_in_one_flight(self):
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=900)
        pid1 = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=500)
        pid3 = insert_pos(self.conn, fid, 1120, 52.2, 21.0, gs=900)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert set(bad[fid]) == {pid1, pid3}


# ---------------------------------------------------------------------------
# scan_flights — cross-validation check
# ---------------------------------------------------------------------------

class TestScanCrossValidation:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        yield
        self.conn.close()

    def test_gs_deviates_from_implied_speed_flagged(self):
        """Report gs=600 but positions imply ~120 kts → deviation > 100 → flagged."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        # ~2 nm apart, 60s gap → implied ~120 kts
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        pid = insert_pos(self.conn, fid, 1060, 52.03, 21.0, gs=600)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_gs_consistent_with_implied_not_flagged(self):
        """Reported gs close to implied → no flag."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=450)
        # ~7.5 nm apart, 60s → implied ~450 kts
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=450)
        insert_pos(self.conn, fid, 1060, 52.125, 21.0, gs=450)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_slow_aircraft_skips_crossval(self):
        """gs < _MIN_GS_XVAL (300) skips cross-validation entirely."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=250)
        # Positions very close (~0 nm implied) but gs=250 → deviation would be huge,
        # but cross-val is skipped for gs < 300.
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=250)
        insert_pos(self.conn, fid, 1060, 52.0001, 21.0, gs=250)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_dt_below_min_adsb_skips_crossval(self):
        """dt < _MIN_DT_ADSB → cross-validation skipped."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        # 3s gap (< 5s min for adsb), reported gs inconsistent but dt too short
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        insert_pos(self.conn, fid, 1003, 52.0001, 21.0, gs=600, source_type="adsb_icao")

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_dt_at_exact_min_adsb_crossval_applies(self):
        """dt == _MIN_DT_ADSB (5s) → cross-validation active."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        # 5s gap, ~0nm apart → implied ~0 kts, gs=600 → deviation=600
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        pid = insert_pos(self.conn, fid, 1005, 52.0001, 21.0, gs=600)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_dt_at_exact_max_crossval_applies(self):
        """dt == _MAX_DT (120s) → cross-validation still active."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        pid = insert_pos(self.conn, fid, 1120, 52.0001, 21.0, gs=600)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_dt_above_max_skips_crossval(self):
        """dt > _MAX_DT → cross-validation skipped (gap too long)."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        insert_pos(self.conn, fid, 1200, 52.0001, 21.0, gs=600)  # 200s > 120s

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_mlat_uses_higher_min_dt(self):
        """MLAT positions need _MIN_DT_OTHER (30s) — 10s gap skips cross-val."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400, source_type="mlat")
        insert_pos(self.conn, fid, 1010, 52.0001, 21.0, gs=600, source_type="mlat")

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad  # 10s < 30s min for mlat

    def test_mlat_at_valid_dt_crossval_applied(self):
        """MLAT at dt=60s → cross-validation runs normally."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400, source_type="mlat")
        pid = insert_pos(self.conn, fid, 1060, 52.0001, 21.0, gs=600, source_type="mlat")

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_bad_position_does_not_advance_prev_reference(self):
        """After a flagged position, prev stays at last good — prevents cascade."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        # p1: good reference at (52.0, 21.0), gs=400
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        # p2: hard-limit violation
        pid2 = insert_pos(self.conn, fid, 1060, 52.125, 21.0, gs=800)
        # p3: if prev advanced to p2, cross-val would use p2's coords.
        # We use gs < _MIN_GS_XVAL to skip cross-val, proving only p2 is bad.
        insert_pos(self.conn, fid, 1120, 52.125, 21.0, gs=250)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert bad[fid] == [pid2]

    def test_null_gs_position_updates_prev_reference(self):
        """Null-gs position becomes prev; next position's cross-val uses it."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=450)
        # p1: gs=None → sets prev to (52.0, 21.0) at ts=1000
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=None)
        # p2: ~7.5nm from p1 in 60s → implied ~450 kts, gs=450 → deviation ~0 → OK
        insert_pos(self.conn, fid, 1060, 52.125, 21.0, gs=450)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_only_null_gs_positions_not_scanned(self):
        """Flight with only NULL gs positions is not in the scan set at all."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=None)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=None)
        insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=None)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert bad == {}

    def test_no_positions_returns_empty(self):
        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert bad == {}

    def test_null_source_type_treated_as_non_adsb(self):
        """Null source_type → not adsb → uses _MIN_DT_OTHER for cross-val."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400, source_type=None)
        # 10s gap < 30s min for non-adsb → skip cross-val
        insert_pos(self.conn, fid, 1010, 52.0001, 21.0, gs=600, source_type=None)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_gs_at_exact_min_xval_threshold_crossval_applies(self):
        """gs == _MIN_GS_XVAL (300) → cross-validation runs."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=300)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        # ~0 nm in 60s, gs=300, deviation = |300 - ~0| = 300 > 100
        pid = insert_pos(self.conn, fid, 1060, 52.0001, 21.0, gs=300)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

    def test_gs_just_below_min_xval_skips_crossval(self):
        """gs == 299 (< _MIN_GS_XVAL) → cross-validation skipped."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=299)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=250)
        insert_pos(self.conn, fid, 1060, 52.0001, 21.0, gs=299)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad


# ---------------------------------------------------------------------------
# scan_flights — edge cases
# ---------------------------------------------------------------------------

class TestScanEdgeCases:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_multiple_flights_independent(self):
        """Bad gs in one flight doesn't affect another."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        insert_aircraft_db(self.conn, "ddeeff", flags=0)

        fid1 = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        pid1 = insert_pos(self.conn, fid1, 1000, 52.0, 21.0, gs=800)

        fid2 = insert_flight(self.conn, icao="ddeeff", max_gs=500)
        insert_pos(self.conn, fid2, 2000, 52.0, 21.0, gs=500)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid1 in bad
        assert pid1 in bad[fid1]
        assert fid2 not in bad

    def test_single_position_only_hard_limit(self):
        """Single position — no prev for cross-val, only hard-limit applies."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=400)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_all_positions_flagged(self):
        """Every position exceeds hard limit — all flagged."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=900)
        pid1 = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        pid2 = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert set(bad[fid]) == {pid1, pid2}


# ---------------------------------------------------------------------------
# _new_max_gs
# ---------------------------------------------------------------------------

class TestNewMaxGs:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_excludes_bad_ids(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        bad_pid = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)
        insert_pos(self.conn, fid, 1120, 52.2, 21.0, gs=450)

        result = _new_max_gs(self.conn, fid, [bad_pid])
        assert result == 450.0

    def test_all_positions_bad_returns_none(self):
        fid = insert_flight(self.conn)
        pid1 = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=900)
        pid2 = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=800)

        result = _new_max_gs(self.conn, fid, [pid1, pid2])
        assert result is None

    def test_mixed_null_and_valid_gs(self):
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=None)
        bad_pid = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)
        insert_pos(self.conn, fid, 1120, 52.2, 21.0, gs=350)

        result = _new_max_gs(self.conn, fid, [bad_pid])
        assert result == 350.0


# ---------------------------------------------------------------------------
# apply_purge
# ---------------------------------------------------------------------------

class TestApplyPurge:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_nulls_gs_for_bad_positions(self):
        fid = insert_flight(self.conn, max_gs=900)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        bad_pid = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)

        apply_purge(self.conn, {fid: [bad_pid]})

        row = self.conn.execute(
            "SELECT gs FROM positions WHERE id = ?", (bad_pid,)
        ).fetchone()
        assert row[0] is None

    def test_preserves_good_gs(self):
        fid = insert_flight(self.conn, max_gs=900)
        good_pid = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        bad_pid = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)

        apply_purge(self.conn, {fid: [bad_pid]})

        row = self.conn.execute(
            "SELECT gs FROM positions WHERE id = ?", (good_pid,)
        ).fetchone()
        assert row[0] == 400

    def test_updates_max_gs_in_flights(self):
        fid = insert_flight(self.conn, max_gs=900)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        bad_pid = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)

        apply_purge(self.conn, {fid: [bad_pid]})

        row = self.conn.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] == 400.0

    def test_max_gs_null_when_all_bad(self):
        fid = insert_flight(self.conn, max_gs=900)
        pid1 = insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        pid2 = insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=900)

        apply_purge(self.conn, {fid: [pid1, pid2]})

        row = self.conn.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] is None

    def test_empty_dict_is_noop(self):
        fid = insert_flight(self.conn, max_gs=500)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=500)

        apply_purge(self.conn, {})

        row = self.conn.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] == 500.0

    def test_multiple_flights_purged(self):
        fid1 = insert_flight(self.conn, icao="aabbcc", max_gs=900)
        bad1 = insert_pos(self.conn, fid1, 1000, 52.0, 21.0, gs=900)
        insert_pos(self.conn, fid1, 1060, 52.1, 21.0, gs=300)

        fid2 = insert_flight(self.conn, icao="ddeeff", max_gs=800)
        bad2 = insert_pos(self.conn, fid2, 2000, 52.0, 21.0, gs=800)
        insert_pos(self.conn, fid2, 2060, 52.1, 21.0, gs=250)

        apply_purge(self.conn, {fid1: [bad1], fid2: [bad2]})

        max1 = self.conn.execute("SELECT max_gs FROM flights WHERE id = ?", (fid1,)).fetchone()[0]
        max2 = self.conn.execute("SELECT max_gs FROM flights WHERE id = ?", (fid2,)).fetchone()[0]
        assert max1 == 300.0
        assert max2 == 250.0


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMain:
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
        os.rmdir(self.tmpdir)

    def test_dry_run_does_not_modify(self, monkeypatch, capsys):
        """Default (no --apply) prints report but doesn't change data."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800,
                            callsign="TEST1", registration="SP-AAA")
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path,
        ])
        main()

        out = capsys.readouterr().out
        assert "dry-run" in out.lower() or "Dry-run" in out
        assert "1 position" in out

        # Data unchanged
        row = self.conn.execute(
            "SELECT gs FROM positions WHERE flight_id = ?", (fid,)
        ).fetchone()
        assert row[0] == 800

    def test_apply_modifies_data(self, monkeypatch, capsys):
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=400)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path, "--apply",
        ])
        main()

        out = capsys.readouterr().out
        assert "Done" in out

        # Re-read — main() opened its own connection, so re-query
        check = sqlite3.connect(self.db_path)
        check.row_factory = sqlite3.Row
        max_gs = check.execute("SELECT max_gs FROM flights WHERE id = ?", (fid,)).fetchone()[0]
        check.close()
        assert max_gs == 400.0

    def test_no_bad_gs_prints_clean(self, monkeypatch, capsys):
        """When no implausible gs found, prints a clean message."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=400)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path,
        ])
        main()

        out = capsys.readouterr().out
        assert "No implausible" in out

    def test_custom_thresholds(self, monkeypatch, capsys):
        """Custom --civil-limit allows previously-bad gs to pass."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path, "--civil-limit", "900",
        ])
        main()

        out = capsys.readouterr().out
        assert "No implausible" in out

    def test_report_shows_flight_label(self, monkeypatch, capsys):
        """Dry-run report includes callsign/registration for identification."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800,
                            callsign="LOT123", registration="SP-LRA")
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path,
        ])
        main()

        out = capsys.readouterr().out
        assert "LOT123" in out
        assert "SP-LRA" in out
