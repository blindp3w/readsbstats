"""Tests for readsbstats.mcp_server.timeparse (host-local time, snapped windows)."""

import os
import time
from datetime import date, datetime

import pytest

from readsbstats.mcp_server.timeparse import (
    day_bounds, parse_instant, resolve_period, to_local_iso,
)


@pytest.fixture(autouse=True)
def setup():
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Warsaw"
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def local(y, mo, d, h=0, mi=0, s=0):
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))


class TestDayBounds:
    def test_normal_day(self):
        a, b = day_bounds(date(2026, 1, 15))
        assert a == local(2026, 1, 15)
        assert b - a == 86400

    def test_spring_forward_is_23h(self):
        a, b = day_bounds(date(2026, 3, 29))
        assert b - a == 23 * 3600
        assert a == local(2026, 3, 29) and b == local(2026, 3, 30)

    def test_fall_back_is_25h(self):
        a, b = day_bounds(date(2026, 10, 25))
        assert b - a == 25 * 3600
        assert a == local(2026, 10, 25) and b == local(2026, 10, 26)

    def test_returns_ints(self):
        assert all(type(x) is int for x in day_bounds(date(2026, 6, 1)))


class TestParseInstant:
    NOW = local(2026, 6, 10, 12, 0, 0)

    def test_now(self):
        assert parse_instant("now", now=self.NOW + 0.7) == self.NOW
        assert parse_instant(" NOW ", now=self.NOW) == self.NOW

    def test_now_default_clock(self):
        assert abs(parse_instant("now") - time.time()) < 3

    def test_date_is_local_midnight(self):
        assert parse_instant("2026-06-10") == local(2026, 6, 10)

    def test_naive_datetime_is_local(self):
        assert parse_instant("2026-06-10T13:30:00") == local(2026, 6, 10, 13, 30)
        assert parse_instant("2026-06-10 13:30") == local(2026, 6, 10, 13, 30)

    def test_offset_honoured(self):
        assert parse_instant("2026-06-10T12:00:00+00:00") == local(2026, 6, 10, 14, 0)
        assert parse_instant("2026-06-10T12:00:00Z") == local(2026, 6, 10, 14, 0)
        assert parse_instant("2026-06-10T12:00:00-05:00") == local(2026, 6, 10, 19, 0)

    @pytest.mark.parametrize("s", ["", "yesterday", "2026-13-01", "10:00", "garbage", "2026-06-10T25:00"])
    def test_invalid(self, s):
        with pytest.raises(ValueError):
            parse_instant(s)


