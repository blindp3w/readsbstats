"""
Tests for collector.py — pure functions and DB-backed flight logic.
Uses an in-memory SQLite database; no real aircraft.json or network I/O.
"""

import datetime as _real_dt
import json
import math
import os
import signal
import sqlite3
import tempfile
import time

import pytest

from readsbstats import config, database, enrichment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from tests._helpers import insert_position, make_db  # noqa: E402 — kept under section header


def _reset_telegram_state():
    """Reset notifier telegram_enabled() cache between tests."""
    from readsbstats import notifier
    notifier._tg_enabled = None
    notifier._tg_validated = False


def _reset_collector_state():
    """Clear all module-level globals in collector between tests."""
    from readsbstats import collector
    collector._active.clear()
    collector._notified_icao.clear()
    collector._squawk_notified.clear()
    collector._last_mtime = 0.0
    _reset_telegram_state()


# ---------------------------------------------------------------------------
# _sd_notify / _shutdown
# ---------------------------------------------------------------------------

class TestSdNotify:
    def test_noop_without_notify_socket(self, monkeypatch):
        from readsbstats import collector
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        collector._sd_notify("READY=1")  # should not raise

    def test_sends_to_socket(self, monkeypatch):
        from readsbstats import collector
        import socket as _socket
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp(dir="/tmp")
        sock_path = os.path.join(tmpdir, "n.sock")
        server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        try:
            server.bind(sock_path)
            monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
            collector._sd_notify("WATCHDOG=1")
            data = server.recv(256)
            assert data == b"WATCHDOG=1"
        finally:
            server.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_abstract_socket(self, monkeypatch):
        """@ prefix should be converted to null byte for abstract sockets."""
        from readsbstats import collector
        monkeypatch.setenv("NOTIFY_SOCKET", "@/test/notify")
        try:
            collector._sd_notify("READY=1")
        except OSError:
            pass  # expected — no listener


class TestShutdown:
    def test_sets_running_false(self):
        from readsbstats import collector
        collector._running = True
        collector._shutdown(signal.SIGTERM, None)
        assert collector._running is False
        collector._running = True  # reset


class TestWatchdogLoop:
    def test_emits_watchdog_then_stops_on_running_false(self, monkeypatch):
        """Watchdog loop must emit at least one WATCHDOG=1 and exit when
        _running goes False, without taking another full sleep cycle."""
        from readsbstats import collector
        import threading as _t
        sent: list[str] = []
        monkeypatch.setattr(collector, "_sd_notify", lambda m: sent.append(m))
        # Compress the interval so the test stays fast.
        monkeypatch.setattr(collector, "_WATCHDOG_INTERVAL_SEC", 1)
        collector._running = True
        thread = _t.Thread(target=collector._watchdog_loop, daemon=True)
        thread.start()
        # Wait for first beat.
        for _ in range(20):
            if sent:
                break
            time.sleep(0.05)
        assert sent and sent[0] == "WATCHDOG=1"
        collector._running = False
        thread.join(timeout=3)
        assert not thread.is_alive()
        collector._running = True  # reset for other tests

    def test_does_not_emit_when_running_false_at_entry(self, monkeypatch):
        from readsbstats import collector
        sent: list[str] = []
        monkeypatch.setattr(collector, "_sd_notify", lambda m: sent.append(m))
        collector._running = False
        try:
            collector._watchdog_loop()
        finally:
            collector._running = True
        assert sent == []


# ---------------------------------------------------------------------------
# haversine_nm
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_is_zero(self):
        from readsbstats.collector import haversine_nm
        assert haversine_nm(52.225, 20.940, 52.225, 20.940) == pytest.approx(0.0, abs=1e-9)

    def test_symmetry(self):
        from readsbstats.collector import haversine_nm
        a = haversine_nm(52.225, 20.940, 48.853, 2.350)   # Warsaw → Paris
        b = haversine_nm(48.853, 2.350, 52.225, 20.940)   # Paris → Warsaw
        assert a == pytest.approx(b, rel=1e-9)

    def test_one_degree_latitude(self):
        """1° of latitude ≈ 60 NM (within 0.2 NM tolerance)."""
        from readsbstats.collector import haversine_nm
        d = haversine_nm(52.0, 20.0, 53.0, 20.0)
        assert 59.8 < d < 60.2

    def test_known_distance_warsaw_london(self):
        """Warsaw → London Heathrow ≈ 1465 km ≈ 791 NM (±5 NM)."""
        from readsbstats.collector import haversine_nm
        d = haversine_nm(52.225, 20.940, 51.477, -0.461)
        assert 786 < d < 796

    def test_antipodal_points(self):
        """Opposite sides of the globe ≈ π × R_earth."""
        from readsbstats.collector import haversine_nm
        R = 3440.065
        d = haversine_nm(0.0, 0.0, 0.0, 180.0)
        assert d == pytest.approx(math.pi * R, abs=0.5)

    def test_positive_result(self):
        from readsbstats.collector import haversine_nm
        assert haversine_nm(0.0, 0.0, 1.0, 1.0) > 0

    def test_north_pole_to_south_pole(self):
        from readsbstats.collector import haversine_nm
        R = 3440.065
        d = haversine_nm(90.0, 0.0, -90.0, 0.0)
        assert d == pytest.approx(math.pi * R, abs=0.5)

    def test_antimeridian_crossing(self):
        """Distance across the antimeridian (lon 179 → -179) should be small."""
        from readsbstats.collector import haversine_nm
        d = haversine_nm(0.0, 179.0, 0.0, -179.0)
        assert d < 130  # ~2 degrees at equator ≈ 120 NM

    def test_same_point_at_pole(self):
        from readsbstats.collector import haversine_nm
        assert haversine_nm(90.0, 45.0, 90.0, -135.0) == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------

class TestIsAdsb:
    @pytest.mark.parametrize("src", ["adsb_icao", "adsb_icao_nt", "adsr_icao", "adsc"])
    def test_true(self, src):
        from readsbstats.collector import _is_adsb
        assert _is_adsb(src) is True

    @pytest.mark.parametrize("src", ["mlat", "mode_s", "other", None, ""])
    def test_false(self, src):
        from readsbstats.collector import _is_adsb
        assert _is_adsb(src) is False


class TestIsMlat:
    def test_mlat_true(self):
        from readsbstats.collector import _is_mlat
        assert _is_mlat("mlat") is True

    @pytest.mark.parametrize("src", ["adsb_icao", "mode_s", None, ""])
    def test_others_false(self, src):
        from readsbstats.collector import _is_mlat
        assert _is_mlat(src) is False


# ---------------------------------------------------------------------------
# _primary_source
# ---------------------------------------------------------------------------

