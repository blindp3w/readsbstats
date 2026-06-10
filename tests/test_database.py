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


class TestSynchronousPragma:
    def test_connect_synchronous_normal_by_default(self, tmp_path):
        """WAL + synchronous=NORMAL is the project default (Pi USB-HDD:
        fsync only at checkpoint; power loss costs at most the last few
        commits, never corruption)."""
        conn = database.connect(str(tmp_path / "sync.db"))
        try:
            # PRAGMA synchronous: 0=OFF 1=NORMAL 2=FULL 3=EXTRA
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        finally:
            conn.close()

    def test_connect_synchronous_full_override(self, tmp_path, monkeypatch):
        """RSBS_DB_SYNCHRONOUS=FULL must reach the actual connection pragma
        (connect() reads config at call time — guard against the f-string
        being baked at import)."""
        monkeypatch.setattr(database.config, "DB_SYNCHRONOUS", "FULL")
        conn = database.connect(str(tmp_path / "sync_full.db"))
        try:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        finally:
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

    def test_backfill_bearing_processes_many_flights_with_cursor(self, tmp_path):
        """Regression for audit-12 #147 — the old LIMIT subquery re-scanned
        the table from the top on every iteration (O(n²) work). The cursor
        pattern uses ``WHERE id > last_id`` so each row is examined once.

        We verify two things:
          1. Every needs-bearing flight gets a bearing.
          2. The whole run completes in well under the "scan-everything"
             worst case — checked by capping wall time."""
        import time as _time
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        # Insert N flights, each with one position northwest of receiver.
        N = 600  # > one batch (500), so the cursor branch is exercised
        rows = []
        for i in range(N):
            cur = conn.execute(
                "INSERT INTO flights (icao_hex, first_seen, last_seen, max_distance_nm) "
                "VALUES (?, ?, ?, ?)",
                (f"a{i:05x}", 1000 + i, 2000 + i, 50.0 + i),
            )
            rows.append(cur.lastrowid)
        for fid in rows:
            conn.execute(
                "INSERT INTO positions (flight_id, ts, lat, lon) "
                "VALUES (?, ?, 53.225, 20.940)",
                (fid, 1500),
            )
        conn.commit()
        conn.close()

        t0 = _time.monotonic()
        database.backfill_bearing(db_path)
        elapsed = _time.monotonic() - t0

        # Every flight must have a bearing now.
        conn = database.connect(db_path)
        null_count = conn.execute(
            "SELECT COUNT(*) FROM flights "
            "WHERE max_distance_nm IS NOT NULL AND max_distance_bearing IS NULL"
        ).fetchone()[0]
        assert null_count == 0, f"{null_count} flights still missing bearing"
        conn.close()

        # Soft perf assert: on a modern dev machine even 600 flights should
        # complete in well under 5s; the old O(n²) pattern would still be
        # fast at this size but the assert prevents future regressions if
        # someone re-introduces full-table scans on every iteration.
        assert elapsed < 5.0, f"backfill_bearing took {elapsed:.2f}s for {N} flights"

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

    def test_idx_positions_flight_ts_built(self, tmp_path):
        """Audit 2026-05-26: composite (flight_id, ts) index must exist on
        fresh installs (DDL) AND on existing DBs after background
        migration runs."""
        db_path = str(tmp_path / "fts.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        # DDL path: fresh init already creates the composite.
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(positions)").fetchall()
        }
        assert "idx_positions_flight_ts" in indexes
        conn.close()

        # Existing-DB path: drop the index, run background migrations,
        # ensure it comes back.
        conn = database.connect(db_path)
        conn.execute("DROP INDEX idx_positions_flight_ts")
        conn.commit()
        conn.close()
        database.run_background_migrations(db_path)
        conn = database.connect(db_path)
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(positions)").fetchall()
        }
        assert "idx_positions_flight_ts" in indexes
        conn.close()

    def test_backfill_flights_enrichment_populates_null_columns(self, tmp_path):
        """Audit 2026-05-26: rows in flights with NULL registration /
        aircraft_type and a matching aircraft_db row must be populated
        by the background backfill."""
        db_path = str(tmp_path / "bf.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) "
            "VALUES ('aabbcc', 'SP-ABC', 'A320')"
        )
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) "
            "VALUES ('aabbcc', 1000, 2000)"
        )
        # Row with no matching aircraft_db entry must stay NULL.
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) "
            "VALUES ('ffffff', 1100, 2100)"
        )
        conn.commit()
        conn.close()

        database._backfill_flights_enrichment(db_path)

        conn = database.connect(db_path)
        row = conn.execute(
            "SELECT registration, aircraft_type FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()
        assert row["registration"] == "SP-ABC"
        assert row["aircraft_type"] == "A320"
        unmatched = conn.execute(
            "SELECT registration, aircraft_type FROM flights WHERE icao_hex='ffffff'"
        ).fetchone()
        assert unmatched["registration"] is None
        assert unmatched["aircraft_type"] is None
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

    def test_migrate_does_not_add_watchlist_alerted(self, tmp_path):
        """_migrate() must not re-introduce the dead watchlist_alerted column.
        Dedup is handled by ``is_new_flight`` in the collector, so the column
        was never read or written. Removed to keep the schema honest."""
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

        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        assert "watchlist_alerted" not in cols
        conn.close()

    def test_background_migration_drops_watchlist_alerted(self, tmp_path):
        """Existing DBs that already had the column added by past _migrate()
        runs should converge to the clean schema. The drop is in
        run_background_migrations() because ALTER TABLE DROP COLUMN rewrites
        the entire table — too slow for _migrate() on a busy receiver."""
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute("ALTER TABLE flights ADD COLUMN watchlist_alerted INTEGER DEFAULT 0")
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, watchlist_alerted) "
            "VALUES ('aabbcc', 1000, 2000, 1)"
        )
        conn.commit()
        conn.close()

        database.run_background_migrations(db_path)

        conn = database.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        assert "watchlist_alerted" not in cols
        # Row survived the column drop
        count = conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
        assert count == 1
        conn.close()

    def test_background_migration_drop_is_noop_when_column_absent(self, tmp_path):
        """If the column was never added (fresh install post-fix), the drop
        step is a clean no-op — no exception, no side effects."""
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        # Sanity: fresh DBs don't have the column.
        conn = database.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        assert "watchlist_alerted" not in cols
        conn.close()

        database.run_background_migrations(db_path)  # must not raise

        conn = database.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
        assert "watchlist_alerted" not in cols
        conn.close()


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
        first_write_done = threading.Event()
        errors: list[BaseException] = []

        def writer():
            try:
                conn = database.connect(db_path)
                ts = 1
                # Synchronisation barrier: the test must observe at least one
                # successful INSERT before the index build runs, otherwise the
                # writer thread can be scheduled out for the entire (~µs)
                # index build on a tiny CI runner and the test sees zero rows.
                conn.execute(
                    "INSERT INTO positions (flight_id, ts, lat, lon) "
                    "VALUES (?, ?, ?, ?)",
                    (fid, ts, 50.0, 20.0),
                )
                conn.commit()
                first_write_done.set()
                ts += 1
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
        assert first_write_done.wait(timeout=5), "writer never produced first row"
        try:
            database._build_positions_indexes(db_path)
        finally:
            stop.set()
            t.join(timeout=10)
        assert not errors, f"writer hit errors: {errors!r}"
        check = database.connect(db_path)
        n = check.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        check.close()
        assert n >= 1

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
        """Backfill runs in run_background_migrations() (collector-only),
        not in _migrate() — slow UPDATE must not block web startup."""
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, primary_source) "
            "VALUES ('aabbcc', 1000, 2000, NULL)"
        )
        conn.commit()
        conn.close()
        database.run_background_migrations(db_path)
        conn = database.connect(db_path)
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
        conn.close()
        database.run_background_migrations(db_path)
        conn = database.connect(db_path)
        row = conn.execute("SELECT primary_source FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row[0] is None  # still NULL — active flight not touched
        conn.close()

    def test_migrate_does_not_run_slow_backfill(self, tmp_path):
        """Regression for #139 — _migrate() must NOT run the full-table backfill
        that historically lived inside it. _migrate() is on the web hot path; the
        backfill belongs in run_background_migrations() (collector-only)."""
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.executescript(database.DDL)
        # Pre-existing closed flight with NULL primary_source
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, primary_source) "
            "VALUES ('aabbcc', 1000, 2000, NULL)"
        )
        conn.commit()
        database._migrate(conn)
        # Should still be NULL — _migrate must not touch it
        row = conn.execute(
            "SELECT primary_source FROM flights WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert row[0] is None
        conn.close()

    def test_migrate_creates_airports_and_callsign_routes(self, tmp_path):
        """Regression for #140 — both tables defined in DDL but historically
        missing from _migrate(). On a web-only restart against an old DB they
        must be created or route_enricher writes / airport joins fail."""
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        # Old-schema simulation: only the minimum tables _migrate() touches
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
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "airports" in tables
        assert "callsign_routes" in tables
        # Schema sanity — primary keys + a couple of expected columns
        airports_cols = {row[1] for row in conn.execute("PRAGMA table_info(airports)")}
        assert "icao_code" in airports_cols
        assert "fetched_at" in airports_cols
        routes_cols = {row[1] for row in conn.execute("PRAGMA table_info(callsign_routes)")}
        assert "callsign" in routes_cols
        assert "origin_icao" in routes_cols
        assert "dest_icao" in routes_cols
        conn.close()


class TestRecoverAircraftDbSwap:
    """BE-3 (Audit 2026-05-31): shared aircraft_db swap recovery.

    The recovery logic previously lived only in db_updater._recover_aborted_swap
    and ran on the weekly updater path. It now lives in database.py so the web
    server and collector recover an interrupted swap on startup, without waiting
    for the next update_aircraft_db() run.
    """

    def test_restores_aircraft_db_old_when_canonical_absent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        # Post-step-3-pre-step-4 state: canonical gone, only _old survives.
        conn.execute(
            "CREATE TABLE aircraft_db_old ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO aircraft_db_old VALUES ('aaaaaa', 'RESTORED', 'A320', '', 0)"
        )
        conn.commit()

        database.recover_aircraft_db_swap(conn)

        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert names == {"aircraft_db"}
        row = conn.execute(
            "SELECT registration FROM aircraft_db WHERE icao_hex='aaaaaa'"
        ).fetchone()
        assert row[0] == "RESTORED"
        conn.close()

    def test_drops_leftover_aircraft_db_old_when_canonical_present(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        # Post-step-4-pre-step-5 state: both present, final DROP didn't run.
        conn.execute(
            "CREATE TABLE aircraft_db ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute("INSERT INTO aircraft_db VALUES ('bbbbbb', 'CANON', '', '', 0)")
        conn.execute(
            "CREATE TABLE aircraft_db_old ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.commit()

        database.recover_aircraft_db_swap(conn)

        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert names == {"aircraft_db"}
        # Canonical data untouched.
        assert conn.execute(
            "SELECT registration FROM aircraft_db WHERE icao_hex='bbbbbb'"
        ).fetchone()[0] == "CANON"
        conn.close()

    def test_drops_orphan_aircraft_db_new(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.execute(
            "CREATE TABLE aircraft_db ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE aircraft_db_new ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute("INSERT INTO aircraft_db_new VALUES ('deaddd', 'STALE', '', '', 0)")
        conn.commit()

        database.recover_aircraft_db_swap(conn)

        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert "aircraft_db_new" not in names
        assert "aircraft_db" in names
        conn.close()

    def test_noop_when_only_canonical_present(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.execute(
            "CREATE TABLE aircraft_db ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.commit()

        database.recover_aircraft_db_swap(conn)  # must not raise

        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert names == {"aircraft_db"}
        conn.close()


class TestRecoverAircraftDbSwapIndexIntegrity:
    """F02 follow-up (Audit 18): after recover_aircraft_db_swap, the recovered
    aircraft_db must still carry idx_aircraft_db_type_code — photo_sources.py
    JOINs aircraft_db.type_code on every type-fallback lookup, so a missing
    index turns those into full-table scans of ~620k rows.

    SQLite index names are schema-global and follow a table RENAME. update_
    aircraft_db builds the new staging table with the PK only and (re)creates
    the type_code index AFTER the swap completes. If the swap is interrupted in
    the 'canonical present + _old leftover' state, the index still lives on the
    old table; dropping that old table removes it, leaving the new canonical
    aircraft_db unindexed.
    """

    @staticmethod
    def _has_type_code_index(conn) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_aircraft_db_type_code' AND tbl_name='aircraft_db'"
        ).fetchone()
        return row is not None

    def test_index_present_after_restoring_old_back(self, tmp_path):
        # Branch: canonical gone, _old survives (first RENAME done, second not).
        # The original aircraft_db carried the index built by _migrate(); after
        # `RENAME aircraft_db -> aircraft_db_old` the index followed to _old.
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.execute(
            "CREATE TABLE aircraft_db_old ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE INDEX idx_aircraft_db_type_code ON aircraft_db_old(type_code)"
        )
        conn.commit()

        database.recover_aircraft_db_swap(conn)

        # Rename-back carries the index to the restored canonical table.
        assert self._has_type_code_index(conn)
        conn.close()

    def test_index_present_after_dropping_orphan_new(self, tmp_path):
        # Branch: canonical present (with its index) + orphan _new staging.
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.execute(
            "CREATE TABLE aircraft_db ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE INDEX idx_aircraft_db_type_code ON aircraft_db(type_code)"
        )
        conn.execute(
            "CREATE TABLE aircraft_db_new ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.commit()

        database.recover_aircraft_db_swap(conn)

        # Canonical never touched → index intact.
        assert self._has_type_code_index(conn)
        conn.close()

    def test_index_present_after_dropping_leftover_old(self, tmp_path):
        # Branch: canonical present (the NEW table, PK-only, NO type_code index)
        # + _old leftover (carries the index that followed the first RENAME).
        # Recovery drops _old; without the fix that also drops the index and
        # leaves the new aircraft_db unindexed.
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        # New canonical table as update_aircraft_db creates it: PK only.
        conn.execute(
            "CREATE TABLE aircraft_db ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute("INSERT INTO aircraft_db VALUES ('newnew', 'NEW', 'B738', '', 0)")
        # Old table still carries the type_code index (followed the rename).
        conn.execute(
            "CREATE TABLE aircraft_db_old ("
            "icao_hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, "
            "type_desc TEXT, flags INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE INDEX idx_aircraft_db_type_code ON aircraft_db_old(type_code)"
        )
        conn.commit()

        # Sanity: pre-recovery the index belongs to _old, not canonical.
        assert not self._has_type_code_index(conn)

        database.recover_aircraft_db_swap(conn)

        # Leftover _old dropped, but the new canonical aircraft_db must end up
        # with the type_code index regardless.
        assert self._has_type_code_index(conn)
        # And the recovered data is the NEW table's row (old was dropped).
        assert conn.execute(
            "SELECT registration FROM aircraft_db WHERE icao_hex='newnew'"
        ).fetchone()[0] == "NEW"
        conn.close()


class TestEnsureBaseSchema:
    """BE-3 (Audit 2026-05-31): explicit base-schema bootstrap for the web
    server. Creates base tables only when missing (never building slow
    positions indexes synchronously), recovers an interrupted aircraft_db swap,
    then runs _migrate()."""

    def test_creates_base_tables_on_empty_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.ensure_base_schema(db_path)
        conn = database.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "flights" in tables
        assert "positions" in tables
        # Representative query must not raise no-such-table.
        assert conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0
        # schema_version recorded.
        assert conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == database.SCHEMA_VERSION
        conn.close()

    def test_recovers_aircraft_db_swap_on_existing_db(self, tmp_path):
        """Existing DB (flights/positions already present) that crashed
        mid-swap: ensure_base_schema must restore aircraft_db from
        aircraft_db_old without re-running the full DDL."""
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO aircraft_db VALUES ('aaaaaa', 'REAL', 'A320', '', 0)"
        )
        conn.commit()
        # Simulate interrupted swap: canonical renamed away, never renamed back.
        conn.execute("ALTER TABLE aircraft_db RENAME TO aircraft_db_old")
        conn.commit()
        conn.close()

        database.ensure_base_schema(db_path)

        conn = database.connect(db_path)
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert names == {"aircraft_db"}
        assert conn.execute(
            "SELECT registration FROM aircraft_db WHERE icao_hex='aaaaaa'"
        ).fetchone()[0] == "REAL"
        conn.close()

    def test_idempotent_on_initialised_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute("INSERT INTO flights (icao_hex, first_seen, last_seen) "
                     "VALUES ('abc123', 1, 2)")
        conn.commit()
        conn.close()

        database.ensure_base_schema(db_path)  # must not wipe data or raise

        conn = database.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1
        conn.close()


class TestInitDbRecoversBeforeDdl:
    """BE-3 (Audit 2026-05-31): init_db must recover an interrupted aircraft_db
    swap BEFORE running the DDL. Otherwise executescript(DDL) re-creates an
    empty aircraft_db, and recovery's 'canonical present + _old leftover' branch
    would DROP aircraft_db_old — discarding the only real copy of the data."""

    def test_recovers_aircraft_db_old_before_ddl_recreates_empty(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        conn.execute(
            "INSERT INTO aircraft_db VALUES ('aaaaaa', 'REAL', 'A320', '', 0)"
        )
        conn.execute(
            "INSERT INTO aircraft_db VALUES ('bbbbbb', 'REAL2', 'B738', '', 0)"
        )
        conn.commit()
        # Interrupted swap: canonical gone, only _old has the real rows.
        conn.execute("ALTER TABLE aircraft_db RENAME TO aircraft_db_old")
        conn.commit()
        conn.close()

        database.init_db(db_path)

        conn = database.connect(db_path)
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'aircraft_db%'"
            ).fetchall()
        }
        assert names == {"aircraft_db"}
        # The two real rows survived — they were NOT discarded by a DDL-first
        # ordering that would have re-created an empty aircraft_db.
        assert conn.execute("SELECT COUNT(*) FROM aircraft_db").fetchone()[0] == 2
        conn.close()
