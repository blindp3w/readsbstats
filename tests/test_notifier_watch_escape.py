"""Regression for W-1 (Audit 2026-06-01): /watch and /unwatch confirmations
must HTML-escape user-supplied values before interpolating into Telegram's
HTML parse mode. Without escaping, a value containing `<`, `>`, or `&` causes
Telegram to reject the message with HTTP 400 and the confirmation is silently
dropped.

Per CLAUDE.md / notifier conventions: every dynamic field interpolated into a
Telegram caption must pass through `notifier._h(...)`.
"""
from __future__ import annotations

import time

import pytest

from readsbstats import notifier
from tests._helpers import make_db


@pytest.fixture()
def db_conn():
    conn = make_db()
    yield conn
    conn.close()


def _sent_capture(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notifier, "_send", lambda txt: sent.append(txt))
    return sent


class TestWatchAddEscapes:
    def test_added_message_escapes_lt_gt_amp(self, db_conn, monkeypatch):
        sent = _sent_capture(monkeypatch)
        # registration-shape value (fails the 6-hex regex) with HTML metachars
        notifier._watch_add(db_conn, "a<b&c")
        assert sent, "expected at least one outbound message"
        payload = sent[-1]
        assert "Added" in payload
        # Must contain escaped entities, never raw metachars in the dynamic field
        assert "&lt;" in payload
        assert "&amp;" in payload
        # The raw substring `A<B&C` (uppercased dynamic field) must NOT appear
        assert "A<B&C" not in payload

    def test_already_watching_escapes(self, db_conn, monkeypatch):
        sent = _sent_capture(monkeypatch)
        # First add — succeeds. Second add — "Already watching" branch.
        notifier._watch_add(db_conn, "a<b&c")
        sent.clear()
        notifier._watch_add(db_conn, "a<b&c")
        assert sent and "Already" in sent[0]
        assert "&lt;" in sent[0]
        assert "&amp;" in sent[0]
        assert "A<B&C" not in sent[0]


class TestWatchRemoveEscapes:
    def test_removed_message_escapes(self, db_conn, monkeypatch):
        sent = _sent_capture(monkeypatch)
        # Seed a watchlist row whose value contains HTML metachars, then remove.
        db_conn.execute(
            "INSERT INTO watchlist (match_type, value, label, created_at) "
            "VALUES (?,?,NULL,?)",
            ("registration", "a<b&c", int(time.time())),
        )
        db_conn.commit()
        notifier._watch_remove(db_conn, "a<b&c")
        assert sent and "Removed" in sent[0]
        assert "&lt;" in sent[0]
        assert "&amp;" in sent[0]
        assert "A<B&C" not in sent[0]

    def test_not_in_watchlist_escapes(self, db_conn, monkeypatch):
        sent = _sent_capture(monkeypatch)
        # Nothing seeded — _watch_remove falls through and emits "Not in".
        notifier._watch_remove(db_conn, "a<b&c")
        assert sent and "Not in watchlist" in sent[0]
        assert "&lt;" in sent[0]
        assert "&amp;" in sent[0]
        assert "A<B&C" not in sent[0]
