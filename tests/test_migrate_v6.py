"""End-to-end test of the offline v5→v6 migration script."""
import time

import pytest

from migrate_v6 import migrate
from readsbstats import database, posenc
from tests.test_database import LEGACY_V5_POSITIONS_DDL


def _make_v5(path):
    conn = database.connect(path)
    conn.executescript(database.DDL)
    conn.executescript("DROP TABLE positions;" + LEGACY_V5_POSITIONS_DDL)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (5, strftime('%s','now'))")
    conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 1, 2)")
    fid = conn.execute("SELECT id FROM flights").fetchone()[0]
    conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, gs, track, rssi, messages, source_type) "
        "VALUES (?, 100, 52.20491, 21.00001, 437.5, 271.3, -23.5, 1500, 'adsb_icao')", (fid,))
    conn.execute(
        "INSERT INTO positions (flight_id, ts, source_type) VALUES (?, 101, 'mlat')", (fid,))
    conn.execute(
        "INSERT INTO positions (flight_id, ts, lat, lon, source_type) "
        "VALUES (?, 102, -1.5, -21.0, 'never_seen_type')", (fid,))
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
