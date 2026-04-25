"""
Tests for adsbx_enricher.py — ADSBexchange military/flag enrichment.
Uses an in-memory SQLite database; no real network I/O.
"""

import importlib
import sqlite3
import time

import pytest

from readsbstats import config, database, enrichment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def insert_flight(conn, *, icao="aabbcc", callsign="LOT123",
                  first_seen=1_000_000, last_seen=1_003_600, active=False):
    cur = conn.execute(
        """INSERT INTO flights
           (icao_hex, callsign, first_seen, last_seen, total_positions,
            lat_min, lat_max, lon_min, lon_max)
           VALUES (?,?,?,?,10,0,0,0,0)""",
        (icao, callsign, first_seen, last_seen),
    )
    fid = cur.lastrowid
    if active:
        conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
            (icao, fid, last_seen),
        )
    conn.commit()
    return fid


# ---------------------------------------------------------------------------
# _parse_area_response
# ---------------------------------------------------------------------------

class TestParseAreaResponse:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import adsbx_enricher
        importlib.reload(adsbx_enricher)
        self.parse = adsbx_enricher._parse_area_response
        yield

    def test_valid_military_aircraft(self):
        data = {"ac": [
            {"hex": "3C6752", "dbFlags": 1, "r": "98+07", "t": "EF2K",
             "desc": "Eurofighter Typhoon", "flight": "GAF123"},
        ]}
        results = self.parse(data)
        assert len(results) == 1
        assert results[0]["icao_hex"] == "3c6752"
        assert results[0]["flags"] == 1
        assert results[0]["registration"] == "98+07"
        assert results[0]["type_code"] == "EF2K"
        assert results[0]["type_desc"] == "Eurofighter Typhoon"

    def test_multiple_aircraft(self):
        data = {"ac": [
            {"hex": "aaa111", "dbFlags": 1, "r": "MIL-1", "t": "F16", "desc": "F-16"},
            {"hex": "bbb222", "dbFlags": 0, "r": "SP-LRA", "t": "B738", "desc": "Boeing 737-800"},
            {"hex": "ccc333", "dbFlags": 2, "r": None, "t": "G5", "desc": "Gulfstream V"},
        ]}
        results = self.parse(data)
        assert len(results) == 3
        assert results[0]["flags"] == 1
        assert results[1]["flags"] == 0
        assert results[2]["flags"] == 2

    def test_empty_ac_array(self):
        assert self.parse({"ac": []}) == []

    def test_missing_ac_key(self):
        assert self.parse({}) == []
        assert self.parse({"ac": None}) == []

    def test_hex_stripped_and_lowered(self):
        data = {"ac": [{"hex": " 3C6752 ", "dbFlags": 1, "r": "X", "t": None, "desc": None}]}
        results = self.parse(data)
        assert results[0]["icao_hex"] == "3c6752"

    def test_skips_entry_without_hex(self):
        data = {"ac": [
            {"dbFlags": 1, "r": "X", "t": "F16", "desc": "F-16"},
            {"hex": "", "dbFlags": 1, "r": "Y", "t": "F16", "desc": "F-16"},
        ]}
        assert self.parse(data) == []

    def test_skips_entry_with_no_useful_data(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": 0, "r": None, "t": None, "desc": None}]}
        assert self.parse(data) == []

    def test_dbflags_string_parsed(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": "1", "r": "X", "t": None, "desc": None}]}
        results = self.parse(data)
        assert results[0]["flags"] == 1

    def test_dbflags_invalid_defaults_to_zero(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": "bad", "r": "X", "t": None, "desc": None}]}
        results = self.parse(data)
        assert results[0]["flags"] == 0

    def test_dbflags_none_defaults_to_zero(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": None, "r": "SP-LRA", "t": None, "desc": None}]}
        results = self.parse(data)
        assert results[0]["flags"] == 0

    def test_registration_and_type_stripped(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": 1, "r": " SP-LRA ", "t": " B738 ", "desc": " Boeing "}]}
        r = self.parse(data)[0]
        assert r["registration"] == "SP-LRA"
        assert r["type_code"] == "B738"
        assert r["type_desc"] == "Boeing"

    def test_combined_flags(self):
        data = {"ac": [{"hex": "aaa111", "dbFlags": 5, "r": "X", "t": None, "desc": None}]}
        results = self.parse(data)
        assert results[0]["flags"] == 5  # military(1) + PIA(4)


# ---------------------------------------------------------------------------
# _upsert_overrides
# ---------------------------------------------------------------------------

class TestUpsertOverrides:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import adsbx_enricher
        importlib.reload(adsbx_enricher)
        importlib.reload(enrichment)
        self.enricher = adsbx_enricher
        self.conn = make_db()
        yield
        self.conn.close()

    def test_insert_new_entry(self):
        entries = [{"icao_hex": "aaa111", "flags": 1,
                    "registration": "98+07", "type_code": "EF2K",
                    "type_desc": "Eurofighter Typhoon"}]
        count = self.enricher._upsert_overrides(self.conn, entries)
        assert count == 1
        row = self.conn.execute(
            "SELECT * FROM adsbx_overrides WHERE icao_hex = 'aaa111'"
        ).fetchone()
        assert row is not None
        assert row["flags"] == 1
        assert row["registration"] == "98+07"
        assert row["type_code"] == "EF2K"
        assert row["first_seen"] > 0
        assert row["last_seen"] == row["first_seen"]

    def test_update_existing_entry_updates_flags_and_last_seen(self):
        entries = [{"icao_hex": "aaa111", "flags": 1,
                    "registration": "98+07", "type_code": "EF2K",
                    "type_desc": "Eurofighter"}]
        self.enricher._upsert_overrides(self.conn, entries)
        first = self.conn.execute(
            "SELECT first_seen FROM adsbx_overrides WHERE icao_hex = 'aaa111'"
        ).fetchone()["first_seen"]

        # Update with new flags
        entries2 = [{"icao_hex": "aaa111", "flags": 3,
                     "registration": None, "type_code": None,
                     "type_desc": None}]
        self.enricher._upsert_overrides(self.conn, entries2)
        row = self.conn.execute(
            "SELECT * FROM adsbx_overrides WHERE icao_hex = 'aaa111'"
        ).fetchone()
        assert row["flags"] == 3
        assert row["registration"] == "98+07"  # kept from first insert
        assert row["type_code"] == "EF2K"      # kept from first insert
        assert row["first_seen"] == first       # unchanged

    def test_upsert_invalidates_enrichment_cache(self):
        enrichment._adsbx_cache["aaa111"] = {"flags": 0}
        entries = [{"icao_hex": "aaa111", "flags": 1,
                    "registration": "X", "type_code": None, "type_desc": None}]
        self.enricher._upsert_overrides(self.conn, entries)
        assert "aaa111" not in enrichment._adsbx_cache

    def test_empty_entries_returns_zero(self):
        assert self.enricher._upsert_overrides(self.conn, []) == 0


# ---------------------------------------------------------------------------
# enrichment.lookup_adsbx
# ---------------------------------------------------------------------------

class TestLookupAdsbx:
    @pytest.fixture(autouse=True)
    def setup(self):
        importlib.reload(enrichment)
        self.conn = make_db()
        yield
        self.conn.close()

    def test_lookup_existing(self):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO adsbx_overrides VALUES (?,?,?,?,?,?,?)",
            ("aaa111", 1, "98+07", "EF2K", "Eurofighter", now, now),
        )
        self.conn.commit()
        result = enrichment.lookup_adsbx(self.conn, "aaa111")
        assert result is not None
        assert result["flags"] == 1
        assert result["registration"] == "98+07"

    def test_lookup_missing_returns_none(self):
        result = enrichment.lookup_adsbx(self.conn, "zzz999")
        assert result is None

    def test_lookup_caches_result(self):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO adsbx_overrides VALUES (?,?,?,?,?,?,?)",
            ("aaa111", 1, "X", "F16", "F-16", now, now),
        )
        self.conn.commit()
        enrichment.lookup_adsbx(self.conn, "aaa111")
        assert "aaa111" in enrichment._adsbx_cache

    def test_lookup_caches_none_for_missing(self):
        enrichment.lookup_adsbx(self.conn, "zzz999")
        assert "zzz999" in enrichment._adsbx_cache
        assert enrichment._adsbx_cache["zzz999"] is None

    def test_invalidate_busts_cache(self):
        enrichment._adsbx_cache["aaa111"] = {"flags": 1}
        enrichment.invalidate_adsbx("aaa111")
        assert "aaa111" not in enrichment._adsbx_cache

    def test_clear_cache_clears_adsbx(self):
        enrichment._adsbx_cache["aaa111"] = {"flags": 1}
        enrichment.clear_cache()
        assert "aaa111" not in enrichment._adsbx_cache


# ---------------------------------------------------------------------------
# _fetch_area — mock HTTP
# ---------------------------------------------------------------------------

class TestFetchArea:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import adsbx_enricher
        importlib.reload(adsbx_enricher)
        self.enricher = adsbx_enricher
        yield

    def test_fetch_raises_transient_on_network_error(self, monkeypatch):
        def mock_get(*args, **kwargs):
            raise ConnectionError("network down")

        import httpx
        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient(mock_get))

        with pytest.raises(self.enricher._TransientError):
            self.enricher._fetch_area()

    def test_fetch_raises_transient_on_http_error(self, monkeypatch):
        def mock_get(*args, **kwargs):
            resp = httpx.Response(429, request=httpx.Request("GET", "http://test"))
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)

        import httpx
        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient(mock_get))

        with pytest.raises(self.enricher._TransientError):
            self.enricher._fetch_area()