class TestResolvePeriod:
    def test_default_is_24h_snapped(self):
        now = local(2026, 6, 10, 12, 7, 31)
        frm, to = resolve_period(None, now=now)
        assert to == local(2026, 6, 10, 12, 0, 0)
        assert to - frm == 86400

    def test_snapping_same_slot_identical(self):
        a = resolve_period("24h", now=local(2026, 6, 10, 12, 0, 1))
        b = resolve_period("24h", now=local(2026, 6, 10, 12, 9, 59))
        assert a == b
        c = resolve_period("24h", now=local(2026, 6, 10, 12, 10, 0))
        assert c != a and c[1] - a[1] == 600

    def test_long_span_floors_to_hour(self):
        now = local(2026, 6, 10, 12, 47, 0)
        frm, to = resolve_period("30d", now=now)
        assert to == local(2026, 6, 10, 12, 0, 0)
        assert to - frm == 30 * 86400
        a = resolve_period("30d", now=local(2026, 6, 10, 12, 1, 0))
        b = resolve_period("30d", now=local(2026, 6, 10, 12, 59, 0))
        assert a == b

    def test_exactly_7d_uses_ten_minutes(self):
        frm, to = resolve_period("7d", now=local(2026, 6, 10, 12, 47, 0))
        assert to == local(2026, 6, 10, 12, 40, 0)
        frm, to = resolve_period("168h", now=local(2026, 6, 10, 12, 47, 0))
        assert to == local(2026, 6, 10, 12, 40, 0)
        frm, to = resolve_period("169h", now=local(2026, 6, 10, 12, 47, 0))
        assert to == local(2026, 6, 10, 12, 0, 0)

    def test_hours_and_days_bounds(self):
        assert resolve_period("1h", now=local(2026, 6, 10, 12, 0, 0))[1] - \
            resolve_period("1h", now=local(2026, 6, 10, 12, 0, 0))[0] == 3600
        resolve_period("720h")
        resolve_period("3650d")
        for bad in ("721h", "3651d", "0h", "0d"):
            with pytest.raises(ValueError):
                resolve_period(bad)

    def test_today(self):
        now = local(2026, 6, 10, 12, 7, 0)
        assert resolve_period("today", now=now) == day_bounds(date(2026, 6, 10))

    def test_yesterday_just_after_midnight(self):
        now = local(2026, 6, 10, 0, 30, 0)
        assert resolve_period("yesterday", now=now) == day_bounds(date(2026, 6, 9))
        assert resolve_period("today", now=now) == day_bounds(date(2026, 6, 10))

    def test_yesterday_across_dst(self):
        now = local(2026, 3, 30, 0, 30, 0)
        frm, to = resolve_period("yesterday", now=now)
        assert to - frm == 23 * 3600 and to == local(2026, 3, 30)

    def test_single_day(self):
        assert resolve_period("2026-03-29", now=local(2026, 6, 1)) == day_bounds(date(2026, 3, 29))

    def test_day_range_inclusive(self):
        frm, to = resolve_period("2026-03-28..2026-03-30", now=local(2026, 6, 1))
        assert frm == local(2026, 3, 28) and to == local(2026, 3, 31)
        assert to - frm == (24 + 23 + 24) * 3600

    def test_day_range_reversed_invalid(self):
        with pytest.raises(ValueError):
            resolve_period("2026-03-30..2026-03-28", now=local(2026, 6, 1))

    def test_range_across_fall_back(self):
        frm, to = resolve_period("2026-10-24..2026-10-25", now=local(2027, 1, 1))
        assert to - frm == (24 + 25) * 3600

    def test_all(self):
        assert resolve_period("all", allow_all=True) == (None, None)
        with pytest.raises(ValueError, match="all"):
            resolve_period("all")

    def test_default_param(self):
        now = local(2026, 6, 10, 12, 0, 0)
        frm, to = resolve_period(None, now=now, default="7d")
        assert to - frm == 7 * 86400

    def test_whitespace_and_case(self):
        now = local(2026, 6, 10, 12, 0, 0)
        assert resolve_period(" 24H ", now=now) == resolve_period("24h", now=now)
        assert resolve_period("Today", now=now) == resolve_period("today", now=now)

    @pytest.mark.parametrize("s", ["7w", "yesterday2", "2026-13-01", "0d", "", "-1d", "1.5d",
                                   "d", "2026-02-30", "2026-03-28..", "..2026-03-28", "abc"])
    def test_invalid(self, s):
        with pytest.raises(ValueError):
            resolve_period(s, now=local(2026, 6, 10))

    def test_max_days(self):
        now = local(2026, 6, 10, 12, 0, 0)
        resolve_period("92d", now=now, max_days=92)
        with pytest.raises(ValueError, match="period longer than 92 days; use a shorter window"):
            resolve_period("93d", now=now, max_days=92)
        with pytest.raises(ValueError, match="longer than 7 days"):
            resolve_period("2026-06-01..2026-06-09", now=now, max_days=7)
        resolve_period("2026-06-01..2026-06-07", now=now, max_days=7)

    def test_max_days_none_unbounded(self):
        resolve_period("3650d", now=local(2026, 6, 10))

    def test_future_only_rejected(self):
        now = local(2026, 6, 10, 12, 0, 0)
        with pytest.raises(ValueError, match="future"):
            resolve_period("2026-06-11", now=now)
        with pytest.raises(ValueError, match="future"):
            resolve_period("2027-01-01..2027-01-03", now=now)

    def test_future_end_partially_allowed(self):
        # a range that has already started is not "future-only"
        now = local(2026, 6, 10, 12, 0, 0)
        frm, to = resolve_period("2026-06-09..2026-06-11", now=now)
        assert frm == local(2026, 6, 9)

    def test_now_default_clock(self):
        frm, to = resolve_period("1h")
        assert to - frm == 3600 and to <= time.time()


class TestToLocalIso:
    def test_summer(self):
        assert to_local_iso(local(2026, 6, 10, 14, 5, 6)) == "2026-06-10T14:05:06+02:00"

    def test_winter(self):
        assert to_local_iso(local(2026, 1, 10, 14, 5, 6)) == "2026-01-10T14:05:06+01:00"

    def test_float_truncated_to_seconds(self):
        assert to_local_iso(local(2026, 1, 10, 14, 5, 6) + 0.9) == "2026-01-10T14:05:06+01:00"

    def test_none(self):
        assert to_local_iso(None) is None

    def test_roundtrip(self):
        ts = local(2026, 10, 25, 2, 30, 0)
        assert datetime.fromisoformat(to_local_iso(ts)).timestamp() == ts
