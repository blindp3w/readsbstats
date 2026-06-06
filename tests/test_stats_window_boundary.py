"""Regression for stats 24h/7d window boundary (Audit 2026-06-01 S).

`api/stats.py` previously used `first_seen > cutoff` for `flights_24h` and
`flights_7d`, while `flights_prev_*` used `first_seen > ? AND first_seen <= ?`.
A flight at exactly the cutoff second was excluded from current and counted
in previous (1-flight drift in the Stats page trend delta).

Fix: half-open [lo, hi) for both windows — `>=` lower bound, `<` upper bound
— matching `_deps._build_date_filter`'s convention.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from readsbstats import cache, enrichment, web
from readsbstats.api import _deps
from readsbstats.api import stats as stats_mod
from tests._helpers import make_db


@pytest.fixture()
def db_conn():
    conn = make_db()
    enrichment.clear_cache()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    monkeypatch.setattr(_deps, "_db", db_conn)
    cache._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True,
                    headers={"X-Requested-With": "XMLHttpRequest"}) as c:
        yield c


def _insert(conn, icao, first_seen):
    conn.execute(
        "INSERT INTO flights (icao_hex, callsign, first_seen, last_seen, "
        "max_distance_nm, total_positions, adsb_positions, mlat_positions, "
        "primary_source, lat_min, lat_max, lon_min, lon_max) "
        "VALUES (?, 'LOT123', ?, ?, 100.0, 10, 9, 1, 'adsb', 0, 0, 0, 0)",
        (icao, first_seen, first_seen + 600),
    )
    conn.commit()


class TestStatsWindowBoundary:
    """flights_24h must include the flight at exactly `cutoff_24h`, and
    flights_prev_24h must exclude it (half-open [lo, hi))."""

    def test_flight_at_cutoff_24h_counts_in_current_not_previous(
        self, client, db_conn,
    ):
        now = int(time.time())
        cutoff_24h = now - 86400
        # Three flights: at cutoff, one second inside, and a day earlier.
        _insert(db_conn, "aa0001", cutoff_24h)           # at boundary
        _insert(db_conn, "aa0002", cutoff_24h + 1)       # inside 24h
        _insert(db_conn, "aa0003", cutoff_24h - 86400)   # solidly in prev_24h

        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        # Current is exposed as `flights_last_24h` at the top level; previous
        # is `trends.flights_24h_prev`.
        assert data.get("flights_last_24h") == 2, (
            f"expected boundary flight included; got flights_last_24h="
            f"{data.get('flights_last_24h')}"
        )
        assert data.get("trends", {}).get("flights_24h_prev") == 1, (
            f"expected prev_24h = 1 (only the day-old flight); got "
            f"{data.get('trends')}"
        )

    def test_flight_at_cutoff_7d_counts_in_current_not_previous(
        self, client, db_conn,
    ):
        now = int(time.time())
        cutoff_7d = now - 7 * 86400
        _insert(db_conn, "bb0001", cutoff_7d)             # at 7d boundary
        _insert(db_conn, "bb0002", cutoff_7d - 7 * 86400) # solidly in prev_7d

        r = client.get("/api/stats")
        data = r.json()
        assert data.get("flights_last_7d") == 1, (
            f"expected boundary flight in flights_last_7d; got "
            f"{data.get('flights_last_7d')}"
        )
        assert data.get("trends", {}).get("flights_7d_prev") == 1

    def test_filtered_branch_boundary_uses_same_operators(
        self, client, db_conn,
    ):
        """The filtered (`?from=&to=`) branch computes flights_24h /
        flights_7d from a separate `live` sub-query in stats.py. The W-7
        operator fix touches both blocks; lock the filtered path too so a
        future copy-paste regression in one block doesn't slip past CI.
        """
        now = int(time.time())
        cutoff_24h = now - 86400
        _insert(db_conn, "cc0001", cutoff_24h)         # at 24h boundary
        _insert(db_conn, "cc0002", cutoff_24h - 86400) # in prev_24h

        # An all-encompassing range forces the filtered branch without
        # excluding either flight from the unrelated `agg` totals.
        r = client.get(f"/api/stats?from=0&to={now + 1}")
        assert r.status_code == 200
        data = r.json()
        assert data.get("flights_last_24h") == 1, (
            f"filtered-branch boundary regression; got flights_last_24h="
            f"{data.get('flights_last_24h')}"
        )
        assert data.get("trends", {}).get("flights_24h_prev") == 1


class TestFilteredStatsCoalescing:
    """PERF-1: concurrent identical filtered-stats requests must coalesce onto a
    single compute under `cache._stats_compute_lock` (the same double-checked
    cache pattern the all-time path uses), so N viewers of one custom window
    don't each run the ~15-query GROUP-BY pass."""

    def test_warm_cache_does_not_recompute(self, client, db_conn, monkeypatch):
        """Deterministic half of the contract: the second request for the same
        filtered window is served from cache and never re-enters the compute."""
        now = int(time.time())
        _insert(db_conn, "dd0001", now - 3600)

        calls = {"n": 0}
        real = stats_mod._compute_stats_sync

        def spy(from_ts, to_ts):
            calls["n"] += 1
            return real(from_ts, to_ts)

        monkeypatch.setattr(stats_mod, "_compute_stats_sync", spy)

        url = f"/api/stats?from=0&to={now + 1}"
        assert client.get(url).status_code == 200
        assert calls["n"] == 1, "first request must compute exactly once"
        # Second identical request: warm cache → no recompute.
        assert client.get(url).status_code == 200
        assert calls["n"] == 1, "warm-cache filtered request must not recompute"

    def test_concurrent_identical_filtered_requests_compute_once(
        self, client, db_conn, monkeypatch,
    ):
        """Two threads hitting the SAME cold filtered window must invoke the
        underlying compute exactly once — the lock serializes the cold compute
        and the loser re-reads the freshly written cache."""
        import threading

        now = int(time.time())
        _insert(db_conn, "ee0001", now - 3600)

        calls = {"n": 0}
        lock = threading.Lock()
        real = stats_mod._compute_stats_sync
        start = threading.Barrier(2)

        def spy(from_ts, to_ts):
            with lock:
                calls["n"] += 1
            # Hold the compute long enough that the second thread is guaranteed
            # to be waiting on _stats_compute_lock before this one writes cache.
            time.sleep(0.2)
            return real(from_ts, to_ts)

        monkeypatch.setattr(stats_mod, "_compute_stats_sync", spy)

        url = f"/api/stats?from=0&to={now + 1}"
        results: list[int] = []

        def hit():
            start.wait(timeout=5)
            results.append(client.get(url).status_code)

        threads = [threading.Thread(target=hit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [200, 200]
        assert calls["n"] == 1, (
            f"concurrent identical filtered requests must coalesce to one "
            f"compute; got {calls['n']}"
        )
