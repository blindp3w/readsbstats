"""Tests for readsbstats.config — error paths and fallback behaviour."""

import importlib
import os

import pytest


class TestIntHelper:
    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("RSBS_TEST_INT", "42")
        from readsbstats.config import _int
        assert _int("RSBS_TEST_INT", "10") == 42

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_TEST_INT", "abc")
        from readsbstats.config import _int
        result = _int("RSBS_TEST_INT", "10")
        assert result == 10
        assert "abc" in capsys.readouterr().err

    def test_empty_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_TEST_INT", "")
        from readsbstats.config import _int
        result = _int("RSBS_TEST_INT", "5")
        assert result == 5

    def test_missing_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("RSBS_TEST_INT", raising=False)
        from readsbstats.config import _int
        assert _int("RSBS_TEST_INT", "99") == 99

    def test_float_string_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_TEST_INT", "3.14")
        from readsbstats.config import _int
        result = _int("RSBS_TEST_INT", "7")
        assert result == 7
        assert "3.14" in capsys.readouterr().err


class TestFloatHelper:
    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("RSBS_TEST_FLOAT", "3.14")
        from readsbstats.config import _float
        assert _float("RSBS_TEST_FLOAT", "1.0") == 3.14

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_TEST_FLOAT", "xyz")
        from readsbstats.config import _float
        result = _float("RSBS_TEST_FLOAT", "2.5")
        assert result == 2.5
        assert "xyz" in capsys.readouterr().err

    def test_integer_string_is_valid(self, monkeypatch):
        monkeypatch.setenv("RSBS_TEST_FLOAT", "42")
        from readsbstats.config import _float
        assert _float("RSBS_TEST_FLOAT", "1.0") == 42.0

    def test_missing_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("RSBS_TEST_FLOAT", raising=False)
        from readsbstats.config import _float
        assert _float("RSBS_TEST_FLOAT", "9.9") == 9.9


class TestBoolHelper:
    """Audit-12 #197 — `_bool` replaced five drifted patterns
    (`os.getenv(...) not in (...)`). The tests pin its single contract."""

    def test_unset_returns_default(self, monkeypatch):
        from readsbstats.config import _bool
        monkeypatch.delenv("RSBS_TEST_BOOL", raising=False)
        assert _bool("RSBS_TEST_BOOL", default=True) is True
        assert _bool("RSBS_TEST_BOOL", default=False) is False

    @pytest.mark.parametrize("val", ["", "0", "false", "FALSE", "no", "NO", "off", "OFF"])
    def test_falsy_values(self, monkeypatch, val):
        from readsbstats.config import _bool
        monkeypatch.setenv("RSBS_TEST_BOOL", val)
        assert _bool("RSBS_TEST_BOOL", default=True) is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "anything-else"])
    def test_truthy_values(self, monkeypatch, val):
        from readsbstats.config import _bool
        monkeypatch.setenv("RSBS_TEST_BOOL", val)
        assert _bool("RSBS_TEST_BOOL", default=False) is True

    def test_whitespace_around_value_handled(self, monkeypatch):
        from readsbstats.config import _bool
        monkeypatch.setenv("RSBS_TEST_BOOL", "  0  ")
        assert _bool("RSBS_TEST_BOOL", default=True) is False
        monkeypatch.setenv("RSBS_TEST_BOOL", "  1  ")
        assert _bool("RSBS_TEST_BOOL", default=False) is True


