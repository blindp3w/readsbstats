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


from tests._helpers import insert_position, make_db  # noqa: E402 — kept under section header


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
    pid = insert_position(conn, flight_id, ts, lat=lat, lon=lon, gs=gs,
                          source_type=source_type)
    conn.commit()
    return pid


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

    @pytest.mark.parametrize("source_type", ["adsr_icao", "adsc"])
    def test_adsr_and_adsc_use_adsb_dt_window(self, source_type):
        """adsr_icao and adsc ARE ADS-B sources (collector._is_adsb /
        posenc.is_adsb_source). A 10s-spaced deviating pair must be
        cross-validated with the 5s ADS-B window, not the 30s 'other' window.
        Regression for the `startswith("adsb")` heuristic that silently
        excluded these two non-`adsb`-prefixed types (Audit 2026-06-20)."""
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=600)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400, source_type=source_type)
        # 10s gap (≥ 5s ADS-B min, < 30s 'other' min); ~0 nm → implied ~0 kts,
        # gs=600 → deviation 600 > 100. Flagged only if the ADS-B window applies.
        pid = insert_pos(self.conn, fid, 1010, 52.0001, 21.0, gs=600, source_type=source_type)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid in bad
        assert pid in bad[fid]

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
# PERF-2 — batched aircraft_db flag lookup (no full-table preload)
# ---------------------------------------------------------------------------