class TestPrimarySource:
    def test_zero_total_returns_other(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(0, 0, 0) == "other"

    def test_100_pct_adsb(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(10, 0, 10) == "adsb"

    def test_exactly_80_pct_adsb(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(8, 0, 10) == "adsb"

    def test_just_below_80_pct_adsb_falls_to_mixed(self):
        from readsbstats.collector import _primary_source
        # 79/100 = 0.79, mlat=0 so adsb+mlat=0.79 >= 0.5 → mixed
        assert _primary_source(79, 0, 100) == "mixed"

    def test_100_pct_mlat(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(0, 10, 10) == "mlat"

    def test_exactly_80_pct_mlat(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(0, 8, 10) == "mlat"

    def test_just_below_80_pct_mlat_falls_to_mixed(self):
        from readsbstats.collector import _primary_source
        assert _primary_source(0, 79, 100) == "mixed"

    def test_mixed_adsb_plus_mlat_above_50_pct(self):
        from readsbstats.collector import _primary_source
        # adsb=3, mlat=3, total=10 → combined=60% → mixed
        assert _primary_source(3, 3, 10) == "mixed"

    def test_exactly_50_pct_combined(self):
        from readsbstats.collector import _primary_source
        # adsb=3, mlat=2, total=10 → combined=50% → mixed
        assert _primary_source(3, 2, 10) == "mixed"

    def test_below_50_pct_combined_returns_other(self):
        from readsbstats.collector import _primary_source
        # adsb=2, mlat=2, total=10 → combined=40% → other
        assert _primary_source(2, 2, 10) == "other"

    def test_adsb_checked_before_mlat(self):
        from readsbstats.collector import _primary_source
        # Both adsb and mlat >= 80% is impossible, but adsb branch is first
        # Verify adsb wins when both are high (degenerate case)
        assert _primary_source(9, 9, 10) == "adsb"


# ---------------------------------------------------------------------------
# _open_flight / _close_flight integration (in-memory DB)
# ---------------------------------------------------------------------------

class TestOpenCloseFlight:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_open_flight_creates_row(self):
        from readsbstats.collector import _open_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, "LOT123", "SP-ABC", "B738",
            None, None, 52.0, 21.0, 35000, 450.0, "adsb_icao", 150.0, 45.0,
        )
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is not None
        assert row["icao_hex"] == "aabbcc"
        assert row["callsign"] == "LOT123"
        assert row["max_distance_nm"] == 150.0

    def test_open_flight_registers_active(self):
        from readsbstats.collector import _open_flight, _active
        _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        assert "aabbcc" in _active

    def test_open_flight_inserts_active_flights_row(self):
        from readsbstats.collector import _open_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        row = self.conn.execute(
            "SELECT * FROM active_flights WHERE icao_hex = ?", ("aabbcc",)
        ).fetchone()
        assert row is not None
        assert row["flight_id"] == fid

    def test_close_flight_deletes_if_too_few_positions(self):
        """Flight with < MIN_POSITIONS_KEEP total_positions must be deleted on close."""
        from readsbstats import config
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        # total_positions stays 0, which is < MIN_POSITIONS_KEEP (default 2)
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is None

    def test_close_flight_sets_primary_source_adsb(self):
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        # Manually set position counts as if 9 ADS-B + 1 other = total 10
        self.conn.execute(
            "UPDATE flights SET total_positions=10, adsb_positions=9, mlat_positions=0 WHERE id=?",
            (fid,),
        )
        self.conn.commit()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT primary_source FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row["primary_source"] == "adsb"

    def test_close_flight_sets_primary_source_mlat(self):
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        self.conn.execute(
            "UPDATE flights SET total_positions=10, adsb_positions=0, mlat_positions=9 WHERE id=?",
            (fid,),
        )
        self.conn.commit()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT primary_source FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row["primary_source"] == "mlat"

    def test_close_flight_removes_from_active(self):
        from readsbstats.collector import _open_flight, _close_flight, _active
        _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        self.conn.execute(
            "UPDATE flights SET total_positions=5 WHERE icao_hex='aabbcc'"
        )
        self.conn.commit()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        assert "aabbcc" not in _active

    def test_close_unknown_icao_is_noop(self):
        """Closing an ICAO not in _active must not raise."""
        from readsbstats.collector import _close_flight
        with self.conn:
            _close_flight(self.conn, "unknown")  # should not raise

    def test_close_flight_keeps_flagged_with_few_positions(self):
        """Military/interesting flights with < MIN_POSITIONS_KEEP must NOT be deleted."""
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        # 1 position — normally deleted, but aircraft is flagged military
        self.conn.execute(
            "UPDATE flights SET total_positions=1 WHERE id=?", (fid,)
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO aircraft_db (icao_hex, flags) VALUES (?, ?)",
            ("aabbcc", config.FLAG_MILITARY),
        )
        self.conn.commit()
        enrichment.clear_cache()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is not None, "Flagged flight should be kept despite few positions"

    def test_close_flight_keeps_adsbx_flagged_with_few_positions(self):
        """Flights flagged via adsbx_overrides must also be kept."""
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        self.conn.execute(
            "UPDATE flights SET total_positions=1 WHERE id=?", (fid,)
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO adsbx_overrides"
            " (icao_hex, flags, first_seen, last_seen) VALUES (?, ?, 1000, 1000)",
            ("aabbcc", config.FLAG_INTERESTING),
        )
        self.conn.commit()
        enrichment.clear_cache()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is not None, "ADSBx-flagged flight should be kept despite few positions"

    def test_close_flight_deletes_unflagged_with_few_positions(self):
        """Unflagged flights with < MIN_POSITIONS_KEEP must still be deleted."""
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        # 0 positions, no flags — should be deleted
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is None

    def test_close_flight_keeps_anonymous_hex_with_few_positions(self):
        """Non-ICAO (anonymous) hex addresses are computed via icao_ranges and
        must be retained even with a single position — the whole point of the
        flag is to surface OPSEC/test sightings at the edge of receiver range."""
        from readsbstats.collector import _open_flight, _close_flight
        # dd85cb falls outside every state-allocated block — see test_icao_ranges.
        fid = _open_flight(
            self.conn, "dd85cb", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        self.conn.execute("UPDATE flights SET total_positions=1 WHERE id=?", (fid,))
        self.conn.commit()
        enrichment.clear_cache()
        with self.conn:
            _close_flight(self.conn, "dd85cb")
        row = self.conn.execute("SELECT * FROM flights WHERE id = ?", (fid,)).fetchone()
        assert row is not None, "Anonymous hex flight must be kept despite few positions"

    def test_enrich_sets_anonymous_flag_for_non_state_hex(self):
        """_enrich must OR in FLAG_ANONYMOUS purely from the icao_hex, no DB row needed."""
        from readsbstats.collector import _enrich
        enrichment.clear_cache()
        _, _, _, flags, _ = _enrich(self.conn, "dd85cb", None, None)
        assert flags & config.FLAG_ANONYMOUS
        # State-allocated address must not pick it up.
        enrichment.clear_cache()
        _, _, _, flags2, _ = _enrich(self.conn, "488001", None, None)
        assert not (flags2 & config.FLAG_ANONYMOUS)

    def test_close_flight_nulls_mlat_gs_outlier(self):
        """MLAT GS spike >>5×p75 must be nulled and max_gs recomputed at close."""
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        # Insert 1 spike + 11 normal MLAT positions (≥ MLAT_OUTLIER_MIN_READINGS)
        spike_pid = insert_position(self.conn, fid, 1001, lat=52.0, lon=21.0,
                                    gs=724.0, source_type="mlat")
        for i in range(11):
            insert_position(self.conn, fid, 2000 + i * 10, lat=52.0, lon=21.0,
                            gs=70.0, source_type="mlat")
        self.conn.execute(
            "UPDATE flights SET total_positions=12, mlat_positions=12, max_gs=724.0 WHERE id=?",
            (fid,),
        )
        self.conn.commit()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        spike_gs = self.conn.execute(
            "SELECT gs FROM positions WHERE id = ?", (spike_pid,)
        ).fetchone()[0]
        assert spike_gs is None, "outlier GS must be nulled"
        max_gs = self.conn.execute(
            "SELECT max_gs FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0]
        assert max_gs == pytest.approx(70.0), "max_gs must be recomputed after nulling"

    def test_close_flight_leaves_normal_mlat_gs_intact(self):
        """MLAT flight with uniform GS must not have any values nulled at close."""
        from readsbstats.collector import _open_flight, _close_flight
        fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        for i in range(12):
            insert_position(self.conn, fid, 1000 + i * 5, lat=52.0, lon=21.0,
                            gs=70.0 + i, source_type="mlat")
        self.conn.execute(
            "UPDATE flights SET total_positions=12, mlat_positions=12, max_gs=81.0 WHERE id=?",
            (fid,),
        )
        self.conn.commit()
        with self.conn:
            _close_flight(self.conn, "aabbcc")
        nulled = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE flight_id = ? AND gs IS NULL", (fid,)
        ).fetchone()[0]
        assert nulled == 0, "no GS values should be nulled in a normal flight"


# ---------------------------------------------------------------------------
# _poll integration — via temp aircraft.json
# ---------------------------------------------------------------------------

class TestPoll:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()

        # Point AIRCRAFT_JSON at a temp file we control
        self.json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(self.json_path))
        monkeypatch.setattr("readsbstats.collector.config", config)

        # Suppress notifier calls
        from readsbstats import notifier
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)
        monkeypatch.setattr(notifier, "notify_squawk",      lambda *a: None)
        monkeypatch.setattr(notifier, "notify_watchlist",   lambda *a: None)

        yield
        self.conn.close()

    def _write_json(self, aircraft: list, now: float | None = None):
        payload = {"now": now or time.time(), "aircraft": aircraft}
        self.json_path.write_text(json.dumps(payload))

    def test_poll_creates_flight_for_aircraft_with_position(self):
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ])
        _poll(self.conn)
        count = self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
        assert count == 1

    def test_poll_skips_aircraft_without_lat_lon(self):
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc"},                          # no lat/lon
            {"hex": "ddeeff", "lat": 52.0},             # missing lon
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_poll_skips_aircraft_with_out_of_range_coords(self, monkeypatch):
        """Lat/lon outside valid Earth coordinates must be rejected (corrupted feed).

        Bumps RECEIVER_MAX_RANGE so the range filter can't mask the bug — we want
        to verify the explicit bounds check, not the side-effect of haversine
        returning a large distance for nonsense input.
        """
        from readsbstats.collector import _poll
        monkeypatch.setattr("readsbstats.config.RECEIVER_MAX_RANGE", 99_999)
        self._write_json([
            {"hex": "aabbcc", "lat": 91.0,    "lon": 21.0,   "seen_pos": 0},
            {"hex": "ddeeff", "lat": -91.0,   "lon": 21.0,   "seen_pos": 0},
            {"hex": "112233", "lat": 52.0,    "lon": 181.0,  "seen_pos": 0},
            {"hex": "445566", "lat": 52.0,    "lon": -181.0, "seen_pos": 0},
            {"hex": "778899", "lat": float("nan"), "lon": 21.0, "seen_pos": 0},
            {"hex": "aabb01", "lat": 52.0,    "lon": float("inf"), "seen_pos": 0},
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0
        assert self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0

    def test_poll_skips_stale_position(self):
        """seen_pos > MAX_SEEN_POS_SEC should be skipped."""
        from readsbstats import config
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": config.MAX_SEEN_POS_SEC + 1},
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_poll_handles_null_seen_pos(self):
        """Regression for audit-12 #146 — `seen_pos: null` in aircraft.json was
        causing `None > 60` TypeError that aborted the whole poll cycle via the
        outer `except Exception`, dropping every aircraft for that tick.

        Now: skip just the bad aircraft and process the rest normally.
        """
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": None},  # malformed
            {"hex": "ddeeff", "lat": 52.1, "lon": 21.1, "seen_pos": 0},     # normal
        ])
        _poll(self.conn)
        # Bad aircraft skipped, good one still recorded
        rows = self.conn.execute("SELECT icao_hex FROM flights ORDER BY icao_hex").fetchall()
        icaos = [r[0] for r in rows]
        assert "ddeeff" in icaos
        assert "aabbcc" not in icaos

    def test_poll_handles_missing_seen_pos_field(self):
        """When `seen_pos` is absent entirely, treat as stale (skip) — same
        as the explicit-null path. Matches the historical `.get(..., 999)`
        behavior but now without a TypeError fallthrough."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0},  # no seen_pos key
            {"hex": "ddeeff", "lat": 52.1, "lon": 21.1, "seen_pos": 0},
        ])
        _poll(self.conn)
        rows = self.conn.execute("SELECT icao_hex FROM flights").fetchall()
        icaos = [r[0] for r in rows]
        assert "ddeeff" in icaos
        assert "aabbcc" not in icaos

    def test_poll_same_aircraft_twice_stays_one_flight(self):
        """Two polls within FLIGHT_GAP_SEC → same flight, just more positions."""
        from readsbstats.collector import _poll
        now = time.time()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)
        # Advance time slightly, rewrite file so mtime changes
        self.json_path.write_text(json.dumps(
            {"now": now + 10, "aircraft": [{"hex": "aabbcc", "lat": 52.1, "lon": 21.1, "seen_pos": 0}]}
        ))
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_poll_gap_creates_new_flight(self, monkeypatch):
        """A gap > FLIGHT_GAP_SEC between positions → two separate flights."""
        from readsbstats import config
        from readsbstats.collector import _poll, _active
        gap = config.FLIGHT_GAP_SEC + 60  # 30 min + 60 s
        now = time.time()

        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)

        first_flight_id = _active["aabbcc"]["flight_id"]
        # Give the first flight enough positions so it won't be pruned on close
        self.conn.execute(
            "UPDATE flights SET total_positions=5 WHERE id=?", (first_flight_id,)
        )
        self.conn.commit()

        # Write new data with pos_ts advanced by more than FLIGHT_GAP_SEC
        future = now + gap
        self.json_path.write_text(json.dumps(
            {"now": future, "aircraft": [{"hex": "aabbcc", "lat": 52.5, "lon": 21.5, "seen_pos": 0}]}
        ))
        _poll(self.conn)

        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 2

    def test_poll_strips_mlat_tilde_prefix(self):
        """MLAT hex entries start with ~ — should be stripped for icao_hex storage."""
        from readsbstats.collector import _poll
        self._write_json([{"hex": "~aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        row = self.conn.execute("SELECT icao_hex FROM flights").fetchone()
        assert row is not None
        assert row["icao_hex"] == "aabbcc"

    def test_poll_rejects_malformed_icao_hex(self):
        """Audit 2026-05-26: ICAO must be 6 lowercase hex chars after ~ strip.

        Before the fix the collector accepted any non-empty hex, letting
        corrupt feed data persist arbitrary identifiers into
        flights.icao_hex and pollute joins, watchlist matches, and
        country classification.

        The sentinels 000000 and ffffff are ADS-B placeholders for
        "no transponder address" and are rejected too.
        """
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "",        "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # empty
            {"hex": "abc",     "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # too short
            {"hex": "abcdefg", "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # too long
            {"hex": "zzzzzz",  "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # non-hex
            {"hex": "000000",  "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # sentinel
            {"hex": "ffffff",  "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # sentinel
            {"hex": "~A1B2C3", "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # valid mlat
        ])
        _poll(self.conn)
        rows = self.conn.execute("SELECT icao_hex FROM flights").fetchall()
        assert [r["icao_hex"] for r in rows] == ["a1b2c3"]

    def test_poll_skips_malformed_numeric_fields(self):
        """Audit 2026-05-26: non-numeric values must not abort the whole
        poll. Two-tier policy:

        * Strict-skip fields (lat, lon, seen_pos, hex): bad value drops
          just that aircraft, with no DB writes.
        * Flexible fields (alt_baro, gs, track, baro_rate, rssi,
          messages, alt_geom): bad value coerces to NULL and the row
          still processes.

        Verifies the coerce-upfront-before-DB-write pattern: bad
        strict-field records produce zero flights AND zero positions
        (no partial writes); flexible-field records persist with NULL.
        """
        from readsbstats.collector import _poll
        self._write_json([
            # Strict-skip failures (each invalid on lat/lon/seen_pos/hex):
            {"hex": "aaaa01", "lat": "not-a-number", "lon": 21.0, "seen_pos": 0},
            {"hex": "aaaa02", "lat": 52.0, "lon": "wrong", "seen_pos": 0},
            {"hex": "aaaa03", "lat": 52.0, "lon": 21.0, "seen_pos": "bad"},
            {"hex": 12345,   "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # non-string hex
            # Flexible-field bad coercion → NULL, row still inserts:
            {"hex": "cccc04", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "alt_baro": "cloud"},
            {"hex": "cccc05", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": "fast"},
            # All-good baseline:
            {"hex": "bbbb01", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
            {"hex": "bbbb02", "lat": 52.1, "lon": 21.1, "seen_pos": 0},
            {"hex": "bbbb03", "lat": 52.2, "lon": 21.2, "seen_pos": 0},
        ])
        _poll(self.conn)

        icaos = [r["icao_hex"] for r in self.conn.execute(
            "SELECT icao_hex FROM flights ORDER BY icao_hex"
        ).fetchall()]
        # Strict-skipped IDs absent:
        for skipped in ("aaaa01", "aaaa02", "aaaa03"):
            assert skipped not in icaos
        # Flexible-field rows still landed (NULL on the bad column):
        assert "cccc04" in icaos
        assert "cccc05" in icaos
        # Good baseline intact:
        for good in ("bbbb01", "bbbb02", "bbbb03"):
            assert good in icaos

        # No partial flights from the strict-skip rows: nothing matching
        # aaaa* should have positions either.
        bad_positions = self.conn.execute(
            "SELECT COUNT(*) FROM positions p JOIN flights f ON f.id = p.flight_id "
            "WHERE f.icao_hex LIKE 'aaaa%'"
        ).fetchone()[0]
        assert bad_positions == 0

    def test_poll_source_type_coerces_non_string(self):
        """PY-3 (Audit 2026-05-31): a non-string `type` field must not
        abort the poll. A dict / list / number for `type` is treated as
        missing (NULL) and the rest of the row still processes. For an
        mlat-hex (leading ``~``) the synthetic ``"mlat"`` fallback still
        applies.
        """
        from readsbstats.collector import _poll
        self._write_json([
            # Non-string type for a normal ICAO → source_type = NULL
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "type": {"x": 1}},
            # Non-string type for an mlat-hex → "mlat" fallback still fires
            {"hex": "~A1B2C3", "lat": 52.1, "lon": 21.1, "seen_pos": 0,
             "type": [1, 2, 3]},
            # Valid baseline so we can verify the others didn't roll it back
            {"hex": "ddeeff", "lat": 52.2, "lon": 21.2, "seen_pos": 0,
             "type": "adsb_icao"},
        ])
        _poll(self.conn)
        from readsbstats import posenc
        rows = self.conn.execute(
            "SELECT f.icao_hex, p.source FROM positions p "
            "JOIN flights f ON f.id = p.flight_id ORDER BY f.icao_hex"
        ).fetchall()
        by_icao = {r["icao_hex"]: posenc.decode_source(r["source"]) for r in rows}
        assert by_icao == {
            "a1b2c3":   "mlat",       # mlat-hex fallback
            "aabbcc":   None,         # non-string → NULL
            "ddeeff":   "adsb_icao",  # baseline preserved
        }

    def test_poll_source_type_caps_oversized_string(self):
        """PY-3: an oversized `type` string must not abort the poll. With the
        v6 schema unknown strings are stored as posenc.OTHER_CODE, so nothing
        unbounded can reach the positions table regardless of feed garbage."""
        from readsbstats import posenc
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "type": "x" * 10_000},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.source FROM positions p "
            "JOIN flights f ON f.id = p.flight_id WHERE f.icao_hex = 'aabbcc'"
        ).fetchone()
        assert row is not None
        assert row["source"] == posenc.OTHER_CODE
        assert posenc.decode_source(row["source"]) == "other"

    # BE-6 (Audit 2026-05-31): validate the top-level feed shape so a corrupt
    # aircraft.json degrades gracefully (skip bad entries / cycle) instead of
    # raising out of _poll and aborting the whole cycle via main()'s except.

    def test_poll_non_dict_top_level_does_not_raise(self):
        """A top-level JSON array (not the expected object) must not raise —
        _poll returns having written nothing."""
        from readsbstats.collector import _poll
        self.json_path.write_text(json.dumps([{"hex": "aabbcc"}]))
        _poll(self.conn)  # must not raise
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_poll_aircraft_not_a_list(self):
        """A non-list `aircraft` value (corrupt feed) is logged + skipped."""
        from readsbstats.collector import _poll
        self.json_path.write_text(json.dumps({"now": time.time(), "aircraft": "garbage"}))
        _poll(self.conn)  # must not raise
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_poll_skips_non_dict_aircraft_entries(self):
        """Non-dict items inside `aircraft` (string / None / int) are skipped
        per-entry, not aborting the whole poll."""
        from readsbstats.collector import _poll
        self.json_path.write_text(json.dumps({"now": time.time(), "aircraft": [
            "not-a-dict",
            None,
            42,
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ]}))
        _poll(self.conn)
        icaos = [r["icao_hex"] for r in self.conn.execute(
            "SELECT icao_hex FROM flights").fetchall()]
        assert icaos == ["aabbcc"]

    def test_poll_non_numeric_now_falls_back(self):
        """A non-numeric `now` must not abort the poll — it falls back to
        wall-clock time so positions still record."""
        from readsbstats.collector import _poll
        self.json_path.write_text(json.dumps({"now": "not-a-number", "aircraft": [
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ]}))
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_poll_normalizes_overlong_feed_strings(self):
        """BE-8: a corrupt/abusive feed must not store unbounded strings.
        callsign≤16, registration≤32, aircraft_type≤16, squawk≤8, category≤16."""
        from readsbstats.collector import _poll
        self._write_json([{
            "hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
            "flight": "A" * 30,
            "r": "R" * 50,
            "t": "T" * 30,
            "squawk": "1" * 20,
            "category": "C" * 30,
        }])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT callsign, registration, aircraft_type, squawk, category "
            "FROM flights WHERE icao_hex = 'aabbcc'"
        ).fetchone()
        assert len(row["callsign"]) == 16
        assert len(row["registration"]) == 32
        assert len(row["aircraft_type"]) == 16
        assert len(row["squawk"]) == 8
        assert len(row["category"]) == 16

    def test_poll_unchanged_file_is_noop(self):
        """If mtime hasn't changed, _poll should not process the file again."""
        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        pos_after_first = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        # Call again without touching the file — same mtime
        _poll(self.conn)
        # No new positions inserted — poll was skipped, not merely idempotent
        pos_after_second = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert pos_after_second == pos_after_first
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_poll_closes_expired_flights(self, monkeypatch):
        """Aircraft absent for > FLIGHT_GAP_SEC should be closed on the next poll."""
        from readsbstats import config
        from readsbstats.collector import _poll, _active
        now = time.time()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)

        fid = _active["aabbcc"]["flight_id"]
        # Give it enough positions to survive
        self.conn.execute("UPDATE flights SET total_positions=5 WHERE id=?", (fid,))
        self.conn.commit()

        # Write a future poll with no aircraft but advanced wall-clock time
        # We back-date last_seen in _active to simulate the gap
        _active["aabbcc"]["last_seen"] -= (config.FLIGHT_GAP_SEC + 60)

        # Write a new file (different content so mtime changes) with no aircraft
        self.json_path.write_text(json.dumps({"now": now + config.FLIGHT_GAP_SEC + 60, "aircraft": []}))
        _poll(self.conn)

        assert "aabbcc" not in _active

    def test_poll_skips_position_beyond_max_range(self, monkeypatch):
        """Positions beyond RECEIVER_MAX_RANGE nm must be silently dropped."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "RECEIVER_MAX_RANGE", 450)
        # Warsaw → London Heathrow ≈ 791 nm — well beyond 450 nm cap
        self._write_json([
            {"hex": "aabbcc", "lat": 51.477, "lon": -0.461, "seen_pos": 0},
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_poll_accepts_position_within_max_range(self, monkeypatch):
        """Positions within RECEIVER_MAX_RANGE nm must be accepted normally."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "RECEIVER_MAX_RANGE", 450)
        # 52.0, 21.0 is ~14 nm from Warsaw receiver — well within range
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1

    def test_poll_rejects_ghost_adsb_position(self, monkeypatch):
        """A position implying impossible speed (ghost ADS-B) must be dropped entirely."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_SPEED_KTS", 1500)
        now = time.time()

        # First position: real aircraft near Warsaw (~23 nm from receiver)
        self._write_json([
            {"hex": "aabbcc", "lat": 52.6, "lon": 20.75, "seen_pos": 0, "type": "mlat"},
        ], now)
        _poll(self.conn)
        first_dist = self.conn.execute(
            "SELECT max_distance_nm FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()["max_distance_nm"]

        # Second position 5 s later: ghost at 59.7°N (~449 nm away, ~323 000 kts implied)
        self.json_path.write_text(json.dumps({
            "now": now + 5,
            "aircraft": [
                {"hex": "aabbcc", "lat": 59.7, "lon": 21.5, "seen_pos": 0, "type": "adsb_icao"},
            ],
        }))
        _poll(self.conn)

        row = self.conn.execute(
            "SELECT max_distance_nm, total_positions FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()
        # Ghost must be dropped — distance and position count must not change
        assert row["max_distance_nm"] == pytest.approx(first_dist)
        assert row["total_positions"] == 1

    def test_poll_accepts_legitimate_fast_position(self, monkeypatch):
        """A fast but physically possible position (< MAX_SPEED_KTS) must be accepted."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_SPEED_KTS", 1500)
        now = time.time()

        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ], now)
        _poll(self.conn)

        # 60 s later, ~10 nm north ≈ 600 kts — well under 1500 kts
        self.json_path.write_text(json.dumps({
            "now": now + 60,
            "aircraft": [
                {"hex": "aabbcc", "lat": 52.17, "lon": 21.0, "seen_pos": 0},
            ],
        }))
        _poll(self.conn)

        total = self.conn.execute(
            "SELECT total_positions FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()["total_positions"]
        assert total == 2

    def test_poll_nulls_gs_for_civil_aircraft_above_civil_limit(self, monkeypatch):
        """GS above MAX_GS_CIVIL_KTS for a civil aircraft must be stored as NULL."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('aabbcc', 'SP-ABC', 'A319', 0)"
        )
        self.conn.commit()
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 790.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs, f.max_gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["gs"] is None
        assert row["max_gs"] is None

    def test_poll_keeps_gs_for_civil_aircraft_at_civil_limit(self, monkeypatch):
        """GS exactly at MAX_GS_CIVIL_KTS must be kept (boundary is inclusive)."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('aabbcc', 'SP-ABC', 'A319', 0)"
        )
        self.conn.commit()
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 750.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'aabbcc'"
        ).fetchone()
        assert row["gs"] == pytest.approx(750.0)

    def test_poll_keeps_gs_for_military_aircraft_above_civil_limit(self, monkeypatch):
        """Military aircraft with GS above civil limit but below military limit must keep GS."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('ae0001', 'MIL-1', 'F16', 1)"  # flags=1 = military
        )
        self.conn.commit()
        self._write_json([
            {"hex": "ae0001", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1200.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'ae0001'"
        ).fetchone()
        assert row["gs"] == pytest.approx(1200.0)

    def test_poll_nulls_gs_for_military_aircraft_above_military_limit(self, monkeypatch):
        """GS above MAX_GS_MILITARY_KTS must be nulled even for military aircraft."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('ae0001', 'MIL-1', 'F16', 1)"
        )
        self.conn.commit()
        self._write_json([
            {"hex": "ae0001", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1900.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'ae0001'"
        ).fetchone()
        assert row["gs"] is None

    def test_poll_nulls_gs_for_unknown_aircraft_above_military_limit(self, monkeypatch):
        """Aircraft not in aircraft_db use the military limit (1800 kts)."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        # No aircraft_db row for 'cabbed'
        self._write_json([
            {"hex": "cabbed", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1900.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'cabbed'"
        ).fetchone()
        assert row["gs"] is None

    def test_poll_keeps_gs_for_unknown_aircraft_below_military_limit(self, monkeypatch):
        """Unknown aircraft with GS below military limit (e.g. 800 kts) keep their GS."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self._write_json([
            {"hex": "cabbed", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 800.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'cabbed'"
        ).fetchone()
        assert row["gs"] == pytest.approx(800.0)

    def test_poll_maintains_rollups(self):
        """Every inserted position must increment grid_daily (both scales)
        and raise coverage_daily within the same transaction."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ])
        _poll(self.conn)
        n_pos = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert n_pos == 1
        fine = self.conn.execute(
            "SELECT SUM(w) FROM grid_daily WHERE scale = 100"
        ).fetchone()[0]
        coarse = self.conn.execute(
            "SELECT SUM(w) FROM grid_daily WHERE scale = 10"
        ).fetchone()[0]
        assert fine == coarse == n_pos
        assert self.conn.execute(
            "SELECT COUNT(*) FROM coverage_daily"
        ).fetchone()[0] == 1

    def test_poll_empty_aircraft_list_leaves_rollups_empty(self):
        """A poll with no aircraft must leave the rollup tables empty (flush no-op)."""
        from readsbstats.collector import _poll
        self._write_json([])
        _poll(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM grid_daily"
        ).fetchone()[0] == 0
        assert self.conn.execute(
            "SELECT COUNT(*) FROM coverage_daily"
        ).fetchone()[0] == 0

    def test_poll_flush_failure_rolls_back_positions(self, monkeypatch):
        """flush() runs inside the poll transaction: if it raises, the
        position inserts must roll back with it — rollups and positions
        can never drift apart. Guards against a future 'defensive'
        try/except around flush, which would silently break the invariant."""
        from readsbstats import rollups
        from readsbstats.collector import _poll

        def boom(conn, acc):
            raise RuntimeError("flush failed")

        monkeypatch.setattr(rollups, "flush", boom)
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ])
        with pytest.raises(RuntimeError, match="flush failed"):
            _poll(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM positions"
        ).fetchone()[0] == 0
        assert self.conn.execute(
            "SELECT COUNT(*) FROM grid_daily"
        ).fetchone()[0] == 0


class TestGsCrossValidation:
    """GS cross-validation: null gs when it disagrees with position-derived speed."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        _reset_collector_state()
        enrichment.clear_cache()
        from readsbstats import config
        from readsbstats import notifier
        from readsbstats.collector import _poll
        self.conn = make_db()
        self._poll = _poll
        json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(json_path))
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        monkeypatch.setattr(config, "MAX_GS_DEVIATION_KTS", 100)
        monkeypatch.setattr(config, "MAX_GS_ACCEL_KTS_S", 8.0)
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)
        monkeypatch.setattr(notifier, "notify_squawk",      lambda *a: None)
        monkeypatch.setattr(notifier, "notify_watchlist",   lambda *a: None)
        self.json_path = json_path
        self.now = time.time()
        yield
        self.conn.close()

    def _write(self, aircraft, ts=None):
        ts = ts or self.now
        self.json_path.write_text(json.dumps({"now": ts, "aircraft": aircraft}))

    def _gs_in_position(self, icao):
        return self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = ? ORDER BY p.ts DESC LIMIT 1",
            (icao,),
        ).fetchone()["gs"]

    def test_gs_nulled_when_deviation_exceeds_threshold(self, monkeypatch):
        """GS much higher than position-derived speed (dt≥30s) must be nulled."""
        # First position
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 550.0}],
                    self.now)
        self._poll(self.conn)
        # Second position 37s later: ~550 kts implied, but reports 700 kts
        # (700 - 550 = 150 kts deviation > 100 threshold)
        self._write(
            [{"hex": "aabbcc", "lat": 52.09422, "lon": 21.0, "seen_pos": 0, "gs": 700.0}],
            self.now + 37,
        )
        self._poll(self.conn)
        assert self._gs_in_position("aabbcc") is None

    def test_gs_kept_when_deviation_within_threshold(self, monkeypatch):
        """GS close to position-derived speed must be kept."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 550.0}],
                    self.now)
        self._poll(self.conn)
        # Implied ≈ 550, reports 560 — only 10 kts off
        self._write(
            [{"hex": "aabbcc", "lat": 52.09422, "lon": 21.0, "seen_pos": 0, "gs": 560.0}],
            self.now + 37,
        )
        self._poll(self.conn)
        assert self._gs_in_position("aabbcc") == pytest.approx(560.0)

    def test_gs_not_cross_validated_when_dt_below_minimum(self, monkeypatch):
        """Short dt (< 30s) skips cross-validation to avoid position-noise false positives."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 550.0}],
                    self.now)
        self._poll(self.conn)
        # 5s later, ~0.764 nm north (≈550 kts implied): reports 700 kts (150 kts off)
        # Cross-validation is skipped because dt=5 < 30 — gs must be kept as-is
        self._write(
            [{"hex": "aabbcc", "lat": 52.01273, "lon": 21.0, "seen_pos": 0, "gs": 700.0}],
            self.now + 5,
        )
        self._poll(self.conn)
        assert self._gs_in_position("aabbcc") == pytest.approx(700.0)

    def test_max_gs_in_flight_not_updated_when_gs_nulled_by_xval(self, monkeypatch):
        """Flight max_gs must not be updated when GS is nulled by cross-validation."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 550.0}],
                    self.now)
        self._poll(self.conn)
        self._write(
            [{"hex": "aabbcc", "lat": 52.09422, "lon": 21.0, "seen_pos": 0, "gs": 700.0}],
            self.now + 37,
        )
        self._poll(self.conn)
        max_gs = self.conn.execute(
            "SELECT max_gs FROM flights WHERE icao_hex = 'aabbcc'"
        ).fetchone()["max_gs"]
        assert max_gs == pytest.approx(550.0)


# ---------------------------------------------------------------------------
# MLAT GS acceleration filter
# ---------------------------------------------------------------------------

class TestMlatGsAccelFilter:
    """MLAT positions with physically impossible acceleration must have GS nulled."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        _reset_collector_state()
        enrichment.clear_cache()
        from readsbstats import config, notifier
        from readsbstats.collector import _poll
        self.conn = make_db()
        self._poll = _poll
        json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(json_path))
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        monkeypatch.setattr(config, "MAX_GS_ACCEL_KTS_S", 8.0)
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)
        monkeypatch.setattr(notifier, "notify_squawk",      lambda *a: None)
        monkeypatch.setattr(notifier, "notify_watchlist",   lambda *a: None)
        self.json_path = json_path
        self.now = time.time()
        yield
        self.conn.close()

    def _write(self, aircraft, ts=None):
        ts = ts or self.now
        self.json_path.write_text(json.dumps({"now": ts, "aircraft": aircraft}))

    def _gs_at(self, icao, idx=-1):
        """Return GS from stored position at given index (default: latest)."""
        rows = self.conn.execute(
            "SELECT p.gs / 10.0 AS gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = ? ORDER BY p.ts",
            (icao,),
        ).fetchall()
        return rows[idx]["gs"]

    def test_mlat_spike_gs_nulled(self):
        """MLAT position with GS jumping from 90→700 in 7s must have GS nulled."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # 7 seconds later: gs jumps to 700 — accel = 610/7 = 87 kts/s >> 8
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 700.0, "type": "mlat"}], self.now + 7)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") is None

    def test_mlat_spike_max_gs_not_updated(self):
        """Flight max_gs must not increase from a spike that gets nulled."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 700.0, "type": "mlat"}], self.now + 7)
        self._poll(self.conn)
        max_gs = self.conn.execute(
            "SELECT max_gs FROM flights WHERE icao_hex = 'aabbcc'"
        ).fetchone()["max_gs"]
        assert max_gs == pytest.approx(90.0)

    def test_mlat_moderate_accel_kept(self):
        """MLAT GS change within threshold (7 kts/s) must be kept."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # 5s later: gs=125 — accel = 35/5 = 7.0 kts/s < 8.0 threshold
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 125.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(125.0)

    def test_mlat_exactly_at_threshold_kept(self):
        """MLAT GS acceleration exactly at the limit must be kept (not strict >)."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 100.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # 5s later: gs=140 — accel = 40/5 = 8.0 kts/s exactly at threshold
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 140.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(140.0)

    def test_adsb_same_accel_not_filtered(self):
        """ADS-B positions must NOT be filtered by the acceleration limiter."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "adsb_icao"}], self.now)
        self._poll(self.conn)
        # Same spike as MLAT test, but source is adsb_icao — must keep GS
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 700.0, "type": "adsb_icao"}], self.now + 7)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(700.0)

    def test_first_mlat_position_no_prev_gs_not_filtered(self):
        """First position of a flight has no prev GS — must not be filtered."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 400.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(400.0)

    def test_mlat_spike_recovery_next_good_gs_kept(self):
        """After a spike is nulled, the next normal GS must be kept."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # Spike at +7s
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 700.0, "type": "mlat"}], self.now + 7)
        self._poll(self.conn)
        # Recovery at +14s — back to normal
        self._write([{"hex": "aabbcc", "lat": 52.002, "lon": 21.002,
                       "seen_pos": 0, "gs": 95.0, "type": "mlat"}], self.now + 14)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(95.0)

    def test_prev_gs_not_advanced_on_nulled_spike(self):
        """When a spike GS is nulled, prev_gs must NOT be updated to the spike value.
        Otherwise the next normal reading would compare against the spike and appear valid."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 90.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # Spike: 90→700 in 5s — nulled
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 700.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        # If prev_gs was updated to 700, this would also be filtered: |700-650|/5 = 10 > 8
        # But if prev_gs stayed at 90, it's also filtered: |90-650|/5 = 112 > 8 → nulled
        self._write([{"hex": "aabbcc", "lat": 52.002, "lon": 21.002,
                       "seen_pos": 0, "gs": 650.0, "type": "mlat"}], self.now + 10)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") is None

    def test_mlat_deceleration_spike_also_caught(self):
        """Spike in the other direction (high→low→high) must also be caught."""
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 400.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # 5s later: gs drops to 10 — accel = 390/5 = 78 kts/s
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 10.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") is None

    def test_mlat_accel_normal_after_hard_limit_nulls_gs(self, monkeypatch):
        """After the hard-limit filter nulls a GS, the next normal MLAT position
        must not be falsely flagged. last_gs stays at the pre-null value."""
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('aabbcc', 'SP-ABC', 'A319', 0)"
        )
        self.conn.commit()
        # Position 1: gs=400
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 400.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # Position 2: gs=800 — nulled by hard-limit (>750 civil), last_gs stays 400
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 800.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        # Position 3: gs=420 — accel from last_gs=400: |420-400|/5 = 4 kts/s < 8 → kept
        self._write([{"hex": "aabbcc", "lat": 52.002, "lon": 21.002,
                       "seen_pos": 0, "gs": 420.0, "type": "mlat"}], self.now + 10)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(420.0)

    def test_mlat_accel_normal_after_cross_validation_nulls_gs(self, monkeypatch):
        """After cross-validation nulls a GS, the next normal MLAT position
        must compare against the last valid GS, not the nulled one."""
        monkeypatch.setattr(config, "MAX_GS_DEVIATION_KTS", 100)
        # Position 1: gs=200 at known location
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 200.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # Position 2: 37s later, ~200 kts implied, but reports 500 (300 kts deviation > 100)
        # → cross-validation nulls gs, last_gs stays 200
        self._write([{"hex": "aabbcc", "lat": 52.09422, "lon": 21.0,
                       "seen_pos": 0, "gs": 500.0, "type": "mlat"}], self.now + 37)
        self._poll(self.conn)
        # Position 3: gs=210 — accel from last_gs=200: |210-200|/5 = 2 kts/s → kept
        self._write([{"hex": "aabbcc", "lat": 52.095, "lon": 21.001,
                       "seen_pos": 0, "gs": 210.0, "type": "mlat"}], self.now + 42)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(210.0)

    def test_mlat_accel_window_matches_last_valid_gs(self):
        """Audit 17: acceleration must be measured over the time since the last
        *valid* GS, not the last *sample*. After a nulled middle sample, the
        gs-delta spans two intervals; dividing it by a one-interval dt (the old
        bug) inflates the computed accel and over-nulls a legitimate change."""
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags)"
            " VALUES ('aabbcc', 'SP-ABC', 'A319', 0)"
        )
        self.conn.commit()
        # Position 1 (t0): gs=400 — valid baseline, last_gs=400 at t0.
        self._write([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0,
                       "seen_pos": 0, "gs": 400.0, "type": "mlat"}], self.now)
        self._poll(self.conn)
        # Position 2 (t+5): gs=800 nulled by the hard limit; last_gs stays 400 @ t0.
        self._write([{"hex": "aabbcc", "lat": 52.001, "lon": 21.001,
                       "seen_pos": 0, "gs": 800.0, "type": "mlat"}], self.now + 5)
        self._poll(self.conn)
        # Position 3 (t+10): gs=460. Real accel since the last valid gs (t0):
        # 60/10 = 6 kts/s < 8 → keep. The old mismatched window 60/5 = 12 > 8
        # would have wrongly nulled it.
        self._write([{"hex": "aabbcc", "lat": 52.002, "lon": 21.002,
                       "seen_pos": 0, "gs": 460.0, "type": "mlat"}], self.now + 10)
        self._poll(self.conn)
        assert self._gs_at("aabbcc") == pytest.approx(460.0)


