"""Tests for readsbstats.config — error paths and fallback behaviour."""

import importlib
import os


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


class TestClampInt:
    def test_value_above_minimum_unchanged(self):
        from readsbstats.config import _clamp_int
        assert _clamp_int("TEST", 10, 1, 5) == 10

    def test_value_at_minimum_unchanged(self):
        from readsbstats.config import _clamp_int
        assert _clamp_int("TEST", 1, 1, 5) == 1

    def test_zero_below_minimum_returns_default(self, capsys):
        from readsbstats.config import _clamp_int
        assert _clamp_int("TEST", 0, 1, 5) == 5
        err = capsys.readouterr().err
        assert "TEST" in err
        assert "0" in err

    def test_negative_returns_default(self, capsys):
        from readsbstats.config import _clamp_int
        assert _clamp_int("TEST", -3, 1, 5) == 5
        assert "TEST" in capsys.readouterr().err


class TestClampFloat:
    def test_value_above_minimum_unchanged(self):
        from readsbstats.config import _clamp_float
        assert _clamp_float("TEST", 3.14, 0.1, 1.0) == 3.14

    def test_value_at_minimum_unchanged(self):
        from readsbstats.config import _clamp_float
        assert _clamp_float("TEST", 0.1, 0.1, 1.0) == 0.1

    def test_zero_below_minimum_returns_default(self, capsys):
        from readsbstats.config import _clamp_float
        assert _clamp_float("TEST", 0.0, 0.1, 1.0) == 1.0
        err = capsys.readouterr().err
        assert "TEST" in err

    def test_negative_returns_default(self, capsys):
        from readsbstats.config import _clamp_float
        assert _clamp_float("TEST", -1.5, 0.1, 1.0) == 1.0
        assert "TEST" in capsys.readouterr().err


class TestConfigValidation:
    """Integration tests: reload module with bad env vars, verify clamping."""

    def test_poll_interval_zero_clamped(self, monkeypatch):
        monkeypatch.setenv("RSBS_POLL_INTERVAL", "0")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.POLL_INTERVAL_SEC >= 1

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

    def test_base_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("RSBS_BASE_URL", "http://example.com/stats/")
        import readsbstats.config
        importlib.reload(readsbstats.config)
        assert readsbstats.config.BASE_URL == "http://example.com/stats"

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
