"""Parity + behaviour tests for the DuckDB analytics module.

DuckDB's `sqlite_scanner` reads via the SQLite VFS and cannot attach to
`:memory:` SQLite databases, so this file uses a file-backed `tmp_path`
fixture instead of the shared in-memory fixture used by `test_web.py`.
The tmp file is wiped between tests via `analytics._reset_for_tests()`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from readsbstats import analytics, config, database, web
from readsbstats import cache
from readsbstats.api import _deps
from readsbstats.api import map as api_map


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_file_db(path: Path):
    conn = database.connect(str(path))
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def _insert_flight(conn) -> int:
    cur = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, first_seen, last_seen, total_positions, primary_source,
             lat_min, lat_max, lon_min, lon_max)
        VALUES ('aabbcc', 'TEST', 1000, 2000, 0, 'adsb', 0,0,0,0)
        """
    )
    conn.commit()
    return cur.lastrowid


def _insert_position(conn, flight_id: int, *, lat: float, lon: float, ts: int) -> None:
    conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, source_type) VALUES (?,?,?,?,?)",
        (flight_id, ts, lat, lon, "adsb_icao"),
    )
    conn.commit()


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """File-backed history.db (DuckDB can attach to it) + matching `_deps._db`
    + matching `config.DB_PATH`. The `analytics` module is reset before
    AND after each test so cross-test state never leaks."""
    db_file = tmp_path / "history.db"
    conn = _make_file_db(db_file)

    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(_deps, "_db", conn)
    monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", str(tmp_path / "duckdb-tmp"))
    monkeypatch.setattr(config, "DUCKDB_HOME_DIR", str(tmp_path / "duckdb-home"))
    cache._cache.clear()
    analytics._reset_for_tests()

    yield conn

    conn.close()
    analytics._reset_for_tests()


def _run_heatmap_with_engine(window: str, *, monkeypatch, use_duckdb: bool):
    """Force a specific engine via the config flag, clear the cache, run."""
    monkeypatch.setattr(config, "USE_DUCKDB", use_duckdb)
    cache._cache.clear()
    analytics._reset_for_tests()
    return api_map._compute_heatmap_sync(window)


def _run_coverage_with_engine(window: str, *, monkeypatch, use_duckdb: bool):
    monkeypatch.setattr(config, "USE_DUCKDB", use_duckdb)
    cache._cache.clear()
    analytics._reset_for_tests()
    return api_map._compute_coverage_sync(window)


# ---------------------------------------------------------------------------
# 1. Heatmap parity — 24h fine grid (precision=2, 0.01°)
# ---------------------------------------------------------------------------


def test_heatmap_parity_24h_fine_grid(file_db, monkeypatch):
    now = int(time.time())
    fid = _insert_flight(file_db)
    cells = [
        (52.10, 21.00, 3),
        (52.11, 21.00, 1),
        (52.10, 21.01, 2),
        (52.20, 21.10, 5),
        (52.30, 21.20, 1),
    ]
    for lat, lon, n in cells:
        for _ in range(n):
            _insert_position(file_db, fid, lat=lat, lon=lon, ts=now)

    sqlite_result = _run_heatmap_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=False)
    duck_result = _run_heatmap_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=True)

    s_points = sorted([(round(p[0], 6), round(p[1], 6), p[2]) for p in sqlite_result["points"]])
    d_points = sorted([(round(p[0], 6), round(p[1], 6), p[2]) for p in duck_result["points"]])

    assert len(s_points) == len(d_points) == 5
    assert sqlite_result["count"] == duck_result["count"] == 12
    for s, d in zip(s_points, d_points):
        assert s[0] == pytest.approx(d[0], abs=1e-6)
        assert s[1] == pytest.approx(d[1], abs=1e-6)
        assert s[2] == pytest.approx(d[2], rel=1e-9)


# ---------------------------------------------------------------------------
# 2. Heatmap parity — window=all (cutoff_ts=None, primary failure mode)
# ---------------------------------------------------------------------------