# ---------------------------------------------------------------------------
# _update_flight_agg — aggregate column correctness
# ---------------------------------------------------------------------------

class TestUpdateFlightAgg:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()
        from readsbstats.collector import _open_flight
        # Open a flight with all aggregate fields NULL / zero
        self.fid = _open_flight(
            self.conn, "aabbcc", 1000, None, None, None,
            None, None, 52.0, 21.0, None, None, None, None, None,
        )
        self.conn.commit()
        yield
        self.conn.close()

    def _agg(self):
        return self.conn.execute(
            "SELECT * FROM flights WHERE id = ?", (self.fid,)
        ).fetchone()

    def _call(self, **kwargs):
        from readsbstats.collector import _update_flight_agg
        defaults = dict(
            conn=self.conn, flight_id=self.fid, icao="aabbcc",
            pos_ts=1001, callsign=None, registration=None, aircraft_type=None,
            squawk=None, lat=52.0, lon=21.0, alt=None, gs=None, rssi=None,
            source_type=None, distance_nm=None, distance_bearing=None,
        )
        defaults.update(kwargs)
        _update_flight_agg(**defaults)
        self.conn.commit()

    # --- position counters ---

    def test_total_positions_increments(self):
        self._call()
        assert self._agg()["total_positions"] == 1
        self._call(pos_ts=1002)
        assert self._agg()["total_positions"] == 2

    def test_adsb_positions_increments_for_adsb_source(self):
        self._call(source_type="adsb_icao")
        assert self._agg()["adsb_positions"] == 1
        assert self._agg()["mlat_positions"] == 0

    def test_mlat_positions_increments_for_mlat_source(self):
        self._call(source_type="mlat")
        assert self._agg()["mlat_positions"] == 1
        assert self._agg()["adsb_positions"] == 0

    def test_other_source_increments_neither_adsb_nor_mlat(self):
        self._call(source_type="mode_s")
        assert self._agg()["adsb_positions"] == 0
        assert self._agg()["mlat_positions"] == 0

    # --- max_alt_baro ---

    def test_max_alt_set_from_null(self):
        self._call(alt=35000)
        assert self._agg()["max_alt_baro"] == 35000

    def test_max_alt_updated_when_higher(self):
        self._call(alt=35000)
        self._call(pos_ts=1002, alt=36000)
        assert self._agg()["max_alt_baro"] == 36000

    def test_max_alt_not_updated_when_lower(self):
        self._call(alt=35000)
        self._call(pos_ts=1002, alt=30000)
        assert self._agg()["max_alt_baro"] == 35000

    def test_max_alt_not_updated_when_none(self):
        self._call(alt=35000)
        self._call(pos_ts=1002, alt=None)
        assert self._agg()["max_alt_baro"] == 35000

    # --- max_gs ---

    def test_max_gs_updated_when_higher(self):
        self._call(gs=400.0)
        self._call(pos_ts=1002, gs=500.0)
        assert self._agg()["max_gs"] == 500.0

    def test_max_gs_not_updated_when_lower(self):
        self._call(gs=500.0)
        self._call(pos_ts=1002, gs=400.0)
        assert self._agg()["max_gs"] == 500.0

    # --- min_rssi / max_rssi ---

    def test_min_rssi_updated_when_lower(self):
        self._call(rssi=-10.0)
        self._call(pos_ts=1002, rssi=-20.0)
        assert self._agg()["min_rssi"] == -20.0

    def test_min_rssi_not_updated_when_higher(self):
        self._call(rssi=-20.0)
        self._call(pos_ts=1002, rssi=-10.0)
        assert self._agg()["min_rssi"] == -20.0

    def test_max_rssi_updated_when_higher(self):
        self._call(rssi=-10.0)
        self._call(pos_ts=1002, rssi=-5.0)
        assert self._agg()["max_rssi"] == -5.0

    def test_max_rssi_not_updated_when_lower(self):
        self._call(rssi=-5.0)
        self._call(pos_ts=1002, rssi=-10.0)
        assert self._agg()["max_rssi"] == -5.0

    def test_min_and_max_rssi_independent(self):
        """Single update sets both min and max to the same value."""
        self._call(rssi=-15.0)
        assert self._agg()["min_rssi"] == -15.0
        assert self._agg()["max_rssi"] == -15.0

    # --- max_distance_nm ---

    def test_distance_updated_when_farther(self):
        self._call(distance_nm=100.0)
        self._call(pos_ts=1002, distance_nm=200.0)
        assert self._agg()["max_distance_nm"] == pytest.approx(200.0)

    def test_distance_not_updated_when_closer(self):
        self._call(distance_nm=200.0)
        self._call(pos_ts=1002, distance_nm=100.0)
        assert self._agg()["max_distance_nm"] == pytest.approx(200.0)

    # --- callsign / squawk write semantics ---

    def test_callsign_first_value_wins(self):
        """callsign uses COALESCE(existing, new) — first non-null sticks."""
        self._call(callsign="LOT123")
        self._call(pos_ts=1002, callsign="RYR456")
        assert self._agg()["callsign"] == "LOT123"

    def test_callsign_set_when_previously_null(self):
        self._call(callsign=None)
        self._call(pos_ts=1002, callsign="LOT123")
        assert self._agg()["callsign"] == "LOT123"

    def test_squawk_last_non_null_wins(self):
        """squawk uses COALESCE(new, existing) — latest non-null overwrites."""
        self._call(squawk="1234")
        self._call(pos_ts=1002, squawk="5678")
        assert self._agg()["squawk"] == "5678"

    def test_squawk_null_does_not_overwrite(self):
        self._call(squawk="1234")
        self._call(pos_ts=1002, squawk=None)
        assert self._agg()["squawk"] == "1234"

    # --- category (audit-12 #144) ---

    def test_category_set_when_previously_null(self):
        """Regression for audit-12 #144 — readsb often emits `category` only
        after the first position. _update_flight_agg must carry it forward,
        not leave the column NULL forever."""
        self._call(category=None)
        self._call(pos_ts=1002, category="A3")
        assert self._agg()["category"] == "A3"

    def test_category_first_value_wins(self):
        """COALESCE(existing, new) — first non-null sticks (same semantics
        as callsign/registration/aircraft_type)."""
        self._call(category="A3")
        self._call(pos_ts=1002, category="A5")
        assert self._agg()["category"] == "A3"

    def test_category_null_does_not_clear(self):
        self._call(category="A3")
        self._call(pos_ts=1002, category=None)
        assert self._agg()["category"] == "A3"

    # --- last_seen ---

    def test_last_seen_advances(self):
        self._call(pos_ts=2000)
        assert self._agg()["last_seen"] == 2000

    def test_last_seen_does_not_go_backwards(self):
        self._call(pos_ts=2000)
        self._call(pos_ts=1500)  # older timestamp
        assert self._agg()["last_seen"] == 2000

    # --- bounding box ---

    def test_lat_lon_bounds_expand(self):
        self._call(lat=52.0, lon=21.0)
        self._call(pos_ts=1002, lat=53.0, lon=22.0)
        row = self._agg()
        assert row["lat_min"] == pytest.approx(52.0)
        assert row["lat_max"] == pytest.approx(53.0)
        assert row["lon_min"] == pytest.approx(21.0)
        assert row["lon_max"] == pytest.approx(22.0)

    def test_lat_lon_bounds_do_not_shrink(self):
        self._call(lat=51.0, lon=20.0)
        self._call(pos_ts=1002, lat=52.0, lon=21.0)
        self._call(pos_ts=1003, lat=51.5, lon=20.5)  # inside existing bounds
        row = self._agg()
        assert row["lat_min"] == pytest.approx(51.0)
        assert row["lat_max"] == pytest.approx(52.0)

    # --- _active state ---

    def test_active_last_seen_updated(self):
        from readsbstats.collector import _active
        self._call(pos_ts=9999)
        assert _active["aabbcc"]["last_seen"] == 9999


