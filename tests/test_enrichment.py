"""Direct tests for readsbstats.enrichment.

Previously covered transitively via collector / web tests, which left
thread-safety of `_LRUDict` and the negative-cache behaviour of the
lookup_* helpers under-asserted.  See improvements.md #131.
"""

import sqlite3
import threading

import pytest

from readsbstats import database, enrichment
from readsbstats.enrichment import _LRUDict


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_conn():
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    # Caches are module-level singletons — must reset between tests so that
    # one test's miss-then-hit doesn't poison another's miss-only assertion.
    enrichment.clear_cache()
    yield conn
    conn.close()
    enrichment.clear_cache()


# ---------------------------------------------------------------------------
# _LRUDict — core behaviour
# ---------------------------------------------------------------------------

class TestLRUDictBasics:
    def test_get_cached_miss(self):
        cache = _LRUDict(maxsize=4)
        hit, value = cache.get_cached("missing")
        assert hit is False
        assert value is None

    def test_get_cached_hit_returns_stored_value(self):
        cache = _LRUDict(maxsize=4)
        cache.put("k", {"a": 1})
        hit, value = cache.get_cached("k")
        assert hit is True
        assert value == {"a": 1}

    def test_get_cached_hit_with_none_value_still_reports_hit(self):
        """Negative cache: None is a legitimate cached value (DB miss).
        get_cached() must distinguish 'absent' from 'present-and-None'."""
        cache = _LRUDict(maxsize=4)
        cache.put("k", None)
        hit, value = cache.get_cached("k")
        assert hit is True
        assert value is None

    def test_put_evicts_oldest_when_over_maxsize(self):
        cache = _LRUDict(maxsize=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        hit_a, _ = cache.get_cached("a")
        hit_b, val_b = cache.get_cached("b")
        hit_c, val_c = cache.get_cached("c")
        assert hit_a is False, "oldest entry 'a' should have been evicted"
        assert (hit_b, val_b) == (True, 2)
        assert (hit_c, val_c) == (True, 3)

    def test_get_cached_moves_to_end(self):
        """Recently-accessed entries are protected from eviction."""
        cache = _LRUDict(maxsize=2)
        cache.put("a", 1)
        cache.put("b", 2)
        # Touch 'a' so 'b' is now the LRU.
        cache.get_cached("a")
        cache.put("c", 3)
        # 'b' should have been evicted, 'a' kept.
        assert cache.get_cached("a") == (True, 1)
        assert cache.get_cached("b") == (False, None)
        assert cache.get_cached("c") == (True, 3)

    def test_invalidate_removes_specific_key(self):
        """Regression for audit-12 #141 — invalidate() is the public API for
        busting a single entry. Callers must not reach into ._lock / .pop()."""
        cache = _LRUDict(maxsize=4)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.invalidate("a")
        assert cache.get_cached("a") == (False, None)
        assert cache.get_cached("b") == (True, 2)

    def test_invalidate_missing_key_is_noop(self):
        cache = _LRUDict(maxsize=4)
        cache.put("a", 1)
        cache.invalidate("never-was-here")  # must not raise
        assert cache.get_cached("a") == (True, 1)

    def test_clear_locked_empties_cache(self):
        """clear_locked() is the public bulk-clear API; callers must not
        reach into ._lock / .clear()."""
        cache = _LRUDict(maxsize=4)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.clear_locked()
        assert cache.get_cached("a") == (False, None)
        assert cache.get_cached("b") == (False, None)
        assert len(cache) == 0


# ---------------------------------------------------------------------------
# _LRUDict — thread-safety
# ---------------------------------------------------------------------------

class TestLRUDictThreadSafety:
    def test_concurrent_put_and_get_does_not_crash(self):
        cache = _LRUDict(maxsize=64)
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(1000):
                    cache.put(f"k{i}", i)
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for i in range(1000):
                    cache.get_cached(f"k{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)] + \
                  [threading.Thread(target=reader) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5.0)
        stop.set()
        assert errors == [], f"concurrent ops raised: {errors!r}"
        # And cache size respects maxsize regardless of write order:
        assert len(cache) <= 64

    def test_concurrent_clear_does_not_leak_keys(self):
        cache = _LRUDict(maxsize=32)
        for i in range(32): cache.put(f"k{i}", i)
        # Clear from one thread while another reads from it.
        errors = []
        def clear():
            try:
                for _ in range(50):
                    cache.clear_locked()
            except Exception as exc:
                errors.append(exc)
        def reader():
            try:
                for _ in range(500):
                    cache.get_cached("k0")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=clear), threading.Thread(target=reader)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5.0)
        assert errors == []


# ---------------------------------------------------------------------------
# lookup_aircraft
# ---------------------------------------------------------------------------

class TestLookupAircraft:
    def test_returns_dict_on_hit(self, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?,?,?,?,?)",
            ("aabbcc", "SP-ABC", "B738", "BOEING 737-800", 0),
        )
        db_conn.commit()
        out = enrichment.lookup_aircraft(db_conn, "aabbcc")
        assert out == {
            "registration": "SP-ABC",
            "type_code": "B738",
            "type_desc": "BOEING 737-800",
            "flags": 0,
        }

    def test_returns_none_on_miss(self, db_conn):
        out = enrichment.lookup_aircraft(db_conn, "deadbe")
        assert out is None

    def test_miss_is_cached_negative(self, db_conn):
        """Second call for an unknown hex must not re-query."""
        out1 = enrichment.lookup_aircraft(db_conn, "deadbe")
        assert out1 is None
        # Insert AFTER first call — if cache works, second call still returns None.
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?,?,?,?,?)", ("deadbe", "X", "Y", "Z", 0),
        )
        db_conn.commit()
        out2 = enrichment.lookup_aircraft(db_conn, "deadbe")
        assert out2 is None, "negative result should have been cached"

    def test_hit_is_cached(self, db_conn):
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?,?,?,?,?)", ("aabbcc", "SP-ABC", "B738", "BOEING 737-800", 0),
        )
        db_conn.commit()
        out1 = enrichment.lookup_aircraft(db_conn, "aabbcc")
        # DELETE the row; cached value must still come back.
        db_conn.execute("DELETE FROM aircraft_db")
        db_conn.commit()
        out2 = enrichment.lookup_aircraft(db_conn, "aabbcc")
        assert out2 == out1