def test_heatmap_parity_window_all(file_db, monkeypatch):
    """cutoff_ts=None path — the primary failure mode (window=all is the
    case that times out under SQLite). The `all` window uses precision=1
    (coarse 0.1° grid); coords are picked so each lands clearly inside a
    distinct cell, no boundary-rounding ambiguity between engines."""
    now = int(time.time())
    fid = _insert_flight(file_db)
    # Three coords each in their own distinct 0.1° cell.
    fixed_coords = [(52.13, 21.04), (52.43, 21.34), (52.73, 21.64)]
    for ts, (lat, lon) in zip((now, now - 31 * 86400, now - 120 * 86400), fixed_coords):
        _insert_position(file_db, fid, lat=lat, lon=lon, ts=ts)

    sqlite_result = _run_heatmap_with_engine("all", monkeypatch=monkeypatch, use_duckdb=False)
    duck_result = _run_heatmap_with_engine("all", monkeypatch=monkeypatch, use_duckdb=True)

    assert sqlite_result["count"] == 3
    assert duck_result["count"] == 3
    assert len(sqlite_result["points"]) == len(duck_result["points"]) == 3


# ---------------------------------------------------------------------------
# 3. Heatmap parity — 30d coarse grid (precision=1)
# ---------------------------------------------------------------------------


def test_heatmap_parity_30d_coarse_grid(file_db, monkeypatch):
    """Coarse 0.1° grid. Coordinates are deliberately mid-cell (NOT on a
    half-rounding boundary like 52.15) so `round()`'s banker's-vs-half-up
    divergence between engines can't fork bucketing."""
    now = int(time.time())
    fid = _insert_flight(file_db)
    for ts in (now - 5 * 86400, now - 10 * 86400, now - 20 * 86400):
        _insert_position(file_db, fid, lat=52.13, lon=21.04, ts=ts)
    _insert_position(file_db, fid, lat=52.22, lon=21.11, ts=now - 28 * 86400)
    _insert_position(file_db, fid, lat=52.13, lon=21.04, ts=now - 31 * 86400)

    sqlite_result = _run_heatmap_with_engine("30d", monkeypatch=monkeypatch, use_duckdb=False)
    duck_result = _run_heatmap_with_engine("30d", monkeypatch=monkeypatch, use_duckdb=True)

    assert sqlite_result["count"] == duck_result["count"] == 4
    s_cells = {(round(p[0], 1), round(p[1], 1)) for p in sqlite_result["points"]}
    d_cells = {(round(p[0], 1), round(p[1], 1)) for p in duck_result["points"]}
    assert s_cells == d_cells


def test_heatmap_parity_at_exact_half_decimal_boundary(file_db, monkeypatch):
    """improvements.md A13-019 — coords sitting exactly on the half-decimal
    rounding boundary (lat=52.05, precision=1) used to land in different
    buckets across engines: SQLite's round() is half-away-from-zero
    (→ 52.1), DuckDB's is banker's (→ 52.0).  The explicit FLOOR(x*10+0.5)
    pattern in both heatmap paths must now yield the same bucket."""
    now = int(time.time())
    fid = _insert_flight(file_db)
    # 52.05 / 21.05 — every digit lands on a 0.1° boundary at precision=1.
    for _ in range(5):
        _insert_position(file_db, fid, lat=52.05, lon=21.05, ts=now)

    sqlite_result = _run_heatmap_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=False)
    duck_result   = _run_heatmap_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=True)

    assert sqlite_result["count"] == duck_result["count"] == 5
    s_cells = {(round(p[0], 6), round(p[1], 6)) for p in sqlite_result["points"]}
    d_cells = {(round(p[0], 6), round(p[1], 6)) for p in duck_result["points"]}
    assert s_cells == d_cells, (
        f"Engines disagree on bucket at exact half-decimal boundary: "
        f"SQLite={s_cells} DuckDB={d_cells}"
    )


# ---------------------------------------------------------------------------
# 4. Coverage parity — 24h window with known bearings + distances
# ---------------------------------------------------------------------------