# ---------------------------------------------------------------------------
# _purge
# ---------------------------------------------------------------------------

class TestPurge:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        self.conn = make_db()
        yield
        self.conn.close()

    def _insert_flight(self, first_seen, last_seen, total_pos=10):
        cur = self.conn.execute(
            """INSERT INTO flights
               (icao_hex, first_seen, last_seen, total_positions,
                adsb_positions, mlat_positions, lat_min, lat_max, lon_min, lon_max)
               VALUES ('aabbcc',?,?,?,0,0,0,0,0,0)""",
            (first_seen, last_seen, total_pos),
        )
        self.conn.commit()
        return cur.lastrowid

    def _insert_position(self, flight_id, ts):
        insert_position(self.conn, flight_id, ts, lat=52.0, lon=21.0)
        self.conn.commit()

    def _insert_pos_full(self, flight_id, ts, *, lat, lon, gs, st):
        insert_position(self.conn, flight_id, ts, lat=lat, lon=lon, gs=gs,
                        source_type=st)
        self.conn.commit()

    def test_crossing_flight_aggregates_recomputed(self, monkeypatch):
        """Audit 17: a flight straddling the retention cutoff keeps some
        positions and loses others, so every position-derived aggregate must be
        recomputed from the SURVIVORS — not left at the pre-purge stale value.
        This is the only Python-side aggregate-correction path."""
        from readsbstats import config as _config, geo
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        monkeypatch.setattr("readsbstats.config.MIN_POSITIONS_KEEP", 2)
        cutoff = int(time.time()) - 86400
        rlat, rlon = _config.RECEIVER_LAT, _config.RECEIVER_LON
        fid = self._insert_flight(cutoff - 100, cutoff + 500, total_pos=3)
        # Seed STALE aggregates reflecting the soon-to-be-deleted far/fast pos.
        self.conn.execute(
            "UPDATE flights SET max_gs=999, max_distance_nm=9999, "
            "max_distance_bearing=123, primary_source='mlat' WHERE id=?", (fid,))
        self.conn.commit()
        # Deleted (before cutoff): far + very fast + MLAT.
        self._insert_pos_full(fid, cutoff - 50, lat=rlat + 5.0, lon=rlon + 5.0,
                              gs=999, st="mlat")
        # Survivors (>= cutoff): nearer + slower + ADS-B.
        self._insert_pos_full(fid, cutoff + 100, lat=rlat + 0.5, lon=rlon,
                              gs=400, st="adsb_icao")
        self._insert_pos_full(fid, cutoff + 200, lat=rlat + 1.0, lon=rlon,
                              gs=450, st="adsb_icao")
        from readsbstats.collector import _purge
        _purge(self.conn)
        row = self.conn.execute(
            "SELECT total_positions, max_gs, max_distance_nm, primary_source "
            "FROM flights WHERE id=?", (fid,)).fetchone()
        assert row["total_positions"] == 2
        assert row["max_gs"] == 450
        # Survivors are all ADS-B → no longer 'mlat'.
        assert row["primary_source"] != "mlat"
        # Distance reflects the farther SURVIVING point, not the deleted one.
        expected = geo.haversine_nm(rlat, rlon, rlat + 1.0, rlon)
        assert row["max_distance_nm"] == pytest.approx(expected, rel=1e-6)
        assert row["max_distance_nm"] < 9999

    def test_retention_zero_skips_purge(self, monkeypatch):
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 0)
        fid = self._insert_flight(1, 2, total_pos=1)
        self._insert_position(fid, 1)
        from readsbstats.collector import _purge
        _purge(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1

    def test_old_positions_deleted(self, monkeypatch):
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        cutoff = int(time.time()) - 86400
        fid = self._insert_flight(cutoff - 100, cutoff - 50)
        self._insert_position(fid, cutoff - 200)  # older than cutoff → deleted
        self._insert_position(fid, cutoff + 200)  # newer than cutoff → kept
        self._insert_position(fid, cutoff + 300)  # newer than cutoff → kept (need ≥ MIN=2 to survive)
        from readsbstats.collector import _purge
        _purge(self.conn)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE flight_id = ?", (fid,)
        ).fetchone()[0]
        assert remaining == 2

    def test_stub_flight_deleted(self, monkeypatch):
        """Flight with total_positions < MIN_POSITIONS_KEEP and old last_seen → deleted."""
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        monkeypatch.setattr("readsbstats.config.MIN_POSITIONS_KEEP", 2)
        cutoff = int(time.time()) - 86400
        fid = self._insert_flight(cutoff - 200, cutoff - 100, total_pos=1)
        from readsbstats.collector import _purge
        _purge(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0] == 0

    def test_recent_flight_not_deleted(self, monkeypatch):
        """Flight whose last_seen is within retention window must survive."""
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        now = int(time.time())
        fid = self._insert_flight(now - 3600, now - 60, total_pos=1)
        from readsbstats.collector import _purge
        _purge(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0] == 1

    def test_active_flight_not_deleted(self, monkeypatch):
        """A flight listed in active_flights must never be purged."""
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        cutoff = int(time.time()) - 86400
        fid = self._insert_flight(cutoff - 200, cutoff - 100, total_pos=1)
        self.conn.execute(
            "INSERT INTO active_flights VALUES ('aabbcc', ?, ?)", (fid, cutoff - 100)
        )
        self.conn.commit()
        from readsbstats.collector import _purge
        _purge(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0] == 1

    def test_zombie_flight_deleted_when_all_positions_purged(self, monkeypatch):
        """A flight whose ALL positions age out must not survive as a zombie.

        Bug: the old order was DELETE positions → DELETE flights (using stale count)
        → UPDATE recount.  A flight with total_positions >= MIN survived the DELETE
        (stale count was fine), then got its count zeroed by the UPDATE, leaving a
        row with total_positions=0 and no position rows — a zombie that persists
        until the next purge cycle.
        """
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        monkeypatch.setattr("readsbstats.config.MIN_POSITIONS_KEEP", 2)
        cutoff = int(time.time()) - 86400
        # Flight well above MIN but ALL positions are old
        fid = self._insert_flight(cutoff - 300, cutoff - 100, total_pos=5)
        for i in range(5):
            self._insert_position(fid, cutoff - 300 + i)  # all before cutoff
        from readsbstats.collector import _purge
        _purge(self.conn)
        assert self.conn.execute(
            "SELECT COUNT(*) FROM flights WHERE id = ?", (fid,)
        ).fetchone()[0] == 0, "flight with all-purged positions should be deleted, not left as zombie"

    def test_total_positions_recounted_after_purge(self, monkeypatch):
        """After positions are purged, total_positions is recomputed from surviving rows."""
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        cutoff = int(time.time()) - 86400
        fid = self._insert_flight(cutoff - 200, cutoff - 50, total_pos=3)
        self._insert_position(fid, cutoff - 300)  # old → deleted
        self._insert_position(fid, cutoff + 100)  # recent → kept
        self._insert_position(fid, cutoff + 200)  # recent → kept
        from readsbstats.collector import _purge
        _purge(self.conn)
        row = self.conn.execute(
            "SELECT total_positions FROM flights WHERE id = ?", (fid,)
        ).fetchone()
        assert row["total_positions"] == 2

    # BE-7 (Audit 2026-05-31): a flight that crosses the cutoff (started before,
    # still seen after) keeps some positions and loses others. ALL its
    # position-derived aggregates must be recomputed from the surviving rows —
    # not just total_positions.

    def _insert_rich_position(self, flight_id, ts, lat, lon, gs, alt, source_type):
        insert_position(self.conn, flight_id, ts, lat=lat, lon=lon, gs=gs,
                        alt_baro=alt, source_type=source_type)
        self.conn.commit()

    def test_crossing_flight_aggregates_recomputed(self, monkeypatch):
        from readsbstats import geo
        from readsbstats.collector import _purge
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        monkeypatch.setattr("readsbstats.config.MIN_POSITIONS_KEEP", 2)
        cutoff = int(time.time()) - 86400

        # Stale aggregates reflect the full pre-purge set (incl. the far/fast/high
        # old positions). After purge they must reflect only the remaining rows.
        cur = self.conn.execute(
            """INSERT INTO flights
               (icao_hex, first_seen, last_seen, total_positions,
                adsb_positions, mlat_positions, max_gs, max_alt_baro,
                lat_min, lat_max, lon_min, lon_max,
                max_distance_nm, max_distance_bearing, primary_source)
               VALUES ('aabbcc',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cutoff - 500, cutoff + 500, 5, 2, 2, 900.0, 50000,
             10.0, 53.0, 10.0, 22.0, 9999.0, 12.0, "adsb"),
        )
        fid = cur.lastrowid
        self.conn.commit()

        # Old positions (ts < cutoff): far away, fast, high — all deleted.
        self._insert_rich_position(fid, cutoff - 400, 10.0, 10.0, 900.0, 50000, "adsb_icao")
        self._insert_rich_position(fid, cutoff - 300, 11.0, 11.0, 800.0, 49000, "mlat")
        # Surviving positions (ts >= cutoff): near the receiver, slower, lower.
        survivors = [
            (cutoff + 100, 52.0, 21.0, 400.0, 30000, "adsb_icao"),
            (cutoff + 200, 52.5, 21.5, 450.0, 31000, "adsb_icao"),
            (cutoff + 300, 53.0, 22.0, 420.0, 30500, "mlat"),
        ]
        for ts, lat, lon, gs, alt, st in survivors:
            self._insert_rich_position(fid, ts, lat, lon, gs, alt, st)

        _purge(self.conn)

        row = self.conn.execute(
            """SELECT total_positions, adsb_positions, mlat_positions,
                      max_gs, max_alt_baro, lat_min, lat_max, lon_min, lon_max,
                      max_distance_nm, max_distance_bearing, primary_source
               FROM flights WHERE id = ?""", (fid,),
        ).fetchone()
        assert row is not None, "crossing flight must survive the purge"
        assert row["total_positions"] == 3
        assert row["adsb_positions"] == 2
        assert row["mlat_positions"] == 1
        assert row["max_gs"] == 450.0          # not the deleted 900
        assert row["max_alt_baro"] == 31000    # not the deleted 50000
        assert row["lat_min"] == 52.0 and row["lat_max"] == 53.0  # not 10/53
        assert row["lon_min"] == 21.0 and row["lon_max"] == 22.0
        # adsb 2/3 < 0.8, mlat 1/3 < 0.8, (adsb+mlat)/3 == 1.0 >= 0.5 → "mixed"
        assert row["primary_source"] == "mixed"

        # max_distance/bearing recomputed over surviving rows only (receiver-relative).
        expected = max(
            geo.haversine_nm(config.RECEIVER_LAT, config.RECEIVER_LON, lat, lon)
            for _, lat, lon, *_ in survivors
        )
        assert row["max_distance_nm"] == pytest.approx(expected, abs=0.01)
        assert row["max_distance_nm"] < 9999.0  # the stale far value is gone

    def test_active_crossing_flight_aggregates_preserved(self, monkeypatch):
        """An open (active) flight that crosses the cutoff must NOT have its
        aggregates recomputed by purge. The collector owns an active flight's
        running aggregates in memory and rewrites them on close; a purge-time
        recompute would momentarily clobber them from a partial position set.
        Steps 3/4 already exclude active flights; step 2 must match."""
        from readsbstats.collector import _purge
        monkeypatch.setattr("readsbstats.config.RETENTION_DAYS", 1)
        monkeypatch.setattr("readsbstats.config.MIN_POSITIONS_KEEP", 2)
        cutoff = int(time.time()) - 86400

        cur = self.conn.execute(
            """INSERT INTO flights
               (icao_hex, first_seen, last_seen, total_positions,
                adsb_positions, mlat_positions, max_gs, max_alt_baro,
                lat_min, lat_max, lon_min, lon_max,
                max_distance_nm, max_distance_bearing, primary_source)
               VALUES ('aabbcc',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cutoff - 500, cutoff + 500, 5, 2, 2, 900.0, 50000,
             10.0, 53.0, 10.0, 22.0, 9999.0, 12.0, "adsb"),
        )
        fid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO active_flights VALUES ('aabbcc', ?, ?)", (fid, cutoff + 500)
        )
        self.conn.commit()

        # Old positions deleted; survivors near the receiver — a recompute
        # would shrink max_gs/max_alt/distance away from the stale values.
        self._insert_rich_position(fid, cutoff - 400, 10.0, 10.0, 900.0, 50000, "adsb_icao")
        for ts, lat, lon, gs, alt, st in [
            (cutoff + 100, 52.0, 21.0, 400.0, 30000, "adsb_icao"),
            (cutoff + 200, 52.5, 21.5, 450.0, 31000, "mlat"),
        ]:
            self._insert_rich_position(fid, ts, lat, lon, gs, alt, st)

        _purge(self.conn)

        row = self.conn.execute(
            """SELECT max_gs, max_alt_baro, lat_min, lat_max,
                      max_distance_nm, max_distance_bearing
               FROM flights WHERE id = ?""", (fid,),
        ).fetchone()
        assert row is not None, "active crossing flight must survive the purge"
        # Aggregates untouched — still the collector-owned stale values.
        assert row["max_gs"] == 900.0
        assert row["max_alt_baro"] == 50000
        assert row["lat_min"] == 10.0 and row["lat_max"] == 53.0
        assert row["max_distance_nm"] == 9999.0
        assert row["max_distance_bearing"] == 12.0


