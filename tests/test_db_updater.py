"""
Tests for db_updater.py — CSV parsing and DB import logic.
All network I/O is replaced by monkeypatching _fetch(); no real downloads.
"""

import gzip
import io
import sqlite3

import pytest

from readsbstats import database, db_updater


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def _aircraft_gz(*rows: list[str]) -> bytes:
    """Build gzip-compressed semicolon-delimited aircraft CSV from row lists."""
    text = "\n".join(";".join(r) for r in rows)
    buf = io.BytesIO()
    with gzip.open(buf, "wt") as gz:
        gz.write(text)
    return buf.getvalue()


def _airlines_csv(*rows: list[str]) -> bytes:
    """Build a comma-delimited airlines.dat from row lists (values are auto-quoted)."""
    lines = []
    for row in rows:
        lines.append(",".join(f'"{v}"' for v in row))
    return "\n".join(lines).encode()


# ---------------------------------------------------------------------------
# update_aircraft_db
# ---------------------------------------------------------------------------

class TestUpdateAircraftDb:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_valid_row_inserted(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "1", "BOEING 737-800"],
        ))
        count = db_updater.update_aircraft_db(conn)
        assert count == 1
        row = conn.execute("SELECT * FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["registration"] == "SP-ABC"
        assert row["type_code"] == "B738"
        assert row["type_desc"] == "BOEING 737-800"
        assert row["flags"] == 1

    def test_empty_csv_returns_zero(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz())
        assert db_updater.update_aircraft_db(conn) == 0

    def test_invalid_hex_chars_skipped(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["GGGGGG", "SP-BAD", "B738", "0", ""],  # G is not valid hex
            ["488001", "SP-OK",  "A320", "0", ""],
        ))
        assert db_updater.update_aircraft_db(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM aircraft_db WHERE icao_hex='488001'").fetchone()[0] == 1

    def test_wrong_length_icao_skipped(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["48800",  "SP-BAD", "B738", "0", ""],   # 5 chars
            ["4880011","SP-BAD", "B738", "0", ""],   # 7 chars
            ["488001", "SP-OK",  "A320", "0", ""],
        ))
        assert db_updater.update_aircraft_db(conn) == 1

    def test_empty_reg_stored_as_null(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "", "B738", "0", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT registration FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["registration"] is None

    def test_empty_type_desc_stored_as_null(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "0", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT type_desc FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["type_desc"] is None

    def test_invalid_flags_defaults_to_zero(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "not_a_number", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 0

    def test_binary_military_flag(self, monkeypatch):
        """'10' in the CSV means military-only (position 0 = military)."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "10", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 1  # military only

    def test_binary_military_interesting_flags(self, monkeypatch):
        """'11' = military (pos 0) + interesting (pos 1)."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "11", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 3  # military(1) + interesting(2)

    def test_binary_ladd_flag(self, monkeypatch):
        """'0001' = LADD only (position 3)."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "0001", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 8  # LADD only

    def test_binary_pia_flag(self, monkeypatch):
        """'0010' = PIA only (position 2)."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "0010", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 4  # PIA only

    def test_binary_military_ladd_flags(self, monkeypatch):
        """'1001' = military (pos 0) + LADD (pos 3)."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-ABC", "B738", "1001", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='488001'").fetchone()
        assert row["flags"] == 9  # military(1) + LADD(8)

    def test_binary_interesting_ladd_flags(self, monkeypatch):
        """'0101' = interesting (pos 1) + LADD (pos 3), NOT military."""
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["aabbcc", "N123AB", "GLF4", "0101", ""],
        ))
        db_updater.update_aircraft_db(conn)
        row = conn.execute("SELECT flags FROM aircraft_db WHERE icao_hex='aabbcc'").fetchone()
        assert row["flags"] == 10   # interesting(2) + LADD(8), no military bit
        assert row["flags"] & 1 == 0

    def test_replaces_previous_data_on_reimport(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-OLD", "B737", "0", ""],
        ))
        db_updater.update_aircraft_db(conn)

        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488002", "SP-NEW", "A320", "0", ""],
        ))
        db_updater.update_aircraft_db(conn)

        assert conn.execute("SELECT COUNT(*) FROM aircraft_db WHERE icao_hex='488001'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM aircraft_db WHERE icao_hex='488002'").fetchone()[0] == 1

    def test_empty_line_skipped(self, monkeypatch):
        conn = self.conn
        # CSV with a blank line in the middle
        raw_text = "488001;SP-ABC;B738;0;BOEING 737-800\n\n488002;SP-XYZ;A320;0;AIRBUS A320"
        buf = io.BytesIO()
        with gzip.open(buf, "wt") as gz:
            gz.write(raw_text)
        monkeypatch.setattr(db_updater, "_fetch", lambda url: buf.getvalue())
        assert db_updater.update_aircraft_db(conn) == 2

    def test_atomic_swap_preserves_old_on_failure(self, monkeypatch):
        """Audit 2026-05-26: a crash mid-import must leave aircraft_db
        intact. Previously the function `DELETE FROM aircraft_db`-d
        before bulk-inserting, so any exception left the table empty
        or partially populated; enrichment/flags degraded until the
        next successful run.
        """
        conn = self.conn
        # Seed an "existing" aircraft_db row that must survive.
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aaaaaa', 'OLD-REG', 'A320', 'Airbus A320', 0)"
        )
        conn.commit()

        # Patch _parse_flags to raise mid-import — once enough rows have
        # been buffered, the parsing helper blows up. Exercises the
        # try/except cleanup path in update_aircraft_db.
        n_calls = {"n": 0}
        original_parse = db_updater._parse_flags
        def _boom_parse(s):
            n_calls["n"] += 1
            if n_calls["n"] > 1:
                raise RuntimeError("simulated parse failure")
            return original_parse(s)
        monkeypatch.setattr(db_updater, "_parse_flags", _boom_parse)

        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-NEW",  "B738", "0", ""],
            ["488002", "SP-NEW2", "B738", "0", ""],
            ["488003", "SP-NEW3", "B738", "0", ""],
        ))

        with pytest.raises(Exception):
            db_updater.update_aircraft_db(conn)

        # Original row preserved.
        rows = conn.execute(
            "SELECT registration FROM aircraft_db WHERE icao_hex='aaaaaa'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["registration"] == "OLD-REG"

        # No half-baked staging table left behind.
        staging = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='aircraft_db_new'"
        ).fetchall()
        assert staging == [], (
            "staging table aircraft_db_new must be dropped on failure"
        )

    def test_relative_size_floor_refuses_drastic_shrink(self, monkeypatch):
        """Audit 2026-05-26: refuse a swap whose new row count is below
        AIRCRAFT_DB_MIN_RATIO × prev_count. Protects against truncated
        upstream downloads that would otherwise wipe most of the DB."""
        from readsbstats import config
        conn = self.conn
        # Seed 10 existing rows; we'll then try to import 3 rows (30%) which
        # is well below the 80% floor.
        for i in range(10):
            conn.execute(
                "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
                f"VALUES ('00{i:04x}', 'REG{i}', 'A320', '', 0)"
            )
        conn.commit()
        monkeypatch.setattr(config, "AIRCRAFT_DB_MIN_RATIO", 0.8)

        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-1", "B738", "0", ""],
            ["488002", "SP-2", "B738", "0", ""],
            ["488003", "SP-3", "B738", "0", ""],
        ))
        with pytest.raises(Exception):
            db_updater.update_aircraft_db(conn)

        # Original 10 rows intact.
        n = conn.execute("SELECT COUNT(*) FROM aircraft_db").fetchone()[0]
        assert n == 10

    def test_first_ever_import_skips_size_check(self, monkeypatch):
        """When aircraft_db is empty (prev_count == 0), import succeeds
        no matter how few rows the upstream provides — there's nothing
        to compare against."""
        from readsbstats import config
        conn = self.conn
        # aircraft_db is empty (make_db seeded nothing).
        monkeypatch.setattr(config, "AIRCRAFT_DB_MIN_RATIO", 0.8)
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-1", "B738", "0", ""],
        ))
        assert db_updater.update_aircraft_db(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM aircraft_db").fetchone()[0] == 1

    def test_cache_cleared_immediately_after_write(self, monkeypatch):
        # Audit-13 A13-018: enrichment cache used to be cleared only at
        # the end of main() — a stale-cache window opened between the
        # aircraft_db write and the run-end clear. Now cleared per step.
        from readsbstats import enrichment
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _aircraft_gz(
            ["488001", "SP-NEW", "B738", "0", ""],
        ))
        # Prime the cache (with a missing hex → cached as None).
        enrichment.lookup_aircraft(conn, "488001")
        hit_before, _ = enrichment._aircraft_cache.get_cached("488001")
        assert hit_before is True  # primed
        # Run the update — cache must be cleared.
        db_updater.update_aircraft_db(conn)
        hit_after, _ = enrichment._aircraft_cache.get_cached("488001")
        assert hit_after is False  # post-clear, key absent


# ---------------------------------------------------------------------------
# update_airlines_db
# ---------------------------------------------------------------------------

class TestUpdateAirlinesDb:
    # Row order: id, name, alias, iata, icao, callsign, country, active

    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_valid_row_inserted(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "LOT Polish Airlines", r"\N", "LO", "LOT", "LOT", "Poland", "Y"],
        ))
        count = db_updater.update_airlines_db(conn)
        assert count == 1
        row = conn.execute("SELECT * FROM airlines WHERE icao_code='LOT'").fetchone()
        assert row["name"] == "LOT Polish Airlines"
        assert row["country"] == "Poland"
        assert row["active"] == 1

    def test_inactive_airline_stored_with_zero_flag(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Old Airline", r"\N", "OA", "OAL", "OAL", "Poland", "N"],
        ))
        db_updater.update_airlines_db(conn)
        row = conn.execute("SELECT active FROM airlines WHERE icao_code='OAL'").fetchone()
        assert row["active"] == 0

    def test_null_icao_code_skipped(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Unknown", r"\N", r"\N", r"\N", r"\N", r"\N", "Y"],
        ))
        assert db_updater.update_airlines_db(conn) == 0

    def test_short_icao_code_skipped(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Bad Airline", r"\N", "BA", "BA", "BA", "UK", "Y"],  # 2-char ICAO
        ))
        assert db_updater.update_airlines_db(conn) == 0

    def test_null_country_stored_as_null(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Test Air", r"\N", "TA", "TST", "TST", r"\N", "Y"],
        ))
        db_updater.update_airlines_db(conn)
        row = conn.execute("SELECT country FROM airlines WHERE icao_code='TST'").fetchone()
        assert row["country"] is None

    def test_null_iata_stored_as_null(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Test Air", r"\N", r"\N", "TST", "TST", "Poland", "Y"],
        ))
        db_updater.update_airlines_db(conn)
        row = conn.execute("SELECT iata_code FROM airlines WHERE icao_code='TST'").fetchone()
        assert row["iata_code"] is None

    def test_empty_name_skipped(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "", r"\N", "LO", "LOT", "LOT", "Poland", "Y"],
        ))
        assert db_updater.update_airlines_db(conn) == 0

    def test_replaces_previous_data_on_reimport(self, monkeypatch):
        conn = self.conn
        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "LOT Polish Airlines", r"\N", "LO", "LOT", "LOT", "Poland", "Y"],
        ))
        db_updater.update_airlines_db(conn)

        monkeypatch.setattr(db_updater, "_fetch", lambda url: _airlines_csv(
            ["1", "Ryanair", r"\N", "FR", "RYR", "RYANAIR", "Ireland", "Y"],
        ))
        db_updater.update_airlines_db(conn)

        assert conn.execute("SELECT COUNT(*) FROM airlines WHERE icao_code='LOT'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM airlines WHERE icao_code='RYR'").fetchone()[0] == 1

    def test_short_row_skipped(self, monkeypatch):
        conn = self.conn
        # Row with fewer than 8 fields
        monkeypatch.setattr(db_updater, "_fetch", lambda url: b'"1","Short","Row"')
        assert db_updater.update_airlines_db(conn) == 0



# ---------------------------------------------------------------------------
# backfill_flights
# ---------------------------------------------------------------------------

class TestBackfillFlights:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def _insert_flight(self, conn, icao, registration=None, aircraft_type=None):
        conn.execute(
            """INSERT INTO flights
               (icao_hex, registration, aircraft_type, first_seen, last_seen, total_positions)
               VALUES (?,?,?,1000000,1003600,10)""",
            (icao, registration, aircraft_type),
        )
        conn.commit()

    def test_fills_missing_registration_and_type(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) VALUES ('488001','SP-ABC','B738')"
        )
        self._insert_flight(conn, "488001")
        updated = db_updater.backfill_flights(conn)
        assert updated == 1
        row = conn.execute("SELECT registration, aircraft_type FROM flights WHERE icao_hex='488001'").fetchone()
        assert row["registration"] == "SP-ABC"
        assert row["aircraft_type"] == "B738"

    def test_does_not_overwrite_existing_registration(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) VALUES ('488001','SP-NEW','B738')"
        )
        self._insert_flight(conn, "488001", registration="SP-OLD")
        db_updater.backfill_flights(conn)
        row = conn.execute("SELECT registration FROM flights WHERE icao_hex='488001'").fetchone()
        assert row["registration"] == "SP-OLD"

    def test_does_not_overwrite_existing_type(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) VALUES ('488001','SP-ABC','A320')"
        )
        self._insert_flight(conn, "488001", aircraft_type="B738")
        db_updater.backfill_flights(conn)
        row = conn.execute("SELECT aircraft_type FROM flights WHERE icao_hex='488001'").fetchone()
        assert row["aircraft_type"] == "B738"

    def test_no_aircraft_db_match_unchanged(self):
        conn = self.conn
        self._insert_flight(conn, "ffffff")
        updated = db_updater.backfill_flights(conn)
        assert updated == 0

    def test_multiple_flights_same_icao_all_filled(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) VALUES ('488001','SP-ABC','B738')"
        )
        self._insert_flight(conn, "488001")
        conn.execute(
            """INSERT INTO flights
               (icao_hex, first_seen, last_seen, total_positions)
               VALUES ('488001', 2000000, 2003600, 5)"""
        )
        conn.commit()
        updated = db_updater.backfill_flights(conn)
        assert updated == 2


# ---------------------------------------------------------------------------
# _fetch()
# ---------------------------------------------------------------------------

class TestFetch:
    def test_fetch_returns_bytes(self, monkeypatch):
        """db_updater._fetch goes through http_safe.safe_urlopen — patch its
        opener and bypass DNS validation."""
        from readsbstats import http_safe

        class FakeResp:
            url = "https://raw.githubusercontent.com/example/data"
            headers = {}
            def read(self, _max): return b"hello"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        # Phase 9 redesign: safe_urlopen builds a fresh opener per call.
        # Bypass resolve+validate (`_resolve_and_validate`) and replace the
        # opener factory with one that returns our FakeResp.
        import urllib.parse as _up
        monkeypatch.setattr(http_safe, "validate_url", lambda url: None)
        monkeypatch.setattr(
            http_safe, "_resolve_and_validate",
            lambda url: (_up.urlparse(url), [
                (1, 1, 6, "", ("1.1.1.1", 443))
            ]),
        )
        def _fake_factory(parsed, target_ip, timeout):
            class _FakeOpener:
                def open(self, req, timeout=None):
                    return FakeResp()
            return _FakeOpener()
        monkeypatch.setattr(http_safe, "_build_pinned_opener", _fake_factory)
        result = db_updater._fetch("https://raw.githubusercontent.com/example/data")
        assert result == b"hello"

    def test_aircraft_url_is_direct_raw_githubusercontent(self):
        """The aircraft CSV URL must point at raw.githubusercontent.com so the
        SSRF-safe fetcher (which blocks redirects) can pull it without going
        through the github.com /raw/ → raw.githubusercontent.com 302."""
        assert db_updater.AIRCRAFT_CSV_URL.startswith(
            "https://raw.githubusercontent.com/"
        )


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_success(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        monkeypatch.setattr(database, "init_db", lambda path=None: None)
        monkeypatch.setattr(database, "connect", lambda path=None: database.connect.__wrapped__(db_path)
            if hasattr(database.connect, "__wrapped__") else sqlite3.connect(db_path))
        # Simplify: just patch connect to use our tmp db
        conn = database.connect(db_path)
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda: conn)
        monkeypatch.setattr(db_updater, "update_aircraft_db", lambda c: 0)
        monkeypatch.setattr(db_updater, "update_airlines_db", lambda c: 0)
        monkeypatch.setattr(db_updater, "backfill_flights", lambda c: 0)
        db_updater.main()  # should not raise

    def test_main_failure_exits(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda: conn)

        def boom(c):
            raise RuntimeError("boom")

        monkeypatch.setattr(db_updater, "update_aircraft_db", boom)
        with pytest.raises(SystemExit) as exc_info:
            db_updater.main()
        assert exc_info.value.code == 1
