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

def make_db() -> sqlite3.Connection:
    """Fresh in-memory DB with full schema and migrations applied."""
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


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
            "SELECT p.gs, f.max_gs FROM positions p JOIN flights f ON f.id = p.flight_id"
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
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
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
            " VALUES ('mil001', 'MIL-1', 'F16', 1)"  # flags=1 = military
        )
        self.conn.commit()
        self._write_json([
            {"hex": "mil001", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1200.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'mil001'"
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
            " VALUES ('mil001', 'MIL-1', 'F16', 1)"
        )
        self.conn.commit()
        self._write_json([
            {"hex": "mil001", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1900.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'mil001'"
        ).fetchone()
        assert row["gs"] is None

    def test_poll_nulls_gs_for_unknown_aircraft_above_military_limit(self, monkeypatch):
        """Aircraft not in aircraft_db use the military limit (1800 kts)."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        # No aircraft_db row for 'unknown1'
        self._write_json([
            {"hex": "unknown1", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 1900.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'unknown1'"
        ).fetchone()
        assert row["gs"] is None

    def test_poll_keeps_gs_for_unknown_aircraft_below_military_limit(self, monkeypatch):
        """Unknown aircraft with GS below military limit (e.g. 800 kts) keep their GS."""
        from readsbstats import config
        from readsbstats.collector import _poll
        monkeypatch.setattr(config, "MAX_GS_CIVIL_KTS", 750)
        monkeypatch.setattr(config, "MAX_GS_MILITARY_KTS", 1800)
        self._write_json([
            {"hex": "unknown1", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "gs": 800.0},
        ])
        _poll(self.conn)
        row = self.conn.execute(
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
            " WHERE f.icao_hex = 'unknown1'"
        ).fetchone()
        assert row["gs"] == pytest.approx(800.0)


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
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
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
            "SELECT p.gs FROM positions p JOIN flights f ON f.id = p.flight_id"
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
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?,?,52,21)",
            (flight_id, ts),
        )
        self.conn.commit()

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
        from readsbstats.collector import _poll
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0,
             "squawk": "7700"},
        ])
        _poll(self.conn)
        assert len(self.squawk_calls) == 1
        assert self.squawk_calls[0][3] == "7700"  # squawk value in args

    def test_emergency_squawk_not_repeated_same_flight(self):
        """Same flight_id with the same emergency squawk must only notify once."""
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

        assert len(self.squawk_calls) == 1  # still only one notification

    def test_all_three_emergency_squawks_trigger(self):
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
        from readsbstats import notifier
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
        assert len(mil_calls) == 1

    def test_military_second_sighting_no_repeat_notification(self, monkeypatch):
        """Subsequent flights for the same military ICAO must not re-notify."""
        from readsbstats import config, notifier
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
        fid = _active["aabbcc"]["flight_id"]
        self.conn.execute("UPDATE flights SET total_positions=5 WHERE id=?", (fid,))
        self.conn.commit()

        # Gap → second flight
        self.json_path.write_text(json.dumps({
            "now": now + gap,
            "aircraft": [{"hex": "aabbcc", "lat": 52.5, "lon": 21.5, "seen_pos": 0}],
        }))
        _poll(self.conn)
        assert len(mil_calls) == 1  # still only one notification

    def test_interesting_first_sighting_queues_notification(self, monkeypatch):
        """First new flight for an interesting ICAO must call notify_interesting."""
        from readsbstats import notifier
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
        assert len(int_calls) == 1


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
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?,1007200,52.0,21.0)",
            (fid,),
        )
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
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, gs) VALUES (?,1010,52.0,21.0,100.0)",
            (fid,),
        )
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, gs) VALUES (?,1020,52.1,21.1,120.0)",
            (fid,),
        )
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
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, gs) VALUES (?,1010,52.0,21.0,100.0)",
            (fid,),
        )
        self.conn.execute(
            "INSERT INTO positions (flight_id, ts, lat, lon, gs) VALUES (?,1020,52.1,21.1,NULL)",
            (fid,),
        )
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
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('mil001', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES ('mil001', 'MIL-1', 1)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "mil001" in collector._notified_icao

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
        from readsbstats import collector
        self.conn.execute(
            "INSERT INTO flights (icao_hex, first_seen, last_seen) VALUES ('ord001', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES ('ord001', 'ORD-1', 0)"
        )
        self.conn.commit()
        collector._load_notified(self.conn)
        assert "ord001" not in collector._notified_icao

    def test_empty_db_leaves_set_empty(self):
        from readsbstats import collector
        collector._load_notified(self.conn)
        assert len(collector._notified_icao) == 0


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
        from readsbstats import collector
        collector._running = True
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

    def test_icao_match_fires_alert(self):
        from readsbstats.collector import _poll
        self._add_watchlist("icao", "aabbcc")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        assert len(self.wl_calls) == 1
        assert self.wl_calls[0][0] == "aabbcc"

    def test_registration_match_fires_alert(self):
        from readsbstats.collector import _poll
        self._add_watchlist("registration", "sp-lrf")
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration) VALUES ('aabbcc', 'SP-LRF')"
        )
        self.conn.commit()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        assert len(self.wl_calls) == 1

    def test_callsign_prefix_match_fires_alert(self):
        from readsbstats.collector import _poll
        self._add_watchlist("callsign_prefix", "lot")
        self._write_json([
            {"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0, "flight": "LOT123"}
        ])
        _poll(self.conn)
        assert len(self.wl_calls) == 1

    def test_no_match_does_not_fire(self):
        from readsbstats.collector import _poll
        self._add_watchlist("icao", "111111")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        assert len(self.wl_calls) == 0

    def test_no_duplicate_alert_same_flight(self):
        """Second poll for the same open flight must not re-trigger the alert."""
        from readsbstats.collector import _poll
        self._add_watchlist("icao", "aabbcc")
        now = time.time()
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}], now)
        _poll(self.conn)
        # Write a second position within the same flight (no gap)
        self.json_path.write_text(json.dumps({
            "now": now + 5,
            "aircraft": [{"hex": "aabbcc", "lat": 52.1, "lon": 21.1, "seen_pos": 0}],
        }))
        _poll(self.conn)
        assert len(self.wl_calls) == 1  # still only one alert

    def test_label_passed_to_notify(self):
        from readsbstats.collector import _poll
        self._add_watchlist("icao", "aabbcc", label="Neighbour's plane")
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        # notify_watchlist(icao, reg, callsign, type_desc, aircraft_type, dist, label, flight_id)
        assert self.wl_calls[0][6] == "Neighbour's plane"

    def test_empty_watchlist_no_alert(self):
        from readsbstats.collector import _poll
        self._write_json([{"hex": "aabbcc", "lat": 52.0, "lon": 21.0, "seen_pos": 0}])
        _poll(self.conn)
        assert len(self.wl_calls) == 0