# ---------------------------------------------------------------------------
# _run_maintenance
# ---------------------------------------------------------------------------

class TestRunMaintenance:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_run_maintenance_executes_without_error(self):
        """_run_maintenance wraps _purge + PRAGMA optimize; must be a no-op
        safe call on a fresh DB with retention disabled."""
        from readsbstats import collector
        collector._run_maintenance(self.conn)   # must not raise


# ---------------------------------------------------------------------------
# _poll edge cases — ground altitude and emergency squawks
# ---------------------------------------------------------------------------

class TestPollEdgeCases:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()

        self.json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(self.json_path))
        monkeypatch.setattr("readsbstats.collector.config", config)

        # Enable Telegram for notification tests
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")

        # Track notifier calls
        from readsbstats import notifier
        self.squawk_calls = []
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)
        monkeypatch.setattr(notifier, "notify_squawk",
                            lambda *a: self.squawk_calls.append(a))

        yield
        self.conn.close()

    def _write_json(self, aircraft, now=None):
        self.json_path.write_text(
            json.dumps({"now": now or time.time(), "aircraft": aircraft})
        )

    def test_ground_altitude_stored_as_zero(self):
        """alt_baro == 'ground' must be stored as integer 0, not NULL."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "alt_baro": "ground"},
        ])
        _poll(self.conn)
        row = self.conn.execute("SELECT alt_baro FROM positions").fetchone()
        assert row is not None
        assert row["alt_baro"] == 0

    def test_at_sign_callsign_treated_as_null(self):
        """Mode S null-padded callsigns like '@@@@@@@@' must be treated as None."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "flight": "@@@@@@@@"},
        ])
        _poll(self.conn)
        row = self.conn.execute("SELECT callsign FROM flights").fetchone()
        assert row["callsign"] is None

    def test_partial_at_sign_callsign_treated_as_null(self):
        """Callsigns with any @ character are garbage and must be treated as None."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "flight": "LOT@@@@@"},
        ])
        _poll(self.conn)
        row = self.conn.execute("SELECT callsign FROM flights").fetchone()
        assert row["callsign"] is None

    def test_missing_alt_baro_stored_as_null(self):
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
        ])
        _poll(self.conn)
        row = self.conn.execute("SELECT alt_baro FROM positions").fetchone()
        assert row["alt_baro"] is None

    def test_emergency_squawk_triggers_notification(self):
        from readsbstats import collector
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "squawk": "7700"},
        ])
        _poll(self.conn)
        collector._drain_notifications(timeout=1.0)
        assert len(self.squawk_calls) == 1
        assert self.squawk_calls[0][3] == "7700"  # squawk value in args

    def test_emergency_squawk_not_repeated_same_flight(self):
        """Same flight_id with the same emergency squawk must only notify once."""
        from readsbstats import collector
        from readsbstats.collector import _poll
        now = time.time()
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "squawk": "7700"},
        ], now)
        _poll(self.conn)

        # Second poll, same flight, same squawk
        self.json_path.write_text(json.dumps({
            "now": now + 10,
            "aircraft": [{"hex": "aabbcc", "lat": 52.1, "lon": 21.1,
                          "seen_pos": 0, "squawk": "7700"}],
        }))
        _poll(self.conn)

        collector._drain_notifications(timeout=1.0)
        assert len(self.squawk_calls) == 1  # still only one notification

    def test_all_three_emergency_squawks_trigger(self):
        from readsbstats import collector
        from readsbstats.collector import _poll
        for i, sqk in enumerate(["7500", "7600", "7700"]):
            _reset_collector_state()
            enrichment.clear_cache()
            conn = make_db()
            self.json_path.write_text(json.dumps({
                "now": time.time(),
                "aircraft": [{"hex": f"aa00{i:02d}", "lat": 52.0, "lon": 21.0,
                               "seen_pos": 0, "squawk": sqk}],
            }))
            _poll(conn)
            conn.close()
        collector._drain_notifications(timeout=1.0)
        assert len(self.squawk_calls) == 3

    def test_non_emergency_squawk_does_not_notify(self):
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "squawk": "1234"},
        ])
        _poll(self.conn)
        assert len(self.squawk_calls) == 0

    def test_squawk_added_to_squawk_notified_set(self):
        from readsbstats.collector import _poll, _squawk_notified
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "squawk": "7600"},
        ])
        _poll(self.conn)
        fid = self.conn.execute("SELECT id FROM flights").fetchone()[0]
        assert fid in _squawk_notified

    def test_ghost_position_with_emergency_squawk_does_not_notify(self, monkeypatch):
        """Audit 2026-05-25: notifications used to be queued and dedupe sets
        mutated BEFORE the ghost-position filter. A bad ADS-B jump carrying a
        7x00 squawk produced an emergency alert for a position the collector
        then rejected, and the flight_id was added to `_squawk_notified` so
        later legitimate emergencies on the same flight were suppressed."""
        from readsbstats import collector, config as cfg
        from readsbstats.collector import _poll, _squawk_notified
        monkeypatch.setattr(cfg, "MAX_SPEED_KTS", 1500)
        now = time.time()

        # First sample: real flight, no emergency squawk.
        self._write_json([
            {"hex": "aabbcc", "lat": 52.6, "lon": 20.75, "seen_pos": 0,
             "type": "mlat"},
        ], now)
        _poll(self.conn)
        fid = self.conn.execute(
            "SELECT id FROM flights WHERE icao_hex='aabbcc'"
        ).fetchone()[0]

        # Second sample 5 s later: ghost (~323 000 kts implied) AND squawk 7700.
        self.json_path.write_text(json.dumps({
            "now": now + 5,
            "aircraft": [
                {"hex": "aabbcc", "lat": 59.7, "lon": 21.5, "seen_pos": 0,
                 "type": "adsb_icao", "squawk": "7700"},
            ],
        }))
        _poll(self.conn)
        collector._drain_notifications(timeout=1.0)

        # Position must not have been recorded.
        total = self.conn.execute(
            "SELECT total_positions FROM flights WHERE id=?", (fid,)
        ).fetchone()["total_positions"]
        assert total == 1
        # No emergency notification must have been queued or sent.
        assert self.squawk_calls == []
        # The flight_id must not be locked out of future legitimate alerts.
        assert fid not in _squawk_notified

    def test_squawk_notified_dropped_on_close_flight(self):
        """Audit-12 #186 — `_squawk_notified` historically grew unboundedly:
        every emergency-squawk flight_id was kept forever. Now the flight_id
        is dropped from the set when `_close_flight` finalises, so the set
        is naturally bounded by max-concurrent-active-flights."""
        from readsbstats.collector import _close_flight, _squawk_notified
        # Set up a flight + put its id in the squawk-notified set
        cur = self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000, 2000, 10)"
        )
        fid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc', ?, 2000)",
            (fid,),
        )
        self.conn.commit()
        # Re-seed in-memory state to match
        from readsbstats import collector as _c
        _c._active["aabbcc"] = {"flight_id": fid, "last_seen": 2000, "last_pos_ts": 2000}
        _squawk_notified.add(fid)

        _close_flight(self.conn, "aabbcc")

        assert fid not in _squawk_notified, (
            "flight_id was not dropped from _squawk_notified on _close_flight"
        )

    def test_empty_hex_entry_skipped(self):
        """Aircraft with empty hex string must not create a flight."""
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "", "lat": 52.0, "lon": 21.0, "seen_pos": 0},
            {"hex": "~", "lat": 52.0, "lon": 21.0, "seen_pos": 0},  # ~ only → empty after strip
        ])
        _poll(self.conn)
        assert self.conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 0

    def test_duplicate_pos_ts_skipped(self):
        """A position with the same ts as last_pos_ts must be ignored."""
        from readsbstats.collector import _poll, _active
        now = time.time()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)
        pos_count_after_first = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

        # Second file with identical now and seen_pos=0 → same pos_ts
        self.json_path.write_text(
            json.dumps({"now": now + 0.001, "aircraft": [
                {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0.001}
            ]})
        )
        _poll(self.conn)
        # pos_ts is truncated to int; if same integer, should be skipped
        pos_count_after_second = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert pos_count_after_second == pos_count_after_first

    def test_military_first_sighting_queues_notification(self, monkeypatch):
        """First new flight for a military ICAO must call notify_military."""
        from readsbstats import collector, notifier
        mil_calls = []
        monkeypatch.setattr(notifier, "notify_military", lambda *a: mil_calls.append(a))

        # Insert a military aircraft in aircraft_db
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags) "
            "VALUES ('aabbcc', 'MIL-1', 'C130', 1)"
        )
        self.conn.commit()

        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        collector._notifications_thread.join(timeout=1)
        assert len(mil_calls) == 1

    def test_military_second_sighting_no_repeat_notification(self, monkeypatch):
        """Subsequent flights for the same military ICAO must not re-notify."""
        from readsbstats import collector, config, notifier
        mil_calls = []
        monkeypatch.setattr(notifier, "notify_military", lambda *a: mil_calls.append(a))

        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags) "
            "VALUES ('aabbcc', 'MIL-1', 'C130', 1)"
        )
        self.conn.commit()

        from readsbstats.collector import _poll, _active
        now = time.time()
        gap = config.FLIGHT_GAP_SEC + 60

        # First flight
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)
        collector._notifications_thread.join(timeout=1)
        fid = _active["aabbcc"]["flight_id"]
        self.conn.execute("UPDATE flights SET total_positions=5 WHERE id=?", (fid,))
        self.conn.commit()

        # Gap → second flight (no pending notifications expected, thread not updated)
        self.json_path.write_text(json.dumps({
            "now": now + gap,
            "aircraft": [{"hex": "aabbcc", "lat": 52.5, "lon": 21.5, "seen_pos": 0}],
        }))
        _poll(self.conn)
        # Second flight for already-notified ICAO → no new thread, join existing (already done)
        if collector._notifications_thread and collector._notifications_thread.is_alive():
            collector._notifications_thread.join(timeout=1)
        assert len(mil_calls) == 1  # still only one notification

    def test_interesting_first_sighting_queues_notification(self, monkeypatch):
        """First new flight for an interesting ICAO must call notify_interesting."""
        from readsbstats import collector, notifier
        int_calls = []
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: int_calls.append(a))

        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags) "
            "VALUES ('aabbcc', 'INT-1', 'B734', 2)"  # flags=2 = interesting
        )
        self.conn.commit()

        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        collector._notifications_thread.join(timeout=1)
        assert len(int_calls) == 1

    def test_notifications_run_in_daemon_thread(self, monkeypatch):
        """notify_military must be called from a non-main daemon thread."""
        import threading as _threading
        from readsbstats import collector, notifier

        notify_threads: list[_threading.Thread] = []

        def capture_thread(*a):
            notify_threads.append(_threading.current_thread())

        monkeypatch.setattr(notifier, "notify_military", capture_thread)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, flags) "
            "VALUES ('aabbcc', 'MIL-1', 'C130', 1)"
        )
        self.conn.commit()

        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        collector._notifications_thread.join(timeout=1)

        assert len(notify_threads) == 1
        assert notify_threads[0] is not _threading.main_thread()
        assert notify_threads[0].daemon


# ---------------------------------------------------------------------------
# _load_active
# ---------------------------------------------------------------------------

class TestLoadActive:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_populates_active_from_db(self):
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000000, 1003600, 10)"
        )
        fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc',?,1003600)",
            (fid,),
        )
        self.conn.commit()

        _load_active(self.conn)

        assert "aabbcc" in _active
        assert _active["aabbcc"]["flight_id"] == fid
        assert _active["aabbcc"]["last_seen"] == 1003600
        assert _active["aabbcc"]["last_pos_ts"] == 1003600

    def test_empty_active_flights_table_clears_active(self):
        from readsbstats.collector import _load_active, _active
        _active["stale"] = {"flight_id": 99, "last_seen": 0, "last_pos_ts": 0}
        _load_active(self.conn)
        assert len(_active) == 0

    def test_last_pos_ts_uses_position_ts_not_active_flights_last_seen(self):
        """After restart, last_pos_ts must come from positions.ts, not active_flights.last_seen.

        active_flights.last_seen is written only when the flight opens and never
        updated.  If the collector ran for two hours, accumulated positions, then
        restarted, active_flights.last_seen is hours in the past.  Using it as
        last_pos_ts makes dt huge on the next poll, so implied_kts is tiny and
        the ghost filter passes any position — effectively disabled.
        """
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000000, 1007200, 100)"
        )
        fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # active_flights.last_seen records the flight *open* time, not last pos
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc',?,1000000)",
            (fid,),
        )
        # Positions accumulated over two hours; most recent at ts=1007200
        insert_position(self.conn, fid, 1007200, lat=52.0, lon=21.0)
        self.conn.commit()

        _load_active(self.conn)

        # Must use position ts (1007200), NOT active_flights.last_seen (1000000)
        assert _active["aabbcc"]["last_pos_ts"] == 1007200

    def test_multiple_entries_all_loaded(self):
        from readsbstats.collector import _load_active, _active
        for i, icao in enumerate(["aabbcc", "ddeeff", "112233"]):
            self.conn.execute(
                "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
                "VALUES (?,1000000,1003600,5)", (icao,),
            )
            fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.execute(
                "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?,?,?)",
                (icao, fid, 1000000 + i),
            )
        self.conn.commit()
        _load_active(self.conn)
        assert len(_active) == 3

    def test_last_gs_restored_from_latest_position(self):
        """After restart, last_gs must come from the most recent position's gs."""
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000, 1020, 3)"
        )
        fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc',?,1000)",
            (fid,),
        )
        insert_position(self.conn, fid, 1010, lat=52.0, lon=21.0, gs=100.0)
        insert_position(self.conn, fid, 1020, lat=52.1, lon=21.1, gs=120.0)
        self.conn.commit()
        _load_active(self.conn)
        assert _active["aabbcc"]["last_gs"] == pytest.approx(120.0)

    def test_last_gs_none_when_latest_position_has_null_gs(self):
        """If the most recent position has NULL gs, last_gs must be None."""
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000, 1020, 2)"
        )
        fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc',?,1000)",
            (fid,),
        )
        insert_position(self.conn, fid, 1010, lat=52.0, lon=21.0, gs=100.0)
        insert_position(self.conn, fid, 1020, lat=52.1, lon=21.1, gs=None)
        self.conn.commit()
        _load_active(self.conn)
        assert _active["aabbcc"]["last_gs"] is None

    def test_last_gs_none_when_no_positions(self):
        """Active flight with no positions must have last_gs=None."""
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
            "VALUES ('aabbcc', 1000, 1000, 0)"
        )
        fid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('aabbcc',?,1000)",
            (fid,),
        )
        self.conn.commit()
        _load_active(self.conn)
        assert _active["aabbcc"]["last_gs"] is None

    def test_load_active_picks_highest_ts_not_highest_id(self):
        """_load_active must restore the latest fix by timestamp. Insert rows
        out of ts order so rowid order and ts order disagree."""
        from readsbstats.collector import _load_active, _active
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('abc123', 100, 200)"
        )
        fid = self.conn.execute("SELECT id FROM flights").fetchone()[0]
        self.conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('abc123', ?, 200)",
            (fid,),
        )
        # Lower rowid, HIGHER ts first; then higher rowid, LOWER ts — the old
        # ORDER BY id DESC wrongly picks the second one.
        insert_position(self.conn, fid, 200, lat=52.0, lon=21.0,
                        source_type="adsb_icao")
        insert_position(self.conn, fid, 150, lat=53.0, lon=20.0,
                        source_type="adsb_icao")
        self.conn.commit()
        _load_active(self.conn)
        assert _active["abc123"]["last_pos_ts"] == 200
        assert _active["abc123"]["last_lat"] == pytest.approx(52.0)