def test_coverage_parity_24h(file_db, monkeypatch):
    """Verify both engines produce equivalent coverage polygons.

    Offsets are picked deliberately OFF cardinal bearings (N/E/S/W) so the
    `CAST(bearing/10 AS INT)` bucket boundary at every multiple of 10° can't
    fork bucketing on a ULP-level bearing difference between engines. The
    test would otherwise flap on whether due-east lands in bucket 8 vs 9."""
    now = int(time.time())
    fid = _insert_flight(file_db)
    rlat = config.RECEIVER_LAT
    rlon = config.RECEIVER_LON
    for d_lat, d_lon in [(0.10, 0.03), (0.03, 0.10), (-0.10, 0.03), (-0.03, 0.10),
                         (0.15, 0.08), (-0.15, -0.12)]:
        _insert_position(file_db, fid, lat=rlat + d_lat, lon=rlon + d_lon, ts=now)

    sqlite_result = _run_coverage_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=False)
    duck_result = _run_coverage_with_engine("24h", monkeypatch=monkeypatch, use_duckdb=True)

    assert sqlite_result["max_range_nm"] == pytest.approx(duck_result["max_range_nm"], rel=1e-6, abs=1e-4)
    assert len(sqlite_result["polygon"]) == len(duck_result["polygon"]) == 36
    for i, (s_pt, d_pt) in enumerate(zip(sqlite_result["polygon"], duck_result["polygon"])):
        assert s_pt[0] == pytest.approx(d_pt[0], abs=1e-4), f"bucket {i} lat diverged"
        assert s_pt[1] == pytest.approx(d_pt[1], abs=1e-4), f"bucket {i} lon diverged"


# ---------------------------------------------------------------------------
# 5. Empty positions table — both engines return safe defaults
# ---------------------------------------------------------------------------


