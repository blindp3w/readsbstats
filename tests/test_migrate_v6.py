"""End-to-end test of the offline v5→v6 migration script."""
import time

import pytest

from migrate_v6 import migrate
from readsbstats import database, posenc
from tests.test_database import LEGACY_V5_POSITIONS_DDL


# The five legacy positions indexes a long-lived production v5 DB carries
# (pre-Phase-1/2 layout). The combined-path test creates them explicitly to
# mirror the real Pi DB before the v6 deploy.
LEGACY_V5_INDEXES = """
CREATE INDEX idx_positions_flight ON positions(flight_id);
CREATE INDEX idx_positions_ts_coords ON positions(ts)
    WHERE lat IS NOT NULL AND lon IS NOT NULL;
CREATE INDEX idx_positions_flight_id_desc ON positions(flight_id, id DESC);
CREATE INDEX idx_positions_ts_flight ON positions(ts, flight_id);
CREATE INDEX idx_positions_ts_lat_lon ON positions(ts, lat, lon);
"""


def _make_v5(path, position_rows=None):
    """Build a v5-layout DB. ``position_rows`` — optional list of
    ``(ts, lat, lon, source_type)`` tuples (lat/lon may be None); defaults to
    the classic 3-row sample at ts=100..102."""
    conn = database.connect(path)
    conn.executescript(database.DDL)
    conn.executescript("DROP TABLE positions;" + LEGACY_V5_POSITIONS_DDL)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (5, strftime('%s','now'))")
    conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 1, 2)")
    fid = conn.execute("SELECT id FROM flights").fetchone()[0]
    if position_rows is None:
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, gs, track, rssi, messages, source_type) "
            "VALUES (?, 100, 52.20491, 21.00001, 437.5, 271.3, -23.5, 1500, 'adsb_icao')", (fid,))
        conn.execute(
            "INSERT INTO positions (flight_id, ts, source_type) VALUES (?, 101, 'mlat')", (fid,))
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) "
            "VALUES (?, 102, -1.5, -21.0, 'never_seen_type')", (fid,))
    else:
        conn.executemany(
            "INSERT INTO positions (flight_id, ts, lat, lon, source_type) "
            "VALUES (?, ?, ?, ?, ?)",
            [(fid, ts, lat, lon, st) for ts, lat, lon, st in position_rows])
    conn.commit()
    conn.close()


def test_migrate_v6_end_to_end(tmp_path):
    path = str(tmp_path / "prod.db")
    _make_v5(path)
    stats = migrate(path)
    assert stats == {"skipped": False, "rows": 3}
    conn = database.connect(path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 6
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        assert "source" in cols and "source_type" not in cols
        assert conn.execute(
            "SELECT lat FROM positions WHERE ts = 100").fetchone()[0] == 5220491
        assert conn.execute(
            "SELECT source FROM positions WHERE ts = 102").fetchone()[0] == posenc.OTHER_CODE
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='positions'")}
        assert {"idx_positions_flight_ts", "idx_positions_ts"} <= idx
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_migrate_v6_is_idempotent(tmp_path):
    path = str(tmp_path / "prod2.db")
    _make_v5(path)
    migrate(path)
    assert migrate(path) == {"skipped": True}


def test_combined_deploy_path_v5_to_rollups(tmp_path):
    """The real Pi upgrade: v5 DB with all legacy indexes → offline migration
    → background migrations → rollups built, final index set, heatmap math.

    run_background_migrations logs exceptions instead of raising, so this
    test asserts the OUTCOMES — a swallowed failure shows up as a missing
    index, an unset ready flag, or wrong rollup weights."""
    path = str(tmp_path / "combined.db")
    # 3 full past days, two coord-bearing rows + one NULL-coords (MLAT-style)
    # row per day. Days are recent so the day-batched backfill loop stays short.
    now = int(time.time())
    today_start = (now // 86400) * 86400
    rows = []
    for d in (3, 2, 1):                       # 3, 2, 1 days ago
        base = today_start - d * 86400
        rows.append((base + 3600, 52.0 + d * 0.01, 21.0, "adsb_icao"))
        rows.append((base + 7200, 52.5, 21.0 + d * 0.01, "adsb_icao"))
        rows.append((base + 10800, None, None, "mlat"))   # no coords → no rollup
    n_past_with_coords = sum(1 for _, lat, lon, _ in rows
                             if lat is not None and lon is not None)
    _make_v5(path, position_rows=rows)
    conn = database.connect(path)
    conn.executescript(LEGACY_V5_INDEXES)
    conn.close()

    migrate(path)
    database.run_background_migrations(path)

    conn = database.connect(path)
    try:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='positions'")}
        assert idx == {"idx_positions_flight_ts", "idx_positions_ts"}
        from readsbstats import rollups
        assert rollups.ready(conn)
        # Every seeded coord-bearing row is on a past day; the coarse grid
        # must account for each of them exactly once.
        n_past = conn.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE ts < (strftime('%s','now')/86400)*86400 "
            "AND lat IS NOT NULL AND lon IS NOT NULL"
        ).fetchone()[0]
        assert n_past == n_past_with_coords == 6
        assert conn.execute(
            "SELECT SUM(w) FROM grid_daily WHERE scale = 10"
        ).fetchone()[0] == n_past
        # Coverage rolled up for all 3 past days as well.
        assert conn.execute(
            "SELECT COUNT(DISTINCT day) FROM coverage_daily"
        ).fetchone()[0] == 3
    finally:
        conn.close()
