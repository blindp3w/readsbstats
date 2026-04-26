"""Tests for readsbstats.health — Phase 1 rule-based checks."""

import pytest

from readsbstats import config, database, health


# ---------------------------------------------------------------------------
# Shared in-memory DB fixture
# ---------------------------------------------------------------------------

def make_db():
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


@pytest.fixture()
def conn():
    c = make_db()
    yield c
    c.close()


def insert_metrics_row(conn, ts, **cols):
    """Insert a sparse receiver_stats row; missing columns are NULL."""
    keys = ["ts", *cols.keys()]
    placeholders = ", ".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO receiver_stats ({', '.join(keys)}) VALUES ({placeholders})",
        (ts, *cols.values()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_no_rows_warns(self, conn):
        check = health._check_heartbeat(conn, now=1_000_000)
        assert check.severity == "warn"
        assert check.name == "heartbeat"

    def test_fresh_row_ok(self, conn):
        insert_metrics_row(conn, ts=999_990)
        check = health._check_heartbeat(conn, now=1_000_000)
        assert check.severity == "ok"
        assert check.value == 10

    def test_stale_row_warns(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - config.HEALTH_HEARTBEAT_WARN_S - 1)
        check = health._check_heartbeat(conn, now=1_000_000)
        assert check.severity == "warn"

    def test_very_stale_critical(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - config.HEALTH_HEARTBEAT_CRIT_S - 1)
        check = health._check_heartbeat(conn, now=1_000_000)
        assert check.severity == "critical"


# ---------------------------------------------------------------------------
# Aircraft visibility
# ---------------------------------------------------------------------------

class TestAircraftVisibility:
    def test_no_rows_info(self, conn):
        check = health._check_aircraft_visibility(conn, now=1_000_000)
        assert check.severity == "info"

    def test_zero_aircraft_critical(self, conn):
        # Fill the window with rows that all show 0 aircraft.
        for offset in range(0, config.HEALTH_AIRCRAFT_GAP_S, 60):
            insert_metrics_row(conn, ts=1_000_000 - offset, ac_with_pos=0)
        check = health._check_aircraft_visibility(conn, now=1_000_000)
        assert check.severity == "critical"

    def test_some_aircraft_ok(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 30, ac_with_pos=12)
        check = health._check_aircraft_visibility(conn, now=1_000_000)
        assert check.severity == "ok"
        assert check.value == 12

    def test_old_aircraft_outside_window_critical(self, conn):
        # An old row showing aircraft + a recent row at zero must still be critical.
        insert_metrics_row(conn, ts=1_000_000 - config.HEALTH_AIRCRAFT_GAP_S - 100, ac_with_pos=20)
        insert_metrics_row(conn, ts=1_000_000 - 60, ac_with_pos=0)
        check = health._check_aircraft_visibility(conn, now=1_000_000)
        assert check.severity == "critical"


# ---------------------------------------------------------------------------
# Noise floor
# ---------------------------------------------------------------------------

class TestNoiseFloor:
    def test_no_rows_info(self, conn):
        check = health._check_noise_floor(conn, now=1_000_000)
        assert check.severity == "info"

    def test_quiet_floor_ok(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 60, noise=-32.0)
        check = health._check_noise_floor(conn, now=1_000_000)
        assert check.severity == "ok"

    def test_warn_threshold(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 60, noise=config.HEALTH_NOISE_WARN_DB + 0.5)
        check = health._check_noise_floor(conn, now=1_000_000)
        assert check.severity == "warn"

    def test_critical_threshold(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 60, noise=config.HEALTH_NOISE_CRIT_DB + 0.5)
        check = health._check_noise_floor(conn, now=1_000_000)
        assert check.severity == "critical"

    def test_average_smooths_spike(self, conn):
        # One spike + many quiet samples = quiet average.
        insert_metrics_row(conn, ts=1_000_000 - 60, noise=-10.0)
        for offset in range(120, 600, 60):
            insert_metrics_row(conn, ts=1_000_000 - offset, noise=-32.0)
        check = health._check_noise_floor(conn, now=1_000_000)
        assert check.severity == "ok"


# ---------------------------------------------------------------------------
# CPU saturation
# ---------------------------------------------------------------------------

class TestCpuSaturation:
    def test_no_rows_info(self, conn):
        check = health._check_cpu_saturation(conn, now=1_000_000)
        assert check.severity == "info"

    def test_idle_cpu_ok(self, conn):
        # 6000 ms / 60000 ms window = 10%
        insert_metrics_row(conn, ts=1_000_000 - 60, cpu_demod=6000.0)
        check = health._check_cpu_saturation(conn, now=1_000_000)
        assert check.severity == "ok"
        assert check.value == 10.0

    def test_warn_at_80_pct(self, conn):
        # 49000 ms / 60000 = 81.7% with default 60s METRICS_INTERVAL
        insert_metrics_row(conn, ts=1_000_000 - 60, cpu_demod=49000.0)
        check = health._check_cpu_saturation(conn, now=1_000_000)
        assert check.severity == "warn"

    def test_critical_at_90_pct(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 60, cpu_demod=55000.0)
        check = health._check_cpu_saturation(conn, now=1_000_000)
        assert check.severity == "critical"


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class TestComputeHealth:
    def test_empty_db_overall_warn(self, conn):
        report = health.compute_health(conn, now=1_000_000)
        # Heartbeat warns ("no rows yet"), others return info → overall = warn
        assert report.overall == "warn"
        names = [c.name for c in report.checks]
        assert set(names) == {
            "heartbeat", "aircraft_visibility", "noise_floor", "cpu_saturation",
            "message_rate", "signal_drop", "aircraft_drop",
            "gain_saturation", "range_degradation",
        }

    def test_healthy_db_overall_ok(self, conn):
        for offset in (0, 60, 120, 180, 240):
            insert_metrics_row(
                conn,
                ts=1_000_000 - offset,
                ac_with_pos=15,
                noise=-32.0,
                cpu_demod=3000.0,
            )
        report = health.compute_health(conn, now=1_000_000)
        assert report.overall == "ok"
        assert report.as_of == 1_000_000

    def test_overall_picks_worst(self, conn):
        # Fresh row, but noise floor is critical → overall = critical
        insert_metrics_row(
            conn,
            ts=1_000_000 - 30,
            ac_with_pos=15,
            noise=-10.0,
            cpu_demod=1000.0,
        )
        report = health.compute_health(conn, now=1_000_000)
        assert report.overall == "critical"

    def test_to_dict_serializable(self, conn):
        insert_metrics_row(conn, ts=1_000_000 - 30, ac_with_pos=15, noise=-32.0, cpu_demod=1000.0)
        report = health.compute_health(conn, now=1_000_000)
        d = report.to_dict()
        assert d["overall"] in ("ok", "info", "warn", "critical")
        assert d["as_of"] == 1_000_000
        assert isinstance(d["checks"], list)
        assert all("name" in c and "severity" in c for c in d["checks"])


# ---------------------------------------------------------------------------
# Phase 2 — baseline helper
# ---------------------------------------------------------------------------

# Pick a "now" anchored to a Wednesday at 14:30 UTC so the DOW+hour has a
# stable strftime answer regardless of the runner's locale.
import datetime as _dt
NOW = int(_dt.datetime(2026, 4, 22, 14, 30, tzinfo=_dt.timezone.utc).timestamp())

def _seed_hour_of_week_history(conn, value, weeks_back, hour_offsets=(0,), col="messages"):
    """Insert one row per (week, offset_in_hour) at the same DOW+hour as NOW."""
    for w in range(1, weeks_back + 1):
        base = NOW - w * 7 * 86400
        for off_s in hour_offsets:
            insert_metrics_row(conn, ts=base + off_s, **{col: value})


class TestBaselineAvg:
    def test_no_data(self, conn):
        avg, n = health._baseline_avg(conn, "messages", NOW, lookback_weeks=4)
        assert avg is None and n == 0

    def test_returns_average_of_matching_hour(self, conn):
        # 3 prior weeks at the same DOW+hour, values 100/200/300 → avg 200
        for week, value in zip((1, 2, 3), (100, 200, 300)):
            insert_metrics_row(conn, ts=NOW - week * 7 * 86400, messages=value)
        avg, n = health._baseline_avg(conn, "messages", NOW, lookback_weeks=4)
        assert n == 3
        assert avg == 200.0

    def test_excludes_current_hour(self, conn):
        # A recent row in the current hour must NOT be included
        insert_metrics_row(conn, ts=NOW - 60, messages=9999)
        insert_metrics_row(conn, ts=NOW - 7 * 86400, messages=100)
        avg, n = health._baseline_avg(conn, "messages", NOW, lookback_weeks=4)
        assert n == 1
        assert avg == 100.0

    def test_excludes_other_hours_and_dows(self, conn):
        # Same hour, wrong DOW
        insert_metrics_row(conn, ts=NOW - 86400, messages=999)
        # Wrong hour, same DOW
        insert_metrics_row(conn, ts=NOW - 7 * 86400 - 3600 * 5, messages=888)
        # Correct match
        insert_metrics_row(conn, ts=NOW - 7 * 86400, messages=200)
        avg, n = health._baseline_avg(conn, "messages", NOW, lookback_weeks=4)
        assert n == 1
        assert avg == 200.0

    def test_lookback_window_limits_history(self, conn):
        # 5 weeks back is outside the 4-week window
        insert_metrics_row(conn, ts=NOW - 5 * 7 * 86400, messages=999)
        insert_metrics_row(conn, ts=NOW - 2 * 7 * 86400, messages=200)
        avg, n = health._baseline_avg(conn, "messages", NOW, lookback_weeks=4)
        assert n == 1
        assert avg == 200.0


# ---------------------------------------------------------------------------
# Phase 2 — message_rate
# ---------------------------------------------------------------------------

class TestMessageRateCheck:
    def test_no_recent_data_info(self, conn):
        check = health._check_message_rate(conn, NOW)
        assert check.severity == "info"

    def test_insufficient_baseline_info(self, conn):
        insert_metrics_row(conn, ts=NOW - 60, messages=1000)
        check = health._check_message_rate(conn, NOW)
        assert check.severity == "info"
        assert "warming up" in check.message.lower()

    def test_normal_ok(self, conn):
        # Healthy current rate matching baseline
        for off in (0, 60, 120, 180):
            insert_metrics_row(conn, ts=NOW - off, messages=1000)
        _seed_hour_of_week_history(conn, 1000, weeks_back=3)
        check = health._check_message_rate(conn, NOW)
        assert check.severity == "ok"

    def test_drop_below_threshold_warns(self, conn):
        # Recent 200/min vs 1000/min baseline = 20% — below default 50% threshold
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, messages=200)
        _seed_hour_of_week_history(conn, 1000, weeks_back=3)
        check = health._check_message_rate(conn, NOW)
        assert check.severity == "warn"
        assert "20" in check.message  # percentage shows up