class _MockClient:
    """Minimal mock for httpx.Client context manager."""
    def __init__(self, get_fn):
        self._get = get_fn
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def get(self, *args, **kwargs):
        return self._get(*args, **kwargs)


# ---------------------------------------------------------------------------
# _poll_area — integration test with mocked HTTP
# ---------------------------------------------------------------------------

class TestPollArea:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import adsbx_enricher
        importlib.reload(adsbx_enricher)
        importlib.reload(enrichment)
        self.enricher = adsbx_enricher
        self.conn = make_db()
        yield
        self.conn.close()

    def test_poll_upserts_military_aircraft(self, monkeypatch):
        monkeypatch.setattr(
            self.enricher, "_fetch_area",
            lambda: {"ac": [
                {"hex": "aaa111", "dbFlags": 1, "r": "98+07", "t": "EF2K",
                 "desc": "Eurofighter Typhoon", "flight": "GAF123"},
                {"hex": "bbb222", "dbFlags": 0, "r": "SP-LRA", "t": "B738",
                 "desc": "Boeing 737-800"},
            ]},
        )
        count = self.enricher._poll_area(self.conn)
        assert count == 2

        row = self.conn.execute(
            "SELECT * FROM adsbx_overrides WHERE icao_hex = 'aaa111'"
        ).fetchone()
        assert row["flags"] == 1
        assert row["registration"] == "98+07"

    def test_poll_skips_empty_response(self, monkeypatch):
        monkeypatch.setattr(self.enricher, "_fetch_area", lambda: {"ac": []})
        count = self.enricher._poll_area(self.conn)
        assert count == 0

    def test_poll_transient_error_propagates(self, monkeypatch):
        def fail():
            raise self.enricher._TransientError("test")
        monkeypatch.setattr(self.enricher, "_fetch_area", fail)
        with pytest.raises(self.enricher._TransientError):
            self.enricher._poll_area(self.conn)


