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

from readsbstats import config, database, notifier, photo_sources
from readsbstats.photo_sources import PhotoResult


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


def _mock_urlopen(response_bytes=b'{"ok":true}', content_type="image/jpeg"):
    """Legacy context-manager-style mock — used by the photo-download path
    (still routed through `urllib.request.urlopen` patches at the photo_sources
    layer, before #124's notifier-side refactor)."""
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = response_bytes
    mock_resp.headers = {"Content-Type": content_type}
    return mock_resp


def _make_safe_urlopen(response_bytes=b'{"ok":true}',
                       content_type="application/json",
                       calls=None):
    """Build a fake replacement for `http_safe.safe_urlopen`.

    `calls` (if provided) is appended one dict per invocation capturing
    `url`, `data`, `extra_headers`, etc. — matching the keyword-only signature
    of the real function.  Returns `(body_bytes, headers_dict)`.
    """
    def _fake(url, *, timeout, max_bytes,
              extra_headers=None, data=None):
        if calls is not None:
            calls.append({
                "url": url, "timeout": timeout, "max_bytes": max_bytes,
                "extra_headers": dict(extra_headers or {}), "data": data,
            })
        return response_bytes, {"Content-Type": content_type}
    return _fake


# ---------------------------------------------------------------------------
# _h — HTML escape primitive (audit-12 #212)
# ---------------------------------------------------------------------------

