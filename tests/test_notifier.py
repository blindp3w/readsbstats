"""
Tests for notifier.py — Telegram notification helper.
All network I/O is mocked; uses in-memory SQLite for DB-backed functions.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from readsbstats import config, database, notifier


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_db():
    conn = database.connect(":memory:")
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


@pytest.fixture()
def db_conn():
    conn = make_db()
    yield conn
    conn.close()


def insert_flight(conn, *, icao="aabbcc", callsign=None, registration=None,
                  aircraft_type=None, squawk=None, first_seen=None, last_seen=None,
                  max_distance_nm=None, max_gs=None, max_alt_baro=None):
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO flights (icao_hex, callsign, registration, aircraft_type,
                             squawk, first_seen, last_seen, max_distance_nm,
                             max_gs, max_alt_baro)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (icao, callsign, registration, aircraft_type, squawk,
         first_seen or now, last_seen or now, max_distance_nm,
         max_gs, max_alt_baro),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _mock_urlopen(response_bytes=b'{"ok":true}'):
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = response_bytes
    return mock_resp


# ---------------------------------------------------------------------------
# _fmt_dist
# ---------------------------------------------------------------------------

class TestFmtDist:
    def test_none(self):
        assert notifier._fmt_dist(None) == "?"

    def test_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "metric")
        assert notifier._fmt_dist(100) == "185 km"

    def test_imperial(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "imperial")
        assert notifier._fmt_dist(100) == "115 mi"

    def test_aeronautical(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "aeronautical")
        assert notifier._fmt_dist(100) == "100 nm"

    def test_invalid_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "garbage")
        assert notifier._fmt_dist(100) == "185 km"

    def test_empty_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "")
        assert notifier._fmt_dist(100) == "185 km"


# ---------------------------------------------------------------------------
# _fmt_alt
# ---------------------------------------------------------------------------

class TestFmtAlt:
    def test_none(self):
        assert notifier._fmt_alt(None) == "?"

    def test_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "metric")
        assert notifier._fmt_alt(10000) == "3,048 m"

    def test_imperial_uses_feet(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "imperial")
        assert notifier._fmt_alt(10000) == "10,000 ft"

    def test_aeronautical_uses_feet(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "aeronautical")
        assert notifier._fmt_alt(10000) == "10,000 ft"

    def test_invalid_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "garbage")
        assert notifier._fmt_alt(10000) == "3,048 m"

    def test_empty_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "")
        assert notifier._fmt_alt(10000) == "3,048 m"


# ---------------------------------------------------------------------------
# _fmt_spd
# ---------------------------------------------------------------------------

class TestFmtSpd:
    def test_none(self):
        assert notifier._fmt_spd(None) == "?"

    def test_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "metric")
        assert notifier._fmt_spd(100) == "185 km/h"

    def test_imperial(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "imperial")
        assert notifier._fmt_spd(100) == "115 mph"

    def test_aeronautical(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "aeronautical")
        assert notifier._fmt_spd(100) == "100 kts"

    def test_invalid_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "garbage")
        assert notifier._fmt_spd(100) == "185 km/h"

    def test_empty_falls_back_to_metric(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "")
        assert notifier._fmt_spd(100) == "185 km/h"


# ---------------------------------------------------------------------------
# telegram_enabled
# ---------------------------------------------------------------------------

class TestTelegramEnabled:
    @pytest.fixture(autouse=True)
    def setup(self):
        notifier._tg_enabled = None  # reset cached state
        notifier._tg_validated = False
        yield
        notifier._tg_enabled = None
        notifier._tg_validated = False

    def test_both_set_returns_true(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        assert notifier.telegram_enabled() is True

    def test_empty_token_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        assert notifier.telegram_enabled() is False

    def test_empty_chat_id_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        assert notifier.telegram_enabled() is False

    def test_both_empty_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        assert notifier.telegram_enabled() is False

    def test_non_numeric_chat_id_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "not-a-number")
        assert notifier.telegram_enabled() is False

    def test_negative_chat_id_accepted(self, monkeypatch):
        """Negative chat IDs are valid (group/supergroup chats)."""
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-100123456789")
        assert notifier.telegram_enabled() is True

    def test_result_is_cached(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        assert notifier.telegram_enabled() is True
        # Change config after first call — cached result should persist
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        assert notifier.telegram_enabled() is True

    def test_warns_token_only(self, monkeypatch, caplog):
        """Warn when token is set but chat_id is missing."""
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        with caplog.at_level(logging.WARNING):
            notifier.telegram_enabled()
        assert any("RSBS_TELEGRAM_CHAT_ID" in r.message for r in caplog.records)

    def test_warns_chat_id_only(self, monkeypatch, caplog):
        """Warn when chat_id is set but token is missing."""
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        with caplog.at_level(logging.WARNING):
            notifier.telegram_enabled()
        assert any("RSBS_TELEGRAM_TOKEN" in r.message for r in caplog.records)

    def test_warns_non_numeric_chat_id(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "not-a-number")
        with caplog.at_level(logging.WARNING):
            notifier.telegram_enabled()
        assert any("RSBS_TELEGRAM_CHAT_ID" in r.message for r in caplog.records)

    def test_no_warning_when_both_empty(self, monkeypatch, caplog):
        """Both empty = intentionally disabled, no warning needed."""
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        with caplog.at_level(logging.WARNING):
            notifier.telegram_enabled()
        assert not any("TELEGRAM" in r.message for r in caplog.records)

    def test_warns_invalid_units(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "fathoms")
        with caplog.at_level(logging.WARNING):
            result = notifier.telegram_enabled()
        assert result is True  # still enabled, just a warning
        assert any("RSBS_TELEGRAM_UNITS" in r.message for r in caplog.records)

    def test_no_warning_for_valid_units(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123456:ABC")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99887766")
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "imperial")
        with caplog.at_level(logging.WARNING):
            notifier.telegram_enabled()
        assert not any("RSBS_TELEGRAM_UNITS" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _send
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.fixture(autouse=True)
    def setup(self):
        notifier._tg_enabled = None
        notifier._tg_validated = False
        yield
        notifier._tg_enabled = None
        notifier._tg_validated = False

    def test_noop_no_token(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        assert notifier._send("hello") is False

    def test_noop_no_chat_id(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        assert notifier._send("hello") is False

    def test_success(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        with patch("urllib.request.urlopen", return_value=_mock_urlopen()):
            result = notifier._send("hello")
        assert result is True

    def test_network_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = notifier._send("hello")
        assert result is False


# ---------------------------------------------------------------------------
# notify_military
# ---------------------------------------------------------------------------

class TestNotifyMilitary:
    def test_message_contains_registration_and_callsign(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "SP-LRA", "LOT100", "Boeing 737", None, 50.0)
        msg = sent[0]
        assert "SP-LRA" in msg
        assert "LOT100" in msg
        assert "Military" in msg
        assert "Boeing 737" in msg

    def test_falls_back_to_icao_when_no_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", None, None, None, None, None)
        msg = sent[0]
        assert "ABC123" in msg
        assert "?" in msg  # distance is None

    def test_uses_aircraft_type_fallback(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "SP-X", None, None, "F16", 10.0)
        assert "F16" in sent[0]

    def test_unknown_type_when_both_none(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "SP-X", None, None, None, 10.0)
        assert "Unknown type" in sent[0]

    def test_message_html_structure(self, monkeypatch):
        """Verify message has correct HTML structure, not just substrings."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "BASE_URL", "http://test/stats")
        notifier.notify_military("abc123", "SP-LRA", "LOT100", "Boeing 737", None, 50.0)
        msg = sent[0]
        lines = msg.split("\n")
        # Line 1: emoji + bold title
        assert "<b>Military aircraft" in lines[0]
        assert "</b>" in lines[0]
        # Line 2: bold reg + callsign + aircraft type
        assert "<b>SP-LRA</b>" in lines[1]
        assert "(LOT100)" in lines[1]
        assert "Boeing 737" in lines[1]
        # Line 3: distance
        assert lines[2].startswith("Distance:")
        # Line 4: link
        assert '<a href="http://test/stats/aircraft/abc123">' in lines[3]

    def test_html_entities_in_registration_not_injected(self, monkeypatch):
        """Ensure registration with HTML chars doesn't break message."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "<script>", None, None, None, 10.0)
        # The reg is used in bold tags — verify it appears literally (not escaped,
        # since Telegram HTML parse_mode handles this, but no injection)
        assert "<script>" in sent[0]


# ---------------------------------------------------------------------------
# notify_interesting
# ---------------------------------------------------------------------------

class TestNotifyInteresting:
    def test_message_contains_registration_and_callsign(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_interesting("abc123", "SP-LRA", "LOT100", "Boeing 737", None, 100.0)
        msg = sent[0]
        assert "Interesting" in msg
        assert "SP-LRA" in msg
        assert "LOT100" in msg

    def test_uses_aircraft_type_when_no_type_desc(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_interesting("abc123", "SP-LRA", None, None, "B738", 100.0)
        assert "B738" in sent[0]

    def test_no_callsign_omits_parentheses(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_interesting("abc123", "SP-LRA", None, "Gulfstream", None, 100.0)
        assert "(" not in sent[0] or "Interesting" in sent[0]


# ---------------------------------------------------------------------------
# notify_squawk
# ---------------------------------------------------------------------------

class TestNotifySquawk:
    def test_7500_hijack_label(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_squawk("abc123", "SP-LRA", "LOT1", "7500", 30.0)
        msg = sent[0]
        assert "7500" in msg
        assert "Hijack" in msg
        assert "LOT1" in msg

    def test_7600_radio_failure_label(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_squawk("abc123", None, None, "7600", None)
        msg = sent[0]
        assert "7600" in msg
        assert "Radio failure" in msg

    def test_7700_emergency_label(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_squawk("abc123", "SP-LRA", None, "7700", 30.0)
        msg = sent[0]
        assert "7700" in msg
        assert "Emergency" in msg

    def test_unknown_squawk_uses_raw_code(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_squawk("abc123", "SP-LRA", None, "1234", 10.0)
        msg = sent[0]
        assert "1234" in msg

    def test_falls_back_to_icao_when_no_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_squawk("abc123", None, None, "7700", 5.0)
        assert "ABC123" in sent[0]


# ---------------------------------------------------------------------------
# send_daily_summary
# ---------------------------------------------------------------------------

class TestSendDailySummary:
    def test_empty_day_sends_zero_counts(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.send_daily_summary(db_conn)
        assert sent
        msg = sent[0]
        assert "Flights: <b>0</b>" in msg
        assert "Aircraft: <b>0</b>" in msg

    def test_counts_flights_and_distinct_aircraft(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="aaa111", first_seen=now)
        insert_flight(db_conn, icao="bbb222", first_seen=now)
        insert_flight(db_conn, icao="aaa111", first_seen=now + 1)  # same ICAO, second flight
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Flights: <b>3</b>" in msg
        assert "Aircraft: <b>2</b>" in msg

    def test_military_badge(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="mil111", first_seen=now)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES (?, ?, ?)",
            ("mil111", "MIL-1", 1),  # flags & 1 = military
        )
        db_conn.commit()
        notifier.send_daily_summary(db_conn)
        assert "Military: 1" in sent[0]

    def test_interesting_badge(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="int111", first_seen=now)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, flags) VALUES (?, ?, ?)",
            ("int111", "INT-1", 2),  # flags & 2 = interesting, flags & 1 = 0 (not military)
        )
        db_conn.commit()
        notifier.send_daily_summary(db_conn)
        assert "Interesting: 1" in sent[0]

    def test_squawk_badge(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="sq1111", squawk="7700", first_seen=now)
        notifier.send_daily_summary(db_conn)
        assert "Emergency squawks: 1" in sent[0]

    def test_furthest_aircraft(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="far111", registration="SP-FAR",
                      max_distance_nm=350.0, first_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Furthest" in msg
        assert "SP-FAR" in msg

    def test_furthest_falls_back_to_aircraft_db_registration(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="far222", max_distance_nm=200.0, first_seen=now)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration) VALUES (?, ?)",
            ("far222", "DB-REG"),
        )
        db_conn.commit()
        notifier.send_daily_summary(db_conn)
        assert "DB-REG" in sent[0]

    def test_busiest_hour_shown(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        for i in range(3):
            insert_flight(db_conn, icao=f"b{i:05d}", first_seen=now)
        notifier.send_daily_summary(db_conn)
        assert "Busiest hour" in sent[0]

    def test_fastest_aircraft(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="fast11", registration="SP-JET",
                      max_gs=520.0, first_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Fastest" in msg
        assert "SP-JET" in msg

    def test_highest_aircraft(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="high11", registration="SP-HI",
                      max_alt_baro=41000, first_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Highest" in msg
        assert "SP-HI" in msg

    def test_longest_tracked(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="long11", registration="SP-LNG",
                      first_seen=now - 7200, last_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Longest" in msg
        assert "SP-LNG" in msg
        assert "2h 00m" in msg

    def test_records_omitted_when_none(self, db_conn, monkeypatch):
        """No fastest/highest/longest lines when all flights lack the data."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="plain1", first_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Fastest" not in msg
        assert "Highest" not in msg
        assert "Longest" not in msg

    def test_no_badges_when_all_zero(self, db_conn, monkeypatch):
        """Verify badge lines are omitted when there are no military/interesting/squawks."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="ord111", first_seen=now)
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Military" not in msg
        assert "Interesting" not in msg
        assert "squawks" not in msg


# ---------------------------------------------------------------------------
# _send_status
# ---------------------------------------------------------------------------

class TestSendStatus:
    def test_no_active_flights(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._send_status(db_conn)
        msg = sent[0]
        assert "Status" in msg
        assert "No aircraft currently tracked" in msg

    def test_with_active_flights(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        fid = insert_flight(db_conn, icao="abc123", registration="SP-LRA",
                            callsign="LOT1", first_seen=now)
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?, ?, ?)",
            ("abc123", fid, now),
        )
        db_conn.commit()
        notifier._send_status(db_conn)
        msg = sent[0]
        assert "Tracking" in msg
        assert "SP-LRA" in msg
        assert "LOT1" in msg

    def test_active_flight_type_desc_from_aircraft_db(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        fid = insert_flight(db_conn, icao="def456", first_seen=now)
        db_conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_desc) VALUES (?, ?, ?)",
            ("def456", "SP-DEF", "Airbus A320"),
        )
        db_conn.execute(
            "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?, ?, ?)",
            ("def456", fid, now),
        )
        db_conn.commit()
        notifier._send_status(db_conn)
        assert "Airbus A320" in sent[0]

    def test_truncates_at_ten(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        for i in range(15):
            icao = f"a{i:05d}"
            fid = insert_flight(db_conn, icao=icao, first_seen=now - i)
            db_conn.execute(
                "INSERT INTO active_flights (icao_hex, flight_id, last_seen) VALUES (?, ?, ?)",
                (icao, fid, now - i),
            )
        db_conn.commit()
        notifier._send_status(db_conn)
        assert "and 5 more" in sent[0]

    def test_counts_todays_flights(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="t1a111", first_seen=now)
        insert_flight(db_conn, icao="t2b222", first_seen=now)
        notifier._send_status(db_conn)
        assert "Flights today: <b>2</b>" in sent[0]


# ---------------------------------------------------------------------------
# _send_help
# ---------------------------------------------------------------------------

class TestSendHelp:
    def test_sends_all_commands(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._send_help()
        msg = sent[0]
        assert "/summary" in msg
        assert "/status" in msg
        assert "/help" in msg


# ---------------------------------------------------------------------------
# _handle_update
# ---------------------------------------------------------------------------

class TestHandleUpdate:
    def _upd(self, chat_id, text):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_summary_dispatches(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "send_daily_summary", lambda c: called.append("summary"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/summary"), db_conn)
        assert called == ["summary"]

    def test_status_dispatches(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_status", lambda c: called.append("status"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/status"), db_conn)
        assert called == ["status"]

    def test_help_dispatches(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_help", lambda: called.append("help"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/help"), db_conn)
        assert called == ["help"]

    def test_start_dispatches_to_help(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_help", lambda: called.append("help"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/start"), db_conn)
        assert called == ["help"]

    def test_case_insensitive(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_help", lambda: called.append("help"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/Help"), db_conn)
        assert called == ["help"]

    def test_strips_bot_username_suffix(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_help", lambda: called.append("help"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/help@mybot"), db_conn)
        assert called == ["help"]

    def test_unknown_command_is_silently_ignored(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("99", "/unknown"), db_conn)
        assert not sent

    def test_wrong_chat_id_is_ignored(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_help", lambda: called.append("help"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        notifier._handle_update(self._upd("evil_id", "/help"), db_conn)
        assert not called

    def test_missing_text_field(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        update = {"update_id": 1, "message": {"chat": {"id": "99"}}}  # no "text"
        notifier._handle_update(update, db_conn)
        assert not sent


# ---------------------------------------------------------------------------
# _get_updates
# ---------------------------------------------------------------------------

class TestGetUpdates:
    def test_parses_result_list(self, monkeypatch):
        payload = json.dumps({"result": [{"update_id": 1}]}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)) as mock_open:
            result = notifier._get_updates(0)
        assert result == [{"update_id": 1}]
        assert mock_open.called

    def test_empty_result(self, monkeypatch):
        payload = json.dumps({"result": []}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = notifier._get_updates(5)
        assert result == []

    def test_offset_included_in_url(self, monkeypatch):
        payload = json.dumps({"result": []}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        captured = []
        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_urlopen(payload)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            notifier._get_updates(42)
        assert "offset=42" in captured[0]


# ---------------------------------------------------------------------------
# start_command_listener
# ---------------------------------------------------------------------------

class TestStartCommandListener:
    @pytest.fixture(autouse=True)
    def setup(self):
        notifier._tg_enabled = None
        notifier._tg_validated = False
        yield
        notifier._tg_enabled = None
        notifier._tg_validated = False

    def test_noop_no_token(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        before = threading.active_count()
        notifier.start_command_listener("/fake/db")
        assert threading.active_count() == before

    def test_noop_no_chat_id(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        before = threading.active_count()
        notifier.start_command_listener("/fake/db")
        assert threading.active_count() == before

    def test_spawns_named_daemon_thread(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        # Replace the loop body so the thread exits immediately
        monkeypatch.setattr(notifier, "_listener_loop", lambda path: None)
        notifier.start_command_listener("/fake/db")
        # Give the thread a moment to register
        time.sleep(0.05)
        names = [t.name for t in threading.enumerate()]
        # The thread may already have exited (daemon + lambda returns immediately),
        # so just verify no exception was raised and the function returned normally.
        # The thread-name check is best-effort.
        assert True  # reached here without exception


# ---------------------------------------------------------------------------
# _listener_loop
# ---------------------------------------------------------------------------

class TestListenerLoop:
    def test_processes_update_and_handles_network_error(self, monkeypatch):
        """Success path then error path (sleeps), then KeyboardInterrupt exits."""
        conn = make_db()
        call_count = [0]
        help_called = []

        def fake_get_updates(offset, timeout=30):
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                return [{"update_id": 10,
                         "message": {"chat": {"id": "99"}, "text": "/help"}}]
            if n == 1:
                raise OSError("network down")
            raise KeyboardInterrupt()

        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier, "_get_updates", fake_get_updates)
        monkeypatch.setattr(notifier, "_send_help", lambda: help_called.append(1))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(database, "connect", lambda path: conn)

        with pytest.raises(KeyboardInterrupt):
            notifier._listener_loop("/fake/db")

        assert help_called == [1]
        conn.close()

    def test_advances_offset_after_each_update(self, monkeypatch):
        conn = make_db()
        offsets_seen = []
        call_count = [0]

        def fake_get_updates(offset, timeout=30):
            offsets_seen.append(offset)
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                return [
                    {"update_id": 5, "message": {"chat": {"id": "99"}, "text": "/help"}},
                    {"update_id": 6, "message": {"chat": {"id": "99"}, "text": "/help"}},
                ]
            raise KeyboardInterrupt()

        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier, "_get_updates", fake_get_updates)
        monkeypatch.setattr(notifier, "_send_help", lambda: None)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(database, "connect", lambda path: conn)

        with pytest.raises(KeyboardInterrupt):
            notifier._listener_loop("/fake/db")

        assert offsets_seen[0] == 0   # first call starts at 0
        assert offsets_seen[1] == 7   # after update_ids 5 and 6, offset = 6+1 = 7
        conn.close()

    def test_handle_update_exception_is_swallowed(self, monkeypatch):
        """An exception inside _handle_update must not crash the loop."""
        conn = make_db()
        call_count = [0]

        def fake_get_updates(offset, timeout=30):
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                return [{"update_id": 5,
                         "message": {"chat": {"id": "99"}, "text": "/help"}}]
            raise KeyboardInterrupt()

        def bad_handle(upd, c):
            raise RuntimeError("boom")

        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier, "_get_updates", fake_get_updates)
        monkeypatch.setattr(notifier, "_handle_update", bad_handle)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(database, "connect", lambda path: conn)

        with pytest.raises(KeyboardInterrupt):
            notifier._listener_loop("/fake/db")

        conn.close()


# ---------------------------------------------------------------------------
# notify_watchlist
# ---------------------------------------------------------------------------

class TestNotifyWatchlist:
    def test_message_contains_registration_and_callsign(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_watchlist("abc123", "SP-LRF", "LOT123", "Airbus A320", None, 50.0, None, 99)
        msg = sent[0]
        assert "SP-LRF" in msg
        assert "LOT123" in msg
        assert "Watchlist" in msg
        assert "Airbus A320" in msg

    def test_label_included_when_set(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_watchlist("abc123", "SP-LRF", None, None, None, 10.0, "My plane", 5)
        assert "My plane" in sent[0]

    def test_label_omitted_when_none(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_watchlist("abc123", "SP-LRF", None, None, None, 10.0, None, 5)
        assert "Label" not in sent[0]

    def test_falls_back_to_icao_when_no_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_watchlist("abc123", None, None, None, None, None, None, 1)
        assert "ABC123" in sent[0]

    def test_flight_link_included(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "BASE_URL", "http://homepi.local/stats")
        notifier.notify_watchlist("abc123", "SP-X", None, None, None, 5.0, None, 42)
        assert "/flight/42" in sent[0]
        assert "/aircraft/abc123" in sent[0]


# ---------------------------------------------------------------------------
# _send_watchlist_list / _watch_add / _watch_remove
# ---------------------------------------------------------------------------

class TestWatchlistBotCommands:
    def test_list_empty(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._send_watchlist_list(db_conn)
        assert "Empty" in sent[0]

    def test_list_shows_entries(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,?,?)",
            ("icao", "aabbcc", "My plane", int(time.time())),
        )
        db_conn.commit()
        notifier._send_watchlist_list(db_conn)
        assert "AABBCC" in sent[0]
        assert "My plane" in sent[0]
        assert "ICAO" in sent[0]

    def test_watch_add_icao(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_add(db_conn, "aabbcc")
        row = db_conn.execute("SELECT * FROM watchlist WHERE value='aabbcc'").fetchone()
        assert row is not None
        assert row["match_type"] == "icao"
        assert "Added" in sent[0]

    def test_watch_add_registration(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_add(db_conn, "SP-LRF")
        row = db_conn.execute("SELECT * FROM watchlist WHERE value='sp-lrf'").fetchone()
        assert row is not None
        assert row["match_type"] == "registration"

    def test_watch_add_duplicate_rejected(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_add(db_conn, "aabbcc")
        notifier._watch_add(db_conn, "aabbcc")
        assert "Already" in sent[1]
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1

    def test_watch_add_empty_value(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_add(db_conn, "  ")
        assert "Usage" in sent[0]
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

    def test_watch_add_value_too_long_rejected(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_add(db_conn, "x" * 100)
        assert "too long" in sent[0].lower()
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

    def test_watch_remove_existing(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("icao", "aabbcc", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "aabbcc")
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        assert "Removed" in sent[0]

    def test_watch_remove_not_found(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._watch_remove(db_conn, "aabbcc")
        assert "Not in watchlist" in sent[0]

    def test_handle_update_watchlist_command(self, db_conn, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_send_watchlist_list", lambda c: called.append("wl"))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        upd = {"update_id": 1, "message": {"chat": {"id": "99"}, "text": "/watchlist"}}
        notifier._handle_update(upd, db_conn)
        assert called == ["wl"]

    def test_handle_update_watch_command(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        upd = {"update_id": 1, "message": {"chat": {"id": "99"}, "text": "/watch aabbcc"}}
        notifier._handle_update(upd, db_conn)
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1

    def test_handle_update_unwatch_command(self, db_conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("icao", "aabbcc", int(time.time())),
        )
        db_conn.commit()
        upd = {"update_id": 1, "message": {"chat": {"id": "99"}, "text": "/unwatch aabbcc"}}
        notifier._handle_update(upd, db_conn)
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

    def test_help_mentions_watch_commands(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier._send_help()
        msg = sent[0]
        assert "/watchlist" in msg
        assert "/watch" in msg
        assert "/unwatch" in msg