# ---------------------------------------------------------------------------
# Phase 2 — signal_drop
# ---------------------------------------------------------------------------

class TestSignalDropCheck:
    def test_no_recent_data_info(self, conn):
        check = health._check_signal_drop(conn, NOW)
        assert check.severity == "info"

    def test_insufficient_baseline_info(self, conn):
        insert_metrics_row(conn, ts=NOW - 60, signal=-20.0)
        check = health._check_signal_drop(conn, NOW)
        assert check.severity == "info"

    def test_normal_ok(self, conn):
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, signal=-20.0)
        _seed_hour_of_week_history(conn, -20.0, weeks_back=3, col="signal")
        check = health._check_signal_drop(conn, NOW)
        assert check.severity == "ok"

    def test_signal_dropped_warns(self, conn):
        # Recent -28 dBFS vs -20 baseline = 8 dB drop, exceeds default 3 dB threshold
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, signal=-28.0)
        _seed_hour_of_week_history(conn, -20.0, weeks_back=3, col="signal")
        check = health._check_signal_drop(conn, NOW)
        assert check.severity == "warn"


# ---------------------------------------------------------------------------
# Phase 2 — aircraft_drop
# ---------------------------------------------------------------------------

class TestAircraftDropCheck:
    def test_no_recent_data_info(self, conn):
        check = health._check_aircraft_drop(conn, NOW)
        assert check.severity == "info"

    def test_insufficient_baseline_info(self, conn):
        insert_metrics_row(conn, ts=NOW - 60, ac_with_pos=10)
        check = health._check_aircraft_drop(conn, NOW)
        assert check.severity == "info"

    def test_quiet_hour_ok(self, conn):
        # Baseline < 1 (overnight) — relative drops are noise; treat as OK
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, ac_with_pos=0)
        _seed_hour_of_week_history(conn, 0.5, weeks_back=3, col="ac_with_pos")
        check = health._check_aircraft_drop(conn, NOW)
        assert check.severity == "ok"
        assert "quiet hour" in check.message.lower()

    def test_normal_ok(self, conn):
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, ac_with_pos=15)
        _seed_hour_of_week_history(conn, 15, weeks_back=3, col="ac_with_pos")
        check = health._check_aircraft_drop(conn, NOW)
        assert check.severity == "ok"

    def test_severe_drop_warns(self, conn):
        # Recent 2 vs baseline 20 = 10% — below default 25% threshold
        for off in (0, 60, 120):
            insert_metrics_row(conn, ts=NOW - off, ac_with_pos=2)
        _seed_hour_of_week_history(conn, 20, weeks_back=3, col="ac_with_pos")
        check = health._check_aircraft_drop(conn, NOW)
        assert check.severity == "warn"


