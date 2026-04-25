"""
Tests for metrics_collector.py — receiver stats time-series collector.
Also includes API endpoint tests via FastAPI TestClient.
"""

import importlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from readsbstats import config, database, enrichment, web


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> sqlite3.Connection:
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


# Full sample mirroring the user's real stats.json structure
SAMPLE_STATS = {
    "now": 1777022965.0,
    "gain_db": 43.9,
    "estimated_ppm": 1.3,
    "aircraft_with_pos": 15,
    "aircraft_without_pos": 2,
    "aircraft_count_by_type": {
        "adsb_icao": 14, "adsb_icao_nt": 0, "adsr_icao": 0,
        "tisb_icao": 0, "adsc": 0, "mlat": 2, "other": 0,
        "mode_s": 1, "adsb_other": 0, "adsr_other": 0,
        "tisb_trackfile": 0, "tisb_other": 0, "mode_ac": 0, "unknown": 0,
    },
    "last1min": {
        "start": 1777022905.0,
        "end": 1777022965.0,
        "local": {
            "samples_processed": 143982592,
            "samples_dropped": 0,
            "samples_lost": 0,
            "modeac": 0,
            "modes": 1338971,
            "bad": 923610,
            "unknown_icao": 409632,
            "accepted": [5391, 338],
            "signal": -15.4,
            "noise": -35.5,
            "peak_signal": -2.9,
            "strong_signals": 2,
        },
        "messages": 5960,
        "position_count_total": 650,
        "position_count_by_type": {
            "adsb_icao": 606, "mlat": 44, "other": 0, "mode_s": 0,
        },
        "remote": {
            "modeac": 0, "modes": 231, "basestation": 0,
            "bad": 0, "unknown_icao": 0, "accepted": [231, 0],
            "bytes_in": 5318, "bytes_out": 788088,
        },
        "cpr": {
            "surface": 0, "airborne": 615,
            "global_ok": 599, "global_bad": 0, "global_range": 0,
            "global_speed": 0, "global_skipped": 0,
            "local_ok": 7, "local_aircraft_relative": 7,
            "local_receiver_relative": 0, "local_skipped": 9,
            "local_range": 0, "local_speed": 0, "filtered": 0,
        },
        "altitude_suppressed": 0,
        "cpu": {
            "demod": 4602, "reader": 1676, "background": 1580,
            "aircraft_json": 48, "globe_json": 0, "binCraft": 0,
            "trace_json": 0, "heatmap_and_state": 64,
            "api_workers": 0, "api_update": 38, "remove_stale": 37,
        },
        "tracks": {"all": 0, "single_message": 2},
        "max_distance": 186770,
    },
}


# ---------------------------------------------------------------------------
# _parse_stats
# ---------------------------------------------------------------------------