class TestParseFeeders:
    def test_empty_string_returns_defaults(self):
        from readsbstats.config import _parse_feeders
        result = _parse_feeders("")
        assert isinstance(result, list)
        assert len(result) > 0
        assert result[0]["name"] == "readsb"

    def test_valid_json_array(self):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "test", "unit": "test.service"}]'
        result = _parse_feeders(raw)
        assert len(result) == 1
        assert result[0]["name"] == "test"

    def test_invalid_json_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        result = _parse_feeders("{bad json")
        assert result[0]["name"] == "readsb"  # got defaults
        assert "RSBS_FEEDERS" in capsys.readouterr().err

    def test_non_array_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        result = _parse_feeders('{"name": "test"}')
        assert result[0]["name"] == "readsb"
        assert "JSON array" in capsys.readouterr().err

    def test_missing_name_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        result = _parse_feeders('[{"unit": "test.service"}]')
        assert result[0]["name"] == "readsb"
        assert "name" in capsys.readouterr().err

    def test_non_dict_item_falls_back(self, capsys):
        """Audit 2026-05-25: a JSON array containing a non-mapping value
        (here `null`) used to raise `TypeError: argument of type 'NoneType'
        is not iterable` from `"name" not in item` and crash config import.
        It now falls back to defaults instead."""
        from readsbstats.config import _parse_feeders
        result = _parse_feeders('[null]')
        assert result[0]["name"] == "readsb"
        err = capsys.readouterr().err
        assert "RSBS_FEEDERS" in err

    def test_string_list_item_falls_back(self, capsys):
        """A JSON string in the feeders array (`"string-feeder"`) used to
        pass the `"name" in item` substring check accidentally for some
        inputs. Type-checking each item rejects this regardless."""
        from readsbstats.config import _parse_feeders
        result = _parse_feeders('["string-feeder"]')
        assert result[0]["name"] == "readsb"
        assert "RSBS_FEEDERS" in capsys.readouterr().err

    def test_non_int_port_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "t", "unit": "t.service", "port": "80"}]'
        result = _parse_feeders(raw)
        assert result[0]["name"] == "readsb"
        assert "port" in capsys.readouterr().err

    def test_out_of_range_port_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "t", "unit": "t.service", "port": 70000}]'
        result = _parse_feeders(raw)
        assert result[0]["name"] == "readsb"
        assert "port" in capsys.readouterr().err

    def test_empty_name_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "", "unit": "t.service"}]'
        result = _parse_feeders(raw)
        assert result[0]["name"] == "readsb"
        assert "name" in capsys.readouterr().err

    def test_non_string_status_type_falls_back(self, capsys):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "t", "unit": "t.service", "status_type": 42}]'
        result = _parse_feeders(raw)
        assert result[0]["name"] == "readsb"
        assert "status_type" in capsys.readouterr().err

    def test_valid_port_accepted(self):
        from readsbstats.config import _parse_feeders
        raw = '[{"name": "t", "unit": "t.service", "port": 8080}]'
        result = _parse_feeders(raw)
        assert result[0]["port"] == 8080

    def test_feeders_list_capped(self, capsys):
        # BE-18: a huge RSBS_FEEDERS array would spawn one subprocess batch
        # per feeder per /api/feeders call. Cap the parsed list so an oversized
        # (or hostile) env value can't blow up the feeder-check fan-out.
        import json as _json
        from readsbstats.config import _parse_feeders, _MAX_FEEDERS
        raw = _json.dumps(
            [{"name": f"f{i}", "unit": f"f{i}.service"}
             for i in range(_MAX_FEEDERS + 25)]
        )
        result = _parse_feeders(raw)
        assert len(result) == _MAX_FEEDERS
        assert "truncat" in capsys.readouterr().err.lower()

    def test_invalid_entry_past_cap_does_not_lose_valid_feeders(self, capsys):
        # The cap must apply BEFORE validation: a malformed entry beyond
        # _MAX_FEEDERS sits in the discarded tail, so it must not trigger a
        # full fallback to defaults that loses the valid leading feeders.
        import json as _json
        from readsbstats.config import _parse_feeders, _MAX_FEEDERS
        good = [{"name": f"f{i}", "unit": f"f{i}.service"}
                for i in range(_MAX_FEEDERS)]
        raw = _json.dumps(good + [{"unit": "no-name.service"}])  # invalid #65
        result = _parse_feeders(raw)
        assert len(result) == _MAX_FEEDERS
        assert result[0]["name"] == "f0"
        assert "truncat" in capsys.readouterr().err.lower()


class TestMinOrDefaultInt:
    def test_value_above_minimum_unchanged(self):
        from readsbstats.config import _min_or_default_int
        assert _min_or_default_int("TEST", 10, 1, 5) == 10

    def test_value_at_minimum_unchanged(self):
        from readsbstats.config import _min_or_default_int
        assert _min_or_default_int("TEST", 1, 1, 5) == 1

    def test_zero_below_minimum_returns_default(self, capsys):
        from readsbstats.config import _min_or_default_int
        assert _min_or_default_int("TEST", 0, 1, 5) == 5
        err = capsys.readouterr().err
        assert "TEST" in err
        assert "0" in err

    def test_negative_returns_default(self, capsys):
        from readsbstats.config import _min_or_default_int
        assert _min_or_default_int("TEST", -3, 1, 5) == 5
        assert "TEST" in capsys.readouterr().err


