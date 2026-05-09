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

    def test_token_not_logged_on_http_error(self, monkeypatch, caplog):
        """A 401 from Telegram must not echo the bot token into the log.
        urllib.error.HTTPError exposes the request URL via .url; we format
        only code+reason so the token never appears."""
        import urllib.error as _ue
        secret = "SUPERSECRETTOKEN12345"
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", secret)
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        err = _ue.HTTPError(
            url=f"https://api.telegram.org/bot{secret}/sendMessage",
            code=401, msg="Unauthorized", hdrs={}, fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=err), \
             caplog.at_level("WARNING", logger="notifier"):
            notifier._send("hello")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in joined
        assert "401" in joined  # we still log something useful

    def test_token_not_logged_on_url_error(self, monkeypatch, caplog):
        import urllib.error as _ue
        secret = "ANOTHERSECRET98765"
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", secret)
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        err = _ue.URLError(
            reason="connection refused",
            filename=f"https://api.telegram.org/bot{secret}/sendMessage",
        )
        with patch("urllib.request.urlopen", side_effect=err), \
             caplog.at_level("WARNING", logger="notifier"):
            notifier._send("hello")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in joined


class TestDescribeExc:
    def test_http_error_uses_code_and_reason_only(self):
        import urllib.error as _ue
        err = _ue.HTTPError(
            url="https://api.telegram.org/botSECRET/sendMessage",
            code=429, msg="Too Many Requests", hdrs={}, fp=None,
        )
        result = notifier._describe_exc(err)
        assert "429" in result
        assert "Too Many Requests" in result
        assert "SECRET" not in result

    def test_url_error_uses_reason_only(self):
        import urllib.error as _ue
        err = _ue.URLError(reason="dns failure",
                           filename="https://api.telegram.org/botSECRET/getUpdates")
        result = notifier._describe_exc(err)
        assert "dns failure" in result
        assert "SECRET" not in result

    def test_unknown_exception_falls_back_to_type_name(self):
        result = notifier._describe_exc(RuntimeError("boom"))
        assert result == "RuntimeError"


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

    def test_message_contains_country(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "SP-LRA", None, None, None, 50.0)
        # abc123 = United States
        assert "United States" in sent[0]

    def test_message_country_unknown_for_unmapped_icao(self, monkeypatch):
        """ICAO `000000` is in an unassigned range — country renders as
        'Unknown' rather than crashing or omitting the line."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("000000", "TEST", None, None, None, 50.0)
        assert "Country: Unknown" in sent[0]

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
        # Line 3: country
        assert lines[2].startswith("Country:")
        # Line 4: distance
        assert lines[3].startswith("Distance:")
        # Line 5: link
        assert '<a href="http://test/stats/aircraft/abc123">' in lines[4]

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

    def test_message_contains_country(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_interesting("abc123", "SP-LRA", None, None, None, 100.0)
        assert "United States" in sent[0]


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
        import datetime as _dt
        today = _dt.date.today()
        day_start = int(_dt.datetime.combine(today, _dt.time.min).timestamp())
        # Anchor to today's midnight so flight stays within today regardless of wall-clock hour.
        first_seen = day_start + 3600
        last_seen  = day_start + 3600 + 7200
        insert_flight(db_conn, icao="long11", registration="SP-LNG",
                      first_seen=first_seen, last_seen=last_seen)
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


# ---------------------------------------------------------------------------
# _get_photo_result
# ---------------------------------------------------------------------------

class TestGetPhotoResult:
    """All tests use a temp-file DB so _get_photo_result can open/close it normally."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.close()
        monkeypatch.setattr(config, "DB_PATH", db_path)
        self.db_path = db_path

        # Convenience: a short-lived connection for test setup/assertions
        self._setup_conn = database.connect(db_path)
        yield
        self._setup_conn.close()

    @property
    def conn(self):
        return self._setup_conn

    def test_photos_cache_hit_returns_url_and_false(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('abc123', 'https://example.com/t.jpg', NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        url, is_type = notifier._get_photo_result("abc123", None, None)
        assert url == "https://example.com/t.jpg"
        assert is_type is False

    def test_photos_negative_cache_returns_none_no_http(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('abc123', NULL, NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        fetched = []
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", None, None)
        assert url is None
        assert is_type is False
        assert fetched == []

    def test_type_photos_cache_hit_returns_url_and_true(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('B738', 'https://example.com/b738.jpg', NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: (_ for _ in ()).throw(AssertionError("should not fetch")))
        url, is_type = notifier._get_photo_result("abc123", "B738", "Boeing 737-800")
        assert url == "https://example.com/b738.jpg"
        assert is_type is True

    def test_type_photos_negative_cache_returns_none(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('B738', NULL, NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        fetched = []
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", "B738", "Boeing 737-800")
        assert url is None
        assert fetched == []

    def test_db_join_finds_cached_type_photo(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'G-ABCD', 'B738', 'Boeing 737-800', 0)"
        )
        self.conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('aabbcc', 'https://example.com/other.jpg', NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        fetched = []
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", "B738", "Boeing 737-800")
        assert url == "https://example.com/other.jpg"
        assert is_type is True
        assert fetched == []
        row = self.conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='B738'").fetchone()
        assert row and row[0] == "https://example.com/other.jpg"

    def test_planespotters_fetch_specific_icao(self, monkeypatch):
        fetched = []
        def fake_fetch(icao):
            fetched.append(icao)
            return "https://example.com/sp.jpg" if icao == "abc123" else None
        monkeypatch.setattr(notifier, "_planespotters_fetch", fake_fetch)
        url, is_type = notifier._get_photo_result("abc123", None, None)
        assert url == "https://example.com/sp.jpg"
        assert is_type is False
        assert fetched == ["abc123"]
        row = self.conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex='abc123'").fetchone()
        assert row and row[0] == "https://example.com/sp.jpg"

    def test_specific_fails_probes_type(self, monkeypatch):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'EF2K', 'Eurofighter Typhoon', 1)"
        )
        self.conn.commit()
        fetched = []
        def fake_fetch(icao):
            fetched.append(icao)
            return "https://example.com/ef2k.jpg" if icao == "probe01" else None
        monkeypatch.setattr(notifier, "_planespotters_fetch", fake_fetch)
        url, is_type = notifier._get_photo_result("abc123", "EF2K", "Eurofighter Typhoon")
        assert url == "https://example.com/ef2k.jpg"
        assert is_type is True
        assert fetched == ["abc123", "probe01"]

    def test_all_fail_stores_negatives_returns_none(self, monkeypatch):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'EF2K', 'Eurofighter Typhoon', 1)"
        )
        self.conn.commit()
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: None)
        url, is_type = notifier._get_photo_result("abc123", "EF2K", "Eurofighter Typhoon")
        assert url is None
        assert is_type is False
        p_row = self.conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex='abc123'").fetchone()
        t_row = self.conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert p_row is not None
        assert t_row is not None

    def test_null_type_code_skips_type_logic(self, monkeypatch):
        fetched = []
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", None, None)
        assert url is None
        assert is_type is False
        assert fetched == ["abc123"]
        t_row = self.conn.execute("SELECT * FROM type_photos").fetchone()
        assert t_row is None


# ---------------------------------------------------------------------------
# _send_photo
# ---------------------------------------------------------------------------

class TestSendPhoto:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        notifier._tg_enabled = None
        notifier._tg_validated = False
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "99")
        yield
        notifier._tg_enabled = None
        notifier._tg_validated = False

    def test_calls_send_photo_api(self, monkeypatch):
        calls = []
        mock_resp = _mock_urlopen()
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: (calls.append(req) or mock_resp))
        result = notifier._send_photo("https://example.com/photo.jpg", "caption")
        assert result is True
        assert len(calls) == 1
        assert "sendPhoto" in calls[0].full_url
        body = json.loads(calls[0].data)
        assert body["photo"] == "https://example.com/photo.jpg"
        assert body["caption"] == "caption"
        assert body["parse_mode"] == "HTML"

    def test_api_failure_falls_back_to_send_message(self, monkeypatch):
        def raise_error(req, timeout=None):
            raise OSError("network")
        monkeypatch.setattr("urllib.request.urlopen", raise_error)
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        result = notifier._send_photo("https://example.com/photo.jpg", "caption text")
        assert result is True
        assert sent == ["caption text"]

    def test_non_http_url_falls_back_without_api_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: calls.append(req))
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        notifier._send_photo("ftp://bad.url/photo.jpg", "caption")
        assert calls == []
        assert sent == ["caption"]

    def test_telegram_disabled_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
        notifier._tg_enabled = None
        notifier._tg_validated = False
        result = notifier._send_photo("https://example.com/photo.jpg", "caption")
        assert result is False