class TestParseStats:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import metrics_collector
        importlib.reload(metrics_collector)
        self.mc = metrics_collector
        yield

    def test_full_stats_data(self):
        ts, row = self.mc._parse_stats(SAMPLE_STATS)
        assert ts == 1777022965
        # Spot-check key fields
        assert row["ac_with_pos"] == 15
        assert row["ac_without_pos"] == 2
        assert row["ac_adsb"] == 14
        assert row["ac_mlat"] == 2
        assert row["signal"] == -15.4
        assert row["noise"] == -35.5
        assert row["peak_signal"] == -2.9
        assert row["strong_signals"] == 2
        assert row["local_modes"] == 1338971
        assert row["local_bad"] == 923610
        assert row["local_unknown_icao"] == 409632
        assert row["local_accepted_0"] == 5391
        assert row["local_accepted_1"] == 338
        assert row["samples_dropped"] == 0
        assert row["samples_lost"] == 0
        assert row["messages"] == 5960
        assert row["positions_total"] == 650
        assert row["positions_adsb"] == 606
        assert row["positions_mlat"] == 44
        assert row["max_distance_m"] == 186770
        assert row["tracks_new"] == 0
        assert row["tracks_single"] == 2
        assert row["cpu_demod"] == 4602
        assert row["cpu_reader"] == 1676
        assert row["cpu_background"] == 1580
        assert row["cpu_aircraft_json"] == 48
        assert row["cpu_heatmap"] == 64
        assert row["remote_modes"] == 231
        assert row["remote_bad"] == 0
        assert row["remote_accepted"] == 231
        assert row["remote_bytes_in"] == 5318
        assert row["remote_bytes_out"] == 788088
        assert row["cpr_airborne"] == 615
        assert row["cpr_global_ok"] == 599
        assert row["cpr_global_bad"] == 0
        assert row["cpr_global_range"] == 0
        assert row["cpr_global_speed"] == 0
        assert row["cpr_global_skipped"] == 0
        assert row["cpr_local_ok"] == 7
        assert row["cpr_local_range"] == 0
        assert row["cpr_local_speed"] == 0
        assert row["cpr_filtered"] == 0
        assert row["altitude_suppressed"] == 0

    def test_all_43_columns_present(self):
        ts, row = self.mc._parse_stats(SAMPLE_STATS)
        assert ts is not None
        for col in self.mc._COLS:
            assert col in row, f"missing column: {col}"

    def test_missing_last1min_returns_none(self):
        ts, row = self.mc._parse_stats({"now": 1.0})
        assert ts is None
        assert row is None

    def test_last1min_not_a_dict_returns_none(self):
        ts, row = self.mc._parse_stats({"last1min": "garbage"})
        assert ts is None
        assert row is None

    def test_missing_end_returns_none(self):
        ts, row = self.mc._parse_stats({"last1min": {"start": 1.0}})
        assert ts is None
        assert row is None

    def test_missing_local_section(self):
        data = {"last1min": {"end": 100.0, "messages": 42}}
        ts, row = self.mc._parse_stats(data)
        assert ts == 100
        assert row["signal"] is None
        assert row["noise"] is None
        assert row["local_modes"] is None
        assert row["messages"] == 42

    def test_missing_remote_section(self):
        data = {"last1min": {"end": 100.0}}
        ts, row = self.mc._parse_stats(data)
        assert row["remote_modes"] is None
        assert row["remote_bytes_in"] is None

    def test_missing_cpu_section(self):
        data = {"last1min": {"end": 100.0}}
        ts, row = self.mc._parse_stats(data)
        assert row["cpu_demod"] is None
        assert row["cpu_reader"] is None

    def test_missing_cpr_section(self):
        data = {"last1min": {"end": 100.0}}
        ts, row = self.mc._parse_stats(data)
        assert row["cpr_global_ok"] is None
        assert row["cpr_airborne"] is None

    def test_accepted_short_array(self):
        data = {
            "last1min": {
                "end": 100.0,
                "local": {"accepted": [42]},
            }
        }
        ts, row = self.mc._parse_stats(data)
        assert row["local_accepted_0"] == 42
        assert row["local_accepted_1"] is None

    def test_accepted_empty_array(self):
        data = {
            "last1min": {
                "end": 100.0,
                "local": {"accepted": []},
            }
        }
        ts, row = self.mc._parse_stats(data)
        assert row["local_accepted_0"] is None
        assert row["local_accepted_1"] is None

    def test_top_level_aircraft_counts(self):
        data = {
            "aircraft_with_pos": 20,
            "aircraft_without_pos": 5,
            "aircraft_count_by_type": {"adsb_icao": 18, "mlat": 3},
            "last1min": {"end": 100.0},
        }
        ts, row = self.mc._parse_stats(data)
        assert row["ac_with_pos"] == 20
        assert row["ac_without_pos"] == 5
        assert row["ac_adsb"] == 18
        assert row["ac_mlat"] == 3

    def test_missing_aircraft_count_by_type(self):
        data = {
            "aircraft_with_pos": 10,
            "last1min": {"end": 100.0},
        }
        ts, row = self.mc._parse_stats(data)
        assert row["ac_with_pos"] == 10
        assert row["ac_adsb"] is None
        assert row["ac_mlat"] is None


# ---------------------------------------------------------------------------
# _insert_row
# ---------------------------------------------------------------------------

class TestInsertRow:
    @pytest.fixture(autouse=True)
    def setup(self):
        from readsbstats import metrics_collector
        importlib.reload(metrics_collector)
        self.mc = metrics_collector
        self.conn = make_db()
        yield
        self.conn.close()

    def test_stores_all_columns(self):
        _, row = self.mc._parse_stats(SAMPLE_STATS)
        self.mc._insert_row(self.conn, 1777022965, row)
        r = self.conn.execute(
            "SELECT * FROM receiver_stats WHERE ts = 1777022965"
        ).fetchone()
        assert r is not None
        assert r["signal"] == -15.4
        assert r["ac_with_pos"] == 15
        assert r["messages"] == 5960
        assert r["cpu_demod"] == 4602

    def test_duplicate_ts_ignored(self):
        _, row = self.mc._parse_stats(SAMPLE_STATS)
        self.mc._insert_row(self.conn, 1000, row)
        # Second insert with same ts — no error, row unchanged
        row2 = dict(row)
        row2["signal"] = -99.0
        self.mc._insert_row(self.conn, 1000, row2)
        r = self.conn.execute(
            "SELECT signal FROM receiver_stats WHERE ts = 1000"
        ).fetchone()
        assert r["signal"] == -15.4  # original value kept

    def test_null_values_stored(self):
        row = {c: None for c in self.mc._COLS}
        self.mc._insert_row(self.conn, 2000, row)
        r = self.conn.execute(
            "SELECT * FROM receiver_stats WHERE ts = 2000"
        ).fetchone()
        assert r is not None
        assert r["signal"] is None
        assert r["messages"] is None