# ---------------------------------------------------------------------------
# Phase 3 — gain saturation
# ---------------------------------------------------------------------------

class TestGainSaturationCheck:
    def test_no_data_info(self, conn):
        check = health._check_gain_saturation(conn, NOW)
        assert check.severity == "info"

    def test_zero_messages_info(self, conn):
        insert_metrics_row(conn, ts=NOW - 60, messages=0, strong_signals=0)
        check = health._check_gain_saturation(conn, NOW)
        assert check.severity == "info"

    def test_low_strong_ratio_ok(self, conn):
        # 50 strong / 10000 messages = 0.5%, well below default 5% threshold
        insert_metrics_row(conn, ts=NOW - 60, messages=10000, strong_signals=50)
        check = health._check_gain_saturation(conn, NOW)
        assert check.severity == "ok"
        assert check.value == 0.5

    def test_high_strong_ratio_warns(self, conn):
        # 700 strong / 10000 messages = 7.0%, above 5% threshold
        insert_metrics_row(conn, ts=NOW - 60, messages=10000, strong_signals=700)
        check = health._check_gain_saturation(conn, NOW)
        assert check.severity == "warn"
        assert "lowering" in check.message.lower()

    def test_null_strong_signals_treated_as_zero(self, conn):
        # strong_signals NULL but messages present — should not crash
        insert_metrics_row(conn, ts=NOW - 60, messages=5000)
        check = health._check_gain_saturation(conn, NOW)
        assert check.severity == "ok"
        assert check.value == 0.0