class TestBatchedFlagLookup:
    """PERF-2: scan_flights must not preload the ENTIRE aircraft_db (~620k
    rows on the Pi) into a Python dict. It only needs the flags for the
    handful of ICAOs that actually have flights, looked up in batches.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_resolves_flags_without_loading_unrelated_rows(self):
        """Flags still resolve correctly even when aircraft_db holds many
        rows that no flight references — those extra rows must NOT be loaded.

        Correctness proof: the military aircraft (flags=1) keeps gs=1000
        (under the 1800 military limit) while the civil one (flags=0) is
        flagged at gs=1000 (over the 750 civil limit). If the batched
        lookup pulled the wrong flags, these would flip.
        """
        # Two aircraft that DO have flights.
        insert_aircraft_db(self.conn, "aabbcc", flags=1)   # military
        insert_aircraft_db(self.conn, "ddeeff", flags=0)   # civil
        # 50 unrelated aircraft_db rows that must never be loaded.
        for i in range(50):
            insert_aircraft_db(self.conn, f"c{i:05x}", flags=i % 2)

        fmil = insert_flight(self.conn, icao="aabbcc", max_gs=1000)
        insert_pos(self.conn, fmil, 1000, 52.0, 21.0, gs=1000)  # 1000 < 1800 → OK
        fciv = insert_flight(self.conn, icao="ddeeff", max_gs=1000)
        pciv = insert_pos(self.conn, fciv, 2000, 52.0, 21.0, gs=1000)  # 1000 > 750 → bad

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fmil not in bad           # military limit applied
        assert fciv in bad and pciv in bad[fciv]   # civil limit applied

    def test_missing_icao_falls_back_to_military_limit(self):
        """An ICAO absent from aircraft_db keeps the same permissive
        (military/unknown) default as before — gs=1000 stays under 1800."""
        # aircraft_db has rows, but none for the flight's ICAO.
        for i in range(10):
            insert_aircraft_db(self.conn, f"b{i:05x}", flags=0)
        fid = insert_flight(self.conn, icao="ffffff", max_gs=1000)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=1000)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert fid not in bad

    def test_does_not_full_scan_aircraft_db(self):
        """Spy on execute(): aircraft_db must only be queried WITH a WHERE
        clause (batched IN-list), never as a bare full-table SELECT."""
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        for i in range(20):
            insert_aircraft_db(self.conn, f"c{i:05x}", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)

        captured: list[str] = []

        class _SpyConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kw):
                captured.append(sql)
                return self._inner.execute(sql, *args, **kw)

        scan_flights(_SpyConn(self.conn), CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)

        aircraft_db_queries = [s for s in captured if "aircraft_db" in s]
        assert aircraft_db_queries, "expected at least one aircraft_db lookup"
        for sql in aircraft_db_queries:
            assert "WHERE" in sql.upper(), (
                f"aircraft_db queried without a WHERE clause (full-table "
                f"preload regressed): {sql!r}"
            )

    def test_more_icaos_than_one_batch(self):
        """Correctness across the chunk boundary: more distinct flight ICAOs
        than the batch size still resolve their flags correctly."""
        from purge_bad_gs import _AIRCRAFT_DB_CHUNK

        n = _AIRCRAFT_DB_CHUNK + 7
        expect_bad = set()
        for i in range(n):
            icao = f"e{i:05x}"
            # Alternate civil/military so flags must be resolved per-icao.
            flags = 0 if i % 2 == 0 else 1
            insert_aircraft_db(self.conn, icao, flags=flags)
            fid = insert_flight(self.conn, icao=icao, max_gs=1000)
            insert_pos(self.conn, fid, 1000 + i, 52.0, 21.0, gs=1000)
            if flags == 0:        # civil → 1000 > 750 → flagged
                expect_bad.add(fid)

        bad = scan_flights(self.conn, CIVIL_LIMIT, MILITARY_LIMIT, DEVIATION)
        assert set(bad.keys()) == expect_bad


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

    def test_empty_bad_ids_does_not_raise(self):
        """Regression for audit-12 #164 — empty bad_ids must not produce
        `id NOT IN ()` SQL syntax error."""
        fid = insert_flight(self.conn)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=400)
        insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=450)

        result = _new_max_gs(self.conn, fid, [])
        assert result == 450.0

    def test_empty_bad_ids_with_no_positions_returns_none(self):
        fid = insert_flight(self.conn)
        result = _new_max_gs(self.conn, fid, [])
        assert result is None


# ---------------------------------------------------------------------------
# apply_purge — batched commits (audit-12 #P3.2)
# ---------------------------------------------------------------------------

class TestApplyPurgeBatching:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_apply_purge_batches_commits(self):
        """Apply per-batch commits so a multi-thousand-flight purge doesn't
        hold the write lock for the whole run (collector starvation)."""
        from purge_bad_gs import _BATCH_SIZE
        from tests._helpers import CountingConn as _CountingConn

        bad: dict[int, list[int]] = {}
        n_flights = _BATCH_SIZE * 2 + 5
        for i in range(n_flights):
            fid = insert_flight(self.conn, icao=f"a{i:05x}")
            insert_pos(self.conn, fid, 1000 + i, 52.0, 21.0, gs=400)
            bad_pid = insert_pos(self.conn, fid, 1060 + i, 52.1, 21.0, gs=900)
            bad[fid] = [bad_pid]

        counter = _CountingConn(self.conn)
        apply_purge(counter, bad)
        assert counter.commits >= 3, (
            f"expected ≥3 commits for {n_flights} flights at batch={_BATCH_SIZE},"
            f" got {counter.commits}"
        )
        # All bad gs values nulled (correctness preserved)
        for fid, ids in bad.items():
            placeholders = ",".join("?" * len(ids))
            null_count = self.conn.execute(
                f"SELECT COUNT(*) FROM positions WHERE id IN ({placeholders}) AND gs IS NULL",
                ids,
            ).fetchone()[0]
            assert null_count == len(ids)


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
            "SELECT gs / 10.0 FROM positions WHERE id = ?", (good_pid,)
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
            "SELECT gs / 10.0 FROM positions WHERE flight_id = ?", (fid,)
        ).fetchone()
        assert row[0] == 800

    def test_apply_modifies_data(self, monkeypatch, capsys):
        insert_aircraft_db(self.conn, "aabbcc", flags=0)
        fid = insert_flight(self.conn, icao="aabbcc", max_gs=800)
        insert_pos(self.conn, fid, 1000, 52.0, 21.0, gs=800)
        insert_pos(self.conn, fid, 1060, 52.1, 21.0, gs=400)
        self.conn.commit()

        monkeypatch.setattr("sys.argv", [
            "purge_bad_gs.py", "--db", self.db_path,
            "--apply", "--i-have-a-backup",
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


# ---------------------------------------------------------------------------
# Audit 2026-05-26: NULL-coordinate defence
# ---------------------------------------------------------------------------


class TestNullCoordinateGuard:
    """purge_bad_gs's cross-validation step calls haversine_nm with the
    previous and current positions. NULL lat/lon would crash via
    math.radians(None) — must skip cleanly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_scan_flights_skips_null_coord_rows(self):
        fid = insert_flight(self.conn)
        # Row with NULL coords but a valid gs value — cross-validation
        # against the previous row would have crashed.
        insert_position(self.conn, fid, 1500, lat=None, lon=None, gs=400.0,
                        source_type="adsb_icao")
        # Bracketing valid positions
        insert_pos(self.conn, fid, 1000, 52.6,  20.75, 400.0, "adsb_icao")
        insert_pos(self.conn, fid, 2000, 52.65, 20.80, 400.0, "adsb_icao")
        self.conn.commit()

        # Before the fix: TypeError. After: the NULL row is skipped, no
        # bad_ids reported because gs values are within the civil limit.
        result = scan_flights(
            self.conn, civil_limit=700, military_limit=1500, deviation=200,
        )
        # No flagged positions (slow enough to be plausible)
        assert result == {}