# ---------------------------------------------------------------------------
# _enrich — aircraft_db lookup + field merging
# ---------------------------------------------------------------------------

class TestEnrich:
    @pytest.fixture(autouse=True)
    def setup(self):
        enrichment.clear_cache()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_no_db_row_returns_original_fields(self):
        from readsbstats.collector import _enrich
        reg, atype, tdesc, flags, found = _enrich(self.conn, "aabbcc", "SP-ABC", "B738")
        assert reg == "SP-ABC"
        assert atype == "B738"
        assert tdesc is None
        assert flags == 0
        assert found is False

    def test_db_row_fills_missing_registration(self):
        from readsbstats.collector import _enrich
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('488001', 'SP-DB', 'A320', 'AIRBUS A320', 0)"
        )
        self.conn.commit()
        reg, atype, tdesc, flags, found = _enrich(self.conn, "488001", None, None)
        assert reg == "SP-DB"
        assert atype == "A320"
        assert tdesc == "AIRBUS A320"
        assert found is True

    def test_existing_registration_not_overwritten(self):
        from readsbstats.collector import _enrich
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('488001', 'SP-DB', 'A320', 'AIRBUS A320', 0)"
        )
        self.conn.commit()
        reg, _, _, _, _ = _enrich(self.conn, "488001", "SP-ORIG", None)
        assert reg == "SP-ORIG"

    def test_military_flags_returned(self):
        from readsbstats.collector import _enrich
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('488001', 'MIL', 'C130', 'HERCULES', 1)"
        )
        self.conn.commit()
        _, _, _, flags, found = _enrich(self.conn, "488001", None, None)
        assert flags == 1
        assert found is True


