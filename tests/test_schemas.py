"""Validation tests for the Pydantic response contracts in schemas.py.

The key-parity tests in test_web.py compare emitted dict *keys*; they don't
prove the models enforce field types/required-ness or that handler output
validates against them. A type change or a non-Optional field returning None
would otherwise surface only as a 500 at serialization time (audit 2026-06-15).
"""

import pytest
from pydantic import ValidationError

from readsbstats import schemas


class TestPositionChart:
    def test_valid_minimal(self):
        m = schemas.PositionChart.model_validate({"ts": 123})
        assert m.ts == 123

    def test_missing_required_ts_raises(self):
        with pytest.raises(ValidationError):
            schemas.PositionChart.model_validate({"lat": 1.0})

    def test_wrong_type_ts_raises(self):
        with pytest.raises(ValidationError):
            schemas.PositionChart.model_validate({"ts": "not-an-int"})

    def test_extra_columns_are_preserved(self):
        # ApiModel uses extra="allow" so an undeclared SELECT column is kept,
        # never dropped — this is the no-behaviour-change guarantee.
        m = schemas.PositionChart.model_validate({"ts": 1, "rssi_x": -42})
        assert m.model_dump().get("rssi_x") == -42


class TestFlightMeta:
    def test_valid_minimal(self):
        m = schemas.FlightMeta.model_validate({"id": 1, "icao_hex": "abc123"})
        assert m.id == 1 and m.icao_hex == "abc123"

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            schemas.FlightMeta.model_validate({"icao_hex": "abc123"})  # no id
        with pytest.raises(ValidationError):
            schemas.FlightMeta.model_validate({"id": 1})  # no icao_hex

    def test_wrong_type_id_raises(self):
        with pytest.raises(ValidationError):
            schemas.FlightMeta.model_validate({"id": "x", "icao_hex": "abc123"})


class TestFlightDetailResponse:
    def test_valid_minimal(self):
        m = schemas.FlightDetailResponse.model_validate(
            {
                "flight": {"id": 1, "icao_hex": "abc123"},
                "positions": [],
                "other_flights": [],
            }
        )
        assert m.flight.id == 1
        assert m.positions == []

    def test_missing_required_flight_raises(self):
        with pytest.raises(ValidationError):
            schemas.FlightDetailResponse.model_validate(
                {"positions": [], "other_flights": []}
            )

    def test_positions_must_be_a_list(self):
        with pytest.raises(ValidationError):
            schemas.FlightDetailResponse.model_validate(
                {"flight": {"id": 1, "icao_hex": "abc123"}, "positions": "x", "other_flights": []}
            )


class TestMapSnapshotResponse:
    def test_valid_minimal(self):
        m = schemas.MapSnapshotResponse.model_validate(
            {"at": 1000, "is_live": True, "aircraft": []}
        )
        assert m.at == 1000 and m.is_live is True

    def test_missing_required_at_raises(self):
        with pytest.raises(ValidationError):
            schemas.MapSnapshotResponse.model_validate({"is_live": True, "aircraft": []})


class TestVdl2MessagesResponse:
    def test_valid_with_one_message(self):
        m = schemas.Vdl2MessagesResponse.model_validate(
            {"messages": [{"id": 1, "ts": 2}]}
        )
        assert m.messages[0].id == 1

    def test_message_missing_required_raises(self):
        with pytest.raises(ValidationError):
            schemas.Vdl2MessagesResponse.model_validate({"messages": [{"ts": 2}]})  # no id


class TestStatsResponse:
    # StatsResponse is all-Optional, so there's no missing-required case; pin a
    # wrong-type instead (a string that can't coerce to int).
    def test_wrong_type_raises(self):
        with pytest.raises(ValidationError):
            schemas.StatsResponse.model_validate({"total_flights": "not-an-int"})

    def test_empty_is_valid(self):
        assert schemas.StatsResponse.model_validate({}).total_flights is None