# ---------------------------------------------------------------------------
# start_background_enricher — feature toggle
# ---------------------------------------------------------------------------

class TestStartBackgroundEnricher:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import adsbx_enricher
        importlib.reload(adsbx_enricher)
        self.enricher = adsbx_enricher
        yield

    def test_disabled_when_not_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "ADSBX_ENABLED", False)
        result = self.enricher.start_background_enricher()
        assert result is None

    def test_enabled_when_flag_set(self, monkeypatch):
        monkeypatch.setattr(config, "ADSBX_ENABLED", True)
        # Mock run_enricher_loop to avoid actual loop
        monkeypatch.setattr(self.enricher, "run_enricher_loop", lambda db_path: None)
        t = self.enricher.start_background_enricher()
        assert t is not None
        assert t.daemon is True
        assert t.name == "adsbx-enricher"
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Collector _enrich merge — simulated
# ---------------------------------------------------------------------------

class TestCollectorEnrichMerge:
    """Verify that enrichment.lookup_adsbx data merges correctly with aircraft_db."""

    @pytest.fixture(autouse=True)
    def setup(self):
        importlib.reload(enrichment)
        self.conn = make_db()
        yield
        self.conn.close()

    def test_merge_flags_or(self):
        """ADSBx military flag should OR-merge with tar1090-db flags."""
        # tar1090-db: interesting only
        self.conn.execute(
            "INSERT INTO aircraft_db VALUES (?,?,?,?,?)",
            ("aaa111", "SP-LRA", "B738", "Boeing 737-800", 2),
        )
        # ADSBx: military
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO adsbx_overrides VALUES (?,?,?,?,?,?,?)",
            ("aaa111", 1, None, None, None, now, now),
        )
        self.conn.commit()

        db_row = enrichment.lookup_aircraft(self.conn, "aaa111")
        adsbx_row = enrichment.lookup_adsbx(self.conn, "aaa111")
        merged_flags = (db_row.get("flags") or 0) | (adsbx_row.get("flags") or 0)
        assert merged_flags == 3  # military(1) + interesting(2)

    def test_adsbx_fills_missing_registration(self):
        """ADSBx registration used when tar1090-db has none."""
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO adsbx_overrides VALUES (?,?,?,?,?,?,?)",
            ("unknown1", 1, "98+07", "EF2K", "Eurofighter", now, now),
        )
        self.conn.commit()

        db_row = enrichment.lookup_aircraft(self.conn, "unknown1")
        adsbx_row = enrichment.lookup_adsbx(self.conn, "unknown1")
        assert db_row is None
        assert adsbx_row is not None
        assert adsbx_row["registration"] == "98+07"

    def test_tar1090_takes_priority_for_registration(self):
        """tar1090-db registration wins over ADSBx when both present."""
        self.conn.execute(
            "INSERT INTO aircraft_db VALUES (?,?,?,?,?)",
            ("aaa111", "SP-LRA", "B738", "Boeing 737-800", 0),
        )
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO adsbx_overrides VALUES (?,?,?,?,?,?,?)",
            ("aaa111", 0, "WRONG", "WRONG", "Wrong", now, now),
        )
        self.conn.commit()

        db_row = enrichment.lookup_aircraft(self.conn, "aaa111")
        adsbx_row = enrichment.lookup_adsbx(self.conn, "aaa111")
        reg = db_row.get("registration") or adsbx_row.get("registration")
        assert reg == "SP-LRA"  # tar1090-db wins
