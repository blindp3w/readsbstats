"""Tests for DB integrity-check logic: collector._startup_integrity_check and check_db.py."""

import os
import pathlib
import sqlite3
import sys
import tempfile

import pytest

from readsbstats import collector, database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_file_db(path: str) -> sqlite3.Connection:
    """Create a fresh on-disk DB with full schema and enough data to fill 100+ pages."""
    conn = database.connect(path)
    conn.executescript(database.DDL)
    database._migrate(conn)
    # 2000 flight rows → ~150 pages of B-tree data, enough to corrupt deep pages.
    for i in range(2000):
        conn.execute(
            "INSERT INTO flights (icao_hex, callsign, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (f"aa{i:04x}", f"CALL{i}", 1000 + i, 2000 + i),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return conn


def corrupt_db_file(path: str) -> None:
    """Overwrite bytes inside a deep B-tree page to trigger quick_check failure.

    Pages near the start (header, sqlite_master, small indexes) cause
    "database is malformed" on open. Page 60+ is solidly in flight-data
    territory where quick_check returns error rows rather than the open
    call itself failing.
    """
    page_size = 4096
    with open(path, "r+b") as f:
        f.seek(60 * page_size + 200)
        f.write(b"\xff" * 512)


# ---------------------------------------------------------------------------
# _startup_integrity_check
# ---------------------------------------------------------------------------

class TestStartupIntegrityCheck:
    """Use a FakeConn to test the helper's branching logic in isolation."""

    def test_clean_db_removes_sentinel_and_checkpoints(self, tmp_path):
        sentinel = tmp_path / ".dirty"
        sentinel.touch()
        calls = []

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

        class FakeConn:
            def execute(self, sql):
                calls.append(sql)
                if "quick_check" in sql:
                    return FakeCursor([("ok",)])
                if "wal_checkpoint" in sql:
                    return FakeCursor([(0, 5, 5)])  # busy=0, log=5, checkpointed=5
                raise AssertionError(f"unexpected SQL: {sql}")

        collector._startup_integrity_check(FakeConn(), sentinel)
        assert not sentinel.exists()
        assert any("quick_check" in s for s in calls)
        assert any("wal_checkpoint(TRUNCATE)" in s for s in calls)

    def test_corrupt_db_raises_and_keeps_sentinel(self, tmp_path, caplog):
        sentinel = tmp_path / ".dirty"
        sentinel.touch()

        class FakeCursor:
            def fetchall(self):
                return [("malformed page 42",), ("wrong # of entries",)]

        class FakeConn:
            def execute(self, sql):
                return FakeCursor()

        import logging
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(collector.StartupIntegrityError):
                collector._startup_integrity_check(FakeConn(), sentinel)
        assert sentinel.exists()  # NOT removed — operator should see it again next boot
        assert any("CORRUPTION DETECTED" in rec.message for rec in caplog.records)

    def test_pragma_exception_raises_and_keeps_sentinel(self, tmp_path, caplog):
        sentinel = tmp_path / ".dirty"
        sentinel.touch()

        class FakeConn:
            def execute(self, sql):
                raise sqlite3.DatabaseError("disk I/O error")

        import logging
        with caplog.at_level(logging.ERROR):
            with pytest.raises(collector.StartupIntegrityError):
                collector._startup_integrity_check(FakeConn(), sentinel)
        assert sentinel.exists()  # not removed on error
        assert any("quick_check failed to run" in rec.message for rec in caplog.records)

    def test_checkpoint_busy_logs_debug(self, tmp_path, caplog):
        sentinel = tmp_path / ".dirty"
        sentinel.touch()

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0]

        class FakeConn:
            def execute(self, sql):
                if "quick_check" in sql:
                    return FakeCursor([("ok",)])
                # checkpoint returns busy=1
                return FakeCursor([(1, 10, 5)])

        import logging
        with caplog.at_level(logging.DEBUG):
            collector._startup_integrity_check(FakeConn(), sentinel)
        assert not sentinel.exists()  # clean → removed even on partial checkpoint
        assert any("WAL checkpoint partial" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# main() fail-closed behaviour on startup integrity failure (BE-1)
# ---------------------------------------------------------------------------

class TestMainFailClosed:
    """On StartupIntegrityError, main() must alert + exit(2) BEFORE any
    background service or the poll loop starts."""

    def test_integrity_failure_exits_before_background_starts(self, tmp_path, monkeypatch):
        from readsbstats import notifier

        sentinel = tmp_path / ".dirty_shutdown"
        sentinel.touch()
        monkeypatch.setattr(collector, "_SENTINEL", sentinel)

        called: list[str] = []
        sent_alerts: list[str] = []
        sd_msgs: list[str] = []

        monkeypatch.setattr(database, "init_db", lambda: called.append("init_db"))
        monkeypatch.setattr(database, "connect", lambda *a, **k: object())
        monkeypatch.setattr(
            collector, "_startup_integrity_check",
            lambda conn, s: (_ for _ in ()).throw(
                collector.StartupIntegrityError("corruption: malformed page 42")
            ),
        )
        # Anything below the integrity check must NOT run.
        monkeypatch.setattr(collector, "_load_active", lambda *a: called.append("load_active"))
        monkeypatch.setattr(collector, "_load_notified", lambda *a: called.append("load_notified"))
        monkeypatch.setattr(
            collector.adsbx_enricher, "start_background_enricher",
            lambda *a, **k: called.append("adsbx"),
        )
        monkeypatch.setattr(
            collector.route_enricher, "start_background_enricher",
            lambda *a, **k: called.append("route_enricher"),
        )
        monkeypatch.setattr(
            collector.metrics_collector, "start_metrics_collector",
            lambda *a, **k: called.append("metrics"),
        )
        monkeypatch.setattr(
            collector, "start_notification_consumer",
            lambda *a, **k: called.append("notify_consumer"),
        )
        monkeypatch.setattr(notifier, "telegram_enabled", lambda: True)
        monkeypatch.setattr(notifier, "_send", lambda text: sent_alerts.append(text) or True)
        monkeypatch.setattr(collector, "_sd_notify", lambda msg: sd_msgs.append(msg))

        with pytest.raises(SystemExit) as ei:
            collector.main()

        assert ei.value.code == 2
        assert called == ["init_db"]  # nothing past the integrity check ran
        assert sentinel.exists()  # sentinel retained for operator
        assert sent_alerts and "integrity" in sent_alerts[0].lower()
        assert any("STATUS=" in m and "integrity" in m.lower() for m in sd_msgs)
        assert "READY=1" not in sd_msgs


# ---------------------------------------------------------------------------
# check_db.py script — exit codes
# ---------------------------------------------------------------------------

class TestCheckDbScript:
    """Invoke check_db.main() via monkeypatched sys.argv and assert SystemExit codes."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        yield
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + ext)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def _run(self, db_path: str, mode: str = "quick") -> int:
        import check_db
        argv = ["check_db.py", "--db", db_path, "--mode", mode]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "argv", argv)
            try:
                check_db.main()
            except SystemExit as e:
                return e.code if isinstance(e.code, int) else 0
        return 0  # main() returned without sys.exit (shouldn't happen)

    def test_clean_db_quick_exits_0(self, capsys):
        conn = make_file_db(self.db_path)
        conn.close()
        rc = self._run(self.db_path, mode="quick")
        captured = capsys.readouterr()
        assert rc == 0
        assert "OK" in captured.out

    def test_clean_db_full_exits_0(self, capsys):
        conn = make_file_db(self.db_path)
        conn.close()
        rc = self._run(self.db_path, mode="full")
        captured = capsys.readouterr()
        assert rc == 0
        assert "OK" in captured.out

    def test_corrupt_db_exits_1(self, capsys):
        conn = make_file_db(self.db_path)
        conn.close()
        corrupt_db_file(self.db_path)
        rc = self._run(self.db_path, mode="quick")
        captured = capsys.readouterr()
        assert rc == 1
        assert "CORRUPTION DETECTED" in captured.err

    def test_missing_db_exits_2(self, capsys):
        # Path does not exist
        rc = self._run(os.path.join(self.tmpdir, "nonexistent.db"), mode="quick")
        captured = capsys.readouterr()
        assert rc == 2
        assert "ERROR" in captured.err