def test_empty_positions_does_not_crash(file_db, monkeypatch):
    for engine in (False, True):
        h = _run_heatmap_with_engine("all", monkeypatch=monkeypatch, use_duckdb=engine)
        assert h["points"] == []
        assert h["count"] == 0

        c = _run_coverage_with_engine("all", monkeypatch=monkeypatch, use_duckdb=engine)
        assert c["max_range_nm"] == 0.0
        assert len(c["polygon"]) == 36
        for pt in c["polygon"]:
            assert pt[0] == pytest.approx(config.RECEIVER_LAT, abs=1e-9)
            assert pt[1] == pytest.approx(config.RECEIVER_LON, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. Fallback when DuckDB is unavailable / per-query failure
# ---------------------------------------------------------------------------


def test_fallback_when_duckdb_unavailable(file_db, monkeypatch):
    now = int(time.time())
    fid = _insert_flight(file_db)
    _insert_position(file_db, fid, lat=52.10, lon=21.00, ts=now)

    monkeypatch.setattr(analytics, "is_available", lambda: False)
    cache._cache.clear()
    result = api_map._compute_heatmap_sync("24h")
    assert result["count"] == 1

    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DuckDB query failure")

    monkeypatch.setattr(analytics, "heatmap", _boom)
    cache._cache.clear()
    result = api_map._compute_heatmap_sync("24h")
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# 7. is_available() must reflect config.USE_DUCKDB on every call
# ---------------------------------------------------------------------------


def _stub_init_connection() -> None:
    """Patch target: replaces _init_connection with a plain in-memory DuckDB
    connection (no sqlite_scanner) so tests that only need is_available()=True
    work offline without the extension."""
    if not analytics._DUCKDB_IMPORT_OK:
        analytics._INFRA_OK = False
        return
    try:
        import duckdb as _duckdb
        analytics._CONN = _duckdb.connect(":memory:")
        analytics._INFRA_OK = True
    except Exception:  # noqa: BLE001
        analytics._INFRA_OK = False


def test_engine_selection_via_env_no_restart(file_db, monkeypatch):
    monkeypatch.setattr(analytics, "_init_connection", _stub_init_connection)
    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()
    assert analytics.is_available() is True

    monkeypatch.setattr(config, "USE_DUCKDB", False)
    assert analytics.is_available() is False

    monkeypatch.setattr(config, "USE_DUCKDB", True)
    assert analytics.is_available() is True


# ---------------------------------------------------------------------------
# 8. Path validator rejects SQL-injection attempts in DB_PATH
# ---------------------------------------------------------------------------


def test_path_validator_rejects_quoted_db_path():
    assert analytics._is_safe_sql_path("/mnt/ext/history.db") is True
    assert analytics._is_safe_sql_path("/tmp/x';attach 'evil.db'as e;--") is False
    assert analytics._is_safe_sql_path('/tmp/x";--') is False
    assert analytics._is_safe_sql_path("/tmp/foo;DROP") is False
    assert analytics._is_safe_sql_path("/tmp/with\x00null") is False
    assert analytics._is_safe_sql_path("") is False


# ---------------------------------------------------------------------------
# 9. Engine init error branches (audit-12 #208)
# ---------------------------------------------------------------------------


def test_init_connection_unsafe_db_path_disables_engine(file_db, monkeypatch, caplog):
    """A DB_PATH containing characters that can't be safely embedded in SQL
    must short-circuit init and flip _INFRA_OK to False (no DuckDB attempt)."""
    import logging
    monkeypatch.setattr(config, "DB_PATH", "/tmp/x';attach 'evil.db'as e;--")
    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()
    with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
        assert analytics.is_available() is False
    assert any("rejecting DuckDB init" in r.message for r in caplog.records)


def test_init_connection_oserror_on_mkdir_disables_engine(file_db, monkeypatch, caplog):
    """`Path(home_dir).mkdir(...)` raising OSError must disable the engine
    and log a warning; the loop falls back to SQLite cleanly."""
    import logging
    from pathlib import Path as _Path

    def _boom(self, *a, **kw):
        raise OSError("simulated mkdir failure")

    monkeypatch.setattr(config, "USE_DUCKDB", True)
    monkeypatch.setattr(_Path, "mkdir", _boom)
    analytics._reset_for_tests()
    with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
        assert analytics.is_available() is False
    assert any("cannot prepare DuckDB dirs" in r.message for r in caplog.records)


def test_init_connection_duckdb_exception_disables_engine(file_db, monkeypatch, caplog):
    """DuckDB raising during INSTALL/LOAD/ATTACH must disable the engine
    (so per-query callers safely return None and SQLite fallback runs)
    and log the failure with the exception type name."""
    import logging
    if not analytics._DUCKDB_IMPORT_OK:
        pytest.skip("duckdb not installed in this environment")

    def _boom(*a, **kw):
        raise RuntimeError("simulated duckdb attach failure")

    monkeypatch.setattr(config, "USE_DUCKDB", True)
    monkeypatch.setattr(analytics.duckdb, "connect", _boom)
    analytics._reset_for_tests()
    with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
        assert analytics.is_available() is False
    assert any("DuckDB init failed" in r.message for r in caplog.records)


def test_heatmap_returns_none_when_engine_throws(file_db, monkeypatch):
    """Per-query exception path: the cursor execute raises, the function
    swallows it and returns None so the caller falls back to SQLite."""
    monkeypatch.setattr(analytics, "_init_connection", _stub_init_connection)
    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()
    # Force engine initialised
    assert analytics.is_available() is True

    class _BoomCursor:
        def execute(self, *a, **kw):
            raise RuntimeError("simulated query failure")

    class _BoomConn:
        def cursor(self):
            return _BoomCursor()

    monkeypatch.setattr(analytics, "_CONN", _BoomConn())
    # Both APIs should fall to None on a per-query exception
    assert analytics.heatmap(None, 2) is None
    assert analytics.coverage(None, 52.0, 21.0, 10) is None


def test_close_resets_conn(file_db, monkeypatch):
    """close() must drop the connection so subsequent is_available() either
    re-initialises or stays False (the test exercises the close path that
    `_reset_for_tests` and shutdown relay on)."""
    monkeypatch.setattr(analytics, "_init_connection", _stub_init_connection)
    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()
    assert analytics.is_available() is True
    assert analytics._CONN is not None

    analytics.close()
    assert analytics._CONN is None


def test_shutdown_race_no_warning(file_db, monkeypatch, caplog):
    """Simulate the shutdown race: close() is called while a query is in
    flight. The in-flight call must return None silently — no WARNING logged,
    no traceback — because the caller (web.py) will fall through to SQLite
    and the noisy log during clean shutdown is confusing."""
    import logging

    monkeypatch.setattr(analytics, "_init_connection", _stub_init_connection)
    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()
    assert analytics.is_available() is True

    # Patch _CONN so that cursor().execute() raises the same exception
    # DuckDB raises when the connection is closed mid-query.
    class _ClosedCursor:
        def execute(self, *a, **kw):
            raise Exception("Invalid Input Error: No open result set")

    class _ClosedConn:
        def cursor(self):
            return _ClosedCursor()

    monkeypatch.setattr(analytics, "_CONN", _ClosedConn())
    # Simulate close() having fired (sets _SHUTDOWN before we get to execute)
    analytics._SHUTDOWN.set()

    with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
        result_h = analytics.heatmap(None, 2)
        result_c = analytics.coverage(None, 52.0, 21.0, 10)

    assert result_h is None
    assert result_c is None
    # No WARNING should appear — shutdown races must be silent
    assert not caplog.records, f"unexpected log records: {caplog.records}"


# ---------------------------------------------------------------------------
# Audit 2026-05-26: DuckDB temp dir cleanup hardening
# ---------------------------------------------------------------------------


class TestDuckdbTempCleanup:
    """The DuckDB init/shutdown previously called `os.unlink` on every file
    in ``DUCKDB_TEMP_DIR``. A misconfigured ``RSBS_DUCKDB_TEMP_DIR`` (e.g.
    ``/tmp``) would delete unrelated files. The fix requires a marker file
    in the directory and only cleans known DuckDB temp patterns; it also
    refuses to use dangerous parent paths."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        # Ensure each test starts from a clean module state — the analytics
        # module memoises infra state across calls.
        analytics._reset_for_tests()
        # All tests will write a benign DB_PATH so init doesn't fail there.
        db = tmp_path / "history.db"
        conn = _make_file_db(db)
        conn.close()
        monkeypatch.setattr(config, "DB_PATH", str(db))
        monkeypatch.setattr(config, "DUCKDB_HOME_DIR", str(tmp_path / "duckdb-home"))
        monkeypatch.setattr(config, "USE_DUCKDB", True)
        yield
        analytics._reset_for_tests()

    def test_refuses_marker_absent_with_foreign_files(
        self, monkeypatch, tmp_path, caplog
    ):
        """If the temp dir contains files but no marker, init must NOT
        delete anything — it disables DuckDB and logs a warning instead."""
        import logging
        tmp = tmp_path / "duckdb-tmp"
        tmp.mkdir()
        sentinel = tmp / "user-data.txt"
        sentinel.write_text("DO NOT DELETE")

        monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", str(tmp))
        with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
            assert analytics.is_available() is False
        assert sentinel.exists(), "foreign file was deleted from un-claimed temp dir"

    def test_marker_present_preserves_foreign_files(self, monkeypatch, tmp_path):
        """With the marker present, init cleans ONLY DuckDB-shaped temp
        files — foreign files alongside the marker survive."""
        tmp = tmp_path / "duckdb-tmp"
        tmp.mkdir()
        marker = tmp / ".readsbstats-duckdb-tmp"
        marker.write_text("")
        foreign = tmp / "user-data.txt"
        foreign.write_text("KEEP ME")
        duck_tmp = tmp / "duckdb_temporary_storage-0.tmp"
        duck_tmp.write_text("scratch")

        monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", str(tmp))
        # We don't care whether DuckDB itself succeeds (depends on
        # binary), we only care about the file-system side effects.
        analytics._ensure_initialised()
        assert foreign.exists()
        assert marker.exists()
        assert not duck_tmp.exists()

    def test_empty_dir_gets_marker(self, monkeypatch, tmp_path):
        """First-ever init on an empty / non-existent dir writes the
        marker so subsequent runs know the directory is owned."""
        tmp = tmp_path / "duckdb-tmp-fresh"
        monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", str(tmp))
        analytics._ensure_initialised()
        assert (tmp / ".readsbstats-duckdb-tmp").exists()

    @pytest.mark.parametrize("bad", ["/", "/tmp", "/var/tmp", "/home"])
    def test_rejects_dangerous_temp_paths(self, monkeypatch, bad, caplog):
        """Refuse to operate when DUCKDB_TEMP_DIR points at a shared
        system directory — even if it would happen to be empty."""
        import logging
        monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", bad)
        with caplog.at_level(logging.WARNING, logger="readsbstats.analytics"):
            assert analytics.is_available() is False
        assert any("temp" in r.message.lower() or "duckdb" in r.message.lower()
                   for r in caplog.records)
