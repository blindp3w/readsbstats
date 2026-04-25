"""Tests for readsbstats.database — connect, init_db, and _migrate."""

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
        """_migrate should compute max_distance_bearing from positions."""
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
        # Re-run _migrate — backfill should compute bearing
        database._migrate(conn)
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
        database._migrate(conn)
        row = conn.execute("SELECT max_distance_bearing FROM flights WHERE icao_hex = 'aabbcc'").fetchone()
        assert row[0] == 45.0  # unchanged
        conn.close()

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
