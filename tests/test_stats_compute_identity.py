"""Value-identity guard for ``api/stats._compute_stats_sync``.

The function was split (audit D1) into a thin orchestrator + per-section
``_stats_*`` helpers. Shape tests don't prove every *value* survived the split,
so this golden test deep-equals the full payload (filtered + unfiltered) against
a recorded baseline on a deterministic seed with a frozen clock.

To refresh the fixture after an *intentional* stats-output change:

    RSBS_RECORD_GOLDEN=1 python -m pytest tests/test_stats_compute_identity.py

then review the diff to ``tests/fixtures/stats_identity_golden.json`` before
committing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from readsbstats import config
from readsbstats.api import _deps, stats
from tests._helpers import make_db

_T = 1_700_000_000          # frozen "now"
_DAY = 86400
_FIXTURE = Path(__file__).parent / "fixtures" / "stats_identity_golden.json"

_FCOLS = (
    "icao_hex,callsign,registration,aircraft_type,squawk,first_seen,last_seen,"
    "max_alt_baro,max_gs,max_distance_nm,max_distance_bearing,total_positions,"
    "adsb_positions,mlat_positions,primary_source,lat_min,lat_max,lon_min,lon_max"
)


def _flight(conn, icao, cs, reg, typ, sq, fs, ls, alt, gs, dist, bearing, tp, adsb, mlat, src):
    conn.execute(
        f"INSERT INTO flights ({_FCOLS}) VALUES ({','.join('?' * 19)})",
        (icao, cs, reg, typ, sq, fs, ls, alt, gs, dist, bearing, tp, adsb, mlat, src, 0, 0, 0, 0),
    )


def _seed(conn) -> None:
    """Deterministic data that exercises every stat section."""
    T = _T
    _flight(conn, "3c6444", "DLH100", "D-AIAA", "A320", None,   T - 3600,        T - 1800,        35000, 450, 120.5, 90,  100, 95,  5,  "adsb")
    _flight(conn, "3c6445", "DLH200", "D-AIAB", "A321", "7700", T - 2 * 3600,    T - 3000,        12000, 300, 40.0,  45,  50,  50,  0,  "adsb")
    _flight(conn, "48400a", "RYR55",  "EI-DAA", "B738", None,   T - 2 * _DAY,    T - 2 * _DAY + 900, 5000, 280, 8.0,  10,  30,  20,  10, "mlat")
    _flight(conn, "48400b", "RYR66",  "EI-DAB", "B738", "7600", T - 5 * _DAY,    T - 5 * _DAY + 900, 25000, 400, 300.9, 200, 80, 80, 0, "adsb")
    _flight(conn, "400abc", "BAW12",  "G-ABCD", "A319", None,   T - 20 * _DAY,   T - 20 * _DAY + 900, 41000, 480, 250.0, 270, 200, 200, 0, "adsb")
    _flight(conn, "a12345", "UAL99",  "N12345", "B772", "7500", T - 40 * _DAY,   T - 40 * _DAY + 900, 38000, 500, 199.0, 300, 300, 300, 0, "adsb")
    _flight(conn, "f00001", "TEST1",  None,     None,   None,   T - 3 * _DAY,    T - 3 * _DAY + 900, 1500, 100, 5.0,   5,   10,  0,  10, "mlat")

    conn.execute("INSERT INTO aircraft_db (icao_hex,registration,type_code,type_desc,flags) VALUES (?,?,?,?,?)",
                 ("3c6444", "D-AIAA", "A320", "Airbus A320", 0))
    conn.execute("INSERT INTO aircraft_db (icao_hex,registration,type_code,type_desc,flags) VALUES (?,?,?,?,?)",
                 ("400abc", "G-ABCD", "A319", "Airbus A319", 1))   # military via db flags
    conn.execute("INSERT INTO adsbx_overrides (icao_hex,flags,registration,type_code,type_desc,first_seen,last_seen) "
                 "VALUES (?,?,?,?,?,?,?)", ("48400a", 2, "EI-DAA", "B738", "Boeing 737", T - _DAY, T))  # interesting
    conn.execute("INSERT INTO airlines (icao_code,name) VALUES ('DLH','Lufthansa')")
    conn.execute("INSERT INTO airlines (icao_code,name) VALUES ('RYR','Ryanair')")
    conn.execute("INSERT INTO airports (icao_code,iata_code,name,country,latitude,longitude,fetched_at) VALUES (?,?,?,?,?,?,?)",
                 ("EDDF", "FRA", "Frankfurt", "Germany", 50.0, 8.5, T))
    conn.execute("INSERT INTO airports (icao_code,iata_code,name,country,latitude,longitude,fetched_at) VALUES (?,?,?,?,?,?,?)",
                 ("EHAM", "AMS", "Schiphol", "Netherlands", 52.3, 4.7, T))
    conn.execute("INSERT INTO callsign_routes (callsign,origin_icao,dest_icao,fetched_at) VALUES (?,?,?,?)",
                 ("DLH100", "EDDF", "EHAM", T))
    conn.execute("INSERT INTO callsign_routes (callsign,origin_icao,dest_icao,fetched_at) VALUES (?,?,?,?)",
                 ("RYR55", "EHAM", "EDDF", T))
    conn.commit()


def test_compute_stats_sync_value_identity(monkeypatch):
    conn = make_db()
    _seed(conn)
    monkeypatch.setattr(_deps, "_db", conn)
    monkeypatch.setattr(stats.time, "time", lambda: _T)        # frozen now
    monkeypatch.setattr(config, "DB_PATH", "/tmp/__nonexistent_stats_db__.db")  # db_size → None
    monkeypatch.setattr(config, "RECEIVER_LAT", 52.2)
    monkeypatch.setattr(config, "RECEIVER_LON", 21.0)

    got = {
        "unfiltered": stats._compute_stats_sync(None, None),
        "filtered": stats._compute_stats_sync(_T - 10 * _DAY, _T),
    }
    got = json.loads(json.dumps(got, default=str))   # normalise Row/tuple → JSON types

    if os.environ.get("RSBS_RECORD_GOLDEN"):
        _FIXTURE.parent.mkdir(exist_ok=True)
        _FIXTURE.write_text(json.dumps(got, indent=2, sort_keys=True))
        pytest.skip("recorded golden fixture")

    expected = json.loads(_FIXTURE.read_text())
    assert got == expected


def test_source_breakdown_other_never_negative(monkeypatch):
    """adsb_pct / mlat_pct are each ROUND(…,1) in SQL, so their sum can exceed
    100 (1/15 of 16 → 6.3 + 93.8 = 100.1); `other` must clamp to >= 0 rather
    than render a negative pie slice. Audit 2026-06-20."""
    conn = make_db()
    # total=16, adsb=1, mlat=15 → adsb_pct=6.3, mlat_pct=93.8 (half-away-from-zero).
    _flight(conn, "abc123", "X1", None, None, None, _T - 3600, _T - 1800,
            1000, 100, 5.0, 90, 16, 1, 15, "mlat")
    conn.commit()
    monkeypatch.setattr(_deps, "_db", conn)
    monkeypatch.setattr(stats.time, "time", lambda: _T)
    monkeypatch.setattr(config, "DB_PATH", "/tmp/__nonexistent_stats_db__.db")
    monkeypatch.setattr(config, "RECEIVER_LAT", 52.2)
    monkeypatch.setattr(config, "RECEIVER_LON", 21.0)
    got = stats._compute_stats_sync(None, None)
    assert got["source_breakdown"]["other"] >= 0.0
