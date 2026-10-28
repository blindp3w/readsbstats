"""Tests for DB integrity-check logic: collector._startup_integrity_check and check_db.py."""

import os
import re
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

    def test_clean_db_keeps_sentinel_and_checkpoints(self, tmp_path):
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
        assert sentinel.exists()   # removed only by a clean shutdown in main()
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
        assert sentinel.exists()  # clean → kept; only a clean shutdown removes it
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
        # Fail closed: init_db() WRITES (DDL, migrations, INSERT), so it must
        # not run before the check passes, and nothing past the check runs.
        assert called == []
        assert sentinel.exists()  # sentinel retained for operator
        assert sent_alerts and "integrity" in sent_alerts[0].lower()
        assert any("STATUS=" in m and "integrity" in m.lower() for m in sd_msgs)
        assert "READY=1" not in sd_msgs

    def test_integrity_alert_send_failure_still_exits_2(self, tmp_path, monkeypatch, caplog):
        """A failing Telegram send must not mask the fail-closed exit: the
        exception is logged and main() still exits 2 with the sentinel kept."""
        import logging
        from readsbstats import notifier

        sentinel = tmp_path / ".dirty_shutdown"
        sentinel.touch()
        monkeypatch.setattr(collector, "_SENTINEL", sentinel)
        monkeypatch.setattr(database, "init_db", lambda: None)
        monkeypatch.setattr(database, "connect", lambda *a, **k: object())
        monkeypatch.setattr(
            collector, "_startup_integrity_check",
            lambda conn, s: (_ for _ in ()).throw(
                collector.StartupIntegrityError("quick_check found 3 issue(s)")
            ),
        )
        monkeypatch.setattr(
            notifier, "_send",
            lambda text: (_ for _ in ()).throw(RuntimeError("telegram down")),
        )
        sd_msgs: list[str] = []
        monkeypatch.setattr(collector, "_sd_notify", lambda msg: sd_msgs.append(msg))

        with caplog.at_level(logging.ERROR, logger="collector"):
            with pytest.raises(SystemExit) as ei:
                collector.main()

        assert ei.value.code == 2
        assert sentinel.exists()
        assert any("Failed to send DB integrity alert" in rec.message
                   for rec in caplog.records)
        assert any("STATUS=" in m and "integrity" in m.lower() for m in sd_msgs)


# ---------------------------------------------------------------------------
# main() ordering / interruption / start-timeout extension
# ---------------------------------------------------------------------------

class _FakeCloseConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestMainStartupCheckFlow:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        self.sentinel = tmp_path / ".dirty_shutdown"
        self.sentinel.touch()
        monkeypatch.setattr(collector, "_SENTINEL", self.sentinel)
        self.called: list[str] = []
        self.sd_msgs: list[str] = []
        monkeypatch.setattr(database, "init_db", lambda: self.called.append("init_db"))
        monkeypatch.setattr(collector, "_sd_notify", lambda m: self.sd_msgs.append(m))
        for name in ("_load_active", "_load_notified", "_load_last_summary_date"):
            monkeypatch.setattr(collector, name, lambda *a, n=name: self.called.append(n))
        monkeypatch.setattr(
            collector.adsbx_enricher, "start_background_enricher",
            lambda *a, **k: self.called.append("adsbx"))
        monkeypatch.setattr(
            collector.route_enricher, "start_background_enricher",
            lambda *a, **k: self.called.append("route_enricher"))
        monkeypatch.setattr(
            collector.metrics_collector, "start_metrics_collector",
            lambda *a, **k: self.called.append("metrics"))
        monkeypatch.setattr(
            collector, "start_notification_consumer",
            lambda *a, **k: self.called.append("notify_consumer"))
        yield
        collector._running = True

    def test_interrupted_check_exits_0_without_touching_anything(self, monkeypatch, caplog):
        import logging
        conn = _FakeCloseConn()
        monkeypatch.setattr(database, "connect", lambda *a, **k: conn)
        monkeypatch.setattr(
            collector.startup_check, "run_quick_check", lambda *a, **k: None)

        with caplog.at_level(logging.WARNING, logger="collector"):
            with pytest.raises(SystemExit) as ei:
                collector.main()

        assert ei.value.code == 0
        assert self.sentinel.exists()      # kept: the check never finished
        assert self.called == []           # no init_db, no loaders, no threads
        assert "READY=1" not in self.sd_msgs
        assert conn.closed
        assert any("quick_check interrupted by shutdown; sentinel kept" in r.getMessage()
                   for r in caplog.records)

    def test_check_runs_before_init_db(self, monkeypatch):
        order: list[str] = []
        conn = _FakeCloseConn()
        monkeypatch.setattr(database, "connect", lambda *a, **k: conn)
        monkeypatch.setattr(database, "init_db", lambda: order.append("init_db"))
        monkeypatch.setattr(
            collector, "_startup_integrity_check",
            lambda c, s: order.append("check"))

        class _Done(Exception):
            pass

        def stop_at_ready(msg):
            if msg == "READY=1":
                raise _Done
        monkeypatch.setattr(collector, "_sd_notify", stop_at_ready)
        monkeypatch.setattr(collector, "_load_active", lambda *a: None)
        monkeypatch.setattr(collector.notifier, "telegram_enabled", lambda: False)
        with pytest.raises(_Done):
            collector.main()
        assert order == ["check", "init_db"]

    def test_slow_check_extends_start_timeout_before_ready(self, monkeypatch, tmp_path):
        import threading
        from readsbstats import startup_check

        release = threading.Event()

        class SlowConn:
            def execute(self, sql):
                if "quick_check" in sql:
                    release.wait(5)
                    return self
                return self

            def fetchall(self):
                return [("ok",)]

            def fetchone(self):
                return (0, 0, 0)

            def interrupt(self):
                release.set()

            def close(self):
                pass

        threading.Timer(0.3, release.set).start()
        monkeypatch.setattr(database, "connect", lambda *a, **k: SlowConn())
        real = startup_check.run_quick_check
        monkeypatch.setattr(
            collector.startup_check, "run_quick_check",
            lambda conn, **kw: real(conn, **{**kw, "heartbeat_sec": 0.02, "poll_sec": 0.02}))

        class _Done(Exception):
            pass

        def stop_at_ready(msg):
            self.sd_msgs.append(msg)
            if msg == "READY=1":
                raise _Done
        monkeypatch.setattr(collector, "_sd_notify", stop_at_ready)
        monkeypatch.setattr(collector.notifier, "telegram_enabled", lambda: False)

        with pytest.raises(_Done):
            collector.main()

        ready = self.sd_msgs.index("READY=1")
        extends = [i for i, m in enumerate(self.sd_msgs) if m.startswith("EXTEND_TIMEOUT_USEC=")]
        assert len(extends) >= 2 and all(i < ready for i in extends)
        # main() owns the run-in-progress sentinel: a passing check must not
        # remove it, or a crash during this run would go undetected.
        assert self.sentinel.exists()


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

    def test_reports_duration(self, capsys):
        # The weekly timer's TimeoutStartSec is sized from these numbers; the
        # journal must carry them on success and on corruption alike.
        conn = make_file_db(self.db_path)
        conn.close()
        self._run(self.db_path, mode="quick")
        assert re.search(r"passed in \d+ s", capsys.readouterr().out)
        corrupt_db_file(self.db_path)
        self._run(self.db_path, mode="quick")
        assert re.search(r"in \d+ s", capsys.readouterr().err)

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