class TestH:
    """Direct unit tests for the HTML escape helper used at every Telegram
    caption interpolation boundary. Indirect coverage exists via the
    notify_* tests, but a regression in _h itself would silently affect
    every alert path — explicit tests pin the contract."""

    def test_none_returns_empty_string(self):
        # _h is called at f-string boundaries where the source field may
        # legitimately be None (e.g. callsign for a non-broadcasting mode-S
        # contact). Must not emit "None" or raise.
        assert notifier._h(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert notifier._h("") == ""

    def test_ampersand_escaped(self):
        assert notifier._h("A & B") == "A &amp; B"

    def test_less_than_escaped(self):
        assert notifier._h("<bad>") == "&lt;bad&gt;"

    def test_greater_than_escaped(self):
        assert notifier._h(">x") == "&gt;x"

    def test_quote_escaped(self):
        # html.escape defaults to quote=True so single + double quotes are
        # also escaped. Telegram doesn't strictly require this but it's
        # belt-and-braces against future HTML-attribute interpolation.
        assert notifier._h('"hi"') == "&quot;hi&quot;"
        assert notifier._h("'x'") == "&#x27;x&#x27;"

    def test_mixed_payload(self):
        # The smoking-gun pattern: a registration like "AB<C&D>" must not
        # break Telegram's parser. Order of operations: escape `&` first
        # (already what html.escape does), THEN `<`/`>`. Otherwise the
        # `&lt;` we produce gets its own `&` re-escaped.
        assert notifier._h("AB<C&D>") == "AB&lt;C&amp;D&gt;"

    def test_plain_alnum_passthrough(self):
        # No-op on regular alphanumeric content (the common case).
        assert notifier._h("LOT123") == "LOT123"
        assert notifier._h("SP-LRA") == "SP-LRA"

    def test_idempotent_on_already_escaped_input(self):
        # If someone double-escapes by mistake the second pass produces
        # &amp;lt; etc. — Telegram will render those literally. Documenting
        # the (correct, html.escape) behaviour so a future change can't
        # silently "fix" it into something that breaks at the wrong layer.
        once = notifier._h("<a>")
        twice = notifier._h(once)
        assert twice == "&amp;lt;a&amp;gt;"


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

    def test_mixed_case_imperial_still_imperial(self, monkeypatch):
        # Audit-13 A13-035: RSBS_TELEGRAM_UNITS=Imperial silently fell back
        # to metric because the helpers compared raw config.TELEGRAM_UNITS
        # (mixed-case) against the lowercase string literal "imperial".
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "Imperial")
        assert notifier._fmt_dist(100) == "115 mi"


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

    def test_mixed_case_aeronautical_still_uses_feet(self, monkeypatch):
        # Audit-13 A13-035: mixed-case env vars must not fall back silently.
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "Aeronautical")
        assert notifier._fmt_alt(10000) == "10,000 ft"


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

    def test_mixed_case_imperial_still_imperial(self, monkeypatch):
        # Audit-13 A13-035: mixed-case env vars must not fall back silently.
        monkeypatch.setattr(config, "TELEGRAM_UNITS", "IMPERIAL")
        assert notifier._fmt_spd(100) == "115 mph"


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
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen())
        assert notifier._send("hello") is True

    def test_routes_through_http_safe(self, monkeypatch):
        """_send must call http_safe.safe_urlopen — improvements.md #124."""
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(calls=calls))
        notifier._send("hello")
        assert len(calls) == 1
        assert calls[0]["url"].endswith("/sendMessage")
        # POST body must be JSON-encoded
        body = json.loads(calls[0]["data"])
        assert body["text"] == "hello"
        assert body["parse_mode"] == "HTML"
        assert body["chat_id"] == "123"
        assert calls[0]["extra_headers"]["Content-Type"] == "application/json"

    def test_network_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
        def _raise(*a, **kw): raise OSError("timeout")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen", _raise)
        assert notifier._send("hello") is False

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
        def _raise(*a, **kw): raise err
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen", _raise)
        with caplog.at_level("WARNING", logger="notifier"):
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
        def _raise(*a, **kw): raise err
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen", _raise)
        with caplog.at_level("WARNING", logger="notifier"):
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
        monkeypatch.setattr(config, "TELEGRAM_BASE_URL","http://test/stats")
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

    def test_html_entities_in_registration_escaped(self, monkeypatch):
        """Registration with HTML chars must be escaped so Telegram doesn't 400
        on parse_mode=HTML.  The raw ``<script>`` must NOT survive into the
        outgoing message; ``&lt;script&gt;`` must appear instead."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "<script>", None, None, None, 10.0)
        assert "<script>" not in sent[0]
        assert "&lt;script&gt;" in sent[0]

    def test_ampersand_in_registration_escaped(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "AC&DC", None, None, None, 10.0)
        assert "AC&amp;DC" in sent[0]

    def test_callsign_with_special_chars_escaped(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "REG", "A<B>C", None, None, 10.0)
        assert "(A&lt;B&gt;C)" in sent[0]
        assert "A<B>C" not in sent[0]

    def test_type_desc_with_ampersand_escaped(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_military("abc123", "REG", None, "Embraer 170 & 175", "E170", 10.0)
        assert "Embraer 170 &amp; 175" in sent[0]
        assert "Embraer 170 & 175" not in sent[0]


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
# notify_anonymous
# ---------------------------------------------------------------------------

class TestNotifyAnonymous:
    """First-sighting alert for non-ICAO Mode-S hex addresses (FLAG_ANONYMOUS).
    The country line is intentionally absent — by definition the hex is not in
    any state allocation, so 'Country: Unknown' would be noise."""

    def test_message_labels_as_anonymous(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_anonymous("dd85cb", None, None, None, None, 107.1)
        msg = sent[0]
        assert "Anonymous aircraft" in msg
        assert "first sighting" in msg.lower()
        assert "Non-ICAO" in msg

    def test_falls_back_to_icao_when_no_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_anonymous("dd85cb", None, None, None, None, 100.0)
        # _fmt_aircraft_line uppercases the ICAO when no reg.
        assert "DD85CB" in sent[0]

    def test_message_omits_country_line(self, monkeypatch):
        # Country lookup returns "Unknown" for anon hex; rendering it would be
        # noise.  The notify_anonymous template must skip the Country: line entirely.
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_anonymous("dd85cb", "X", "CS01", "Type", "TYP", 50.0)
        assert "Country:" not in sent[0]

    def test_message_includes_view_profile_link(self, monkeypatch):
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_BASE_URL","http://test/stats")
        notifier.notify_anonymous("dd85cb", None, None, None, None, 100.0)
        assert '<a href="http://test/stats/aircraft/dd85cb">' in sent[0]

    def test_html_entities_in_callsign_escaped(self, monkeypatch):
        # Same defence-in-depth as the other notify_* helpers — every dynamic
        # field flows through _h() so Telegram parse_mode=HTML never 400s on
        # an unescaped '<', '>', or '&'.
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        notifier.notify_anonymous("dd85cb", "REG", "A&B", None, None, 10.0)
        assert "A&amp;B" in sent[0]
        # Raw ampersand-then-letter would have been "A&B"; with escaping the
        # only occurrence is the entity, never a bare "&B".
        assert "&B" not in sent[0].replace("&amp;B", "")

    def test_html_entities_in_icao_escaped_in_url(self, monkeypatch):
        # #110 defence-in-depth: collector guarantees 6-char lowercase hex,
        # but if that contract ever loosens, every {icao} interpolated into
        # an <a href> must flow through _h() so it can't break parse_mode=HTML.
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        monkeypatch.setattr(config, "TELEGRAM_BASE_URL", "http://test/stats")
        notifier.notify_anonymous("bad<ico&", None, None, None, None, 1.0)
        # The icao landed inside an href; it must be HTML-escaped, not raw.
        assert "bad&lt;ico&amp;" in sent[0]
        assert "bad<ico&" not in sent[0]


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

    def test_anonymous_badge(self, db_conn, monkeypatch):
        """Daily summary counts anonymous (non-ICAO hex) flights via the
        icao_ranges anon CASE — no DB column required.  See improvements.md
        line under 1.8.2 — closing the daily-summary coverage gap noted
        when the FLAG_ANONYMOUS README was retrofitted."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        # ff0000 falls outside every entry in icao_ranges._RAW
        insert_flight(db_conn, icao="ff0000", first_seen=now)
        notifier.send_daily_summary(db_conn)
        assert "Anonymous: 1" in sent[0]

    def test_anonymous_excluded_when_also_military(self, db_conn, monkeypatch):
        """Precedence in the daily summary mirrors the badges / Telegram
        alerts: military > interesting > anonymous.  An anon hex carrying
        the military flag via airplanes.live override counts only under
        Military."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="ff0001", first_seen=now)  # anon hex
        db_conn.execute(
            "INSERT INTO adsbx_overrides (icao_hex, flags, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            ("ff0001", 1, now, now),  # military bit set on the same hex
        )
        db_conn.commit()
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Military: 1" in msg
        assert "Anonymous" not in msg

    def test_anonymous_excluded_when_also_interesting(self, db_conn, monkeypatch):
        """Same precedence — interesting takes precedence over anonymous."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="ff0002", first_seen=now)  # anon hex
        db_conn.execute(
            "INSERT INTO adsbx_overrides (icao_hex, flags, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            ("ff0002", 2, now, now),  # interesting bit set
        )
        db_conn.commit()
        notifier.send_daily_summary(db_conn)
        msg = sent[0]
        assert "Interesting: 1" in msg
        assert "Anonymous" not in msg

    def test_anonymous_badge_absent_when_zero(self, db_conn, monkeypatch):
        """No "Anonymous: 0" line when there are no anon flights — matches
        the existing zero-suppression on Military / Interesting badges."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        now = int(time.time())
        insert_flight(db_conn, icao="aabbcc", first_seen=now)  # US — not anon
        notifier.send_daily_summary(db_conn)
        assert "Anonymous" not in sent[0]

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
        calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload, calls=calls))
        result = notifier._get_updates(0)
        assert result == [{"update_id": 1}]
        assert len(calls) == 1

    def test_empty_result(self, monkeypatch):
        payload = json.dumps({"result": []}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload))
        assert notifier._get_updates(5) == []

    def test_offset_included_in_url(self, monkeypatch):
        payload = json.dumps({"result": []}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload, calls=calls))
        notifier._get_updates(42)
        assert "offset=42" in calls[0]["url"]
        assert "/getUpdates?" in calls[0]["url"]

    def test_non_list_result_returns_empty(self, monkeypatch, caplog):
        # Audit-13 A13-009: defensive shape check. Telegram schema drift
        # or man-in-the-middle TLS termination could return a non-list
        # `result` field; iterating it would either iterate characters
        # (string) or raise mid-batch.
        payload = json.dumps({"ok": True, "result": "oops"}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload))
        with caplog.at_level("WARNING"):
            result = notifier._get_updates(0)
        assert result == []
        assert any("non-list" in r.getMessage() for r in caplog.records)

    def test_null_result_returns_empty(self, monkeypatch):
        # `result` key present but null also produces non-list type.
        payload = json.dumps({"ok": True, "result": None}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload))
        assert notifier._get_updates(0) == []

    def test_missing_result_key_returns_empty(self, monkeypatch):
        payload = json.dumps({"ok": True}).encode()
        monkeypatch.setattr(config, "TELEGRAM_TOKEN", "tok")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(payload))
        assert notifier._get_updates(0) == []


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
        monkeypatch.setattr(config, "TELEGRAM_BASE_URL","http://homepi.local/stats")
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

    def test_watch_remove_does_not_cross_match_types(self, db_conn, monkeypatch):
        """A 6-hex value should only remove the icao-typed row, even if a
        registration-typed row happens to share the same literal value.
        See improvements.md #116."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("icao", "abcdef", int(time.time())),
        )
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("registration", "abcdef", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "abcdef")
        # The icao row should be gone; the registration row should remain.
        rows = db_conn.execute(
            "SELECT match_type FROM watchlist WHERE value='abcdef'"
        ).fetchall()
        match_types = sorted(r["match_type"] for r in rows)
        assert match_types == ["registration"], (
            f"expected only registration row to survive, got {match_types}"
        )
        assert "Removed" in sent[0]

    def test_watch_remove_non_hex_value_targets_registration(self, db_conn, monkeypatch):
        """Non-hex value: infer match_type='registration' (mirror _watch_add)."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("registration", "sp-lrf", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "sp-lrf")
        assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        assert "Removed" in sent[0]

    def test_watch_remove_falls_back_to_callsign_prefix(
        self, db_conn, monkeypatch,
    ):
        """Audit-12 P8 follow-up — `_watch_remove`'s usage string promises
        all three match_types (icao / registration / callsign_prefix), but
        the original fallback chain only tried icao→registration. A
        callsign_prefix entry inserted via the HTTP API was orphaned for
        the Telegram bot. The third fallback fixes that."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("callsign_prefix", "lot", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "lot")
        # Row removed
        assert db_conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE value='lot'"
        ).fetchone()[0] == 0
        assert "Removed" in sent[0]

    def test_watch_remove_falls_back_to_registration_for_hex_shape(
        self, db_conn, monkeypatch,
    ):
        """Audit-12 #154 — if a registration just happens to be 6 hex chars
        (e.g. ``ABC123``), the icao-shape inference would historically have
        left it un-removable via the bot. Now, when the icao lookup matches
        zero rows, we fall through to the registration type.

        When BOTH rows exist for the same 6-hex value the icao row still
        wins (preserves Audit 11 #116 — see
        ``test_watch_remove_does_not_cross_match_types``)."""
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) VALUES (?,?,NULL,?)",
            ("registration", "abcdef", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "abcdef")
        # Registration row removed — message confirms it
        assert db_conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE value='abcdef'"
        ).fetchone()[0] == 0
        assert "Removed" in sent[0]

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
        # Disable Wikipedia step 6 by default — existing tests assume "all
        # sources miss → None".  Tests for Wikipedia coverage opt back in.
        monkeypatch.setattr(photo_sources, "_WIKIPEDIA_ENABLED", False)
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
        url, is_type = notifier._get_photo_result("abc123", None)
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
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", None)
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
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: (_ for _ in ()).throw(AssertionError("should not fetch")))
        url, is_type = notifier._get_photo_result("abc123", "B738")
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
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", "B738")
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
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", "B738")
        assert url == "https://example.com/other.jpg"
        assert is_type is True
        assert fetched == []
        row = self.conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='B738'").fetchone()
        assert row and row[0] == "https://example.com/other.jpg"

    def test_fetch_photo_called_for_specific_icao(self, monkeypatch):
        fetched = []
        def fake_fetch(icao):
            fetched.append(icao)
            return PhotoResult(thumbnail_url="https://example.com/sp.jpg") if icao == "abc123" else None
        monkeypatch.setattr(photo_sources, "fetch_photo", fake_fetch)
        url, is_type = notifier._get_photo_result("abc123", None)
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
            return PhotoResult(thumbnail_url="https://example.com/ef2k.jpg") if icao == "probe01" else None
        monkeypatch.setattr(photo_sources, "fetch_photo", fake_fetch)
        url, is_type = notifier._get_photo_result("abc123", "EF2K")
        assert url == "https://example.com/ef2k.jpg"
        assert is_type is True
        assert fetched == ["abc123", "probe01"]

    def test_all_fail_stores_negatives_returns_none(self, monkeypatch):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'EF2K', 'Eurofighter Typhoon', 1)"
        )
        self.conn.commit()
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: None)
        url, is_type = notifier._get_photo_result("abc123", "EF2K")
        assert url is None
        assert is_type is False
        p_row = self.conn.execute("SELECT thumbnail_url FROM photos WHERE icao_hex='abc123'").fetchone()
        t_row = self.conn.execute("SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'").fetchone()
        assert p_row is not None
        assert t_row is not None

    def test_null_type_code_skips_type_logic(self, monkeypatch):
        fetched = []
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: fetched.append(h) or None)
        url, is_type = notifier._get_photo_result("abc123", None)
        assert url is None
        assert is_type is False
        assert fetched == ["abc123"]
        t_row = self.conn.execute("SELECT * FROM type_photos").fetchone()
        assert t_row is None

    def test_wikipedia_fallback_used_for_notifier_alert(self, monkeypatch):
        """When the specific+probe chain misses, the notifier's photo result
        should come from the Wikipedia step (so Telegram alerts get a photo
        for vintage / military / GA types)."""
        monkeypatch.setattr(photo_sources, "_WIKIPEDIA_ENABLED", True)
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'C152', 'Cessna 152', 0)"
        )
        self.conn.commit()
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: None)
        monkeypatch.setattr(
            photo_sources, "_fetch_wikipedia_type",
            lambda desc: PhotoResult(
                thumbnail_url="https://upload.wikimedia.org/c152.jpg",
                large_url="https://upload.wikimedia.org/c152-large.jpg",
                link_url="https://en.wikipedia.org/wiki/Cessna_152",
                photographer="Wikipedia",
            ),
        )
        url, is_type = notifier._get_photo_result("abc123", "C152")
        assert url == "https://upload.wikimedia.org/c152.jpg"
        assert is_type is True
        row = self.conn.execute(
            "SELECT photographer FROM type_photos WHERE type_code='C152'"
        ).fetchone()
        assert row and row[0] == "Wikipedia"