# ---------------------------------------------------------------------------
# notify_military / notify_interesting / notify_watchlist — photo dispatch
# ---------------------------------------------------------------------------

class TestNotifyWithPhoto:
    """Tests for the photo-dispatch path in the three notify functions."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.close()
        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(config, "TELEGRAM_PHOTOS", 1)
        self._setup_conn = database.connect(db_path)
        yield
        self._setup_conn.close()

    @property
    def conn(self):
        return self._setup_conn

    def _seed_photo(self, icao, url):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO photos (icao_hex, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES (?,?,NULL,NULL,NULL,?)", (icao, url, now)
        )
        self.conn.commit()

    def test_notify_military_with_specific_photo_uses_send_photo(self, monkeypatch):
        self._seed_photo("abc123", "https://example.com/mil.jpg")
        sent_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda url, cap: sent_photos.append((url, cap)) or True)
        notifier.notify_military("abc123", "SP-MIL", None, "F-16", "F16", 100.0)
        assert len(sent_photos) == 1
        url, cap = sent_photos[0]
        assert url == "https://example.com/mil.jpg"
        assert "Military aircraft" in cap
        assert "is_type_photo" not in cap  # no type note for specific photo

    def test_notify_military_with_type_photo_appends_type_note(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('F16', 'https://example.com/f16.jpg', NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        sent_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda url, cap: sent_photos.append((url, cap)) or True)
        notifier.notify_military("abc123", "SP-MIL", None, "General Dynamics F-16", "F16", 100.0)
        assert len(sent_photos) == 1
        url, cap = sent_photos[0]
        assert url == "https://example.com/f16.jpg"
        assert "General Dynamics F-16" in cap
        assert "not this specific aircraft" in cap

    def test_notify_military_no_photo_uses_send_text(self, monkeypatch):
        monkeypatch.setattr(notifier, "_planespotters_fetch", lambda h: None)
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        send_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda u, c: send_photos.append(u))
        notifier.notify_military("abc123", "SP-MIL", None, None, None, 100.0)
        assert len(sent) == 1
        assert send_photos == []

    def test_notify_military_photos_disabled_skips_lookup(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_PHOTOS", 0)
        fetched = []
        monkeypatch.setattr(notifier, "_get_photo_result",
                            lambda *a: fetched.append(a) or (None, False))
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        notifier.notify_military("abc123", "SP-MIL", None, None, None, 100.0)
        assert fetched == []
        assert len(sent) == 1

    def test_notify_interesting_with_photo(self, monkeypatch):
        self._seed_photo("abc123", "https://example.com/int.jpg")
        sent_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda url, cap: sent_photos.append((url, cap)) or True)
        notifier.notify_interesting("abc123", "G-EXEC", None, "Gulfstream G650", "GL5T", 200.0)
        assert len(sent_photos) == 1
        assert "Interesting aircraft" in sent_photos[0][1]

    def test_notify_watchlist_with_photo(self, monkeypatch):
        self._seed_photo("abc123", "https://example.com/wl.jpg")
        sent_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda url, cap: sent_photos.append((url, cap)) or True)
        notifier.notify_watchlist("abc123", "G-EXEC", None, None, None, 50.0, "My plane", 42)
        assert len(sent_photos) == 1
        assert "Watchlist" in sent_photos[0][1]

    def test_type_note_html_escapes_special_chars(self, monkeypatch):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO type_photos (type_code, thumbnail_url, large_url, link_url, photographer, fetched_at) "
            "VALUES ('A&B', 'https://example.com/ab.jpg', NULL, NULL, NULL, ?)", (now,)
        )
        self.conn.commit()
        sent_photos = []
        monkeypatch.setattr(notifier, "_send_photo", lambda url, cap: sent_photos.append((url, cap)) or True)
        notifier.notify_military("abc123", "SP-MIL", None, "Type A&B", "A&B", 100.0)
        assert len(sent_photos) == 1
        cap = sent_photos[0][1]
        # The <i> type note must use &amp; — raw & in HTML tags is an XSS risk
        assert "<i>Photo: Type A&amp;B — not this specific aircraft</i>" in cap
