"""
readsbstats — aircraft / airline enrichment helpers.

Provides fast in-process lookups against locally downloaded databases:
  - aircraft_db  (from tar1090-db CSV) → registration, type code, description
  - airlines     (from OpenFlights)    → full airline name by ICAO code
"""

import sqlite3
import threading
from collections import OrderedDict

# ---------------------------------------------------------------------------
# In-memory caches — populated on first lookup, evict oldest when full.
# The underlying tables are updated weekly by db_updater.py.
# ---------------------------------------------------------------------------
_MAX_AIRCRAFT = 50_000  # ~620k in DB; only cache what we actually see
_MAX_AIRLINE  = 2_000   # ~5k airlines; 2k covers heavy traffic
_MAX_ADSBX   = 10_000


class _LRUDict(OrderedDict):
    """Thread-safe OrderedDict that evicts the oldest entry when maxsize is exceeded."""

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get_cached(self, key):
        """Return (True, value) if key is cached, else (False, None)."""
        with self._lock:
            if key in self:
                self.move_to_end(key)
                return True, self[key]
        return False, None

    def put(self, key, value):
        with self._lock:
            self[key] = value
            self.move_to_end(key)
            if len(self) > self._maxsize:
                self.popitem(last=False)

    def invalidate(self, key) -> None:
        """Remove `key` from the cache if present (no-op otherwise)."""
        with self._lock:
            self.pop(key, None)

    def clear_locked(self) -> None:
        """Atomic bulk-clear. Use this rather than reaching into ._lock / .clear()."""
        with self._lock:
            self.clear()


_aircraft_cache = _LRUDict(_MAX_AIRCRAFT)
_airline_cache  = _LRUDict(_MAX_AIRLINE)
_adsbx_cache    = _LRUDict(_MAX_ADSBX)


def lookup_aircraft(conn: sqlite3.Connection, icao_hex: str) -> dict | None:
    """
    Return {registration, type_code, type_desc} for the given ICAO hex, or None.
    Result is cached in-process for the lifetime of the collector.
    """
    hit, result = _aircraft_cache.get_cached(icao_hex)
    if hit:
        return result

    row = conn.execute(
        "SELECT registration, type_code, type_desc, flags FROM aircraft_db WHERE icao_hex = ?",
        (icao_hex,),
    ).fetchone()

    result = dict(row) if row else None
    _aircraft_cache.put(icao_hex, result)
    return result


def lookup_airline(conn: sqlite3.Connection, callsign: str | None) -> str | None:
    """
    Return the full airline name for a callsign (e.g. 'LOT123' → 'LOT Polish Airlines').
    Uses the first 3 uppercase characters as the ICAO airline code.
    """
    if not callsign or len(callsign) < 3:
        return None

    code = callsign[:3].upper()

    hit, result = _airline_cache.get_cached(code)
    if hit:
        return result

    row = conn.execute(
        "SELECT name FROM airlines WHERE icao_code = ?", (code,)
    ).fetchone()

    result = row["name"] if row else None
    _airline_cache.put(code, result)
    return result


def lookup_adsbx(conn: sqlite3.Connection, icao_hex: str) -> dict | None:
    """
    Return {flags, registration, type_code, type_desc} from adsbx_overrides, or None.
    Cached in-process for the lifetime of the collector.
    """
    hit, result = _adsbx_cache.get_cached(icao_hex)
    if hit:
        return result

    row = conn.execute(
        "SELECT flags, registration, type_code, type_desc "
        "FROM adsbx_overrides WHERE icao_hex = ?",
        (icao_hex,),
    ).fetchone()

    result = dict(row) if row else None
    _adsbx_cache.put(icao_hex, result)
    return result


def invalidate_adsbx(icao_hex: str) -> None:
    """Bust the cache for a single hex after adsbx_enricher upserts it."""
    _adsbx_cache.invalidate(icao_hex)


def clear_cache() -> None:
    """Clear all caches — call after db_updater runs to pick up fresh data."""
    _aircraft_cache.clear_locked()
    _airline_cache.clear_locked()
    _adsbx_cache.clear_locked()