class TestMinOrDefaultFloat:
    def test_value_above_minimum_unchanged(self):
        from readsbstats.config import _min_or_default_float
        assert _min_or_default_float("TEST", 3.14, 0.1, 1.0) == 3.14

    def test_value_at_minimum_unchanged(self):
        from readsbstats.config import _min_or_default_float
        assert _min_or_default_float("TEST", 0.1, 0.1, 1.0) == 0.1

    def test_zero_below_minimum_returns_default(self, capsys):
        from readsbstats.config import _min_or_default_float
        assert _min_or_default_float("TEST", 0.0, 0.1, 1.0) == 1.0
        err = capsys.readouterr().err
        assert "TEST" in err

    def test_negative_returns_default(self, capsys):
        from readsbstats.config import _min_or_default_float
        assert _min_or_default_float("TEST", -1.5, 0.1, 1.0) == 1.0
        assert "TEST" in capsys.readouterr().err


class TestConfigValidation:
    """Integration tests: reload module with bad env vars, verify clamping."""

    def test_poll_interval_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_POLL_INTERVAL", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.POLL_INTERVAL_SEC >= 1

    def test_mlat_outlier_factor_below_min_falls_back_to_5(self, monkeypatch):
        # Audit-13 A13-006: previously fell back to 20.0 (4× the
        # documented default of 5.0); clamp default now matches.
        monkeypatch.setenv("RSBS_MLAT_OUTLIER_FACTOR", "1.0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.MLAT_OUTLIER_FACTOR == 5.0

    def test_mlat_outlier_factor_above_min_unchanged(self, monkeypatch):
        monkeypatch.setenv("RSBS_MLAT_OUTLIER_FACTOR", "7.5")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.MLAT_OUTLIER_FACTOR == 7.5

    def test_flight_gap_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_FLIGHT_GAP", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.FLIGHT_GAP_SEC >= 1

    def test_max_page_size_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_MAX_PAGE_SIZE", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.MAX_PAGE_SIZE >= 1

    def test_page_size_clamped_to_max(self, monkeypatch):
        monkeypatch.setenv("RSBS_PAGE_SIZE", "9999")
        monkeypatch.setenv("RSBS_MAX_PAGE_SIZE", "50")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DEFAULT_PAGE_SIZE <= readsbstats.config.MAX_PAGE_SIZE

    def test_adsbx_poll_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_ADSBX_INTERVAL", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ADSBX_POLL_INTERVAL >= 1

    def test_max_speed_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_MAX_SPEED_KTS", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.MAX_SPEED_KTS >= 1

    def test_adsbx_range_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_ADSBX_RANGE", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ADSBX_RANGE_NM >= 1

    def test_route_batch_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_ROUTE_BATCH", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROUTE_BATCH_SIZE >= 1

    def test_route_rate_limit_negative_falls_back_to_default(self, monkeypatch):
        # STY-1: a negative rate limit is nonsensical; fall back to the 1.0s default.
        monkeypatch.setenv("RSBS_ROUTE_RATE_LIMIT", "-1")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROUTE_RATE_LIMIT_SEC == 1.0

    def test_route_rate_limit_zero_passes_through_disabled(self, monkeypatch):
        # STY-1: 0 means "no inter-call delay" (disabled) — min is 0.0, so it passes.
        monkeypatch.setenv("RSBS_ROUTE_RATE_LIMIT", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROUTE_RATE_LIMIT_SEC == 0.0

    def test_route_rate_limit_valid_unchanged(self, monkeypatch):
        # STY-1: no upper clamp — a valid positive value passes through untouched.
        monkeypatch.setenv("RSBS_ROUTE_RATE_LIMIT", "2.5")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROUTE_RATE_LIMIT_SEC == 2.5


class TestReceiverLocationValidation:
    """BUG-6: RECEIVER_LAT/RECEIVER_LON range-checked via cleaners.valid_lat/
    valid_lon. Out-of-range values fall back to the documented Warsaw defaults
    with a stderr warning; valid values pass through."""

    def test_lat_out_of_range_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_LAT", "100")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.RECEIVER_LAT == 52.24199
        assert "RSBS_LAT" in capsys.readouterr().err

    def test_lon_out_of_range_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_LON", "-200")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.RECEIVER_LON == 21.02872
        assert "RSBS_LON" in capsys.readouterr().err

    def test_valid_lat_passes_through(self, monkeypatch):
        monkeypatch.setenv("RSBS_LAT", "51.5")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.RECEIVER_LAT == 51.5


class TestStringNormalization:
    """Trailing slashes stripped, empty DB_PATH rejected."""

    def test_root_path_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_ROOT_PATH", "/stats/")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROOT_PATH == "/stats"

    def test_root_path_empty_stays_empty(self, monkeypatch):
        monkeypatch.setenv("RSBS_ROOT_PATH", "")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ROOT_PATH == ""

    def test_telegram_base_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_TELEGRAM_BASE_URL", "http://example.com/stats/")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TELEGRAM_BASE_URL == "http://example.com/stats"

    def test_adsbx_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_ADSBX_URL", "https://api.airplanes.live/v2/")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.ADSBX_API_URL == "https://api.airplanes.live/v2"

    def test_db_path_empty_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_DB_PATH", "")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_PATH != ""
        assert "RSBS_DB_PATH" in capsys.readouterr().err

    def test_db_path_whitespace_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_DB_PATH", "   ")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_PATH.strip() != ""
        assert "RSBS_DB_PATH" in capsys.readouterr().err


class TestTimeFormat:
    """RSBS_TIME_FORMAT — allow-list (24h | 12h), invalid falls back to 24h."""

    def test_defaults_to_24h(self, monkeypatch):
        monkeypatch.delenv("RSBS_TIME_FORMAT", raising=False)
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TIME_FORMAT == "24h"

    def test_accepts_12h(self, monkeypatch):
        monkeypatch.setenv("RSBS_TIME_FORMAT", "12h")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TIME_FORMAT == "12h"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("RSBS_TIME_FORMAT", "12H")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TIME_FORMAT == "12h"

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_TIME_FORMAT", "  12h  ")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TIME_FORMAT == "12h"

    def test_invalid_falls_back_to_24h(self, monkeypatch):
        monkeypatch.setenv("RSBS_TIME_FORMAT", "bogus")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.TIME_FORMAT == "24h"


class TestDbSynchronous:
    """RSBS_DB_SYNCHRONOUS — allow-list (FULL | NORMAL), invalid falls back to NORMAL."""

    def test_defaults_to_normal(self, monkeypatch):
        monkeypatch.delenv("RSBS_DB_SYNCHRONOUS", raising=False)
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "NORMAL"

    def test_accepts_full(self, monkeypatch):
        monkeypatch.setenv("RSBS_DB_SYNCHRONOUS", "FULL")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "FULL"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("RSBS_DB_SYNCHRONOUS", "full")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "FULL"

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_DB_SYNCHRONOUS", "  NORMAL  ")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "NORMAL"

    def test_invalid_falls_back_to_normal(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_DB_SYNCHRONOUS", "TURBO")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "NORMAL"
        assert "RSBS_DB_SYNCHRONOUS" in capsys.readouterr().err

    def test_blank_env_falls_back_silently(self, monkeypatch, capsys):
        """RSBS_DB_SYNCHRONOUS= (blank/whitespace) means unset — must default
        to NORMAL without emitting a stderr warning (blank ≈ unset model)."""
        monkeypatch.setenv("RSBS_DB_SYNCHRONOUS", "")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.DB_SYNCHRONOUS == "NORMAL"
        assert capsys.readouterr().err == ""


class TestGridFineRetentionDays:
    def test_below_floor_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("RSBS_GRID_FINE_RETENTION_DAYS", "3")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.GRID_FINE_RETENTION_DAYS == 14
        assert "RSBS_GRID_FINE_RETENTION_DAYS" in capsys.readouterr().err

    def test_at_floor_passes_through(self, monkeypatch):
        monkeypatch.setenv("RSBS_GRID_FINE_RETENTION_DAYS", "8")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.GRID_FINE_RETENTION_DAYS == 8

    def test_above_floor_passes_through(self, monkeypatch):
        monkeypatch.setenv("RSBS_GRID_FINE_RETENTION_DAYS", "30")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.GRID_FINE_RETENTION_DAYS == 30