# ---------------------------------------------------------------------------
# _poll_stats
# ---------------------------------------------------------------------------

class TestPollStats:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        from readsbstats import metrics_collector
        importlib.reload(metrics_collector)
        self.mc = metrics_collector
        self.conn = make_db()
        self.tmp = tmp_path
        yield
        self.conn.close()

    def test_successful_poll(self):
        path = self.tmp / "stats.json"
        path.write_text(json.dumps(SAMPLE_STATS))
        result = self.mc._poll_stats(self.conn, str(path))
        assert result is True
        row = self.conn.execute(
            "SELECT * FROM receiver_stats WHERE ts = 1777022965"
        ).fetchone()
        assert row is not None
        assert row["signal"] == -15.4

    def test_missing_file_raises_transient(self):
        with pytest.raises(self.mc._TransientError):
            self.mc._poll_stats(self.conn, str(self.tmp / "nonexistent.json"))

    def test_corrupt_json_raises_transient(self):
        path = self.tmp / "bad.json"
        path.write_text("{invalid json")
        with pytest.raises(self.mc._TransientError):
            self.mc._poll_stats(self.conn, str(path))

    def test_missing_last1min_returns_false(self):
        path = self.tmp / "stats.json"
        path.write_text(json.dumps({"now": 1.0}))
        result = self.mc._poll_stats(self.conn, str(path))
        assert result is False

    def test_duplicate_ts_is_idempotent(self):
        path = self.tmp / "stats.json"
        path.write_text(json.dumps(SAMPLE_STATS))
        self.mc._poll_stats(self.conn, str(path))
        self.mc._poll_stats(self.conn, str(path))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM receiver_stats"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# start_metrics_collector
# ---------------------------------------------------------------------------

class TestStartMetricsCollector:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        from readsbstats import metrics_collector
        importlib.reload(metrics_collector)
        self.mc = metrics_collector
        self.monkeypatch = monkeypatch
        yield

    def test_disabled_returns_none(self):
        self.monkeypatch.setattr(config, "METRICS_ENABLED", False)
        result = self.mc.start_metrics_collector()
        assert result is None

    def test_enabled_starts_thread(self):
        self.monkeypatch.setattr(config, "METRICS_ENABLED", True)
        # Mock the loop to exit immediately
        self.monkeypatch.setattr(self.mc, "run_metrics_loop", lambda db_path: None)
        t = self.mc.start_metrics_collector()
        assert t is not None
        assert t.daemon is True
        assert t.name == "metrics-collector"
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# API — /api/metrics
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_conn():
    conn = make_db()
    enrichment.clear_cache()
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    from readsbstats import route_enricher
    monkeypatch.setattr(web, "_db", db_conn)
    monkeypatch.setattr(route_enricher, "start_background_enricher", lambda: None)
    web._cache.clear()
    with TestClient(web.app, raise_server_exceptions=True) as c:
        yield c


def _insert_metric_row(conn, ts, signal=-15.0, noise=-35.0, messages=5000,
                       max_distance_m=200000, ac_with_pos=10, **kwargs):
    """Insert a minimal metrics row for testing."""
    from readsbstats.metrics_collector import _COLS, _INSERT_SQL
    row = {c: None for c in _COLS}
    row.update(signal=signal, noise=noise, messages=messages,
               max_distance_m=max_distance_m, ac_with_pos=ac_with_pos)
    row.update(kwargs)
    values = tuple(row.get(c) for c in _COLS)
    conn.execute(_INSERT_SQL, (ts, *values))
    conn.commit()