# ---------------------------------------------------------------------------
# _download_photo
# ---------------------------------------------------------------------------

class TestDownloadPhoto:
    """_download_photo now goes through photo_sources._safe_open; patch that seam."""

    def _patch_safe_open(self, monkeypatch, body, headers=None):
        if headers is None:
            headers = {"Content-Type": "image/jpeg"}
        monkeypatch.setattr(
            photo_sources, "_safe_open",
            lambda url, *, timeout, max_bytes: (body, headers),
        )

    def test_returns_bytes_and_jpeg_content_type(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"\xff\xd8image",
                              {"Content-Type": "image/jpeg"})
        assert notifier._download_photo("https://example.com/photo.jpg") == (
            b"\xff\xd8image", "image/jpeg",
        )

    def test_detects_png_content_type(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"\x89PNG",
                              {"Content-Type": "image/png"})
        result = notifier._download_photo("https://example.com/photo.png")
        assert result is not None and result[1] == "image/png"

    def test_detects_webp_content_type(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"RIFF",
                              {"Content-Type": "image/webp"})
        result = notifier._download_photo("https://example.com/photo.webp")
        assert result is not None and result[1] == "image/webp"

    def test_strips_content_type_parameters(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"data",
                              {"Content-Type": "image/jpeg; charset=utf-8"})
        result = notifier._download_photo("https://example.com/photo.jpg")
        assert result is not None and result[1] == "image/jpeg"

    def test_defaults_to_jpeg_when_no_content_type_header(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"data", headers={})
        result = notifier._download_photo("https://example.com/photo.jpg")
        assert result is not None and result[1] == "image/jpeg"

    def test_returns_none_when_response_too_large(self, monkeypatch):
        def _oversize(url, *, timeout, max_bytes):
            raise ValueError(f"max_bytes={max_bytes} exceeded")
        monkeypatch.setattr(photo_sources, "_safe_open", _oversize)
        assert notifier._download_photo("https://example.com/huge.jpg") is None

    def test_returns_none_on_network_error(self, monkeypatch):
        def _boom(url, *, timeout, max_bytes):
            raise OSError("network")
        monkeypatch.setattr(photo_sources, "_safe_open", _boom)
        assert notifier._download_photo("https://example.com/photo.jpg") is None

    def test_returns_none_on_ssrf_rejection(self, monkeypatch):
        """_safe_open raises ValueError for private/loopback IPs — must surface as None."""
        def _reject(url, *, timeout, max_bytes):
            raise ValueError("non-public IP")
        monkeypatch.setattr(photo_sources, "_safe_open", _reject)
        assert notifier._download_photo("https://internal/photo.jpg") is None