# ---------------------------------------------------------------------------
# Audit-13 A13-031 — `gs IS NOT NULL` SELECT filter, not the Python guard
#
# The audit flagged "scan_flights advances `prev` on gs=None positions
# regardless of geographic plausibility" (purge_bad_gs.py:119-121).
# Round-2 triage found this concern is moot: the SELECT at line 93
# already filters `WHERE gs IS NOT NULL`, so the cursor never yields a
# gs=None row. The Python `if gs is None: prev = pos; continue` block
# is defence-in-depth dead code — kept in case someone loosens the
# SELECT, but never exercised in practice.
#
# This regression test pins the SELECT filter, which is the actual
# guarantee. If a future refactor removes `WHERE gs IS NOT NULL` from
# the cursor, gs=None rows would reach the Python guard and the audit's
# hypothetical concern would become live — at which point we'd need to
# revisit the per-row branch. The test fails loudly if that happens.
# ---------------------------------------------------------------------------

class TestScanFiltersGsNoneAtSql:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_gs_none_rows_excluded_by_select(self):
        """Insert a flight with two valid gs positions plus one gs=NULL
        position between them at a wildly-implausible location. If the
        SELECT filter is ever removed, the next valid gs's
        cross-validation reference would shift to the gs=NULL row's
        coords and this test would fail.
        """
        fid = insert_flight(self.conn)
        # Position A: valid gs, at origin.
        insert_pos(self.conn, fid, 1000, 52.00, 21.0, gs=400.0, source_type="adsb_icao")
        # Position B: gs=NULL, claims to be 1° north (huge jump). If the
        # SELECT filter is removed, B becomes the cross-validation
        # reference for C below.
        insert_position(self.conn, fid, 1030, lat=53.0, lon=21.0, gs=None,
                        source_type="adsb_icao")
        # Position C: valid gs, ~6.67 nm north of A (400 kts × 60 s).
        # 0.111° of latitude = 6.67 nm — within deviation tolerance.
        insert_pos(self.conn, fid, 1060, 52.111, 21.0, gs=400.0, source_type="adsb_icao")
        self.conn.commit()

        import purge_bad_gs as pbg
        recorded: list[tuple] = []
        orig = pbg.haversine_nm

        def spy(plat, plon, lat, lon):
            recorded.append((plat, plon, lat, lon))
            return orig(plat, plon, lat, lon)

        pbg.haversine_nm = spy
        try:
            bad = scan_flights(
                self.conn,
                civil_limit=CIVIL_LIMIT,
                military_limit=MILITARY_LIMIT,
                deviation=DEVIATION,
            )
        finally:
            pbg.haversine_nm = orig

        # Sanity: nothing flagged — A→C is plausible.
        assert bad == {}

        # The cross-validation reference must never be B (lat=53).
        # If `WHERE gs IS NOT NULL` is ever removed from the SELECT,
        # B leaks in and this assertion fires.
        b_used_as_reference = [
            (plat, plon) for plat, plon, _, _ in recorded if plat == 53.0
        ]
        assert b_used_as_reference == [], (
            f"gs=NULL row leaked through the SELECT filter into the "
            f"cross-validation reference: {b_used_as_reference}"
        )
