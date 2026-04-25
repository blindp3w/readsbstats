"""
Tests for sim.py — local development aircraft simulator.
No real filesystem writes except where a tmp_path is provided.
"""
from __future__ import annotations

import json
import math
import time

import pytest

from readsbstats import sim


# ---------------------------------------------------------------------------
# _make_state
# ---------------------------------------------------------------------------

class TestMakeState:
    def test_returns_expected_keys(self):
        state = sim._make_state(0)
        assert set(state) == {"radius_nm", "bearing0", "speed_dps", "alt_ft",
                              "gs_kts", "squawk", "rssi", "source"}

    def test_deterministic_with_same_seed(self):
        assert sim._make_state(7) == sim._make_state(7)

    def test_different_seeds_differ(self):
        assert sim._make_state(0) != sim._make_state(1)

    def test_radius_in_range(self):
        for seed in range(20):
            state = sim._make_state(seed)
            assert 30 <= state["radius_nm"] <= 380

    def test_bearing_in_range(self):
        for seed in range(20):
            state = sim._make_state(seed)
            assert 0 <= state["bearing0"] < 360

    def test_squawk_is_four_digit_string(self):
        for seed in range(20):
            state = sim._make_state(seed)
            assert len(state["squawk"]) == 4
            assert state["squawk"].isdigit()

    def test_source_is_valid(self):
        valid = {"adsb_icao", "mlat"}
        for seed in range(20):
            assert sim._make_state(seed)["source"] in valid


# ---------------------------------------------------------------------------
# _bearing_to_latlon
# ---------------------------------------------------------------------------

class TestBearingToLatlon:
    def test_north_increases_latitude(self):
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=0, dist_nm=60)
        assert lat > 52.0
        assert abs(lon - 21.0) < 0.01  # barely changes

    def test_south_decreases_latitude(self):
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=180, dist_nm=60)
        assert lat < 52.0

    def test_east_increases_longitude(self):
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=90, dist_nm=60)
        assert lon > 21.0
        assert abs(lat - 52.0) < 0.01

    def test_west_decreases_longitude(self):
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=270, dist_nm=60)
        assert lon < 21.0

    def test_zero_distance_returns_origin(self):
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=45, dist_nm=0)
        assert lat == pytest.approx(52.0)
        assert lon == pytest.approx(21.0)

    def test_north_60nm_approx_one_degree(self):
        """1 nm ≈ 1/60°, so 60 nm north ≈ 1° latitude change."""
        lat, lon = sim._bearing_to_latlon(52.0, 21.0, bearing_deg=0, dist_nm=60)
        assert lat == pytest.approx(53.0, abs=0.01)


# ---------------------------------------------------------------------------
# _build_aircraft_list
# ---------------------------------------------------------------------------

class TestBuildAircraftList:
    def test_returns_all_aircraft(self):
        result = sim._build_aircraft_list(now=0.0)
        assert len(result) == len(sim.AIRCRAFT_DEFS)

    def test_each_entry_has_required_fields(self):
        required = {"hex", "type", "flight", "r", "t", "category",
                    "lat", "lon", "alt_baro", "alt_geom", "gs", "track",
                    "baro_rate", "squawk", "messages", "rssi", "seen", "seen_pos"}
        for entry in sim._build_aircraft_list(now=0.0):
            assert required.issubset(entry.keys()), f"missing fields in {entry}"

    def test_icao_hex_matches_definitions(self):
        result = sim._build_aircraft_list(now=0.0)
        expected_icaos = {defn[0] for defn in sim.AIRCRAFT_DEFS}
        actual_icaos = {e["hex"] for e in result}
        assert actual_icaos == expected_icaos

    def test_lat_lon_are_floats_in_plausible_range(self):
        for entry in sim._build_aircraft_list(now=0.0):
            assert -90 <= entry["lat"] <= 90
            assert -180 <= entry["lon"] <= 180

    def test_now_affects_position(self):
        r1 = sim._build_aircraft_list(now=0.0)
        r2 = sim._build_aircraft_list(now=3600.0)
        # At least one aircraft should have moved
        any_moved = any(
            e1["lat"] != e2["lat"] or e1["lon"] != e2["lon"]
            for e1, e2 in zip(r1, r2)
        )
        assert any_moved


# ---------------------------------------------------------------------------
# STATES module-level initialisation
# ---------------------------------------------------------------------------

class TestStates:
    def test_states_keys_match_aircraft_defs(self):
        expected = {defn[0] for defn in sim.AIRCRAFT_DEFS}
        assert set(sim.STATES.keys()) == expected

    def test_each_state_has_all_fields(self):
        for icao, state in sim.STATES.items():
            assert "radius_nm" in state, f"missing radius_nm for {icao}"


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_writes_valid_json_and_exits_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        out = str(tmp_path / "aircraft.json")
        monkeypatch.setattr(sim, "OUTPUT_PATH", out)
        monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            sim.main()

        assert (tmp_path / "aircraft.json").exists()
        data = json.loads((tmp_path / "aircraft.json").read_text())
        assert "aircraft" in data
        assert "now" in data
        assert "messages" in data
        assert len(data["aircraft"]) == len(sim.AIRCRAFT_DEFS)

    def test_aircraft_count_in_output(self, tmp_path, monkeypatch):
        out = str(tmp_path / "aircraft.json")
        monkeypatch.setattr(sim, "OUTPUT_PATH", out)
        monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            sim.main()

        data = json.loads((tmp_path / "aircraft.json").read_text())
        assert len(data["aircraft"]) == 8  # len(AIRCRAFT_DEFS)
