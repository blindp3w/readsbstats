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
    """File-backed history.db (DuckDB can attach to it) + matching `web._db`
    + matching `config.DB_PATH`. The `analytics` module is reset before
    AND after each test so cross-test state never leaks."""
    db_file = tmp_path / "history.db"
    conn = _make_file_db(db_file)

    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(web, "_db", conn)
    monkeypatch.setattr(config, "DUCKDB_TEMP_DIR", str(tmp_path / "duckdb-tmp"))
    monkeypatch.setattr(config, "DUCKDB_HOME_DIR", str(tmp_path / "duckdb-home"))
    web._cache.clear()
    analytics._reset_for_tests()

    yield conn

    conn.close()
    analytics._reset_for_tests()


def _run_heatmap_with_engine(window: str, *, monkeypatch, use_duckdb: bool):
    """Force a specific engine via the config flag, clear the cache, run."""
    monkeypatch.setattr(config, "USE_DUCKDB", use_duckdb)
    web._cache.clear()
    analytics._reset_for_tests()
    return web._compute_heatmap_sync(window)


def _run_coverage_with_engine(window: str, *, monkeypatch, use_duckdb: bool):
    monkeypatch.setattr(config, "USE_DUCKDB", use_duckdb)
    web._cache.clear()
    analytics._reset_for_tests()
    return web._compute_coverage_sync(window)


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
    web._cache.clear()
    result = web._compute_heatmap_sync("24h")
    assert result["count"] == 1

    monkeypatch.setattr(config, "USE_DUCKDB", True)
    analytics._reset_for_tests()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DuckDB query failure")

    monkeypatch.setattr(analytics, "heatmap", _boom)
    web._cache.clear()
    result = web._compute_heatmap_sync("24h")
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# 7. is_available() must reflect config.USE_DUCKDB on every call
# ---------------------------------------------------------------------------


def test_engine_selection_via_env_no_restart(file_db, monkeypatch):
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
