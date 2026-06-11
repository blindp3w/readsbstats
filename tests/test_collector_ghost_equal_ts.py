"""Lock for the ghost-filter equal-pos_ts behaviour (Audit 2026-06-01 S).

When two consecutive polls report the same `pos_ts` for the same aircraft,
the strict `<` check at the entry of the per-aircraft loop lets the second
sample through, but the ghost filter further down (`if dt <= 0: continue`)
drops it. That is the correct readsb-semantics behaviour: equal pos_ts
means readsb has no new fix, so we should not double-insert.

The old comment said "equal timestamps are new observations" — misleading.
The reconciled comment makes the actual contract explicit. This test locks
that contract so a future "fix" doesn't reintroduce duplicate position rows
for the same pos_ts.
"""
from __future__ import annotations

import json
import time

import pytest

from readsbstats import collector, config, enrichment, notifier
from tests._helpers import make_db


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    collector._active.clear()
    collector._notified_icao.clear()
    collector._squawk_notified.clear()
    collector._last_mtime = 0.0
    enrichment.clear_cache()

    monkeypatch.setattr(notifier, "notify_military",    lambda *a, **k: None)
    monkeypatch.setattr(notifier, "notify_interesting", lambda *a, **k: None)
    monkeypatch.setattr(notifier, "notify_squawk",      lambda *a, **k: None)
    monkeypatch.setattr(notifier, "notify_watchlist",   lambda *a, **k: None)

    yield


def _write_json(path, aircraft, now):
    path.write_text(json.dumps({"now": now, "aircraft": aircraft}))


def test_equal_pos_ts_does_not_insert_second_position(monkeypatch, tmp_path):
    """Two polls report the same pos_ts for one aircraft → exactly one row."""
    conn = make_db()
    json_path = tmp_path / "aircraft.json"
    monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(json_path))
    monkeypatch.setattr("readsbstats.collector.config", config)

    now = int(time.time())
    # First poll: aircraft at (52.0, 21.0), seen_pos=0 → pos_ts = now
    _write_json(json_path, [{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
    # Bump mtime so the second poll re-reads (collector tracks last_mtime).
    import os
    os.utime(json_path, (now - 5, now - 5))
    collector._poll(conn)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1

    # Second poll: same now, different lat/lon but same pos_ts.
    # The ghost filter's `dt <= 0` must drop this, no second row inserted.
    _write_json(json_path, [{"hex": "aabbcc", "lat": 52.5, "lon": 21.5, "seen_pos": 0}], now)
    os.utime(json_path, (now + 1, now + 1))
    collector._poll(conn)

    rows = conn.execute(
        "SELECT lat / 100000.0 AS lat, lon / 100000.0 AS lon FROM positions "
        "WHERE flight_id IN "
        "(SELECT id FROM flights WHERE icao_hex='aabbcc') ORDER BY id"
    ).fetchall()
    assert len(rows) == 1, f"expected exactly 1 position row, got {len(rows)}"
    # And it must be the FIRST observation, not overwritten by the second.
    assert (rows[0]["lat"], rows[0]["lon"]) == (52.0, 21.0)

    conn.close()