# ---------------------------------------------------------------------------
# _read_aircraft_json — error paths
# ---------------------------------------------------------------------------

class TestReadAircraftJson:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        yield

    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        from readsbstats import config
        from readsbstats.collector import _read_aircraft_json
        monkeypatch.setattr(config, "AIRCRAFT_JSON", str(tmp_path / "nonexistent.json"))
        from readsbstats import collector
        monkeypatch.setattr(collector, "config", config)
        assert _read_aircraft_json() is None

    def test_invalid_json_returns_none(self, monkeypatch, tmp_path):
        from readsbstats import config
        from readsbstats.collector import _read_aircraft_json
        bad_file = tmp_path / "aircraft.json"
        bad_file.write_text("{ this is not valid json")
        monkeypatch.setattr(config, "AIRCRAFT_JSON", str(bad_file))
        from readsbstats import collector
        monkeypatch.setattr(collector, "config", config)
        assert _read_aircraft_json() is None


# ---------------------------------------------------------------------------
# database.init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        for expected in ("flights", "positions", "active_flights",
                         "aircraft_db", "airlines", "photos", "schema_version"):
            assert expected in tables, f"missing table: {expected}"

    def test_records_schema_version(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        conn = database.connect(db_path)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        conn.close()
        assert row["version"] == database.SCHEMA_VERSION

    def test_idempotent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        database.init_db(db_path)
        database.init_db(db_path)  # must not raise


# ---------------------------------------------------------------------------
# enrichment — cache hit and short callsign paths
# ---------------------------------------------------------------------------

class TestEnrichmentCachePaths:
    @pytest.fixture(autouse=True)
    def setup(self):
        enrichment.clear_cache()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_lookup_airline_short_callsign_returns_none(self):
        assert enrichment.lookup_airline(self.conn, "LO") is None   # 2 chars
        assert enrichment.lookup_airline(self.conn, "") is None     # empty
        assert enrichment.lookup_airline(self.conn, None) is None   # None

    def test_lookup_airline_cache_hit(self):
        self.conn.execute(
            "INSERT INTO airlines (icao_code, name) VALUES ('LOT', 'LOT Polish Airlines')"
        )
        self.conn.commit()
        # First call populates cache
        result1 = enrichment.lookup_airline(self.conn, "LOT123")
        assert result1 == "LOT Polish Airlines"
        # Second call hits cache (line 48)
        result2 = enrichment.lookup_airline(self.conn, "LOT456")
        assert result2 == "LOT Polish Airlines"

    def test_lookup_aircraft_cache_hit(self):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code) "
            "VALUES ('488001', 'SP-ABC', 'B738')"
        )
        self.conn.commit()
        r1 = enrichment.lookup_aircraft(self.conn, "488001")
        r2 = enrichment.lookup_aircraft(self.conn, "488001")  # cache hit
        assert r1 is r2  # same dict object from cache


# ---------------------------------------------------------------------------
# _shutdown — lines 52-53
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_sets_running_false(self):
        from readsbstats import collector
        collector._running = True
        collector._shutdown(signal.SIGTERM, None)
        assert collector._running is False
        collector._running = True  # restore for other tests


# ---------------------------------------------------------------------------
# _close_flight: row is None — line 194
# ---------------------------------------------------------------------------

class TestCloseFlightOrphanedState:
    """Active state exists but the flights row has been deleted → row is None path."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_orphaned_active_state_does_not_raise(self):
        from readsbstats import collector
        collector._active["zzz001"] = {
            "flight_id": 99999, "last_seen": 1000, "last_pos_ts": 999
        }
        # flight_id 99999 does not exist in flights table
        with self.conn:
            collector._close_flight(self.conn, "zzz001")
        assert "zzz001" not in collector._active


# ---------------------------------------------------------------------------
# Notification exception handler in _poll — lines 477-478
# ---------------------------------------------------------------------------

class TestNotificationException:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()
        self.json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(self.json_path))
        monkeypatch.setattr("readsbstats.collector.config", config)
        yield
        self.conn.close()

    def _write_json(self, aircraft, now=None):
        self.json_path.write_text(
            json.dumps({"now": now or time.time(), "aircraft": aircraft})
        )

    def test_squawk_notification_exception_does_not_crash_poll(self, monkeypatch):
        from readsbstats import notifier
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)

        def bad_squawk(*a):
            raise RuntimeError("send failed")
        monkeypatch.setattr(notifier, "notify_squawk", bad_squawk)

        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "squawk": "7700"}
        ])

        from readsbstats.collector import _poll
        _poll(self.conn)  # must not raise


# ---------------------------------------------------------------------------
# _load_notified — lines 520-529
# ---------------------------------------------------------------------------

class TestLoadNotified:
    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_collector_state()
        self.conn = make_db()
        yield
        self.conn.close()

    def test_loads_military_icao(self):
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('ae0001', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES ('ae0001', 'MIL-1', 1)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "ae0001" in collector._notified_icao

    def test_loads_interesting_icao(self):
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('int001', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES ('int001', 'INT-1', 2)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "int001" in collector._notified_icao

    def test_ignores_ordinary_aircraft(self):
        # Use a real state-allocated hex (Poland 0x488000-0x48FFFF) — a
        # synthetic mnemonic like 'ord001' would now be flagged as anonymous
        # by the icao_ranges check (no state block contains 'ord').
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('488042', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES ('488042', 'ORD-1', 0)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "488042" not in collector._notified_icao

    def test_empty_db_leaves_set_empty(self):
        from readsbstats import collector
        collector._load_notified(self.conn)
        assert len(collector._notified_icao) == 0

    def test_loads_anonymous_icao_without_aircraft_db_row(self):
        """Non-ICAO hex (e.g. dd85cb) won't have an aircraft_db entry — the
        computed-at-query-time anon CASE must still pull it into the notified
        set so a restart doesn't re-alert."""
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('dd85cb', 1000, 1000)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "dd85cb" in collector._notified_icao

    def test_does_not_load_state_allocated_icao_without_flags(self):
        """Polish-allocated hex with no military/interesting flag must not be
        pre-loaded — that would suppress the FIRST real alert when we later
        spot it."""
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('488001', 1000, 1000)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "488001" not in collector._notified_icao

    def test_loads_adsbx_only_flagged_icao(self):
        """BE-4 (Audit 2026-05-31): a flight flagged interesting/military ONLY
        via adsbx_overrides (airplanes.live) with no aircraft_db row must be
        pre-loaded into the dedupe set, or a collector restart re-alerts for it.
        """
        from readsbstats import collector
        # State-allocated Polish hex so the anonymous CASE can't mask the test;
        # the ONLY flag source is adsbx_overrides.
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('488042', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO adsbx_overrides "
            "(icao_hex, flags, first_seen, last_seen) VALUES ('488042', 2, 1, 1)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "488042" in collector._notified_icao


# ---------------------------------------------------------------------------
# _dispatch_one — routes a queued notification tuple to the right notify_* helper
# ---------------------------------------------------------------------------

class TestDispatchOne:
    def test_routes_anon_kind_to_notify_anonymous(self, monkeypatch):
        from readsbstats import collector, notifier
        captured = {}
        def fake_anon(icao, reg, cs, td, at, dist):
            captured["args"] = (icao, reg, cs, td, at, dist)
        monkeypatch.setattr(notifier, "notify_anonymous", fake_anon)
        # Mirror the tuple shape that _poll() appends for kind="anon" — same
        # shape as the existing "mil" / "int" tuples.
        collector._dispatch_one(
            ("anon", "dd85cb", None, None, None, None, 107.1)
        )
        assert captured["args"] == ("dd85cb", None, None, None, None, 107.1)

    def test_mil_still_routes_to_notify_military(self, monkeypatch):
        # Regression guard — adding the anon branch must not break the
        # existing routing for military/interesting/squawk.
        from readsbstats import collector, notifier
        captured = []
        monkeypatch.setattr(notifier, "notify_military",
                            lambda *a: captured.append(("mil", a)))
        monkeypatch.setattr(notifier, "notify_anonymous",
                            lambda *a: captured.append(("anon", a)))
        collector._dispatch_one(("mil", "abc123", "REG", "CS", "Type", "TYP", 50.0))
        assert captured == [("mil", ("abc123", "REG", "CS", "Type", "TYP", 50.0))]

    def test_unknown_kind_logs_warning(self, monkeypatch, caplog):
        # Audit-13 A13-027: a typo or future-version notification kind
        # used to vanish silently. Now logs a warning so the alert loss
        # is visible in journalctl.
        from readsbstats import collector
        import logging
        with caplog.at_level(logging.WARNING, logger="readsbstats.collector"):
            collector._dispatch_one(("xyz", "abc123"))
        assert any("xyz" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _check_daily_summary — lines 534-544
# ---------------------------------------------------------------------------

class TestCheckDailySummary:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        from readsbstats import collector
        collector._last_summary_date = None
        collector._summary_time_warned = False
        _reset_telegram_state()
        # Enable Telegram by default for daily summary tests
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        yield
        from readsbstats import collector as _collector
        _collector._last_summary_date = None
        _collector._summary_time_warned = False
        _reset_telegram_state()

    def _patch_now(self, monkeypatch, dt):
        from readsbstats import collector

        class _FakeDT(_real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt

        monkeypatch.setattr(collector.datetime, "datetime", _FakeDT)

    def test_sends_at_matching_time(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "21:00")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        collector._check_daily_summary(conn)
        conn.close()

        assert sent == [1]
        assert collector._last_summary_date == _real_dt.date(2026, 4, 14)

    def test_does_not_send_at_wrong_time(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "21:00")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 15, 30))

        conn = make_db()
        collector._check_daily_summary(conn)
        conn.close()

        assert not sent

    def test_does_not_send_twice_same_day(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "21:00")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        collector._check_daily_summary(conn)
        collector._check_daily_summary(conn)  # same date → no second send
        conn.close()

        assert len(sent) == 1

    def test_invalid_summary_time_is_noop(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "not:valid:time")

        conn = make_db()
        collector._check_daily_summary(conn)
        conn.close()

        assert not sent

    def test_empty_summary_time_disables(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        collector._check_daily_summary(conn)
        conn.close()

        assert not sent

    def test_off_summary_time_disables(self, monkeypatch):
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "off")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        collector._check_daily_summary(conn)
        conn.close()

        assert not sent

    def test_invalid_summary_time_logs_warning(self, monkeypatch, caplog):
        """Invalid HH:MM like '25:00' or 'abc' logs a warning once."""
        import logging
        from readsbstats import collector, notifier
        sent = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: sent.append(1))
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "25:99")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        with caplog.at_level(logging.WARNING):
            collector._check_daily_summary(conn)
        conn.close()

        assert not sent
        assert any("RSBS_SUMMARY_TIME" in r.message for r in caplog.records)

    def test_summary_exception_is_swallowed(self, monkeypatch):
        from readsbstats import collector, notifier

        def bad_summary(c):
            raise RuntimeError("send failed")

        monkeypatch.setattr(notifier, "send_daily_summary", bad_summary)
        monkeypatch.setattr(config, "TELEGRAM_SUMMARY_TIME", "21:00")
        self._patch_now(monkeypatch, _real_dt.datetime(2026, 4, 14, 21, 0))

        conn = make_db()
        collector._check_daily_summary(conn)  # must not raise
        conn.close()


# ---------------------------------------------------------------------------
# main() — lines 552-592
# ---------------------------------------------------------------------------

class TestMain:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        _reset_collector_state()
        enrichment.clear_cache()
        from readsbstats import collector, database
        collector._running = True
        # Stub the daemon threads main() spawns: the watchdog busy-loops with
        # monkeypatched sleep, and run_background_migrations would close the
        # shared in-memory connection out from under the main loop.
        monkeypatch.setattr(database, "run_background_migrations",
                            lambda *a, **kw: None)
        monkeypatch.setattr(collector, "_watchdog_loop", lambda: None)
        yield
        collector._running = True  # always restore

    @staticmethod
    def _sleep_exit():
        from readsbstats import collector
        def fake_sleep(s):
            collector._running = False
        return fake_sleep

    def test_basic_run(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        poll_calls = []
        monkeypatch.setattr(collector, "_poll", lambda c: poll_calls.append(1))
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()
        assert poll_calls == [1]

    def test_poll_exception_is_swallowed(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        monkeypatch.setattr(collector, "_poll",
                            lambda c: (_ for _ in ()).throw(RuntimeError("poll error")))
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()  # must not raise

    def test_purge_triggered(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        monkeypatch.setattr(collector, "_poll", lambda c: None)
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        # Zero interval means purge always triggers
        monkeypatch.setattr(config, "PURGE_INTERVAL_SEC", 0)
        purge_calls = []
        monkeypatch.setattr(collector, "_purge", lambda c: purge_calls.append(1))
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()
        assert purge_calls == [1]

    def test_purge_exception_is_swallowed(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        monkeypatch.setattr(collector, "_poll", lambda c: None)
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        monkeypatch.setattr(config, "PURGE_INTERVAL_SEC", 0)
        monkeypatch.setattr(collector, "_purge",
                            lambda c: (_ for _ in ()).throw(RuntimeError("purge error")))
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()  # must not raise

    def test_shutdown_closes_active_flights(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions)"
            " VALUES ('abc123', 1000, 1000, 5)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('abc123', ?, 1000)",
            (fid,),
        )
        conn.commit()

        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        monkeypatch.setattr(collector, "_poll", lambda c: None)
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()
        assert "abc123" not in collector._active

    def test_shutdown_exception_is_swallowed(self, monkeypatch):
        from readsbstats import collector, database, notifier
        conn = make_db()
        conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions)"
            " VALUES ('abc123', 1000, 1000, 5)"
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES ('abc123', ?, 1000)",
            (fid,),
        )
        conn.commit()

        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda p=None: conn)
        monkeypatch.setattr(notifier, "start_command_listener", lambda p: None)
        monkeypatch.setattr(collector, "_poll", lambda c: None)
        monkeypatch.setattr(collector, "_check_daily_summary", lambda c: None)
        monkeypatch.setattr(collector, "_close_flight",
                            lambda c, icao: (_ for _ in ()).throw(RuntimeError("close failed")))
        monkeypatch.setattr(time, "sleep", self._sleep_exit())

        collector.main()  # must not raise