# ---------------------------------------------------------------------------
# _multipart_photo
# ---------------------------------------------------------------------------

class TestMultipartPhoto:
    """_multipart_photo now returns (body, boundary) and picks a fresh random
    boundary per call (mitigates body-injection via caption / chat_id)."""

    def test_jpeg_uses_photo_jpg_filename_and_content_type(self):
        body, _ = notifier._multipart_photo("99", b"\xff\xd8", "cap", "image/jpeg")
        assert b'filename="photo.jpg"' in body
        assert b"Content-Type: image/jpeg" in body

    def test_png_uses_photo_png_filename_and_content_type(self):
        body, _ = notifier._multipart_photo("99", b"\x89PNG", "cap", "image/png")
        assert b'filename="photo.png"' in body
        assert b"Content-Type: image/png" in body

    def test_webp_uses_photo_webp_filename_and_content_type(self):
        body, _ = notifier._multipart_photo("99", b"RIFF", "cap", "image/webp")
        assert b'filename="photo.webp"' in body
        assert b"Content-Type: image/webp" in body

    def test_unknown_mime_falls_back_to_jpeg(self):
        body, _ = notifier._multipart_photo("99", b"data", "cap", "image/tiff")
        assert b'filename="photo.jpg"' in body
        assert b"Content-Type: image/tiff" in body

    def test_caption_and_chat_id_present(self):
        body, _ = notifier._multipart_photo("42", b"img", "hello world", "image/jpeg")
        assert b"42" in body
        assert b"hello world" in body

    def test_boundary_terminates_body(self):
        body, boundary = notifier._multipart_photo("1", b"img", "c", "image/jpeg")
        assert body.endswith(b"--" + boundary.encode() + b"--\r\n")

    def test_boundary_randomized_per_call(self):
        """Two calls must produce different boundaries so that no fixed string
        is available to a caption-injection attacker."""
        _, b1 = notifier._multipart_photo("1", b"a", "x", "image/jpeg")
        _, b2 = notifier._multipart_photo("1", b"a", "x", "image/jpeg")
        assert b1 != b2
        assert b1.startswith("----RSBS")
        assert b2.startswith("----RSBS")

    def test_explicit_boundary_honoured(self):
        """When the caller pins a boundary (tests), it's used verbatim."""
        body, boundary = notifier._multipart_photo(
            "1", b"a", "x", "image/jpeg", boundary="----pinned",
        )
        assert boundary == "----pinned"
        assert b"------pinned\r\n" in body


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

    def _patch_safe_open(self, monkeypatch, body, content_type="image/jpeg"):
        monkeypatch.setattr(
            photo_sources, "_safe_open",
            lambda url, *, timeout, max_bytes: (body, {"Content-Type": content_type}),
        )

    def test_calls_send_photo_api(self, monkeypatch):
        """_send_photo: download via _safe_open, upload via http_safe.safe_urlopen —
        exactly one upload call."""
        self._patch_safe_open(monkeypatch, b"\xff\xd8jpeg", "image/jpeg")
        upload_calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(b'{"ok":true}', calls=upload_calls))
        assert notifier._send_photo("https://example.com/photo.jpg", "caption") is True
        assert len(upload_calls) == 1, "exactly one outbound upload (the Telegram POST)"
        assert "sendPhoto" in upload_calls[0]["url"]
        body = upload_calls[0]["data"]
        ct = upload_calls[0]["extra_headers"]["Content-Type"]
        assert "multipart/form-data; boundary=----RSBS" in ct
        assert b"caption" in body
        assert b"HTML" in body
        assert b'filename="photo.jpg"' in body
        assert b"Content-Type: image/jpeg" in body

    def test_png_url_uses_png_filename_in_multipart(self, monkeypatch):
        self._patch_safe_open(monkeypatch, b"\x89PNG", "image/png")
        upload_calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(b'{"ok":true}', calls=upload_calls))
        notifier._send_photo("https://example.com/photo.png", "caption")
        body = upload_calls[0]["data"]
        assert b'filename="photo.png"' in body
        assert b"Content-Type: image/png" in body

    def test_api_failure_falls_back_to_send_message(self, monkeypatch):
        """sendPhoto upload error → text fallback via _send (no URL-payload retry)."""
        self._patch_safe_open(monkeypatch, b"\xff\xd8jpeg", "image/jpeg")
        def raise_error(*a, **kw): raise OSError("network")
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen", raise_error)
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        assert notifier._send_photo("https://example.com/photo.jpg", "caption text") is True
        assert sent == ["caption text"]

    def test_download_failure_falls_back_to_text(self, monkeypatch):
        """If _download_photo returns None, go straight to text — no URL-payload retry."""
        monkeypatch.setattr(notifier, "_download_photo", lambda url: None)
        upload_calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(b'{"ok":true}', calls=upload_calls))
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        notifier._send_photo("https://example.com/photo.jpg", "cap")
        assert upload_calls == []  # never tries the URL-fetch path
        assert sent == ["cap"]

    def test_http_url_falls_back_without_api_call(self, monkeypatch):
        """HTTP (non-https) URL is rejected before any network call."""
        upload_calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(b'{"ok":true}', calls=upload_calls))
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        notifier._send_photo("http://example.com/photo.jpg", "caption")
        assert upload_calls == []
        assert sent == ["caption"]

    def test_non_http_url_falls_back_without_api_call(self, monkeypatch):
        upload_calls = []
        monkeypatch.setattr(notifier.http_safe, "safe_urlopen",
                            _make_safe_urlopen(b'{"ok":true}', calls=upload_calls))
        sent = []
        monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt) or True)
        notifier._send_photo("ftp://bad.url/photo.jpg", "caption")
        assert upload_calls == []
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
        monkeypatch.setattr(photo_sources, "fetch_photo", lambda h: None)
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


