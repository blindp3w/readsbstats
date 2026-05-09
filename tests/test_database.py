"""Tests for readsbstats.database — connect, init_db, and _migrate."""

import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from readsbstats import database


class TestConnect:
    def test_returns_connection(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_row_factory_set(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        assert conn.row_factory is sqlite3.Row
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()


class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "flights", "positions", "active_flights", "aircraft_db",
            "airlines", "photos", "watchlist", "airports",
            "callsign_routes", "adsbx_overrides", "schema_version",
        }
        assert expected.issubset(tables)
        conn.close()

    def test_schema_version_recorded(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row is not None
        assert row[0] == database.SCHEMA_VERSION
        conn.close()

    def test_idempotent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        database.init_db(db_path)  # should not raise
        conn = database.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count == 1
        conn.close()


class TestMigrate:
    def test_adds_missing_columns(self, tmp_path):
        """Simulate an old DB missing max_distance_bearing, verify _migrate adds it."""
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        # Create a flights table resembling an old schema (missing some newer columns)
        conn.execute("""
            CREATE TABLE flights (
                id INTEGER PRIMARY KEY,
                icao_hex TEXT,
                callsign TEXT,
                registration TEXT,
                aircraft_type TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                max_distance_nm REAL,
                max_alt_baro REAL,
                max_gs REAL,
                total_positions INTEGER DEFAULT 0,
                adsb_positions INTEGER DEFAULT 0,
                mlat_positions INTEGER DEFAULT 0,
                primary_source TEXT
            )
        """)
        # Need other tables for _migrate to succeed
        conn.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, flight_id INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS active_flights (icao_hex TEXT PRIMARY KEY, flight_id INTEGER NOT NULL, last_seen INTEGER NOT NULL)")
        conn.commit()

        database._migrate(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        assert "max_distance_bearing" in cols
        assert "origin_icao" in cols
        assert "dest_icao" in cols
        conn.close()

    def test_creates_watchlist_table(self, tmp_path):
        """_migrate should create watchlist table on old DBs that lack it."""
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.execute("""
            CREATE TABLE flights (
                id INTEGER PRIMARY KEY, icao_hex TEXT, callsign TEXT,
                registration TEXT, aircraft_type TEXT,
                first_seen INTEGER, last_seen INTEGER,
                max_distance_nm REAL, max_alt_baro REAL, max_gs REAL,
                total_positions INTEGER DEFAULT 0,
                adsb_positions INTEGER DEFAULT 0,
                mlat_positions INTEGER DEFAULT 0,
                primary_source TEXT
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, flight_id INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS active_flights (icao_hex TEXT PRIMARY KEY, flight_id INTEGER NOT NULL, last_seen INTEGER NOT NULL)")
        conn.commit()

        database._migrate(conn)

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "watchlist" in tables
        assert "adsbx_overrides" in tables
        conn.close()

    def test_creates_indexes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(flights)").fetchall()
            if row[1] is not None
        }
        assert "idx_flights_dist" in indexes
        assert "idx_flights_icao_first" in indexes
        conn.close()

    def test_backfills_bearing_for_flights_with_positions(self, tmp_path):
        """backfill_bearing should compute max_distance_bearing from positions."""
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        # Insert a flight with max_distance_nm but no bearing
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm) "
            "VALUES ('aabbcc', 1000, 2000, 100.0)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Add a position north of receiver (bearing ~0°)
        conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, 1500, 53.225, 20.940)",
            (fid,),
        )
        conn.commit()
        conn.close()
        # backfill_bearing opens its own connection
        database.backfill_bearing(db_path)
        conn = database.connect(db_path)
        row = conn.execute("SELECT max_distance_bearing FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row[0] is not None
        # Position is roughly north → bearing should be near 0° (or 360°)
        assert row[0] < 10 or row[0] > 350
        conn.close()

    def test_backfill_skipped_when_bearing_already_set(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm, max_distance_bearing) "
            "VALUES ('aabbcc', 1000, 2000, 100.0, 45.0)"
        )
        conn.commit()
        conn.close()
        database.backfill_bearing(db_path)
        conn = database.connect(db_path)
        row = conn.execute("SELECT max_distance_bearing FROM flights WHERE icao_hex = 'aabbcc'").fetchone()
        assert row[0] == 45.0  # unchanged
        conn.close()

    def test_build_positions_indexes_creates_expected_indexes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        database._build_positions_indexes(db_path)
        conn = database.connect(db_path)
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(positions)").fetchall()
            if row[1] is not None
        }
        assert "idx_positions_flight_id_desc" in indexes
        assert "idx_positions_ts_flight" in indexes
        assert "idx_positions_ts_lat_lon" in indexes
        assert "idx_positions_ts_coords" in indexes
        conn.close()

    def test_partial_index_recorded_with_where_clause(self, tmp_path):
        """idx_positions_ts_coords is a partial index — verify the WHERE
        clause survives in sqlite_master so the planner can use it."""
        db_path = str(tmp_path / "partial.db")
        database.init_db(db_path)
        database._build_positions_indexes(db_path)
        conn = database.connect(db_path)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_positions_ts_coords'"
        ).fetchone()
        assert ddl is not None and ddl[0] is not None
        sql = ddl[0]
        assert "WHERE" in sql.upper()
        assert "lat IS NOT NULL" in sql
        assert "lon IS NOT NULL" in sql
        conn.close()

    def test_run_background_migrations_idempotent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        database.run_background_migrations(db_path)
        database.run_background_migrations(db_path)  # second call must not raise
        conn = database.connect(db_path)
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(positions)").fetchall()
            if row[1] is not None
        }
        assert "idx_positions_flight_id_desc" in indexes
        conn.close()

    def test_background_migrations_swallow_connect_failure(self, monkeypatch):
        """If connect() itself raises, the helpers must not crash with
        UnboundLocalError trying to close a connection that was never opened."""
        def boom(_path):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(database, "connect", boom)
        # Both helpers must swallow the error and return cleanly.
        database._build_positions_indexes("/nonexistent.db")
        database.backfill_bearing("/nonexistent.db")


class TestBackgroundMigrationsConcurrency:
    """The collector spawns `run_background_migrations` while still actively
    writing to `positions`.  CREATE INDEX takes the SQLite write lock, so a
    naive design can starve writers past `busy_timeout`.  These tests verify
    the helpers tolerate concurrent INSERT traffic."""

    def test_index_build_under_concurrent_writes(self, tmp_path):
        import threading
        db_path = str(tmp_path / "concur.db")
        database.init_db(db_path)
        # Seed a flight so writers have a flight_id to attach to.
        seed = database.connect(db_path)
        seed.execute("INSERT INTO flights (icao_hex, first_seen, last_seen) "
                     "VALUES ('aabbcc', 0, 0)")
        fid = seed.execute(
            "SELECT id FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()[0]
        seed.commit()
        seed.close()

        stop = threading.Event()
        errors: list[BaseException] = []

        def writer():
            try:
                conn = database.connect(db_path)
                ts = 1
                while not stop.is_set():
                    conn.execute(
                        "INSERT INTO positions (flight_id, ts, lat, lon) "
                        "VALUES (?, ?, ?, ?)",
                        (fid, ts, 50.0 + ts * 0.0001, 20.0 + ts * 0.0001),
                    )
                    conn.commit()
                    ts += 1
                conn.close()
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            database._build_positions_indexes(db_path)
        finally:
            stop.set()
            t.join(timeout=10)
        assert not errors, f"writer hit errors: {errors!r}"
        # Writes did get through.
        check = database.connect(db_path)
        n = check.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        check.close()
        assert n > 0

    def test_backfill_skips_out_of_range_coords(self, tmp_path):
        """backfill_bearing must not crash or write nonsense bearings for
        positions with out-of-range lat/lon.  The query already filters
        non-NULL coords; this test pins that bad rows don't poison output."""
        db_path = str(tmp_path / "badcoords.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        # Two flights: one with sane positions, one with out-of-range.
        conn.execute("INSERT INTO flights (icao_hex, first_seen, last_seen, "
                     "max_distance_nm, max_distance_bearing) "
                     "VALUES ('aabbcc', 0, 0, 100.0, NULL)")
        sane_fid = conn.execute(
            "SELECT id FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()[0]
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) "
                     "VALUES (?, 0, 53.0, 21.0)", (sane_fid,))

        conn.execute("INSERT INTO flights (icao_hex, first_seen, last_seen, "
                     "max_distance_nm, max_distance_bearing) "
                     "VALUES ('ddeeff', 0, 0, 100.0, NULL)")
        bad_fid = conn.execute(
            "SELECT id FROM flights WHERE icao_hex='ddeeff'"
        ).fetchone()[0]
        # 91/-181 are physically impossible — the trig functions still produce
        # a number, but the bearing should at minimum be in [0, 360).
        conn.execute("INSERT INTO positions (flight_id, ts, lat, lon) "
                     "VALUES (?, 0, 91.0, -181.0)", (bad_fid,))
        conn.commit()
        conn.close()

        database.backfill_bearing(db_path)

        check = database.connect(db_path)
        sane_b = check.execute(
            "SELECT max_distance_bearing FROM flights WHERE id = ?", (sane_fid,)
        ).fetchone()[0]
        bad_b = check.execute(
            "SELECT max_distance_bearing FROM flights WHERE id = ?", (bad_fid,)
        ).fetchone()[0]
        check.close()
        assert sane_b is not None and 0.0 <= sane_b < 360.0
        # Out-of-range bearing must still be a finite number in [0, 360); we
        # don't pin a specific value because the result is meaningless, but it
        # must not be NaN/inf and must not have crashed the query.
        assert bad_b is not None
        assert math.isfinite(bad_b)
        assert 0.0 <= bad_b < 360.0


class TestSnapshotDb:
    def test_snapshot_creates_sibling_file(self, tmp_path):
        src = str(tmp_path / "history.db")
        database.init_db(src)
        dest = database.snapshot_db(src)
        assert Path(dest).exists()
        assert Path(dest).parent == Path(tmp_path)
        # Dest is a valid sqlite file with the same schema.
        conn = sqlite3.connect(dest)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "flights" in tables
        finally:
            conn.close()

    def test_snapshot_explicit_dest(self, tmp_path):
        src = str(tmp_path / "history.db")
        database.init_db(src)
        dest = str(tmp_path / "explicit.db")
        result = database.snapshot_db(src, dest)
        assert result == dest
        assert Path(dest).exists()

    def test_snapshot_refuses_to_clobber_existing(self, tmp_path):
        src = str(tmp_path / "history.db")
        database.init_db(src)
        dest = str(tmp_path / "exists.db")
        Path(dest).write_text("placeholder")
        with pytest.raises(FileExistsError):
            database.snapshot_db(src, dest)
        # Original placeholder must be untouched.
        assert Path(dest).read_text() == "placeholder"

    def test_primary_source_backfill_on_closed_flights(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, primary_source) "
            "VALUES ('aabbcc', 1000, 2000, NULL)"
        )
        conn.commit()
        database._migrate(conn)
        row = conn.execute("SELECT primary_source FROM flights WHERE icao_hex = 'aabbcc'").fetchone()
        assert row[0] == "other"
        conn.close()

    def test_primary_source_not_changed_on_active_flights(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, primary_source) "
            "VALUES ('aabbcc', 1000, 2000, NULL)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc', ?, 2000)",
            (fid,),
        )
        conn.commit()
        database._migrate(conn)
        row = conn.execute("SELECT primary_source FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row[0] is None  # still NULL — active flight not touched
        conn.close()