# ---------------------------------------------------------------------------
# Phase 3 — range degradation
# ---------------------------------------------------------------------------

class TestRangeDegradationCheck:
    def test_no_data_info(self, conn):
        check = health._check_range_degradation(conn, NOW)
        assert check.severity == "info"

    def test_insufficient_history_info(self, conn):
        # Only 5 days of data — short window is 7d, need ≥ 14d total
        for day in range(5):
            insert_metrics_row(conn, ts=NOW - day * 86400, max_distance_m=200_000)
        check = health._check_range_degradation(conn, NOW)
        assert check.severity == "info"
        assert "collecting" in check.message.lower()

    def test_normal_range_ok(self, conn):
        # 20 days of data, max range stable at 250 km in both windows
        for day in range(20):
            insert_metrics_row(conn, ts=NOW - day * 86400, max_distance_m=250_000)
        check = health._check_range_degradation(conn, NOW)
        assert check.severity == "ok"

    def test_recent_drop_info(self, conn):
        # Older data shows 400 km peak; recent 7 days max 300 km = 75% < 85% threshold.
        # Day 7 is omitted because the short window includes ts >= NOW - 7d (inclusive),
        # which would otherwise pull the 400 km peak into the short window.
        for day in range(8, 26):
            insert_metrics_row(conn, ts=NOW - day * 86400, max_distance_m=400_000)
        for day in range(7):
            insert_metrics_row(conn, ts=NOW - day * 86400, max_distance_m=300_000)
        check = health._check_range_degradation(conn, NOW)
        assert check.severity == "info"
        assert "antenna" in check.message.lower() or "connector" in check.message.lower()