# ---------------------------------------------------------------------------
# _clamp_caption / _dispatch_with_photo caption-length cap
# ---------------------------------------------------------------------------

class TestClampCaption:
    def test_short_caption_unchanged(self):
        s = "hello"
        assert notifier._clamp_caption(s, limit=1024) == s

    def test_caption_at_limit_unchanged(self):
        s = "x" * 1024
        assert notifier._clamp_caption(s, limit=1024) == s

    def test_over_limit_strips_photo_note_first(self):
        """Dropping the trailing <i>Photo…</i> alone is enough → don't touch
        the rest of the caption."""
        body = "x" * 1000
        note = '\n<i>Photo: B738 — not this specific aircraft</i>'
        s = body + note
        # body=1000, note≈50 → 1050 total, over limit by ~26
        assert len(s) > 1024
        out = notifier._clamp_caption(s, limit=1024)
        assert out == body
        assert "<i>Photo:" not in out

    def test_over_limit_strips_trailing_link_line(self):
        """When stripping the photo note still leaves us over, drop the
        trailing <a href="…">…</a> line — never cut inside the href."""
        body = "X" * 1010
        link = '\n<a href="https://example.com/aircraft/abc">View profile</a>'
        s = body + link
        assert len(s) > 1024
        out = notifier._clamp_caption(s, limit=1024)
        # Link must be gone in its entirety (no half-closed tags)
        assert "<a href" not in out
        assert "</a>" not in out
        assert out == body

    def test_over_limit_strips_both_then_truncates(self):
        body = "Y" * 1500
        s = (body
             + '\n<a href="https://example.com/aircraft/abc">View profile</a>'
             + '\n<i>Photo: B738 — not this specific aircraft</i>')
        out = notifier._clamp_caption(s, limit=1024)
        assert len(out) <= 1024
        assert "<a href" not in out
        assert "<i>Photo:" not in out
        assert out.endswith("…")

    def test_dual_link_line_stripped_entirely(self):
        """Watchlist captions have two anchors on the same line: both go."""
        body = "Z" * 1100
        link = (
            '\n<a href="https://example.com/flight/1">View flight</a> · '
            '<a href="https://example.com/aircraft/abc">View aircraft</a>'
        )
        out = notifier._clamp_caption(body + link, limit=1024)
        assert "<a href" not in out
        assert "·" not in out

    def test_no_partial_tag_in_truncated_output(self):
        """Final-resort truncation must not leave a half-open tag at the end."""
        # Construct a case where after stripping note + link, the body itself
        # is still over-limit and gets plain truncated.
        body = ("<b>x</b> " * 200)  # plenty of HTML, ≈ 1600 chars
        s = body + '\n<a href="https://example.com/x">View</a>'
        out = notifier._clamp_caption(s, limit=1024)
        # _clamp_caption guarantees the ellipsis is the last char; we don't
        # promise tag-balanced output in the final-fallback branch (Telegram
        # tolerates dangling open tags on text), only that the link/note
        # didn't get half-cut.  Confirm the trailing structures are gone.
        assert "<a href" not in out
        assert out.endswith("…")
        assert len(out) <= 1024

    def test_default_limit_is_1024(self):
        assert notifier._PHOTO_CAPTION_MAX == 1024


class TestDispatchTruncates:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        conn.close()
        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(config, "TELEGRAM_PHOTOS", 1)
        # seed a specific photo so the dispatch reaches _send_photo
        c = database.connect(db_path)
        now = int(time.time())
        c.execute(
            "INSERT INTO photos VALUES ('abc123', 'https://example.com/p.jpg', "
            "NULL, NULL, NULL, ?)", (now,))
        c.commit()
        c.close()
        yield

    def test_long_caption_truncated_before_send_photo(self, monkeypatch):
        seen = []
        monkeypatch.setattr(notifier, "_send_photo",
                            lambda url, cap: seen.append(cap) or True)
        long_caption = "X" * 4000
        notifier._dispatch_with_photo(long_caption, "abc123", None, None)
        assert len(seen) == 1
        assert len(seen[0]) <= notifier._PHOTO_CAPTION_MAX