# ---------------------------------------------------------------------------
# lookup_airline
# ---------------------------------------------------------------------------

class TestLookupAirline:
    def test_returns_name_for_known_prefix(self, db_conn):
        db_conn.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES (?,?,?,?,?)",
            ("LOT", "LOT Polish Airlines", "LO", "Poland", 1),
        )
        db_conn.commit()
        assert enrichment.lookup_airline(db_conn, "LOT123") == "LOT Polish Airlines"

    def test_strips_whitespace_before_lookup(self, db_conn):
        # A leading/trailing space used to yield code=" LO" → no match
        # (audit 2026-06-15 Low — defensive).
        db_conn.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES (?,?,?,?,?)",
            ("LOT", "LOT Polish Airlines", "LO", "Poland", 1),
        )
        db_conn.commit()
        assert enrichment.lookup_airline(db_conn, " LOT123") == "LOT Polish Airlines"
        assert enrichment.lookup_airline(db_conn, "AB ") is None  # stripped len < 3

    def test_short_callsign_returns_none(self, db_conn):
        assert enrichment.lookup_airline(db_conn, "AB") is None
        assert enrichment.lookup_airline(db_conn, "") is None

    def test_none_callsign_returns_none(self, db_conn):
        assert enrichment.lookup_airline(db_conn, None) is None

    def test_unknown_prefix_returns_none_and_caches(self, db_conn):
        out1 = enrichment.lookup_airline(db_conn, "ZZZ999")
        assert out1 is None
        # Insert AFTER first call — cache should keep returning None.
        db_conn.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES (?,?,?,?,?)", ("ZZZ", "Late Insert", None, None, 1),
        )
        db_conn.commit()
        assert enrichment.lookup_airline(db_conn, "ZZZ999") is None

    def test_uppercases_prefix(self, db_conn):
        db_conn.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES (?,?,?,?,?)",
            ("RYR", "Ryanair", "FR", "Ireland", 1),
        )
        db_conn.commit()
        assert enrichment.lookup_airline(db_conn, "ryr1234") == "Ryanair"


# ---------------------------------------------------------------------------
# lookup_adsbx + invalidate_adsbx
# ---------------------------------------------------------------------------

class TestLookupAdsbx:
    def test_hit_returns_dict(self, db_conn):
        db_conn.execute(
            "INSERT INTO adsbx_overrides "
            "(icao_hex, flags, registration, type_code, type_desc, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            ("aabbcc", 1, "REG", "TYP", "DESC", 1, 2),
        )
        db_conn.commit()
        out = enrichment.lookup_adsbx(db_conn, "aabbcc")
        assert out == {
            "flags": 1, "registration": "REG", "type_code": "TYP", "type_desc": "DESC",
        }

    def test_miss_returns_none(self, db_conn):
        assert enrichment.lookup_adsbx(db_conn, "deadbe") is None

    def test_invalidate_busts_cache_for_specific_hex(self, db_conn):
        # Populate then bust.
        db_conn.execute(
            "INSERT INTO adsbx_overrides "
            "(icao_hex, flags, registration, type_code, type_desc, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            ("aabbcc", 1, None, None, None, 1, 2),
        )
        db_conn.commit()
        enrichment.lookup_adsbx(db_conn, "aabbcc")  # cache it
        # Update the row, then invalidate.
        db_conn.execute("UPDATE adsbx_overrides SET flags = ? WHERE icao_hex = ?",
                        (2, "aabbcc"))
        db_conn.commit()
        # Without invalidation, the cached flags=1 would persist:
        enrichment.invalidate_adsbx("aabbcc")
        out = enrichment.lookup_adsbx(db_conn, "aabbcc")
        assert out["flags"] == 2


# ---------------------------------------------------------------------------
# clear_cache resets all three
# ---------------------------------------------------------------------------

class TestClearCache:
    def test_clear_resets_all_three_caches(self, db_conn):
        # Seed aircraft, airline, and adsbx caches.
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES (?,?,?,?,?)", ("aabbcc", "R", "T", "D", 0),
        )
        db_conn.execute(
            "INSERT INTO airlines (icao_code, name, iata_code, country, active) "
            "VALUES (?,?,?,?,?)", ("LOT", "LOT", "LO", "PL", 1),
        )
        db_conn.execute(
            "INSERT INTO adsbx_overrides "
            "(icao_hex, flags, registration, type_code, type_desc, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?)", ("aabbcc", 1, None, None, None, 1, 2),
        )
        db_conn.commit()
        enrichment.lookup_aircraft(db_conn, "aabbcc")
        enrichment.lookup_airline(db_conn, "LOT123")
        enrichment.lookup_adsbx(db_conn, "aabbcc")
        # All three internal caches now non-empty.
        assert len(enrichment._aircraft_cache) > 0
        assert len(enrichment._airline_cache) > 0
        assert len(enrichment._adsbx_cache) > 0
        enrichment.clear_cache()
        assert len(enrichment._aircraft_cache) == 0
        assert len(enrichment._airline_cache) == 0
        assert len(enrichment._adsbx_cache) == 0