class TestMetricsPage:
    def test_metrics_page_returns_200(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "Receiver Metrics" in resp.text

    def test_metrics_page_no_data_message(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "RSBS_METRICS_ENABLED" in resp.text  # hint shown when empty

    def test_metrics_page_hides_message_with_data(self, client, db_conn):
        _insert_metric_row(db_conn, int(time.time()), signal=-15.0)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "RSBS_METRICS_ENABLED" not in resp.text


class TestMetricsApi:
    def test_empty_table(self, client):
        resp = client.get("/api/metrics?metrics=signal,noise")
        assert resp.status_code == 200
        data = resp.json()
        assert data["metrics"] == ["signal", "noise"]
        assert data["data"] == [[], [], []]

    def test_returns_data(self, client, db_conn):
        now = int(time.time())
        _insert_metric_row(db_conn, now - 60, signal=-12.0, noise=-33.0)
        _insert_metric_row(db_conn, now, signal=-15.0, noise=-35.0)
        resp = client.get(f"/api/metrics?from={now - 120}&to={now + 1}&metrics=signal,noise")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 3  # [timestamps, signal, noise]
        assert len(data["data"][0]) == 2
        assert data["data"][1][0] == -12.0
        assert data["data"][2][1] == -35.0

    def test_invalid_column_400(self, client):
        resp = client.get("/api/metrics?metrics=signal,evil_sql")
        assert resp.status_code == 400
        assert "evil_sql" in resp.json()["error"]

    def test_empty_metrics_returns_empty(self, client):
        resp = client.get("/api/metrics?metrics=")
        assert resp.status_code == 200
        data = resp.json()
        assert data["metrics"] == []
        assert data["data"] == []

    def test_default_time_range(self, client, db_conn):
        """Without from/to params, defaults to last 24h."""
        now = int(time.time())
        _insert_metric_row(db_conn, now - 3600, signal=-10.0)    # 1h ago — in range
        _insert_metric_row(db_conn, now - 100000, signal=-20.0)  # ~28h ago — out of range
        resp = client.get("/api/metrics?metrics=signal")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"][0]) == 1
        assert data["data"][1][0] == -10.0

    def test_bucket_seconds_raw_for_short_range(self, client, db_conn):
        """Range <= 24h should return raw data (bucket_seconds=0)."""
        now = int(time.time())
        _insert_metric_row(db_conn, now, signal=-15.0)
        resp = client.get(f"/api/metrics?from={now - 3600}&to={now + 1}&metrics=signal")
        data = resp.json()
        assert data["bucket_seconds"] == 0

    def test_bucket_seconds_5min_for_7d(self, client, db_conn):
        """Range within 7 days should use 5-min buckets."""
        now = int(time.time())
        _insert_metric_row(db_conn, now, signal=-15.0)
        resp = client.get(f"/api/metrics?from={now - 604000}&to={now + 1}&metrics=signal")
        data = resp.json()
        assert data["bucket_seconds"] == 300

    def test_aggregation_sum_for_messages(self, client, db_conn):
        """Messages are SUM-aggregated in buckets."""
        base = 1_000_000
        _insert_metric_row(db_conn, base, messages=100)
        _insert_metric_row(db_conn, base + 60, messages=200)
        # Range within 7 days to trigger 5-min bucketing
        resp = client.get(f"/api/metrics?from={base - 1}&to={base + 86400}&metrics=messages")
        data = resp.json()
        assert data["bucket_seconds"] == 300
        # Both rows fall in the same 5-min bucket → SUM = 300
        assert data["data"][1][0] == 300

    def test_aggregation_avg_for_signal(self, client, db_conn):
        """Signal is AVG-aggregated in buckets."""
        base = 1_000_000
        _insert_metric_row(db_conn, base, signal=-10.0)
        _insert_metric_row(db_conn, base + 60, signal=-20.0)
        resp = client.get(f"/api/metrics?from={base - 1}&to={base + 604800}&metrics=signal")
        data = resp.json()
        # Both in same bucket → AVG = -15.0
        assert data["data"][1][0] == pytest.approx(-15.0)

    def test_aggregation_max_for_distance(self, client, db_conn):
        """max_distance_m is MAX-aggregated in buckets."""
        base = 1_000_000
        _insert_metric_row(db_conn, base, max_distance_m=100000)
        _insert_metric_row(db_conn, base + 60, max_distance_m=200000)
        resp = client.get(f"/api/metrics?from={base - 1}&to={base + 604800}&metrics=max_distance_m")
        data = resp.json()
        assert data["data"][1][0] == 200000

    def test_response_has_no_enabled_field(self, client):
        """API response doesn't leak collector config to the web layer."""
        resp = client.get("/api/metrics?metrics=signal")
        assert "enabled" not in resp.json()


# ---------------------------------------------------------------------------
# Schema — receiver_stats table exists
# ---------------------------------------------------------------------------

class TestReceiverStatsSchema:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.conn = make_db()
        yield
        self.conn.close()

    def test_table_exists(self):
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "receiver_stats" in tables

    def test_ts_is_primary_key(self):
        cols = self.conn.execute("PRAGMA table_info(receiver_stats)").fetchall()
        ts_col = [c for c in cols if c[1] == "ts"][0]
        assert ts_col[5] == 1  # pk flag

    def test_column_count(self):
        cols = self.conn.execute("PRAGMA table_info(receiver_stats)").fetchall()
        assert len(cols) == 44  # ts + 43 metrics