# ---------------------------------------------------------------------------
# Watchlist matching — _poll integration
# ---------------------------------------------------------------------------

class TestWatchlistAlerts:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        _reset_collector_state()
        enrichment.clear_cache()
        self.conn = make_db()
        self.json_path = tmp_path / "aircraft.json"
        monkeypatch.setattr("readsbstats.config.AIRCRAFT_JSON", str(self.json_path))
        monkeypatch.setattr("readsbstats.collector.config", config)
        # Enable Telegram for watchlist notification tests
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        from readsbstats import notifier
        monkeypatch.setattr(notifier, "notify_military",    lambda *a: None)
        monkeypatch.setattr(notifier, "notify_interesting", lambda *a: None)
        monkeypatch.setattr(notifier, "notify_squawk",      lambda *a: None)
        self.wl_calls = []
        monkeypatch.setattr(notifier, "notify_watchlist",
                            lambda *a: self.wl_calls.append(a))
        yield
        self.conn.close()

    def _write_json(self, aircraft, now=None):
        self.json_path.write_text(json.dumps(
            {"now": now or time.time(), "aircraft": aircraft}
        ))

    def _add_watchlist(self, match_type, value, label=None):
        self.conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,?,?)",
            (match_type, value.lower(), label, int(time.time())),
        )
        self.conn.commit()

    def _poll_and_join(self):
        from readsbstats import collector
        from readsbstats.collector import _poll
        _poll(self.conn)
        if collector._notifications_thread:
            collector._notifications_thread.join(timeout=1)

    def test_icao_match_fires_alert(self):
        self._add_watchlist("icao", "aabbcc")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        self._poll_and_join()
        assert len(self.wl_calls) == 1
        assert self.wl_calls[0][0] == "aabbcc"

    def test_registration_match_fires_alert(self):
        self._add_watchlist("registration", "sp-lrf")
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration) VALUES ('aabbcc', 'SP-LRF')"
        )
        self.conn.commit()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        self._poll_and_join()
        assert len(self.wl_calls) == 1

    def test_callsign_prefix_match_fires_alert(self):
        self._add_watchlist("callsign_prefix", "lot")
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "flight": "LOT123"}
        ])
        self._poll_and_join()
        assert len(self.wl_calls) == 1

    def test_no_match_does_not_fire(self):
        from readsbstats.collector import _poll
        self._add_watchlist("icao", "111111")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)  # no thread started — nothing to join
        assert len(self.wl_calls) == 0

    def test_no_duplicate_alert_same_flight(self):
        """Second poll for the same open flight must not re-trigger the alert."""
        self._add_watchlist("icao", "aabbcc")
        now = time.time()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        self._poll_and_join()
        # Write a second position within the same flight (no gap)
        self.json_path.write_text(json.dumps({
            "now": now + 5,
            "aircraft": [{"hex": "aabbcc", "lat": 52.1, "lon": 21.1, "seen_pos": 0}],
        }))
        from readsbstats.collector import _poll
        _poll(self.conn)  # no new notification → no thread started
        assert len(self.wl_calls) == 1  # still only one alert

    def test_label_passed_to_notify(self):
        self._add_watchlist("icao", "aabbcc", label="Neighbour's plane")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        self._poll_and_join()
        # notify_watchlist(icao, reg, callsign, type_desc, aircraft_type, dist, label, flight_id)
        assert self.wl_calls[0][6] == "Neighbour's plane"

    def test_empty_watchlist_no_alert(self):
        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)  # no watchlist match → no thread started
        assert len(self.wl_calls) == 0


# ---------------------------------------------------------------------------
# Notification queue / consumer
# ---------------------------------------------------------------------------

class TestNotificationConsumer:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        _reset_collector_state()
        from readsbstats import collector, notifier
        # Drain any leftover queue items from earlier tests (defensive).
        collector._drain_notifications(timeout=0.1)
        # Re-prime telegram state.
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        # Stub notify_* so we observe dispatch order.
        self.calls: list = []
        monkeypatch.setattr(notifier, "notify_military",
                            lambda *a: self.calls.append(("mil", a)))
        monkeypatch.setattr(notifier, "notify_squawk",
                            lambda *a: self.calls.append(("sqk", a)))
        yield

    def test_consumer_dispatches_in_fifo_order(self):
        from readsbstats import collector
        collector.start_notification_consumer()
        collector._notification_queue.put(
            ("mil", "aabbcc", "REG1", None, "Type1", "T1", 100.0),
        )
        collector._notification_queue.put(
            ("sqk", "ddeeff", "REG2", None, "7700", 50.0),
        )
        collector._drain_notifications(timeout=1.0)
        assert [c[0] for c in self.calls] == ["mil", "sqk"]

    def test_consumer_survives_dispatch_exception(self, monkeypatch):
        from readsbstats import collector, notifier
        # First call raises; second must still be processed.
        boom_called = []
        def boom(*a):
            boom_called.append(a)
            raise RuntimeError("simulated dispatch error")
        monkeypatch.setattr(notifier, "notify_military", boom)
        ok_called = []
        monkeypatch.setattr(notifier, "notify_squawk",
                            lambda *a: ok_called.append(a))

        collector.start_notification_consumer()
        collector._notification_queue.put(
            ("mil", "aabbcc", "REG1", None, "T", "T1", 100.0),
        )
        collector._notification_queue.put(
            ("sqk", "ddeeff", "REG2", None, "7700", 50.0),
        )
        collector._drain_notifications(timeout=1.0)
        assert len(boom_called) == 1
        assert len(ok_called) == 1

    def test_consumer_logs_dispatch_exception_and_keeps_draining(self, monkeypatch, caplog):
        """A raising notify_* dispatch must be LOGGED (collector.py ~:618
        `log.exception("Notification dispatch error")`), not swallowed
        silently — the log line is the only thing that surfaces a dropped
        Telegram alert in journalctl. The sibling
        test_consumer_survives_dispatch_exception proves the thread keeps
        draining; this one asserts the error is actually observable AND that
        the second item is still processed (thread stayed alive)."""
        import logging
        from readsbstats import collector, notifier

        def boom(*a):
            raise RuntimeError("simulated dispatch error")
        monkeypatch.setattr(notifier, "notify_military", boom)
        ok_called = []
        monkeypatch.setattr(notifier, "notify_squawk",
                            lambda *a: ok_called.append(a))

        with caplog.at_level(logging.ERROR, logger="readsbstats.collector"):
            collector.start_notification_consumer()
            collector._notification_queue.put(
                ("mil", "aabbcc", "REG1", None, "T", "T1", 100.0),
            )
            collector._notification_queue.put(
                ("sqk", "ddeeff", "REG2", None, "7700", 50.0),
            )
            collector._drain_notifications(timeout=1.0)

        # The raising dispatch was logged (visible in journalctl, not dropped).
        err_records = [
            rec for rec in caplog.records
            if "Notification dispatch error" in rec.message
        ]
        assert err_records, "raising dispatch must be logged"
        # log.exception → record carries the traceback for the original error.
        assert err_records[0].exc_info is not None
        # …and the consumer kept draining: the second item was still processed.
        assert len(ok_called) == 1

    def test_start_notification_consumer_is_idempotent(self):
        from readsbstats import collector
        t1 = collector.start_notification_consumer()
        t2 = collector.start_notification_consumer()
        assert t1 is t2
        assert t1.daemon is True
        assert t1.name == "tg-dispatch"

    def test_drain_returns_when_queue_empty(self):
        from readsbstats import collector
        # No work enqueued — drain must return immediately within timeout.
        collector._drain_notifications(timeout=0.5)
        assert collector._notification_queue.unfinished_tasks == 0

    def test_stop_notification_consumer_drains_pending_before_join(self):
        """Audit-12 #145 — on shutdown, queued items must be dispatched
        BEFORE the consumer thread is joined. Previously the consumer was
        a daemon thread that the interpreter killed abruptly, so any
        alerts queued in the last poll cycle were silently dropped."""
        from readsbstats import collector
        collector.start_notification_consumer()

        # Enqueue work and immediately ask for shutdown — the helper must
        # drain the queue first, then stop the consumer.
        collector._notification_queue.put(
            ("mil", "aabbcc", "REG1", None, "Type1", "T1", 100.0),
        )
        collector._notification_queue.put(
            ("sqk", "ddeeff", "REG2", None, "7700", 50.0),
        )

        collector.stop_notification_consumer(timeout=2.0)

        # All items processed (FIFO):
        assert [c[0] for c in self.calls] == ["mil", "sqk"]
        # Thread has exited:
        assert collector._consumer_thread is None or not collector._consumer_thread.is_alive()
        # Queue is fully drained:
        assert collector._notification_queue.unfinished_tasks == 0

    def test_stop_notification_consumer_is_noop_when_not_started(self):
        from readsbstats import collector
        # Ensure no thread is alive at entry
        if collector._consumer_thread is not None:
            collector._notification_queue.put(None)
            collector._consumer_thread.join(timeout=1.0)
            collector._consumer_thread = None
        # Must not raise
        collector.stop_notification_consumer(timeout=0.5)


# ---------------------------------------------------------------------------
# Consumer thread reuses one sqlite connection
# ---------------------------------------------------------------------------

class TestConsumerSqliteReuse:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        _reset_collector_state()
        from readsbstats import collector, database, notifier
        # Stop any previously started consumer so we can start a fresh one
        # against our test DB path.
        if collector._consumer_thread and collector._consumer_thread.is_alive():
            collector._notification_queue.put(None)  # sentinel → exits
            collector._consumer_thread.join(timeout=1.0)
            collector._consumer_thread = None
            collector._notifications_thread = None

        db_path = str(tmp_path / "consumer.db")
        database.init_db(db_path)
        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")

        # Stub Telegram-sending so we don't make network calls.
        monkeypatch.setattr(notifier, "notify_military",
                            lambda *a, **kw: None)
        yield
        # Tear down the consumer cleanly so it doesn't leak into other tests.
        if collector._consumer_thread and collector._consumer_thread.is_alive():
            collector._notification_queue.put(None)
            collector._consumer_thread.join(timeout=1.0)
            collector._consumer_thread = None
            collector._notifications_thread = None

    def test_consumer_opens_one_connection_for_its_lifetime(self, monkeypatch):
        """Spawning the consumer should call database.connect() exactly once;
        subsequent alerts must reuse the same connection via the thread-local."""
        from readsbstats import collector, database, notifier

        connect_calls = []
        real_connect = database.connect
        def counting_connect(path=None):
            connect_calls.append(path)
            return real_connect(path)
        monkeypatch.setattr(database, "connect", counting_connect)

        # Capture thread-local state inside the consumer (the main thread
        # can't see it directly).
        observed = {"conn_set_in_consumer": False}
        orig_notify = notifier.notify_military
        def watching_notify(*a, **kw):
            if getattr(notifier._thread_local, "conn", None) is not None:
                observed["conn_set_in_consumer"] = True
            return orig_notify(*a, **kw)
        monkeypatch.setattr(notifier, "notify_military", watching_notify)

        collector.start_notification_consumer()
        # Enqueue two alerts; each one would have opened its own connection
        # under the old code path.
        for _ in range(2):
            collector._notification_queue.put(
                ("mil", "aabbcc", "REG", None, "T", "T1", 100.0),
            )
        collector._drain_notifications(timeout=2.0)
        # Exactly one connect call from the consumer; tests that pre-init the
        # DB above use the unpatched connect.
        assert len(connect_calls) == 1
        # Inside the consumer thread, the thread-local must be populated when
        # a dispatched alert runs.
        assert observed["conn_set_in_consumer"] is True


# ---------------------------------------------------------------------------
# PERF-3 — notification queue is bounded; hot-path enqueue sheds load
# ---------------------------------------------------------------------------

class TestNotificationQueueBounded:
    """The dispatch queue must be bounded so a slow Telegram/CDN during a
    burst can't grow it without limit (the consumer does DB lookups + up to
    10 MB photo downloads per item). The hot-path producer in `_poll` must
    never block on a full queue — a blocking put would stall the systemd
    watchdog — so it drops the item and logs a warning instead."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        _reset_collector_state()
        from readsbstats import collector
        import queue as _queue
        # Capture the REAL module-level bound before swapping, so
        # test_queue_is_bounded asserts against the production default rather
        # than this fixture's replacement.
        self.real_maxsize = collector._notification_queue.maxsize
        # Use a fresh bounded queue per test so filling it can't poison other
        # tests; monkeypatch auto-restores the module global on teardown.
        # Hardcode the bound under test (not derived from the global, which is
        # still unbounded in the RED phase — deriving 0 would make .full() loop
        # forever below).
        self._q = _queue.Queue(maxsize=500)
        monkeypatch.setattr(collector, "_notification_queue", self._q)
        yield

    def test_queue_is_bounded(self):
        # A bound large enough that normal bursts never hit it, but finite.
        # Asserts the production module-level default (captured pre-swap).
        assert self.real_maxsize == 500

    def test_hot_path_enqueue_drops_and_warns_when_full(self, caplog):
        """Fill the queue to maxsize, then the next hot-path enqueue must NOT
        raise (no blocking, no crash) and MUST log a drop — backpressure sheds
        load and the service keeps running."""
        import logging
        from readsbstats import collector
        item = ("mil", "aabbcc", "REG1", None, "Type1", "T1", 100.0)
        # Saturate the queue.
        while not self._q.full():
            self._q.put_nowait(item)
        assert self._q.full()
        assert self._q.qsize() == 500

        with caplog.at_level(logging.WARNING, logger="readsbstats.collector"):
            # Must return normally (sheds load) rather than block or raise.
            collector._enqueue_alert(item)

        # The over-limit item was dropped, not enqueued.
        assert self._q.qsize() == 500
        assert any(
            "notification queue full" in rec.getMessage().lower()
            for rec in caplog.records
        ), "a dropped hot-path alert must be logged at WARNING"

    def test_hot_path_enqueue_succeeds_with_room(self):
        """With capacity available the hot-path enqueue stores the item."""
        from readsbstats import collector
        item = ("sqk", "ddeeff", "REG2", None, "7700", 50.0)
        collector._enqueue_alert(item)
        assert self._q.qsize() == 1
        assert self._q.get_nowait() == item
